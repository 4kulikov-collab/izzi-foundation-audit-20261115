import hashlib
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import DatabaseError, connections
from django.test import TestCase, override_settings, Client
from django.urls import reverse
from presentations.models import Hotel, TravelRequest, Approval, Release
from presentations.services import approve_request, publish_request, build_snapshot, snapshot_hash
from speech_workflow import test_inquiry_boundary as voice_helpers
from speech_workflow.models import VoiceSubmission, VoiceInquiryDraft
from speech_workflow.services import edit_transcript, confirm_transcript, revoke_transcript_confirmation, cancel_submission
from speech_workflow.adapters import MockVoiceInquiryAdapter, InquiryDraftResult
from speech_workflow.inquiry_boundary import prepare_inquiry_draft
from worksheet_reviews.models import WorksheetReview
from .models import Inquiry, Version, Operation, WorkLink, SelectionLink, VoiceSourceLink, VoiceSelection, Confirmation
from .voice import prepare, attach_draft
from .services import source_file, confirm


@override_settings(VOICE_INQUIRY_BOUNDARY_ENABLED=True)
class VoiceLedgerTests(TestCase):
    source = voice_helpers.VoiceInquiryBoundaryTests.source
    draft = voice_helpers.VoiceInquiryBoundaryTests.draft
    confirm_draft = voice_helpers.VoiceInquiryBoundaryTests.confirm

    def setUp(self):
        self.owner = get_user_model().objects.create_user('synthetic-voice-ledger')
        self.other = get_user_model().objects.create_user('synthetic-other-ledger')
        self.data = {key:'' for key in ('party','children_count','dates','budget','trip_type','beach','wishes')}
        self.data.update(party='Два взрослых',budget='180000',wishes='Тихий номер')
        self.reply = 'Уточните даты поездки.'
        self.hotels = [Hotel.objects.create(name='Синтетический отель '+str(i),summary='Только синтетический пример',
            pros='Тестовое свойство',limitations='Не проверен',source_reference='Вымышленный локальный каталог') for i in range(2)]
        self.item,self.revision,self.voice_draft = self.draft()
        self.confirm_draft(self.voice_draft)

    def tearDown(self):
        for item in VoiceSubmission.objects.all():item.audio.delete(save=False)
        super().tearDown()

    def prepare(self, **kwargs):
        args={'actor':self.owner,'draft_id':self.voice_draft.pk,'expected_hash':self.voice_draft.content_hash,
              'hotel_ids':[self.hotels[0].pk],'traveller_label':'Синтетическое обращение'}
        args.update(kwargs)
        return prepare(**args)

    def approve(self, selection):
        return approve_request(selection,self.owner,snapshot_hash(build_snapshot(selection)))

    def release(self, selection):
        return publish_request(selection,self.owner,self.approve(selection).pk)[0]

    def test_two_real_pdf_releases_one_inquiry_and_no_fake_worksheet(self):
        one=self.prepare();first=self.release(one)
        self.assertEqual(self.prepare().pk,one.pk)
        two=self.prepare(hotel_ids=[self.hotels[1].pk]);second=self.release(two)
        self.assertNotEqual(first.pk,second.pk)
        self.assertTrue(bytes(first.pdf_bytes).startswith(b'%PDF-'))
        self.assertTrue(bytes(second.pdf_bytes).startswith(b'%PDF-'))
        self.assertEqual(Inquiry.objects.count(),1)
        self.assertEqual((VoiceSourceLink.objects.count(),VoiceSelection.objects.count(),SelectionLink.objects.count()),(1,2,2))
        self.assertEqual((WorksheetReview.objects.count(),WorkLink.objects.count()),(0,0))
        versions=[v for v in Version.objects.all() if v.snapshot['release']]
        self.assertEqual(len(versions),2)
        for version in versions:
            self.assertEqual(version.snapshot['voice']['draft_hash'],self.voice_draft.content_hash)
            self.assertTrue(Confirmation.objects.filter(version=version).exists())
            raw,mime=source_file(actor=self.owner,version_id=version.pk)
            self.assertEqual(mime,'audio/wav');self.assertEqual(hashlib.sha256(raw).hexdigest(),self.item.audio_sha256)
        # No transcript/private file identifiers enter the customer HTML snapshot.
        self.assertNotIn(str(self.item.pk),first.html_content)
        self.assertNotIn(self.revision.text,first.html_content)
        self.assertNotIn('private-audio',first.html_content)

    def test_existing_inquiry_and_multiple_voice_sources_stay_together(self):
        target=Inquiry.objects.create(owner=self.owner,traveller_label='Существующее синтетическое')
        for _ in range(2):
            _,_,draft=self.draft(target);self.confirm_draft(draft)
            selection=prepare(actor=self.owner,draft_id=draft.pk,expected_hash=draft.content_hash,hotel_ids=[self.hotels[0].pk])
            self.assertEqual(SelectionLink.objects.get(selection=selection).inquiry_id,target.pk)
        self.assertEqual(Inquiry.objects.count(),1)
        self.assertEqual(VoiceSourceLink.objects.count(),2)

    def test_stale_hash_unconfirmed_and_foreign_actor_leave_no_records(self):
        with self.assertRaises(ValidationError):self.prepare(expected_hash='0'*64)
        with self.assertRaises(PermissionDenied):self.prepare(actor=self.other)
        VoiceInquiryDraft.objects.filter(pk=self.voice_draft.pk).update(confirmed_at=None)
        with self.assertRaises(ValidationError):self.prepare()
        self.assertEqual(Inquiry.objects.count(),0)

    def test_foreign_target_and_rebinding_rejected(self):
        # First source already bound; a later confirmed revision must retain that inquiry.
        selection=self.prepare();original=SelectionLink.objects.get(selection=selection).inquiry_id
        changed=edit_transcript(self.item.pk,self.owner,self.revision.number,self.revision.content_hash,'Новая синтетическая расшифровка')
        confirm_transcript(self.item.pk,self.owner,changed.number,changed.content_hash)
        other=Inquiry.objects.create(owner=self.owner,traveller_label='Другое обращение')
        newer=prepare_inquiry_draft(self.item.pk,self.owner,changed.number,changed.content_hash,target_inquiry_id=other.pk,
            adapter=MockVoiceInquiryAdapter(InquiryDraftResult(self.data,self.reply)))
        self.confirm_draft(newer)
        with self.assertRaises(ValidationError):prepare(actor=self.owner,draft_id=newer.pk,expected_hash=newer.content_hash,hotel_ids=[self.hotels[0].pk])
        self.assertEqual(VoiceSourceLink.objects.get(submission=self.item).inquiry_id,original)

    def test_revision_change_requires_new_draft_confirmation_and_presentation_approval(self):
        one=self.prepare();approval=self.approve(one)
        changed=edit_transcript(self.item.pk,self.owner,self.revision.number,self.revision.content_hash,'Обновлённые синтетические пожелания')
        with self.assertRaises(ValidationError):publish_request(one,self.owner,approval.pk)
        with self.assertRaises(ValidationError):self.prepare()
        confirm_transcript(self.item.pk,self.owner,changed.number,changed.content_hash)
        newer=prepare_inquiry_draft(self.item.pk,self.owner,changed.number,changed.content_hash,
            adapter=MockVoiceInquiryAdapter(InquiryDraftResult(self.data,self.reply)))
        with self.assertRaises(ValidationError):prepare(actor=self.owner,draft_id=newer.pk,expected_hash=newer.content_hash,hotel_ids=[self.hotels[1].pk])
        self.confirm_draft(newer)
        two=prepare(actor=self.owner,draft_id=newer.pk,expected_hash=newer.content_hash,hotel_ids=[self.hotels[1].pk])
        with self.assertRaises(ValidationError):publish_request(two,self.owner,approval.pk)
        second=self.release(two)
        self.assertTrue(second.pdf_hash)
        self.assertEqual(Inquiry.objects.count(),1)
        self.assertEqual(SelectionLink.objects.get(selection=one).inquiry_id,SelectionLink.objects.get(selection=two).inquiry_id)

    def test_revoked_or_cancelled_source_blocks_new_releases_but_history_unchanged(self):
        selection=self.prepare();release=self.release(selection);old_pdf=bytes(release.pdf_bytes)
        version=Version.objects.filter(snapshot__release__id=str(release.pk)).get()
        revoke_transcript_confirmation(self.item.pk,self.owner,self.revision.number,self.revision.content_hash)
        with self.assertRaises(ValidationError):self.approve(selection)
        with self.assertRaises(ValidationError):confirm(actor=self.owner,version_id=version.pk,expected_hash=version.digest)
        self.assertEqual(source_file(actor=self.owner,version_id=version.pk)[1],'audio/wav')
        cancel_submission(self.item.pk,self.owner)
        with self.assertRaises(ValidationError):self.prepare()
        release.refresh_from_db();self.assertEqual(bytes(release.pdf_bytes),old_pdf)

    def test_deactivated_owner_cannot_prepare_confirm_release_or_read_source(self):
        selection=self.prepare();approval=self.approve(selection);version=Version.objects.first()
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        for action in (lambda:self.prepare(),lambda:publish_request(selection,self.owner,approval.pk),
                       lambda:source_file(actor=self.owner,version_id=version.pk),
                       lambda:confirm(actor=self.owner,version_id=version.pk,expected_hash=version.digest)):
            with self.assertRaises(PermissionDenied):action()
        self.assertEqual(Release.objects.count(),0)

    def test_prepare_failure_rolls_back_inquiry_links_versions_and_operation(self):
        with patch('inquiries.voice.VoiceSelection.objects.create',side_effect=DatabaseError('synthetic fault')):
            with self.assertRaises(DatabaseError):self.prepare()
        for model in (Inquiry,VoiceSourceLink,VoiceSelection,Version,Operation,SelectionLink,TravelRequest):
            self.assertEqual(model.objects.count(),0,model.__name__)
        self.assertTrue(self.item.audio.storage.exists(self.item.audio.name))

    def test_attach_failure_rolls_back_new_inquiry_and_source_link(self):
        with patch('inquiries.voice.Version.objects.create',side_effect=DatabaseError('synthetic fault')):
            with self.assertRaises(DatabaseError):attach_draft(actor=self.owner,draft_id=self.voice_draft.pk,
                expected_hash=self.voice_draft.content_hash,traveller_label='Синтетическое')
        self.assertEqual((Inquiry.objects.count(),VoiceSourceLink.objects.count(),Operation.objects.count()),(0,0,0))

    def test_pdf_failure_and_ledger_failure_do_not_leave_partial_release(self):
        selection=self.prepare();approval=self.approve(selection);count=Version.objects.count()
        with patch('presentations.services.render_pdf',side_effect=RuntimeError('synthetic pdf fault')):
            with self.assertRaises(RuntimeError):publish_request(selection,self.owner,approval.pk)
        with patch('presentations.services.render_pdf',return_value=b'%PDF-synthetic-fault-path'),patch('inquiries.voice.confirm',side_effect=DatabaseError('synthetic ledger fault')):
            with self.assertRaises(DatabaseError):publish_request(selection,self.owner,approval.pk)
        self.assertEqual(Release.objects.count(),0);self.assertEqual(Version.objects.count(),count)
        self.assertEqual(Confirmation.objects.count(),0)
        # The same explicit approval can be retried after a transient failure.
        saved,_=publish_request(selection,self.owner,approval.pk)
        repeated,token=publish_request(selection,self.owner,approval.pk)
        self.assertEqual(saved.pk,repeated.pk);self.assertIsNone(token)
        self.assertEqual(Release.objects.count(),1)

    def test_access_or_source_revocation_during_render_rolls_back(self):
        selection=self.prepare();approval=self.approve(selection);count=Version.objects.count()
        for kind in ('deactivate','revoke'):
            def change(*args,**kwargs):
                if kind=='deactivate':get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
                else:revoke_transcript_confirmation(self.item.pk,self.owner,self.revision.number,self.revision.content_hash)
                return b'%PDF-synthetic-fault-path'
            with patch('presentations.services.render_pdf',side_effect=change):
                with self.assertRaises((ValidationError,PermissionDenied)):publish_request(selection,self.owner,approval.pk)
            self.assertEqual(Release.objects.count(),0);self.assertEqual(Version.objects.count(),count)

    def test_missing_audio_is_not_replaced_by_database_state(self):
        selection=self.prepare();version=Version.objects.first()
        self.item.audio.storage.delete(self.item.audio.name)
        with self.assertRaisesRegex(ValidationError,'не восстанавливает'):
            source_file(actor=self.owner,version_id=version.pk)
        with self.assertRaises(ValidationError):self.approve(selection)
        self.assertEqual(Inquiry.objects.count(),1)

    def test_tampered_requirements_or_missing_source_link_rejected(self):
        selection=self.prepare();selection.requirements='Подмена';selection.save()
        with self.assertRaises(ValidationError):self.approve(selection)
        self.assertEqual(Release.objects.count(),0)
        VoiceSourceLink.objects.all().delete()
        with self.assertRaises(ValidationError):self.approve(selection)

    @override_settings(VOICE_INQUIRY_BOUNDARY_ENABLED=False)
    def test_disabled_stage_cannot_attach(self):
        with self.assertRaises(ValidationError):self.prepare()
        self.assertEqual(Inquiry.objects.count(),0)

    def test_http_selection_director_overview_private_wav_and_csrf(self):
        self.client.force_login(self.owner)
        url=reverse('inquiries:voice_selection',args=[self.voice_draft.pk])
        self.assertContains(self.client.get(url),'SpeechKit и Luna отключены')
        data={'expected_hash':self.voice_draft.content_hash,'traveller_label':'Синтетическое обращение','hotels':[str(self.hotels[0].pk)]}
        self.assertEqual(self.client.post(url,data).status_code,302)
        self.assertEqual(self.client.post(url,data).status_code,302)
        inquiry=Inquiry.objects.get();version=Version.objects.first()
        voice_page=self.client.get(reverse('speech_workflow:detail',args=[self.item.pk]))
        self.assertContains(voice_page,'Повторная подборка в том же обращении')
        self.assertNotContains(voice_page,'новое обращение ещё не создано')
        self.assertContains(self.client.get(reverse('inquiries:detail',args=[inquiry.pk])),'Исходный WAV этой версии')
        file_url=reverse('inquiries:source',args=[version.pk]);response=self.client.get(file_url)
        self.assertEqual(response['Content-Type'],'audio/wav');self.assertIn('no-store',response['Cache-Control'])
        self.assertEqual(hashlib.sha256(response.content).hexdigest(),self.item.audio_sha256)
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.owner)
        self.assertEqual(csrf.post(url,data).status_code,403)
        self.client.force_login(self.other)
        for target in (url,file_url,reverse('inquiries:detail',args=[inquiry.pk])):
            self.assertEqual(self.client.get(target).status_code,404)

    def test_explicit_confirm_replay_and_changed_offer_require_new_approval(self):
        selection=self.prepare();version=Version.objects.first()
        first=confirm(actor=self.owner,version_id=version.pk,expected_hash=version.digest)
        again=confirm(actor=self.owner,version_id=version.pk,expected_hash=version.digest)
        self.assertEqual(first.pk,again.pk)
        approval=self.approve(selection)
        Hotel.objects.filter(pk=self.hotels[0].pk).update(summary='Новое синтетическое описание')
        with self.assertRaises(ValidationError):publish_request(selection,self.owner,approval.pk)
        self.assertEqual(Release.objects.count(),0)
        self.assertTrue(self.release(selection).pdf_hash)

    def test_failure_with_existing_inquiry_preserves_it_without_partial_voice_binding(self):
        target=Inquiry.objects.create(owner=self.owner,traveller_label='Существующее синтетическое')
        _,_,draft=self.draft(target);self.confirm_draft(draft)
        with patch('inquiries.voice.SelectionLink.objects.create',side_effect=DatabaseError('synthetic fault')):
            with self.assertRaises(DatabaseError):prepare(actor=self.owner,draft_id=draft.pk,
                expected_hash=draft.content_hash,hotel_ids=[self.hotels[0].pk])
        self.assertEqual(Inquiry.objects.get().pk,target.pk)
        for model in (VoiceSourceLink,VoiceSelection,Version,Operation,SelectionLink,TravelRequest):
            self.assertEqual(model.objects.count(),0,model.__name__)

    def test_owner_deactivation_closes_existing_share_and_archive(self):
        selection=self.prepare();release,token=publish_request(selection,self.owner,self.approve(selection).pk)
        visitor=Client();share=reverse('presentations:share',args=[token])
        self.assertEqual(visitor.post(share).status_code,302)
        self.assertEqual(visitor.get(share).status_code,200)
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertEqual(visitor.get(share).status_code,404)
        self.assertEqual(visitor.get(reverse('presentations:share_pdf',args=[token])).status_code,404)
        self.assertEqual(Release.objects.get().pdf_hash,release.pdf_hash)
