import hashlib
import wave

from django.core.exceptions import ValidationError

# Temporary component-test limits; not approved production limits or a provider specification.
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_AUDIO_SECONDS = 300


def inspect_audio(upload):
    if upload.size <= 0 or upload.size > MAX_AUDIO_BYTES:
        raise ValidationError('Тестовый WAV должен быть непустым и не превышать 5 МиБ.')
    upload.seek(0)
    data = upload.read(MAX_AUDIO_BYTES + 1)
    upload.seek(0)
    try:
        if len(data) > MAX_AUDIO_BYTES:
            raise ValueError
        import io
        with wave.open(io.BytesIO(data), 'rb') as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            rate = stream.getframerate()
            frames = stream.getnframes()
            frame_data = stream.readframes(frames)
            duration = frames / rate if rate else 0
            if (channels not in (1, 2) or sample_width not in (1, 2, 3, 4)
                    or rate < 8000 or rate > 96000 or frames <= 0
                    or len(frame_data) != frames * channels * sample_width
                    or duration > MAX_AUDIO_SECONDS):
                raise ValueError
    except (wave.Error, EOFError, OSError, ValueError, ZeroDivisionError):
        raise ValidationError('Файл повреждён или не соответствует тестовому формату PCM WAV до 5 минут.')
    return {'sha256': hashlib.sha256(data).hexdigest(), 'duration_seconds': max(1, int(duration + 0.999))}
