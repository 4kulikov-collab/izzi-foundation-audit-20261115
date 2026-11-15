"""Regression tests use disposable synthetic SQLite files, never the local user DB."""
import io
import sqlite3
import tempfile
from pathlib import Path
from django.core.management import call_command, CommandError
from django.test import TransactionTestCase


class InquiryBackupSchemaTests(TransactionTestCase):
    def check_rejected(self, statements):
        with tempfile.TemporaryDirectory(prefix='inquiry-schema-') as directory:
            backup = Path(directory) / 'backup.sqlite3'
            target = Path(directory) / 'restored.sqlite3'
            call_command('backup_local', destination=str(backup), stdout=io.StringIO())
            with sqlite3.connect(backup) as db:
                for statement in statements:
                    db.execute(statement)
            with self.assertRaises(CommandError):
                call_command('restore_local', backup=str(backup), destination=str(target),
                             test_copy=True, stdout=io.StringIO())
            self.assertFalse(target.exists())
            self.assertFalse(list(Path(directory).glob('.travelhub-snapshot-*')))

    def test_missing_inquiry_table_rejected(self):
        self.check_rejected(['DROP TABLE inquiries_operation'])

    def test_missing_inquiry_column_rejected(self):
        self.check_rejected(['ALTER TABLE inquiries_operation DROP COLUMN fingerprint'])

    def test_missing_conditional_unique_index_rejected(self):
        self.check_rejected(['DROP INDEX inquiry_external_owner_unique'])

    def test_missing_foreign_key_rejected(self):
        self.check_rejected(['DROP TABLE inquiries_worklink',
            'CREATE TABLE inquiries_worklink (id integer PRIMARY KEY, work_id char(32) NOT NULL UNIQUE, inquiry_id char(32) NOT NULL)'])
