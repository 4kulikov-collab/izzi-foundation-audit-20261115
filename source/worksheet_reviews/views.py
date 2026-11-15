from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from presentations.views import _private

from travelcards.forms import CardFieldsForm, FIELD_LABELS, VersionForm

from .forms import DeleteWorkConfirmationForm, WorksheetReviewForm, WorksheetSourceReplaceForm, WorksheetUploadForm
from .models import WorksheetReview
from .ocr_services import (current_revision as current_ocr_revision, current_source, initialize_ocr,
                           replace_source as replace_worksheet_source, rerun_ocr,
                           save_fields as save_ocr_fields)
from .services import create_review, update_review
from .ui import work_state, recognition_label, history_rows


@login_required
@require_GET
def review_list(request):
    reviews = list(WorksheetReview.objects.filter(owner=request.user, owner__is_active=True))
    for review in reviews:
        review.manager_state = work_state(review)
    return _private(render(request, 'worksheet_reviews/list.html', {'reviews': reviews}))


@login_required
@require_http_methods(['GET', 'POST'])
def upload(request):
    form = WorksheetUploadForm(request.POST or None, request.FILES or None, owner=request.user)
    if request.method == 'POST' and form.is_valid():
        review = create_review(
            owner=request.user, work_code=form.cleaned_data['work_code'],
            worksheet_bytes=form.cleaned_data['_worksheet_bytes'],
            worksheet_type=form.cleaned_data['_worksheet_type'],
            hotel_choices=form.hotel_choice_values(),
            expert_overview=form.cleaned_data['expert_overview'],
        )
        initialize_ocr(review, request.user)
        return redirect('worksheet_reviews:detail', pk=review.pk)
    return _private(render(request, 'worksheet_reviews/upload.html', {'form': form},
                           status=422 if request.method == 'POST' else 200))


@login_required
@require_http_methods(['GET', 'POST'])
def detail(request, pk):
    review = get_object_or_404(WorksheetReview, pk=pk, owner=request.user, owner__is_active=True)
    ocr_revision = current_ocr_revision(review) or initialize_ocr(review, request.user)
    if request.method == 'POST' and request.POST.get('action') == 'replace_source':
        source = current_source(review)
        source_form = WorksheetSourceReplaceForm(request.POST, request.FILES, source=source)
        if source_form.is_valid():
            try:
                replace_worksheet_source(
                    review, request.user, source_form.cleaned_data['expected_source_version'],
                    source_form.cleaned_data['expected_source_hash'],
                    worksheet_bytes=source_form.cleaned_data['_worksheet_bytes'],
                    worksheet_type=source_form.cleaned_data['_worksheet_type'],
                )
                review.refresh_from_db()
                initialize_ocr(review, request.user)
                return redirect('worksheet_reviews:detail', pk=review.pk)
            except ValidationError as error:
                return _review_detail(request, review, current_ocr_revision(review), source_form=source_form,
                                      error=' '.join(error.messages), status=409)
        return _review_detail(request, review, ocr_revision, source_form=source_form, status=422)
    if request.method == 'POST' and request.POST.get('action') in {'save_ocr', 'confirm_ocr'}:
        field_form = CardFieldsForm(request.POST, revision=ocr_revision)
        if field_form.is_valid():
            try:
                save_ocr_fields(
                    review, request.user, field_form.cleaned_data['expected_revision'],
                    field_form.cleaned_data['expected_hash'], field_form.field_snapshot(),
                    confirm=request.POST.get('action') == 'confirm_ocr',
                )
                return redirect('worksheet_reviews:detail', pk=review.pk)
            except ValidationError as error:
                ocr_revision = current_ocr_revision(review)
                return _review_detail(request, review, ocr_revision, field_form=field_form,
                                      error=' '.join(error.messages), status=409)
        return _review_detail(request, review, ocr_revision, field_form=field_form, status=422)
    if request.method == 'POST' and request.POST.get('action') == 'rerun_ocr':
        version_form = VersionForm(request.POST)
        if version_form.is_valid():
            try:
                rerun_ocr(review, request.user, version_form.cleaned_data['expected_revision'],
                          version_form.cleaned_data['expected_hash'])
                return redirect('worksheet_reviews:detail', pk=review.pk)
            except ValidationError as error:
                return _review_detail(request, review, current_ocr_revision(review),
                                      error=' '.join(error.messages), status=409)
        return _review_detail(request, review, ocr_revision, error='Некорректная версия OCR.', status=422)
    form = WorksheetReviewForm(request.POST or None, initial={
        'expert_overview': review.expert_overview,
    }, initial_hotels=review.hotel_choices.values('name', 'reference_text'))
    if request.method == 'POST' and form.is_valid():
        update_review(review, request.user, hotel_choices=form.hotel_choice_values(),
                      expert_overview=form.cleaned_data['expert_overview'])
        return redirect('worksheet_reviews:detail', pk=review.pk)
    return _review_detail(request, review, ocr_revision, form=form,
                          status=422 if request.method == 'POST' else 200)


