"""Local OCR snapshots attached to a single private worksheet review."""
import copy
import hashlib
import json
import re
import secrets

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from travelcards.forms import FIELD_LABELS, normalize_fields
from travelcards.ocr import RECOGNITION_KEY, _empty_fields, recognize_card_photo

from .models import WorksheetFileRevision, WorksheetOCRRevision, WorksheetReview

FAILURE_STATES = {'unavailable', 'timeout', 'error', 'no_text', 'no_fields_found', 'unsupported_pdf'}


def _snapshot_hash(review, source_sha256, fields, original_text, metadata):
    payload = {'source_sha256': source_sha256, 'fields': fields,
               'original_text': original_text, 'metadata': metadata,
               'schema': 'worksheet-ocr-v1'}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def current_revision(review):
    latest = review.ocr_revisions.select_related('confirmed_by').first()
    return latest if latest and latest.source_sha256 == current_source(review).worksheet_sha256 and latest.metadata.get('source_version', 1) == review.current_source_version else None


def current_source(review):
    if review.current_source_version == 1:
        return review
    return review.file_revisions.get(number=review.current_source_version)


def _run_ocr(source):
    if source.worksheet_type not in ('image/png', 'image/jpeg'):
        note = 'Этот файл сохранён без OCR: локальный адаптер принимает только PNG/JPEG. Проверьте оригинал вручную.'
        return _empty_fields('manual_fallback', note), {
            'engine': 'Tesseract', 'version': '', 'language': 'rus',
            'status': 'unsupported_pdf', 'note': note,
        }, ''
    try:
        from django.conf import settings
        adapter = getattr(settings, 'IZZI_IMAGE_ADAPTER', None)
        if adapter is None:
            fields, metadata = recognize_card_photo(bytes(source.worksheet_bytes))
        else:
            from intelligence.fields import extract
            fields, metadata = extract(adapter, kind='image', content=bytes(source.worksheet_bytes))
    except Exception:
        note = 'Локальное распознавание завершилось ошибкой. Оригинал сохранён; проверьте поля вручную.'
        return _empty_fields('manual_fallback', note), {
            'engine': 'Tesseract', 'version': '', 'language': 'rus', 'status': 'error', 'note': note,
        }, ''
    metadata = copy.deepcopy(metadata)
    original_text = str(metadata.pop('raw_text', '') or '')[:10000]
    return fields, metadata, original_text


def _create_revision(review, number, fields, metadata, original_text):
    source_sha256 = current_source(review).worksheet_sha256
    metadata = {**metadata, 'source_version': review.current_source_version}
    fields = copy.deepcopy(fields)
    fields[RECOGNITION_KEY] = metadata
    revision = WorksheetOCRRevision.objects.create(
        review=review, number=number, source_sha256=source_sha256,
        fields=fields, metadata=metadata, original_text=original_text,
        content_hash=_snapshot_hash(review, source_sha256, fields, original_text, metadata),
    )

    from config.diagnostics import emit
    transaction.on_commit(lambda: emit("ocr", metadata.get("status")))
    return revision


def initialize_ocr(review, actor):
    """Create the first OCR draft after the one upload has already been saved."""
    if not actor.is_authenticated or not actor.is_active or review.owner_id != actor.pk:
        raise PermissionDenied
    source = current_source(review)
    fields, metadata, original_text = _run_ocr(source)
    fields[RECOGNITION_KEY] = metadata
    with transaction.atomic():
        owner = get_user_model().objects.select_for_update().filter(pk=actor.pk, is_active=True).first()
        locked = WorksheetReview.objects.select_for_update().get(pk=review.pk, owner=actor)
        existing = current_revision(locked)
        locked_source = current_source(locked)
        if existing and existing.source_sha256 == locked_source.worksheet_sha256:
            return existing
        if locked_source.worksheet_sha256 != source.worksheet_sha256 or owner is None:
            raise PermissionDenied
        latest = locked.ocr_revisions.first()
        next_number = latest.number + 1 if latest else 1
        return _create_revision(locked, next_number, fields, metadata, original_text)


