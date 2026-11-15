from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateAudioStorage(FileSystemStorage):
    """Local prototype storage with no URL that could expose an audio file."""

    def __init__(self):
        super().__init__(location=settings.DATA_DIR / 'private-audio', base_url=None)

    def url(self, name):
        raise ValueError('Аудио выдаётся только через маршрут с проверкой владельца.')


private_audio_storage = PrivateAudioStorage()
