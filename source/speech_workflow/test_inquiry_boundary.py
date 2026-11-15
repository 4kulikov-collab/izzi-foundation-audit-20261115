import hashlib
import traceback
import uuid
from dataclasses import replace
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from inquiries.models import Inquiry, Version, WorkLink, SelectionLink
from .adapters import (AdapterDisabled, InquiryDraftResult, MockVoiceInquiryAdapter, MockSpeechKitAdapter,
                       TranscriptResult, VoiceReplyResult, MockLunaVoiceReplyAdapter, ProviderFailure)
from .contracts import FIELDS
from .models import VoiceSubmission, VoiceInquiryDraft
from .services import (create_submission, current_revision, transcribe_submission, confirm_transcript,
                       edit_transcript, revoke_transcript_confirmation, cancel_submission, apply_transcription_result)
from .inquiry_boundary import prepare_inquiry_draft, confirm_inquiry_draft, synthesize_reply
from .tests import wav_upload


@override_settings(VOICE_INQUIRY_BOUNDARY_ENABLED=True)
class VoiceInquiryBoundaryTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('synthetic-voice-manager')
        self.other = get_user_model().objects.create_user('synthetic-other-manager')
        self.data = {k: '' for k in FIELDS}
        self.data.update(party='Два взрослых', budget='180000', wishes='Тихий номер')
        self.reply = 'Пожелания записаны в локальный черновик. Даты нужно уточнить.'

    def tearDown(self):
        for item in VoiceSubmission.objects.all():
            item.audio.delete(save=False)
        super().tearDown()

    def source(self, confirmed=True):
        item = create_submission(self.owner, uuid.uuid4(), wav_upload())
        rev = current_revision(item)
        rev = transcribe_submission(item.pk, self.owner, rev.number, rev.content_hash,
            adapter=MockSpeechKitAdapter(TranscriptResult(text='Синтетическая речь: два взрослых, 180000 рублей, тихий номер.',
                uncertainties=('даты не указаны',))))
        if confirmed:
            confirm_transcript(item.pk, self.owner, rev.number, rev.content_hash)
        return item, rev

    def draft(self, target=None):
        item, rev = self.source()
        draft = prepare_inquiry_draft(item.pk, self.owner, rev.number, rev.content_hash,
            target_inquiry_id=target.pk if target else None,
            adapter=MockVoiceInquiryAdapter(InquiryDraftResult(self.data, self.reply)))
        return item, rev, draft

    def confirm(self, draft):
        return confirm_inquiry_draft(draft.pk, self.owner, draft.content_hash,
            reviewed_fields=[k for k,v in self.data.items() if v], accept_unknowns=True, reply_reviewed=True)

    def voice(self):
        return VoiceReplyResult(wav_upload(value=42).read(), hashlib.sha256(self.reply.encode()).hexdigest())

    @override_settings(VOICE_INQUIRY_BOUNDARY_ENABLED=False)
    def test_default_boundary_off(self):
        item, rev = self.source()
        with self.assertRaises(AdapterDisabled):
            prepare_inquiry_draft(item.pk, self.owner, rev.number, rev.content_hash,
                adapter=MockVoiceInquiryAdapter(InquiryDraftResult(self.data)))
        self.assertEqual(VoiceInquiryDraft.objects.count(), 0)

    def test_defaults_never_call_live_providers(self):
        item, rev = self.source()
        with self.assertRaises(AdapterDisabled):
            prepare_inquiry_draft(item.pk, self.owner, rev.number, rev.content_hash)
        _, _, draft = self.draft(); self.confirm(draft)
        with self.assertRaises(AdapterDisabled):
            synthesize_reply(draft.pk, self.owner, draft.content_hash)

    def test_complete_synthetic_flow_preserves_source_and_never_writes_ledger(self):
        item, rev, draft = self.draft()
        rev.refresh_from_db()
        self.assertTrue(rev.is_mock)
        self.assertIsNone(draft.confirmed_at)
        self.assertEqual(draft.fields['dates'], {'value': '', 'state': 'unknown'})
        self.assertEqual(draft.transcript_revision_id, rev.pk)
        self.confirm(draft)
        saved = VoiceInquiryDraft.objects.get(pk=draft.pk)
        self.assertEqual(saved.content_hash, draft.content_hash)
        self.assertIsNotNone(saved.confirmed_at)
        response = synthesize_reply(saved.pk, self.owner, saved.content_hash,
            adapter=MockLunaVoiceReplyAdapter(self.voice()))
        self.assertTrue(response['is_mock'])
        self.assertIn('речь не синтезирована', response['message'])
        with item.audio.open('rb') as original:
            self.assertEqual(hashlib.sha256(original.read()).hexdigest(), item.audio_sha256)
        self.assertEqual((Inquiry.objects.count(), Version.objects.count(), WorkLink.objects.count(), SelectionLink.objects.count()), (0,0,0,0))

    def test_requires_two_explicit_confirmations(self):
        item, rev = self.source(confirmed=False)
        with self.assertRaises(ValidationError):
            prepare_inquiry_draft(item.pk, self.owner, rev.number, rev.content_hash)
        _, _, draft = self.draft()
        with self.assertRaises(ValidationError):
            synthesize_reply(draft.pk, self.owner, draft.content_hash, adapter=MockLunaVoiceReplyAdapter(self.voice()))
        for kwargs in ({'reviewed_fields': []}, {'reviewed_fields':['party','budget','wishes']},
                       {'reviewed_fields':['party','budget','wishes'], 'accept_unknowns':True}):
            with self.assertRaises(ValidationError):
                confirm_inquiry_draft(draft.pk, self.owner, draft.content_hash, **kwargs)
        draft.refresh_from_db(); self.assertIsNone(draft.confirmed_at)

    def test_repeat_keeps_same_draft_target_and_does_not_call_adapter(self):
        target=Inquiry.objects.create(owner=self.owner, traveller_label='Синтетическое обращение')
        item, rev, draft=self.draft(target)
        class Never:
            def draft_inquiry(self, text):
                raise AssertionError('A duplicate must not call provider')
        again=prepare_inquiry_draft(item.pk,self.owner,rev.number,rev.content_hash,target_inquiry_id=target.pk,adapter=Never())
        self.assertEqual(again.pk,draft.pk)
        another=Inquiry.objects.create(owner=self.owner, traveller_label='Другое синтетическое')
        with self.assertRaises(ValidationError):
            prepare_inquiry_draft(item.pk,self.owner,rev.number,rev.content_hash,target_inquiry_id=another.pk)
        _,_,second=self.draft(target)
        self.assertEqual(second.target_inquiry_id,draft.target_inquiry_id)
        self.assertEqual(Inquiry.objects.count(),2)
        self.assertEqual(Version.objects.count(),0)

    def test_foreign_owner_target_and_inactive_access_rejected(self):
        item,rev,draft=self.draft()
        foreign=Inquiry.objects.create(owner=self.other, traveller_label='Синтетическое чужое')
        with self.assertRaises(PermissionDenied):
            prepare_inquiry_draft(item.pk,self.owner,rev.number,rev.content_hash,target_inquiry_id=foreign.pk)
        with self.assertRaises(PermissionDenied):
            confirm_inquiry_draft(draft.pk,self.other,draft.content_hash,reviewed_fields=[])
        self.owner.is_active=False; self.owner.save(update_fields=['is_active'])
        with self.assertRaises(PermissionDenied):self.confirm(draft)

    def test_changed_hash_and_tampered_fields_rejected(self):
        _,_,draft=self.draft()
        with self.assertRaises(ValidationError):
            confirm_inquiry_draft(draft.pk,self.owner,'0'*64,reviewed_fields=[])
        VoiceInquiryDraft.objects.filter(pk=draft.pk).update(reply_text='Подменённое содержание')
        with self.assertRaises(ValidationError):self.confirm(draft)

    def mutate(self, kind, item, rev):
        if kind=='edit':edit_transcript(item.pk,self.owner,rev.number,rev.content_hash,'Новая синтетическая редакция')
        elif kind=='revoke':revoke_transcript_confirmation(item.pk,self.owner,rev.number,rev.content_hash)
        elif kind=='cancel':cancel_submission(item.pk,self.owner)
        else:get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)

    def test_late_draft_rejected_after_edit_revoke_cancel_or_access_change(self):
        for kind in ('edit','revoke','cancel','deactivate'):
            with self.subTest(kind=kind):
                item,rev=self.source()
                outer=self
                class Late:
                    def draft_inquiry(self,text):
                        outer.mutate(kind,item,rev)
                        return InquiryDraftResult(outer.data)
                with self.assertRaises((ValidationError,PermissionDenied)):
                    prepare_inquiry_draft(item.pk,self.owner,rev.number,rev.content_hash,adapter=Late())
                self.assertFalse(VoiceInquiryDraft.objects.filter(transcript_revision=rev).exists())

    def test_late_voice_rejected_after_edit_revoke_cancel_or_access_change(self):
        for kind in ('edit','revoke','cancel','deactivate'):
            with self.subTest(kind=kind):
                item,rev,draft=self.draft();self.confirm(draft)
                outer=self
                class Late:
                    def synthesize(self,text,*,timeout_seconds):
                        outer.mutate(kind,item,rev)
                        return outer.voice()
                with self.assertRaises((ValidationError,PermissionDenied)):
                    synthesize_reply(draft.pk,self.owner,draft.content_hash,adapter=Late())

    def test_invalid_draft_fields_and_non_rub_amounts_rejected(self):
        item,rev=self.source()
        variants=[{}, {**self.data,'phone':'synthetic'}, {**self.data,'budget':'100 USD'},
                  {**self.data,'budget':'-180000'}, {**self.data,'wishes':None}]
        for fields in variants:
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                prepare_inquiry_draft(item.pk,self.owner,rev.number,rev.content_hash,
                    adapter=MockVoiceInquiryAdapter(InquiryDraftResult(fields)))
        with self.assertRaises(ValidationError):
            prepare_inquiry_draft(item.pk,self.owner,rev.number,rev.content_hash,
                adapter=MockVoiceInquiryAdapter(InquiryDraftResult(self.data,is_mock=False)))
        self.assertEqual(VoiceInquiryDraft.objects.count(),0)

    def test_stt_schema_rejection_keeps_original_revision(self):
        item=create_submission(self.owner,uuid.uuid4(),wav_upload());rev=current_revision(item)
        valid=TranscriptResult('Тест')
        cases=[None, replace(valid,text=None), replace(valid,segments=({'start':float('nan'),'end':1,'text':'x'},)),
               replace(valid,uncertainties=('x'*601,)), replace(valid,language_code='not-ru'),
               replace(valid,segments=({'start':0,'end':1,'text':'x','token':'synthetic'},))]
        for result in cases:
            with self.subTest(result=result),self.assertRaises(ValidationError):
                apply_transcription_result(item.pk,self.owner,rev.number,rev.content_hash,result)
        self.assertEqual(item.revisions.count(),1)

    def test_corrupted_source_never_reaches_stt_or_draft(self):
        item,rev=self.source()
        with item.audio.storage.open(item.audio.name,'wb') as output:output.write(b'corrupted synthetic')
        with self.assertRaises(ValidationError):
            prepare_inquiry_draft(item.pk,self.owner,rev.number,rev.content_hash,
                adapter=MockVoiceInquiryAdapter(InquiryDraftResult(self.data)))
        other=create_submission(self.owner,uuid.uuid4(),wav_upload());first=current_revision(other)
        with other.audio.storage.open(other.audio.name,'wb') as output:output.write(wav_upload(value=4).read())
        with self.assertRaises(ValidationError):
            transcribe_submission(other.pk,self.owner,first.number,first.content_hash,
                adapter=MockSpeechKitAdapter(TranscriptResult('Не применять')))
        self.assertEqual(other.revisions.count(),1)

    def test_invalid_reply_binary_metadata_or_text_hash_rejected(self):
        _,_,draft=self.draft();self.confirm(draft);valid=self.voice()
        for result in (replace(valid,audio=b'OggSfake'),replace(valid,audio=b'x'*(5*1024*1024+1)),
                       replace(valid,text_sha256='0'*64),replace(valid,voice='unverified-real-voice'),
                       replace(valid,is_mock=False),replace(valid,media_type='text/html')):
            with self.subTest(meta=(result.media_type,len(result.audio))),self.assertRaises(ValidationError):
                synthesize_reply(draft.pk,self.owner,draft.content_hash,adapter=MockLunaVoiceReplyAdapter(result))

    def test_provider_errors_and_timeouts_do_not_leak_details(self):
        _,_,draft=self.draft();self.confirm(draft)
        marker='synthetic-secret-marker https://invalid.invalid/private'
        for failure in (TimeoutError(marker),RuntimeError(marker),ProviderFailure(marker),AdapterDisabled(marker)):
            class Broken:
                def synthesize(self,text,*,timeout_seconds):raise failure
            with self.assertRaises((ProviderFailure,AdapterDisabled)) as caught:
                synthesize_reply(draft.pk,self.owner,draft.content_hash,adapter=Broken())
            self.assertNotIn(marker,''.join(traceback.format_exception(caught.exception)))
        with patch('speech_workflow.inquiry_boundary.time.monotonic',side_effect=[0,16]):
            with self.assertRaises(ProviderFailure):
                synthesize_reply(draft.pk,self.owner,draft.content_hash,adapter=MockLunaVoiceReplyAdapter(self.voice()))

    def test_raw_stt_timeout_bounded_and_no_revision_created(self):
        item=create_submission(self.owner,uuid.uuid4(),wav_upload());rev=current_revision(item)
        class Timeout:
            calls=0
            def transcribe(self,audio):
                self.calls+=1
                raise TimeoutError('synthetic-secret-marker')
        adapter=Timeout()
        with self.assertRaises(ProviderFailure) as caught:
            transcribe_submission(item.pk,self.owner,rev.number,rev.content_hash,adapter=adapter)
        self.assertEqual(adapter.calls,2);self.assertEqual(item.revisions.count(),1)
        self.assertNotIn('synthetic-secret-marker',str(caught.exception))

    def test_manager_http_confirmation_labels_escape_and_owner_checks(self):
        self.data['wishes']='<script>synthetic</script>'
        item,rev,draft=self.draft()
        self.client.force_login(self.owner)
        url=reverse('speech_workflow:detail',args=[item.pk])
        page=self.client.get(url)
        self.assertContains(page,'<title>Голосовая заметка · TravelHub</title>',html=True)
        self.assertContains(page,'<h2>Черновик обращения из голоса</h2>',count=1,html=True)
        self.assertContains(page,'Заглушка')
        self.assertContains(page,'U-ON: не синхронизировано')
        self.assertContains(page,'&lt;script&gt;synthetic&lt;/script&gt;')
        self.assertNotContains(page,'<script>synthetic</script>')
        confirm_url=reverse('speech_workflow:confirm_inquiry_draft',args=[item.pk])
        data={'draft_id':str(draft.pk),'expected_hash':draft.content_hash,
              'reviewed_fields':['party','budget','wishes'],'accept_unknowns':'yes','reply_reviewed':'yes'}
        self.assertEqual(self.client.post(confirm_url,{**data,'expected_hash':'0'*64}).status_code,409)
        self.assertEqual(self.client.post(confirm_url,data).status_code,302)
        self.assertEqual(self.client.post(confirm_url,data).status_code,302)
        draft.refresh_from_db();self.assertIsNotNone(draft.confirmed_at)
        self.assertEqual(self.client.post(confirm_url,{'draft_id':'bad'}).status_code,404)
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.owner)
        self.assertEqual(csrf.post(confirm_url,data).status_code,403)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code,404)
        self.assertEqual(self.client.post(confirm_url,data).status_code,404)


    def test_migration_labels_legacy_mock_without_rewriting_content(self):
        from importlib import import_module
        from django.apps import apps
        item, revision = self.source()
        original_hash = revision.content_hash
        type(revision).objects.filter(pk=revision.pk).update(is_mock=False)
        migration = import_module('speech_workflow.migrations.0004_transcriptrevision_is_mock_voiceinquirydraft')
        migration.mark_existing_mocks(apps, None)
        revision.refresh_from_db()
        self.assertTrue(revision.is_mock)
        self.assertEqual(revision.content_hash, original_hash)
        self.assertIsNotNone(revision.confirmed_at)


    def test_changed_transcript_bytes_with_stored_hash_are_rejected(self):
        item, revision, draft = self.draft()
        type(revision).objects.filter(pk=revision.pk).update(text='Подмена синтетического текста без нового хеша')
        with self.assertRaises(ValidationError):
            self.confirm(draft)
        draft.refresh_from_db()
        self.assertIsNone(draft.confirmed_at)


    def test_existing_luna_llm_errors_are_sanitized_without_changing_revision(self):
        from .services import structure_confirmed_transcript
        item, revision = self.source()
        class BrokenLuna:
            def structure_observation(self, text):
                raise TimeoutError('synthetic-secret-marker https://invalid.invalid/private')
        with self.assertRaises(ProviderFailure) as caught:
            structure_confirmed_transcript(item.pk, self.owner, revision.number,
                                           revision.content_hash, adapter=BrokenLuna())
        self.assertEqual(caught.exception.category, 'timeout')
        self.assertNotIn('synthetic-secret-marker', ''.join(traceback.format_exception(caught.exception)))
        item.refresh_from_db()
        self.assertEqual(item.current_revision, revision.number)
        self.assertFalse(VoiceInquiryDraft.objects.exists())
