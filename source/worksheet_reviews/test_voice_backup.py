"""Real SQLite snapshots and files, synthetic identities/WAV only."""
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied
from django.core.management.base import CommandError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from inquiries import test_voice as helpers
from speech_workflow.models import VoiceSubmission
from . import voice_backup as backup


@override_settings(VOICE_INQUIRY_BOUNDARY_ENABLED=True)
class VoiceBundleTests(TransactionTestCase):
    source = helpers.VoiceLedgerTests.source
    draft = helpers.VoiceLedgerTests.draft
    confirm_draft = helpers.VoiceLedgerTests.confirm_draft
    prepare = helpers.VoiceLedgerTests.prepare
    approve = helpers.VoiceLedgerTests.approve
    release = helpers.VoiceLedgerTests.release

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='izzi-synthetic-bundle-test-')
        self.root = Path(self.temp.name)
        helpers.VoiceLedgerTests.setUp(self)
        self.operator = get_user_model().objects.create_user('synthetic-operator', is_superuser=True)
        self.archive = self.root / 'internal.zip'
        self.entry = self.root / 'restored.json'

    def tearDown(self):
        for item in VoiceSubmission.objects.all():
            item.audio.delete(save=False)
        self.temp.cleanup()
        super().tearDown()

    def create(self, actor=None):
        return backup.create_voice_bundle(self.archive, actor=actor or self.operator, synthetic_only=True)

    def restore(self, actor=None):
        return backup.restore_voice_bundle(self.archive, self.entry, actor=actor or self.operator, synthetic_only=True)

    def change_zip(self, mutate):
        with zipfile.ZipFile(self.archive) as z:
            entries = {n: z.read(n) for n in z.namelist()}
        mutate(entries)
        self.archive.unlink()
        with zipfile.ZipFile(self.archive, 'x') as z:
            for name, value in entries.items():
                z.writestr(name, value)

    def rejected(self):
        with self.assertRaisesMessage(CommandError, backup.ERROR):
            self.restore()
        self.assertFalse(self.entry.exists())
        self.assertEqual(list(self.root.glob('.izzi-restored-*')), [])

    def test_db_wav_confirmations_and_two_pdf_roundtrip(self):
        first = self.release(self.prepare())
        self.release(self.prepare(hotel_ids=[self.hotels[1].pk]))
        original = Path(self.item.audio.path).read_bytes()
        self.create()
        root = self.restore()
        self.assertEqual(backup.verify_restored_copy(self.entry), root)
        self.assertEqual((root / 'private-audio' / self.item.audio.name).read_bytes(), original)
        with sqlite3.connect(root / 'prototype.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM inquiries_inquiry').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM presentations_release').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM inquiries_selectionlink').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT pdf_bytes FROM presentations_release WHERE id=?', (first.pk.hex,)).fetchone()[0], bytes(first.pdf_bytes))
            self.assertEqual(db.execute('SELECT count(*) FROM speech_workflow_voiceinquirydraft WHERE confirmed_at IS NOT NULL').fetchone()[0], 1)
        self.assertEqual(Path(self.item.audio.path).read_bytes(), original)

    def test_normal_manager_cannot_export_all_owners(self):
        with self.assertRaises(PermissionDenied): self.create(self.owner)
        self.assertFalse(self.archive.exists())

    def test_normal_manager_cannot_restore(self):
        self.create()
        with self.assertRaises(PermissionDenied): self.restore(self.other)
        self.assertFalse(self.entry.exists())

    def test_anonymous_cannot_export(self):
        with self.assertRaises(PermissionDenied): self.create(AnonymousUser())

    def test_stale_active_operator_object_rechecked(self):
        get_user_model().objects.filter(pk=self.operator.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied): self.create()

    def test_synthetic_acknowledgement_required(self):
        with self.assertRaises(PermissionDenied):
            backup.create_voice_bundle(self.archive, actor=self.operator)

    def test_revoked_operator_before_publication_leaves_no_restore(self):
        self.create()
        original = backup._operator
        def recheck(actor, synthetic):
            original(actor, synthetic)
            get_user_model().objects.filter(pk=actor.pk).update(is_active=False)
        with patch.object(backup, '_operator', side_effect=recheck):
            with self.assertRaises(PermissionDenied): self.restore()
        self.assertFalse(self.entry.exists())
        self.assertFalse(list(self.root.glob('.izzi-restored-*')))

    def test_missing_original_rejects_backup(self):
        Path(self.item.audio.path).unlink()
        with self.assertRaises(CommandError): self.create()
        self.assertFalse(self.archive.exists())
        self.assertFalse(list(self.root.glob('.izzi-backup-*')))

    def test_corrupt_original_rejects_backup(self):
        Path(self.item.audio.path).write_bytes(b'not-WAV')
        with self.assertRaises(CommandError): self.create()
        self.assertFalse(self.archive.exists())

    def test_audio_symlink_is_rejected(self):
        source = Path(self.item.audio.path)
        other = self.root / 'outside.wav'
        other.write_bytes(source.read_bytes()); source.unlink(); source.symlink_to(other)
        with self.assertRaises(CommandError): self.create()

    def test_missing_audio_in_archive(self):
        self.create(); self.change_zip(lambda e: e.pop('audio/0000.wav')); self.rejected()

    def test_corrupt_audio_in_archive(self):
        self.create(); self.change_zip(lambda e: e.update({'audio/0000.wav': b'broken'})); self.rejected()

    def test_corrupt_database(self):
        self.create(); self.change_zip(lambda e: e.update({'database.sqlite3': b'broken'})); self.rejected()

    def test_unsupported_manifest(self):
        self.create()
        def change(e):
            m=json.loads(e['manifest.json']); m['schema']='future'; e['manifest.json']=json.dumps(m)
        self.change_zip(change); self.rejected()

    def test_manifest_path_traversal_even_with_correct_wav(self):
        self.create()
        def change(e):
            m=json.loads(e['manifest.json']); m['audio'][0]['path']='../outside.wav'; e['manifest.json']=json.dumps(m)
        self.change_zip(change); self.rejected()
        self.assertFalse((self.root / 'outside.wav').exists())

    def test_extra_file_or_path_not_extracted(self):
        self.create(); self.change_zip(lambda e: e.update({'../outside': b'internal'})); self.rejected()
        self.assertFalse((self.root / 'outside').exists())

    def test_duplicate_member_rejected(self):
        self.create()
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(self.archive, 'a') as z: z.writestr('audio/0000.wav', b'fake')
        self.rejected()

    def test_compressed_member_rejected(self):
        self.create()
        with zipfile.ZipFile(self.archive) as z: entries={n:z.read(n) for n in z.namelist()}
        self.archive.unlink()
        with zipfile.ZipFile(self.archive,'x',compression=zipfile.ZIP_DEFLATED) as z:
            for n,v in entries.items(): z.writestr(n,v)
        self.rejected()

    def test_oversized_archive_rejected_before_read(self):
        self.create()
        with patch.object(backup, 'MAX_BUNDLE', 1): self.rejected()

    def test_failed_audio_write_cleans_complete_stage(self):
        self.create()
        original=backup._write
        def write(path, raw):
            if path.suffix=='.wav': raise OSError('SYNTHETIC_PRIVATE_DO_NOT_LEAK')
            original(path, raw)
        with patch.object(backup,'_write',side_effect=write): self.rejected()

    def test_failed_final_validation_is_not_published(self):
        self.create()
        with patch.object(backup,'verify_restored_copy',side_effect=CommandError('private')): self.rejected()

    def test_failed_atomic_publication_cleans_stage(self):
        self.create()
        with patch.object(backup.os,'link',side_effect=OSError('private')): self.rejected()

    def test_existing_pointer_unchanged(self):
        self.create(); self.entry.write_bytes(b'keep')
        with self.assertRaises(CommandError): self.restore()
        self.assertEqual(self.entry.read_bytes(), b'keep')

    def test_racing_pointer_never_overwritten(self):
        self.create(); original=os.link
        def race(source, destination):
            Path(destination).write_bytes(b'racer'); return original(source, destination)
        with patch.object(backup.os,'link',side_effect=race):
            with self.assertRaises(CommandError): self.restore()
        self.assertEqual(self.entry.read_bytes(), b'racer')
        self.assertFalse(list(self.root.glob('.izzi-restored-*')))

    def test_source_sessions_stay_but_snapshot_sessions_and_key_absent(self):
        marker='SYNTHETIC_SESSION_PRIVATE_MARKER'
        Session.objects.create(session_key='synthetic-key',session_data=marker,expire_date=timezone.now())
        self.create(); root=self.restore()
        self.assertTrue(Session.objects.filter(session_key='synthetic-key').exists())
        raw=(root/'prototype.sqlite3').read_bytes()
        self.assertNotIn(marker.encode(),raw)
        self.assertFalse((root/'django-secret-key').exists())
        with zipfile.ZipFile(self.archive) as z:
            self.assertEqual(set(z.namelist()),{'database.sqlite3','manifest.json','audio/0000.wav'})
            self.assertIn('internal-unencrypted-not-for-tourists', z.read('manifest.json').decode())

    def test_post_restore_audio_loss_detected(self):
        self.create(); root=self.restore()
        (root/'private-audio'/self.item.audio.name).unlink()
        with self.assertRaises(CommandError): backup.verify_restored_copy(self.entry)

    def test_post_restore_database_corruption_detected(self):
        self.create(); root=self.restore(); (root/'prototype.sqlite3').write_bytes(b'corrupt')
        with self.assertRaises(CommandError): backup.verify_restored_copy(self.entry)

    def test_cross_owner_inquiry_binding_rejected(self):
        self.prepare()
        from inquiries.models import Inquiry
        Inquiry.objects.update(owner=self.other)
        with self.assertRaises(CommandError): self.create()

    def test_damaged_release_digest_rejected(self):
        release=self.release(self.prepare())
        from presentations.models import Release
        Release.objects.filter(pk=release.pk).update(pdf_hash='0'*64)
        with self.assertRaises(CommandError): self.create()

    def test_invalid_voice_version_binding_rejected_even_with_rehashed_snapshot(self):
        self.prepare()
        from inquiries.models import Version
        from inquiries.services import digest
        version=Version.objects.first(); snapshot=version.snapshot
        snapshot['voice']['submission_id']='00000000-0000-0000-0000-000000000001'
        Version.objects.filter(pk=version.pk).update(snapshot=snapshot,digest=digest(snapshot))
        with self.assertRaises(CommandError): self.create()

    def test_wrong_voice_schema_rejected_after_database_checksum_updated(self):
        self.create()
        def change(entries):
            path=self.root/'tampered.sqlite3';path.write_bytes(entries['database.sqlite3'])
            with sqlite3.connect(path) as db: db.execute('ALTER TABLE speech_workflow_voiceinquirydraft DROP COLUMN reply_text')
            raw=path.read_bytes();entries['database.sqlite3']=raw
            m=json.loads(entries['manifest.json']);m['database']={'sha256':backup._hash(raw),'size':len(raw)}
            entries['manifest.json']=json.dumps(m)
        self.change_zip(change);self.rejected()

    def test_symlink_destination_rejected_without_touching_target(self):
        self.create()
        target=self.root/'keep';target.write_bytes(b'keep');self.entry.symlink_to(target)
        with self.assertRaises(CommandError): self.restore()
        self.assertEqual(target.read_bytes(),b'keep')

    def test_backup_existing_file_never_overwritten(self):
        self.archive.write_bytes(b'keep')
        with self.assertRaises(CommandError): self.create()
        self.assertEqual(self.archive.read_bytes(),b'keep')

    def test_backup_failure_at_publication_cleans_snapshot(self):
        with patch.object(backup.os,'link',side_effect=OSError('SYNTHETIC_PRIVATE_DATA')):
            with self.assertRaisesMessage(CommandError,backup.ERROR):self.create()
        self.assertFalse(self.archive.exists())
        self.assertFalse(list(self.root.glob('.izzi-backup-*')))

    def test_invalid_zip_is_rejected(self):
        self.archive.write_bytes(b'not zip');self.rejected()

    def test_missing_voice_source_in_database_rejects_manifest_audio(self):
        self.create()
        def change(e):
            m=json.loads(e['manifest.json']);m['audio']=[];e['manifest.json']=json.dumps(m);e.pop('audio/0000.wav')
        self.change_zip(change);self.rejected()
