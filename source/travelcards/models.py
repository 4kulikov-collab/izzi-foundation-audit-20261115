import uuid

from django.conf import settings
from django.db import models


class PhotoCard(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    title = models.CharField('Название тестовой карты', max_length=160)
    upload_key = models.UUIDField()
    current_revision = models.PositiveIntegerField(default=1)
    travel_request = models.OneToOneField('presentations.TravelRequest', null=True, blank=True,
                                         on_delete=models.PROTECT, related_name='source_card')
    synced_revision = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['owner', 'upload_key'], name='photo_card_upload_key')]


class CardSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    card = models.ForeignKey(PhotoCard, on_delete=models.PROTECT, related_name='sources')
    sha256 = models.CharField(max_length=64)
    image_bytes = models.BinaryField()
    mime_type = models.CharField(max_length=20)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['card', 'sha256'], name='photo_card_source_digest')]


class CardRevision(models.Model):
    card = models.ForeignKey(PhotoCard, on_delete=models.PROTECT, related_name='revisions')
    number = models.PositiveIntegerField()
    source = models.ForeignKey(CardSource, on_delete=models.PROTECT)
    fields = models.JSONField()
    content_hash = models.CharField(max_length=64)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-number']
        constraints = [models.UniqueConstraint(fields=['card', 'number'], name='photo_card_revision_number')]
