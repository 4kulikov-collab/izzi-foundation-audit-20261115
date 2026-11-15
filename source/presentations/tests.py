"""Component acceptance for the synthetic B prototype, not full product acceptance."""
import copy
import hashlib
import io
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader

from .models import Approval, Hotel, Release, TravelRequest
from .rendering import deny_resource_fetch
from .services import approve_request, build_snapshot, publish_request, snapshot_hash


class PresentationPrototypeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.owner = users.objects.create_user('demo-owner', password='synthetic-test-password')
        cls.other = users.objects.create_user('demo-other', password='synthetic-other-password')
        cls.hotels = [Hotel.objects.create(
            name=f'Тестовый отель {i}', summary=f'Тестовое описание варианта {i}',
            pros='Спокойная атмосфера\nПример удобного размещения',
            limitations='Сведения только для теста', source_reference='Синтетический набор B',
            internal_notes='HOTEL_PRIVATE_CANARY') for i in range(1, 4)]
        cls.request = TravelRequest.objects.create(
            owner=cls.owner, title='Тестовая семейная поездка',
            traveller_summary='Двое взрослых — вымышленный запрос',
            requirements='Спокойный отдых', internal_notes='REQUEST_PRIVATE_CANARY')
        cls.request.selected_hotels.set(cls.hotels)
        from worksheet_reviews.services import create_review
        from inquiries.services import preview, attach
        work = create_review(owner=cls.owner, work_code='SYNTH-PRESENTATION', worksheet_bytes=b'synthetic source', worksheet_type='image/png', hotel_choices=[])
        snapshot = preview(actor=cls.owner, work_id=work.pk, selection_id=cls.request.pk)
        attach(actor=cls.owner, key='fixture-link', work_id=work.pk, selection_id=cls.request.pk, expected_hash=snapshot['digest'], traveller_label='Synthetic')

    def approve(self):
        self.request.refresh_from_db()
        return approve_request(self.request, self.owner, snapshot_hash(build_snapshot(self.request)))

    def publish(self):
        approval = self.approve()
        release, token = publish_request(self.request, self.owner, approval.pk)
        return release, token

    def grant(self, token, enforce_csrf=False):
        client = Client(enforce_csrf_checks=enforce_csrf)
        url = reverse('presentations:share', args=[token])
        gate = client.get(url)
        self.assertEqual(gate.status_code, 200)
        if enforce_csrf:
            csrf = client.cookies['csrftoken'].value
            response = client.post(url, {'csrfmiddlewaretoken': csrf})
        else:
            response = client.post(url)
        self.assertEqual(response.status_code, 302)
        return client

    def test_T23_catalog_changes_preserve_published_html_and_pdf(self):
        release, token = self.publish()
        before = bytes(release.pdf_bytes)
        original = copy.deepcopy(release.snapshot)
        client = self.grant(token)
        html = client.get(reverse('presentations:share', args=[token])).content
        Hotel.objects.filter(pk=self.hotels[0].pk).update(name='CHANGED_AFTER_RELEASE')
        self.request.title = 'CHANGED_REQUEST'
        self.request.save()
        release.refresh_from_db()
        self.assertEqual(release.snapshot, original)
        self.assertEqual(bytes(release.pdf_bytes), before)
        self.assertEqual(client.get(reverse('presentations:share', args=[token])).content, html)
        self.assertNotIn(b'CHANGED_AFTER_RELEASE', html)
        self.assertNotIn(b'CHANGED_REQUEST', html)

    def test_template_revision_blocks_pending_old_approval_without_changing_release(self):
        old_snapshot = build_snapshot(self.request)
        old_snapshot['template_version'] = 'prototype-b-2'
        with patch('presentations.services.build_snapshot', return_value=old_snapshot):
            old_approval = approve_request(self.request, self.owner, snapshot_hash(old_snapshot))
            release, token = publish_request(self.request, self.owner, old_approval.pk)
            pending = approve_request(self.request, self.owner, snapshot_hash(old_snapshot))
        old_html, old_pdf = release.html_content, bytes(release.pdf_bytes)
        self.assertEqual(build_snapshot(self.request)['template_version'], 'cards:1')
        with self.assertRaises(ValidationError):
            publish_request(self.request, self.owner, pending.pk)
        client = self.grant(token)
        self.assertEqual(client.get(reverse('presentations:share', args=[token])).content.decode(), old_html)
        self.assertEqual(client.get(reverse('presentations:share_pdf', args=[token])).content, old_pdf)
        release.refresh_from_db()
        self.assertEqual(release.snapshot['template_version'], 'prototype-b-2')
        self.assertEqual(Release.objects.count(), 1)

    def test_T34_snapshot_whitelist_excludes_internal_fields(self):
        snapshot = build_snapshot(self.request)
        serialized = str(snapshot)
        for private in ('HOTEL_PRIVATE_CANARY', 'REQUEST_PRIVATE_CANARY', 'internal_notes', 'password'):
            self.assertNotIn(private, serialized)
        self.assertTrue(snapshot['synthetic'])
        self.assertEqual(len(snapshot['hotels']), 3)

    def test_web_and_actual_pdf_share_snapshot_and_no_private_content(self):
        release, token = self.publish()
        pdf = bytes(release.pdf_bytes)
        self.assertTrue(pdf.startswith(b'%PDF-'))
        text = '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
        html = self.grant(token).get(reverse('presentations:share', args=[token])).content.decode()
        for value in [self.request.title] + [h.name for h in self.hotels]:
            self.assertIn(value, text)
            self.assertIn(value, html)
        for private in ['HOTEL_PRIVATE_CANARY', 'REQUEST_PRIVATE_CANARY']:
            self.assertNotIn(private, text)
            self.assertNotIn(private, html)
        self.assertEqual(release.pdf_hash, hashlib.sha256(pdf).hexdigest())
        self.assertEqual(release.content_hash, snapshot_hash(release.snapshot))

    def test_T34_neutral_link_preview_never_exposes_snapshot(self):
        _, token = self.publish()
        for ua in ['TelegramBot', 'facebookexternalhit', 'ordinary-browser']:
            response = Client().get(reverse('presentations:share', args=[token]), HTTP_USER_AGENT=ua)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, self.request.title)
            for hotel in self.hotels:
                self.assertNotContains(response, hotel.name)
            self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertIn('noindex', response.headers['X-Robots-Tag'])
            self.assertEqual(response.headers['Referrer-Policy'], 'same-origin')

    def test_T33_revoke_blocks_html_and_pdf_even_with_existing_grant(self):
        release, token = self.publish()
        client = self.grant(token)
        pdf_url = reverse('presentations:share_pdf', args=[token])
        self.assertEqual(client.get(pdf_url).status_code, 200)
        self.client.force_login(self.owner)
        self.assertIn(self.client.post(reverse('presentations:revoke', args=[release.pk])).status_code, (200, 302))
        self.assertEqual(client.get(reverse('presentations:share', args=[token])).status_code, 404)
        self.assertEqual(client.get(pdf_url).status_code, 404)

    def test_T33_expired_link_denies_all_routes(self):
        release, token = self.publish()
        client = self.grant(token)
        Release.objects.filter(pk=release.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(client.get(reverse('presentations:share', args=[token])).status_code, 404)
        self.assertEqual(client.get(reverse('presentations:share_pdf', args=[token])).status_code, 404)

    def test_pdf_requires_explicit_grant_and_grants_are_release_scoped(self):
        first, first_token = self.publish()
        second, second_token = self.publish()
        client = self.grant(first_token)
        self.assertEqual(Client().get(reverse('presentations:share_pdf', args=[first_token])).status_code, 404)
        self.assertEqual(client.get(reverse('presentations:share_pdf', args=[second_token])).status_code, 404)

    def test_T25_foreign_manager_cannot_view_edit_approve_publish_or_revoke(self):
        release, _ = self.publish()
        self.client.force_login(self.other)
        for name in ['request_edit', 'preview']:
            self.assertEqual(self.client.get(reverse('presentations:' + name, args=[self.request.pk])).status_code, 404)
        for name in ['request_edit', 'approve', 'publish']:
            self.assertEqual(self.client.post(reverse('presentations:' + name, args=[self.request.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('presentations:revoke', args=[release.pk])).status_code, 404)

    def test_T26_disabling_owner_invalidates_public_links(self):
        _, token = self.publish()
        client = self.grant(token)
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertEqual(client.get(reverse('presentations:share', args=[token])).status_code, 404)
        self.assertEqual(client.get(reverse('presentations:share_pdf', args=[token])).status_code, 404)

    def test_T35_stale_approval_rejects_changed_catalog(self):
        approval = self.approve()
        Hotel.objects.filter(pk=self.hotels[0].pk).update(summary='Новая версия содержания')
        with self.assertRaises(ValidationError):
            publish_request(self.request, self.owner, approval.pk)
        self.assertEqual(Release.objects.count(), 0)

    def test_T35_approval_hash_cannot_be_forged(self):
        with self.assertRaises(ValidationError):
            approve_request(self.request, self.owner, '0' * 64)
        self.assertEqual(Approval.objects.count(), 0)

    def test_malformed_approval_hash_returns_conflict_instead_of_server_error(self):
        self.client.force_login(self.owner)
        for value in ('я', 'a' * 63, 'a' * 65, ''):
            with self.subTest(value=value):
                response = self.client.post(reverse('presentations:approve', args=[self.request.pk]),
                                            {'content_hash': value})
                self.assertEqual(response.status_code, 409)
        self.assertEqual(Approval.objects.count(), 0)

    def test_browser_same_origin_form_succeeds_and_foreign_origin_is_denied(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        client.get(reverse('presentations:preview', args=[self.request.pk]))
        data = {'content_hash': snapshot_hash(build_snapshot(self.request)),
                'csrfmiddlewaretoken': client.cookies['csrftoken'].value}
        url = reverse('presentations:approve', args=[self.request.pk])
        self.assertEqual(client.post(url, data, HTTP_ORIGIN='http://testserver').status_code, 200)
        for origin in ('null', 'https://foreign.invalid'):
            self.assertEqual(client.post(url, data, HTTP_ORIGIN=origin).status_code, 403)

    def test_pdf_resource_loader_refuses_network_and_local_files(self):
        for url in ('https://example.invalid/photo.png', 'http://127.0.0.1/private',
                    'file:///etc/passwd', 'data:text/plain,synthetic'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                deny_resource_fetch(url)

    def test_repeat_publication_is_idempotent_and_token_not_stored_raw(self):
        approval = self.approve()
        first, token = publish_request(self.request, self.owner, approval.pk)
        second, repeated_token = publish_request(self.request, self.owner, approval.pk)
        self.assertEqual(first.pk, second.pk)
        self.assertIsNone(repeated_token)
        self.assertEqual(Release.objects.count(), 1)
        self.assertNotEqual(first.token_digest, token)
        self.assertEqual(first.token_digest, hashlib.sha256(token.encode()).hexdigest())

    def test_publication_and_grant_require_post_with_csrf(self):
        _, token = self.publish()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        for name in ['approve', 'publish']:
            url = reverse('presentations:' + name, args=[self.request.pk])
            self.assertEqual(client.get(url).status_code, 405)
            self.assertEqual(client.post(url).status_code, 403)
        url = reverse('presentations:share', args=[token])
        self.assertEqual(Client(enforce_csrf_checks=True).post(url).status_code, 403)
        self.assertEqual(self.grant(token, enforce_csrf=True).get(url).status_code, 200)

    def test_published_snapshot_rejects_application_level_mutation(self):
        release, _ = self.publish()
        release.snapshot = {'title': 'tampered'}
        with self.assertRaises(ValidationError):
            release.save()

    def test_untrusted_markup_is_escaped_in_release(self):
        self.request.requirements = '<script>alert("CANARY")</script>'
        self.request.save()
        release, token = self.publish()
        html = self.grant(token).get(reverse('presentations:share', args=[token])).content.decode()
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_unknown_link_does_not_reveal_request_data(self):
        self.assertEqual(self.client.get(reverse('presentations:share', args=['invalid'])).status_code, 404)

    def test_T36_renderer_failure_does_not_create_release(self):
        approval = self.approve()
        with patch('presentations.services.render_pdf', side_effect=RuntimeError('synthetic render failure')):
            with self.assertRaises(RuntimeError):
                publish_request(self.request, self.owner, approval.pk)
        self.assertEqual(Release.objects.count(), 0)

    def _assert_previous_release_preserved(self, release, token):
        self.assertEqual(Release.objects.count(), 1)
        stored = Release.objects.get(pk=release.pk)
        for field in ('snapshot', 'content_hash', 'html_content', 'pdf_hash',
                      'token_digest', 'expires_at', 'revoked_at'):
            self.assertEqual(getattr(stored, field), getattr(release, field))
        self.assertEqual(bytes(stored.pdf_bytes), bytes(release.pdf_bytes))
        client = self.grant(token)
        html = client.get(reverse('presentations:share', args=[token]))
        pdf = client.get(reverse('presentations:share_pdf', args=[token]))
        self.assertEqual(html.status_code, 200)
        self.assertEqual(html.content, release.html_content.encode('utf-8'))
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.content, bytes(release.pdf_bytes))

    def test_T35_publish_with_disappeared_selection_returns_to_editor(self):
        release, token = self.publish()
        approval = self.approve()
        self.request.selected_hotels.clear()
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('presentations:publish', args=[self.request.pk]),
            {'approval_id': str(approval.pk)},
        )
        self.assertRedirects(response, reverse('presentations:request_edit', args=[self.request.pk]))
        self.assertIn('no-store', response.headers['Cache-Control'])
        self._assert_previous_release_preserved(release, token)

    def test_T36_renderer_failure_with_disappeared_selection_returns_to_editor(self):
        release, token = self.publish()
        approval = self.approve()
        self.client.force_login(self.owner)

        def publish_then_selection_disappears(*args, **kwargs):
            try:
                return publish_request(*args, **kwargs)
            finally:
                # Model a change after the service rollback, before error rendering.
                self.request.selected_hotels.clear()

        with patch('presentations.views.publish_request', side_effect=publish_then_selection_disappears), patch(
                'presentations.services.render_pdf', side_effect=RuntimeError('synthetic render failure')) as renderer:
            response = self.client.post(
                reverse('presentations:publish', args=[self.request.pk]),
                {'approval_id': str(approval.pk)},
            )
        renderer.assert_called_once()
        self.assertFalse(self.request.selected_hotels.exists())
        self.assertRedirects(response, reverse('presentations:request_edit', args=[self.request.pk]))
        self.assertIn('no-store', response.headers['Cache-Control'])
        self._assert_previous_release_preserved(release, token)
