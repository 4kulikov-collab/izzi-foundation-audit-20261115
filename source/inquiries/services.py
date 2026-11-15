"""Owner-scoped, atomic local contract; all source lookups are local."""
import hashlib
import json
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from presentations.models import Release, TravelRequest
from worksheet_reviews.models import WorksheetReview
from .adapters import ADAPTERS
from .models import Confirmation, Inquiry, Operation, SelectionLink, Version, WorkLink


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _actor(actor):
    if not actor.is_authenticated or not get_user_model().objects.select_for_update().filter(
            pk=actor.pk, is_active=True).exists():
        raise PermissionDenied('Владелец недоступен.')


def _owned(model, pk, actor):
    try:
        return model.objects.select_for_update().get(pk=pk, owner_id=actor.pk)
    except model.DoesNotExist:
        raise PermissionDenied('Объект недоступен.')


def _snapshot(actor, work_id, selection_id, release_id):
    work = _owned(WorksheetReview, work_id, actor)
    from worksheet_reviews.ocr_services import current_source
    source = current_source(work)
    actual_hash = hashlib.sha256(bytes(source.worksheet_bytes)).hexdigest()
    if actual_hash != source.worksheet_sha256:
        raise ValidationError('Исходник не соответствует сохранённому SHA.')
    result = {'schema': 1, 'currency': 'RUB', 'work': {
        'id': str(work.pk), 'code': work.work_code, 'source_sha256': actual_hash,
        'source_type': source.worksheet_type, 'source_version': work.current_source_version,
        'expert_overview': work.expert_overview,
        'hotels': list(work.hotel_choices.order_by('position').values('position', 'name', 'reference_text')),
    }, 'selection': None, 'release': None}
    result.update(_presentation_snapshot(actor, selection_id, release_id))
    return result


def _presentation_snapshot(actor, selection_id, release_id):
    result = {'selection': None, 'release': None}
    if selection_id:
        selection = _owned(TravelRequest, selection_id, actor)
        result['selection'] = {'id': str(selection.pk), 'revision': selection.revision,
            'title': selection.title, 'traveller_summary': selection.traveller_summary,
            'requirements': selection.requirements,
            'hotels': [{**h, 'id': str(h['id'])} for h in selection.selected_hotels.order_by('id').values(
                'id', 'name', 'summary', 'pros', 'limitations', 'source_reference')]}
    if selection_id:
        from .offers import read
        offer = read(selection)
        if offer is not None:
            result['selection']['exact_offer'] = offer
    if release_id:
        if not selection_id:
            raise ValidationError('Выпуск требует указать подборку.')
        try:
            release = Release.objects.get(pk=release_id, request_id=selection_id, request__owner_id=actor.pk)
        except Release.DoesNotExist:
            raise PermissionDenied('Выпуск недоступен.')
        if (digest(release.snapshot) != release.content_hash or
                hashlib.sha256(bytes(release.pdf_bytes)).hexdigest() != release.pdf_hash):
            raise ValidationError('Нарушена целостность выпуска.')
        result['release'] = {'id': str(release.pk), 'content_hash': release.content_hash,
            'pdf_hash': release.pdf_hash, 'snapshot': release.snapshot}
    return result


@transaction.atomic
def preview(*, actor, work_id, selection_id=None, release_id=None):
    _actor(actor)
    snapshot = _snapshot(actor, work_id, selection_id, release_id)
    return {'snapshot': snapshot, 'digest': digest(snapshot)}


