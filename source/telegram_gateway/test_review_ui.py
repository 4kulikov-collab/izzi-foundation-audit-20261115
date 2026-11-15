"""Synthetic button/callback transport, actual existing revision services. No Telegram sockets."""
import copy,json,tempfile,uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from django.test import TransactionTestCase,SimpleTestCase,override_settings
from django.contrib.auth import get_user_model
from django.db import transaction
from inquiries.models import OrganizationIdentity,Inquiry
from worksheet_reviews.models import WorksheetReview
from worksheet_reviews.ocr_services import current_revision,save_fields
from .adapter import accept,GatewayError
from .models import ReviewSession,UpdateReceipt
from .tests import MemoryTransport
from .test_chat_review import synthetic_fields
from .http_transport import TelegramHTTPTransport,TelegramTransportError
from .test_http_transport import HTTPFixture,raw_response,TOKEN

class ButtonTransport(MemoryTransport):
    def __init__(self,content):super().__init__(content);self.keyboards=[];self.acks=[];self.fail_ack=False
    def send_buttons(self,chat_id,text,keyboard,**kwargs):
        self.keyboards.append(copy.deepcopy(keyboard));self.send_message(chat_id,text,**kwargs)
    def answer_callback(self,callback_id,text,**kwargs):
        self.acks.append(callback_id)
        if self.fail_ack:raise TimeoutError('SYNTHETIC_SECRET_MUST_NOT_LEAK')

