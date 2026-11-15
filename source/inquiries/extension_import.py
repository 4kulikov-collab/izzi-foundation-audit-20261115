"""Local synthetic v2 input -> existing exact-offer/worksheet flow. No network transport."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from django.core.exceptions import ValidationError
from django.db import transaction

MAX = 128 * 1024
ROOT = Path(__file__).resolve().parents[1]
MESSAGES = {
    'invalid': 'Синтетическая строка отклонена: неоднозначные или несогласованные данные.',
    'incomplete': 'Импорт неполон: подтвердите запись и заполните обязательные поля источника. Подборка не создана.',
    'unsupported': 'Схема или версия расширения не поддержана. Версия 0.9 не проверена.',
}


def checked_source(raw):
    try:
        size = len(raw.encode('utf-8')) if isinstance(raw, str) else MAX + 1
    except UnicodeEncodeError:
        size = MAX + 1
    if size > MAX:
        raise ValidationError('Допустим синтетический JSON до 128 КиБ.')
    node = shutil.which('node')
    if not node:
        raise ValidationError('Локальный Node.js недоступен; импорт отключён.')
    # No shell/input paths, inherited NODE_OPTIONS, tokens or remote calls.
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'SystemRoot') if k in os.environ}
    try:
        result = subprocess.run([node, str(ROOT/'offline_contract/server_bridge.cjs')],
            input=raw.encode('utf-8'), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, check=False, env=env, cwd=ROOT)
        if len(result.stdout) > MAX * 2:
            raise ValueError()
        data = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ValidationError('Локальная проверка импорта недоступна или превысила время ожидания.') from None
    if result.returncode or data.get('status') != 'ready':
        raise ValidationError(MESSAGES.get(data.get('status'), MESSAGES['invalid']))
    return data['source']


def rubles(kopecks):
    return f'{kopecks // 100}.{kopecks % 100:02d}'


def offer_fields(source):
    n = source['selected']
    # meal is NOT a tariff, route/flightType are NOT confirmed flight details.
    return {'room_type': n['room'], 'tariff': None, 'price_basis': None,
            'base_price': rubles(n['sourcePriceKopecks']), 'currency': n['currency'], 'flight': None}


def normalize_source(value, offer):
    if type(value) is not dict or 'raw' not in value:
        raise ValidationError('Отсутствует проверяемый источник выбранной строки.')
    source = checked_source(value['raw'])
    if source != value or any(offer[k] != v for k, v in offer_fields(source).items()):
        raise ValidationError('Параметры предложения не совпадают с выбранной строкой источника.')
    return source


def public_fields(source):
    n = source['selected']
    return {'hotel_name': n['hotel']['name'], 'meal': n['meal'],
            'departure_date': n['departureDate'], 'nights': n['nights'], 'occupancy': n['occupancy'],
            'observed_at': source['provenance']['observedAt'],
            'source_label': 'Синтетическая строка структуры расширения ' + source['extension_version'] +
                            '; сайт и живое расширение не подключены.'}


def check_binding(actor, work, inquiry_id, source):
    """An owner-local draft cannot move between inquiries or reuse changed confirmation."""
    from .models import SelectedOffer, SelectionLink, WorkLink
    linked = WorkLink.objects.filter(work_id=work.pk).first()
    target = str(linked.inquiry_id) if linked else str(inquiry_id) if inquiry_id else None
    rows = SelectedOffer.objects.filter(selection__owner=actor,
        data__import_source__identity__draftId=source['identity']['draftId'])
    for row in rows:
        old = row.data['import_source']
        if (old['identity']['confirmationId'] == source['identity']['confirmationId'] and
                old['confirmation_hash'] != source['confirmation_hash']):
            raise ValidationError('Подтверждение источника повторно использовано с другими данными.')
        association = SelectionLink.objects.get(selection_id=row.selection_id)
        if str(association.inquiry_id) != target:
            raise ValidationError('Черновик источника уже связан с другим обращением.')


@transaction.atomic
def prepare_import(*, actor, work_id, expected_hash, hotel_id, raw,
                   manager_confirmed=False, inquiry_id=None, traveller_label=''):
    from presentations.models import Hotel
    from worksheet_reviews.models import WorksheetReview
    from .services import _actor, _owned
    from .manager import prepare
    from .offers import SCHEMA
    _actor(actor)
    _owned(WorksheetReview, work_id, actor)  # Before parser, never inspect another owner's work.
    if manager_confirmed is not True:
        raise ValidationError('Нужна ручная сверка и подтверждение выбранной строки менеджером.')
    source = checked_source(raw)
    try:
        hotel = Hotel.objects.get(pk=hotel_id)
    except (Hotel.DoesNotExist, ValueError, ValidationError):
        raise ValidationError('Укажите существующий синтетический отель явно.') from None
    if hotel.name != source['selected']['hotel']['name']:
        raise ValidationError('Явно указанный отель не соответствует выбранному источнику.')
    offer = {'schema': SCHEMA, 'synthetic_only': True, 'hotel_id': str(hotel.pk),
             **offer_fields(source), 'client_price': rubles(source['final_price_kopecks']),
             'selection_confirmed': True, 'import_source': source}
    return prepare(actor=actor, work_id=work_id, expected_hash=expected_hash, hotel_ids=[hotel.pk],
                   inquiry_id=inquiry_id, traveller_label=traveller_label, offer=offer)
