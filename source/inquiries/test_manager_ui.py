"""Manager-facing HTTP journey, private archives, Russian recovery and local demo data."""
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase
from django.urls import reverse

from presentations.models import Hotel, Release, TravelRequest
from worksheet_reviews.models import WorksheetReview
from worksheet_reviews.ocr_services import current_revision, replace_source
from worksheet_reviews.test_ocr_integration import png_upload
from travelcards.forms import FIELD_LABELS
from travelcards.ocr import _empty_fields
from .management.commands.seed_manager_demo import demo_fields
from .models import Inquiry, SelectionLink, Version


class ManagerInterfaceTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('synthetic-ui-owner')
        self.other = get_user_model().objects.create_user('synthetic-ui-other')
        self.client.force_login(self.owner)
        self.hotels = [Hotel.objects.create(name=f'Вымышленный отель {i}', summary='Синтетический вариант',
            pros='Тестовые достоинства', limitations='Нет реального предложения', source_reference='Синтетика') for i in range(2)]

    def upload(self, code='UI-001', metadata=None, fields=None):
        metadata = metadata or {'status': 'unavailable', 'note': 'Распознавание недоступно; введите поля вручную.'}
        fields = fields or _empty_fields('manual_fallback', metadata['note'])
        with patch('worksheet_reviews.ocr_services.recognize_card_photo', return_value=(fields, metadata)):
            response = self.client.post(reverse('worksheet_reviews:upload'), {'work_code': code, 'worksheet': png_upload()})
        self.assertEqual(response.status_code, 302)
        return WorksheetReview.objects.get(work_code=code)

    def confirm(self, work):
        rev = current_revision(work)
        data = {'action': 'confirm_ocr', 'expected_revision': rev.number, 'expected_hash': rev.content_hash, 'budget_currency': 'RUB'}
        for key, field in demo_fields().items():
            data.update({key+'_value': field['value'], key+'_state': field['state'], key+'_source': field['source']['description']})
        response = self.client.post(reverse('worksheet_reviews:detail', args=[work.pk]), data)
        self.assertEqual(response.status_code, 302)
        return current_revision(work)

    def prepare(self, work, hotel=0, inquiry=None):
        data = {'expected_hash': current_revision(work).content_hash, 'hotels': [str(self.hotels[hotel].pk)],
                'traveller_label': 'Вымышленная поездка'}
        if inquiry:
            data['inquiry'] = str(inquiry.pk)
        response = self.client.post(reverse('inquiries:selection', args=[work.pk]), data)
        self.assertEqual(response.status_code, 302)
        return self.client.get(response.url)

    def publish(self, preview):
        selection = preview.context['travel_request']
        approved = self.client.post(reverse('presentations:approve', args=[selection.pk]), {'content_hash': preview.context['content_hash']})
        self.assertContains(approved, 'U-ON: не синхронизировано')
        response = self.client.post(reverse('presentations:publish', args=[selection.pk]), {'approval_id': approved.context['approval'].pk})
        self.assertContains(response, 'Выпуск привязан к обращению')
        return response.context['release'], response.context['share_url']

    def test_manager_path_from_home_to_two_releases_and_reopen_files(self):
        home = self.client.get('/')
        self.assertContains(home, reverse('worksheet_reviews:upload'))
        self.assertNotContains(home, reverse('presentations:request_new'))
        self.assertRedirects(self.client.get(reverse('presentations:request_new')), reverse('worksheet_reviews:upload'))
        work = self.upload()
        self.assertContains(self.client.get('/'), 'Лист ожидает сверки')
        choice = reverse('inquiries:selection', args=[work.pk])
        self.assertNotContains(self.client.get(choice), 'Подготовить подборку в обращении')
        denied = self.client.post(choice, {'expected_hash': current_revision(work).content_hash,
            'hotels': [str(self.hotels[0].pk)], 'traveller_label': 'Вымышленный турист'})
        self.assertContains(denied, 'Подтвердите актуальную карту', status_code=422)
        self.assertEqual(Inquiry.objects.count(), 0)
        self.confirm(work)
        self.assertContains(self.client.get('/'), 'Лист сверен')
        preview = self.prepare(work)
        selection = preview.context['travel_request']
        self.assertEqual(self.client.post(reverse('presentations:publish', args=[selection.pk]), {'approval_id': uuid4()}).status_code, 409)
        first, public_url = self.publish(preview)
        first_pdf, first_html = bytes(first.pdf_bytes), first.html_content
        inquiry = Inquiry.objects.get()
        history = self.client.get(reverse('inquiries:detail', args=[inquiry.pk]))
        self.assertContains(history, 'Повторная подборка')
        self.assertContains(history, 'Выпуск привязан к обращению')
        self.assertContains(history, reverse('presentations:archive_pdf', args=[first.pk]))
        self.assertEqual(self.client.get(reverse('presentations:archive_pdf', args=[first.pk])).content, first_pdf)
        self.assertEqual(self.client.get(reverse('presentations:archive', args=[first.pk])).content.decode(), first_html)
        version = Version.objects.get(snapshot__release__id=str(first.pk))
        self.assertEqual(self.client.get(reverse('inquiries:source', args=[version.pk])).content, bytes(work.worksheet_bytes))
        repeated = self.prepare(work)
        self.assertEqual(repeated.context['travel_request'].pk, selection.pk)
        second, _ = self.publish(self.prepare(work, hotel=1, inquiry=inquiry))
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(Inquiry.objects.count(), 1)
        self.assertEqual(Release.objects.count(), 2)
        first.refresh_from_db()
        self.assertEqual(first.html_content, first_html)
        self.assertEqual(bytes(first.pdf_bytes), first_pdf)
        self.assertRedirects(self.client.get(reverse('presentations:request_edit', args=[selection.pk])), choice)
        self.client.post(reverse('presentations:revoke', args=[first.pk]))
        self.assertEqual(Client().get(public_url).status_code, 404)
        self.assertEqual(self.client.get(reverse('presentations:archive_pdf', args=[first.pk])).content, first_pdf)

    def test_another_sheet_explicitly_uses_existing_inquiry_with_readable_label(self):
        first = self.upload(); self.confirm(first); self.prepare(first)
        inquiry = Inquiry.objects.get()
        second = self.upload('UI-002'); self.confirm(second)
        page = self.client.get(reverse('inquiries:selection', args=[second.pk]) + '?inquiry=' + str(inquiry.pk))
        self.assertContains(page, 'Вымышленная поездка')
        self.assertNotContains(page, 'Inquiry object')
        self.assertEqual(page.context['form'].initial['inquiry'], inquiry.pk)
        self.prepare(second, inquiry=inquiry)
        self.assertEqual(Inquiry.objects.count(), 1)
        self.assertEqual(SelectionLink.objects.filter(inquiry=inquiry).count(), 2)

    def test_source_replacement_shows_pending_and_recovery_without_reviving_approval(self):
        work = self.upload(); self.confirm(work); preview = self.prepare(work)
        selection = preview.context['travel_request']
        replace_source(work, self.owner, 1, work.worksheet_sha256, worksheet_bytes=png_upload('navy').read(), worksheet_type='image/png')
        self.assertContains(self.client.get('/'), 'Лист ожидает сверки')
        blocked = self.client.get(reverse('presentations:preview', args=[selection.pk]))
        self.assertContains(blocked, 'Вернуться к сверке листа')
        self.assertNotContains(blocked, 'Содержание проверено</button>')
        response = self.client.post(reverse('presentations:approve', args=[selection.pk]), {'content_hash': preview.context['content_hash']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Release.objects.count(), 0)

    def test_private_inquiry_and_files_are_unavailable_to_other_or_anonymous_user(self):
        work = self.upload(); self.confirm(work); release, _ = self.publish(self.prepare(work))
        inquiry = Inquiry.objects.get(); version = Version.objects.get(snapshot__release__id=str(release.pk))
        urls = [reverse('inquiries:detail', args=[inquiry.pk]), reverse('inquiries:source', args=[version.pk]),
                reverse('presentations:archive', args=[release.pk]), reverse('presentations:archive_pdf', args=[release.pk])]
        for url in urls:
            self.assertIn('no-store', self.client.get(url)['Cache-Control'])
            self.assertEqual(Client().get(url).status_code, 302)
        self.client.force_login(self.other)
        for url in urls:
            self.assertContains(self.client.get(url), 'Страница недоступна', status_code=404)
        self.assertNotContains(self.client.get(reverse('inquiries:list')), inquiry.traveller_label)
        self.client.force_login(self.owner)
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_invalid_ids_and_unavailable_catalog_do_not_create_partial_entities(self):
        work = self.upload(); self.confirm(work)
        url = reverse('inquiries:selection', args=[work.pk])
        self.assertEqual(self.client.get(url + '?inquiry=bad-id').status_code, 404)
        foreign = Inquiry.objects.create(owner=self.other, traveller_label='Чужое обращение')
        self.assertEqual(self.client.get(url + '?inquiry=' + str(foreign.pk)).status_code, 404)
        for data in ({'hotels': [str(uuid4())]}, {'hotels': [str(self.hotels[0].pk)], 'inquiry': str(foreign.pk)}, {'hotels': []}):
            data.update(expected_hash=current_revision(work).content_hash, traveller_label='Синтетика')
            self.assertEqual(self.client.post(url, data).status_code, 422)
        self.assertEqual(TravelRequest.objects.count(), 0)
        Hotel.objects.all().delete()
        self.assertContains(self.client.get(url), 'В каталоге пока нет вариантов')

    def test_russian_status_history_and_fixed_ruble_ui_allow_manual_fallback(self):
        work = self.upload()
        detail = reverse('worksheet_reviews:detail', args=[work.pk])
        page = self.client.get(detail)
        self.assertContains(page, 'Распознавание недоступно')
        self.assertNotContains(page, '>unavailable<')
        self.assertContains(page, 'type="hidden" name="budget_currency" value="RUB"')
        self.confirm(work)
        page = self.client.get(detail)
        self.assertContains(page, 'Текущая редакция сверена и подтверждена')
        self.assertContains(page, 'Состав путешественников')
        for raw in ('>party<', '>unknown<', '>manual_fallback<', '>legacy_schema_default<'):
            self.assertNotContains(page, raw)

    def test_foreign_budget_can_be_corrected_to_rubles_through_visible_fields(self):
        fields = demo_fields(); fields['budget'].update(value='1000 USD', currency='USD', state='conflict')
        work = self.upload(fields=fields)
        page = self.client.get(reverse('worksheet_reviews:detail', args=[work.pk]))
        self.assertEqual(page.context['field_form'].initial['budget_currency'], 'RUB')
        self.assertContains(page, 'Уточните сумму в рублях без автоматической конвертации')
        self.confirm(work)
        self.assertEqual(current_revision(work).fields['budget']['currency'], 'RUB')
        self.assertEqual(current_revision(work).fields['budget']['value'], '200000')

    def test_pdf_failure_is_visible_and_no_release_or_history_is_saved(self):
        work = self.upload(); self.confirm(work); preview = self.prepare(work)
        selection = preview.context['travel_request']
        approved = self.client.post(reverse('presentations:approve', args=[selection.pk]), {'content_hash': preview.context['content_hash']})
        before = Version.objects.count()
        with patch('presentations.services.render_pdf', side_effect=RuntimeError('synthetic renderer failure')):
            page = self.client.post(reverse('presentations:publish', args=[selection.pk]), {'approval_id': approved.context['approval'].pk})
        self.assertContains(page, 'Выпуск не создан', status_code=503)
        self.assertEqual(Release.objects.count(), 0)
        self.assertEqual(Version.objects.count(), before)

    def test_csrf_failure_is_russian_and_does_not_change_records(self):
        client = Client(enforce_csrf_checks=True); client.force_login(self.owner)
        page = client.post(reverse('worksheet_reviews:upload'), {'work_code': 'UI-CSRF'})
        self.assertContains(page, 'Действие не разрешено', status_code=403)
        self.assertNotContains(page, 'CSRF verification failed', status_code=403)
        self.assertEqual(WorksheetReview.objects.count(), 0)

    def test_demo_seed_is_idempotent_and_preserves_manager_edits(self):
        with patch('worksheet_reviews.ocr_services.recognize_card_photo', return_value=(_empty_fields('manual_fallback', 'Синтетика'), {'status': 'unavailable'})):
            call_command('seed_manager_demo', username=self.owner.username, stdout=StringIO())
        self.assertEqual(WorksheetReview.objects.count(), 3)
        self.assertEqual(Release.objects.count(), 1)
        work = WorksheetReview.objects.get(work_code='DEMO-REVIEW')
        work.expert_overview = 'Сохранённая правка менеджера'; work.save()
        call_command('seed_manager_demo', username=self.owner.username, stdout=StringIO())
        self.assertEqual(WorksheetReview.objects.count(), 3)
        self.assertEqual(Release.objects.count(), 1)
        work.refresh_from_db(); self.assertEqual(work.expert_overview, 'Сохранённая правка менеджера')
        self.assertIsNone(current_revision(work).confirmed_at)
        self.assertContains(self.client.get('/'), 'DEMO-HISTORY')

    def test_demo_seed_requires_existing_active_owner(self):
        with self.assertRaises(CommandError):
            call_command('seed_manager_demo', username='missing', stdout=StringIO())
        self.assertEqual(WorksheetReview.objects.count(), 0)
