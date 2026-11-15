"""Offline polling contracts: fake HTTP/source, synthetic images and isolated Django DB."""
import copy,json,tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from django.test import SimpleTestCase,TransactionTestCase,override_settings
from django.contrib.auth import get_user_model
from inquiries.models import OrganizationIdentity,Inquiry
from worksheet_reviews.models import WorksheetReview
from worksheet_reviews.ocr_services import current_revision
from travelcards.ocr import _empty_fields
from .tests import TelegramGatewayTests
from .test_http_transport import HTTPFixture,raw_response,TOKEN
from .http_transport import TelegramPollingHTTPTransport,TelegramTransportError
from .models import PollingState,UpdateReceipt
from .polling import run,PollingError,exclusive
from .chat_review import review_token


def api(value,**kwargs):return raw_response(json.dumps({'ok':True,'result':value}).encode(),**kwargs)

class PollHTTPTests(SimpleTestCase):
    def test_disabled_does_not_read_secret_or_start_worker(self):
        provider=lambda:self.fail('secret')
        transport=TelegramPollingHTTPTransport(provider,runner=lambda *a:self.fail('worker'))
        with self.assertRaises(TelegramTransportError):transport.get_updates(0)
        with self.assertRaises(TelegramTransportError):transport.poll_status()

    def test_fixed_origin_identity_and_long_poll_request(self):
        fixture=HTTPFixture([api({'id':123,'is_bot':True}),api({'url':''}),api([{'update_id':41,'message':{'text':'SYNTHETIC'}}])])
        transport=TelegramPollingHTTPTransport(lambda:TOKEN,enabled=True,runner=fixture.run)
        self.assertEqual(transport.poll_status(),{'bot_id':123,'webhook_set':False})
        self.assertEqual(transport.get_updates(40)[0]['update_id'],41)
        wire=fixture.requests()[-1];self.assertIn(b'/getUpdates ',wire)
        body=json.loads(wire.split(b'\r\n\r\n',1)[1]);self.assertEqual(body,{'offset':40,'timeout':10,'limit':1,'allowed_updates':['message','callback_query']})
        self.assertNotIn(TOKEN,repr(transport))

    def test_webhook_url_is_reduced_to_boolean_and_never_returned(self):
        fixture=HTTPFixture([api({'id':123,'is_bot':True}),api({'url':'https://synthetic.invalid/'+TOKEN})])
        value=TelegramPollingHTTPTransport(lambda:TOKEN,enabled=True,runner=fixture.run).poll_status()
        self.assertEqual(value,{'bot_id':123,'webhook_set':True});self.assertNotIn(TOKEN,repr(value))

    def test_empty_poll_and_malformed_or_oversized_batch(self):
        fixture=HTTPFixture([api([])])
        self.assertEqual(TelegramPollingHTTPTransport(lambda:TOKEN,enabled=True,runner=fixture.run).get_updates(0),[])
        for result in ({},[{'update_id':True}],[{'update_id':1},{'update_id':2}]):
            fixture=HTTPFixture([api(result)])
            with self.assertRaises(TelegramTransportError):TelegramPollingHTTPTransport(lambda:TOKEN,enabled=True,runner=fixture.run).get_updates(0)

    def test_timeout_http_conflict_redirect_and_secret_errors_stop_without_retry(self):
        for status in (302,409,429,500):
            fixture=HTTPFixture([api({'description':TOKEN},status=status)])
            with self.assertRaises(TelegramTransportError) as caught:TelegramPollingHTTPTransport(lambda:TOKEN,enabled=True,runner=fixture.run).get_updates(0)
            self.assertNotIn(TOKEN,str(caught.exception));self.assertEqual(len(fixture.requests()),1)
        fixture=HTTPFixture([api([])]);fixture.read_failure=TimeoutError(TOKEN)
        with self.assertRaises(TelegramTransportError) as caught:TelegramPollingHTTPTransport(lambda:TOKEN,enabled=True,runner=fixture.run).get_updates(0)
        self.assertNotIn(TOKEN,str(caught.exception))
        def failure(*args):raise RuntimeError(TOKEN)
        with self.assertRaises(TelegramTransportError) as caught:TelegramPollingHTTPTransport(lambda:TOKEN,enabled=True,runner=failure).get_updates(0)
        self.assertIsNone(caught.exception.__context__)

    def test_negative_offset_and_unbounded_values_refused_before_worker(self):
        t=TelegramPollingHTTPTransport(lambda:TOKEN,enabled=True,runner=lambda *a:self.fail('worker'))
        huge=TelegramPollingHTTPTransport(lambda:TOKEN,enabled=True,runner=lambda *a:b'U'+b' '*65537)
        with self.assertRaises(TelegramTransportError):huge.get_updates(0)
        for offset in (-1,True,2**53):
            with self.assertRaises(TelegramTransportError):t.get_updates(offset)

class Source:
    def __init__(self,items,bot_id=123):self.items=list(items);self.offsets=[];self.status={'bot_id':bot_id,'webhook_set':False}
    def poll_status(self):return self.status
    def get_updates(self,offset):
        self.offsets.append(offset)
        if not self.items:return []
        value=self.items.pop(0)
        return value() if callable(value) else value

