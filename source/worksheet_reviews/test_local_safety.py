"""Tests for owner deletion, hidden-password account creation, and SQLite test-copy recovery."""
import io
import os
import sqlite3
import tempfile
import getpass
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from .models import HotelChoice, WorksheetFileRevision, WorksheetOCRRevision, WorksheetReview


class OwnerDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user('delete-owner', password='synthetic-password')
        cls.other = User.objects.create_user('delete-other', password='synthetic-password')
        cls.work = WorksheetReview.objects.create(
            owner=cls.owner, work_code='DEMO-DEL1', worksheet_bytes=b'synthetic worksheet bytes',
            worksheet_type='application/pdf', worksheet_sha256='a' * 64, current_source_version=2,
            expert_overview='Synthetic expert overview.',
        )
        cls.file_revision = WorksheetFileRevision.objects.create(
            review=cls.work, number=2, worksheet_bytes=b'synthetic replacement bytes',
            worksheet_type='application/pdf', worksheet_sha256='b' * 64, created_by=cls.owner,
        )
        cls.ocr_revision = WorksheetOCRRevision.objects.create(
            review=cls.work, number=1, source_sha256='a' * 64, fields={'wishes': {'value': 'synthetic'}},
            original_text='Synthetic OCR text.', metadata={'status': 'recognized'}, content_hash='c' * 64,
        )
        for position in range(1, 4):
            HotelChoice.objects.create(review=cls.work, position=position,
                                       name=f'Synthetic hotel {position}', reference_text='Synthetic only')

    def test_get_does_not_delete_and_wrong_identifier_cannot_delete(self):
        self.client.force_login(self.owner)
        url = reverse('worksheet_reviews:delete_confirm', args=[self.work.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertTrue(WorksheetReview.objects.filter(pk=self.work.pk).exists())
        response = self.client.post(url, {'confirm_id': 'not-the-work-id'})
        self.assertEqual(response.status_code, 422)
        self.assertTrue(WorksheetReview.objects.filter(pk=self.work.pk).exists())
        self.assertEqual(HotelChoice.objects.filter(review=self.work).count(), 3)

    def test_owner_must_pass_csrf_and_confirm_uuid_to_delete_work_and_related_choices(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        url = reverse('worksheet_reviews:delete_confirm', args=[self.work.pk])
        page = client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(client.post(url, {'confirm_id': str(self.work.pk)}).status_code, 403)
        self.assertTrue(WorksheetReview.objects.filter(pk=self.work.pk).exists())

        csrf = client.cookies['csrftoken'].value
        response = client.post(url, {'confirm_id': str(self.work.pk), 'csrfmiddlewaretoken': csrf},
                               HTTP_ORIGIN='http://testserver')
        self.assertEqual(response.status_code, 302)
        self.assertFalse(WorksheetReview.objects.filter(pk=self.work.pk).exists())
        self.assertEqual(HotelChoice.objects.count(), 0)
        self.assertFalse(WorksheetOCRRevision.objects.filter(pk=self.ocr_revision.pk).exists())
        self.assertFalse(WorksheetFileRevision.objects.filter(pk=self.file_revision.pk).exists())
        self.assertEqual(client.get(reverse('worksheet_reviews:sheet', args=[self.work.pk])).status_code, 404)

    def test_foreign_owner_cannot_view_or_delete_another_owners_work(self):
        client = Client()
        client.force_login(self.other)
        url = reverse('worksheet_reviews:delete_confirm', args=[self.work.pk])
        self.assertEqual(client.get(url).status_code, 404)
        self.assertEqual(client.post(url, {'confirm_id': str(self.work.pk)}).status_code, 404)
        self.assertTrue(WorksheetReview.objects.filter(pk=self.work.pk).exists())
        self.assertEqual(HotelChoice.objects.filter(review=self.work).count(), 3)
        self.assertTrue(WorksheetOCRRevision.objects.filter(pk=self.ocr_revision.pk).exists())
        self.assertTrue(WorksheetFileRevision.objects.filter(pk=self.file_revision.pk).exists())


class LocalOwnerAccountTests(TestCase):
    def test_create_private_owner_hides_password_and_never_prints_it(self):
        username = 'private-test-owner'
        synthetic_password = 'Synthetic!Only-Password-314'
        fake_tty = io.StringIO(username + '\n')
        fake_tty.isatty = lambda: True
        output = io.StringIO()
        with patch('worksheet_reviews.management.commands.create_private_owner.sys.stdin', fake_tty), \
             patch('worksheet_reviews.management.commands.create_private_owner.input', return_value=username), \
             patch('worksheet_reviews.management.commands.create_private_owner.getpass.getpass',
                   side_effect=[synthetic_password, synthetic_password]):
            call_command('create_private_owner', stdout=output)

        user = get_user_model().objects.get(username=username)
        self.assertTrue(user.check_password(synthetic_password))
        self.assertNotEqual(user.password, synthetic_password)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertNotIn(synthetic_password, output.getvalue())

    def test_noninteractive_mode_and_password_mismatch_create_no_account(self):
        username = 'private-never-created'
        fake_pipe = io.StringIO(username + '\n')
        fake_pipe.isatty = lambda: False
        with patch('worksheet_reviews.management.commands.create_private_owner.sys.stdin', fake_pipe), \
             patch('worksheet_reviews.management.commands.create_private_owner.input', return_value=username), \
             patch('worksheet_reviews.management.commands.create_private_owner.getpass.getpass') as get_password:
            with self.assertRaises(CommandError):
                call_command('create_private_owner', stdout=io.StringIO())
        get_password.assert_not_called()
        self.assertFalse(get_user_model().objects.filter(username=username).exists())

        fake_tty = io.StringIO(username + '\n')
        fake_tty.isatty = lambda: True
        with patch('worksheet_reviews.management.commands.create_private_owner.sys.stdin', fake_tty), \
             patch('worksheet_reviews.management.commands.create_private_owner.input', return_value=username), \
             patch('worksheet_reviews.management.commands.create_private_owner.getpass.getpass',
                   side_effect=['Synthetic!Only-Password-314', 'mismatch']):
            with self.assertRaises(CommandError):
                call_command('create_private_owner', stdout=io.StringIO())
        self.assertFalse(get_user_model().objects.filter(username=username).exists())

    def test_terminal_password_echo_fallback_is_rejected(self):
        username = 'private-unsafe-terminal'
        fake_tty = io.StringIO(username + '\n')
        fake_tty.isatty = lambda: True
        with patch('worksheet_reviews.management.commands.create_private_owner.sys.stdin', fake_tty), \
             patch('worksheet_reviews.management.commands.create_private_owner.input', return_value=username), \
             patch('worksheet_reviews.management.commands.create_private_owner.getpass.getpass',
                   side_effect=getpass.GetPassWarning('echo fallback')):
            with self.assertRaises(CommandError):
                call_command('create_private_owner', stdout=io.StringIO())
        self.assertFalse(get_user_model().objects.filter(username=username).exists())


class LocalBackupRestoreTests(TransactionTestCase):
    def setUp(self):
        owner = get_user_model().objects.create_user('backup-synthetic-owner')
        self.work = WorksheetReview.objects.create(
            owner=owner, work_code='DEMO-BACK1', worksheet_bytes=b'%PDF-synthetic-worksheet',
            worksheet_type='application/pdf', worksheet_sha256='b' * 64,
            expert_overview='Synthetic backup overview.',
        )
        for position in range(1, 4):
            HotelChoice.objects.create(review=self.work, position=position,
                                       name=f'Backup-only synthetic {position}',
                                       reference_text=f'https://example.invalid/{position}')

    def test_consistent_backup_restore_and_integrity_on_external_test_copy(self):
        with tempfile.TemporaryDirectory(prefix='travelhub-backup-test-') as directory:
            root = Path(directory)
            backup = root / 'synthetic-backup.sqlite3'
            restored = root / 'restored-test-copy.sqlite3'
            call_command('backup_local', destination=str(backup), stdout=io.StringIO())
            self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)

            call_command('restore_local', backup=str(backup), destination=str(restored), test_copy=True,
                         stdout=io.StringIO())
            self.assertEqual(os.stat(restored).st_mode & 0o777, 0o600)
            with sqlite3.connect(restored) as database:
                self.assertEqual(database.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                row = database.execute(
                    'SELECT work_code, worksheet_bytes, expert_overview FROM worksheet_reviews_worksheetreview WHERE id=?',
                    [self.work.pk.hex],
                ).fetchone()
                self.assertEqual(row, ('DEMO-BACK1', b'%PDF-synthetic-worksheet', 'Synthetic backup overview.'))
                hotels = database.execute(
                    'SELECT position, name, reference_text FROM worksheet_reviews_hotelchoice WHERE review_id=? ORDER BY position',
                    [self.work.pk.hex],
                ).fetchall()
                self.assertEqual(len(hotels), 3)
                self.assertEqual(hotels[0], (1, 'Backup-only synthetic 1', 'https://example.invalid/1'))

    def test_restore_requires_test_copy_and_never_overwrites_existing_destination(self):
        with tempfile.TemporaryDirectory(prefix='travelhub-restore-safety-') as directory:
            root = Path(directory)
            backup = root / 'backup.sqlite3'
            target = root / 'existing-copy.sqlite3'
            call_command('backup_local', destination=str(backup), stdout=io.StringIO())
            target.write_bytes(b'preserve this existing file')
            with self.assertRaises(CommandError):
                call_command('restore_local', backup=str(backup), destination=str(target),
                             stdout=io.StringIO())
            self.assertEqual(target.read_bytes(), b'preserve this existing file')

            with self.assertRaises(CommandError):
                call_command('restore_local', backup=str(backup), destination=str(target), test_copy=True,
                             stdout=io.StringIO())
            self.assertEqual(target.read_bytes(), b'preserve this existing file')

            new_target = root / 'new-copy.sqlite3'
            call_command('restore_local', backup=str(backup), destination=str(new_target), test_copy=True,
                         stdout=io.StringIO())
            with sqlite3.connect(new_target) as database:
                self.assertEqual(database.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_destination_created_during_snapshot_is_not_clobbered(self):
        import worksheet_reviews.local_backup as local_backup

        with tempfile.TemporaryDirectory(prefix='travelhub-restore-race-') as directory:
            root = Path(directory)
            backup = root / 'backup.sqlite3'
            target = root / 'initially-absent-copy.sqlite3'
            sentinel = b'concurrent file must survive'
            call_command('backup_local', destination=str(backup), stdout=io.StringIO())
            original = local_backup._snapshot_to_temporary

            def create_target_during_snapshot(source, destination):
                temporary = original(source, destination)
                target.write_bytes(sentinel)
                return temporary

            with patch.object(local_backup, '_snapshot_to_temporary', side_effect=create_target_during_snapshot):
                with self.assertRaises(CommandError):
                    call_command('restore_local', backup=str(backup), destination=str(target), test_copy=True,
                                 stdout=io.StringIO())
            self.assertEqual(target.read_bytes(), sentinel)

    def test_wrong_named_tables_and_incompatible_schema_are_rejected_before_target_changes(self):
        from worksheet_reviews.local_backup import validate_database

        with tempfile.TemporaryDirectory(prefix='travelhub-incompatible-schema-') as directory:
            root = Path(directory)
            invalid_backup = root / 'wrong-schema.sqlite3'
            target = root / 'existing-copy.sqlite3'
            with sqlite3.connect(invalid_backup) as database:
                for table in ('django_migrations', 'auth_user',
                              'worksheet_reviews_worksheetreview', 'worksheet_reviews_hotelchoice'):
                    database.execute(f'CREATE TABLE "{table}" (wrong_column TEXT)')
            target.write_bytes(b'keep destination on schema failure')

            with self.assertRaises(CommandError):
                validate_database(invalid_backup)
            with self.assertRaises(CommandError):
                call_command('restore_local', backup=str(invalid_backup), destination=str(target),
                             test_copy=True, stdout=io.StringIO())
            self.assertEqual(target.read_bytes(), b'keep destination on schema failure')

    def test_backup_cannot_be_written_inside_repository_or_over_existing_file(self):
        project_backup = Path(__file__).resolve().parents[1] / '.must-not-create-backup.sqlite3'
        with self.assertRaises(CommandError):
            call_command('backup_local', destination=str(project_backup), stdout=io.StringIO())
        self.assertFalse(project_backup.exists())

        with tempfile.TemporaryDirectory(prefix='travelhub-backup-exists-') as directory:
            existing = Path(directory) / 'existing.sqlite3'
            existing.write_bytes(b'keep existing bytes')
            with self.assertRaises(CommandError):
                call_command('backup_local', destination=str(existing), stdout=io.StringIO())
            self.assertEqual(existing.read_bytes(), b'keep existing bytes')
