"""Synthetic preflight/configuration regression tests. No external service or real secrets."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from .configuration import CHECKS, PilotError, default_config_path, write_template, parse_config
from .preflight import assess, local_ocr_ready
from .test_runtime import config_data, TOKEN, WEBHOOK

CHECKED={'python_3_12':True,'virtual_environment':True,'missing_or_different_packages':[],
         'platform_verified':False,'public_https_verified':False,'live_telegram_verified':False}

class PreflightTests(SimpleTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.parent=Path(self.tmp.name);self.root=self.parent/'source';self.root.mkdir()
        (self.root/'PACKAGE.json').write_text('{}')
        self.cfg=self.parent/'config.json'
        blocker=patch('socket.socket.connect',side_effect=AssertionError('No network'))
        blocker.start();self.addCleanup(blocker.stop)

    def test_template_uses_external_default_and_is_disabled_invalid_until_completed(self):
        path=default_config_path(self.root)
        self.assertEqual(path,self.parent/'source-pilot-config.json')
        write_template(self.root,path);data=json.loads(path.read_text())
        self.assertFalse(data['telegram_enabled']);self.assertFalse(any(data['confirmations'].values()))
        self.assertIsNone(data['telegram_user_id'])
        with self.assertRaises(PilotError):parse_config(path.read_text())
        with self.assertRaises(PilotError):write_template(self.root,path)
        self.assertEqual(json.loads(path.read_text()),data)

    def test_template_rejects_source_directory_and_symlink(self):
        with self.assertRaises(PilotError):write_template(self.root,self.root/'config.json')
        target=self.parent/'target';target.write_text('SYNTHETIC_KEEP')
        link=self.parent/'link';link.symlink_to(target)
        with self.assertRaises(PilotError):write_template(self.root,link)
        self.assertEqual(target.read_text(),'SYNTHETIC_KEEP')

    def test_missing_model_blocks_preflight_despite_complete_local_declarations(self):
        data=config_data();data.update(telegram_enabled=True,approval_reference='SYNTHETIC',confirmations={key:True for key in CHECKS})
        self.cfg.write_text(json.dumps(data))
        with patch('pilot_runtime.preflight.dependencies',return_value=CHECKED),patch('pilot_runtime.preflight.local_ocr_ready',return_value=False):
            report=assess(self.root,self.cfg)
        self.assertFalse(report['local_checks_passed']);self.assertIn('local_russian_ocr_unavailable',report['blockers'])
        self.assertFalse(report['secrets_read']);self.assertFalse(report['live_telegram_verified'])

    def test_declared_readiness_never_means_live_delivery_or_https_checked(self):
        data=config_data();data.update(telegram_enabled=True,approval_reference='SYNTHETIC',confirmations={key:True for key in CHECKS})
        self.cfg.write_text(json.dumps(data))
        with patch('pilot_runtime.preflight.dependencies',return_value=CHECKED),patch('pilot_runtime.preflight.local_ocr_ready',return_value=True):
            report=assess(self.root,self.cfg)
        self.assertTrue(report['local_checks_passed']);self.assertFalse(report['live_telegram_verified'])
        self.assertFalse(report['database_checked']);self.assertTrue(report['external_checks_required'])

    def test_invalid_config_and_probe_exceptions_do_not_echo_private_values(self):
        self.cfg.write_text(json.dumps({'token':TOKEN,'internal':'SYNTHETIC_PRIVATE'}))
        with patch('pilot_runtime.preflight.dependencies',return_value=CHECKED),patch('pilot_runtime.preflight.local_ocr_ready',return_value=False):
            text=json.dumps(assess(self.root,self.cfg))
        self.assertNotIn(TOKEN,text);self.assertNotIn('SYNTHETIC_PRIVATE',text)
        with patch('travelcards.ocr.russian_tesseract_available',side_effect=RuntimeError(TOKEN)):
            self.assertFalse(local_ocr_ready())

    def test_engine_listing_alone_does_not_pass_corrupt_or_timed_out_model(self):
        with patch('travelcards.ocr.russian_tesseract_available',return_value=True):
            for status in ('error','timeout','unavailable','no_text'):
                with self.subTest(status=status),patch('travelcards.ocr.recognize_card_photo',return_value=({}, {'status':status})):
                    self.assertFalse(local_ocr_ready())

    def test_serve_refuses_missing_ocr_before_secret_read_or_server_import(self):
        from .cli import main
        data=config_data();data.update(telegram_enabled=True,approval_reference='SYNTHETIC',confirmations={key:True for key in CHECKS})
        cfg=parse_config(json.dumps(data))
        with patch('pilot_runtime.cli.verify'),patch('pilot_runtime.cli.dependencies',return_value=CHECKED),patch('pilot_runtime.cli.load_config',return_value=cfg),patch('pilot_runtime.preflight.local_ocr_ready',return_value=False),patch('pilot_runtime.cli.read_interactive') as secrets,patch('pilot_runtime.cli.configure') as configure:
            with self.assertRaisesMessage(PilotError,'OCR'):main(self.root,['serve','--confirm-local-start','--confirm-telegram'])
            secrets.assert_not_called();configure.assert_not_called()

    def test_direct_activation_also_refuses_missing_ocr(self):
        from .runtime import activate
        from .secrets_boundary import SecretBundle
        data=config_data();data.update(telegram_enabled=True,approval_reference='SYNTHETIC',confirmations={key:True for key in CHECKS})
        cfg=parse_config(json.dumps(data))
        with patch('pilot_runtime.runtime.readiness',return_value={'status':'ready'}),patch('pilot_runtime.preflight.local_ocr_ready',return_value=False),patch('telegram_gateway.http_transport.TelegramHTTPTransport') as transport:
            with self.assertRaisesMessage(PilotError,'OCR'):activate(cfg,SecretBundle.from_values(TOKEN,WEBHOOK))
            transport.assert_not_called()

    def test_preflight_without_config_returns_codes_and_never_reads_secrets(self):
        from .cli import main
        with patch('pilot_runtime.cli.verify'),patch('pilot_runtime.preflight.dependencies',return_value=CHECKED),patch('pilot_runtime.preflight.local_ocr_ready',return_value=False),patch('pilot_runtime.cli.read_interactive') as secrets,contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(self.root,['preflight']),2)
        report=json.loads(output.getvalue());self.assertIn('configuration_missing_or_invalid',report['blockers'])
        self.assertFalse(report['server_started']);self.assertFalse(report['network_used']);secrets.assert_not_called()

    def test_probe_runs_without_django_setup_or_creating_runtime_files(self):
        import subprocess,sys,os
        env=dict(os.environ,TRAVELHUB_DATA_DIR=str(self.parent/'must-not-create'))
        env.pop('DJANGO_SETTINGS_MODULE',None)
        code="from pilot_runtime.preflight import local_ocr_ready;import sys,json;ok=local_ocr_ready();print(json.dumps({'probe':ok,'models_loaded':'presentations.models' in sys.modules}))"
        run=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[1],env=env,capture_output=True,text=True,timeout=35)
        self.assertEqual(run.returncode,0)
        self.assertFalse(json.loads(run.stdout)['models_loaded']);self.assertFalse((self.parent/'must-not-create').exists())
