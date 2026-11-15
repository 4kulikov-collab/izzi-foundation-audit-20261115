"""Synthetic offline pilot checks: no real configuration, secret, server or network activation."""
import copy
from dataclasses import replace
import getpass
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TransactionTestCase, override_settings

from .configuration import BASE, CHECKS, PilotError, load_config, parse_config, state_directory, stop
from .runtime import GuardedTransport, PilotBoundary, activate, readiness, serve_options
from .secrets_boundary import MemoryToken, SecretBundle, read_interactive

TOKEN = '123456789:SYNTHETIC_ONLY_NOT_A_REAL_BOT_TOKEN'
WEBHOOK = 'SYNTHETIC_WEBHOOK_SECRET_FOR_TESTS_ONLY'


def config_data():
    # Synthetic non-reserved syntax for parser tests; never resolved or contacted.
    return {'schema':1,'purpose':'synthetic-closed-pilot','origin':'https://izzi-pilot.synthetic-company.ru',
            'owner_id':1,'telegram_user_id':100001,'chat_id':100001,'gateway':'synthetic-pilot',
            'telegram_enabled':False,'approval_reference':None,'confirmations':{key:False for key in CHECKS}}


class ConfigurationTests(SimpleTestCase):
    def test_disabled_config_does_not_claim_live_or_infrastructure_approval(self):
        cfg=parse_config(json.dumps(config_data()))
        self.assertFalse(cfg.telegram_enabled)
        self.assertFalse(any(cfg.confirmations.values()))
        self.assertEqual(repr(cfg),'<PilotConfig values hidden>')

    def test_unknown_fields_and_secret_values_are_rejected_without_echo(self):
        data=config_data();data['bot_token']=TOKEN
        with self.assertRaises(PilotError) as caught:parse_config(json.dumps(data))
        self.assertNotIn(TOKEN,str(caught.exception))
        with self.assertRaises(PilotError):parse_config('{"schema":1,"schema":1}')

    def test_origins_reject_placeholders_http_paths_ip_wildcard_and_credentials(self):
        for origin in ('https://pilot.example.invalid','http://pilot.synthetic-company.ru','https://127.0.0.1','https://x.test',
                       'https://example.com','https://pilot.synthetic-company.ru/path','https://pilot.synthetic-company.ru:443',
                       'https://user:password@pilot.synthetic-company.ru','https://*.synthetic-company.ru','https://pilot.synthetic-company.ru?key=x'):
            data=config_data();data['origin']=origin
            with self.subTest(origin_type=origin[:8]),self.assertRaises(PilotError):parse_config(json.dumps(data))

    def test_binding_ids_and_enabled_require_explicit_approval(self):
        for key,value in [('owner_id',True),('telegram_user_id',None),('chat_id',100002),('gateway','../bot'),('telegram_enabled','false')]:
            data=config_data();data[key]=value
            with self.assertRaises(PilotError):parse_config(json.dumps(data))
        data=config_data();data['telegram_enabled']=True
        with self.assertRaises(PilotError):parse_config(json.dumps(data))
        data['approval_reference']='SYNTHETIC-APPROVAL';data['confirmations']={key:True for key in CHECKS}
        self.assertTrue(parse_config(json.dumps(data)).telegram_enabled)

    def test_bounded_config_file_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'config.json';p.write_bytes(b'x'*16385)
            with self.assertRaises(PilotError):load_config(p)
            p.write_text(json.dumps(config_data()));link=root/'link.json';link.symlink_to(p)
            with self.assertRaises(PilotError):load_config(link)
            self.assertFalse(load_config(p).telegram_enabled)

    def test_foreign_state_and_external_database_are_not_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=root/'.local-state/pilot';state.mkdir(parents=True)
            db=state/'prototype.sqlite3';db.write_bytes(b'SYNTHETIC_FOREIGN_DB')
            with self.assertRaises(PilotError):state_directory(root,create=True)
            self.assertEqual(db.read_bytes(),b'SYNTHETIC_FOREIGN_DB')
            db.unlink();state.rmdir()
            own=state_directory(root,create=True);db=own/'prototype.sqlite3';outside=root/'outside';outside.write_bytes(b'SYNTHETIC_SENTINEL');db.symlink_to(outside)
            with self.assertRaises(PilotError):state_directory(root)
            self.assertEqual(outside.read_bytes(),b'SYNTHETIC_SENTINEL')

    def test_stop_is_idempotent_and_preserves_owned_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=state_directory(root,create=True);db=state/'prototype.sqlite3';db.write_bytes(b'SYNTHETIC_KEEP')
            stop(root);stop(root)
            self.assertTrue((state/'STOP').exists());self.assertEqual(db.read_bytes(),b'SYNTHETIC_KEEP')
            self.assertEqual(state_directory(root),state)


