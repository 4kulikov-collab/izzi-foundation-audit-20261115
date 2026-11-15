"""Chat-interface contract tests. Transport is fake; real OCR has a separate explicit test."""
import json
from unittest import skipUnless
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
from unittest.mock import patch
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase,override_settings
from django.urls import reverse
from travelcards.forms import FIELD_LABELS,normalize_fields
from travelcards.ocr import _empty_fields,russian_tesseract_available
from worksheet_reviews.models import WorksheetReview
from worksheet_reviews.ocr_services import current_revision,save_fields
from inquiries.models import Inquiry
from .models import UpdateReceipt
from .chat_review import suggestions,review_token
from config.diagnostics import read_events,emit


def synthetic_fields():
    f=_empty_fields('local_tesseract','Полностью вымышленный результат для теста контракта, не OCR фото.')
    f['party'].update(value='двое взрослых',state='ocr_candidate')
    f['budget'].update(value='180000',state='ocr_candidate')
    return f


class ChatReviewTests(TransactionTestCase):
    def setUp(self):
        from .tests import TelegramGatewayTests
        TelegramGatewayTests.setUp(self)
        self.binding=replace(self.binding,in_chat_review=True)
        self.chat_scope=override_settings(TELEGRAM_GATEWAY=self.binding);self.chat_scope.enable();self.addCleanup(self.chat_scope.disable)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.log_scope=override_settings(SANDBOX_DIAGNOSTICS_ENABLED=True,DATA_DIR=Path(self.tmp.name));self.log_scope.enable();self.addCleanup(self.log_scope.disable)
        self.seq=1000

    def post(self,update=None,**kw):
        return self.client.post(self.url,json.dumps(self.update if update is None else update),content_type='application/json',HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=self.binding.webhook_secret,**kw)

    def photo(self):
        self.update['update_id']+=1;self.update['message']['message_id']+=1
        with patch('worksheet_reviews.ocr_services.recognize_card_photo',return_value=(synthetic_fields(),{'status':'recognized','engine':'synthetic-test-stub'})):
            response=self.post()
        self.assertEqual(response.status_code,200)
        return WorksheetReview.objects.latest('created_at')

    def command(self,text,repeat=None):
        self.seq+=1
        u={'update_id':self.seq,'message':{'message_id':self.seq,'from':{'id':100001,'is_bot':False},'chat':{'id':100001,'type':'private'},'text':text}}
        if repeat:u=repeat
        return self.post(u),u

    def token(self,work):return review_token(work,current_revision(work))

    def corrected(self,work,value='185000'):
        self.assertEqual(self.command(f'ИСПРАВИТЬ {self.token(work)} 4 {value}')[0].status_code,200)
        self.command(f'ВЕРНО {self.token(work)} 1')
        self.command(f'ПОДТВЕРДИТЬ {self.token(work)}')
        self.assertIsNotNone(current_revision(work).confirmed_at)

    def test_chat_fields_edit_confirm_and_duplicate_use_existing_persistence(self):
        work=self.photo();original=bytes(work.worksheet_bytes)
        self.assertIn('Кандидат OCR',self.transport.messages[-1][1])
        self.assertIn('Лист ожидает сверки',self.transport.messages[-1][1])
        self.command(f'ПОДТВЕРДИТЬ {self.token(work)}')
        self.assertIsNone(current_revision(work).confirmed_at)
        self.assertTrue(any(e['code']=='pending_review' for e in read_events(self.tmp.name)))
        response,u=self.command(f'ИСПРАВИТЬ {self.token(work)} 4 185000')
        revision=current_revision(work);count=work.ocr_revisions.count();sent=len(self.transport.messages)
        self.assertEqual(normalize_fields(revision.fields)['budget']['value'],'185000')
        self.command('ignored',repeat=u)
        self.assertEqual(work.ocr_revisions.count(),count);self.assertEqual(len(self.transport.messages),sent)
        self.command(f'ВЕРНО {self.token(work)} 1');self.command(f'ПОДТВЕРДИТЬ {self.token(work)}')
        self.assertIsNotNone(current_revision(work).confirmed_at)
        work.refresh_from_db();self.assertEqual(bytes(work.worksheet_bytes),original)
        self.assertEqual(Inquiry.objects.count(),0)
        self.assertIn('U-ON: не синхронизировано',self.transport.messages[-1][1])

    def test_stale_token_and_switch_to_new_photo_do_not_edit_wrong_work(self):
        first=self.photo();token=self.token(first)
        self.command(f'ИСПРАВИТЬ {token} 4 185000')
        self.command(f'НЕИЗВЕСТНО {token} 4')
        self.assertEqual(normalize_fields(current_revision(first).fields)['budget']['value'],'185000')
        second=self.photo()
        self.assertNotEqual(first.pk,second.pk)
        before=current_revision(second).content_hash
        self.command(f'ИСПРАВИТЬ {token} 4 999999')
        self.assertEqual(current_revision(second).content_hash,before)
        self.assertNotEqual(token,self.token(second))

    def test_unknown_is_not_filled_and_bad_currency_or_commands_are_rejected(self):
        w=self.photo();self.command(f'НЕИЗВЕСТНО {self.token(w)} 4')
        self.assertEqual(normalize_fields(current_revision(w).fields)['budget']['state'],'unknown')
        for command in (f'ИСПРАВИТЬ {self.token(w)} 4 200 USD',f'ВЕРНО {self.token(w)} 2',f'ПРИМЕНИТЬ {self.token(w)} 4'):
            before=current_revision(w).content_hash;self.command(command);self.assertEqual(current_revision(w).content_hash,before)

    def test_retrieval_is_owner_local_confirmed_exact_context_and_never_auto_applied(self):
        first=self.photo();self.corrected(first)
        second=self.photo();hints=suggestions(second,self.owner)
        self.assertEqual(hints,{'budget':'185000'})
        self.assertEqual(normalize_fields(current_revision(second).fields)['budget']['value'],'180000')
        self.assertEqual(suggestions(second,self.other),{})
        self.command(f'ПРИМЕНИТЬ {self.token(second)} 4')
        self.assertEqual(normalize_fields(current_revision(second).fields)['budget']['value'],'185000')
        self.assertIsNone(current_revision(second).confirmed_at)

    def test_conflicting_confirmed_corrections_disable_suggestion(self):
        a=self.photo();self.corrected(a,'185000')
        b=self.photo();self.corrected(b,'190000')
        c=self.photo();self.assertEqual(suggestions(c,self.owner),{})

    def test_unconfirmed_correction_does_not_teach_next_sheet(self):
        a=self.photo();self.command(f'ИСПРАВИТЬ {self.token(a)} 4 185000')
        b=self.photo();self.assertEqual(suggestions(b,self.owner),{})

    def test_errors_timeout_and_corrupt_photo_keep_safe_journal(self):
        self.transport.fail_download=True
        self.assertEqual(self.post().status_code,503)
        self.transport.fail_download=False;self.transport.content=b'PRIVATE_IMAGE_CONTENT'
        self.assertEqual(self.post().json()['status'],'image_rejected')
        text=Path(self.tmp.name,'diagnostics.jsonl').read_text()
        for value in ('PRIVATE_IMAGE_CONTENT','SYNTHETIC_TOKEN','UNTRUSTED_IGNORED_CAPTION','100001','file_id','chat_id'):self.assertNotIn(value,text)
        events=read_events(self.tmp.name)
        self.assertTrue(any(x['code']=='file_unavailable' for x in events))
        emit('ocr','PRIVATE_TEXT');self.assertEqual(read_events(self.tmp.name),events)

    def test_other_and_revoked_user_do_not_read_fields_or_corrections(self):
        w=self.photo();u={'update_id':999,'message':{'message_id':999,'from':{'id':111,'is_bot':False},'chat':{'id':111,'type':'private'},'text':'ПОЛЯ'}}
        self.assertEqual(self.post(u).status_code,403)
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertEqual(self.command('ПОЛЯ')[0].status_code,403)

    @skipUnless(russian_tesseract_available(), "Tesseract/rus unavailable; real photo test NOT RUN, no mock substitute")
    def test_real_local_tesseract_photo_unknowns_and_manual_correction(self):
        self.assertTrue(russian_tesseract_available(),'Для приёмки нужен уже установленный Tesseract/rus; не подменять mock')
        original=(Path(settings.BASE_DIR)/'docs/fixtures/photo-ocr/printed-exif-1.jpg').read_bytes()
        self.transport.content=original;self.update['message']['document'].update(mime_type='image/jpeg',file_size=len(original))
        self.assertEqual(self.post().status_code,200)
        work=WorksheetReview.objects.get();rev=current_revision(work);fields=normalize_fields(rev.fields)
        self.assertEqual(rev.metadata['engine'],'Tesseract');self.assertEqual(rev.metadata['status'],'recognized')
        self.assertEqual(fields['budget']['value'],'180000');self.assertEqual(fields['children_count']['state'],'unknown')
        self.assertIsNone(rev.confirmed_at);self.assertEqual(bytes(work.worksheet_bytes),original)
        self.command(f'ИСПРАВИТЬ {self.token(work)} 7 тихий номер вдали от дороги')
        self.assertEqual(normalize_fields(current_revision(work).fields)['wishes']['value'],'тихий номер вдали от дороги')
        self.assertIsNone(current_revision(work).confirmed_at)
        initial_text=self.transport.messages[0][1]
        for index,key in enumerate(FIELD_LABELS,1):
            if normalize_fields(current_revision(work).fields)[key]['state']=='ocr_candidate':
                self.command(f'ВЕРНО {self.token(work)} {index}')
        self.command(f'ПОДТВЕРДИТЬ {self.token(work)}')
        self.assertIsNotNone(current_revision(work).confirmed_at)
        confirmed_text=self.transport.messages[-1][1]
        # Repeat the same printed synthetic image as a NEW message; this is not independent generalization.
        self.update['update_id']+=1;self.update['message']['message_id']+=1
        self.assertEqual(self.post().status_code,200)
        next_work=WorksheetReview.objects.exclude(pk=work.pk).get()
        self.assertEqual(suggestions(next_work,self.owner),{'wishes':'тихий номер вдали от дороги'})
        self.assertEqual(normalize_fields(current_revision(next_work).fields)['wishes']['value'],'тихий номер')
        hint_text=self.transport.messages[-1][1]
        self.assertIn(f'ПРИМЕНИТЬ {self.token(next_work)} 7 — принять эту подсказку',hint_text)
        self.assertNotIn(f'ПРИМЕНИТЬ {self.token(next_work)} 4',hint_text)
        self.command(f'ПРИМЕНИТЬ {self.token(next_work)} 7')
        self.assertEqual(normalize_fields(current_revision(next_work).fields)['wishes']['value'],'тихий номер вдали от дороги')
        self.assertIsNone(current_revision(next_work).confirmed_at)
        import os
        output=os.environ.get('IZZI_CHAT_PROOF')
        if output:
            Path(output).write_text(json.dumps({'telegram_transport':'synthetic fixture; NOT live delivery',
                'ocr':'real local Tesseract/rus on printed synthetic image','handwriting_tested':False,
                'same_image_repeat_not_independent_holdout':True,'new_message_hint_count':1,
                'auto_applied':False,'original_unchanged':bytes(next_work.worksheet_bytes)==original,
                'initial_message':initial_text,'confirmed_message':confirmed_text,'next_sheet_hint_message':hint_text},ensure_ascii=False,indent=2))

    def test_unavailable_ocr_preserves_original_and_unknown_status(self):
        with patch('worksheet_reviews.ocr_services.recognize_card_photo',side_effect=TimeoutError('PRIVATE_TEXT')):
            response=self.post()
        self.assertEqual(response.status_code,200);self.assertNotIn('PRIVATE_TEXT',response.json()['text'])
        fields=normalize_fields(current_revision(WorksheetReview.objects.get()).fields)
        self.assertTrue(all(v['state']=='unknown' and not v['value'] for v in (fields[k] for k in FIELD_LABELS)))

    def test_disabled_model_adapter_uses_common_fields_without_calling_local_ocr(self):
        from intelligence.fields import DisabledAdapter
        with override_settings(IZZI_IMAGE_ADAPTER=DisabledAdapter()),patch('worksheet_reviews.ocr_services.recognize_card_photo') as local:
            response=self.post();local.assert_not_called()
        self.assertEqual(response.status_code,200)
        rev=current_revision(WorksheetReview.objects.get())
        self.assertEqual(rev.metadata['status'],'unavailable');self.assertIsNone(rev.confirmed_at)

    def test_measured_synthetic_retrieval_matrix_not_ocr_accuracy(self):
        teacher=self.photo();self.corrected(teacher,'185000')
        same=self.photo();positive=suggestions(same,self.owner)
        different=self.photo();f=synthetic_fields();f['party']['value']='трое взрослых'
        from worksheet_reviews.ocr_services import _create_revision
        _create_revision(different,current_revision(different).number+1,f,{'status':'recognized','engine':'synthetic-test-stub'},'')
        unrelated=suggestions(different,self.owner)
        unknown=self.photo();f=synthetic_fields();f['budget'].update(value='',state='unknown')
        _create_revision(unknown,current_revision(unknown).number+1,f,{'status':'recognized','engine':'synthetic-test-stub'},'')
        absent=suggestions(unknown,self.owner)
        conflict=self.photo();self.corrected(conflict,'190000');last=self.photo();ambiguous=suggestions(last,self.owner)
        self.assertEqual(positive,{'budget':'185000'})
        self.assertEqual((unrelated,absent,ambiguous),({},{},{}))
        import os
        path=os.environ.get('IZZI_CORRECTION_EFFECT_REPORT')
        if path:
            Path(path).write_text(json.dumps({'method':'synthetic OCR results, not photo recognition or model retraining',
                'positive_cases':1,'correct_suggestions':len(positive),'negative_cases':3,
                'false_suggestions':sum(len(x) for x in (unrelated,absent,ambiguous)),
                'automatic_field_changes':0,'manual_confirmation_required':True},ensure_ascii=False,indent=2))
