"""Validate untrusted synthetic adapter results before persisting or exposing them."""
import json
import math
import re
from django.core.exceptions import ValidationError
from .adapters import TranscriptResult, InquiryDraftResult

FIELDS = ('party', 'children_count', 'dates', 'budget', 'trip_type', 'beach', 'wishes')


def validate_transcript(result):
    def invalid():
        raise ValidationError('Некорректный результат STT; исходник сохранён, доступен ручной ввод.')
    if not isinstance(result, TranscriptResult) or type(result.is_mock) is not bool:
        invalid()
    if not isinstance(result.text, str) or len(result.text) > 12000:
        invalid()
    if (result.language_code != 'ru-RU' or not isinstance(result.adapter, str)
            or result.adapter not in ('mock-speechkit', 'yandex-speechkit-stt')
            or not isinstance(result.version, str) or len(result.version) > 48):
        invalid()
    if not isinstance(result.segments, (tuple, list)) or not isinstance(result.uncertainties, (tuple, list)):
        invalid()
    if len(result.segments) > 500 or len(result.uncertainties) > 500:
        invalid()
    for segment in result.segments:
        if not isinstance(segment, dict) or not {'start', 'end', 'text'} <= segment.keys() or set(segment) - {'start', 'end', 'text', 'speaker'}:
            invalid()
        start, end = segment['start'], segment['end']
        if (type(start) not in (float, int) or type(end) not in (float, int)
                or not math.isfinite(start) or not math.isfinite(end) or not 0 <= start <= end <= 300
                or not isinstance(segment['text'], str) or len(segment['text']) > 12000
                or segment.get('speaker') is not None):
            invalid()
    if any(not isinstance(x, str) or len(x) > 600 for x in result.uncertainties):
        invalid()
    if len(json.dumps([result.segments, result.uncertainties], ensure_ascii=False).encode()) > 64000:
        invalid()
    return result


def validate_draft(result):
    if (not isinstance(result, InquiryDraftResult) or result.is_mock is not True
            or not isinstance(result.fields, dict) or set(result.fields) != set(FIELDS)
            or any(not isinstance(v, str) or len(v) > 600 for v in result.fields.values())
            or not isinstance(result.reply_text, str) or len(result.reply_text) > 2000):
        raise ValidationError('Нужна полная синтетическая структура семи полей без дополнительных данных.')
    if result.fields['budget'] and not re.fullmatch(r'[1-9][0-9]{0,8}', result.fields['budget']):
        raise ValidationError('Синтетический бюджет: целое положительное число рублей без обозначения другой валюты; либо пусто.')
    # No extraction/normalization or invented defaults. Currency is always RUB.
    return {key: {'value': value, 'state': 'candidate' if value.strip() else 'unknown'}
            for key, value in result.fields.items()}
