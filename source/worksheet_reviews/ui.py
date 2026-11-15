"""Russian read-only presentation of worksheet state; no OCR or confirmations here."""
from travelcards.forms import FIELD_LABELS, STATE_LABELS, normalize_fields
from .ocr_services import current_revision

RECOGNITION_LABELS = {
    'recognized': 'Текст распознан', 'unavailable': 'Распознавание недоступно',
    'timeout': 'Время распознавания истекло', 'error': 'Ошибка распознавания',
    'no_text': 'Текст не найден', 'no_fields_found': 'Поля не определены',
    'unsupported_pdf': 'PDF: ручной ввод', 'synthetic_manual': 'Демонстрационные ручные данные',
}
SOURCE_LABELS = {'manual_review': 'Ручная сверка', 'local_tesseract': 'Локальное распознавание',
                 'manual_fallback': 'Ручной ввод', 'legacy_schema_default': 'Требует проверки',
                 'synthetic_manual': 'Синтетический пример'}


def recognition_label(metadata):
    return RECOGNITION_LABELS.get(metadata.get('status'), 'Проверьте поля вручную')


def work_state(work):
    revision = current_revision(work)
    confirmed = bool(revision and revision.confirmed_at)
    return {'confirmed': confirmed,
            'label': 'Лист сверен' if confirmed else 'Лист ожидает сверки'}


def history_rows(revisions):
    rows = []
    for revision in revisions:
        fields = normalize_fields(revision.fields)
        rows.append({'revision': revision, 'status': recognition_label(revision.metadata),
            'fields': [{'label': label, 'value': fields[key]['value'],
                        'state': STATE_LABELS.get(fields[key]['state'], 'Требует проверки'),
                        'source': SOURCE_LABELS.get(fields[key]['source'].get('kind'), 'Источник требует проверки')}
                       for key, label in FIELD_LABELS.items()]})
    return rows
