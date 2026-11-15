"""Private manager route with actual local OCR, synthetic images, no listening server."""
import hashlib
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from travelcards.forms import FIELD_LABELS, normalize_fields
from travelcards.ocr import russian_tesseract_available
from travelcards.test_photo_ocr import FIXTURES
from .models import WorksheetReview


@skipUnless(russian_tesseract_available(), 'Real local rus required')
class RealPhotoManagerFlowTests(TestCase):
    def test_rotated_upload_manual_confirmation_rerun_and_source_acl(self):
        owner = get_user_model().objects.create_user('synthetic-photo-owner')
        other = get_user_model().objects.create_user('synthetic-photo-other')
        self.client.force_login(owner)
        raw = (FIXTURES / 'printed-exif-6.jpg').read_bytes()
        response = self.client.post(reverse('worksheet_reviews:upload'), {
            'work_code': 'SYNTHETIC-EXIF',
            'worksheet': SimpleUploadedFile('synthetic.jpg', raw, content_type='image/jpeg'),
        })
        self.assertEqual(response.status_code, 302)
        review = WorksheetReview.objects.get(work_code='SYNTHETIC-EXIF')
        revision = review.ocr_revisions.get(number=1)
        self.assertEqual(bytes(review.worksheet_bytes), raw)
        self.assertEqual(review.worksheet_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(revision.source_sha256, review.worksheet_sha256)
        self.assertTrue(revision.metadata['preprocessing']['applied'])
        self.assertEqual(revision.metadata['preprocessing']['source_sha256'], review.worksheet_sha256)
        self.assertIsNone(revision.confirmed_at)
        url = reverse('worksheet_reviews:detail', args=[review.pk])
        page = self.client.get(url)
        for label in ('Лист ожидает сверки', 'U-ON: не синхронизировано', 'Замечания к исходному OCR',
                      'вдали от дороги', 'Трансфер: не указан', 'Сохранённый оригинал не изменён'):
            self.assertContains(page, label)

        def form(current, action):
            result = {'expected_revision': current.number, 'expected_hash': current.content_hash, 'action': action}
            for key, field in normalize_fields(current.fields).items():
                if key in FIELD_LABELS:
                    result.update({key + '_value': field['value'], key + '_state': field['state'],
                                   key + '_source': field['source']['description']})
            result['budget_currency'] = 'RUB'
            return result

        # Real extracted candidates still cannot be confirmed implicitly.
        self.assertEqual(self.client.post(url, form(revision, 'confirm_ocr')).status_code, 409)
        confirmed_form = form(revision, 'confirm_ocr')
        for key in FIELD_LABELS:
            if revision.fields[key]['state'] == 'ocr_candidate':
                confirmed_form[key + '_state'] = 'known'
                confirmed_form[key + '_source'] = 'Сверено менеджером по синтетическому фото.'
        confirmed_form['wishes_value'] = 'тихий номер вдали от дороги'
        self.assertEqual(self.client.post(url, confirmed_form).status_code, 302)
        confirmed = review.ocr_revisions.order_by('-number').first()
        self.assertIsNotNone(confirmed.confirmed_at)
        self.assertEqual(confirmed.fields['children_count']['state'], 'unknown')
        self.assertEqual(confirmed.fields['dates']['value'], '10-20 июня')
        self.assertEqual(confirmed.fields['wishes']['source']['kind'], 'manual_review')
        self.assertEqual(self.client.post(url, confirmed_form).status_code, 409)
        self.assertEqual(self.client.post(url, form(confirmed, 'rerun_ocr')).status_code, 302)
        rerun = review.ocr_revisions.order_by('-number').first()
        self.assertIsNone(rerun.confirmed_at)
        self.assertEqual(rerun.fields['wishes']['value'], 'тихий номер вдали от дороги')
        self.assertEqual(rerun.fields['budget']['value'], '180000')
        confirmed.refresh_from_db()
        self.assertIsNotNone(confirmed.confirmed_at)
        review.refresh_from_db()
        self.assertEqual(bytes(review.worksheet_bytes), raw)
        self.client.logout()
        self.client.force_login(owner)
        self.assertContains(self.client.get(url), 'тихий номер вдали от дороги')
        self.client.force_login(other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(reverse('worksheet_reviews:sheet', args=[review.pk])).status_code, 404)
