import hashlib

from django.core.exceptions import PermissionDenied
from django.db import transaction

from .models import HotelChoice, WorksheetReview


@transaction.atomic
def create_review(*, owner, work_code, worksheet_bytes, worksheet_type, hotel_choices, expert_overview=''):
    if not owner.is_authenticated or not owner.is_active:
        raise PermissionDenied
    review = WorksheetReview.objects.create(
        owner=owner, work_code=work_code, worksheet_bytes=worksheet_bytes,
        worksheet_type=worksheet_type,
        worksheet_sha256=hashlib.sha256(worksheet_bytes).hexdigest(),
        expert_overview=expert_overview,
    )
    HotelChoice.objects.bulk_create([
        HotelChoice(review=review, **choice) for choice in hotel_choices
    ])
    return review


@transaction.atomic
def update_review(review, owner, *, hotel_choices, expert_overview):
    review = WorksheetReview.objects.select_for_update().get(pk=review.pk, owner=owner, owner__is_active=True)
    review.hotel_choices.all().delete()
    HotelChoice.objects.bulk_create([
        HotelChoice(review=review, **choice) for choice in hotel_choices
    ])
    review.expert_overview = expert_overview
    review.save(update_fields=['expert_overview', 'updated_at'])
    return review
