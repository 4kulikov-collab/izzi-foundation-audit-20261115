import copy
import re
import uuid

from django import forms

from presentations.models import Hotel

from .field_schema import FIELD_LABELS

_CURRENCY_CODES = re.compile(r'(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])', re.IGNORECASE)
_UNLISTED_CURRENCY_TEXT = re.compile(r'(?:¥|\b(?:currency|yen|peso|franc|baht|bat|rupee|reais)\b|бат(?:а|ов|ы)?|валют\w*)', re.IGNORECASE)
_FOREIGN_CURRENCY_MARKERS = {
    'USD': re.compile(r'\$|\b(?:USD|доллар\w*)\b', re.IGNORECASE),
    'EUR': re.compile(r'€|\b(?:EUR|евро)\b', re.IGNORECASE),
    'TRY': re.compile(r'₺|\b(?:TRY|лир\w*)\b', re.IGNORECASE),
    'KZT': re.compile(r'\b(?:KZT|тенге)\b', re.IGNORECASE),
    'BYN': re.compile(r'\b(?:BYN|белорусск\w+ рубл\w*)\b', re.IGNORECASE),
    'CNY': re.compile(r'\b(?:CNY|юан\w*|yuan)\b', re.IGNORECASE),
    'GBP': re.compile(r'£|\b(?:GBP|фунт\w*|pound\w*)\b', re.IGNORECASE),
    'AED': re.compile(r'\b(?:AED|дирхам\w*|dirham\w*)\b', re.IGNORECASE),
}
STATE_LABELS = {'known': 'Указано и проверено вручную', 'unknown': 'Не указано',
                'unreadable': 'Неразборчиво', 'conflict': 'Конфликт',
                'ocr_candidate': 'Кандидат OCR — проверить по фото',
                'not_important': 'Не важно', 'declined': 'Нет / отказ',
                'not_applicable': 'Неприменимо к этому типу поездки'}


def normalize_fields(fields):
    """Return a read-time schema view; do not mutate or re-hash legacy revisions."""
    normalized = copy.deepcopy(fields or {})
    for key in FIELD_LABELS:
        value = normalized.get(key)
        if not isinstance(value, dict):
            value = {}
        value.setdefault('value', '')
        value.setdefault('state', 'unknown')
        value.setdefault('source', {
            'kind': 'legacy_schema_default', 'page': None,
            'description': 'В прежней редакции поля не было; значение не выводится автоматически.',
        })
        if not isinstance(value.get('source'), dict):
            value['source'] = {
                'kind': 'legacy_schema_default', 'page': None,
                'description': 'Источник отсутствует в старой схеме; проверьте по исходнику.',
            }
        value['source'].setdefault('description', 'Источник отсутствует; проверьте по исходнику.')
        if key == 'budget':
            raw_currency = value.get('currency')
            text = str(value.get('value') or '')
            explicit_codes = {code.upper() for code in _CURRENCY_CODES.findall(text)}
            named_currencies = {code for code, pattern in _FOREIGN_CURRENCY_MARKERS.items()
                                if pattern.search(text)}
            mentions = explicit_codes | named_currencies
            if _UNLISTED_CURRENCY_TEXT.search(text):
                mentions.add('UNK')
            foreign_mentions = mentions - {'RUB'}
            if len(mentions) > 1:
                value['currency'] = 'UNK'
                value['state'] = 'conflict'
            elif foreign_mentions:
                detected = next(iter(foreign_mentions))
                value['currency'] = detected
                value['state'] = 'conflict'
            else:
                value['currency'] = raw_currency or 'RUB'
                if value['currency'] != 'RUB':
                    value['state'] = 'conflict'
        normalized[key] = value
    return normalized


class PhotoForm(forms.Form):
    photo = forms.FileField(label='Тестовое фото PNG или JPEG',
                            help_text='Один JPEG или PNG до 5 МБ и 12 мегапикселей; только вымышленные данные.',
                            widget=forms.ClearableFileInput(attrs={'accept': 'image/png,image/jpeg'}))


class PhotoReplaceForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput())
    expected_hash = forms.RegexField(r'^[0-9a-f]{64}$', widget=forms.HiddenInput())
    photo = forms.FileField(label='Тестовое фото PNG или JPEG',
                            help_text='Один JPEG или PNG до 5 МБ и 12 мегапикселей; только вымышленные данные.',
                            widget=forms.ClearableFileInput(attrs={'accept': 'image/png,image/jpeg'}))

    def __init__(self, *args, revision=None, **kwargs):
        super().__init__(*args, **kwargs)
        if revision:
            self.initial.update(expected_revision=revision.number, expected_hash=revision.content_hash)


class UploadForm(PhotoForm):
    title = forms.CharField(label='Название тестовой карты', max_length=160,
                           initial='Вымышленная Карта путешествия')
    upload_key = forms.UUIDField(widget=forms.HiddenInput(), initial=uuid.uuid4)


class VersionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput())
    expected_hash = forms.RegexField(r'^[0-9a-f]{64}$', widget=forms.HiddenInput())


class CardFieldsForm(VersionForm):
    def __init__(self, *args, revision=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.revision_fields = normalize_fields(revision.fields) if revision else None
        for key, label in FIELD_LABELS.items():
            self.fields[key + '_value'] = forms.CharField(label=label, required=False, max_length=600,
                                                         widget=forms.Textarea(attrs={'rows': 2}))
            self.fields[key + '_state'] = forms.ChoiceField(label='Состояние', choices=STATE_LABELS.items())
            self.fields[key + '_source'] = forms.CharField(label='Где проверено вручную', max_length=300,
                                                          help_text='Укажите строку/область фото или основание проверки OCR-кандидата.')
            if revision:
                value = self.revision_fields[key]
                self.initial.update({key + '_value': value['value'], key + '_state': value['state'],
                                     key + '_source': value['source']['description']})
        self.fields['budget_currency'] = forms.CharField(
            label='Валюта', required=False, max_length=3,
            help_text='В этом сценарии по умолчанию RUB. Другую валюту не конвертируйте; оставьте конфликт и уточните сумму в рублях.',
            widget=forms.TextInput(attrs={'maxlength': 3, 'autocomplete': 'off'}),
        )
        if revision:
            self.initial['budget_currency'] = self.revision_fields['budget'].get('currency') or 'RUB'
            self.initial.update(expected_revision=revision.number, expected_hash=revision.content_hash)

    def clean(self):
        cleaned = super().clean()
        for key in FIELD_LABELS:
            state = cleaned.get(key + '_state')
            value = cleaned.get(key + '_value', '').strip()
            if state == 'known' and not value:
                self.add_error(key + '_value', 'Для указанного значения нужен проверенный текст.')
            if state in ('unknown', 'not_important', 'declined', 'not_applicable') and value:
                self.add_error(key + '_value', 'Очистите значение либо выберите подходящее состояние.')
        currency = (cleaned.get('budget_currency') or '').strip().upper()
        if currency and not re.fullmatch(r'[A-Z]{3}', currency, flags=re.ASCII):
            self.add_error('budget_currency', 'Укажите буквенный код валюты, например RUB.')
        if len(currency) not in (0, 3):
            self.add_error('budget_currency', 'Для валюты нужен трёхбуквенный код.')
        if currency not in ('', 'RUB') and cleaned.get('budget_state') != 'conflict':
            self.add_error('budget_state', 'Иностранную валюту сохраняйте как конфликт; уточните сумму в RUB без конвертации.')
        if cleaned.get('budget_state') == 'known' and currency not in ('', 'RUB'):
            self.add_error('budget_currency', 'В этой карте подтверждается бюджет в рублях; другую валюту оставьте конфликтом и уточните сумму в RUB.')
        budget_value = cleaned.get('budget_value', '')
        explicit_codes = {code.upper() for code in _CURRENCY_CODES.findall(budget_value)}
        named_currencies = {code for code, pattern in _FOREIGN_CURRENCY_MARKERS.items()
                            if pattern.search(budget_value)}
        explicit_foreign = (explicit_codes - {'RUB'}) | named_currencies
        if _UNLISTED_CURRENCY_TEXT.search(budget_value):
            explicit_foreign.add('UNK')
        if explicit_foreign:
            if len(explicit_foreign) != 1 or currency not in explicit_foreign:
                self.add_error('budget_currency', 'Валюта в тексте бюджета не совпадает с полем валюты; уточните её, не переводя в RUB.')
            if cleaned.get('budget_state') != 'conflict':
                self.add_error('budget_state', 'Явную иностранную или неизвестную валюту оставьте конфликтом до уточнения суммы в RUB.')
        cleaned['budget_currency'] = currency or 'RUB'
        return cleaned

    def field_snapshot(self):
        snapshot = {}
        for key in FIELD_LABELS:
            value = self.cleaned_data[key + '_value'].strip()
            state = self.cleaned_data[key + '_state']
            description = self.cleaned_data[key + '_source'].strip()
            previous = self.revision_fields[key] if self.revision_fields else None
            if previous and (value, state, description) == (
                    previous['value'], previous['state'], previous['source']['description']):
                source = copy.deepcopy(previous['source'])
            else:
                source = {'kind': 'manual_review', 'page': 1, 'description': description}
            snapshot[key] = {'value': value, 'state': state, 'source': source}
        snapshot['budget']['currency'] = self.cleaned_data['budget_currency']
        return snapshot


class HotelSelectionForm(VersionForm):
    hotels = forms.ModelMultipleChoiceField(label='Предложения отелей (от 1 до 5)', queryset=Hotel.objects.all(),
                                            widget=forms.CheckboxSelectMultiple())

    def clean_hotels(self):
        hotels = self.cleaned_data['hotels']
        if not 1 <= len(hotels) <= 5:
            raise forms.ValidationError('Выберите от одного до пяти вариантов.')
        return hotels