class ButtonTests(TransactionTestCase):
    def setUp(self):
        from .tests import TelegramGatewayTests
        TelegramGatewayTests.setUp(self)
        self.org=OrganizationIdentity.objects.create();self.transport=ButtonTransport(self.png)
        self.binding=replace(self.binding,transport=self.transport,in_chat_review=True,in_chat_buttons=True,bot_id=123)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        setting=override_settings(DATA_DIR=Path(self.tmp.name));setting.enable();self.addCleanup(setting.disable)
        stub=patch('worksheet_reviews.ocr_services.recognize_card_photo',return_value=(synthetic_fields(),{'status':'recognized','engine':'synthetic stub'}))
        stub.start();self.addCleanup(stub.stop);self.seq=1000

    def photo(self):
        self.update['update_id']+=1;self.update['message']['message_id']+=1
        accept(self.update,self.binding);return WorksheetReview.objects.latest('created_at')

    def button(self,label):
        for row in self.transport.keyboards[-1]['inline_keyboard']:
            for b in row:
                if b['text']==label:return b['callback_data']
        self.fail('Missing button: '+label)

    def callback(self,data=None,label=None):
        self.seq+=1
        return {'update_id':self.seq,'callback_query':{'id':'cb_'+str(self.seq),'from':{'id':100001,'is_bot':False},
            'message':{'message_id':800,'from':{'id':123,'is_bot':True},'chat':{'id':100001,'type':'private'}},
            'data':data or self.button(label)}}

    def click(self,label):
        u=self.callback(label=label);return accept(u,self.binding),u

    def text(self,value):
        self.seq+=1
        u={'update_id':self.seq,'message':{'message_id':self.seq,'from':{'id':100001,'is_bot':False},'chat':{'id':100001,'type':'private'},'text':value}}
        return accept(u,self.binding),u

    def field(self,index):
        from travelcards.forms import FIELD_LABELS
        self.click(f'{index}. {list(FIELD_LABELS.values())[index-1]}')

    def test_buttons_edit_confirm_same_workflow_without_service_commands(self):
        work=self.photo();self.assertNotIn('ИСПРАВИТЬ ',self.transport.messages[-1][1]);self.assertNotIn('Версия:',self.transport.messages[-1][1])
        self.field(4);self.click('Исправить');self.assertEqual(ReviewSession.objects.get().panel,'edit')
        self.assertIn('рублях',self.transport.messages[-1][1]);self.text('185000')
        self.assertEqual(current_revision(work).fields['budget']['value'],'185000');self.assertIsNone(current_revision(work).confirmed_at)
        self.click('Назад');self.field(1);self.click('Верно');self.click('Назад');self.click('Подтвердить')
        self.assertIsNotNone(current_revision(work).confirmed_at);self.assertEqual(bytes(work.worksheet_bytes),self.png)
        self.assertEqual(Inquiry.objects.count(),0);self.assertIn('U-ON: не синхронизировано',self.transport.messages[-1][1])

    def test_unchecked_candidates_cannot_be_confirmed(self):
        work=self.photo();self.click('Подтвердить')
        self.assertIsNone(current_revision(work).confirmed_at);self.assertIn('Остались кандидаты',self.transport.messages[-1][1])

    def test_callback_duplicate_is_one_ack_reply_and_revision(self):
        work=self.photo();self.field(1);_,u=self.click('Верно')
        number=current_revision(work).number;counts=(len(self.transport.messages),len(self.transport.acks));nonce=ReviewSession.objects.get().nonce
        result=accept(u,self.binding)
        self.assertTrue(result['repeated']);self.assertEqual(current_revision(work).number,number)
        self.assertEqual((len(self.transport.messages),len(self.transport.acks)),counts);self.assertEqual(ReviewSession.objects.get().nonce,nonce)

    def test_same_callback_id_changed_payload_is_conflict(self):
        self.photo();_,u=self.click('Подтвердить');u['update_id']+=1;u['callback_query']['data']=self.button('Подтвердить')
        with self.assertRaises(GatewayError) as e:accept(u,self.binding)
        self.assertEqual(e.exception.status,409)

    def test_old_button_after_mutation_or_cancel_cannot_apply(self):
        work=self.photo();self.field(1);old=self.button('Верно');self.click('Верно');number=current_revision(work).number
        self.assertIn('устарели',accept(self.callback(data=old),self.binding)['text']);self.assertEqual(current_revision(work).number,number)
        self.click('Исправить');cancel=self.button('Отмена');self.click('Отмена')
        self.assertEqual(ReviewSession.objects.get().panel,'field')
        self.assertIn('устарели',accept(self.callback(data=cancel),self.binding)['text']);self.assertEqual(current_revision(work).number,number)

    def test_foreign_sender_chat_bot_and_malformed_callback_have_no_effect(self):
        work=self.photo();before=current_revision(work).number;counts=(len(self.transport.messages),len(self.transport.acks))
        for what in ('sender','chat','bot','data','inline'):
            u=self.callback(label='Подтвердить')
            if what=='sender':u['callback_query']['from']['id']=200002
            if what=='chat':u['callback_query']['message']['chat']['id']=200002
            if what=='bot':u['callback_query']['message']['from']['id']=999
            if what=='data':u['callback_query']['data']='SYNTHETIC_PRIVATE_BAD'
            if what=='inline':u['callback_query']['inline_message_id']='synthetic'
            with self.assertRaises(GatewayError):accept(u,self.binding)
        self.assertEqual(current_revision(work).number,before);self.assertEqual((len(self.transport.messages),len(self.transport.acks)),counts)

    def test_changed_organization_context_is_denied_without_field_or_reply_changes(self):
        work=self.photo();u=self.callback(label='Подтвердить');counts=(len(self.transport.messages),len(self.transport.acks))
        OrganizationIdentity.objects.filter(pk=1).update(organization_id=uuid.uuid4())
        with self.assertRaises(GatewayError):accept(u,self.binding)
        self.assertIsNone(current_revision(work).confirmed_at);self.assertEqual((len(self.transport.messages),len(self.transport.acks)),counts)

    def test_new_photo_cancels_pending_edit_and_old_callback_does_not_switch_back(self):
        first=self.photo();self.field(4);self.click('Исправить');old=self.button('Отмена');second=self.photo()
        self.text('190000');self.assertEqual(current_revision(second).fields['budget']['value'],'180000')
        self.assertIn('устарели',accept(self.callback(data=old),self.binding)['text']);self.assertEqual(ReviewSession.objects.get().review_id,second.pk)
        self.assertEqual(current_revision(first).fields['budget']['value'],'180000')

    def test_web_revision_change_rejects_pending_value(self):
        work=self.photo();self.field(4);self.click('Исправить');rev=current_revision(work)
        fields={k:v for k,v in rev.fields.items() if k!='_recognition'};fields['wishes']['value']='Вымышленная ручная правка';fields['wishes']['state']='known'
        save_fields(work,self.owner,rev.number,rev.content_hash,fields)
        self.text('190000');self.assertEqual(current_revision(work).fields['budget']['value'],'180000');self.assertIn('устарели',self.transport.messages[-1][1])

    def test_invalid_budget_retains_edit_and_cancel_does_not_change_revision(self):
        work=self.photo();number=current_revision(work).number;self.field(4);self.click('Исправить');self.text('200 USD')
        self.assertEqual(ReviewSession.objects.get().panel,'edit');self.assertEqual(current_revision(work).number,number)
        self.click('Отмена');self.assertEqual(current_revision(work).number,number)
        self.text('190000');self.assertEqual(current_revision(work).number,number)

    def test_pending_text_duplicate_does_not_repeat_edit(self):
        work=self.photo();self.field(4);self.click('Исправить');_,u=self.text('185000');number=current_revision(work).number;sent=len(self.transport.messages)
        accept(u,self.binding);self.assertEqual(current_revision(work).number,number);self.assertEqual(len(self.transport.messages),sent)

    def test_stop_and_revocation_block_callbacks_and_pending_text(self):
        work=self.photo();self.field(4);self.click('Исправить');u=self.callback(label='Отмена');number=current_revision(work).number
        stop=Path(self.tmp.name)/'STOP';stop.touch()
        with self.assertRaises(GatewayError):accept(u,self.binding)
        with self.assertRaises(GatewayError):self.text('185000')
        stop.unlink();get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        with self.assertRaises(GatewayError):accept(u,self.binding)
        self.assertEqual(current_revision(work).number,number)

    def test_receipt_failure_rolls_back_field_and_session(self):
        work=self.photo();self.field(4);self.click('Исправить');old=ReviewSession.objects.get().nonce;number=current_revision(work).number
        with patch('telegram_gateway.review_ui.UpdateReceipt.objects.create',side_effect=RuntimeError('SYNTHETIC_INTERNAL')):
            with self.assertRaises(RuntimeError):self.text('185000')
        self.assertEqual(current_revision(work).number,number);self.assertEqual(ReviewSession.objects.get().nonce,old)
        self.assertEqual(ReviewSession.objects.get().panel,'edit')

    def test_ack_timeout_is_not_retried_and_does_not_echo_secret(self):
        self.photo();self.transport.fail_ack=True;result,u=self.click('Подтвердить');count=len(self.transport.acks)
        self.assertEqual(result['callback_ack_state'],'unknown');self.assertNotIn('SECRET',result['text'])
        accept(u,self.binding);self.assertEqual(len(self.transport.acks),count)

    def test_hints_require_confirmed_history_and_explicit_click(self):
        first=self.photo();self.field(4);self.click('Исправить');self.text('185000');self.click('Назад')
        second=self.photo();self.field(4)
        self.assertFalse(any(b['text']=='Применить подсказку' for row in self.transport.keyboards[-1]['inline_keyboard'] for b in row))
        # Explicitly confirm the historical correction using existing revision service.
        rev=current_revision(first);fields={k:v for k,v in rev.fields.items() if k!='_recognition'}
        fields['party']['state']='known';fields['party']['source']['kind']='manual_review'
        save_fields(first,self.owner,rev.number,rev.content_hash,fields,confirm=True)
        self.text('ПОЛЯ');self.field(4)
        self.assertEqual(current_revision(second).fields['budget']['value'],'180000');self.assertIn('Не применена',self.transport.messages[-1][1])
        self.click('Применить подсказку');self.assertEqual(current_revision(second).fields['budget']['value'],'185000');self.assertIsNone(current_revision(second).confirmed_at)

    def test_rejected_new_photo_clears_old_pending_edit(self):
        work=self.photo();self.field(4);self.click('Исправить')
        self.transport.content=b'not an image';self.photo()
        self.assertEqual(ReviewSession.objects.get().panel,'summary')
        self.text('190000');self.assertEqual(current_revision(work).fields['budget']['value'],'180000')

    def test_stop_or_changed_company_prevents_photo_download_and_storage(self):
        self.photo();before=(WorksheetReview.objects.count(),len(self.transport.downloads))
        stop=Path(self.tmp.name)/'STOP';stop.touch()
        with self.assertRaises(GatewayError):self.photo()
        stop.unlink();OrganizationIdentity.objects.filter(pk=1).update(organization_id=uuid.uuid4())
        with self.assertRaises(GatewayError):self.photo()
        self.assertEqual((WorksheetReview.objects.count(),len(self.transport.downloads)),before)

    def test_hidden_long_hint_has_no_confirmation_action(self):
        self.photo();self.field(4)
        with patch('telegram_gateway.review_ui.suggestions',return_value={'budget':'x'*3100}):
            self.text('ПОЛЯ');self.field(4)
        labels=[b['text'] for row in self.transport.keyboards[-1]['inline_keyboard'] for b in row]
        self.assertEqual(labels,['Назад']);self.assertIn('скрытое значение',self.transport.messages[-1][1])

    def test_other_owner_cannot_use_saved_keyboard_or_get_fields(self):
        work=self.photo();u=self.callback(label='Подтвердить');counts=(len(self.transport.messages),len(self.transport.acks))
        other=replace(self.binding,owner_id=self.other.pk,telegram_user_id=200002,chat_id=200002)
        with self.assertRaises(GatewayError):accept(u,other)
        self.assertIsNone(current_revision(work).confirmed_at);self.assertEqual((len(self.transport.messages),len(self.transport.acks)),counts)

    def test_forged_action_for_current_panel_does_not_confirm(self):
        work=self.photo();self.field(4);self.click('Исправить')
        data='iz:'+ReviewSession.objects.get().nonce.hex+':confirm'
        result=accept(self.callback(data=data),self.binding)
        self.assertIn('устарели',result['text']);self.assertIsNone(current_revision(work).confirmed_at)
        self.assertEqual(ReviewSession.objects.get().panel,'edit')

    def test_polling_stops_on_uncertain_callback_ack(self):
        from .polling import run
        self.photo();update=self.callback(label='Подтвердить');self.transport.fail_ack=True
        class Source:
            def poll_status(self):return {'bot_id':123,'webhook_set':False}
            def get_updates(self,offset):return [update]
        binding=replace(self.binding,mode='polling',webhook_secret=None,review_origin='')
        result=run(binding,Source(),self.tmp.name,bot_id=123,organization_id=self.org.organization_id,enabled=True,max_cycles=2)
        self.assertEqual(result,{'status':'reply_uncertain','processed':1});self.assertEqual(len(self.transport.acks),1)

