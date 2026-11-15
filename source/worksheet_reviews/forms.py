from io import BytesIO
import secrets

from django import forms
from django.core.exceptions import ValidationError
from .models import WorksheetReview


class HotelChoiceFieldsMixin:
    def __init__(self, *args, initial_hotels=(), owner=None, **kwargs):
        super().__init__(*args, **kwargs)
        initial_hotels = list(initial_hotels)
        for index in range(1, 4):
            initial = initial_hotels[index - 1] if len(initial_hotels) >= index else {}
            self.fields[f'hotel_{index}_name'] = forms.CharField(
                label=f'Отель {index} — название', max_length=160, required=False,
                widget=forms.TextInput(attrs={'autocomplete': 'off'}),
                initial=initial.get('name', ''),
            )
            self.fields[f'hotel_{index}_reference'] = forms.CharField(
                label='Ссылка или пояснение из подбора', required=False, max_length=1600,
                widget=forms.Textarea(attrs={'rows': 2, 'spellcheck': 'false'}),
                initial=initial.get('reference_text', ''),
            )
        self.owner = owner

    def clean(self):
        cleaned = super().clean()
        for index in range(1, 4):
            if cleaned.get(f'hotel_{index}_reference') and not cleaned.get(f'hotel_{index}_name'):
                self.add_error(f'hotel_{index}_name', 'Укажите название для этой заметки или оставьте оба поля пустыми.')
        work_code = cleaned.get('work_code')
        if work_code and self.owner and WorksheetReview.objects.filter(owner=self.owner, work_code=work_code).exists():
            self.add_error('work_code', 'Этот код карты уже сохранён. Откройте существующую работу.')
        return cleaned

    def hotel_choice_values(self):
        return [
            {'position': index, 'name': self.cleaned_data[f'hotel_{index}_name'],
             'reference_text': self.cleaned_data[f'hotel_{index}_reference']}
            for index in range(1, 4) if self.cleaned_data.get(f'hotel_{index}_name', '').strip()
        ]


class WorksheetUploadForm(HotelChoiceFieldsMixin, forms.Form):
    work_code = forms.RegexField(
        label='Код карты', regex=r'^[A-Z0-9-]{3,32}$', max_length=32,
        error_messages={'invalid': 'Используйте код карты из 3–32 латинских букв, цифр или дефисов.'},
        help_text='Без ФИО и контактов. Только вымышленный код для прототипа.',
        widget=forms.TextInput(attrs={'autocomplete': 'off', 'pattern': '[A-Z0-9-]{3,32}'}),
    )
    worksheet = forms.FileField(
        label='Лист подбора (PDF, JPEG или PNG)',
        help_text='Только синтетический лист. Локальный предел прототипа: 5 МБ; PDF до 20 страниц.',
        widget=forms.ClearableFileInput(attrs={'accept': 'application/pdf,image/jpeg,image/png'}),
    )
    expert_overview = forms.CharField(
        label='Экспертный обзор Никиты', required=False, max_length=5000,
        help_text='Ручная заметка эксперта. Сохраняется отдельно от фактов об отеле.',
        widget=forms.Textarea(attrs={'rows': 8}),
    )

    def clean_worksheet(self):
        upload = self.cleaned_data['worksheet']
        data, mime = inspect_worksheet(upload)
        upload.seek(0)
        self.cleaned_data['_worksheet_bytes'] = data
        self.cleaned_data['_worksheet_type'] = mime
        return upload


def inspect_worksheet(upload):
    if upload.size > 5 * 1024 * 1024:
        raise ValidationError('Лист превышает локальный предел прототипа 5 МБ.')
    data = upload.read(5 * 1024 * 1024 + 1)
    if not data or len(data) > 5 * 1024 * 1024:
        raise ValidationError('Файл пустой или превышает локальный предел 5 МБ.')

    if data.startswith(b'%PDF-'):
        if upload.content_type not in ('application/pdf', 'application/octet-stream'):
            raise ValidationError('Заявленный тип не совпадает с PDF-файлом.')
        try:
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(data), strict=True)
            if reader.is_encrypted or not 1 <= len(reader.pages) <= 20:
                raise ValidationError('Для теста нужен незашифрованный PDF на 1–20 страниц.')
        except ValidationError:
            raise
        except Exception:
            raise ValidationError('PDF повреждён или не читается.')
        mime = 'application/pdf'
    else:
        from PIL import Image, UnidentifiedImageError
        try:
            with Image.open(BytesIO(data)) as image:
                if image.format not in ('PNG', 'JPEG') or image.width * image.height > 12_000_000:
                    raise ValidationError('Принимается PNG/JPEG до 12 мегапикселей.')
                fmt = image.format
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
        except ValidationError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
            raise ValidationError('Файл не является исправным PNG/JPEG.')
        mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg'}[fmt]
        if upload.content_type not in (mime, 'application/octet-stream'):
            raise ValidationError('Заявленный тип не совпадает с изображением.')
    return data, mime


class WorksheetSourceReplaceForm(forms.Form):
    expected_source_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput())
    expected_source_hash = forms.RegexField(r'^[0-9a-f]{64}$', widget=forms.HiddenInput())
    worksheet = forms.FileField(
        label='Новое фото / файл листа (будет создана новая исходная версия)',
        help_text='Заменённый оригинал останется в истории. Только синтетический файл до 5 МБ.',
        widget=forms.ClearableFileInput(attrs={'accept': 'application/pdf,image/jpeg,image/png'}),
    )

    def __init__(self, *args, source=None, **kwargs):
        super().__init__(*args, **kwargs)
        if source is not None:
            self.initial.update(expected_source_version=source.number if hasattr(source, 'number') else 1,
                                expected_source_hash=source.worksheet_sha256)

    def clean_worksheet(self):
        upload = self.cleaned_data['worksheet']
        data, mime = inspect_worksheet(upload)
        self.cleaned_data['_worksheet_bytes'] = data
        self.cleaned_data['_worksheet_type'] = mime
        return upload


class WorksheetReviewForm(HotelChoiceFieldsMixin, forms.Form):
    expert_overview = forms.CharField(
        label='Экспертный обзор Никиты', required=False, max_length=5000,
        widget=forms.Textarea(attrs={'rows': 8}),
    )


class DeleteWorkConfirmationForm(forms.Form):
    confirm_id = forms.CharField(label='Идентификатор работы', max_length=36,
                                 widget=forms.TextInput(attrs={'autocomplete': 'off'}))

    def __init__(self, *args, expected_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected_id = expected_id

    def clean_confirm_id(self):
        entered = self.cleaned_data['confirm_id'].strip()
        if not secrets.compare_digest(entered, self.expected_id):
            raise ValidationError('Идентификатор не совпадает; работа не удалена.')
        return entered
