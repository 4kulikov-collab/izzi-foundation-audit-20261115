import uuid

from django import forms

from .models import HotelObservationDraft


class AudioUploadForm(forms.Form):
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput())
    audio = forms.FileField(label='Голосовой исходник', help_text='Только PCM WAV; тестовый предел 5 МиБ и 5 минут. Эти значения не согласованы для production.')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['idempotency_key'].initial = uuid.uuid4()


class TranscriptEditForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput())
    expected_hash = forms.RegexField(r'^[0-9a-f]{64}$', widget=forms.HiddenInput())
    text = forms.CharField(label='Расшифровка', required=False, max_length=12000, widget=forms.Textarea(attrs={'rows': 8}))


class ObservationDraftForm(forms.Form):
    hotel_name = forms.CharField(max_length=160)
    observed_on = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    building_room = forms.CharField(max_length=160, required=False)
    theme = forms.CharField(max_length=100)
    experience = forms.ChoiceField(choices=HotelObservationDraft.Experience.choices)
    observation_text = forms.CharField(max_length=5000, widget=forms.Textarea(attrs={'rows': 5}))
