"""Local manager workflow. No CRM requests or tourist messaging."""
from django.core.exceptions import ValidationError
from django.db import transaction
from presentations.models import TravelRequest, Hotel
from travelcards.forms import normalize_fields, FIELD_LABELS
from travelcards.services import _display
from worksheet_reviews.models import WorksheetReview
from worksheet_reviews.ocr_services import current_revision, current_source
from .models import WorksheetSelection, WorkLink, SelectionLink
from .services import _actor, _owned, digest, preview, attach, confirm


def values(work, revision):
    fields = normalize_fields(revision.fields)
    return {'title': 'Подборка ' + work.work_code,
            'traveller_summary': f"{_display(fields['party'])} · дети: {_display(fields['children_count'])}",
            'requirements': '\n'.join(f'{FIELD_LABELS[k]}: {_display(fields[k])}'
                                      for k in ('dates', 'budget', 'trip_type', 'beach', 'wishes'))}


@transaction.atomic
def prepare(*, actor, work_id, expected_hash, hotel_ids, inquiry_id=None, traveller_label='', offer=None):
    _actor(actor)
    work = _owned(WorksheetReview, work_id, actor)
    revision = current_revision(work)
    if not revision or revision.confirmed_at is None or revision.content_hash != expected_hash:
        raise ValidationError('Подтвердите актуальную карту перед подборкой.')
    fields = normalize_fields(revision.fields)
    if any(fields[k]['state'] == 'ocr_candidate' for k in FIELD_LABELS):
        raise ValidationError('Остались неподтверждённые кандидаты OCR.')
    ids = sorted(set(str(x) for x in hotel_ids))
    hotels = list(Hotel.objects.filter(pk__in=ids))
    if len(hotels) != len(ids) or not 1 <= len(hotels) <= 5:
        raise ValidationError('Выберите от 1 до 5 существующих вариантов.')
    from .offers import normalize
    from .models import SelectedOffer
    normalized_offer = normalize(offer, ids) if offer is not None else None
    if normalized_offer and normalized_offer.get('import_source'):
        from .extension_import import check_binding
        check_binding(actor, work, inquiry_id, normalized_offer['import_source'])
    identity = {'owner': actor.pk, 'work': str(work.pk), 'ocr': revision.pk, 'hotels': ids}
    if normalized_offer is not None:
        identity['offer'] = normalized_offer
    signature = digest(identity)
    previous = WorksheetSelection.objects.filter(signature=signature).first()
    linked = WorkLink.objects.filter(work_id=work.pk).first()
    if linked and inquiry_id and str(linked.inquiry_id) != str(inquiry_id):
        raise ValidationError('Лист уже связан с другим обращением.')
    if previous:
        selection = previous.selection
        from .offers import read
        if read(selection) != normalized_offer:
            raise ValidationError('Выбранное предложение изменилось вне разрешённого контракта.')
        ensure_ready(selection, actor)
        if sorted(str(x) for x in selection.selected_hotels.values_list('pk', flat=True)) != ids:
            raise ValidationError('Подборка была изменена после подготовки. Откройте её и проверьте предложения.')
        return selection
    if linked:
        inquiry_id, traveller_label = linked.inquiry_id, ''
    if inquiry_id:
        traveller_label = ''
    if not inquiry_id and not traveller_label.strip():
        raise ValidationError('Укажите имя / обозначение туриста для нового обращения.')
    selection = TravelRequest.objects.create(owner=actor, **values(work, revision))
    selection.selected_hotels.set(hotels)
    if normalized_offer is not None:
        SelectedOffer.objects.create(selection=selection, data=normalized_offer, digest=digest(normalized_offer))
    WorksheetSelection.objects.create(selection=selection, work=work, ocr_revision=revision, signature=signature)
    snapshot = preview(actor=actor, work_id=work.pk, selection_id=selection.pk)
    attach(actor=actor, key='prepare:' + signature, work_id=work.pk,
           expected_hash=snapshot['digest'], inquiry_id=inquiry_id,
           traveller_label=traveller_label, selection_id=selection.pk)
    return selection


