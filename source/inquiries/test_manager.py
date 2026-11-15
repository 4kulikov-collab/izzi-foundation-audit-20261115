from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError, PermissionDenied
from django.test import TestCase
from django.urls import reverse
from presentations.models import Hotel, Release, TravelRequest
from presentations.services import approve_request, build_snapshot, snapshot_hash, publish_request
from worksheet_reviews.services import create_review
from worksheet_reviews.test_ocr_integration import png_upload, known_fields
from worksheet_reviews.ocr_services import initialize_ocr, save_fields, replace_source, current_revision
from .manager import prepare
from .models import Inquiry, SelectionLink, Version, WorksheetSelection
from .services import source_file

class ManagerFlowTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('manager-test')
        self.other = get_user_model().objects.create_user('other-test')
        self.work = create_review(owner=self.owner, work_code='SYNTH-FLOW',
            worksheet_bytes=png_upload().read(), worksheet_type='image/png',
            hotel_choices=[{'position': i+1, 'name': f'Synthetic {i}', 'reference_text': 'fixture'} for i in range(3)], expert_overview='Synthetic')
        self.fields = known_fields('Synthetic')
        with patch('worksheet_reviews.ocr_services.recognize_card_photo', return_value=(self.fields, {'status':'mock'})):
            rev = initialize_ocr(self.work, self.owner)
        self.rev = save_fields(self.work, self.owner, rev.number, rev.content_hash, self.fields, confirm=True)
        self.hotels = [Hotel.objects.create(name=f'Synthetic {i}', summary='synthetic',pros='test',limitations='test',source_reference='fixture') for i in range(2)]
    def prepare(self, **kw):
        args=dict(actor=self.owner, work_id=self.work.pk, expected_hash=self.rev.content_hash,hotel_ids=[self.hotels[0].pk],traveller_label='Synthetic')
        args.update(kw)
        return prepare(**args)
    def release(self, selection):
        approval=approve_request(selection,self.owner,snapshot_hash(build_snapshot(selection)))
        return publish_request(selection,self.owner,approval.pk)[0]
    def test_two_presentations_one_inquiry_real_pdf_and_repeat(self):
        one=self.prepare()
        self.assertEqual(self.prepare().pk,one.pk)
        first=self.release(one)
        self.assertTrue(bytes(first.pdf_bytes).startswith(b'%PDF-'))
        two=self.prepare(hotel_ids=[self.hotels[1].pk])
        second=self.release(two)
        self.assertNotEqual(first.pk,second.pk)
        self.assertEqual(Inquiry.objects.count(),1)
        self.assertEqual(SelectionLink.objects.count(),2)
        releases=[v for v in Version.objects.all() if v.snapshot['release']]
        self.assertEqual(len(releases),2)
        self.assertTrue(all(v.inquiry.sync_status=='not_synchronized' for v in releases))
        self.assertEqual(source_file(actor=self.owner,version_id=releases[0].pk)[0], bytes(self.work.worksheet_bytes))
    def test_replacement_invalidates_confirmation_before_ocr_completes(self):
        selection=self.prepare()
        replace_source(self.work,self.owner,1,self.work.worksheet_sha256,worksheet_bytes=png_upload('red').read(),worksheet_type='image/png')
        self.work.refresh_from_db()
        self.assertIsNone(current_revision(self.work))
        with self.assertRaises(ValidationError): self.release(selection)
        with patch('worksheet_reviews.ocr_services.recognize_card_photo', return_value=(self.fields,{'status':'mock'})):
            newer=initialize_ocr(self.work,self.owner)
        self.assertGreater(newer.number,self.rev.number)
        self.assertIsNone(newer.confirmed_at)
        self.assertTrue(self.work.ocr_revisions.get(pk=self.rev.pk).confirmed_at)
    def test_other_owner_and_stale_hash_rejected(self):
        with self.assertRaises(PermissionDenied): self.prepare(actor=self.other)
        with self.assertRaises(ValidationError): self.prepare(expected_hash='0'*64)
        self.assertEqual(Inquiry.objects.count(),0)
    def test_deactivation_during_render_rolls_back_release(self):
        selection=self.prepare()
        approval=approve_request(selection,self.owner,snapshot_hash(build_snapshot(selection)))
        def deactivate(*args,**kw):
            get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
            return b'%PDF-test'
        with patch('presentations.services.render_pdf',side_effect=deactivate):
            with self.assertRaises(PermissionDenied): publish_request(selection,self.owner,approval.pk)
        self.assertEqual(Release.objects.count(),0)
    def test_ui_connects_selection_and_replay(self):
        self.client.force_login(self.owner)
        url=reverse('inquiries:selection',args=[self.work.pk])
        self.assertContains(self.client.get(url),'не синхронизировано')
        data={'expected_hash':self.rev.content_hash,'hotels':[str(self.hotels[0].pk)],'traveller_label':'Synthetic'}
        self.assertEqual(self.client.post(url,data).status_code,302)
        self.assertEqual(self.client.post(url,data).status_code,302)
        self.assertEqual(WorksheetSelection.objects.count(),1)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code,404)
    def test_historical_file_after_replacement_is_exact(self):
        selection=self.prepare();self.release(selection)
        version=Version.objects.filter(snapshot__release__isnull=False).last()
        original=bytes(self.work.worksheet_bytes)
        replace_source(self.work,self.owner,1,self.work.worksheet_sha256,worksheet_bytes=png_upload('red').read(),worksheet_type='image/png')
        self.assertEqual(source_file(actor=self.owner,version_id=version.pk)[0],original)

    def test_returning_to_original_photo_does_not_revive_confirmation(self):
        original=bytes(self.work.worksheet_bytes)
        red=replace_source(self.work,self.owner,1,self.work.worksheet_sha256,worksheet_bytes=png_upload('red').read(),worksheet_type='image/png')
        self.work.refresh_from_db()
        replace_source(self.work,self.owner,2,red.worksheet_sha256,worksheet_bytes=original,worksheet_type='image/png')
        self.work.refresh_from_db()
        self.assertIsNone(current_revision(self.work))
        with self.assertRaises(ValidationError): self.prepare()
    def test_second_work_can_use_same_inquiry_explicitly(self):
        one=self.prepare()
        inquiry=SelectionLink.objects.get(selection=one).inquiry
        other=create_review(owner=self.owner,work_code='SYNTH-SECOND',worksheet_bytes=png_upload('red').read(),worksheet_type='image/png',hotel_choices=[{'position':i+1,'name':f'Test {i}','reference_text':'fixture'} for i in range(3)],expert_overview='test')
        with patch('worksheet_reviews.ocr_services.recognize_card_photo',return_value=(self.fields,{'status':'mock'})):
            rev=initialize_ocr(other,self.owner)
        rev=save_fields(other,self.owner,rev.number,rev.content_hash,self.fields,confirm=True)
        two=prepare(actor=self.owner,work_id=other.pk,expected_hash=rev.content_hash,hotel_ids=[self.hotels[1].pk],inquiry_id=inquiry.pk)
        self.assertEqual(SelectionLink.objects.get(selection=two).inquiry_id,inquiry.pk)
        self.assertEqual(Inquiry.objects.count(),1)

    def test_replay_after_edit_does_not_silently_return_different_hotels(self):
        one=self.prepare()
        one.selected_hotels.set([self.hotels[1]])
        with self.assertRaises(ValidationError): self.prepare()
        self.assertEqual(Inquiry.objects.count(),1)
        self.assertEqual(WorksheetSelection.objects.count(),1)

    def test_legacy_api_cannot_approve_or_publish_without_inquiry(self):
        from presentations.models import Approval
        selection=TravelRequest.objects.create(owner=self.owner,title='Legacy',traveller_summary='Synthetic',requirements='Synthetic')
        selection.selected_hotels.set([self.hotels[0]])
        digest=snapshot_hash(build_snapshot(selection))
        with self.assertRaisesRegex(ValidationError,'обращением'):
            approve_request(selection,self.owner,digest)
        old_approval=Approval.objects.create(request=selection,approved_by=self.owner,content_hash=digest)
        with self.assertRaisesRegex(ValidationError,'обращением'):
            publish_request(selection,self.owner,old_approval.pk)
        self.assertEqual(Release.objects.count(),0)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse('presentations:approve',args=[selection.pk]),{'content_hash':digest}).status_code,409)
        self.assertEqual(self.client.post(reverse('presentations:publish',args=[selection.pk]),{'approval_id':old_approval.pk}).status_code,409)
        self.assertEqual(Release.objects.count(),0)

    def test_upload_without_hotel_choices_is_allowed(self):
        self.client.force_login(self.owner)
        with patch('worksheet_reviews.ocr_services.recognize_card_photo', return_value=(self.fields,{'status':'mock'})):
            response=self.client.post(reverse('worksheet_reviews:upload'),{'work_code':'SYNTH-NO-HOTELS','worksheet':png_upload(),'expert_overview':''})
        self.assertEqual(response.status_code,302)
        from worksheet_reviews.models import WorksheetReview
        work=WorksheetReview.objects.get(work_code='SYNTH-NO-HOTELS')
        self.assertEqual(work.hotel_choices.count(),0)

    def test_missing_link_after_approval_blocks_release(self):
        selection=self.prepare()
        approval=approve_request(selection,self.owner,snapshot_hash(build_snapshot(selection)))
        SelectionLink.objects.filter(selection=selection).delete()
        with self.assertRaises(ValidationError):publish_request(selection,self.owner,approval.pk)
        self.assertEqual(Release.objects.count(),0)

    def test_legacy_source_replacement_requires_new_explicit_binding(self):
        from .services import preview, attach
        selection=TravelRequest.objects.create(owner=self.owner,title='Legacy with source',traveller_summary='Synthetic',requirements='Synthetic')
        selection.selected_hotels.set([self.hotels[0]])
        snap=preview(actor=self.owner,work_id=self.work.pk,selection_id=selection.pk)
        attach(actor=self.owner,key='legacy-source',work_id=self.work.pk,selection_id=selection.pk,expected_hash=snap['digest'],traveller_label='Synthetic')
        approval=approve_request(selection,self.owner,snapshot_hash(build_snapshot(selection)))
        replace_source(self.work,self.owner,1,self.work.worksheet_sha256,worksheet_bytes=png_upload('red').read(),worksheet_type='image/png')
        with self.assertRaises(ValidationError):publish_request(selection,self.owner,approval.pk)
        with self.assertRaises(ValidationError):approve_request(selection,self.owner,snapshot_hash(build_snapshot(selection)))
        self.assertEqual(Release.objects.count(),0)

    def test_legacy_rebinding_source_invalidates_old_approval(self):
        from .services import preview, attach
        selection=TravelRequest.objects.create(owner=self.owner,title='Legacy context',traveller_summary='Synthetic',requirements='Synthetic')
        selection.selected_hotels.set([self.hotels[0]])
        snap=preview(actor=self.owner,work_id=self.work.pk,selection_id=selection.pk)
        version=attach(actor=self.owner,key='legacy-context-1',work_id=self.work.pk,selection_id=selection.pk,expected_hash=snap['digest'],traveller_label='Synthetic')
        approval=approve_request(selection,self.owner,snapshot_hash(build_snapshot(selection)))
        replace_source(self.work,self.owner,1,self.work.worksheet_sha256,worksheet_bytes=png_upload('red').read(),worksheet_type='image/png')
        snap=preview(actor=self.owner,work_id=self.work.pk,selection_id=selection.pk)
        attach(actor=self.owner,key='legacy-context-2',work_id=self.work.pk,selection_id=selection.pk,expected_hash=snap['digest'],inquiry_id=version.inquiry_id)
        with self.assertRaises(ValidationError):publish_request(selection,self.owner,approval.pk)
        self.assertEqual(Release.objects.count(),0)

    def test_old_string_source_version_cannot_revive_after_aba(self):
        from .services import preview, attach, digest
        selection=TravelRequest.objects.create(owner=self.owner,title='Legacy migrated',traveller_summary='Synthetic',requirements='Synthetic')
        selection.selected_hotels.set([self.hotels[0]])
        snap=preview(actor=self.owner,work_id=self.work.pk,selection_id=selection.pk)
        version=attach(actor=self.owner,key='legacy-migration',work_id=self.work.pk,selection_id=selection.pk,expected_hash=snap['digest'],traveller_label='Synthetic')
        # Simulate exactly the pre-PR15 persisted schema, not a production mutation.
        old=version.snapshot;old['work']['source_version']=old['work']['source_sha256']
        Version.objects.filter(pk=version.pk).update(snapshot=old,digest=digest(old))
        approval=approve_request(selection,self.owner,snapshot_hash(build_snapshot(selection)))
        original=bytes(self.work.worksheet_bytes)
        red=replace_source(self.work,self.owner,1,self.work.worksheet_sha256,worksheet_bytes=png_upload('red').read(),worksheet_type='image/png')
        self.work.refresh_from_db()
        replace_source(self.work,self.owner,2,red.worksheet_sha256,worksheet_bytes=original,worksheet_type='image/png')
        with self.assertRaises(ValidationError):publish_request(selection,self.owner,approval.pk)
        self.assertEqual(Release.objects.count(),0)
