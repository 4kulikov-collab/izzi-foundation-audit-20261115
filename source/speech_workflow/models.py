import uuid

from django.conf import settings
from django.db import models

from .storage import private_audio_storage


def private_audio_path(instance, filename):
    return f'{instance.owner_id}/{instance.pk}/{uuid.uuid4().hex}.wav'


class VoiceSubmission(models.Model):
    class Status(models.TextChoices):
        MANUAL_TRANSCRIPTION = 'manual_transcription', 'Нужен ручной ввод расшифровки'
        TRANSCRIPT_READY = 'transcript_ready', 'Есть черновик расшифровки'
        CONFIRMED = 'confirmed', 'Расшифровка подтверждена'
        CANCELLED = 'cancelled', 'Работа отменена'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='voice_submissions')
    idempotency_key = models.UUIDField()
    audio = models.FileField(storage=private_audio_storage, upload_to=private_audio_path)
    audio_sha256 = models.CharField(max_length=64)
    duration_seconds = models.PositiveIntegerField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.MANUAL_TRANSCRIPTION)
    current_revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['owner', 'idempotency_key'], name='uniq_voice_owner_idempotency')]


class TranscriptRevision(models.Model):
    submission = models.ForeignKey(VoiceSubmission, on_delete=models.CASCADE, related_name='revisions')
    number = models.PositiveIntegerField()
    source_sha256 = models.CharField(max_length=64)
    language_code = models.CharField(max_length=16, default='ru-RU')
    processor = models.CharField(max_length=48, default='manual')
    processor_version = models.CharField(max_length=48, blank=True)
    is_mock = models.BooleanField(default=False)
    text = models.TextField(max_length=12000, blank=True)
    segments = models.JSONField(default=list, blank=True)
    uncertainties = models.JSONField(default=list, blank=True)
    content_hash = models.CharField(max_length=64)
    authored_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmation_revoked_at = models.DateTimeField(null=True, blank=True)
    confirmation_revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                                 on_delete=models.PROTECT, related_name='revoked_transcripts')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['number']
        constraints = [models.UniqueConstraint(fields=['submission', 'number'], name='uniq_voice_transcript_revision')]


class HotelObservationDraft(models.Model):
    class Experience(models.TextChoices):
        INSPECTION = 'inspection', 'Осмотр'
        PERSONAL_STAY = 'personal_stay', 'Личное проживание'
        REPRESENTATIVE = 'representative', 'Слова представителя отеля'
        TRAVELLER_REPORT = 'traveller_report', 'Пересказ туриста'
        OTHER = 'other', 'Другое / нужно уточнить'

    class Status(models.TextChoices):
        NEEDS_REVIEW = 'needs_review', 'Ожидает проверки менеджером'
        WITHDRAWN = 'withdrawn', 'Отозван вместе с подтверждением расшифровки'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transcript_revision = models.OneToOneField(TranscriptRevision, on_delete=models.PROTECT, related_name='observation_draft')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    hotel_name = models.CharField(max_length=160)
    observed_on = models.DateField()
    building_room = models.CharField(max_length=160, blank=True)
    theme = models.CharField(max_length=100)
    experience = models.CharField(max_length=24, choices=Experience.choices)
    observation_text = models.TextField(max_length=5000)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.NEEDS_REVIEW)
    created_at = models.DateTimeField(auto_now_add=True)


class VoiceInquiryDraft(models.Model):
    """Private pre-inquiry draft; never an active ledger/CRM record by itself."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transcript_revision = models.OneToOneField(TranscriptRevision, on_delete=models.PROTECT,
                                               related_name='inquiry_draft')
    target_inquiry = models.ForeignKey('inquiries.Inquiry', null=True, blank=True, on_delete=models.PROTECT)
    fields = models.JSONField()
    reply_text = models.TextField(max_length=2000, blank=True)
    is_mock = models.BooleanField(default=True)
    content_hash = models.CharField(max_length=64)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
