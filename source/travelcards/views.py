from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from presentations.views import _private
from .forms import (CardFieldsForm, FIELD_LABELS, HotelSelectionForm,
                    PhotoReplaceForm, UploadForm, VersionForm)
from .models import CardSource, PhotoCard
from .services import (confirm_card, create_card, current_revision, prepare_request,
                       replace_photo, save_fields, StalePhotoRevision)


def _owned(request, pk):
    return get_object_or_404(PhotoCard, pk=pk, owner=request.user, owner__is_active=True)


def _detail(request, card, *, field_form=None, photo_form=None, hotel_form=None, error=None, status=200):
    card.refresh_from_db()
    revision = current_revision(card)
    recognition = revision.fields.get('_recognition', {})
    field_form = field_form if field_form is not None else CardFieldsForm(revision=revision)
    ctx = {'card': card, 'revision': revision, 'field_form': field_form,
           'field_groups': [{'key': key, 'label': label, 'value': field_form[key + '_value'],
                             'state': field_form[key + '_state'], 'source': field_form[key + '_source']}
                            for key, label in FIELD_LABELS.items()],
           'photo_form': photo_form if photo_form is not None else PhotoReplaceForm(revision=revision),
           'hotel_form': hotel_form if hotel_form is not None else HotelSelectionForm(initial={
               'expected_revision': revision.number, 'expected_hash': revision.content_hash}),
           'source_url': reverse('travelcards:source', args=[card.pk, revision.source_id]),
           'is_confirmed': revision.confirmed_at is not None, 'content_hash': revision.content_hash,
           'recognition': recognition,
           'ocr_observations': [
               {'label': FIELD_LABELS[key], 'lines': lines}
               for key, lines in recognition.get('field_observations', {}).items() if len(lines) > 1
           ],
           'error': error, 'history': card.revisions.all()}
    return _private(render(request, 'travelcards/detail.html', ctx, status=status))


@login_required
@require_GET
def card_list(request):
    return _private(render(request, 'travelcards/list.html', {'cards': PhotoCard.objects.filter(owner=request.user)}))


@login_required
@require_http_methods(['GET', 'POST'])
def upload(request):
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        try:
            card = create_card(request.user, form.cleaned_data['title'], form.cleaned_data['upload_key'],
                               form.cleaned_data['photo'])
            return redirect('travelcards:detail', pk=card.pk)
        except ValidationError as error:
            form.add_error('photo', error)
    return _private(render(request, 'travelcards/upload.html', {'form': form},
                           status=422 if request.method == 'POST' else 200))


@login_required
@require_http_methods(['GET', 'POST'])
def detail(request, pk):
    card = _owned(request, pk)
    if request.method == 'GET':
        return _detail(request, card)
    form = CardFieldsForm(request.POST, revision=current_revision(card))
    if form.is_valid():
        try:
            save_fields(card, request.user, form.cleaned_data['expected_revision'],
                        form.cleaned_data['expected_hash'], form.field_snapshot())
            return redirect('travelcards:detail', pk=pk)
        except ValidationError as error:
            return _detail(request, card, field_form=form, error=' '.join(error.messages), status=409)
    return _detail(request, card, field_form=form, status=422)


@login_required
@require_POST
def photo_replace(request, pk):
    card = _owned(request, pk)
    form = PhotoReplaceForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            replace_photo(card, request.user, form.cleaned_data['expected_revision'],
                          form.cleaned_data['expected_hash'], form.cleaned_data['photo'])
            return redirect('travelcards:detail', pk=pk)
        except StalePhotoRevision as error:
            return _detail(request, card, photo_form=form, error=' '.join(error.messages), status=409)
        except ValidationError as error:
            form.add_error('photo', error)
    return _detail(request, card, photo_form=form, status=422)


@login_required
@require_GET
def source(request, pk, source_pk):
    card = _owned(request, pk)
    image = get_object_or_404(CardSource, card=card, pk=source_pk)
    response = HttpResponse(bytes(image.image_bytes), content_type=image.mime_type)
    response['Content-Disposition'] = 'inline; filename="synthetic-card.' + ('png' if image.mime_type == 'image/png' else 'jpg') + '"'
    return _private(response)


@login_required
@require_POST
def confirm(request, pk):
    card = _owned(request, pk)
    form = VersionForm(request.POST)
    if form.is_valid():
        try:
            confirm_card(card, request.user, form.cleaned_data['expected_revision'], form.cleaned_data['expected_hash'])
            return redirect('travelcards:detail', pk=pk)
        except ValidationError as error:
            return _detail(request, card, error=' '.join(error.messages), status=409)
    return _detail(request, card, error='Некорректная версия. Обновите страницу карты.', status=422)


@login_required
@require_POST
def prepare(request, pk):
    card = _owned(request, pk)
    form = HotelSelectionForm(request.POST)
    if form.is_valid():
        try:
            travel_request = prepare_request(card, request.user, form.cleaned_data['expected_revision'],
                                             form.cleaned_data['expected_hash'], form.cleaned_data['hotels'])
            return redirect('presentations:preview', pk=travel_request.pk)
        except ValidationError as error:
            return _detail(request, card, hotel_form=form, error=' '.join(error.messages), status=409)
    return _detail(request, card, hotel_form=form, status=422)
