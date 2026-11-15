import uuid

from django.conf import settings
from django.db import models


class WorksheetReview(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='worksheet_reviews')
    work_code = models.CharField('Учебный код работы', max_length=32)
    worksheet_bytes = models.BinaryField()
    worksheet_type = models.CharField(max_length=20)
    worksheet_sha256 = models.CharField(max_length=64)
    current_source_version = models.PositiveIntegerField(default=1)
    expert_overview = models.TextField('Экспертный обзор', blank=True, max_length=5000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        constraints = [models.UniqueConstraint(fields=['owner', 'work_code'], name='unique_worksheet_owner_code')]

    def __str__(self):
        return self.work_code


class HotelChoice(models.Model):
    review = models.ForeignKey(WorksheetReview, on_delete=models.CASCADE, related_name='hotel_choices')
    position = models.PositiveSmallIntegerField()
    name = models.CharField('Название отеля', max_length=160)
    reference_text = models.TextField('Ссылка или текст из подбора', blank=True, max_length=1600)

    class Meta:
        ordering = ['position']
        constraints = [models.UniqueConstraint(fields=['review', 'position'], name='unique_worksheet_hotel_position')]


class WorksheetOCRRevision(models.Model):
    """Immutable OCR/manual-review snapshots; the worksheet bytes stay on WorksheetReview."""
    review = models.ForeignKey(WorksheetReview, on_delete=models.CASCADE, related_name='ocr_revisions')
    number = models.PositiveIntegerField()
    source_sha256 = models.CharField(max_length=64)
    fields = models.JSONField()
    original_text = models.TextField(blank=True)
    metadata = models.JSONField(default=dict)
    content_hash = models.CharField(max_length=64)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.PROTECT, related_name='confirmed_worksheet_ocr')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-number']
        constraints = [models.UniqueConstraint(fields=['review', 'number'], name='unique_worksheet_ocr_revision')]


class WorksheetFileRevision(models.Model):
    """Replacement sources are retained as history; version 1 stays on WorksheetReview."""
    review = models.ForeignKey(WorksheetReview, on_delete=models.CASCADE, related_name='file_revisions')
    number = models.PositiveIntegerField()
    worksheet_bytes = models.BinaryField()
    worksheet_type = models.CharField(max_length=20)
    worksheet_sha256 = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name='replaced_worksheet_sources')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-number']
        constraints = [models.UniqueConstraint(fields=['review', 'number'], name='unique_worksheet_file_revision')]
