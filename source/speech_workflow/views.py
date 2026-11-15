from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from presentations.views import _private
from .forms import AudioUploadForm, ObservationDraftForm, TranscriptEditForm
from .models import HotelObservationDraft, VoiceSubmission, VoiceInquiryDraft
from .services import (confirm_transcript, create_observation_draft, create_submission,
                       cancel_submission, current_revision, edit_transcript,
                       revoke_transcript_confirmation)


def _owned(request, pk):
    return get_object_or_404(VoiceSubmission, pk=pk, owner=request.user, owner__is_active=True)


@login_required
@require_GET
def submission_list(request):
    items = VoiceSubmission.objects.filter(owner=request.user, owner__is_active=True)
    return _private(render(request, 'speech_workflow/list.html', {'items': items}))


@login_required
@require_http_methods(['GET', 'POST'])
def upload(request):
    form = AudioUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        try:
            item = create_submission(request.user, form.cleaned_data['idempotency_key'], form.cleaned_data['audio'])
        except PermissionDenied:
            raise Http404
        except ValidationError as error:
            form.add_error('audio', error)
            return _private(render(request, 'speech_workflow/upload.html', {'form': form}, status=422))
        return redirect('speech_workflow:detail', pk=item.pk)
    status = 422 if request.method == 'POST' else 200
    return _private(render(request, 'speech_workflow/upload.html', {'form': form}, status=status))


@login_required
@require_GET
def detail(request, pk):
    item = _owned(request, pk)
    revision = current_revision(item)
    edit_form = TranscriptEditForm(initial={
        'expected_revision': revision.number,
        'expected_hash': revision.content_hash,
        'text': revision.text,
    })
    observation_form = ObservationDraftForm()
    observation = HotelObservationDraft.objects.filter(transcript_revision=revision).first()
    draft = VoiceInquiryDraft.objects.filter(transcript_revision=revision).first()
    from inquiries.models import VoiceSourceLink
    voice_link = VoiceSourceLink.objects.filter(submission=item, inquiry__owner=request.user).select_related('inquiry').first()
    from travelcards.forms import FIELD_LABELS
    draft_fields = [(key, FIELD_LABELS[key], value) for key, value in draft.fields.items()] if draft else []
    return _private(render(request, 'speech_workflow/detail.html', {
        'item': item, 'revision': revision, 'edit_form': edit_form,
        'observation_form': observation_form, 'observation': observation,
        'inquiry_draft': draft, 'draft_fields': draft_fields, 'voice_inquiry_link': voice_link,
        'voice_boundary_enabled': settings.VOICE_INQUIRY_BOUNDARY_ENABLED,
    }))


@login_required
@require_GET
def audio_source(request, pk):
    item = _owned(request, pk)
    try:
        source = item.audio.open('rb')
    except (OSError, ValueError):
        raise Http404
    response = FileResponse(source, content_type='audio/wav', as_attachment=True, filename='voice-note.wav')
    return _private(response)


@login_required
@require_POST
def transcript_edit(request, pk):
    item = _owned(request, pk)
    form = TranscriptEditForm(request.POST)
    if not form.is_valid():
        return _private(render(request, 'speech_workflow/detail.html', {
            'item': item, 'revision': current_revision(item), 'edit_form': form,
            'observation_form': ObservationDraftForm(),
        }, status=422))
    try:
        edit_transcript(pk, request.user, form.cleaned_data['expected_revision'],
                        form.cleaned_data['expected_hash'], form.cleaned_data['text'])
    except PermissionDenied:
        raise Http404
    except ValidationError as error:
        form.add_error(None, error)
        return _private(render(request, 'speech_workflow/detail.html', {
            'item': item, 'revision': current_revision(item), 'edit_form': form,
            'observation_form': ObservationDraftForm(),
        }, status=409))
    return redirect('speech_workflow:detail', pk=pk)


