"""Manager route with actual parser and explicitly STUBBED image-to-text stage."""
import io
import hashlib
from unittest.mock import patch
from PIL import Image
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from travelcards.forms import FIELD_LABELS,normalize_fields
from travelcards.ocr import parse_card_text
from .models import WorksheetReview


class ParserSemanticManagerFlowTests(TestCase):
    def test_review_confirmation_and_edit_keep_unknowns_conflicts_and_original(self):
        owner=get_user_model().objects.create_user('synthetic-parser-owner')
        self.client.force_login(owner)
        buffer=io.BytesIO();Image.new('RGB',(50,40),'white').save(buffer,format='PNG');raw=buffer.getvalue()
        text='Тип поездки: неизвестно\nПожелания: экскурсии\nДети: -2\nБюджет: -509000 рублей'
        fields,meta=parse_card_text(text)
        # This isolates the text-parser/UI contract; no claim of reading this blank image.
        with patch('worksheet_reviews.ocr_services.recognize_card_photo',return_value=(fields,meta)):
            response=self.client.post(reverse('worksheet_reviews:upload'),{
                'work_code':'SYNTHETIC-PARSER','worksheet':SimpleUploadedFile('synthetic.png',raw,content_type='image/png')})
        self.assertEqual(response.status_code,302)
        work=WorksheetReview.objects.get(work_code='SYNTHETIC-PARSER')
        revision=work.ocr_revisions.get(number=1)
        self.assertIsNone(revision.confirmed_at)
        self.assertEqual(bytes(work.worksheet_bytes),raw)
        self.assertEqual(work.worksheet_sha256,hashlib.sha256(raw).hexdigest())
        self.assertEqual(revision.original_text,text)
        self.assertEqual(revision.fields['trip_type']['state'],'unknown')
        url=reverse('worksheet_reviews:detail',args=[work.pk]);page=self.client.get(url)
        for label in ('Лист ожидает сверки','не синхронизировано','Отрицательный бюджет','Отрицательное число детей'):
            self.assertContains(page,label)

        def form(rev,action):
            data={'expected_revision':rev.number,'expected_hash':rev.content_hash,'action':action,'budget_currency':'RUB'}
            for key,field in normalize_fields(rev.fields).items():
                if key in FIELD_LABELS:
                    data.update({key+'_value':field['value'],key+'_state':field['state'],key+'_source':field['source']['description']})
            return data

        self.assertEqual(self.client.post(url,form(revision,'confirm_ocr')).status_code,409)
        data=form(revision,'confirm_ocr')
        data.update({'wishes_state':'known','wishes_source':'Менеджер сверил синтетические пожелания.'})
        self.assertEqual(self.client.post(url,data).status_code,302)
        confirmed=work.ocr_revisions.order_by('-number').first()
        self.assertIsNotNone(confirmed.confirmed_at)
        self.assertEqual(confirmed.fields['trip_type']['state'],'unknown')
        self.assertEqual(confirmed.fields['budget']['state'],'conflict')
        self.assertEqual(confirmed.fields['children_count']['state'],'conflict')
        self.assertEqual(confirmed.fields['wishes']['source']['kind'],'manual_review')
        edited=form(confirmed,'save_ocr');edited.update({'wishes_value':'экскурсии и музеи'})
        self.assertEqual(self.client.post(url,edited).status_code,302)
        latest=work.ocr_revisions.order_by('-number').first()
        self.assertIsNone(latest.confirmed_at);confirmed.refresh_from_db();self.assertIsNotNone(confirmed.confirmed_at)
        work.refresh_from_db();self.assertEqual(bytes(work.worksheet_bytes),raw)
        self.assertEqual(self.client.post(url,data).status_code,409)
