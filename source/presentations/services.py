import hashlib
import json
import re
import secrets
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Approval, Release, TravelRequest
from .rendering import render_pdf, render_release_html


def _check_actor(travel_request, actor):
    if not actor.is_authenticated or not actor.is_active or actor.pk != travel_request.owner_id:
        raise PermissionDenied("Заявка недоступна.")


def _lines(value):
    return [line.strip() for line in value.splitlines() if line.strip()]


def build_snapshot(travel_request):
    from inquiries.manager import provenance_hash
    from inquiries.models import WorksheetSelection
    hotels = list(travel_request.selected_hotels.order_by("name", "id"))
    if not 1 <= len(hotels) <= 5:
        raise ValidationError("Для подборки нужно от одного до пяти тестовых отелей.")
    from .themes import descriptor
    design=descriptor(travel_request.presentation_template)
    result = {
        "presentation": design,
        "schema_version": 1,
        "source_context_hash": provenance_hash(travel_request),
        "template_version": travel_request.presentation_template,
        "synthetic": True,
        # Worksheet titles contain the private work code. Keep it in the ledger,
        # never in client HTML/PDF; also covers drafts prepared before this fix.
        "title": ('Подборка путешествия' if WorksheetSelection.objects.filter(
            selection_id=travel_request.pk).exists() else travel_request.title),
        "traveller_summary": travel_request.traveller_summary,
        "requirements": travel_request.requirements,
        "request_revision": travel_request.revision,
        "hotels": [{
            "id": str(hotel.pk),
            "name": hotel.name,
            "summary": hotel.summary,
            "pros": _lines(hotel.pros),
            "limitations": _lines(hotel.limitations),
            "source_reference": hotel.source_reference,
        } for hotel in hotels],
    }

    from inquiries.offers import read, public
    from inquiries.services import digest
    offer = read(travel_request)
    if offer is not None:
        result.update(schema_version=2, offer=public(offer), offer_context_hash=digest(offer))
        if offer.get('import_source'):
            # Source row identity/name cannot drift with unrelated mutable catalog prose.
            n = offer['import_source']['selected']
            result['hotels'] = [{'id': offer['hotel_id'], 'name': n['hotel']['name'],
                'summary': 'Синтетический выбранный вариант; свойства отеля не проверены.',
                'pros': [], 'limitations': ['Наличие и бронирование не подтверждены.'],
                'source_reference': result['offer']['imported']['source_label']}]
    return result


def snapshot_hash(snapshot):
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@transaction.atomic
def approve_request(travel_request, actor, expected_hash):
    current = TravelRequest.objects.select_for_update().get(pk=travel_request.pk)
    _check_actor(current, actor)
    from travelcards.services import ensure_card_ready
    ensure_card_ready(current)
    from inquiries.manager import ensure_ready
    ensure_ready(current, actor)
    content_hash = snapshot_hash(build_snapshot(current))
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash or "") or not secrets.compare_digest(content_hash, expected_hash):
        raise ValidationError("Подборка изменилась. Проверьте актуальный предпросмотр ещё раз.")
    return Approval.objects.create(request=current, approved_by=actor, content_hash=content_hash)


@transaction.atomic
def publish_request(travel_request, actor, approval_id):
    current = TravelRequest.objects.select_for_update().get(pk=travel_request.pk)
    _check_actor(current, actor)
    from travelcards.services import ensure_card_ready
    ensure_card_ready(current)
    from inquiries.manager import ensure_ready
    ensure_ready(current, actor)
    try:
        approval = Approval.objects.select_for_update().get(
            pk=approval_id, request=current, approved_by=actor
        )
    except (Approval.DoesNotExist, ValueError, ValidationError):
        raise ValidationError("Одобрение не найдено. Вернитесь к предпросмотру.")
    existing = Release.objects.filter(approval=approval).first()
    if existing:
        return existing, None
    snapshot = build_snapshot(current)
    digest = snapshot_hash(snapshot)
    if not secrets.compare_digest(digest, approval.content_hash):
        raise ValidationError("После одобрения данные изменились. Нужно новое подтверждение.")
    html = render_release_html(snapshot)
    pdf = render_pdf(snapshot, html_content=html)
    if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF-"):
        raise ValidationError("PDF не сформирован. Выпуск не создан; повторите позже.")
    # Compare again after rendering to reject changes seen during a slow conversion.
    current.refresh_from_db()
    if snapshot_hash(build_snapshot(current)) != digest:
        raise ValidationError("Данные изменились во время формирования PDF. Подтвердите новую версию.")
    token = secrets.token_urlsafe(32)
    release = Release.objects.create(
        request=current, approval=approval, snapshot=snapshot, content_hash=digest,
        html_content=html, pdf_bytes=pdf, pdf_hash=hashlib.sha256(pdf).hexdigest(),
        token_digest=token_digest(token), expires_at=timezone.now() + timedelta(days=7),
    )
    from inquiries.manager import record_release
    record_release(current, actor, release)
    return release, token


@transaction.atomic
def select_template(*,selection_id,actor,key,expected_hash):
    """Change only the next release's design. Existing release bytes are never touched."""
    from inquiries.services import _actor,_owned
    from inquiries.manager import ensure_ready
    from .themes import resolve
    _actor(actor)
    current=_owned(TravelRequest,selection_id,actor)
    ensure_ready(current,actor)
    resolve(key)
    if not isinstance(expected_hash,str) or snapshot_hash(build_snapshot(current))!=expected_hash:
        raise ValidationError('Подборка изменилась. Откройте актуальный предпросмотр.')
    if current.presentation_template!=key:
        current.presentation_template=key
        current.save(update_fields=['presentation_template','updated_at'])
    return current
