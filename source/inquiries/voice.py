"""Voice source in the existing local ledger and presentation pipeline; no CRM/network."""
import hashlib
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from presentations.models import TravelRequest, Hotel
from speech_workflow.models import VoiceInquiryDraft, VoiceSubmission
from speech_workflow.services import _submission
from speech_workflow.inquiry_boundary import _checked, _enabled
from speech_workflow.audio import MAX_AUDIO_BYTES
from travelcards.forms import FIELD_LABELS
from .models import Inquiry, Version, Operation, VoiceSourceLink, VoiceSelection, SelectionLink
from .services import _actor, _owned, digest, _presentation_snapshot, confirm


def checked(draft_id, actor, expected_hash):
    _actor(actor)
    from speech_workflow.adapters import AdapterDisabled
    try:
        _enabled()
    except AdapterDisabled:
        raise ValidationError('Голосовой сценарий выключен; новые действия недоступны.') from None
    draft = _checked(draft_id, actor, expected_hash)
    _submission(draft.transcript_revision.submission_id, actor)
    draft = _checked(draft_id, actor, expected_hash)
    if draft.confirmed_at is None:
        raise ValidationError('Сначала отдельно подтвердите голосовой черновик менеджером.')
    return draft


def values(draft):
    display = lambda key: draft.fields[key]['value'] if draft.fields[key]['value'].strip() else 'Неизвестно — уточнить'
    return {'title': 'Подборка по голосовой заметке',
            'traveller_summary': display('party') + ' · дети: ' + display('children_count'),
            'requirements': '\n'.join(FIELD_LABELS[k] + ': ' + display(k) + (' руб.' if k == 'budget' and draft.fields[k]['value'] else '')
                                      for k in ('dates', 'budget', 'trip_type', 'beach', 'wishes'))}


def _source(draft):
    revision = draft.transcript_revision
    return {'kind': 'voice', 'submission_id': str(revision.submission_id),
            'draft_id': str(draft.pk), 'draft_hash': draft.content_hash,
            'transcript_revision_id': revision.pk, 'transcript_number': revision.number,
            'transcript_hash': revision.content_hash, 'source_sha256': revision.source_sha256,
            'media_type': 'audio/wav', 'is_mock': draft.is_mock}


def _version(inquiry, draft, actor, selection=None, release=None):
    snapshot = {'schema': 2, 'currency': 'RUB', 'voice': _source(draft), 'work': None,
                **_presentation_snapshot(actor, selection.pk if selection else None, release.pk if release else None)}
    checksum = digest(snapshot)
    version = Version.objects.filter(inquiry=inquiry, digest=checksum).first()
    if not version:
        number = (inquiry.versions.aggregate(n=Max('number'))['n'] or 0) + 1
        version = Version.objects.create(inquiry=inquiry, number=number, snapshot=snapshot, digest=checksum)
    return version


@transaction.atomic
def attach_draft(*, actor, draft_id, expected_hash, traveller_label=''):
    draft = checked(draft_id, actor, expected_hash)
    source_id = draft.transcript_revision.submission_id
    if not isinstance(traveller_label, str) or len(traveller_label) > 160:
        raise ValidationError('Обозначение обращения должно быть строкой до 160 знаков.')
    linked = VoiceSourceLink.objects.select_related('inquiry').filter(submission_id=source_id).first()
    if linked:
        inquiry = _owned(Inquiry, linked.inquiry_id, actor)
        if draft.target_inquiry_id and draft.target_inquiry_id != inquiry.pk:
            raise ValidationError('Голосовой источник уже относится к другому обращению.')
        if traveller_label and traveller_label != inquiry.traveller_label:
            raise ValidationError('Повтор не меняет обозначение обращения.')
    elif draft.target_inquiry_id:
        inquiry = _owned(Inquiry, draft.target_inquiry_id, actor)
        if traveller_label:
            raise ValidationError('Для существующего обращения обозначение не передаётся.')
    else:
        if not traveller_label.strip():
            raise ValidationError('Укажите синтетическое обозначение нового обращения.')
        inquiry = Inquiry.objects.create(owner=actor, traveller_label=traveller_label)
        from .director import bind_new_inquiry
        bind_new_inquiry(inquiry)
    source_link, _ = VoiceSourceLink.objects.get_or_create(submission_id=source_id, defaults={'inquiry': inquiry})
    if source_link.inquiry_id != inquiry.pk:
        raise ValidationError('Голосовой источник уже закреплён за другим обращением.')
    version = _version(inquiry, draft, actor)
    key = 'voice-draft:' + str(draft.pk)
    fingerprint = digest({'draft': str(draft.pk), 'hash': expected_hash, 'inquiry': str(inquiry.pk)})
    operation, created = Operation.objects.get_or_create(owner=actor, key=key,
        defaults={'fingerprint': fingerprint, 'version': version})
    if not created and (operation.fingerprint != fingerprint or operation.version_id != version.pk):
        raise ValidationError('Повтор относится к другому содержимому.')
    return version


