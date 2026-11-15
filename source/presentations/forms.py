from django import forms

from .models import TravelRequest


class TravelRequestForm(forms.ModelForm):
    class Meta:
        model = TravelRequest
        fields = ["title", "traveller_summary", "requirements", "selected_hotels", "internal_notes"]
        widgets = {
            "traveller_summary": forms.Textarea(attrs={"rows": 3}),
            "requirements": forms.Textarea(attrs={"rows": 4}),
            "internal_notes": forms.Textarea(attrs={"rows": 3}),
            "selected_hotels": forms.CheckboxSelectMultiple(),
        }
        labels = {"selected_hotels": "Варианты отелей (от 1 до 5)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        travel_request = self.instance
        if travel_request.pk and (hasattr(travel_request, 'source_card') or hasattr(travel_request, 'worksheetselection') or hasattr(travel_request, 'voiceselection')):
            for name in ('traveller_summary', 'requirements'):
                self.fields[name].disabled = True
                self.fields[name].help_text = (
                    'Поле связано с подтверждённым источником. Исправляйте карту или голосовую расшифровку, '
                    'создайте и подтвердите новую редакцию.'
                )

    def clean_selected_hotels(self):
        hotels = self.cleaned_data["selected_hotels"]
        if not 1 <= len(hotels) <= 5:
            raise forms.ValidationError("Выберите от одного до пяти тестовых отелей.")
        return hotels