class PollingTests(TransactionTestCase):
    def setUp(self):
        TelegramGatewayTests.setUp(self)
        self.binding=replace(self.binding,webhook_secret=None,review_origin='',in_chat_review=True,mode='polling')
        self.org=OrganizationIdentity.objects.create()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.directory=Path(self.tmp.name)
        stub=patch('worksheet_reviews.ocr_services.recognize_card_photo',return_value=(_empty_fields('manual_fallback','Synthetic only'),{'status':'unavailable'}))
        stub.start();self.addCleanup(stub.stop)

    def run_source(self,source,**kwargs):return run(self.binding,source,self.directory,bot_id=123,organization_id=self.org.organization_id,enabled=True,max_cycles=kwargs.pop('max_cycles',1),**kwargs)

    def command(self,text,n):return {'update_id':n,'message':{'message_id':n,'from':{'id':100001,'is_bot':False},'chat':{'id':100001,'type':'private'},'text':text}}

    def test_photo_edit_confirm_cursor_and_database_remain_same_workflow(self):
        def edit():
            w=WorksheetReview.objects.get();return [self.command('ИСПРАВИТЬ '+review_token(w,current_revision(w))+' 4 180000',102)]
        def confirm():
            w=WorksheetReview.objects.get();return [self.command('ПОДТВЕРДИТЬ '+review_token(w,current_revision(w)),103)]
        source=Source([[self.update],edit,confirm])
        result=self.run_source(source,max_cycles=3)
        self.assertEqual(result['processed'],3);self.assertEqual(source.offsets,[0,102,103])
        work=WorksheetReview.objects.get();self.assertEqual(bytes(work.worksheet_bytes),self.png)
        self.assertIsNotNone(current_revision(work).confirmed_at);self.assertEqual(PollingState.objects.get().next_offset,104)
        self.assertEqual(Inquiry.objects.count(),0);self.assertEqual(len(self.transport.messages),3)
        self.run_source(Source([[]]));self.assertEqual(WorksheetReview.objects.count(),1)

    def test_crash_after_receipt_before_cursor_replays_without_second_work_or_reply(self):
        with patch.object(PollingState,'save',side_effect=RuntimeError(TOKEN)):
            with self.assertRaises(PollingError):self.run_source(Source([[self.update]]))
        # save patch can also fail initial get_or_create; create the state first and retry controlled failure.
        self.run_source(Source([[]]));cursor=PollingState.objects.get()
        with patch.object(PollingState,'save',side_effect=RuntimeError(TOKEN)):
            with self.assertRaises(PollingError) as caught:self.run_source(Source([[self.update]]))
        self.assertNotIn(TOKEN,str(caught.exception));self.assertEqual(WorksheetReview.objects.count(),1)
        sent=len(self.transport.messages);self.run_source(Source([[self.update]]))
        self.assertEqual(WorksheetReview.objects.count(),1);self.assertEqual(len(self.transport.messages),sent)
        self.assertEqual(PollingState.objects.get().next_offset,102)

    def test_foreign_private_chat_group_and_forward_do_not_download_or_write_work(self):
        for change in ('foreign','group','forward'):
            u=copy.deepcopy(self.update);u['update_id']+=len(self.transport.downloads)+100
            if change=='foreign':u['message']['from']['id']=200002
            if change=='group':u['message']['chat']['type']='group'
            if change=='forward':u['message']['forward_origin']={'type':'user'}
            PollingState.objects.all().delete();self.run_source(Source([[u]]))
        self.assertFalse(self.transport.downloads);self.assertFalse(self.transport.messages);self.assertFalse(WorksheetReview.objects.exists())

    def test_revoked_owner_before_and_during_poll_does_not_ack(self):
        self.owner.is_active=False;self.owner.save()
        with self.assertRaises(PollingError):self.run_source(Source([[self.update]]))
        self.owner.is_active=True;self.owner.save()
        def revoked():get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False);return [self.update]
        with self.assertRaises(PollingError):self.run_source(Source([revoked]))
        self.assertEqual(PollingState.objects.get().next_offset,0);self.assertFalse(WorksheetReview.objects.exists())

    def test_stop_before_and_after_poll_retains_cursor_and_no_send(self):
        (self.directory/'STOP').touch();s=Source([[self.update]])
        self.assertEqual(self.run_source(s)['status'],'stopped');self.assertFalse(s.offsets)
        (self.directory/'STOP').unlink()
        def stopped():(self.directory/'STOP').touch();return [self.update]
        self.assertEqual(self.run_source(Source([stopped]))['status'],'stopped')
        self.assertFalse(WorksheetReview.objects.exists());self.assertEqual(PollingState.objects.get().next_offset,0)

    def test_single_poller_lock_and_keyboard_interrupt_release(self):
        with exclusive(self.directory):
            with self.assertRaises(PollingError):self.run_source(Source([[]]))
        def interrupt():raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.run_source(Source([interrupt]))
        self.assertFalse((self.directory/'telegram-polling.lock').exists())

    def test_existing_webhook_wrong_bot_or_changed_binding_are_closed(self):
        for status in ({'bot_id':999,'webhook_set':False},{'bot_id':123,'webhook_set':True},{'bot_id':123,'webhook_set':0}):
            source=Source([]);source.status=status
            with self.assertRaises(PollingError):self.run_source(source)
            self.assertFalse(source.offsets)
        self.run_source(Source([[]]));self.binding=replace(self.binding,telegram_user_id=200002,chat_id=200002)
        with self.assertRaises(PollingError):self.run_source(Source([[]]))

    def test_other_organization_and_webhook_entry_cannot_use_polling_binding(self):
        import uuid
        with self.assertRaises(PollingError):run(self.binding,Source([]),self.directory,bot_id=123,organization_id=uuid.uuid4(),enabled=True)
        with override_settings(TELEGRAM_GATEWAY=self.binding):self.assertEqual(self.client.post(self.url,{},content_type='application/json').status_code,503)

    def test_download_timeout_leaves_cursor_and_reply_uncertainty_stops(self):
        self.transport.fail_download=True
        with self.assertRaises(PollingError):self.run_source(Source([[self.update]]))
        self.assertEqual(PollingState.objects.get().next_offset,0)
        self.transport.fail_download=False;self.transport.fail_reply=True
        self.assertEqual(self.run_source(Source([[self.update]]))['status'],'reply_uncertain')
        self.assertEqual(PollingState.objects.get().next_offset,102)

    def test_stale_cursor_response_stops_without_new_operations(self):
        self.run_source(Source([[self.update]]));sent=len(self.transport.messages)
        with self.assertRaises(PollingError):self.run_source(Source([[self.update]]))
        self.assertEqual(len(self.transport.messages),sent);self.assertEqual(WorksheetReview.objects.count(),1)

    def test_stop_during_download_does_not_continue_to_send(self):
        self.transport.before_download=lambda:(self.directory/'STOP').touch()
        with self.assertRaises(PollingError):self.run_source(Source([[self.update]]))
        self.assertFalse(WorksheetReview.objects.exists());self.assertFalse(self.transport.messages)
        self.assertEqual(PollingState.objects.get().next_offset,0)

