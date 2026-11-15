from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse, Http404
from uuid import UUID
from django.shortcuts import get_object_or_404, render, redirect
from django.views.decorators.http import require_http_methods
from presentations.models import Hotel
from presentations.views import _private
from worksheet_reviews.models import WorksheetReview
from worksheet_reviews.ocr_services import current_revision
from .models import Inquiry, WorkLink, Version
from .services import source_file
from worksheet_reviews.ui import work_state
from .manager import prepare

class InquiryChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f'{obj.traveller_label} · {str(obj.pk)[:8]}'


class SelectionForm(forms.Form):
    expected_hash = forms.CharField(widget=forms.HiddenInput)
    traveller_label = forms.CharField(label='Имя / обозначение туриста', max_length=160, required=False, help_text='Заполняется только для нового обращения.')
    inquiry = InquiryChoiceField(label='Обращение', queryset=Inquiry.objects.none(), required=False, empty_label='Создать новое обращение')
    hotels = forms.ModelMultipleChoiceField(label='Варианты из каталога (от 1 до 5)', queryset=Hotel.objects.all(), widget=forms.CheckboxSelectMultiple)
    exact_offer = forms.BooleanField(label='Указать выбранное синтетическое предложение', required=False)
    room_type = forms.CharField(label='Тип номера — как в выбранном предложении', max_length=200, required=False)
    tariff = forms.CharField(label='Тариф — как выбран менеджером', max_length=200, required=False)
    price_basis = forms.CharField(label='За что цена: состав, срок и услуги', max_length=200, required=False)
    base_price = forms.CharField(label='Базовая цена, рубли (только менеджеру)', required=False, max_length=20, help_text='Например 180000.00. Не бюджет туриста.')
    client_price = forms.CharField(label='Клиентская цена, рубли (показывается туристу)', required=False, max_length=20)
    currency = forms.ChoiceField(label='Валюта предложения', choices=[('RUB','Рубли (RUB)')], required=False)
    selection_confirmed = forms.BooleanField(label='Явно выбрал этот тип номера и тариф (синтетика)', required=False)
    flight_confirmed = forms.BooleanField(label='Перелёт сверен по указанному основанию (синтетика)', required=False)
    flight_details = forms.CharField(label='Подтверждённые сведения о перелёте; иначе оставить пустым', required=False, max_length=400)
    flight_evidence = forms.CharField(label='Основание сверки перелёта (только менеджеру)', required=False, max_length=400)

    def clean(self):
        data = super().clean()
        if not data.get('exact_offer') and any(data.get(k) for k in ('room_type','tariff','price_basis','base_price','client_price','flight_details','flight_evidence','flight_confirmed','selection_confirmed')):
            raise forms.ValidationError('Включите выбранное синтетическое предложение или очистите его поля.')
        if not data.get('flight_confirmed') and (data.get('flight_details') or data.get('flight_evidence')):
            raise forms.ValidationError('Неподтверждённые сведения о перелёте не включаются в предложение.')
        return data

    def offer_data(self):
        data = self.cleaned_data
        if not data.get('exact_offer'):
            return None
        from .offers import SCHEMA
        hotels = list(data['hotels'])
        if len(hotels) != 1:
            raise ValidationError('Для выбранного предложения нужен ровно один отель.')
        return {'schema': SCHEMA, 'synthetic_only': True, 'hotel_id': str(hotels[0].pk),
                **{k: data.get(k) for k in ('room_type','tariff','price_basis','base_price','client_price','currency','selection_confirmed')},
                'flight': {'confirmed': True, 'details': data['flight_details'], 'evidence': data['flight_evidence']} if data['flight_confirmed'] else None}

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['inquiry'].queryset = Inquiry.objects.filter(owner=actor).order_by('-created_at')

@login_required
@require_http_methods(['GET', 'POST'])
def selection(request, work_id):
    work = get_object_or_404(WorksheetReview, pk=work_id, owner=request.user, owner__is_active=True)
    revision = current_revision(work)
    linked = WorkLink.objects.filter(work_id=work.pk).select_related('inquiry').first()
    chosen = linked.inquiry if linked else None
    if not linked and request.GET.get('inquiry'):
        try:
            chosen_id = UUID(request.GET['inquiry'])
        except ValueError:
            raise Http404
        chosen = get_object_or_404(Inquiry, pk=chosen_id, owner=request.user)
    initial = {'expected_hash': revision.content_hash if revision else '', 'inquiry': chosen.pk if chosen else None}
    form = SelectionForm(request.POST or None, actor=request.user, initial=initial)
    if linked:
        form.fields['inquiry'].queryset = Inquiry.objects.filter(pk=linked.inquiry_id, owner=request.user)
        form.fields['inquiry'].empty_label = None
        form.fields['traveller_label'].widget = forms.HiddenInput()
    if request.method == 'POST' and form.is_valid():
        try:
            result = prepare(actor=request.user, work_id=work.pk,
                             expected_hash=form.cleaned_data['expected_hash'],
                             hotel_ids=[h.pk for h in form.cleaned_data['hotels']],
                             inquiry_id=form.cleaned_data['inquiry'].pk if form.cleaned_data['inquiry'] else None,
                             traveller_label=form.cleaned_data['traveller_label'], offer=form.offer_data())
            return redirect('presentations:preview', pk=result.pk)
        except ValidationError as error:
            form.add_error(None, error)
    return _private(render(request, 'inquiries/selection.html', {'work': work, 'form': form, 'linked': linked, 'manager_state': work_state(work),
                            'has_hotels': Hotel.objects.exists()},
                           status=422 if request.method == 'POST' else 200))