class SecretBoundaryTests(SimpleTestCase):
    def test_memory_provider_has_no_io_and_repr_hides_values(self):
        bundle=SecretBundle.from_values(TOKEN,WEBHOOK)
        with patch('builtins.open',side_effect=AssertionError('No secret file I/O')),patch('os.getenv',side_effect=AssertionError('No environment secret')):
            self.assertEqual(bundle.token(),TOKEN)
        self.assertNotIn(TOKEN,repr(bundle));self.assertNotIn(WEBHOOK,repr(bundle));self.assertNotIn(TOKEN,repr(bundle.token))

    def test_arbitrary_callable_and_invalid_tokens_cannot_enter_bundle(self):
        with self.assertRaises(PilotError):SecretBundle(lambda: TOKEN,WEBHOOK)
        for value in ('',TOKEN+'/bad','https://example.invalid/'+TOKEN):
            with self.assertRaises(PilotError) as caught:MemoryToken(value)
            self.assertNotIn(TOKEN,str(caught.exception))
        with self.assertRaises(PilotError):SecretBundle.from_values(TOKEN,'short')

    def test_noninteractive_input_refuses_before_reading(self):
        with patch('pilot_runtime.secrets_boundary.sys.stdin.isatty',return_value=False),patch('pilot_runtime.secrets_boundary.getpass.getpass') as reader:
            with self.assertRaises(PilotError):read_interactive()
            reader.assert_not_called()

    def test_hidden_synthetic_input_and_echo_warning_fail_closed(self):
        with patch('pilot_runtime.secrets_boundary.sys.stdin.isatty',return_value=True),patch('pilot_runtime.secrets_boundary.getpass.getpass',side_effect=[TOKEN,WEBHOOK]):
            self.assertEqual(read_interactive().token(),TOKEN)
        with patch('pilot_runtime.secrets_boundary.sys.stdin.isatty',return_value=True),patch('pilot_runtime.secrets_boundary.getpass.getpass',side_effect=getpass.GetPassWarning('synthetic')):
            with self.assertRaises(PilotError):read_interactive()


class StopBoundaryTests(SimpleTestCase):
    def test_stop_blocks_download_and_send_without_touching_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory);transport=SimpleNamespace(iter_file=lambda *a,**k:self.fail('download'),send_message=lambda *a,**k:self.fail('send'))
            guard=GuardedTransport(transport,state);(state/'STOP').touch()
            with self.assertRaises(PilotError):list(guard.iter_file('synthetic'))
            with self.assertRaises(PilotError):guard.send_message(1,'Synthetic')
            self.assertEqual(guard.active,0)

    def test_stop_during_download_blocks_following_data_and_releases_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)
            def chunks(*args,**kwargs):
                yield b'synthetic-first';(state/'STOP').touch();yield b'synthetic-second'
            guard=GuardedTransport(SimpleNamespace(iter_file=chunks),state)
            stream=guard.iter_file('synthetic');self.assertEqual(next(stream),b'synthetic-first');self.assertEqual(guard.active,1)
            with self.assertRaises(PilotError):next(stream)
            self.assertEqual(guard.active,0)

    def test_server_options_are_loopback_only_and_do_not_trust_host_proxy(self):
        opts=serve_options()
        self.assertEqual((opts['host'],opts['port'],opts['threads']),('127.0.0.1',8765,1))
        self.assertEqual(opts['trusted_proxy'],'127.0.0.1')
        self.assertEqual(opts['trusted_proxy_headers'],{'x-forwarded-proto'})
        self.assertTrue(opts['clear_untrusted_proxy_headers']);self.assertFalse(opts['expose_tracebacks'])


