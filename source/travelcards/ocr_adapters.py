"""Swappable photo-to-text contract. The production candidate is intentionally disabled."""
from dataclasses import dataclass


class OCRAdapterDisabled(RuntimeError):
    pass


@dataclass(frozen=True)
class OCRBlock:
    text: str
    page: int | None = None
    bounds: tuple = ()
    confidence: float | None = None


@dataclass(frozen=True)
class OCRResult:
    text: str
    blocks: tuple = ()
    uncertainties: tuple = ()
    language_code: str = 'ru'
    adapter: str = 'mock-vision-ocr'
    is_mock: bool = True


class YandexVisionOCRAdapter:
    """Candidate for image -> text. No endpoint, credentials, SDK or network path configured."""
    name = 'yandex-vision-ocr-candidate'

    def recognize(self, image_bytes, mime_type):
        raise OCRAdapterDisabled(
            'Yandex Vision OCR — кандидат; API, ограничения, условия и стоимость не подтверждены.')


class MockVisionOCRAdapter:
    """Test-only canned text; does not inspect its image argument or make a network call."""
    name = 'mock-vision-ocr'

    def __init__(self, result):
        self.result = result

    def recognize(self, image_bytes, mime_type):
        return self.result


def photo_ocr_adapter():
    """Return the disabled current candidate; photo cards continue using the explicit stub."""
    return YandexVisionOCRAdapter()
