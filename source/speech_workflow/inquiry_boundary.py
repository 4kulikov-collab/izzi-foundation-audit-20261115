"""Disabled-by-default local seam; persists pre-inquiry drafts, never writes CRM."""
import hashlib
import json
import time
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone
from inquiries.models import Inquiry
from .adapters import (AdapterDisabled, VoiceInquiryAdapter, LunaVoiceReplyAdapter,
                       VoiceReplyResult, ProviderFailure, safe_adapter_call)
from .audio import MAX_AUDIO_BYTES, inspect_audio
from .contracts import FIELDS, validate_draft
from .models import VoiceInquiryDraft
from .services import _submission, _ensure_open, _check_version, _revision_hash, StaleTranscript


def _enabled():
    if getattr(settings, 'VOICE_INQUIRY_BOUNDARY_ENABLED', False) is not True:
        raise AdapterDisabled('Голосовой черновик обращения выключен. Доступна ручная расшифровка.')


def _ready(pk, actor, number, expected_hash):
    _enabled()
    item = _submission(pk, actor, lock=False)
    _ensure_open(item)
    revision = _check_version(item, number, expected_hash)
    actual_revision_hash = _revision_hash(revision.source_sha256, revision.number, revision.text,
                                          revision.segments, revision.uncertainties)
    if actual_revision_hash != revision.content_hash:
        raise StaleTranscript('Содержимое расшифровки не соответствует подтверждённому хешу.')
    if revision.source_sha256 != item.audio_sha256:
        raise StaleTranscript('Редакция относится к другому исходнику.')
    if revision.confirmed_at is None or revision.confirmation_revoked_at is not None:
        raise ValidationError('Сначала подтвердите точную расшифровку менеджером.')
    try:
        with item.audio.open('rb') as source:
            raw = source.read(MAX_AUDIO_BYTES + 1)
    except (OSError, ValueError):
        raise ValidationError('Исходное аудио недоступно; подтверждение и ответ остановлены.') from None
    if len(raw) > MAX_AUDIO_BYTES or hashlib.sha256(raw).hexdigest() != item.audio_sha256:
        raise ValidationError('Исходное аудио не соответствует сохранённому SHA.')
    return revision


def _target(actor, target_id):
    if target_id is None:
        return None
    try:
        return Inquiry.objects.get(pk=target_id, owner_id=actor.pk)
    except (Inquiry.DoesNotExist, ValueError, ValidationError):
        raise PermissionDenied('Обращение недоступно.') from None


def _digest(revision, target_id, fields, reply_text, is_mock):
    value = {'schema': 'voice-inquiry-draft-v1', 'revision_id': revision.pk,
             'revision_hash': revision.content_hash, 'audio_sha256': revision.source_sha256,
             'target_inquiry_id': str(target_id) if target_id else None, 'currency': 'RUB',
             'fields': fields, 'reply_text': reply_text, 'is_mock': is_mock}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def prepare_inquiry_draft(pk, actor, number, expected_hash, *, target_inquiry_id=None, adapter=None):
    revision = _ready(pk, actor, number, expected_hash)
    target = _target(actor, target_inquiry_id)
    existing = VoiceInquiryDraft.objects.filter(transcript_revision=revision).first()
    if existing:
        if existing.target_inquiry_id != (target.pk if target else None):
            raise ValidationError('Черновик нельзя перепривязать к другому обращению.')
        return _checked(existing.pk, actor, existing.content_hash)
    adapter = adapter or VoiceInquiryAdapter()
    result = safe_adapter_call(lambda: adapter.draft_inquiry(revision.text))
    fields = validate_draft(result)
    # Copy provider-owned mutable data before persisting.
    fields = json.loads(json.dumps(fields, ensure_ascii=False))
    with transaction.atomic():
        _submission(pk, actor)
        revision = _ready(pk, actor, number, expected_hash)
        target = _target(actor, target_inquiry_id)
        digest = _digest(revision, target.pk if target else None, fields, result.reply_text, True)
        draft, created = VoiceInquiryDraft.objects.get_or_create(transcript_revision=revision, defaults={
            'target_inquiry': target, 'fields': fields, 'reply_text': result.reply_text,
            'is_mock': True, 'content_hash': digest})
        if not created and draft.content_hash != digest:
            raise StaleTranscript('Для редакции уже сохранён другой черновик. Повтор не заменяет его.')
        return draft


