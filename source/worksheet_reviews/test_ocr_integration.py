from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from PIL import Image
from pypdf import PdfWriter

from travelcards.forms import FIELD_LABELS, normalize_fields
from worksheet_reviews.models import HotelChoice, WorksheetOCRRevision, WorksheetReview
from worksheet_reviews.ocr_services import current_source


def png_upload(color='white'):
    image = Image.new('RGB', (48, 36), color=color)
    stream = BytesIO()
    image.save(stream, format='PNG')
    return SimpleUploadedFile('synthetic-work.png', stream.getvalue(), content_type='image/png')


def pdf_upload():
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.write(stream)
    return SimpleUploadedFile('synthetic-work.pdf', stream.getvalue(), content_type='application/pdf')


def known_fields(prefix):
    result = {
        key: {'value': f'{prefix} {key}', 'state': 'known',
              'source': {'kind': 'local_tesseract', 'page': 1, 'description': 'Печатный synthetic fixture.'}}
        for key in FIELD_LABELS
    }
    result['budget']['currency'] = 'RUB'
    return result


class WorksheetOCRIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user('ocr-owner', password='synthetic-password')
        cls.other = User.objects.create_user('ocr-other', password='synthetic-password')

    def post_upload(self, code='OCR-001', *, file=None):
        self.client.force_login(self.owner)
        data = {'work_code': code, 'worksheet': file or png_upload(),
                'expert_overview': f'Обзор только {code}.'}
        for index in range(1, 4):
            data[f'hotel_{index}_name'] = f'Вымышленный {code} отель {index}'
            data[f'hotel_{index}_reference'] = f'Синтетическая заметка {index}'
        return self.client.post(reverse('worksheet_reviews:upload'), data)

    def fields_post(self, revision, *, action='save_ocr', overrides=None):
        values = normalize_fields(revision.fields)
        data = {'action': action, 'expected_revision': revision.number,
                'expected_hash': revision.content_hash}
        for key in FIELD_LABELS:
            value = values[key]
            data[key + '_value'] = value['value']
            data[key + '_state'] = value['state']
            data[key + '_source'] = value['source']['description']
        data['budget_currency'] = values['budget'].get('currency', 'RUB')
        data.update(overrides or {})
        return data

    def fake_recognition(self, prefix, text):
        fields = known_fields(prefix)
        if prefix == 'Первый OCR':
            fields['wishes']['state'] = 'ocr_candidate'
        return fields, {
            'engine': 'mock-only-test', 'version': 'fixture', 'language': 'rus',
            'status': 'recognized', 'note': 'Подставленный текст только для теста.',
            'raw_text': text,
        }

    def test_one_upload_edit_confirm_rerun_and_reopen_preserve_work_data(self):
        first = self.fake_recognition('Первый OCR', 'Исходный synthetic OCR текст.')
        second = self.fake_recognition('Второй OCR', 'Повторный synthetic OCR текст.')
        with patch('worksheet_reviews.ocr_services.recognize_card_photo', side_effect=[first, second]) as recognize:
            response = self.post_upload()
            self.assertEqual(response.status_code, 302)
            recognize.assert_called_once()
            review = WorksheetReview.objects.get(work_code='OCR-001')
            revision = review.ocr_revisions.get(number=1)
            self.assertEqual(revision.source_sha256, review.worksheet_sha256)
            self.assertEqual(revision.original_text, 'Исходный synthetic OCR текст.')
            self.assertEqual(revision.fields['wishes']['state'], 'ocr_candidate')
            self.assertEqual(bytes(review.worksheet_bytes)[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(WorksheetOCRRevision.objects.filter(review=review).count(), 1)
            self.assertEqual(HotelChoice.objects.filter(review=review).count(), 3)

            page = self.client.get(reverse('worksheet_reviews:detail', args=[review.pk]))
            self.assertContains(page, 'Исходный synthetic OCR текст.')
            self.assertContains(page, 'Пожелания по фото')

            unreviewed = self.fields_post(revision, action='confirm_ocr')
            self.assertEqual(self.client.post(reverse('worksheet_reviews:detail', args=[review.pk]), unreviewed).status_code, 409)

            saved_data = self.fields_post(revision, overrides={
                'wishes_value': 'Исправленное пожелание владельца',
                'wishes_state': 'known',
                'wishes_source': 'Сверено вручную с синтетическим фото.',
            })
            self.assertEqual(self.client.post(reverse('worksheet_reviews:detail', args=[review.pk]), saved_data).status_code, 302)
            edited = review.ocr_revisions.first()
            self.assertEqual(edited.number, 2)
            self.assertEqual(edited.fields['wishes']['source']['kind'], 'manual_review')

            confirm_data = self.fields_post(edited, action='confirm_ocr')
            self.assertEqual(self.client.post(reverse('worksheet_reviews:detail', args=[review.pk]), confirm_data).status_code, 302)
            confirmed = review.ocr_revisions.first()
            self.assertIsNotNone(confirmed.confirmed_at)
            self.assertEqual(confirmed.confirmed_by, self.owner)

            rerun_data = {'action': 'rerun_ocr', 'expected_revision': confirmed.number,
                          'expected_hash': confirmed.content_hash}
            self.assertEqual(self.client.post(reverse('worksheet_reviews:detail', args=[review.pk]), rerun_data).status_code, 302)
            rerun = review.ocr_revisions.first()
            self.assertEqual(rerun.number, 3)
            self.assertIsNone(rerun.confirmed_at)
            self.assertEqual(rerun.original_text, 'Повторный synthetic OCR текст.')
            self.assertEqual(rerun.fields['wishes']['value'], 'Исправленное пожелание владельца')
            self.assertEqual(rerun.fields['wishes']['source']['kind'], 'manual_review')
            self.assertEqual(rerun.fields['party']['value'], 'Второй OCR party')
            review.refresh_from_db()
            self.assertEqual(review.expert_overview, 'Обзор только OCR-001.')
            self.assertEqual(list(review.hotel_choices.values_list('name', flat=True)),
                             [f'Вымышленный OCR-001 отель {n}' for n in range(1, 4)])
            self.assertEqual(review.worksheet_sha256, rerun.source_sha256)
            self.assertEqual(self.client.post(reverse('worksheet_reviews:detail', args=[review.pk]), rerun_data).status_code, 409)
            self.assertEqual(review.ocr_revisions.count(), 3)
            reopened = self.client.get(reverse('worksheet_reviews:detail', args=[review.pk]))
            self.assertContains(reopened, 'Исправленное пожелание владельца')
            self.assertContains(reopened, 'Вымышленный OCR-001 отель 1')
            self.assertContains(reopened, 'Обзор только OCR-001.')

    def test_pdf_ocr_fallback_saves_original_and_manual_path_without_calling_engine(self):
        with patch('worksheet_reviews.ocr_services.recognize_card_photo') as recognize:
            response = self.post_upload('OCR-PDF', file=pdf_upload())
        self.assertEqual(response.status_code, 302)
        recognize.assert_not_called()
        review = WorksheetReview.objects.get(work_code='OCR-PDF')
        revision = review.ocr_revisions.get(number=1)
        self.assertEqual(revision.metadata['status'], 'unsupported_pdf')
        self.assertEqual(revision.original_text, '')
        self.assertTrue(bytes(review.worksheet_bytes).startswith(b'%PDF-'))
        self.assertEqual(self.client.get(reverse('worksheet_reviews:sheet', args=[review.pk])).status_code, 200)

    def test_replacing_source_keeps_original_and_revokes_prior_confirmation(self):
        first = self.fake_recognition('Исходный OCR', 'Исходное фото, синтетика.')
        second = self.fake_recognition('Новый OCR', 'Новое фото, синтетика.')
        with patch('worksheet_reviews.ocr_services.recognize_card_photo', side_effect=[first, second]) as recognize:
            self.assertEqual(self.post_upload('OCR-REPL').status_code, 302)
            review = WorksheetReview.objects.get(work_code='OCR-REPL')
            first_revision = review.ocr_revisions.get(number=1)
            confirm = self.fields_post(first_revision, action='confirm_ocr')
            self.assertEqual(self.client.post(reverse('worksheet_reviews:detail', args=[review.pk]), confirm).status_code, 302)
            confirmed = review.ocr_revisions.get(number=1)
            original_bytes = bytes(review.worksheet_bytes)
            new_file = png_upload(color='navy')
            response = self.client.post(reverse('worksheet_reviews:detail', args=[review.pk]), {
                'action': 'replace_source', 'expected_source_version': 1,
                'expected_source_hash': review.worksheet_sha256, 'worksheet': new_file,
            })
            self.assertEqual(response.status_code, 302)
            self.assertEqual(recognize.call_count, 2)  # initial upload and one replacement only

        review.refresh_from_db()
        current = current_source(review)
        latest = review.ocr_revisions.first()
        self.assertEqual(review.current_source_version, 2)
        self.assertNotEqual(bytes(current.worksheet_bytes), original_bytes)
        self.assertNotEqual(latest.source_sha256, confirmed.source_sha256)
        self.assertIsNone(latest.confirmed_at)
        self.assertEqual(confirmed.original_text, 'Исходное фото, синтетика.')
        self.assertTrue(review.file_revisions.filter(number=2).exists())
        self.assertEqual(review.expert_overview, 'Обзор только OCR-REPL.')
        self.assertEqual(review.hotel_choices.count(), 3)
        old_source_response = self.client.get(reverse('worksheet_reviews:sheet_version', args=[review.pk, 1]))
        self.assertEqual(old_source_response.content, original_bytes)
        current_response = self.client.get(reverse('worksheet_reviews:sheet', args=[review.pk]))
        self.assertEqual(current_response.content, bytes(current.worksheet_bytes))
        stale_replace = self.client.post(reverse('worksheet_reviews:detail', args=[review.pk]), {
            'action': 'replace_source', 'expected_source_version': 1,
            'expected_source_hash': review.worksheet_sha256, 'worksheet': png_upload(color='teal'),
        })
        self.assertEqual(stale_replace.status_code, 409)
        self.assertEqual(review.file_revisions.count(), 1)
        self.assertEqual(self.client.post(reverse('worksheet_reviews:detail', args=[review.pk]), confirm).status_code, 409)

    def test_unavailable_ocr_keeps_upload_and_can_be_manually_confirmed(self):
        fallback = {key: {'value': '', 'state': 'unknown',
                          'source': {'kind': 'manual_fallback', 'page': None,
                                     'description': 'OCR unavailable; проверьте оригинал вручную.'}}
                    for key in FIELD_LABELS}
        fallback['budget']['currency'] = 'RUB'
        with patch('worksheet_reviews.ocr_services.recognize_card_photo', return_value=(fallback, {
                'engine': 'Tesseract', 'version': '', 'language': 'rus', 'status': 'unavailable',
                'note': 'Нет локального OCR.', 'raw_text': ''})):
            response = self.post_upload('OCR-OFF')
        self.assertEqual(response.status_code, 302)
        review = WorksheetReview.objects.get(work_code='OCR-OFF')
        revision = review.ocr_revisions.get()
        self.assertTrue(bytes(review.worksheet_bytes).startswith(b'\x89PNG'))
        self.assertEqual(revision.metadata['status'], 'unavailable')
        confirm_data = self.fields_post(revision, action='confirm_ocr')
        self.assertEqual(self.client.post(reverse('worksheet_reviews:detail', args=[review.pk]), confirm_data).status_code, 302)
        self.assertIsNotNone(review.ocr_revisions.first().confirmed_at)

    def test_timeout_error_and_empty_ocr_keep_original_and_report_distinct_statuses(self):
        fallback = {key: {'value': '', 'state': 'unknown',
                          'source': {'kind': 'manual_fallback', 'page': None,
                                     'description': 'Синтетический fallback.'}}
                    for key in FIELD_LABELS}
        fallback['budget']['currency'] = 'RUB'
        for index, status in enumerate(('timeout', 'error', 'no_text', 'no_fields_found')):
            with self.subTest(status=status), patch(
                    'worksheet_reviews.ocr_services.recognize_card_photo',
                    return_value=(fallback, {'engine': 'Tesseract', 'version': '', 'language': 'rus',
                                             'status': status, 'note': f'Синтетический статус {status}.',
                                             'raw_text': ''})):
                response = self.post_upload(f'OCR-F{index}')
                self.assertEqual(response.status_code, 302)
                review = WorksheetReview.objects.get(work_code=f'OCR-F{index}')
                revision = review.ocr_revisions.get()
                self.assertEqual(revision.metadata['status'], status)
                self.assertTrue(bytes(review.worksheet_bytes).startswith(b'\x89PNG'))
                self.assertEqual(revision.fields['wishes']['state'], 'unknown')

    def test_ocr_revision_and_actions_are_private_to_owner(self):
        with patch('worksheet_reviews.ocr_services.recognize_card_photo', return_value=self.fake_recognition('OCR', 'mock')):
            self.assertEqual(self.post_upload('OCR-ACL').status_code, 302)
        review = WorksheetReview.objects.get(work_code='OCR-ACL')
        other = Client()
        other.force_login(self.other)
        self.assertEqual(other.get(reverse('worksheet_reviews:detail', args=[review.pk])).status_code, 404)
        data = self.fields_post(review.ocr_revisions.get(), action='confirm_ocr')
        self.assertEqual(other.post(reverse('worksheet_reviews:detail', args=[review.pk]), data).status_code, 404)