@login_required
@require_POST
def transcript_confirm(request, pk):
    item = _owned(request, pk)
    form = TranscriptEditForm(request.POST)
    if not form.is_valid():
        return _private(render(request, 'speech_workflow/detail.html', {
            'item': item, 'revision': current_revision(item), 'edit_form': form,
            'observation_form': ObservationDraftForm(),
        }, status=422))
    try:
        confirm_transcript(pk, request.user, form.cleaned_data['expected_revision'], form.cleaned_data['expected_hash'])
    except PermissionDenied:
        raise Http404
    except ValidationError as error:
        form.add_error(None, error)
        return _private(render(request, 'speech_workflow/detail.html', {
            'item': item, 'revision': current_revision(item), 'edit_form': form,
            'observation_form': ObservationDraftForm(),
        }, status=409))
    return redirect('speech_workflow:detail', pk=pk)


@login_required
@require_POST
def transcript_revoke(request, pk):
    item = _owned(request, pk)
    form = TranscriptEditForm(request.POST)
    if not form.is_valid():
        raise Http404
    try:
        revoke_transcript_confirmation(pk, request.user, form.cleaned_data['expected_revision'],
                                       form.cleaned_data['expected_hash'])
    except PermissionDenied:
        raise Http404
    except ValidationError as error:
        form.add_error(None, error)
        return _private(render(request, 'speech_workflow/detail.html', {
            'item': item, 'revision': current_revision(item), 'edit_form': form,
            'observation_form': ObservationDraftForm(),
        }, status=409))
    return redirect('speech_workflow:detail', pk=pk)


@login_required
@require_POST
def cancel(request, pk):
    _owned(request, pk)
    try:
        cancel_submission(pk, request.user)
    except PermissionDenied:
        raise Http404
    return redirect('speech_workflow:detail', pk=pk)


@login_required
@require_POST
def observation_create(request, pk):
    item = _owned(request, pk)
    revision = current_revision(item)
    version_form = TranscriptEditForm(request.POST)
    observation_form = ObservationDraftForm(request.POST)
    if not version_form.is_valid() or not observation_form.is_valid():
        return _private(render(request, 'speech_workflow/detail.html', {
            'item': item, 'revision': revision, 'edit_form': TranscriptEditForm(initial={
                'expected_revision': revision.number, 'expected_hash': revision.content_hash, 'text': revision.text}),
            'observation_form': observation_form,
        }, status=422))
    try:
        create_observation_draft(pk, request.user, version_form.cleaned_data['expected_revision'],
                                 version_form.cleaned_data['expected_hash'], observation_form.cleaned_data)
    except PermissionDenied:
        raise Http404
    except ValidationError as error:
        observation_form.add_error(None, error)
        return _private(render(request, 'speech_workflow/detail.html', {
            'item': item, 'revision': current_revision(item), 'edit_form': TranscriptEditForm(initial={
                'expected_revision': revision.number, 'expected_hash': revision.content_hash, 'text': revision.text}),
            'observation_form': observation_form,
        }, status=409))
    return redirect('speech_workflow:detail', pk=pk)


@login_required
@require_POST
def inquiry_draft_confirm(request, pk):
    item = _owned(request, pk)
    import uuid
    try:
        draft_id = uuid.UUID(request.POST.get('draft_id', ''))
    except (ValueError, TypeError, AttributeError):
        raise Http404
    draft = get_object_or_404(VoiceInquiryDraft, transcript_revision__submission=item, pk=draft_id)
    from .inquiry_boundary import confirm_inquiry_draft
    from .adapters import AdapterDisabled
    try:
        confirm_inquiry_draft(draft.pk, request.user, request.POST.get('expected_hash'),
            reviewed_fields=request.POST.getlist('reviewed_fields'),
            accept_unknowns=request.POST.get('accept_unknowns') == 'yes',
            reply_reviewed=request.POST.get('reply_reviewed') == 'yes')
    except PermissionDenied:
        raise Http404
    except (ValidationError, AdapterDisabled) as error:
        message = '; '.join(error.messages) if isinstance(error, ValidationError) else str(error)
        return _private(HttpResponse(message, status=409, content_type='text/plain; charset=utf-8'))
    return redirect('speech_workflow:detail', pk=item.pk)
