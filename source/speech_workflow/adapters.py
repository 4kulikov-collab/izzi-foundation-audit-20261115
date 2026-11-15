"""Provider contracts only. Live network implementations are intentionally absent."""
from dataclasses import dataclass


class AdapterDisabled(RuntimeError):
    pass


class ProviderFailure(RuntimeError):
    def __init__(self, category, *, retryable=False):
        category = category if category in ('timeout', 'http_429', 'access_denied', 'provider_error', 'invalid_result', 'attempts_exhausted') else 'provider_error'
        super().__init__(category)
        self.category = category
        self.retryable = retryable


class AdapterCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    segments: tuple = ()
    uncertainties: tuple = ()
    adapter: str = 'mock-speechkit'
    is_mock: bool = True
    language_code: str = 'ru-RU'
    version: str = ''


@dataclass(frozen=True)
class ObservationResult:
    fields: dict
    adapter: str = 'mock-luna'
    is_mock: bool = True


class YandexSpeechKitAdapter:
    """Switchable STT contract. No credentials, HTTP client, or network path is configured."""
    name = 'yandex-speechkit-stt'

    def transcribe(self, audio_file):
        raise AdapterDisabled('SpeechKit выключен: API, доступность, формат и тариф не проверены.')


class GPT6LunaAdapter:
    """Switchable LLM contract. No endpoint, model ID, credentials, or network path is configured."""
    name = 'gpt-6-luna-llm'

    def structure_observation(self, confirmed_text):
        raise AdapterDisabled('Luna выключена: API-идентификатор, доступность и тариф не проверены.')


class MockSpeechKitAdapter:
    """Test-only canned result; never inspects audio and never makes a network call."""
    name = 'mock-speechkit'

    def __init__(self, result):
        self.result = result

    def transcribe(self, audio_file):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class MockLunaAdapter:
    """Test-only canned structure; it does not infer from transcript text."""
    name = 'mock-luna'

    def __init__(self, result):
        self.result = result

    def structure_observation(self, confirmed_text):
        return self.result


def speech_to_text_adapter():
    return YandexSpeechKitAdapter()


def language_model_adapter():
    return GPT6LunaAdapter()


def run_stt_with_bounded_retries(adapter, audio_file, *, cancelled=lambda: False, max_attempts=2):
    """Injected adapter seam for component tests; the UI/default flow never calls it."""
    if max_attempts < 1 or max_attempts > 2:
        raise ValueError('Тестовый контракт допускает не более двух попыток.')
    for attempt in range(max_attempts):
        if cancelled():
            raise AdapterCancelled('Задача отменена до применения результата.')
        if hasattr(audio_file, 'seek'):
            audio_file.seek(0)
        try:
            result = safe_adapter_call(lambda: adapter.transcribe(audio_file))
        except ProviderFailure as error:
            if error.retryable and attempt + 1 < max_attempts:
                continue
            raise
        if cancelled():
            raise AdapterCancelled('Задача отменена; поздний результат отброшен.')
        return result
    raise ProviderFailure('attempts_exhausted', retryable=False)


@dataclass(frozen=True)
class InquiryDraftResult:
    """Untrusted local suggestion; not an inquiry or a CRM operation."""
    fields: dict
    reply_text: str = ''
    is_mock: bool = True


class VoiceInquiryAdapter:
    def draft_inquiry(self, confirmed_text):
        raise AdapterDisabled('Подготовка обращения из голоса выключена; внешний API не подключён.')


class MockVoiceInquiryAdapter:
    """Canned fields, no inference from speech or transcript."""
    def __init__(self, result):
        self.result = result

    def draft_inquiry(self, confirmed_text):
        return self.result


@dataclass(frozen=True)
class VoiceReplyResult:
    audio: bytes
    text_sha256: str
    voice: str = 'luna-contract'
    media_type: str = 'audio/wav'
    is_mock: bool = True


class LunaVoiceReplyAdapter:
    """Requested voice contract only, NOT a verified provider/model/voice identifier."""
    def synthesize(self, approved_text, *, timeout_seconds):
        raise AdapterDisabled('Голосовой ответ Luna выключен: поставщик, API и голос не подтверждены.')


class MockLunaVoiceReplyAdapter:
    """Returns synthetic WAV bytes; never produces speech or sends messages."""
    def __init__(self, result):
        self.result = result

    def synthesize(self, approved_text, *, timeout_seconds):
        return self.result


def safe_adapter_call(call):
    """No credentials/URLs/provider bodies in exposed errors; no implicit fallback."""
    try:
        return call()
    except AdapterDisabled:
        raise AdapterDisabled("Адаптер выключен; внешняя обработка не выполнялась.") from None
    except AdapterCancelled:
        raise AdapterCancelled("Работа отменена; результат отброшен.") from None
    except TimeoutError:
        raise ProviderFailure('timeout', retryable=True) from None
    except ProviderFailure as error:
        raise ProviderFailure(error.category, retryable=error.retryable) from None
    except Exception:
        raise ProviderFailure('provider_error', retryable=False) from None
