"""Local photo upload, offline OCR draft, and manager confirmation contract."""
import copy
import hashlib
import io
import json
import re
import secrets
import warnings

from PIL import Image, UnidentifiedImageError
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from presentations.models import TravelRequest
from .forms import FIELD_LABELS, STATE_LABELS, normalize_fields
from .models import CardRevision, CardSource, PhotoCard
from .ocr import RECOGNITION_KEY, recognize_card_photo

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 12_000_000


class StalePhotoRevision(ValidationError):
    """The upload form was rendered for an older photo-card revision."""


def inspect_photo(upload):
    if upload.size > MAX_BYTES:
        raise ValidationError('Фото превышает тестовый предел 5 МБ.')
    raw = upload.read(MAX_BYTES + 1)
    if not raw or len(raw) > MAX_BYTES:
        raise ValidationError('Фото пустое или превышает тестовый предел 5 МБ.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as img:
                if img.format not in ('PNG', 'JPEG'):
                    raise ValidationError('Принимаются только настоящие PNG и JPEG.')
                if img.width * img.height > MAX_PIXELS:
                    raise ValidationError('Фото превышает тестовый предел 12 мегапикселей.')
                image_format, width, height = img.format, img.width, img.height
                if getattr(img, 'n_frames', 1) != 1:
                    raise ValidationError('Для опыта требуется один неподвижный кадр.')
                img.verify()
            with Image.open(io.BytesIO(raw)) as img:
                img.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, ValueError):
        raise ValidationError('Файл повреждён или не является допустимым фото.')
    mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg'}[image_format]
    if upload.content_type not in (mime, 'application/octet-stream'):
        raise ValidationError('Заявленный тип файла не совпадает с его содержимым.')
    return {'image_bytes': raw, 'sha256': hashlib.sha256(raw).hexdigest(),
            'mime_type': mime, 'width': width, 'height': height}


def digest(source, fields):
    payload = {'source_sha256': source.sha256, 'fields': fields, 'schema': 'temporary-photo-card-v1'}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _card(pk, actor):
    card = PhotoCard.objects.select_for_update().get(pk=pk)
    if not actor.is_authenticated or not actor.is_active or card.owner_id != actor.pk:
        raise PermissionDenied
    return card


def current_revision(card):
    return card.revisions.select_related('source').get(number=card.current_revision)


def check_version(card, number, expected_hash):
    revision = current_revision(card)
    if number != revision.number or not re.fullmatch(r'[0-9a-f]{64}', expected_hash or '') or not secrets.compare_digest(revision.content_hash, expected_hash):
        raise ValidationError('Карта изменилась. Откройте текущую версию и проверьте её заново.')
    return revision


def create_card(actor, title, upload_key, photo):
    if not actor.is_authenticated:
        raise PermissionDenied

    # Authentication state on request.user may be stale while OCR runs. Lock
    # and re-read the authoritative DB owner before and after the slow OCR step.
    with transaction.atomic():
        owner = get_user_model().objects.select_for_update().filter(pk=actor.pk).first()
        if owner is None or not owner.is_active:
            raise PermissionDenied
        existing = PhotoCard.objects.select_for_update().filter(owner=owner, upload_key=upload_key).first()
        if existing:
            return existing
    validated = inspect_photo(photo)
    fields, recognition = recognize_card_photo(validated['image_bytes'])
    fields[RECOGNITION_KEY] = recognition
    try:
        with transaction.atomic():
            owner = get_user_model().objects.select_for_update().filter(pk=actor.pk).first()
            if owner is None or not owner.is_active:
                raise PermissionDenied
            existing = PhotoCard.objects.select_for_update().filter(owner=owner, upload_key=upload_key).first()
            if existing:
                return existing
            card = PhotoCard.objects.create(owner=owner, title=title, upload_key=upload_key)
            source = CardSource.objects.create(card=card, **validated)
            CardRevision.objects.create(card=card, number=1, source=source, fields=fields,
                                        content_hash=digest(source, fields))
            return card
    except IntegrityError:
        # A concurrent retry may win the unique owner/upload-key constraint.
        with transaction.atomic():
            owner = get_user_model().objects.select_for_update().filter(pk=actor.pk).first()
            if owner is None or not owner.is_active:
                raise PermissionDenied
            existing = PhotoCard.objects.filter(owner=owner, upload_key=upload_key).first()
            if existing:
                return existing
        raise


def _new_revision(card, source, fields):
    card.current_revision += 1
    card.save(update_fields=['current_revision'])
    if card.travel_request_id:
        TravelRequest.objects.filter(pk=card.travel_request_id).update(revision=F('revision') + 1)
    return CardRevision.objects.create(card=card, number=card.current_revision, source=source, fields=fields,
                                       content_hash=digest(source, fields))