def ensure_ready(selection, actor):
    _actor(actor)
    from .models import VoiceSelection
    if VoiceSelection.objects.filter(selection=selection).exists():
        from .voice import ensure_ready as voice_ready
        voice_ready(selection, actor)
        return
    link = WorksheetSelection.objects.filter(selection=selection).select_related('work', 'ocr_revision').first()
    association = SelectionLink.objects.filter(selection=selection, inquiry__owner=actor).first()
    if not association:
        raise ValidationError('Сначала свяжите подборку с обращением через подтверждённую карту.')
    if not link:
        legacy_work(selection, actor, association)
        return
    work = _owned(WorksheetReview, link.work_id, actor)
    revision = current_revision(work)
    if not revision or revision.pk != link.ocr_revision_id or not revision.confirmed_at:
        raise ValidationError('Карта изменилась. Подтвердите её и создайте новую подборку.')
    if not SelectionLink.objects.filter(selection=selection, inquiry__owner=actor).exists():
        raise ValidationError('У подборки нет обращения.')
    expected = values(work, revision)
    if any(getattr(selection, k) != expected[k] for k in ('traveller_summary', 'requirements')):
        raise ValidationError('Пожелания изменены вне подтверждённой карты.')


def record_release(selection, actor, release):
    from .models import VoiceSelection
    if VoiceSelection.objects.filter(selection=selection).exists():
        from .voice import record_release as voice_release
        return voice_release(selection, actor, release)
    link = WorksheetSelection.objects.filter(selection=selection).first()
    ensure_ready(selection, actor)
    association = SelectionLink.objects.get(selection=selection)
    inquiry = association.inquiry
    work_id = link.work_id if link else legacy_work(selection, actor, association).pk
    snapshot = preview(actor=actor, work_id=work_id, selection_id=selection.pk, release_id=release.pk)
    version = attach(actor=actor, key='release:' + str(release.pk), work_id=work_id,
                     expected_hash=snapshot['digest'], inquiry_id=inquiry.pk,
                     selection_id=selection.pk, release_id=release.pk)
    confirm(actor=actor, version_id=version.pk, expected_hash=version.digest)


def legacy_work(selection, actor, association):
    """Legacy selections must have explicit source provenance, never a fabricated inquiry."""
    from .models import Version
    version = Version.objects.filter(inquiry=association.inquiry,
        snapshot__selection__id=str(selection.pk)).order_by('-number').first()
    if not version:
        raise ValidationError('У подборки отсутствует проверяемая связь с исходным листом.')
    if not version.snapshot.get('work'):
        raise ValidationError('У подборки отсутствует действующая связь с источником.')
    work = _owned(WorksheetReview, version.snapshot['work']['id'], actor)
    original = version.snapshot['work']
    original_version = original.get('source_version', 1)
    if not isinstance(original_version, int):
        original_version = 1  # Pre-PR15 contracts used SHA as version and only had source 1.
    if (current_source(work).worksheet_sha256 != original['source_sha256'] or
            original_version != work.current_source_version):
        raise ValidationError('Исходный лист изменился после привязки. Обновите связь с обращением.')
    return work


def provenance_hash(selection):
    """Opaque context fingerprint binds approval to the exact inquiry/source relation."""
    from .models import Version, VoiceSelection
    if VoiceSelection.objects.filter(selection=selection).exists():
        from .voice import provenance_hash as voice_hash
        return voice_hash(selection)
    association = SelectionLink.objects.filter(selection=selection).first()
    if not association:
        return ''
    version = Version.objects.filter(inquiry=association.inquiry,
        snapshot__selection__id=str(selection.pk)).order_by('-number').first()
    if not version:
        return ''
    source = version.snapshot.get('work')
    if not source:
        return ''
    return digest({'inquiry': str(association.inquiry_id), 'work': source['id'],
                   'source_version': source.get('source_version'), 'source_sha256': source['source_sha256']})
