"""Synthetic Telegram updates, in-memory transport, real existing manager services; no network."""
import copy
from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import struct
import zlib
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.db import transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from PIL import Image

from inquiries.manager import prepare
from inquiries.models import Inquiry, SelectionLink, Version
from inquiries.services import source_file
from presentations.models import Hotel
from presentations.services import approve_request, build_snapshot, snapshot_hash, publish_request
from travelcards.ocr import russian_tesseract_available
from worksheet_reviews.models import WorksheetReview
from worksheet_reviews.ocr_services import current_revision, save_fields
from worksheet_reviews.test_ocr_integration import known_fields
from .adapter import Binding, CHUNK_BYTES, GatewayError, MAX_FILE_BYTES, accept
from .models import UpdateReceipt


class MemoryTransport:
    """Test-only fixture, NOT a working Telegram client."""
    def __init__(self, content):
        self.content, self.downloads, self.messages = content, [], []
        self.before_download = None
        self.fail_download = self.fail_reply = False
        self.closed = False

    def iter_file(self, file_id, *, max_bytes, chunk_size, timeout_seconds):
        self.downloads.append(file_id)
        try:
            if self.before_download:
                self.before_download()
            if self.fail_download:
                raise TimeoutError('SYNTHETIC_TOKEN_MUST_NOT_LEAK')
            for offset in range(0, len(self.content), chunk_size):
                yield self.content[offset:offset+chunk_size]
        finally:
            self.closed = True

    def send_message(self, chat_id, text, *, timeout_seconds):
        self.messages.append((chat_id, text))
        if self.fail_reply:
            raise TimeoutError('SYNTHETIC_TOKEN_MUST_NOT_LEAK')