def _checked(pk, actor, expected_hash):
    try:
        draft = VoiceInquiryDraft.objects.select_related('transcript_revision__submission').get(
            pk=pk, transcript_revision__submission__owner_id=actor.pk)
    except (VoiceInquiryDraft.DoesNotExist, ValueError, ValidationError):
        raise PermissionDenied('Черновик недоступен.') from None
    revision = draft.transcript_revision
    _ready(revision.submission_id, actor, revision.number, revision.content_hash)
    _target(actor, draft.target_inquiry_id)
    actual = _digest(revision, draft.target_inquiry_id, draft.fields, draft.reply_text, draft.is_mock)
    if not isinstance(expected_hash, str) or expected_hash != draft.content_hash or actual != expected_hash:
        raise StaleTranscript('Черновик изменён; подтвердите актуальное содержание.')
    return draft


@transaction.atomic
def confirm_inquiry_draft(pk, actor, expected_hash, *, reviewed_fields, accept_unknowns=False, reply_reviewed=False):
    _enabled()
    # Serialise with transcript editing/revocation on databases that support row locks.
    draft = _checked(pk, actor, expected_hash)
    _submission(draft.transcript_revision.submission_id, actor)
    draft = _checked(pk, actor, expected_hash)
    required = {key for key in FIELDS if draft.fields[key]['state'] == 'candidate'}
    if not isinstance(reviewed_fields, (list, tuple)) or any(type(x) is not str for x in reviewed_fields):
        raise ValidationError('Отметьте проверенные поля.')
    if len(set(reviewed_fields)) != len(reviewed_fields) or set(reviewed_fields) != required:
        raise ValidationError('Проверьте каждое заполненное поле; неизвестное не дополняется автоматически.')
    if len(required) < len(FIELDS) and accept_unknowns is not True:
        raise ValidationError('Явно подтвердите, что неизвестные поля остаются неизвестными.')
    if draft.reply_text and reply_reviewed is not True:
        raise ValidationError('Отдельно проверьте точный текст будущего голосового ответа.')
    if draft.confirmed_at is None:
        draft.confirmed_at = timezone.now()
        draft.save(update_fields=['confirmed_at'])
    return draft


def synthesize_reply(pk, actor, expected_hash, *, adapter=None, timeout_seconds=5):
    _enabled()
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 15:
        raise ValidationError('Локальный лимит ожидания должен быть от 0 до 15 секунд.')
    draft = _checked(pk, actor, expected_hash)
    if draft.confirmed_at is None or not draft.reply_text.strip():
        raise ValidationError('Ответ доступен только после проверки черновика и непустого текста менеджером.')
    adapter = adapter or LunaVoiceReplyAdapter()
    started = time.monotonic()
    result = safe_adapter_call(lambda: adapter.synthesize(draft.reply_text, timeout_seconds=timeout_seconds))
    # Deadline rejection is not forced cancellation of arbitrary adapter code.
    if time.monotonic() - started > timeout_seconds:
        raise ProviderFailure('timeout')
    fresh = _checked(pk, actor, expected_hash)
    if fresh.confirmed_at is None:
        raise StaleTranscript('Подтверждение изменилось; ответ отброшен.')
    text_sha = hashlib.sha256(draft.reply_text.encode()).hexdigest()
    if (not isinstance(result, VoiceReplyResult) or result.is_mock is not True
            or result.voice != 'luna-contract' or result.media_type != 'audio/wav'
            or type(result.audio) is not bytes or not 0 < len(result.audio) <= MAX_AUDIO_BYTES
            or result.text_sha256 != text_sha):
        raise ValidationError('Непригодный синтетический голосовой ответ; отправка не выполнялась.')
    inspected = inspect_audio(SimpleUploadedFile('synthetic-response.wav', result.audio, content_type='audio/wav'))
    return {'audio': result.audio, 'audio_sha256': inspected['sha256'], 'text_sha256': text_sha,
            'draft_id': str(draft.pk), 'target_inquiry_id': str(draft.target_inquiry_id) if draft.target_inquiry_id else None,
            'is_mock': True, 'message': 'Заглушка Luna: синтетический WAV, речь не синтезирована и не отправлена.'}
