"""Revoke after snapshot; restored authorization must not become a working runtime."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import Client, TransactionTestCase, SimpleTestCase, override_settings
from django.urls import reverse
from inquiries.models import Office, ManagerOffice, DirectorOfficeGrant, Inquiry, InquiryOffice
from .local_backup import create_backup, restore_test_copy
from .voice_backup import create_voice_bundle, restore_voice_bundle, verify_restored_copy

SYNTHETIC_KEY = 'synthetic-restore-risk-signing-key-not-production'


@override_settings(SECRET_KEY=SYNTHETIC_KEY)
class RestoredAuthorizationTests(TransactionTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='izzi-revocation-restore-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        users = get_user_model()
        self.owner = users.objects.create_user('synthetic-old-owner', password='Synthetic-owner-only-123')
        self.director = users.objects.create_user('synthetic-old-director', password='Synthetic-director-only-456')
        self.operator = users.objects.create_user('synthetic-current-operator', is_superuser=True)
        office = Office.objects.create(name='Вымышленный офис восстановления')
        ManagerOffice.objects.create(user=self.owner, office=office)
        self.grant = DirectorOfficeGrant.objects.create(user=self.director, office=office)
        self.inquiry = Inquiry.objects.create(owner=self.owner, traveller_label='Вымышленная поездка до отзыва')
        InquiryOffice.objects.create(inquiry=self.inquiry, office=office, owner_at_binding=self.owner)
        self.client = Client(); self.client.force_login(self.director)
        self.session_key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertEqual(self.client.get(reverse('inquiries:director_list')).status_code, 200)

    def revoke(self):
        DirectorOfficeGrant.objects.filter(pk=self.grant.pk).update(is_active=False)
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertEqual(self.client.get(reverse('inquiries:director_list')).status_code, 403)
        Session.objects.all().delete()

    def probe(self, root):
        # Reuse ONLY this test's synthetic signing key to cover the worst case.
        (root/'django-secret-key').write_text(SYNTHETIC_KEY)
        code = '''import os,sys,json
os.environ.update(DJANGO_SETTINGS_MODULE='config.settings',TRAVELHUB_DATA_DIR=sys.argv[1])
import django
from django.core.exceptions import ImproperlyConfigured
try:
 django.setup()
except ImproperlyConfigured:
 print(json.dumps({'runtime_blocked':True}))
else:
 from django.test import Client
 from django.conf import settings
 c=Client();c.cookies[settings.SESSION_COOKIE_NAME]=sys.argv[2]
 old=c.get('/inquiries/director/').status_code
 fresh=c.login(username='synthetic-old-director',password='Synthetic-director-only-456')
 print(json.dumps({'runtime_blocked':False,'old_session_status':old,'fresh_director_login':fresh,'fresh_director_status':c.get('/inquiries/director/').status_code}))
'''
        result = subprocess.run([sys.executable, '-c', code, str(root), self.session_key],
            cwd=settings.BASE_DIR, env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, 'Synthetic runtime probe failed')
        return json.loads(result.stdout)

    def old_permissions_present(self, database):
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute('SELECT is_active FROM auth_user WHERE id=?',(self.owner.pk,)).fetchone()[0],1)
            self.assertEqual(db.execute('SELECT is_active FROM inquiries_directorofficegrant WHERE id=?',(self.grant.pk,)).fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM inquiries_inquiry').fetchone()[0],1)

    def test_sqlite_old_backup_cannot_restore_working_access_or_sessions(self):
        backup = self.root/'old.sqlite3'; create_backup(backup)
        with sqlite3.connect(backup) as db:
            self.assertGreater(db.execute('SELECT count(*) FROM django_session').fetchone()[0],0)
        self.revoke()
        target = self.root/'restored';target.mkdir()
        restore_test_copy(backup, target/'prototype.sqlite3')
        self.old_permissions_present(target/'prototype.sqlite3')
        self.assertEqual(self.probe(target), {'runtime_blocked':True})
        with sqlite3.connect(target/'prototype.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM django_session').fetchone()[0],0)
        self.assertFalse(DirectorOfficeGrant.objects.get(pk=self.grant.pk).is_active)
        self.assertFalse(get_user_model().objects.get(pk=self.owner.pk).is_active)

    def test_voice_bundle_old_grant_requires_reauthorization_even_without_sessions(self):
        archive=self.root/'old.zip'
        create_voice_bundle(archive,actor=self.operator,synthetic_only=True)
        self.revoke()
        entry=self.root/'restore.json'
        target=restore_voice_bundle(archive,entry,actor=self.operator,synthetic_only=True)
        self.assertEqual(verify_restored_copy(entry),target)
        self.old_permissions_present(target/'prototype.sqlite3')
        self.assertEqual(self.probe(target), {'runtime_blocked':True})
        self.assertFalse(DirectorOfficeGrant.objects.get(pk=self.grant.pk).is_active)

    def test_copying_only_restored_database_keeps_startup_blocked(self):
        import shutil
        backup=self.root/'old.sqlite3';create_backup(backup)
        target=self.root/'restored';target.mkdir()
        restore_test_copy(backup,target/'prototype.sqlite3')
        copied=self.root/'copied';copied.mkdir()
        shutil.copyfile(target/'prototype.sqlite3',copied/'prototype.sqlite3')
        self.assertEqual(self.probe(copied),{'runtime_blocked':True})

    def test_quarantine_failure_does_not_publish_sqlite_copy_or_leak_error(self):
        from unittest.mock import patch
        from django.core.management.base import CommandError
        backup=self.root/'old.sqlite3';create_backup(backup)
        target=self.root/'failed.sqlite3'
        with patch('worksheet_reviews.local_backup.quarantine_copy',side_effect=sqlite3.OperationalError('PRIVATE_SYNTHETIC_SENTINEL')):
            with self.assertRaises(CommandError) as error:restore_test_copy(backup,target)
        self.assertNotIn('PRIVATE_SYNTHETIC_SENTINEL',str(error.exception))
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob('.travelhub-snapshot-*')))

    def test_quarantine_failure_does_not_publish_voice_pointer(self):
        from unittest.mock import patch
        from django.core.management.base import CommandError
        archive=self.root/'old.zip';create_voice_bundle(archive,actor=self.operator,synthetic_only=True)
        entry=self.root/'failed.json'
        with patch('worksheet_reviews.voice_backup.quarantine_copy',side_effect=sqlite3.OperationalError('PRIVATE_SYNTHETIC_SENTINEL')):
            with self.assertRaises(CommandError) as error:restore_voice_bundle(archive,entry,actor=self.operator,synthetic_only=True)
        self.assertNotIn('PRIVATE_SYNTHETIC_SENTINEL',str(error.exception))
        self.assertFalse(entry.exists());self.assertFalse(list(self.root.glob('.izzi-restored-*')))

    def test_source_and_backup_unchanged_when_restoring_twice(self):
        import hashlib
        backup=self.root/'old.sqlite3';create_backup(backup)
        before=hashlib.sha256(backup.read_bytes()).hexdigest()
        sessions=Session.objects.count()
        for name in ('one','two'):
            target=self.root/name;target.mkdir()
            restore_test_copy(backup,target/'prototype.sqlite3')
            self.assertEqual(self.probe(target),{'runtime_blocked':True})
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(),before)
        self.assertEqual(Session.objects.count(),sessions)
        self.assertTrue(DirectorOfficeGrant.objects.get(pk=self.grant.pk).is_active)

    def test_removed_quarantine_is_not_verified_even_if_pointer_rehashed(self):
        import hashlib
        from django.core.management.base import CommandError
        archive=self.root/'old.zip';create_voice_bundle(archive,actor=self.operator,synthetic_only=True)
        entry=self.root/'restore.json';target=restore_voice_bundle(archive,entry,actor=self.operator,synthetic_only=True)
        from config.restore_guard import TABLE
        with sqlite3.connect(target/'prototype.sqlite3') as db:db.execute('DROP TABLE '+TABLE)
        pointer=json.loads(entry.read_text());raw=(target/'prototype.sqlite3').read_bytes()
        pointer['database']={'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw)}
        entry.write_text(json.dumps(pointer))
        with self.assertRaises(CommandError):verify_restored_copy(entry)

    def test_old_restore_receipt_is_rejected_not_claimed_safe(self):
        from django.core.management.base import CommandError
        archive=self.root/'old.zip';create_voice_bundle(archive,actor=self.operator,synthetic_only=True)
        entry=self.root/'restore.json';restore_voice_bundle(archive,entry,actor=self.operator,synthetic_only=True)
        pointer=json.loads(entry.read_text())
        pointer={k:pointer[k] for k in ('schema','directory','manifest_sha256')};pointer['schema']='izzi-internal-voice-backup-v1'
        entry.write_text(json.dumps(pointer))
        with self.assertRaises(CommandError):verify_restored_copy(entry)


class StartupGuardTests(SimpleTestCase):
    def test_ordinary_and_absent_database_allowed_without_creating_files(self):
        from config.restore_guard import require_runtime_allowed
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'prototype.sqlite3';require_runtime_allowed(path);self.assertFalse(path.exists())
            with sqlite3.connect(path) as db:db.execute('CREATE TABLE ordinary (id INTEGER)')
            require_runtime_allowed(path)

    def test_empty_or_malformed_marker_still_blocks(self):
        from config.restore_guard import require_runtime_allowed,TABLE,BLOCKED
        from django.core.exceptions import ImproperlyConfigured
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'prototype.sqlite3'
            with sqlite3.connect(path) as db:db.execute('CREATE TABLE '+TABLE+' (wrong_column TEXT)')
            with self.assertRaisesMessage(ImproperlyConfigured,BLOCKED):require_runtime_allowed(path)

    def test_corrupt_database_fails_closed_without_path_in_message(self):
        from config.restore_guard import require_runtime_allowed
        from django.core.exceptions import ImproperlyConfigured
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'prototype.sqlite3';path.write_bytes(b'corrupt synthetic SQLite')
            with self.assertRaises(ImproperlyConfigured) as error:require_runtime_allowed(path)
            self.assertNotIn(tmp,str(error.exception))
