"""Local inquiry ledger. Mutations go through inquiries.services, never a CRM API."""
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class ImmutableRecord(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Запись неизменяема; создайте новую версию.')
        return super().save(*args, **kwargs)


class Inquiry(ImmutableRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    traveller_label = models.CharField(max_length=160)
    currency = models.CharField(max_length=3, default='RUB')
    crm_system = models.CharField(max_length=10, blank=True)
    external_id = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def sync_status(self):
        return 'not_synchronized'

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(currency='RUB'), name='inquiry_rub_only'),
            models.CheckConstraint(condition=(models.Q(crm_system='', external_id='') |
                (models.Q(crm_system__in=['uon', 'amocrm']) & ~models.Q(external_id=''))),
                name='inquiry_external_pair'),
            models.UniqueConstraint(fields=['owner', 'crm_system', 'external_id'],
                condition=~models.Q(external_id=''), name='inquiry_external_owner_unique'),
        ]


class WorkLink(ImmutableRecord):
    # Stable IDs survive deletion of the source; no copy of its private file is made.
    work_id = models.UUIDField(unique=True)
    inquiry = models.ForeignKey(Inquiry, on_delete=models.PROTECT, related_name='works')


class SelectionLink(ImmutableRecord):
    selection = models.OneToOneField('presentations.TravelRequest', on_delete=models.PROTECT)
    inquiry = models.ForeignKey(Inquiry, on_delete=models.PROTECT, related_name='selections')


class Version(ImmutableRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inquiry = models.ForeignKey(Inquiry, on_delete=models.PROTECT, related_name='versions')
    number = models.PositiveIntegerField()
    snapshot = models.JSONField()
    digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['inquiry', 'number'], name='inquiry_version_number'),
            models.UniqueConstraint(fields=['inquiry', 'digest'], name='inquiry_version_digest'),
        ]


class Confirmation(ImmutableRecord):
    version = models.OneToOneField(Version, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


class Operation(ImmutableRecord):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    key = models.CharField(max_length=120)
    fingerprint = models.CharField(max_length=64)
    version = models.ForeignKey(Version, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'key'], name='inquiry_operation_key')]


class WorksheetSelection(ImmutableRecord):
    """Exact confirmed worksheet revision that produced a manager selection."""
    selection = models.OneToOneField('presentations.TravelRequest', on_delete=models.PROTECT)
    work = models.ForeignKey('worksheet_reviews.WorksheetReview', on_delete=models.PROTECT)
    ocr_revision = models.ForeignKey('worksheet_reviews.WorksheetOCRRevision', on_delete=models.PROTECT)
    signature = models.CharField(max_length=64, unique=True)


class VoiceSourceLink(ImmutableRecord):
    submission = models.OneToOneField('speech_workflow.VoiceSubmission', on_delete=models.PROTECT)
    inquiry = models.ForeignKey(Inquiry, on_delete=models.PROTECT, related_name='voice_sources')


class VoiceSelection(ImmutableRecord):
    selection = models.OneToOneField('presentations.TravelRequest', on_delete=models.PROTECT)
    draft = models.ForeignKey('speech_workflow.VoiceInquiryDraft', on_delete=models.PROTECT)
    signature = models.CharField(max_length=64, unique=True)


class Office(models.Model):
    """Explicit local scope; no real offices are seeded by migration."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)


class ManagerOffice(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    is_active = models.BooleanField(default=True)


class DirectorOfficeGrant(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'office'], name='director_office_unique')]


class InquiryOffice(ImmutableRecord):
    inquiry = models.OneToOneField(Inquiry, on_delete=models.PROTECT, related_name='office_binding')
    office = models.ForeignKey(Office, on_delete=models.PROTECT)
    owner_at_binding = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)


class SelectedOffer(ImmutableRecord):
    """Exact private input for one selection; adjustments create another selection."""
    selection = models.OneToOneField('presentations.TravelRequest', on_delete=models.PROTECT)
    data = models.JSONField()
    digest = models.CharField(max_length=64)


class OrganizationIdentity(ImmutableRecord):
    """V1: exactly one organization per isolated database/files/key directory, never shared tenancy."""
    slot = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    organization_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    label = models.CharField(max_length=100, default='Синтетическая организация')

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(slot=1), name='one_organization_per_database')]