def _review_detail(request, review, ocr_revision, *, form=None, field_form=None, source_form=None, error=None, status=200):
    form = form or WorksheetReviewForm(initial={
        'expert_overview': review.expert_overview,
    }, initial_hotels=review.hotel_choices.values('name', 'reference_text'))
    field_form = field_form or CardFieldsForm(revision=ocr_revision)
    from django.forms import HiddenInput
    field_form.fields['budget_currency'].widget = HiddenInput()
    if not field_form.is_bound:
        field_form.initial['budget_currency'] = 'RUB'
    source = current_source(review)
    source_form = source_form or WorksheetSourceReplaceForm(source=source)
    from inquiries.models import WorkLink
    inquiry_link = WorkLink.objects.filter(work_id=review.pk, inquiry__owner=request.user).select_related('inquiry').first()
    context = {
        'inquiry_link': inquiry_link, 'manager_state': work_state(review),
        'recognition_label': recognition_label(ocr_revision.metadata),
        'budget_requires_rubles': field_form.revision_fields['budget'].get('currency') not in ('', 'RUB'),
        'history_rows': history_rows(review.ocr_revisions.exclude(pk=ocr_revision.pk)),
        'review': review, 'form': form, 'field_form': field_form,
        'source': source, 'source_form': source_form,
        'source_history': ([{'number': 1, 'worksheet_type': review.worksheet_type,
                             'worksheet_sha256': review.worksheet_sha256}]
                           + list(review.file_revisions.filter(number__lt=review.current_source_version)))
                           if review.current_source_version > 1 else [],
        'field_groups': [{'key': key, 'label': label, 'value': field_form[key + '_value'],
                          'state': field_form[key + '_state'], 'source': field_form[key + '_source']}
                         for key, label in FIELD_LABELS.items()],
        'ocr_revision': ocr_revision,
        'ocr_history': review.ocr_revisions.exclude(pk=ocr_revision.pk),
        'recognition': ocr_revision.metadata,
        'is_ocr_confirmed': ocr_revision.confirmed_at is not None,
        'sheet_url': reverse('worksheet_reviews:sheet', args=[review.pk]),
        'error': error,
    }
    return _private(render(request, 'worksheet_reviews/detail.html', context, status=status))


@login_required
@require_GET
def sheet(request, pk):
    review = get_object_or_404(WorksheetReview, pk=pk, owner=request.user, owner__is_active=True)
    source = current_source(review)
    response = HttpResponse(bytes(source.worksheet_bytes), content_type=source.worksheet_type)
    ext = 'pdf' if source.worksheet_type == 'application/pdf' else ('png' if source.worksheet_type == 'image/png' else 'jpg')
    response['Content-Disposition'] = f'inline; filename="worksheet-{review.work_code}.{ext}"'
    return _private(response)


@login_required
@require_GET
def sheet_version(request, pk, number):
    review = get_object_or_404(WorksheetReview, pk=pk, owner=request.user, owner__is_active=True)
    source = review if number == 1 else get_object_or_404(review.file_revisions, number=number)
    response = HttpResponse(bytes(source.worksheet_bytes), content_type=source.worksheet_type)
    ext = 'pdf' if source.worksheet_type == 'application/pdf' else ('png' if source.worksheet_type == 'image/png' else 'jpg')
    response['Content-Disposition'] = f'inline; filename="worksheet-{review.work_code}-v{number}.{ext}"'
    return _private(response)


@login_required
@require_http_methods(['GET', 'POST'])
def delete_confirm(request, pk):
    review = get_object_or_404(WorksheetReview, pk=pk, owner=request.user, owner__is_active=True)
    form = DeleteWorkConfirmationForm(request.POST or None, expected_id=str(review.pk))
    if request.method == 'POST' and form.is_valid():
        try:
            review.delete()
        except ProtectedError:
            form.add_error(None, 'Лист связан с обращением и историей выпусков. Удаление заблокировано.')
        else:
            return redirect('worksheet_reviews:list')
    return _private(render(request, 'worksheet_reviews/delete_confirm.html', {
        'review': review, 'form': form,
    }, status=422 if request.method == 'POST' else 200))