@transaction.atomic
def prepare(*, actor, draft_id, expected_hash, hotel_ids, traveller_label=''):
    draft = checked(draft_id, actor, expected_hash)
    ids = sorted(set(str(x) for x in hotel_ids))
    hotels = list(Hotel.objects.filter(pk__in=ids))
    if len(hotels) != len(ids) or not 1 <= len(ids) <= 5:
        raise ValidationError('Выберите от 1 до 5 существующих синтетических вариантов.')
    version = attach_draft(actor=actor, draft_id=draft.pk, expected_hash=expected_hash, traveller_label=traveller_label)
    inquiry = _owned(Inquiry, version.inquiry_id, actor)
    signature = digest({'owner': actor.pk, 'draft': str(draft.pk), 'hash': expected_hash, 'hotels': ids})
    previous = VoiceSelection.objects.filter(signature=signature).select_related('selection').first()
    if previous:
        ensure_ready(previous.selection, actor)
        if sorted(str(x) for x in previous.selection.selected_hotels.values_list('pk', flat=True)) != ids:
            raise ValidationError('Выбор вариантов изменился; повтор не заменяет сохранённое.')
        return previous.selection
    selection = TravelRequest.objects.create(owner=actor, **values(draft))
    selection.selected_hotels.set(hotels)
    VoiceSelection.objects.create(selection=selection, draft=draft, signature=signature)
    SelectionLink.objects.create(selection=selection, inquiry=inquiry)
    _version(inquiry, draft, actor, selection)
    return selection


def ensure_ready(selection, actor):
    _actor(actor)
    if selection.owner_id != actor.pk:
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied('Подборка недоступна.')
    link = VoiceSelection.objects.select_related('draft').get(selection=selection)
    draft = checked(link.draft_id, actor, link.draft.content_hash)
    source_link = VoiceSourceLink.objects.filter(submission_id=draft.transcript_revision.submission_id).first()
    association = SelectionLink.objects.filter(selection=selection, inquiry__owner_id=actor.pk).first()
    if not source_link or not association or source_link.inquiry_id != association.inquiry_id:
        raise ValidationError('Связь голоса и подборки с обращением нарушена.')
    if draft.target_inquiry_id and draft.target_inquiry_id != association.inquiry_id:
        raise ValidationError('Подборка относится к другому обращению.')
    expected = values(draft)
    if any(getattr(selection, k) != expected[k] for k in ('traveller_summary', 'requirements')):
        raise ValidationError('Пожелания изменены вне подтверждённого голосового черновика.')
    return draft, association.inquiry


def provenance_hash(selection):
    link = VoiceSelection.objects.select_related('draft__transcript_revision').get(selection=selection)
    association = SelectionLink.objects.filter(selection=selection).first()
    return digest({'inquiry': str(association.inquiry_id) if association else None, 'voice': _source(link.draft)})


def record_release(selection, actor, release):
    draft, inquiry = ensure_ready(selection, actor)
    version = _version(inquiry, draft, actor, selection, release)
    confirm(actor=actor, version_id=version.pk, expected_hash=version.digest)


def validate_version(version, actor):
    voice = version.snapshot.get('voice', {})
    draft = checked(voice.get('draft_id'), actor, voice.get('draft_hash'))
    link = VoiceSourceLink.objects.filter(submission_id=draft.transcript_revision.submission_id, inquiry=version.inquiry).first()
    if not link or voice != _source(draft):
        raise ValidationError('Версия обращения не соответствует голосовому источнику.')


def source_file(version, actor):
    # Historical audio remains readable by its active owner, even after text revocation.
    _actor(actor)
    if version.inquiry.owner_id != actor.pk or digest(version.snapshot) != version.digest:
        raise ValidationError('Нарушена целостность записи обращения.')
    voice = version.snapshot.get('voice', {})
    item = _owned(VoiceSubmission, voice.get('submission_id'), actor)
    if not VoiceSourceLink.objects.filter(submission=item, inquiry=version.inquiry).exists():
        raise ValidationError('Аудио не связано с этим обращением.')
    try:
        with item.audio.open('rb') as source:
            raw = source.read(MAX_AUDIO_BYTES + 1)
    except (OSError, ValueError):
        raise ValidationError('Оригинальный WAV недоступен. Повторное открытие БД не восстанавливает аудиофайл.') from None
    if len(raw) > MAX_AUDIO_BYTES or hashlib.sha256(raw).hexdigest() != voice.get('source_sha256'):
        raise ValidationError('Оригинальный WAV отсутствует или изменён; новый файл не подставлен.')
    return raw, 'audio/wav'