@transaction.atomic
def attach(*, actor, key, work_id, expected_hash, inquiry_id=None, traveller_label='',
           selection_id=None, release_id=None, crm_system='', external_id=''):
    """Create from a work, or attach explicitly. Same key + changed payload is an error."""
    _actor(actor)
    if not isinstance(key, str) or not key.strip() or len(key) > 120:
        raise ValidationError('Требуется ключ операции длиной до 120 символов.')
    if crm_system not in ('', 'uon', 'amocrm') or bool(crm_system) != bool(external_id):
        raise ValidationError('Укажите систему и внешний ID вместе.')
    if len(external_id) > 160 or len(traveller_label) > 160:
        raise ValidationError('Слишком длинное значение.')
    if inquiry_id and (crm_system or external_id or traveller_label):
        raise ValidationError('Для существующего обращения передайте только локальный ID.')
    fingerprint = digest({'work': str(work_id), 'selection': str(selection_id), 'release': str(release_id),
        'inquiry': str(inquiry_id), 'hash': expected_hash, 'label': traveller_label,
        'crm': crm_system, 'external': external_id})
    prior = Operation.objects.filter(owner=actor, key=key).select_related('version').first()
    if prior:
        if prior.fingerprint != fingerprint:
            raise ValidationError('Ключ уже использован для другого запроса.')
        return prior.version
    snapshot = _snapshot(actor, work_id, selection_id, release_id)
    if digest(snapshot) != expected_hash:
        raise ValidationError('Источник или подборка изменились. Обновите предпросмотр.')
    work_link = WorkLink.objects.filter(work_id=work_id).first()
    if inquiry_id:
        inquiry = _owned(Inquiry, inquiry_id, actor)
    elif work_link:
        inquiry = _owned(Inquiry, work_link.inquiry_id, actor)
        if (traveller_label, crm_system, external_id) != (
                inquiry.traveller_label, inquiry.crm_system, inquiry.external_id):
            raise ValidationError('Работа уже связана; используйте локальный ID обращения.')
    else:
        if crm_system and Inquiry.objects.filter(owner=actor, crm_system=crm_system, external_id=external_id).exists():
            raise ValidationError('Внешний ID уже связан; выберите существующий локальный ID.')
        inquiry = Inquiry.objects.create(owner=actor, traveller_label=traveller_label,
                                         crm_system=crm_system, external_id=external_id)
        from .director import bind_new_inquiry
        bind_new_inquiry(inquiry)
    if work_link and work_link.inquiry_id != inquiry.pk:
        raise ValidationError('Работу нельзя перепривязать к другому обращению.')
    WorkLink.objects.get_or_create(work_id=work_id, defaults={'inquiry': inquiry})
    if selection_id:
        link, _ = SelectionLink.objects.get_or_create(selection_id=selection_id, defaults={'inquiry': inquiry})
        if link.inquiry_id != inquiry.pk:
            raise ValidationError('Подборка уже относится к другому обращению.')
    version = Version.objects.filter(inquiry=inquiry, digest=expected_hash).first()
    if not version:
        number = (inquiry.versions.aggregate(n=Max('number'))['n'] or 0) + 1
        version = Version.objects.create(inquiry=inquiry, number=number, snapshot=snapshot, digest=expected_hash)
    Operation.objects.create(owner=actor, key=key, fingerprint=fingerprint, version=version)
    return version


@transaction.atomic
def confirm(*, actor, version_id, expected_hash):
    _actor(actor)
    try:
        version = Version.objects.select_for_update().get(pk=version_id, inquiry__owner_id=actor.pk)
    except Version.DoesNotExist:
        raise PermissionDenied('Версия недоступна.')
    if version.snapshot.get('schema') == 2:
        from .voice import validate_version
        validate_version(version, actor)
    if version.digest != expected_hash or digest(version.snapshot) != expected_hash:
        raise ValidationError('Подтверждение относится к другой версии.')
    return Confirmation.objects.get_or_create(version=version, defaults={'actor': actor, 'digest': expected_hash})[0]


@transaction.atomic
def sync_status(*, actor, inquiry_id, system='uon'):
    _actor(actor)
    _owned(Inquiry, inquiry_id, actor)
    if system not in ADAPTERS:
        raise ValidationError('Неизвестная CRM.')
    # Deliberately no invocation of a replaceable adapter: activation needs a separate task.
    return {'status': 'not_synchronized', 'message': 'Не синхронизировано: адаптер CRM не подключён.'}


@transaction.atomic
def read(*, actor, inquiry_id):
    _actor(actor)
    inquiry = _owned(Inquiry, inquiry_id, actor)
    return {'local_id': str(inquiry.pk), 'external_id': inquiry.external_id,
            'crm_system': inquiry.crm_system, 'sync_status': inquiry.sync_status,
            'versions': list(inquiry.versions.order_by('number').values('id', 'number', 'digest', 'snapshot'))}


@transaction.atomic
def source_file(*, actor, version_id):
    """Resolve only the exact original. Never substitute a newer worksheet."""
    _actor(actor)
    try:
        version = Version.objects.get(pk=version_id, inquiry__owner_id=actor.pk)
    except Version.DoesNotExist:
        raise PermissionDenied('Версия недоступна.')
    if version.snapshot.get('schema') == 2:
        from .voice import source_file as voice_source_file
        return voice_source_file(version, actor)
    source = version.snapshot['work']
    try:
        work = WorksheetReview.objects.get(pk=source['id'], owner_id=actor.pk)
    except WorksheetReview.DoesNotExist:
        raise ValidationError('Исходник удалён или недоступен; сохранено только происхождение.')
    source_version = source.get('source_version', 1)
    original = work if source_version == 1 or not isinstance(source_version, int) else work.file_revisions.filter(number=source_version).first()
    if original is None:
        raise ValidationError('Исторический исходник недоступен.')
    data = bytes(original.worksheet_bytes)
    if hashlib.sha256(data).hexdigest() != source['source_sha256']:
        raise ValidationError('Эта версия исходника недоступна; новый файл не подставлен.')
    return data, source['source_type']
