"""Offline synthetic DB + WAV bundle. No HTTP route, encryption or live restore."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import CommandError
from django.db import connections

from inquiries.backup_schema import validate_model_schema
from config.restore_guard import quarantine_copy, require_quarantined, STATE
from speech_workflow.audio import MAX_AUDIO_BYTES, inspect_audio
from speech_workflow.storage import private_audio_storage
from .local_backup import _snapshot_to_temporary, validate_database, validate_external_path

SCHEMA = 'izzi-internal-voice-backup-v1'
RESTORE_SCHEMA = 'izzi-internal-voice-restore-v2'
MAX_DB = 64 * 1024 * 1024
MAX_AUDIO_FILES = 32
MAX_MANIFEST = 64 * 1024
MAX_BUNDLE = MAX_DB + MAX_AUDIO_FILES * MAX_AUDIO_BYTES + 2 * MAX_MANIFEST
ERROR = 'Резервная копия недоступна, повреждена или несовместима; восстановление не опубликовано.'


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def _operator(actor, synthetic_only):
    if (synthetic_only is not True or not getattr(actor, 'is_authenticated', False)
            or not getattr(actor, 'pk', None)
            or not get_user_model().objects.filter(pk=actor.pk, is_active=True, is_superuser=True).exists()):
        raise PermissionDenied('Нужен активный локальный оператор и явное подтверждение синтетического сценария.')


def _path(raw):
    path = Path(os.path.abspath(Path(raw).expanduser()))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise CommandError(ERROR)
    return validate_external_path(path, label='Внутренняя резервная копия')


def _read(path, maximum):
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError('file')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if not raw or len(raw) > maximum:
        raise ValueError('size')
    return raw


def _write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as stream:
        os.chmod(path, 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _catalog(path):
    """Verify SQLite structure and embedded hashes, then derive audio paths from DB."""
    validate_database(path)
    with closing(sqlite3.connect(f'file:{quote(path.as_posix(), safe="/")}?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        for app in ('speech_workflow', 'presentations'):
            validate_model_schema(db, app)
        rows = list(db.execute('SELECT id,owner_id,audio,audio_sha256,duration_seconds FROM speech_workflow_voicesubmission ORDER BY id'))
        if len(rows) > MAX_AUDIO_FILES:
            raise ValueError('count')
        catalog = []
        for row in rows:
            sid = row['id']
            if not re.fullmatch('[0-9a-f]{32}', sid) or not re.fullmatch('[0-9a-f]{64}', row['audio_sha256']):
                raise ValueError('source')
            dashed = f'{sid[:8]}-{sid[8:12]}-{sid[12:16]}-{sid[16:20]}-{sid[20:]}'
            if not re.fullmatch(re.escape(f"{row['owner_id']}/{dashed}/") + '[0-9a-f]{32}\\.wav', row['audio']):
                raise ValueError('path')
            catalog.append({'path': row['audio'], 'sha256': row['audio_sha256'], 'duration': row['duration_seconds']})
        for row in db.execute('SELECT inquiry_id,snapshot,digest FROM inquiries_version'):
            snap = json.loads(row['snapshot'])
            if _hash(_json(snap)) != row['digest']:
                raise ValueError('version')
            if snap.get('schema') == 2:
                voice = snap['voice']
                source = db.execute('''SELECT s.audio_sha256,d.content_hash,r.content_hash
                    FROM speech_workflow_voicesubmission s
                    JOIN inquiries_voicesourcelink l ON l.submission_id=s.id
                    JOIN speech_workflow_transcriptrevision r ON r.submission_id=s.id
                    JOIN speech_workflow_voiceinquirydraft d ON d.transcript_revision_id=r.id
                    WHERE l.inquiry_id=? AND s.id=? AND d.id=? AND r.id=?''',
                    (row['inquiry_id'], voice['submission_id'].replace('-', ''),
                     voice['draft_id'].replace('-', ''), voice['transcript_revision_id'])).fetchone()
                if source is None or tuple(source) != (voice['source_sha256'], voice['draft_hash'], voice['transcript_hash']):
                    raise ValueError('voice version binding')
        for row in db.execute('SELECT snapshot,content_hash,pdf_bytes,pdf_hash FROM presentations_release'):
            if _hash(_json(json.loads(row['snapshot']))) != row['content_hash'] or _hash(row['pdf_bytes']) != row['pdf_hash']:
                raise ValueError('release')
        # Foreign keys alone do not prevent a cross-owner source/selection association.
        invalid = db.execute('''SELECT 1 FROM inquiries_voicesourcelink l
            JOIN inquiries_inquiry i ON i.id=l.inquiry_id
            JOIN speech_workflow_voicesubmission s ON s.id=l.submission_id
            WHERE i.owner_id != s.owner_id LIMIT 1''').fetchone()
        mismatch = db.execute('''SELECT 1 FROM inquiries_voiceselection v
            JOIN speech_workflow_voiceinquirydraft d ON d.id=v.draft_id
            JOIN speech_workflow_transcriptrevision r ON r.id=d.transcript_revision_id
            LEFT JOIN inquiries_voicesourcelink a ON a.submission_id=r.submission_id
            LEFT JOIN inquiries_selectionlink l ON l.selection_id=v.selection_id
            JOIN presentations_travelrequest t ON t.id=v.selection_id
            LEFT JOIN inquiries_inquiry i ON i.id=l.inquiry_id
            WHERE a.id IS NULL OR l.id IS NULL OR a.inquiry_id != l.inquiry_id
               OR t.owner_id != i.owner_id LIMIT 1''').fetchone()
        if invalid or mismatch:
            raise ValueError('binding')
        return catalog


def _check_audio(raw, record):
    info = inspect_audio(SimpleUploadedFile('synthetic.wav', raw, content_type='audio/wav'))
    if info['sha256'] != record['sha256'] or info['duration_seconds'] != record['duration']:
        raise ValueError('audio')


def create_voice_bundle(destination, *, actor, synthetic_only=False):
    """Snapshot the current disposable SQLite DB and all its private WAV originals."""
    _operator(actor, synthetic_only)
    destination = _path(destination)
    if destination.exists() or not destination.parent.is_dir():
        raise CommandError(ERROR)
    connection = connections['default']
    if connection.vendor != 'sqlite' or connection.in_atomic_block:
        raise CommandError('Нужна локальная SQLite вне открытой транзакции.')
    connection.ensure_connection()
    try:
        with tempfile.TemporaryDirectory(prefix='.izzi-backup-', dir=destination.parent) as temp:
            folder = Path(temp)
            database = _snapshot_to_temporary(connection.connection, folder / 'snapshot')
            with closing(sqlite3.connect(database)) as db:
                db.execute('PRAGMA secure_delete=ON')
                db.execute('DELETE FROM django_session')
                db.commit()
                db.execute('VACUUM')
            catalog = _catalog(database)
            db_raw = _read(database, MAX_DB)
            manifest = {'schema': SCHEMA, 'classification': 'internal-unencrypted-not-for-tourists',
                        'database': {'sha256': _hash(db_raw), 'size': len(db_raw)}, 'audio': []}
            archive = folder / 'bundle.zip'
            with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_STORED) as package:
                os.chmod(archive, 0o600)
                package.writestr('database.sqlite3', db_raw)
                for index, record in enumerate(catalog):
                    source = Path(private_audio_storage.location) / record['path']
                    raw = _read(source, MAX_AUDIO_BYTES)
                    _check_audio(raw, record)
                    entry = f'audio/{index:04d}.wav'
                    package.writestr(entry, raw)
                    manifest['audio'].append(dict(record, entry=entry, size=len(raw)))
                package.writestr('manifest.json', _json(manifest))
            _operator(actor, synthetic_only)
            os.link(archive, destination)  # atomic no-clobber publication
    except PermissionDenied:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError, sqlite3.Error, ValidationError, CommandError):
        raise CommandError(ERROR) from None
    return destination


def _manifest(package):
    infos = package.infolist()
    names = [i.filename for i in infos]
    if len(infos) > MAX_AUDIO_FILES + 2 or len(names) != len(set(names)):
        raise ValueError('members')
    for info in infos:
        maximum = MAX_DB if info.filename == 'database.sqlite3' else MAX_MANIFEST if info.filename == 'manifest.json' else MAX_AUDIO_BYTES
        if (info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 1 or
                info.file_size != info.compress_size or not 0 < info.file_size <= maximum or
                stat.S_ISLNK(info.external_attr >> 16)):
            raise ValueError('member')
    manifest = json.loads(package.read('manifest.json'))
    if (set(manifest) != {'schema', 'classification', 'database', 'audio'} or manifest['schema'] != SCHEMA or
            manifest['classification'] != 'internal-unencrypted-not-for-tourists' or not isinstance(manifest['audio'], list)):
        raise ValueError('manifest')
    if set(manifest['database']) != {'sha256', 'size'}:
        raise ValueError('database')
    expected = {'manifest.json', 'database.sqlite3'}
    for index, record in enumerate(manifest['audio']):
        if set(record) != {'path', 'sha256', 'duration', 'entry', 'size'} or record['entry'] != f'audio/{index:04d}.wav':
            raise ValueError('audio entry')
        expected.add(record['entry'])
    if set(names) != expected:
        raise ValueError('members')
    return manifest


def restore_voice_bundle(backup, entry, *, actor, synthetic_only=False):
    """Publish a NEW pointer only after complete DB+WAV validation; never activate it."""
    _operator(actor, synthetic_only)
    backup, entry = _path(backup), _path(entry)
    if entry.exists() or not entry.parent.is_dir():
        raise CommandError(ERROR)
    stage = None
    published = False
    try:
        if backup.stat().st_size > MAX_BUNDLE:
            raise ValueError('bundle size')
        stage = Path(tempfile.mkdtemp(prefix='.izzi-restored-', dir=entry.parent))
        with zipfile.ZipFile(backup, 'r') as package:
            manifest = _manifest(package)
            raw = package.read('database.sqlite3')
            if {'sha256': _hash(raw), 'size': len(raw)} != manifest['database']:
                raise ValueError('database hash')
            database = stage / 'prototype.sqlite3'
            _write(database, raw)
            catalog = _catalog(database)
            if catalog != [{k: r[k] for k in ('path', 'sha256', 'duration')} for r in manifest['audio']]:
                raise ValueError('catalog')
            for record in manifest['audio']:
                raw = package.read(record['entry'])
                if len(raw) != record['size']:
                    raise ValueError('size')
                _check_audio(raw, record)
                _write(stage / 'private-audio' / record['path'], raw)
            _write(stage / 'backup-manifest.json', _json(manifest))
        quarantine_copy(database)
        restored_database = _read(database, MAX_DB)
        pointer = stage / 'restore-entry.json'
        _write(pointer, _json({'schema': RESTORE_SCHEMA, 'directory': stage.name,
            'manifest_sha256': _hash(_json(manifest)), 'authorization': STATE,
            'database': {'sha256': _hash(restored_database), 'size': len(restored_database)}}))
        verify_restored_copy(pointer, data_root=stage)
        _operator(actor, synthetic_only)
        os.link(pointer, entry)
        published = True
    except PermissionDenied:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError, zipfile.BadZipFile, sqlite3.Error, ValidationError, CommandError):
        raise CommandError(ERROR) from None
    finally:
        if stage is not None and not published:
            shutil.rmtree(stage)
    return stage


def verify_restored_copy(entry, *, data_root=None):
    """Verify integrity and quarantine, never permission to activate the restored copy."""
    try:
        entry = _path(entry)
        pointer = json.loads(_read(entry, MAX_MANIFEST))
        if (set(pointer) != {'schema', 'directory', 'manifest_sha256', 'database', 'authorization'}
                or pointer['schema'] != RESTORE_SCHEMA or pointer['authorization'] != STATE
                or not re.fullmatch(r'\.izzi-restored-[a-z0-9_]+', pointer['directory'])):
            raise ValueError('pointer')
        root = _path(data_root if data_root is not None else entry.parent / pointer['directory'])
        if root.name != pointer['directory']:
            raise ValueError('root')
        raw = _read(root / 'backup-manifest.json', MAX_MANIFEST)
        if _hash(raw) != pointer['manifest_sha256']:
            raise ValueError('manifest hash')
        manifest = json.loads(raw)
        raw = _read(root / 'prototype.sqlite3', MAX_DB)
        if {'sha256': _hash(raw), 'size': len(raw)} != pointer['database']:
            raise ValueError('database hash')
        require_quarantined(root / 'prototype.sqlite3')
        catalog = _catalog(root / 'prototype.sqlite3')
        if catalog != [{k: r[k] for k in ('path', 'sha256', 'duration')} for r in manifest['audio']]:
            raise ValueError('catalog')
        for record in manifest['audio']:
            raw = _read(root / 'private-audio' / record['path'], MAX_AUDIO_BYTES)
            if len(raw) != record['size']:
                raise ValueError('size')
            _check_audio(raw, record)
        return root
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError, sqlite3.Error, ValidationError, CommandError):
        raise CommandError(ERROR) from None