@transaction.atomic
def replace_photo(card, actor, expected_revision, expected_hash, photo):
    card = _card(card.pk, actor)
    validated = inspect_photo(photo)
    previous = current_revision(card)
    version_is_current = (
        expected_revision == previous.number
        and isinstance(expected_hash, str)
        and re.fullmatch(r'[0-9a-f]{64}', expected_hash)
        and secrets.compare_digest(previous.content_hash, expected_hash)
    )
    if not version_is_current:
        # Exact replay of a completed upload is a no-op; a different stale image
        # cannot roll the current source backward or create a new revision.
        if previous.source.sha256 == validated['sha256']:
            return previous
        raise StalePhotoRevision(
            'Карта изменилась в другой вкладке. Обновите страницу и повторите замену.'
        )
    if previous.source.sha256 == validated['sha256']:
        return previous
    source, _ = CardSource.objects.get_or_create(card=card, sha256=validated['sha256'], defaults=validated)
    fields = normalize_fields(previous.fields)
    for key in FIELD_LABELS:
        value = fields[key]
        value['source'] = {'kind': 'carried_forward', 'page': None,
                           'description': 'Перенесено с прежнего фото; требуется ручная сверка с новым.'}
        if value['state'] == 'known':
            value['state'] = 'conflict'
    fields[RECOGNITION_KEY] = {
        'engine': 'Tesseract', 'version': '', 'language': 'rus',
        'status': 'replacement_not_processed',
        'note': 'Новое фото сохранено, OCR для замены не запускался; значения перенесены для ручной сверки.',
    }
    return _new_revision(card, source, fields)


@transaction.atomic
def save_fields(card, actor, number, expected_hash, fields):
    card = _card(card.pk, actor)
    previous = check_version(card, number, expected_hash)
    normalized_previous = normalize_fields(previous.fields)
    previous_field_values = {key: normalized_previous[key] for key in FIELD_LABELS}
    if fields == previous_field_values:
        return previous
    snapshot = copy.deepcopy(fields)
    if RECOGNITION_KEY in previous.fields:
        snapshot[RECOGNITION_KEY] = copy.deepcopy(previous.fields[RECOGNITION_KEY])
    return _new_revision(card, previous.source, snapshot)


@transaction.atomic
def confirm_card(card, actor, number, expected_hash):
    card = _card(card.pk, actor)
    revision = check_version(card, number, expected_hash)
    fields = normalize_fields(revision.fields)
    pending = [key for key in FIELD_LABELS if fields[key]['state'] == 'ocr_candidate']
    if pending:
        labels = ', '.join(FIELD_LABELS[key] for key in pending)
        raise ValidationError(f'Сначала проверьте кандидаты OCR по фотографии: {labels}.')
    if revision.confirmed_at is None:
        revision.confirmed_at = timezone.now()
        revision.confirmed_by = actor
        revision.save(update_fields=['confirmed_at', 'confirmed_by'])
    return revision


def ensure_card_ready(travel_request):
    card = PhotoCard.objects.filter(travel_request=travel_request).first()
    if card:
        revision = current_revision(card)
        fields = normalize_fields(revision.fields)
        if any(fields[key]['state'] == 'ocr_candidate' for key in FIELD_LABELS):
            raise ValidationError('Проверьте все кандидаты OCR по фотографии перед использованием карты.')
        request_values = _request_values(card, revision)
        if revision.confirmed_at is None or card.synced_revision != revision.number:
            raise ValidationError('Карта по фото изменена. Подтвердите новую версию и обновите подборку из карты.')
        if any(getattr(travel_request, name) != request_values[name]
               for name in ('traveller_summary', 'requirements')):
            raise ValidationError(
                'Поля связанной подборки отличаются от карты. Исправьте их в карте, '
                'создайте и подтвердите новую редакцию перед одобрением.'
            )


def _display(field):
    if field['state'] == 'known':
        return field['value']
    # Unreadable/conflicting tentative values are never promoted to known requirements.
    return STATE_LABELS[field['state']]


def _request_values(card, revision):
    fields = normalize_fields(revision.fields)
    return {
        'title': card.title,
        'traveller_summary': f"{_display(fields['party'])} · дети: {_display(fields['children_count'])}",
        'requirements': '\n'.join(
            f'{FIELD_LABELS[key]}: {_display(fields[key])}'
            for key in ('dates', 'budget', 'trip_type', 'beach', 'wishes')
        ),
    }


@transaction.atomic
def prepare_request(card, actor, number, expected_hash, hotels):
    card = _card(card.pk, actor)
    revision = check_version(card, number, expected_hash)
    if revision.confirmed_at is None:
        raise ValidationError('Сначала подтвердите точную версию карты.')
    if not 1 <= len(hotels) <= 5:
        raise ValidationError('Выберите от одного до пяти вариантов.')
    request_values = _request_values(card, revision)
    if card.travel_request_id:
        request = TravelRequest.objects.select_for_update().get(pk=card.travel_request_id, owner=actor)
        if card.synced_revision == revision.number and any(
                getattr(request, name) != request_values[name]
                for name in ('traveller_summary', 'requirements')):
            raise ValidationError(
                'Поля связанной подборки отличаются от карты. Исправьте их в карте, '
                'создайте и подтвердите новую редакцию перед переходом.'
            )
        if card.synced_revision == revision.number and set(request.selected_hotels.all()) == set(hotels):
            return request
        request.revision += 1
    else:
        request = TravelRequest(owner=actor)
    request.title = request_values['title']
    request.traveller_summary = request_values['traveller_summary']
    request.requirements = request_values['requirements']
    request.save()
    request.selected_hotels.set(hotels)
    card.travel_request = request
    card.synced_revision = revision.number
    card.save(update_fields=['travel_request', 'synced_revision'])
    return request
