"""Owner-local exact-match correction retrieval from common persisted review history.
No provider weights, automatic edits, channel state or separate database.
"""
from travelcards.forms import FIELD_LABELS, normalize_fields
from .ocr_services import current_revision
KEYS=list(FIELD_LABELS)


def suggestions(review, actor):
    """Exact-match retrieval of confirmed corrections; never change OCR/confirm automatically."""
    from worksheet_reviews.models import WorksheetOCRRevision
    if not actor.is_active or review.owner_id != actor.pk:return {}
    from django.contrib.auth import get_user_model
    if not get_user_model().objects.filter(pk=actor.pk,is_active=True).exists():return {}
    current=current_revision(review)
    if not current:return {}
    now=normalize_fields(current.fields);matches={};seen=set()
    history=WorksheetOCRRevision.objects.filter(review__owner=actor,confirmed_at__isnull=False).exclude(review=review).select_related('review').order_by('-created_at','-pk')[:200]
    for confirmed in history:
        identity=(confirmed.review_id,confirmed.source_sha256)
        if identity in seen:continue
        seen.add(identity)
        original=WorksheetOCRRevision.objects.filter(review_id=confirmed.review_id,source_sha256=confirmed.source_sha256).order_by('number').first()
        if not original or original.metadata.get('status')!='recognized':continue
        initial=normalize_fields(original.fields);final=normalize_fields(confirmed.fields)
        for key in KEYS:
            a,b,c=initial[key],final[key],now[key]
            if (c['state']=='ocr_candidate' and c['value'] and a['state']=='ocr_candidate' and
                a['value']==c['value'] and b['state']=='known' and b['value'] and b['value']!=a['value'] and
                b['source'].get('kind')=='manual_review'):
                # Match the rest of the recognized context, not just a common number/name.
                if all((initial[k]['value'],initial[k]['state'])==(now[k]['value'],now[k]['state']) for k in KEYS if k!=key):
                    matches.setdefault(key,set()).add(b['value'])
    return {key:next(iter(values)) for key,values in matches.items() if len(values)==1}