class PollConfigTests(SimpleTestCase):
    def data(self):
        from .polling_cli import CHECKS
        return {'schema':1,'purpose':'synthetic-polling-pilot','owner_id':1,'telegram_user_id':100001,'chat_id':100001,
            'bot_id':123,'organization_id':'00000000-0000-0000-0000-000000000001','gateway':'synthetic-poll',
            'enabled':False,'approval_reference':None,'confirmations':{k:False for k in CHECKS}}

    def test_disabled_single_private_binding_and_explicit_enable(self):
        from .polling_cli import load
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'config.json';data=self.data();p.write_text(json.dumps(data))
            self.assertFalse(load(p).enabled)
            data['enabled']=True;p.write_text(json.dumps(data))
            with self.assertRaises(ValueError):load(p)
            data.update(approval_reference='SYNTHETIC',confirmations={k:True for k in data['confirmations']});p.write_text(json.dumps(data))
            self.assertTrue(load(p).enabled)
            data['chat_id']=200002;p.write_text(json.dumps(data))
            with self.assertRaises(ValueError):load(p)

    def test_unknown_secret_fields_duplicates_and_symlinks_rejected(self):
        from .polling_cli import load
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'config.json';data=self.data();data['token']=TOKEN;p.write_text(json.dumps(data))
            with self.assertRaises(ValueError):load(p)
            p.write_text('{"schema":1,"schema":1}')
            with self.assertRaises(Exception):load(p)
            link=Path(td)/'link';link.symlink_to(p)
            with self.assertRaises(ValueError):load(link)

    def test_secret_entry_requires_terminal_and_hides_getpass_warning(self):
        from .polling_cli import read_token
        import getpass
        with patch('telegram_gateway.polling_cli.sys.stdin.isatty',return_value=False),patch('telegram_gateway.polling_cli.getpass.getpass') as reader:
            with self.assertRaises(ValueError):read_token()
            reader.assert_not_called()
        with patch('telegram_gateway.polling_cli.sys.stdin.isatty',return_value=True),patch('telegram_gateway.polling_cli.getpass.getpass',side_effect=getpass.GetPassWarning('synthetic')):
            with self.assertRaises(getpass.GetPassWarning):read_token()

    def test_disabled_run_never_reads_secret_or_starts_process(self):
        from .polling_cli import main,load
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'cfg.json';p.write_text(json.dumps(self.data()))
            with patch('stage_one_demo.package.verify'),patch('telegram_gateway.polling_cli.read_token') as reader,patch('telegram_gateway.polling_cli.configure') as configure:
                self.assertEqual(main(['run','--directory',td,'--config',str(p),'--confirm-telegram']),2)
                reader.assert_not_called();configure.assert_not_called()
