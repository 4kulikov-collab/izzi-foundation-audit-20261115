from django.conf import settings
from django.db import models


class UpdateReceipt(models.Model):
    """No raw update, names, chat IDs, file IDs, token or message body is retained."""
    gateway = models.CharField(max_length=64)
    update_id = models.PositiveBigIntegerField()
    message_key = models.CharField(max_length=64)
    payload_hash = models.CharField(max_length=64)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    review = models.ForeignKey('worksheet_reviews.WorksheetReview', null=True, on_delete=models.SET_NULL)
    result = models.CharField(max_length=20)
    reply_state = models.CharField(max_length=16, default='pending')
    callback_ack_state = models.CharField(max_length=16, default='not_required')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['gateway', 'update_id'], name='telegram_unique_update'),
            models.UniqueConstraint(fields=['gateway', 'message_key'], name='telegram_unique_message'),
        ]


class PollingState(models.Model):
    gateway = models.CharField(max_length=64, primary_key=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    binding_hash = models.CharField(max_length=64)
    next_offset = models.PositiveBigIntegerField(default=0)


class ReviewSession(models.Model):
    gateway = models.CharField(max_length=64)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    review = models.ForeignKey('worksheet_reviews.WorksheetReview',null=True,on_delete=models.SET_NULL)
    context_hash = models.CharField(max_length=64)
    revision_hash = models.CharField(max_length=64)
    nonce = models.UUIDField()
    panel = models.CharField(max_length=16,default='summary')
    field_key = models.CharField(max_length=32,default='')

    class Meta:
        constraints=[models.UniqueConstraint(fields=['gateway','owner'],name='telegram_unique_review_session')]
