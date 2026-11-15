import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Hotel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("Название", max_length=160)
    summary = models.TextField("Описание", max_length=3000)
    pros = models.TextField("Плюсы — по одному на строку", max_length=3000)
    limitations = models.TextField("Ограничения — по одному на строку", max_length=3000)
    source_reference = models.CharField("Источник", max_length=300)
    internal_notes = models.TextField("Служебные заметки", blank=True, max_length=3000)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class TravelRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    title = models.CharField("Название подборки", max_length=160)
    traveller_summary = models.TextField("Состав и пожелания путешественников", max_length=3000)
    requirements = models.TextField("Что важно в поездке", max_length=3000)
    internal_notes = models.TextField("Служебные заметки — клиенту не показываются", blank=True, max_length=3000)
    selected_hotels = models.ManyToManyField(Hotel, blank=True)
    presentation_template = models.CharField("Оформление", max_length=80, default="cards:1")
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title


class Approval(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(TravelRequest, related_name="approvals", on_delete=models.PROTECT)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    content_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


class Release(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(TravelRequest, related_name="releases", on_delete=models.PROTECT)
    approval = models.OneToOneField(Approval, on_delete=models.PROTECT)
    snapshot = models.JSONField()
    content_hash = models.CharField(max_length=64)
    html_content = models.TextField()
    pdf_bytes = models.BinaryField()
    pdf_hash = models.CharField(max_length=64)
    token_digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def available(self):
        return self.revoked_at is None and self.expires_at > timezone.now()

    def save(self, *args, **kwargs):
        # Application invariant, not a claim of protection against direct SQL/admin access.
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            for name in ("request_id", "approval_id", "snapshot", "content_hash", "html_content",
                         "pdf_bytes", "pdf_hash", "token_digest", "created_at"):
                if getattr(original, name) != getattr(self, name):
                    raise ValidationError("Выпущенная версия неизменяема. Создайте новый выпуск.")
        super().save(*args, **kwargs)