class PilotHTTPTests(TransactionTestCase):
    def setUp(self):
        probe=patch('pilot_runtime.preflight.local_ocr_ready',return_value=True);probe.start();self.addCleanup(probe.stop)
        self.owner=get_user_model().objects.create_user('synthetic-pilot-owner')
        self.other=get_user_model().objects.create_user('synthetic-pilot-other')
        data=config_data();data['owner_id']=self.owner.pk;self.cfg=parse_config(json.dumps(data))
        self.storage=tempfile.TemporaryDirectory();self.addCleanup(self.storage.cleanup);self.state=Path(self.storage.name)
        (self.state/'prototype.sqlite3').touch()  # Guard marker only; actual Django test DB remains isolated.
        self.scope=override_settings(PILOT_CONFIG=self.cfg,PILOT_STATE=self.state,PILOT_TRANSPORT=None,TELEGRAM_GATEWAY=None,ALLOWED_HOSTS=[self.cfg.host])
        self.scope.enable();self.addCleanup(self.scope.disable)
        self.factory=RequestFactory()
        self.network=patch('socket.socket.connect',side_effect=AssertionError('No network in pilot tests'));self.network.start();self.addCleanup(self.network.stop)

    def request(self,path='/',*,secure=True,host=None,user=None):
        req=self.factory.get(path,secure=secure,HTTP_HOST=host or self.cfg.host);req.user=user or self.owner
        return PilotBoundary(lambda request:HttpResponse('synthetic-private-view'))(req)

    def test_health_ready_is_local_django_evidence_not_live_https(self):
        response=self.request('/__pilot__/health')
        self.assertEqual(response.status_code,200)
        data=json.loads(response.content);self.assertEqual(data['status'],'ready');self.assertEqual(data['gateway'],'off')
        self.assertFalse(data['live_telegram_verified']);self.assertEqual(response['Cache-Control'],'no-store')
        self.assertNotIn(self.cfg.host,response.content.decode());self.assertNotIn('owner_id',response.content.decode())

    def test_wrong_host_http_other_user_and_public_share_are_blocked(self):
        self.assertEqual(self.request(secure=False).status_code,403)
        self.assertEqual(self.request(host='other.synthetic-company.ru').status_code,403)
        self.assertEqual(self.request(user=self.other).status_code,403)
        self.assertEqual(self.request('/s/synthetic-only/').status_code,404)
        self.assertEqual(self.request().status_code,200)

    def test_missing_schema_inactive_or_admin_owner_is_not_ready(self):
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertEqual(self.request('/__pilot__/health').status_code,503)
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=True,is_staff=True)
        self.assertEqual(readiness()['status'],'blocked')
        get_user_model().objects.filter(pk=self.owner.pk).update(is_staff=False)
        with patch('django.db.migrations.executor.MigrationExecutor.migration_plan',return_value=['synthetic_pending']):
            self.assertEqual(readiness()['status'],'blocked')

    def test_stop_retains_health_and_denies_new_requests(self):
        (self.state/'STOP').touch()
        response=self.request('/__pilot__/health');self.assertEqual(response.status_code,503)
        self.assertEqual(json.loads(response.content)['status'],'stopped')
        self.assertEqual(self.request('/integrations/telegram/update/').status_code,503)

    def test_enable_requires_approval_memory_secrets_readiness_and_same_binding(self):
        bundle=SecretBundle.from_values(TOKEN,WEBHOOK)
        with self.assertRaises(PilotError):activate(replace(self.cfg,telegram_enabled=True),bundle)
        config=replace(self.cfg,telegram_enabled=True,approval_reference='SYNTHETIC-APPROVAL',confirmations={key:True for key in CHECKS})
        with self.assertRaises(PilotError):activate(replace(config,owner_id=self.other.pk),bundle)
        (self.state/'STOP').touch()
        with self.assertRaises(PilotError):activate(config,bundle)
        (self.state/'STOP').unlink()
        with patch('telegram_gateway.http_transport.run_isolated',side_effect=AssertionError('Do not start HTTP worker')):
            activate(config,bundle)  # In-memory synthetic configuration only; no request or token installation.
        from django.conf import settings
        self.assertEqual(settings.TELEGRAM_GATEWAY.owner_id,self.owner.pk)
        self.assertEqual(settings.PILOT_TRANSPORT.active,0)
        self.assertNotIn(TOKEN,repr(settings.TELEGRAM_GATEWAY))

