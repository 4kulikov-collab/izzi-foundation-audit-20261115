import hashlib
import json
import secrets

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .adapters import language_model_adapter, run_stt_with_bounded_retries, speech_to_text_adapter, safe_adapter_call
from .audio import inspect_audio
from .models import HotelObservationDraft, TranscriptRevision, VoiceSubmission
from .storage import private_audio_storage


class StaleTranscript(ValidationError):
    pass


def _submission(pk, actor, *, lock=True):
    manager = VoiceSubmission.objects.select_for_update() if lock else VoiceSubmission.objects
    item = manager.get(pk=pk)
    if (not actor.is_authenticated or not actor.pk or item.owner_id != actor.pk
            or not get_user_model().objects.filter(pk=actor.pk, is_active=True).exists()):
        raise PermissionDenied
    return item


def _ensure_open(item):
    if item.status == VoiceSubmission.Status.CANCELLED:
        raise ValidationError('Работа отменена; начните новую заметку, если нужно продолжить.')


def _revision_hash(source_hash, number, text, segments, uncertainties):
    payload = {'source': source_hash, 'number': number, 'text': text,
               'segments': segments, 'uncertainties': uncertainties}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def create_submission(actor, idempotency_key, audio):
    if (not actor.is_authenticated or not actor.pk
            or not get_user_model().objects.filter(pk=actor.pk, is_active=True).exists()):
        raise PermissionDenied
    inspected = inspect_audio(audio)
    saved_audio_name = None
    try:
        with transaction.atomic():
            existing = VoiceSubmission.objects.filter(owner=actor, idempotency_key=idempotency_key).first()
            if existing:
                if not secrets.compare_digest(existing.audio_sha256, inspected['sha256']):
                    raise ValidationError('Ключ повтора уже связан с другим аудиофайлом.')
                return existing
            item = VoiceSubmission(owner=actor, idempotency_key=idempotency_key,
                                   audio_sha256=inspected['sha256'], duration_seconds=inspected['duration_seconds'])
            item.audio.save('source.wav', audio, save=False)
            saved_audio_name = item.audio.name
            item.save()
            TranscriptRevision.objects.create(
                submission=item, number=1, source_sha256=item.audio_sha256, text='',
                segments=[], uncertainties=[], content_hash=_revision_hash(item.audio_sha256, 1, '', [], []),
                authored_by=actor)
            return item
    except Exception:
        if saved_audio_name:
            private_audio_storage.delete(saved_audio_name)
        raise


def current_revision(item):
    return item.revisions.get(number=item.current_revision)


def _check_version(item, number, expected_hash):
    revision = current_revision(item)
    if number != revision.number or not isinstance(expected_hash, str) or len(expected_hash) != 64 or not secrets.compare_digest(revision.content_hash, expected_hash):
        raise StaleTranscript('Расшифровка изменилась. Откройте актуальную редакцию и проверьте её заново.')
    return revision


@transaction.atomic
def edit_transcript(pk, actor, number, expected_hash, text):
    item = _submission(pk, actor)
    _ensure_open(item)
    current = _check_version(item, number, expected_hash)
    if len(text) > 12000:
        raise ValidationError('Расшифровка превышает тестовый предел 12 000 знаков.')
    if text == current.text:
        return current
    next_number = current.number + 1
    revision = TranscriptRevision.objects.create(
        submission=item, number=next_number, source_sha256=item.audio_sha256, text=text,
        segments=[], uncertainties=[], content_hash=_revision_hash(item.audio_sha256, next_number, text, [], []),
        authored_by=actor)
    HotelObservationDraft.objects.filter(transcript_revision=current).update(
        status=HotelObservationDraft.Status.WITHDRAWN)
    item.current_revision = next_number
    item.status = VoiceSubmission.Status.TRANSCRIPT_READY if text.strip() else VoiceSubmission.Status.MANUAL_TRANSCRIPTION
    item.save(update_fields=['current_revision', 'status', 'updated_at'])
    return revision


@transaction.atomic
def apply_transcription_result(pk, actor, number, expected_hash, result):
    """Apply one adapter result only to the exact source/transcript version it read."""
    item = _submission(pk, actor)
    _ensure_open(item)
    current = _check_version(item, number, expected_hash)
    if current.confirmed_at is not None:
        raise StaleTranscript('Поздний результат нельзя применить после подтверждения этой редакции.')
    if current.source_sha256 != item.audio_sha256:
        raise StaleTranscript('Результат относится к другому исходному аудио.')
    from .contracts import validate_transcript
    validate_transcript(result)
    text = result.text
    segments = list(result.segments)
    uncertainties = list(result.uncertainties)
    if len(text) > 12000 or len(segments) > 500 or len(uncertainties) > 500:
        raise ValidationError('Результат превышает временные ограничения прототипа.')
    next_number = current.number + 1
    revision = TranscriptRevision.objects.create(
        submission=item, number=next_number, source_sha256=item.audio_sha256,
        language_code=result.language_code, processor=result.adapter, processor_version=result.version, is_mock=result.is_mock, text=text,
        segments=segments, uncertainties=uncertainties,
        content_hash=_revision_hash(item.audio_sha256, next_number, text, segments, uncertainties),
        authored_by=actor)
    item.current_revision = next_number
    HotelObservationDraft.objects.filter(transcript_revision=current).update(
        status=HotelObservationDraft.Status.WITHDRAWN)
    item.status = VoiceSubmission.Status.TRANSCRIPT_READY if text.strip() else VoiceSubmission.Status.MANUAL_TRANSCRIPTION
    item.save(update_fields=['current_revision', 'status', 'updated_at'])
    return revision


