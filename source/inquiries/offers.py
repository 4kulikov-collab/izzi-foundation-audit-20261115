"""Strict local synthetic offer contract; no extension, supplier or CRM transport."""
from copy import deepcopy
from decimal import Decimal
import re
from uuid import UUID
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction

SCHEMA = 'izzi.local.selected-offer.v1'
FIELDS = {'schema', 'synthetic_only', 'hotel_id', 'room_type', 'tariff', 'price_basis',
          'base_price', 'client_price', 'currency', 'selection_confirmed', 'flight', 'import_source'}
LABELS = {'hotel_id': 'отель', 'room_type': 'тип номера', 'tariff': 'тариф',
          'price_basis': 'за что указана цена', 'base_price': 'базовая цена',
          'client_price': 'клиентская цена', 'currency': 'валюта',
          'selection_confirmed': 'явный выбор менеджера'}


def text(value, name, limit=200):
    if value is None or value == '':
        return None
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValidationError('Недопустимое поле предложения: '+name+'.')
    value = value.strip()
    if value.casefold() in ('неизвестно', 'не указано', 'уточнить', 'нет данных', 'не подтверждено',
                            'не определено', 'unknown', 'n/a', 'tbd', '?', '—', '-'):
        return None
    return value or None


def money(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,2})?', value):
        raise ValidationError('Цена должна быть строкой рублей с максимум двумя знаками после точки.')
    amount = Decimal(value)
    if not Decimal('0') < amount <= Decimal('1000000000'):
        raise ValidationError('Цена вне допустимого диапазона.')
    return format(amount, '.2f')


def normalize(data, hotel_ids):
    if type(data) is not dict or set(data)-FIELDS:
        raise ValidationError('Неизвестные поля предложения.')
    if data.get('schema') != SCHEMA or data.get('synthetic_only') is not True:
        raise ValidationError('Поддержан только локальный синтетический контракт v1.')
    ids = [str(x) for x in hotel_ids]
    if len(ids) != 1:
        raise ValidationError('Для точного предложения выберите ровно один отель.')
    hotel = data.get('hotel_id')
    if hotel is not None:
        try:
            hotel = str(UUID(hotel)) if isinstance(hotel, str) else None
        except (ValueError, TypeError):
            hotel = None
        if hotel != ids[0]:
            raise ValidationError('Предложение относится к другому или неизвестному отелю.')
    currency = data.get('currency')
    if currency == '':
        currency = None
    if currency is not None and (type(currency) is not str or currency != 'RUB'):
        raise ValidationError('В локальном контракте поддержаны только рубли RUB; конвертации нет.')
    confirmed = data.get('selection_confirmed', False)
    if type(confirmed) is not bool:
        raise ValidationError('Нужно явное подтверждение выбранных параметров.')
    flight = data.get('flight')
    if flight is not None:
        if type(flight) is not dict or set(flight) != {'confirmed', 'details', 'evidence'} or flight['confirmed'] is not True:
            raise ValidationError('Перелёт допустим только с подтверждением, сведениями и основанием.')
        flight = {'confirmed': True, 'details': text(flight['details'], 'перелёт', 400),
                  'evidence': text(flight['evidence'], 'основание перелёта', 400)}
        if not flight['details'] or not flight['evidence']:
            raise ValidationError('Неполное подтверждение перелёта.')
    result = {'schema': SCHEMA, 'synthetic_only': True, 'hotel_id': hotel,
            **{key: text(data.get(key), LABELS[key]) for key in ('room_type', 'tariff', 'price_basis')},
            'base_price': money(data.get('base_price')), 'client_price': money(data.get('client_price')),
            'currency': currency, 'selection_confirmed': confirmed, 'flight': flight}

    if 'import_source' in data:
        from .extension_import import normalize_source
        result['import_source'] = normalize_source(data['import_source'], result)
    return result


def read(selection):
    from .models import SelectedOffer
    from .services import digest
    row = SelectedOffer.objects.filter(selection_id=selection.pk).first()
    if row is None:
        return None
    current = normalize(row.data, selection.selected_hotels.values_list('pk', flat=True))
    if current != row.data or digest(current) != row.digest:
        raise ValidationError('Нарушена целостность выбранного предложения.')
    return deepcopy(current)


def public(data):
    missing = [LABELS[key] for key in LABELS if not data.get(key)]
    # Base price and evidence never leave the private ledger/manager preview.
    complete = not missing
    result = {'status': 'complete' if complete else 'incomplete', 'missing': missing,
            'room_type': data['room_type'], 'tariff': data['tariff'], 'price_basis': data['price_basis'],
            'client_price': data['client_price'], 'currency': data['currency'],
            'client_price_display': (format(Decimal(data['client_price']), ',.2f').replace(',', ' ').replace('.', ',')
                                     if data['client_price'] else None),
            'flight': {'details': data['flight']['details']} if data['flight'] else None}

    if data.get('import_source'):
        from .extension_import import public_fields
        result['imported'] = public_fields(data['import_source'])
    return result


@transaction.atomic
def reprice(*, actor, selection_id, expected_hash, client_price):
    from presentations.models import TravelRequest
    from presentations.services import build_snapshot, snapshot_hash
    from .services import _actor, _owned
    from .models import WorksheetSelection, SelectionLink
    from .manager import prepare, ensure_ready
    _actor(actor)
    selection = _owned(TravelRequest, selection_id, actor)
    ensure_ready(selection, actor)
    if snapshot_hash(build_snapshot(selection)) != expected_hash:
        raise ValidationError('Предложение изменилось. Откройте актуальную версию.')
    data = read(selection)
    if data is None:
        raise ValidationError('Сначала укажите выбранное предложение.')
    source = WorksheetSelection.objects.filter(selection=selection).first()
    if not source:
        raise ValidationError('Корректировка цены сейчас поддержана для листового сценария.')
    price = money(client_price)
    if price is None:
        raise ValidationError('Укажите клиентскую цену.')
    data['client_price'] = price
    return prepare(actor=actor, work_id=source.work_id, expected_hash=source.ocr_revision.content_hash,
                   hotel_ids=[data['hotel_id']] if data['hotel_id'] else list(selection.selected_hotels.values_list('pk', flat=True)),
                   inquiry_id=SelectionLink.objects.get(selection=selection).inquiry_id, offer=data)