class PilotCLITests(SimpleTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        probe=patch('pilot_runtime.preflight.local_ocr_ready',return_value=True);probe.start();self.addCleanup(probe.stop)
        self.cfg=parse_config(json.dumps(config_data()))
        self.checks={'python_3_12':True,'virtual_environment':True,'missing_or_different_packages':[]}

    def test_no_start_flags_or_telegram_confirmation_never_reads_secrets(self):
        from .cli import main
        enabled=replace(self.cfg,telegram_enabled=True,approval_reference='SYNTHETIC',confirmations={key:True for key in CHECKS})
        with patch('pilot_runtime.cli.verify'),patch('pilot_runtime.cli.load_config',return_value=enabled),patch('pilot_runtime.cli.dependencies',return_value=self.checks),patch('pilot_runtime.cli.state_directory',return_value=self.root),patch('pilot_runtime.cli.read_interactive') as reader:
            with self.assertRaises(PilotError):main(self.root,['serve'])
            with self.assertRaises(PilotError):main(self.root,['serve','--confirm-local-start'])
            reader.assert_not_called()

    def test_bad_database_readiness_blocks_secret_input(self):
        from .cli import main
        enabled=replace(self.cfg,telegram_enabled=True,approval_reference='SYNTHETIC',confirmations={key:True for key in CHECKS})
        with patch('pilot_runtime.cli.verify'),patch('pilot_runtime.cli.load_config',return_value=enabled),patch('pilot_runtime.cli.dependencies',return_value=self.checks),patch('pilot_runtime.cli.state_directory',return_value=self.root),patch('pilot_runtime.cli.configure'),patch('pilot_runtime.cli.readiness',return_value={'status':'blocked'}),patch('pilot_runtime.cli.read_interactive') as reader:
            with self.assertRaises(PilotError):main(self.root,['serve','--confirm-local-start','--confirm-telegram'])
            reader.assert_not_called()

    def test_disabled_start_contract_uses_fake_server_without_network_or_secrets(self):
        from .cli import main
        calls=[]
        server=SimpleNamespace(serve=lambda app,**kwargs:calls.append(kwargs))
        with patch('pilot_runtime.cli.verify'),patch('pilot_runtime.cli.load_config',return_value=self.cfg),patch('pilot_runtime.cli.dependencies',return_value=self.checks),patch('pilot_runtime.cli.state_directory',return_value=self.root),patch('pilot_runtime.cli.configure'),patch('pilot_runtime.cli.readiness',return_value={'status':'ready'}),patch('pilot_runtime.cli.read_interactive') as reader,patch('django.core.wsgi.get_wsgi_application',return_value=object()),patch.dict('sys.modules',{'waitress':server}),patch('builtins.print'):
            self.assertEqual(main(self.root,['serve','--confirm-local-start']),0)
            reader.assert_not_called()
        self.assertEqual(calls,[serve_options()])


class CurrentPackageStateTests(SimpleTestCase):
    def package(self, parent):
        import hashlib
        from stage_one_demo.package import SCHEMA
        root = Path(parent)/'source'; root.mkdir()
        (root/'code.py').write_text('# synthetic\n')
        (root/'PACKAGE.json').write_text(json.dumps({'schema':SCHEMA,'commit':'a'*40,
            'files':{'code.py':hashlib.sha256((root/'code.py').read_bytes()).hexdigest()}}))
        return root

    def test_prepare_state_does_not_break_current_package_integrity(self):
        from .package import verify
        with tempfile.TemporaryDirectory() as parent:
            root=self.package(parent); verify(root)
            state=state_directory(root, create=True)
            self.assertEqual(state, root.parent/'source-pilot-state')
            (state/'synthetic-private.txt').write_text('synthetic only')
            self.assertEqual(state_directory(root), state)
            verify(root); stop(root); verify(root)
            self.assertTrue((state/'STOP').is_file())

    def test_existing_foreign_state_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as parent:
            root=self.package(parent); state=root.parent/'source-pilot-state'; state.mkdir()
            with self.assertRaises(PilotError): state_directory(root, create=True)
            self.assertEqual(list(state.iterdir()), [])

    def test_state_symlink_and_extra_source_file_rejected(self):
        from .package import verify
        with tempfile.TemporaryDirectory() as parent:
            root=self.package(parent); target=root.parent/'external'; target.mkdir()
            (root.parent/'source-pilot-state').symlink_to(target, target_is_directory=True)
            with self.assertRaises(PilotError): state_directory(root, create=True)
            (root/'pilot-config.json').write_text('{}')
            with self.assertRaises(ValueError): verify(root)