class ButtonHTTPTests(SimpleTestCase):
    def test_keyboard_and_ack_use_fixed_safe_operations(self):
        def response(v):return raw_response(json.dumps({'ok':True,'result':v}).encode())
        fixture=HTTPFixture([response({'message_id':1,'chat':{'id':100001}}),response(True)])
        t=TelegramHTTPTransport(lambda:TOKEN,enabled=True,runner=fixture.run)
        keyboard={'inline_keyboard':[[{'text':'Верно','callback_data':'iz:'+'a'*32+':ok'}]]}
        t.send_buttons(100001,'Синтетическая сверка',keyboard,timeout_seconds=10);t.answer_callback('cb_123','Обработано',timeout_seconds=10)
        self.assertIn(b'reply_markup',fixture.requests()[0]);self.assertIn(b'answerCallbackQuery',fixture.requests()[1])
        self.assertNotIn(b'"url"',fixture.requests()[1])

    def test_arbitrary_urls_extra_fields_and_bad_data_refused_before_runner(self):
        t=TelegramHTTPTransport(lambda:TOKEN,enabled=True,runner=lambda *a:self.fail('worker'))
        for b in ({'text':'Ссылка','url':'https://synthetic.invalid'}, {'text':'Верно','callback_data':'bad'}, {'text':'Верно','callback_data':'iz:'+'a'*32+':ok','extra':'bad'}):
            with self.assertRaises(TelegramTransportError):t.send_buttons(100001,'test',{'inline_keyboard':[[b]]},timeout_seconds=10)
        with self.assertRaises(TelegramTransportError):t.answer_callback('bad:token','test',timeout_seconds=10)