def transcribe_submission(pk, actor, number, expected_hash, *, adapter=None, cancelled=lambda: False):
    """Run one injected STT adapter and bind its result to the exact current revision."""
    item = _submission(pk, actor, lock=False)
    _ensure_open(item)
    revision = _check_version(item, number, expected_hash)
    if revision.confirmed_at is not None:
        raise StaleTranscript('Подтверждённую редакцию нельзя заменять результатом STT.')
    adapter = adapter or speech_to_text_adapter()
    with item.audio.open('rb') as audio_file:
        inspected = inspect_audio(audio_file)
        if inspected['sha256'] != item.audio_sha256:
            raise ValidationError('Аудио не соответствует сохранённому SHA; STT не выполнялся.')
        result = run_stt_with_bounded_retries(adapter, audio_file, cancelled=cancelled)
    return apply_transcription_result(pk, actor, number, expected_hash, result)


def structure_confirmed_transcript(pk, actor, number, expected_hash, *, adapter=None):
    """Return an untrusted structure suggestion from transcript text only; never persist it."""
    item = _submission(pk, actor, lock=False)
    _ensure_open(item)
    revision = _check_version(item, number, expected_hash)
    if revision.confirmed_at is None or revision.confirmation_revoked_at is not None:
        raise ValidationError('Для структурирования подтвердите точную редакцию текста.')
    adapter = adapter or language_model_adapter()
    suggestion = safe_adapter_call(lambda: adapter.structure_observation(revision.text))
    # Provider/test work may take time. Re-read permission, cancellation, exact version,
    # and approval after it returns before exposing the suggestion to the caller.
    fresh_item = _submission(pk, actor, lock=False)
    _ensure_open(fresh_item)
    fresh_revision = _check_version(fresh_item, number, expected_hash)
    if fresh_revision.confirmed_at is None or fresh_revision.confirmation_revoked_at is not None:
        raise StaleTranscript('Подтверждение отозвано во время структурирования; результат отброшен.')
    return suggestion


@transaction.atomic
def confirm_transcript(pk, actor, number, expected_hash):
    item = _submission(pk, actor)
    _ensure_open(item)
    revision = _check_version(item, number, expected_hash)
    if not revision.text.strip():
        raise ValidationError('Сначала внесите и проверьте расшифровку вручную.')
    if revision.confirmation_revoked_at is not None:
        raise ValidationError('Подтверждение отозвано. Создайте новую редакцию и проверьте её заново.')
    if revision.confirmed_at is None:
        revision.confirmed_at = timezone.now()
        revision.save(update_fields=['confirmed_at'])
    item.status = VoiceSubmission.Status.CONFIRMED
    item.save(update_fields=['status', 'updated_at'])
    return revision


@transaction.atomic
def create_observation_draft(pk, actor, number, expected_hash, data):
    item = _submission(pk, actor)
    _ensure_open(item)
    revision = _check_version(item, number, expected_hash)
    if revision.confirmed_at is None or revision.confirmation_revoked_at is not None:
        raise ValidationError('Подтвердите точную редакцию расшифровки перед созданием черновика.')
    defaults = {
        'author': actor,
        'hotel_name': data['hotel_name'].strip(),
        'observed_on': data['observed_on'],
        'building_room': data.get('building_room', '').strip(),
        'theme': data['theme'].strip(),
        'experience': data['experience'],
        'observation_text': data['observation_text'].strip(),
    }
    existing = HotelObservationDraft.objects.filter(transcript_revision=revision).first()
    if existing:
        if any(getattr(existing, key) != value for key, value in defaults.items()):
            raise ValidationError('Для этой подтверждённой редакции уже создан другой черновик.')
        return existing
    return HotelObservationDraft.objects.create(transcript_revision=revision, **defaults)


@transaction.atomic
def revoke_transcript_confirmation(pk, actor, number, expected_hash):
    item = _submission(pk, actor)
    _ensure_open(item)
    revision = _check_version(item, number, expected_hash)
    if revision.confirmed_at is None:
        raise ValidationError('Эта редакция не была подтверждена.')
    if revision.confirmation_revoked_at is None:
        revision.confirmation_revoked_at = timezone.now()
        revision.confirmation_revoked_by = actor
        revision.save(update_fields=['confirmation_revoked_at', 'confirmation_revoked_by'])
    item.status = VoiceSubmission.Status.TRANSCRIPT_READY
    item.save(update_fields=['status', 'updated_at'])
    HotelObservationDraft.objects.filter(transcript_revision=revision).update(
        status=HotelObservationDraft.Status.WITHDRAWN)
    return revision


@transaction.atomic
def cancel_submission(pk, actor):
    item = _submission(pk, actor)
    if item.status == VoiceSubmission.Status.CANCELLED:
        return item
    revision = current_revision(item)
    if revision.confirmed_at and revision.confirmation_revoked_at is None:
        revision.confirmation_revoked_at = timezone.now()
        revision.confirmation_revoked_by = actor
        revision.save(update_fields=['confirmation_revoked_at', 'confirmation_revoked_by'])
        HotelObservationDraft.objects.filter(transcript_revision=revision).update(
            status=HotelObservationDraft.Status.WITHDRAWN)
    item.status = VoiceSubmission.Status.CANCELLED
    item.save(update_fields=['status', 'updated_at'])
    return item
