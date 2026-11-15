"""Private synthetic review workflow; no catalogue or real student data required."""
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from PIL import Image
from pypdf import PdfWriter

from .models import HotelChoice, WorksheetReview


def synthetic_png():
    image = Image.new('RGB', (40, 30), color='white')
    stream = BytesIO()
    image.save(stream, format='PNG')
    return SimpleUploadedFile('synthetic-sheet.png', stream.getvalue(), content_type='image/png')


class WorksheetReviewFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user('demo-owner', password='synthetic-password')
        cls.other = User.objects.create_user('demo-other', password='synthetic-password')

    def form_data(self, code, *, hotel_names=None, refs=None, overview=''):
        hotel_names = hotel_names or ['Совершенно новый объект «Маяк»', 'Учебный отель «Лагуна-26»', 'Неизвестный объект «Лес»']
        refs = refs or ['', '', '']
        data = {'work_code': code, 'worksheet': synthetic_png(), 'expert_overview': overview}
        for index in range(1, 4):
            data[f'hotel_{index}_name'] = hotel_names[index - 1]
            data[f'hotel_{index}_reference'] = refs[index - 1]
        return data

    def upload(self, code='DEMO-001', *, owner=None, **kwargs):
        self.client.force_login(owner or self.owner)
        return self.client.post(reverse('worksheet_reviews:upload'), self.form_data(code, **kwargs))

    def test_upload_reopen_and_manual_overview_are_bound_to_the_same_work(self):
        response = self.upload(overview='Синтетический обзор первой работы.')
        self.assertEqual(response.status_code, 302)
        review = WorksheetReview.objects.get(work_code='DEMO-001')
        self.assertEqual(review.owner, self.owner)
        self.assertEqual(list(review.hotel_choices.values_list('position', flat=True)), [1, 2, 3])
        self.assertEqual(review.hotel_choices.get(position=1).name, 'Совершенно новый объект «Маяк»')
        self.assertEqual(review.expert_overview, 'Синтетический обзор первой работы.')

        reopened = self.client.get(reverse('worksheet_reviews:detail', args=[review.pk]))
        self.assertEqual(reopened.status_code, 200)
        self.assertContains(reopened, 'Совершенно новый объект «Маяк»')
        self.assertContains(reopened, 'Синтетический обзор первой работы.')

        self.client.post(reverse('worksheet_reviews:detail', args=[review.pk]), {
            'hotel_1_name': 'Обновлённый синтетический отель', 'hotel_1_reference': 'https://example.invalid/hotel',
            'hotel_2_name': 'Второй новый отель', 'hotel_2_reference': 'Из заметки № 2',
            'hotel_3_name': 'Третий новый отель', 'hotel_3_reference': '',
            'expert_overview': 'Обновлённый обзор относится только к DEMO-001.',
        })
        review.refresh_from_db()
        reopened = self.client.get(reverse('worksheet_reviews:detail', args=[review.pk]))
        self.assertContains(reopened, 'Обновлённый синтетический отель')
        self.assertContains(reopened, 'Обновлённый обзор относится только к DEMO-001.')
        self.assertEqual(review.hotel_choices.get(position=1).reference_text, 'https://example.invalid/hotel')

    def test_arbitrary_hotels_and_reference_text_need_no_catalog_or_network(self):
        refs = ['https://example.invalid/one', 'Студенческая заметка без URL', 'https://example.invalid/three']
        response = self.upload(code='DEMO-REF1', refs=refs)
        self.assertEqual(response.status_code, 302)
        review = WorksheetReview.objects.get(work_code='DEMO-REF1')
        self.assertEqual(list(review.hotel_choices.values_list('reference_text', flat=True)), refs)
        detail = self.client.get(reverse('worksheet_reviews:detail', args=[review.pk]))
        self.assertContains(detail, 'https://example.invalid/one')
        self.assertNotContains(detail, 'href="https://example.invalid/one"')
        self.assertEqual(HotelChoice.objects.count(), 3)

    def test_separate_work_records_keep_hotels_files_and_overviews_independent(self):
        self.assertEqual(self.upload('DEMO-101', hotel_names=['Работа 101 — A', 'Работа 101 — B', 'Работа 101 — C'],
                                     overview='Обзор только работы 101.').status_code, 302)
        self.assertEqual(self.upload('DEMO-102', hotel_names=['Работа 102 — X', 'Работа 102 — Y', 'Работа 102 — Z'],
                                     overview='Обзор только работы 102.').status_code, 302)
        first = WorksheetReview.objects.get(work_code='DEMO-101')
        second = WorksheetReview.objects.get(work_code='DEMO-102')
        first_page = self.client.get(reverse('worksheet_reviews:detail', args=[first.pk]))
        second_page = self.client.get(reverse('worksheet_reviews:detail', args=[second.pk]))
        self.assertContains(first_page, 'Работа 101 — A')
        self.assertContains(first_page, 'Обзор только работы 101.')
        self.assertNotContains(first_page, 'Работа 102 — X')
        self.assertNotContains(first_page, 'Обзор только работы 102.')
        self.assertContains(second_page, 'Работа 102 — X')
        self.assertContains(second_page, 'Обзор только работы 102.')
        self.assertNotContains(second_page, 'Работа 101 — A')

        first_sheet = self.client.get(reverse('worksheet_reviews:sheet', args=[first.pk]))
        second_sheet = self.client.get(reverse('worksheet_reviews:sheet', args=[second.pk]))
        self.assertEqual(first_sheet['Content-Type'], 'image/png')
        self.assertEqual(second_sheet['Content-Type'], 'image/png')
        self.assertEqual(first_sheet.content, second_sheet.content)
        self.assertEqual(self.client.get(reverse('worksheet_reviews:list')).status_code, 200)

    def test_file_and_record_are_private_to_authenticated_owner(self):
        self.upload()
        review = WorksheetReview.objects.get(work_code='DEMO-001')
        url = reverse('worksheet_reviews:sheet', args=[review.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

        other = Client()
        other.force_login(self.other)
        self.assertEqual(other.get(url).status_code, 404)
        self.assertEqual(other.get(reverse('worksheet_reviews:detail', args=[review.pk])).status_code, 404)
        self.assertNotContains(other.get(reverse('worksheet_reviews:list')), 'DEMO-001')
        self.assertEqual(Client().get(url).status_code, 302)

    def test_synthetic_pdf_can_be_saved_and_opened_only_by_owner(self):
        pdf = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=400)
        writer.write(pdf)
        self.client.force_login(self.owner)
        data = self.form_data('DEMO-PDF1')
        data['worksheet'] = SimpleUploadedFile('synthetic.pdf', pdf.getvalue(), content_type='application/pdf')
        response = self.client.post(reverse('worksheet_reviews:upload'), data)
        self.assertEqual(response.status_code, 302)
        review = WorksheetReview.objects.get(work_code='DEMO-PDF1')
        file_response = self.client.get(reverse('worksheet_reviews:sheet', args=[review.pk]))
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response['Content-Type'], 'application/pdf')
        self.assertTrue(file_response.content.startswith(b'%PDF-'))

    def test_duplicate_owner_code_is_rejected_without_overwriting_work(self):
        self.assertEqual(self.upload('DEMO-777', overview='Первоначальный обзор.').status_code, 302)
        response = self.client.post(reverse('worksheet_reviews:upload'), self.form_data(
            'DEMO-777', hotel_names=['Дубль A', 'Дубль B', 'Дубль C'], overview='Подмена.',
        ))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(WorksheetReview.objects.filter(owner=self.owner, work_code='DEMO-777').count(), 1)
        self.assertEqual(WorksheetReview.objects.get(work_code='DEMO-777').expert_overview, 'Первоначальный обзор.')

    def test_work_code_rejects_names_and_upload_does_not_create_users(self):
        before = get_user_model().objects.count()
        self.client.force_login(self.owner)
        response = self.client.post(reverse('worksheet_reviews:upload'), self.form_data('Иванов-И.И.'))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(get_user_model().objects.count(), before)
        self.assertEqual(WorksheetReview.objects.count(), 0)

    def test_invalid_or_corrupt_file_is_rejected_without_saving(self):
        self.client.force_login(self.owner)
        invalid_files = [
            b'not a real PNG',
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\x01IHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02\x00\x00\x00\x0bIDATx\xda\x63\xfc\xff\x1f\x00\x03\x03\x02\x00\xef\xa3\x9e\x2a\x00\x00\x00\x00IEND\xaeB`\x82',
        ]
        for index, data in enumerate(invalid_files):
            with self.subTest(index=index):
                response = self.client.post(reverse('worksheet_reviews:upload'), self.form_data(
                    f'DEMO-BAD{index}',
                ) | {'worksheet': SimpleUploadedFile('broken.png', data, content_type='image/png')})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(WorksheetReview.objects.count(), 0)

    def test_owner_list_has_no_student_queue_and_ui_requires_manual_ocr_review(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('worksheet_reviews:list'))
        self.assertContains(response, 'Подтверждённая карта используется для подборки и локального обращения')
        self.assertContains(response, 'Перед подборкой менеджер сверяет поля с листом и подтверждает их')
        self.assertNotContains(response, 'OCR, оценивание и автоматическая рекомендация не выполняются')