@transaction.atomic
def replace_source(review, actor, expected_version, expected_hash, *, worksheet_bytes, worksheet_type):
    locked = WorksheetReview.objects.select_for_update().filter(
        pk=review.pk, owner=actor, owner__is_active=True,
    ).first()
    if locked is None or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied
    source = current_source(locked)
    if (expected_version != locked.current_source_version or not isinstance(expected_hash, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_hash)
            or not secrets.compare_digest(source.worksheet_sha256, expected_hash)):
        raise ValidationError('Исходный лист изменился. Откройте текущую работу перед заменой фото.')
    new_hash = hashlib.sha256(worksheet_bytes).hexdigest()
    if secrets.compare_digest(new_hash, source.worksheet_sha256):
        return source
    new_version = locked.current_source_version + 1
    replacement = WorksheetFileRevision.objects.create(
        review=locked, number=new_version, worksheet_bytes=worksheet_bytes,
        worksheet_type=worksheet_type, worksheet_sha256=new_hash, created_by=actor,
    )
    locked.current_source_version = new_version
    locked.save(update_fields=['current_source_version', 'updated_at'])
    return replacement


def _locked_current(review_id, actor, number, expected_hash):
    review = WorksheetReview.objects.select_for_update().filter(
        pk=review_id, owner=actor, owner__is_active=True,
    ).first()
    if review is None or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied
    revision = current_revision(review)
    if (revision is None or number != revision.number
            or not isinstance(expected_hash, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_hash)
            or not secrets.compare_digest(revision.content_hash, expected_hash)
            or revision.source_sha256 != current_source(review).worksheet_sha256):
        raise ValidationError('Фото или редакция изменились. Откройте текущую версию и сверьте её заново.')
    return review, revision


@transaction.atomic
def save_fields(review, actor, number, expected_hash, fields, *, confirm=False):
    locked_review, previous = _locked_current(review.pk, actor, number, expected_hash)
    normalized = normalize_fields(previous.fields)
    previous_values = {key: normalized[key] for key in FIELD_LABELS}
    snapshot = copy.deepcopy(fields)
    snapshot[RECOGNITION_KEY] = copy.deepcopy(previous.metadata)
    if fields == previous_values:
        revision = previous
    else:
        revision = _create_revision(locked_review, previous.number + 1, snapshot,
                                    previous.metadata, previous.original_text)
    if confirm:
        pending = [key for key in FIELD_LABELS if normalize_fields(revision.fields)[key]['state'] == 'ocr_candidate']
        if pending:
            labels = ', '.join(FIELD_LABELS[key] for key in pending)
            raise ValidationError(f'Сначала проверьте кандидаты OCR по фото: {labels}.')
        if revision.confirmed_at is None:
            revision.confirmed_at = timezone.now()
            revision.confirmed_by = actor
            revision.save(update_fields=['confirmed_at', 'confirmed_by'])
    from config.diagnostics import emit
    transaction.on_commit(lambda: emit("review", "confirmed" if confirm else "saved"))
    return revision


def rerun_ocr(review, actor, number, expected_hash):
    # Validate before the slow local OCR and again under lock after it completes.
    with transaction.atomic():
        _, previous = _locked_current(review.pk, actor, number, expected_hash)
        previous_id = previous.pk
        source_sha = previous.source_sha256
        locked_review = WorksheetReview.objects.get(pk=review.pk)
        source = current_source(locked_review)
        existing_fields = normalize_fields(previous.fields)
        if source.worksheet_sha256 != source_sha:
            raise ValidationError('Исходное фото изменилось; обновите страницу для проверки новой версии.')
    candidate_fields, metadata, original_text = _run_ocr(source)
    status = metadata.get('status')
    if status in FAILURE_STATES:
        merged = {key: copy.deepcopy(existing_fields[key]) for key in FIELD_LABELS}
        metadata = {**metadata, 'manual_fallback_preserved': True,
                    'note': metadata.get('note', '') + ' Ранее исправленные поля сохранены.'}
    else:
        merged = {}
        for key in FIELD_LABELS:
            old = existing_fields[key]
            if old.get('source', {}).get('kind') == 'manual_review':
                merged[key] = copy.deepcopy(old)
            else:
                merged[key] = candidate_fields[key]
    merged[RECOGNITION_KEY] = copy.deepcopy(metadata)
    with transaction.atomic():
        locked_review, latest = _locked_current(review.pk, actor, number, expected_hash)
        if latest.pk != previous_id or current_source(locked_review).worksheet_sha256 != source_sha:
            raise ValidationError('Фото или редакция изменились во время повторного OCR; результат не сохранён.')
        return _create_revision(locked_review, latest.number + 1, merged, metadata, original_text)
