import re

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import TravelRequestForm
from .models import Release, TravelRequest
from .services import approve_request, build_snapshot, publish_request, snapshot_hash, token_digest


def _owned(request, pk):
    return get_object_or_404(TravelRequest, pk=pk, owner=request.user, owner__is_active=True)


def _private(response):
    response["Cache-Control"] = "no-store, private"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response["Referrer-Policy"] = "same-origin"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Frame-Options"] = "DENY"
    response["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
        "font-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


@login_required
@require_GET
def dashboard(request):
    from worksheet_reviews.models import WorksheetReview
    from worksheet_reviews.ui import work_state
    reviews = list(WorksheetReview.objects.filter(owner=request.user, owner__is_active=True))
    for review in reviews:
        review.manager_state = work_state(review)
    requests = TravelRequest.objects.filter(owner=request.user).select_related('selectionlink__inquiry', 'worksheetselection__work')
    releases = Release.objects.filter(request__owner=request.user).select_related("request")
    return _private(render(request, "presentations/dashboard.html", {"requests": requests, "releases": releases, "reviews": reviews, "now": timezone.now()}))


@login_required
@require_http_methods(["GET", "POST"])
def request_new(request):
    return redirect('worksheet_reviews:upload')


@login_required
@require_http_methods(["GET", "POST"])
def request_edit(request, pk):
    travel_request = _owned(request, pk)
    from inquiries.models import WorksheetSelection, VoiceSelection
    voice = VoiceSelection.objects.filter(selection=travel_request, selection__owner=request.user).first()
    if voice:
        return redirect('inquiries:voice_selection', draft_id=voice.draft_id)
    source = WorksheetSelection.objects.filter(selection=travel_request, work__owner=request.user).first()
    if source:
        return redirect('inquiries:selection', work_id=source.work_id)
    form = TravelRequestForm(request.POST or None, instance=travel_request)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            updated = form.save(commit=False)
            updated.revision += 1
            updated.save()
            form.save_m2m()
        return redirect("presentations:preview", pk=travel_request.pk)
    return _private(render(request, "presentations/request_form.html", {"form": form, "travel_request": travel_request}))


def _preview_context(travel_request, **extra):
    snapshot = build_snapshot(travel_request)
    from .themes import choices,resolve
    from .rendering import render_release_content
    extra['template_choices']=choices()
    extra['template_label']=resolve(travel_request.presentation_template).label
    extra['client_preview']=render_release_content(snapshot)
    from inquiries.models import SelectionLink, WorksheetSelection
    from inquiries.manager import ensure_ready
    source = WorksheetSelection.objects.filter(selection=travel_request, work__owner=travel_request.owner).select_related("work").first()
    extra["source_work"] = source.work if source else None
    try:
        ensure_ready(travel_request, travel_request.owner)
    except ValidationError as error:
        extra["workflow_error"] = " ".join(error.messages)
    from inquiries.offers import read
    extra['private_offer'] = read(travel_request)
    extra['inquiry_link'] = SelectionLink.objects.filter(selection=travel_request).select_related('inquiry').first()
    return {"travel_request": travel_request, "snapshot": snapshot, "content_hash": snapshot_hash(snapshot), **extra}


@login_required
@require_GET
def preview(request, pk):
    travel_request = _owned(request, pk)
    try:
        context = _preview_context(travel_request)
    except ValidationError:
        return redirect("presentations:request_edit", pk=pk)
    return _private(render(request, "presentations/preview.html", context))


@login_required
@require_POST
def approve(request, pk):
    travel_request = _owned(request, pk)
    try:
        approval = approve_request(travel_request, request.user, request.POST.get("content_hash", ""))
        context = _preview_context(travel_request, approval=approval)
        return _private(render(request, "presentations/approved.html", context))
    except ValidationError as error:
        try:
            context = _preview_context(travel_request, error=" ".join(error.messages))
        except ValidationError:
            return redirect("presentations:request_edit", pk=pk)
        return _private(render(request, "presentations/preview.html", context, status=409))


def _publication_error(request, travel_request, message, status):
    try:
        context = _preview_context(travel_request, error=message)
    except ValidationError:
        return _private(redirect("presentations:request_edit", pk=travel_request.pk))
    return _private(render(request, "presentations/preview.html", context, status=status))


@login_required
@require_POST
def publish(request, pk):
    travel_request = _owned(request, pk)
    try:
        release, token = publish_request(travel_request, request.user, request.POST.get("approval_id", ""))
    except ValidationError as error:
        return _publication_error(request, travel_request, " ".join(error.messages), 409)
    except Exception:
        # Do not log raw content or exception strings from the renderer.
        return _publication_error(
            request, travel_request,
            "Не удалось сформировать PDF. Выпуск не создан; повторите после проверки.", 503,
        )
    share_url = request.build_absolute_uri(reverse("presentations:share", args=[token])) if token else None
    from inquiries.models import SelectionLink
    inquiry_link = SelectionLink.objects.filter(selection=travel_request).select_related('inquiry').first()
    return _private(render(request, "presentations/published.html", {"release": release, "share_url": share_url, 'inquiry_link': inquiry_link}))


@login_required
@require_POST
def revoke(request, pk):
    release = get_object_or_404(Release, pk=pk, request__owner=request.user)
    if release.revoked_at is None:
        release.revoked_at = timezone.now()
        release.save(update_fields=["revoked_at"])
    return redirect("presentations:dashboard")


def _live_release(token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise Http404
    release = get_object_or_404(
        Release.objects.select_related("request__owner"), token_digest=token_digest(token),
        revoked_at__isnull=True, expires_at__gt=timezone.now(), request__owner__is_active=True,
    )
    return release


def _granted(request, release):
    return release.token_digest in request.session.get("release_grants", [])


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def share(request, token):
    release = _live_release(token)
    if request.method == "POST":
        grants = request.session.get("release_grants", [])
        request.session["release_grants"] = list(dict.fromkeys([*grants[-19:], release.token_digest]))
        return _private(redirect("presentations:share", token=token))
    if not _granted(request, release):
        # Never pass release, snapshot, or request metadata to the neutral gate.
        return _private(render(request, "presentations/share_gate.html"))
    return _private(HttpResponse(release.html_content, content_type="text/html; charset=utf-8"))


@require_GET
def share_pdf(request, token):
    release = _live_release(token)
    if not _granted(request, release):
        raise Http404
    response = HttpResponse(bytes(release.pdf_bytes), content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="travelhub-demo.pdf"'
    return _private(response)


@login_required
@require_GET
def archive(request, pk):
    release = get_object_or_404(Release, pk=pk, request__owner=request.user, request__owner__is_active=True)
    return _private(HttpResponse(release.html_content, content_type='text/html; charset=utf-8'))


@login_required
@require_GET
def archive_pdf(request, pk):
    release = get_object_or_404(Release, pk=pk, request__owner=request.user, request__owner__is_active=True)
    response = HttpResponse(bytes(release.pdf_bytes), content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="izzi-selection.pdf"'
    return _private(response)


@login_required
@require_POST
def reprice(request, pk):
    travel_request = _owned(request, pk)
    from inquiries.offers import reprice as prepare_price
    try:
        result = prepare_price(actor=request.user, selection_id=travel_request.pk,
                               expected_hash=request.POST.get('content_hash'),
                               client_price=request.POST.get('client_price'))
    except ValidationError as error:
        return _private(render(request, 'presentations/preview.html',
            _preview_context(travel_request, error=' '.join(error.messages)), status=422))
    return redirect('presentations:preview', pk=result.pk)


@login_required
@require_POST
def change_template(request,pk):
    travel_request=_owned(request,pk)
    from .services import select_template
    try:
        select_template(selection_id=pk,actor=request.user,key=request.POST.get('template'),expected_hash=request.POST.get('content_hash'))
    except ValidationError as error:
        return _publication_error(request,travel_request,' '.join(error.messages),422)
    return redirect('presentations:preview',pk=pk)
