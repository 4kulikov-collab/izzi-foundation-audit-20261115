"""Read-only director scope and verified metadata. No CRM, impersonation or grants UI."""
import hashlib
import re
from pathlib import Path
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import F
from presentations.models import Release
from speech_workflow.audio import MAX_AUDIO_BYTES
from speech_workflow.models import VoiceSubmission
from worksheet_reviews.models import WorksheetReview
from .models import (DirectorOfficeGrant, Inquiry, InquiryOffice, ManagerOffice,
                     SelectionLink, VoiceSelection, WorkLink, WorksheetSelection)
from .services import digest

ERROR = 'Связь или файл не подтверждены; содержимое скрыто.'


def bind_new_inquiry(inquiry):
    """Called inside existing creation transactions. Never reassign historical inquiries."""
    membership = ManagerOffice.objects.filter(user_id=inquiry.owner_id, is_active=True,
                                              office__is_active=True, user__is_active=True).first()
    if membership:
        InquiryOffice.objects.create(inquiry=inquiry, office=membership.office,
                                     owner_at_binding_id=inquiry.owner_id)


def grants(actor):
    if (not getattr(actor, 'is_authenticated', False) or not getattr(actor, 'pk', None)
            or not get_user_model().objects.filter(pk=actor.pk, is_active=True).exists()):
        raise PermissionDenied('Директорский доступ не предоставлен.')
    result = list(DirectorOfficeGrant.objects.filter(user_id=actor.pk, is_active=True,
        office__is_active=True).select_related('office').order_by('office__name', 'office_id'))
    if not result:
        raise PermissionDenied('Директорский доступ не предоставлен.')
    return result


def scope(actor):
    office_ids = [g.office_id for g in grants(actor)]
    return Inquiry.objects.filter(office_binding__office_id__in=office_ids,
        office_binding__owner_at_binding_id=F('owner_id')).select_related('owner', 'office_binding__office')


def allowed(actor, inquiry_id):
    inquiry = scope(actor).filter(pk=inquiry_id).first()
    if inquiry is None:
        raise PermissionDenied('Обращение недоступно.')
    return inquiry


def _uuid(value):
    return UUID(str(value))


def _source(inquiry, snapshot):
    if snapshot.get('schema') == 2:
        record = snapshot['voice']
        source_id = _uuid(record['submission_id'])
        source = VoiceSubmission.objects.filter(pk=source_id, owner_id=inquiry.owner_id,
            voicesourcelink__inquiry_id=inquiry.pk).first()
        if source is None:
            raise ValueError
        # No arbitrary path or symlink reads if a stored filename is corrupted.
        if not re.fullmatch(re.escape(f'{inquiry.owner_id}/{source.pk}/')+'[0-9a-f]{32}\\.wav', source.audio.name):
            raise ValueError
        path = Path(source.audio.path)
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError
        with path.open('rb') as stream:
            raw = stream.read(MAX_AUDIO_BYTES + 1)
        if len(raw) > MAX_AUDIO_BYTES or source.audio_sha256 != record['source_sha256']:
            raise ValueError
        mime, kind = 'audio/wav', 'Голосовой WAV (SpeechKit/Luna не подключены)'
    elif snapshot.get('schema') == 1:
        record = snapshot['work']
        source_id = _uuid(record['id'])
        if not WorkLink.objects.filter(work_id=source_id, inquiry=inquiry).exists():
            raise ValueError
        source = WorksheetReview.objects.get(pk=source_id, owner_id=inquiry.owner_id)
        number = record.get('source_version', 1)
        if type(number) is not int or number < 1:
            raise ValueError
        if number != 1:
            source = source.file_revisions.get(number=number)
        raw = bytes(source.worksheet_bytes)
        mime, kind = source.worksheet_type, 'Лист пожеланий'
        if mime != record['source_type']:
            raise ValueError
    else:
        raise ValueError
    if hashlib.sha256(raw).hexdigest() != record['source_sha256']:
        raise ValueError
    return {'kind': kind, 'id': str(source_id), 'sha256': record['source_sha256'], 'mime': mime}


def audit_version(inquiry, version):
    """Return a whitelist, never snapshot/private text. Caller must scope the inquiry."""
    from django.core.exceptions import ObjectDoesNotExist
    try:
        snap = version.snapshot
        if version.inquiry_id != inquiry.pk or digest(snap) != version.digest:
            raise ValueError
        source = _source(inquiry, snap)
        row = {'number': version.number, 'version_id': version.pk, 'source': source,
               'state': 'Источник связан с обращением', 'release': None, 'selection_id': None, 'valid': True}
        selection = snap.get('selection')
        if selection:
            sid = _uuid(selection['id'])
            link = SelectionLink.objects.filter(selection_id=sid, inquiry=inquiry,
                selection__owner_id=inquiry.owner_id).first()
            if link is None:
                raise ValueError
            if snap.get('schema') == 2:
                origin = VoiceSelection.objects.filter(selection_id=sid, draft_id=_uuid(snap['voice']['draft_id']),
                    draft__transcript_revision_id=snap['voice']['transcript_revision_id'],
                    draft__transcript_revision__submission_id=source['id']).first()
                if origin is None or origin.draft.content_hash != snap['voice']['draft_hash']:
                    raise ValueError
            else:
                origin = WorksheetSelection.objects.filter(selection_id=sid, work_id=source['id']).first()
                if origin is None:
                    raise ValueError  # Legacy/unproven provenance stays explicitly unverified.
            row.update(state='Подборка связана с обращением', selection_id=str(sid))
        release_data = snap.get('release')
        if release_data:
            if not selection:
                raise ValueError
            release = Release.objects.get(pk=_uuid(release_data['id']), request_id=sid,
                                          request__owner_id=inquiry.owner_id)
            if (digest(release.snapshot) != release.content_hash or release.content_hash != release_data['content_hash']
                    or release.snapshot != release_data['snapshot'] or release.pdf_hash != release_data['pdf_hash']
                    or hashlib.sha256(bytes(release.pdf_bytes)).hexdigest() != release.pdf_hash):
                raise ValueError
            row.update(state='Выпуск связан с обращением', release={
                'id': str(release.pk), 'pdf_sha256': release.pdf_hash,
                'revoked': release.revoked_at is not None, 'expires_at': release.expires_at})
        return row
    except (ValueError, TypeError, KeyError, AttributeError, OSError, ObjectDoesNotExist):
        return {'number': version.number, 'valid': False, 'state': ERROR}


def timeline(actor, inquiry_id):
    inquiry = allowed(actor, inquiry_id)
    rows = [audit_version(inquiry, v) for v in inquiry.versions.order_by('number')]
    seen = {}
    for row in rows:
        if row.get('release'):
            rid = row['release']['id']
            if rid not in seen:
                seen[rid] = len(seen) + 1
            row['release']['order'] = seen[rid]
    allowed(actor, inquiry.pk)  # Revocation during slow file checks must discard the response.
    return inquiry, rows


def pdf(actor, version_id):
    from .models import Version
    version = Version.objects.filter(pk=version_id, inquiry__in=scope(actor)).select_related('inquiry').first()
    if version is None:
        raise PermissionDenied('Файл недоступен.')
    row = audit_version(version.inquiry, version)
    if not row.get('valid') or not row.get('release'):
        raise ValidationError(ERROR)
    release = Release.objects.get(pk=row['release']['id'])
    raw = bytes(release.pdf_bytes)
    if hashlib.sha256(raw).hexdigest() != row['release']['pdf_sha256']:
        raise ValidationError(ERROR)
    allowed(actor, version.inquiry_id)
    return raw