@login_required
@require_http_methods(['GET'])
def inquiry_list(request):
    inquiries = Inquiry.objects.filter(owner=request.user, owner__is_active=True).order_by('-created_at')
    from .models import DirectorOfficeGrant
    director_access = DirectorOfficeGrant.objects.filter(user=request.user, user__is_active=True,
        is_active=True, office__is_active=True).exists()
    return _private(render(request, 'inquiries/list.html', {'inquiries': inquiries, 'director_access': director_access}))


@login_required
@require_http_methods(['GET'])
def inquiry_detail(request, pk):
    inquiry = get_object_or_404(Inquiry, pk=pk, owner=request.user, owner__is_active=True)
    works = list(WorksheetReview.objects.filter(owner=request.user,
        pk__in=inquiry.works.values_list('work_id', flat=True)))
    for work in works:
        work.manager_state = work_state(work)
    return _private(render(request, 'inquiries/detail.html', {'inquiry': inquiry, 'works': works,
        'voice_sources': inquiry.voice_sources.select_related('submission'),
        'versions': inquiry.versions.order_by('-number'),
        'selections': inquiry.selections.select_related('selection').order_by('-selection__created_at')}))


@login_required
@require_http_methods(['GET'])
def version_source(request, pk):
    version = get_object_or_404(Version, pk=pk, inquiry__owner=request.user, inquiry__owner__is_active=True)
    try:
        data, mime = source_file(actor=request.user, version_id=version.pk)
    except ValidationError as error:
        return _private(render(request, 'inquiries/source_error.html',
            {'inquiry': version.inquiry, 'error': ' '.join(error.messages)}, status=409))
    response = HttpResponse(data, content_type=mime)
    ext = {'image/png': 'png', 'image/jpeg': 'jpg', 'application/pdf': 'pdf', 'audio/wav': 'wav'}[mime]
    label = 'voice' if mime == 'audio/wav' else 'worksheet'
    disposition = 'attachment' if mime == 'audio/wav' else 'inline'
    response['Content-Disposition'] = f'{disposition}; filename="{label}-v{version.number}.{ext}"'
    return _private(response)


class VoiceSelectionForm(SelectionForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        del self.fields['inquiry']
        for name in ('exact_offer','room_type','tariff','price_basis','base_price','client_price','currency','selection_confirmed','flight_confirmed','flight_details','flight_evidence'):
            del self.fields[name]


@login_required
@require_http_methods(['GET', 'POST'])
def voice_selection(request, draft_id):
    from speech_workflow.models import VoiceInquiryDraft
    from .models import VoiceSourceLink
    from .voice import prepare as prepare_voice
    draft = get_object_or_404(VoiceInquiryDraft, pk=draft_id,
        transcript_revision__submission__owner=request.user,
        transcript_revision__submission__owner__is_active=True)
    linked = VoiceSourceLink.objects.filter(submission_id=draft.transcript_revision.submission_id).select_related('inquiry').first()
    form = VoiceSelectionForm(request.POST or None, actor=request.user, initial={'expected_hash': draft.content_hash})
    if linked or draft.target_inquiry_id:
        form.fields['traveller_label'].widget = forms.HiddenInput()
    if request.method == 'POST' and form.is_valid():
        try:
            selection = prepare_voice(actor=request.user, draft_id=draft.pk,
                expected_hash=form.cleaned_data['expected_hash'], traveller_label=form.cleaned_data['traveller_label'],
                hotel_ids=[h.pk for h in form.cleaned_data['hotels']])
            return redirect('presentations:preview', pk=selection.pk)
        except ValidationError as error:
            form.add_error(None, error)
    return _private(render(request, 'inquiries/voice_selection.html', {'draft': draft, 'linked': linked, 'form': form},
        status=422 if request.method == 'POST' else 200))