class TelegramGatewayTests(TransactionTestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('synthetic-telegram-owner')
        self.other = get_user_model().objects.create_user('synthetic-other-owner')
        self.png = (Path(settings.BASE_DIR)/'docs/fixtures/synthetic-travel-card.png').read_bytes()
        self.transport = MemoryTransport(self.png)
        self.binding = Binding('synthetic-bot', self.owner.pk, 100001, 100001,
                               'SYNTHETIC_WEBHOOK_SECRET_FOR_TESTS_ONLY', self.transport, 'http://127.0.0.1:8000')
        self.update = {'update_id': 101, 'message': {'message_id': 501, 'from': {'id': 100001, 'is_bot': False, 'first_name': 'Synthetic'},
                       'chat': {'id': 100001, 'type': 'private'},
                       'document': {'file_id': 'synthetic_file_1', 'mime_type': 'image/png', 'file_size': len(self.png), 'file_name': '../../ignored.png'},
                       'caption': 'UNTRUSTED_IGNORED_CAPTION'}}
        self.client = Client(enforce_csrf_checks=True)
        self.url = reverse('telegram-webhook')
        self.settings_scope = override_settings(TELEGRAM_GATEWAY=self.binding)
        self.settings_scope.enable(); self.addCleanup(self.settings_scope.disable)
        self.network = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden in this suite'))
        self.network.start(); self.addCleanup(self.network.stop)

    def post(self, update=None, secret=None, **kwargs):
        return self.client.post(self.url, json.dumps(self.update if update is None else update), content_type='application/json',
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=self.binding.webhook_secret if secret is None else secret, **kwargs)

    def test_image_enters_existing_private_review_without_automatic_confirmation(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        data = response.json(); work = WorksheetReview.objects.get()
        self.assertEqual(bytes(work.worksheet_bytes), self.png)
        self.assertEqual(work.owner, self.owner)
        self.assertFalse(work.ocr_revisions.exists())  # OCR starts on existing review page.
        self.assertIn('Лист ожидает сверки', data['text'])
        self.assertIn('U-ON: не синхронизировано', self.transport.messages[0][1])
        self.assertFalse(Inquiry.objects.exists())
        self.assertEqual(self.client.get(data['review_path']).status_code, 302)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(data['review_path']).status_code, 404)
        self.assertEqual(self.client.get(reverse('worksheet_reviews:sheet', args=[work.pk])).status_code, 404)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('worksheet_reviews:sheet', args=[work.pk])).content, self.png)

    def test_repeat_neither_downloads_nor_replies_nor_creates_second_work(self):
        first = self.post(); second = self.post()
        self.assertEqual(first.json()['review_path'], second.json()['review_path'])
        self.assertTrue(second.json()['repeated'])
        self.assertEqual((WorksheetReview.objects.count(), UpdateReceipt.objects.count(), len(self.transport.downloads), len(self.transport.messages)), (1, 1, 1, 1))

    def test_changed_update_and_same_message_under_new_update_are_conflicts(self):
        self.post()
        changed = copy.deepcopy(self.update); changed['message']['document']['file_id'] = 'other_file'
        self.assertEqual(self.post(changed).status_code, 409)
        changed = copy.deepcopy(self.update); changed['update_id'] = 102
        self.assertEqual(self.post(changed).status_code, 409)
        self.assertEqual(len(self.transport.downloads), 1)
        self.assertEqual(WorksheetReview.objects.count(), 1)

    def test_overlapping_delivery_reuses_first_committed_work(self):
        def overlapping():
            self.transport.before_download = None
            accept(self.update, self.binding)
        self.transport.before_download = overlapping
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(WorksheetReview.objects.count(), 1)
        self.assertEqual(UpdateReceipt.objects.count(), 1)
        self.assertEqual(len(self.transport.messages), 1)

    def test_deleted_work_stays_deleted_on_replay(self):
        self.post(); WorksheetReview.objects.get().delete()
        self.assertEqual(self.post().json()['status'], 'work_deleted')
        self.assertEqual(WorksheetReview.objects.count(), 0)
        self.assertEqual(len(self.transport.messages), 1)

    def test_disabled_and_invalid_configuration_never_download(self):
        for binding in (None, replace(self.binding, webhook_secret='short'), replace(self.binding, review_origin='https://example.invalid/?token=bad')):
            with self.subTest(binding=type(binding).__name__), override_settings(TELEGRAM_GATEWAY=binding):
                self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.transport.downloads, [])

    def test_secret_header_required_even_with_owner_session(self):
        self.client.force_login(self.owner)
        for value in ('', 'wrong', 'секрет'):
            self.assertEqual(self.post(secret=value).status_code, 403)
        self.assertEqual(self.transport.downloads, [])
        self.assertNotIn(self.binding.webhook_secret, repr(self.binding))

    def test_untrusted_users_groups_and_bots_are_denied_before_download(self):
        for mutation in ('user', 'chat', 'group', 'bot', 'boolean_id'):
            update = copy.deepcopy(self.update); message = update['message']
            if mutation == 'user': message['from']['id'] = 100002
            if mutation == 'chat': message['chat']['id'] = 100002
            if mutation == 'group': message['chat']['type'] = 'group'
            if mutation == 'bot': message['from']['is_bot'] = True
            if mutation == 'boolean_id': message['from']['id'] = True
            with self.subTest(mutation=mutation): self.assertEqual(self.post(update).status_code, 403)
        self.assertEqual(self.transport.downloads, [])

    def test_inactive_owner_and_deactivation_during_download_block_storage(self):
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(self.transport.downloads, [])
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=True)
        self.transport.before_download = lambda: get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertEqual(self.post().status_code, 403)
        self.assertFalse(WorksheetReview.objects.exists())
        self.assertFalse(UpdateReceipt.objects.exists())
        self.assertEqual(self.transport.messages, [])

    def test_deactivation_before_reply_preserves_work_but_suppresses_output(self):
        from .adapter import result as original_result
        def deactivate(receipt, binding):
            value = original_result(receipt, binding)
            get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
            return value
        with patch('telegram_gateway.adapter.result', side_effect=deactivate):
            self.assertEqual(self.post().status_code, 403)
        self.assertEqual(WorksheetReview.objects.count(), 1)
        self.assertEqual(self.transport.messages, [])
        self.assertEqual(self.post().status_code, 403)

    def test_inactive_owner_cannot_replay_or_get_private_status(self):
        self.post(); get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(len(self.transport.messages), 1)

    def test_changed_owner_binding_cannot_reuse_receipt(self):
        self.post()
        with override_settings(TELEGRAM_GATEWAY=replace(self.binding, owner_id=self.other.pk)):
            self.assertEqual(self.post().status_code, 403)
        self.assertEqual(WorksheetReview.objects.count(), 1)

    def test_unsupported_updates_forwards_albums_and_urls_never_fetch(self):
        cases = [{'update_id': 1, 'edited_message': self.update['message']}, [], {'update_id': True}]
        for field in ('forward_origin', 'media_group_id', 'via_bot'):
            update = copy.deepcopy(self.update); update['message'][field] = 'synthetic'; cases.append(update)
        update = copy.deepcopy(self.update); update['message']['document']['file_id'] = 'https://example.invalid/not-fetched'; cases.append(update)
        for update in cases:
            with self.subTest(case=type(update).__name__): self.assertEqual(self.post(update).status_code, 400)
        self.assertEqual(self.transport.downloads, [])

    def test_request_size_json_duplicate_keys_and_method_type(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        headers = {'HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN': self.binding.webhook_secret}
        self.assertEqual(self.client.post(self.url, 'x'*65537, content_type='application/json', **headers).status_code, 413)
        self.assertEqual(self.client.post(self.url, '{"update_id":1,"update_id":2}', content_type='application/json', **headers).status_code, 400)
        self.assertEqual(self.client.post(self.url, '{', content_type='application/json', **headers).status_code, 400)
        self.assertEqual(self.client.post(self.url, '{}', content_type='text/plain', **headers).status_code, 415)
        self.assertEqual(self.transport.downloads, [])

    def test_declared_size_and_type_checked_before_download(self):
        for field, value in [('file_size', MAX_FILE_BYTES+1), ('file_size', -1), ('mime_type', 'application/pdf')]:
            update = copy.deepcopy(self.update); update['message']['document'][field] = value
            self.assertEqual(self.post(update).status_code, 400)
        self.assertEqual(self.transport.downloads, [])

    def test_actual_oversize_stream_is_closed_and_rejected_once(self):
        self.transport.content = b'x'*(MAX_FILE_BYTES+1)
        response = self.post()
        self.assertEqual(response.json()['status'], 'image_rejected')
        self.assertTrue(self.transport.closed)
        self.assertFalse(WorksheetReview.objects.exists())
        self.assertEqual(self.post().json()['status'], 'image_rejected')
        self.assertEqual(len(self.transport.downloads), 1)
        self.assertEqual(len(self.transport.messages), 1)

    def test_corrupt_and_mime_mismatched_images_are_not_saved(self):
        self.transport.content = b'not-an-image'
        self.assertEqual(self.post().json()['status'], 'image_rejected')
        update = copy.deepcopy(self.update); update['update_id'] += 1; update['message']['message_id'] += 1
        self.transport.content = self.png; update['message']['document']['mime_type'] = 'image/jpeg'
        self.assertEqual(self.post(update).json()['status'], 'image_rejected')
        self.assertFalse(WorksheetReview.objects.exists())

    def test_pixel_bomb_header_is_a_permanent_rejection_not_retryable_download_error(self):
        content = bytearray(self.png)
        content[16:24] = struct.pack('>II', 20000, 20000)
        content[29:33] = struct.pack('>I', zlib.crc32(content[12:29]))
        self.transport.content = bytes(content)
        self.assertEqual(self.post().json()['status'], 'image_rejected')
        self.assertFalse(WorksheetReview.objects.exists())
        self.assertEqual(self.post().json()['status'], 'image_rejected')
        self.assertEqual(len(self.transport.downloads), 1)

    def test_photo_uses_largest_resolution_and_preserves_received_jpeg(self):
        buffer = io.BytesIO(); Image.new('RGB', (30, 20), 'white').save(buffer, format='JPEG')
        self.transport.content = buffer.getvalue()
        update = copy.deepcopy(self.update); message = update['message']; message.pop('document')
        message['photo'] = [{'width': 10, 'height': 10, 'file_id': 'small'}, {'width': 30, 'height': 20, 'file_id': 'large'}]
        self.assertEqual(self.post(update).status_code, 200)
        self.assertEqual(self.transport.downloads, ['large'])
        self.assertEqual(bytes(WorksheetReview.objects.get().worksheet_bytes), buffer.getvalue())

    def test_download_timeout_is_retryable_without_secret_leak_or_partial_work(self):
        self.transport.fail_download = True
        response = self.post(); self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'SYNTHETIC_TOKEN_MUST_NOT_LEAK', response.content)
        self.assertFalse(UpdateReceipt.objects.exists()); self.assertFalse(WorksheetReview.objects.exists())
        self.transport.fail_download = False
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(WorksheetReview.objects.count(), 1)

    def test_unknown_reply_is_not_resent_but_saved_work_remains_accessible(self):
        self.transport.fail_reply = True
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['reply_state'], 'unknown')
        self.assertNotIn(b'SYNTHETIC_TOKEN_MUST_NOT_LEAK', response.content)
        self.assertEqual(self.post().json()['reply_state'], 'unknown')
        self.assertEqual(len(self.transport.messages), 1)
        self.assertEqual(WorksheetReview.objects.count(), 1)

    def test_interrupted_reply_attempt_is_never_automatically_repeated(self):
        self.post(); UpdateReceipt.objects.update(reply_state='attempted')
        self.assertEqual(self.post().json()['reply_state'], 'attempted')
        self.assertEqual(len(self.transport.messages), 1)

    def test_creation_failure_rolls_back_receipt_and_work(self):
        with patch('telegram_gateway.adapter.create_review', side_effect=RuntimeError('SYNTHETIC_TOKEN_MUST_NOT_LEAK')):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'SYNTHETIC_TOKEN_MUST_NOT_LEAK', response.content)
        self.assertFalse(UpdateReceipt.objects.exists()); self.assertFalse(WorksheetReview.objects.exists())
        self.assertEqual(self.transport.messages, [])
        self.assertEqual(self.post().status_code, 200)

    def test_no_raw_identifiers_caption_or_secrets_persisted_in_receipt(self):
        self.post(); receipt = UpdateReceipt.objects.values().get()
        serialized = str(receipt)
        for forbidden in ('synthetic_file_1', 'UNTRUSTED_IGNORED_CAPTION', self.binding.webhook_secret, '../../ignored.png'):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(self.post().headers['Cache-Control'], 'no-store')

    def test_service_rejects_outer_transaction_before_transport(self):
        with transaction.atomic(), self.assertRaises(GatewayError): accept(self.update, self.binding)
        self.assertEqual(self.transport.downloads, [])

    def test_backup_restores_receipt_and_exact_original_and_rejects_missing_ledger(self):
        self.post()
        with tempfile.TemporaryDirectory(prefix='izzi-telegram-backup-') as directory:
            backup, restored = (Path(directory)/name for name in ('backup.sqlite3', 'restored.sqlite3'))
            call_command('backup_local', destination=str(backup), stdout=io.StringIO())
            call_command('restore_local', backup=str(backup), destination=str(restored), test_copy=True, stdout=io.StringIO())
            with sqlite3.connect(restored) as db:
                self.assertEqual(db.execute('SELECT update_id, reply_state FROM telegram_gateway_updatereceipt').fetchone(), (101, 'sent'))
                self.assertEqual(db.execute('SELECT worksheet_bytes FROM worksheet_reviews_worksheetreview').fetchone()[0], self.png)
            with sqlite3.connect(backup) as db: db.execute('DROP TABLE telegram_gateway_updatereceipt')
            with self.assertRaises(CommandError):
                call_command('restore_local', backup=str(backup), destination=str(Path(directory)/'rejected.sqlite3'), test_copy=True, stdout=io.StringIO())
            self.assertFalse((Path(directory)/'rejected.sqlite3').exists())

    def test_fake_telegram_to_real_ocr_manual_review_two_releases_same_inquiry(self):
        if not russian_tesseract_available(): self.skipTest('Real local rus model required for this integration test.')
        response = self.post(); work = WorksheetReview.objects.get()
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(response.json()['review_path']), 'Текст распознан')
        revision = current_revision(work)
        hotels = [Hotel.objects.create(name=f'Synthetic TG {i}', summary='Синтетический пример', pros='Пример', limitations='Пример', source_reference='synthetic') for i in range(2)]
        with self.assertRaises(ValidationError):
            prepare(actor=self.owner, work_id=work.pk, expected_hash=revision.content_hash, hotel_ids=[hotels[0].pk])
        fields = known_fields('Синтетическое ручное уточнение'); fields['budget']['currency'] = 'RUB'
        revision = save_fields(work, self.owner, revision.number, revision.content_hash, fields, confirm=True)
        releases = []
        for hotel in hotels:
            selection = prepare(actor=self.owner, work_id=work.pk, expected_hash=revision.content_hash, hotel_ids=[hotel.pk], traveller_label='Вымышленная поездка Telegram')
            approval = approve_request(selection, self.owner, snapshot_hash(build_snapshot(selection)))
            releases.append(publish_request(selection, self.owner, approval.pk)[0])
        self.assertEqual(Inquiry.objects.count(), 1)
        self.assertEqual(SelectionLink.objects.count(), 2)
        self.assertTrue(all(bytes(release.pdf_bytes).startswith(b'%PDF-') for release in releases))
        versions = Version.objects.exclude(snapshot__release=None)
        self.assertTrue(all(v.inquiry.sync_status == 'not_synchronized' for v in versions))
        self.assertEqual(source_file(actor=self.owner, version_id=versions.first().pk)[0], self.png)
        self.assertIn('Лист сверен', self.post().json()['text'])
        self.assertEqual(len(self.transport.messages), 1)
