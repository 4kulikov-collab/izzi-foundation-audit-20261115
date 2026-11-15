"""Two actual isolated SQLite roots, same numeric owner IDs, no server/network."""
import tempfile
from pathlib import Path
from django.test import SimpleTestCase
from .synthetic_theme_smoke import run

class ThemeCompanyTests(SimpleTestCase):
    def test_two_companies_cannot_switch_or_read_each_others_versions(self):
        with tempfile.TemporaryDirectory(prefix='izzi-theme-proof-') as td:
            report=run(Path(td)/'proof')
            self.assertTrue(report['company_b']['foreign_company_denied'])
            self.assertTrue(report['restart_preserved'])
            self.assertTrue(report['company_a']['director_links_verified'])
            self.assertTrue(report['runtime_db_keys_sessions_not_exported'])
