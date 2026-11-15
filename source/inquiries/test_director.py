"""Synthetic office/role boundaries; Django client is not a native browser."""
import copy
import io
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse
from pypdf import PdfReader
from . import director
from .models import Inquiry, InquiryOffice, Office, ManagerOffice, DirectorOfficeGrant, Version, SelectionLink, WorkLink
from .services import digest
from . import test_manager as manager_helpers


class DirectorScopeTests(TestCase):
    prepare = manager_helpers.ManagerFlowTests.prepare
    release = manager_helpers.ManagerFlowTests.release

    def build_fixture(self):
        manager_helpers.ManagerFlowTests.setUp(self)
        self.a=Office.objects.create(name='Синтетический офис А')
        self.b=Office.objects.create(name='Синтетический офис Б')
        self.c=Office.objects.create(name='СКРЫТЫЙ СИНТЕТИЧЕСКИЙ ОФИС')
        ManagerOffice.objects.create(user=self.owner,office=self.a)
        ManagerOffice.objects.create(user=self.other,office=self.b)
        self.first=self.release(self.prepare())
        self.second=self.release(self.prepare(hotel_ids=[self.hotels[1].pk]))
        self.inquiry=Inquiry.objects.get()
        self.other_inquiry=Inquiry.objects.create(owner=self.other,traveller_label='СИНТЕТИКА-Б')
        director.bind_new_inquiry(self.other_inquiry)
        hidden=get_user_model().objects.create_user('synthetic-hidden-manager')
        self.hidden=Inquiry.objects.create(owner=hidden,traveller_label='СКРЫТОЕ-ОБРАЩЕНИЕ')
        InquiryOffice.objects.create(inquiry=self.hidden,office=self.c,owner_at_binding=hidden)
        self.viewer=get_user_model().objects.create_user('synthetic-director')
        self.ga=DirectorOfficeGrant.objects.create(user=self.viewer,office=self.a)
        self.gb=DirectorOfficeGrant.objects.create(user=self.viewer,office=self.b)
        self.list_url=reverse('inquiries:director_list')
        self.detail_url=reverse('inquiries:director_detail',args=[self.inquiry.pk])
        self.version=Version.objects.filter(snapshot__release__id=str(self.first.pk)).get()
        self.pdf_url=reverse('inquiries:director_pdf',args=[self.version.pk])
    def setUp(self):
        self.build_fixture()
        self.client.force_login(self.viewer)

    def test_two_offices_visible_third_not_even_in_filters_or_count(self):
        response=self.client.get(self.list_url)
        self.assertContains(response,self.a.name);self.assertContains(response,self.b.name)
        self.assertContains(response,'СИНТЕТИКА-Б');self.assertNotContains(response,self.hidden.traveller_label)
        self.assertNotContains(response,self.c.name);self.assertNotContains(response,'synthetic-hidden-manager')
        self.assertEqual(response.context['page'].paginator.count,2)

    def test_pagination_contains_only_allowed_records(self):
        for i in range(26):
            item=Inquiry.objects.create(owner=self.owner,traveller_label=f'Синтетическая страница {i}')
            director.bind_new_inquiry(item)
        response=self.client.get(self.list_url,{'page':2,'office':str(self.a.pk)})
        self.assertEqual(response.context['page'].paginator.count,27)
        self.assertEqual(len(response.context['items']),2)
        self.assertNotIn('page=',response.context['filters'])
        self.assertNotContains(response,self.hidden.traveller_label)

    def test_office_filter(self):
        response=self.client.get(self.list_url,{'office':str(self.b.pk)})
        self.assertEqual([i.pk for i in response.context['items']],[self.other_inquiry.pk])

    def test_manager_filter(self):
        response=self.client.get(self.list_url,{'manager':str(self.owner.pk)})
        self.assertEqual([i.pk for i in response.context['items']],[self.inquiry.pk])

    def test_combined_filter_empty_not_broadened(self):
        response=self.client.get(self.list_url,{'office':str(self.a.pk),'manager':str(self.other.pk)})
        self.assertEqual(response.context['page'].paginator.count,0)

    def test_search_bounded_and_escaped(self):
        self.assertEqual(self.client.get(self.list_url,{'q':'СИНТЕТИКА-Б'}).context['page'].paginator.count,1)
        response=self.client.get(self.list_url,{'q':'<script>alert(1)</script>'})
        self.assertNotContains(response,'<script>')
        self.assertEqual(self.client.get(self.list_url,{'q':'x'*121}).status_code,404)

    def test_unauthorized_or_malformed_filters_do_not_leak(self):
        for query in ({'office':str(self.c.pk)},{'office':'broken'},{'manager':str(self.hidden.owner_id)},{'manager':'not-id'}):
            response=self.client.get(self.list_url,query)
            self.assertEqual(response.status_code,404);self.assertNotIn(self.hidden.traveller_label.encode(),response.content)

    def test_director_without_second_office_grant_is_denied_detail(self):
        self.gb.delete()
        self.assertEqual(self.client.get(reverse('inquiries:director_detail',args=[self.other_inquiry.pk])).status_code,403)
        self.assertNotContains(self.client.get(self.list_url),'СИНТЕТИКА-Б')

    def test_manager_cannot_use_director_routes_even_for_own_record(self):
        self.client.force_login(self.owner)
        for url in (self.list_url,self.detail_url,self.pdf_url): self.assertEqual(self.client.get(url).status_code,403)

    def test_staff_or_superuser_has_no_implicit_director_access(self):
        get_user_model().objects.filter(pk=self.owner.pk).update(is_staff=True,is_superuser=True)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.list_url).status_code,403)

    def test_manager_cannot_read_other_office_or_same_office_owner_routes(self):
        self.client.force_login(self.owner)
        for target in (self.other_inquiry,self.hidden):
            self.assertEqual(self.client.get(reverse('inquiries:detail',args=[target.pk])).status_code,404)
        InquiryOffice.objects.filter(inquiry=self.other_inquiry).update(office=self.a)
        self.assertEqual(self.client.get(reverse('inquiries:detail',args=[self.other_inquiry.pk])).status_code,404)

    def test_director_cannot_use_manager_mutation_or_raw_source_routes(self):
        self.assertEqual(self.client.get(reverse('inquiries:source',args=[self.version.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse('presentations:preview',args=[self.first.request_id])).status_code,404)
        self.assertEqual(self.client.post(reverse('presentations:revoke',args=[self.first.pk])).status_code,404)
        self.assertEqual(self.client.post(self.detail_url).status_code,405)

    def test_anonymous_redirect_and_service_denial(self):
        self.client.logout();self.assertEqual(self.client.get(self.list_url).status_code,302)
        with self.assertRaises(PermissionDenied): director.scope(AnonymousUser())

    def test_revoked_grant_same_logged_in_session_blocks_html_and_pdf(self):
        self.assertEqual(self.client.get(self.pdf_url).status_code,200)
        DirectorOfficeGrant.objects.filter(pk=self.ga.pk).update(is_active=False)
        self.assertEqual(self.client.get(self.detail_url).status_code,403)
        self.assertEqual(self.client.get(self.pdf_url).status_code,403)
        self.assertNotContains(self.client.get(self.list_url),self.inquiry.traveller_label)

    def test_deactivated_director_rechecked_with_stale_python_actor(self):
        get_user_model().objects.filter(pk=self.viewer.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied): director.timeline(self.viewer,self.inquiry.pk)
        self.assertEqual(self.client.get(self.pdf_url).status_code,302)

    def test_office_deactivation_blocks_existing_grant(self):
        Office.objects.filter(pk=self.a.pk).update(is_active=False)
        self.assertEqual(self.client.get(self.detail_url).status_code,403)

    def test_inactive_manager_history_remains_for_authorized_director(self):
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertContains(self.client.get(self.detail_url),'доступ менеджера отключён')
        self.assertEqual(self.client.get(self.pdf_url).status_code,200)

    def test_revocation_during_file_audit_discards_response(self):
        original=director.audit_version
        def revoke(*args):
            row=original(*args)
            DirectorOfficeGrant.objects.filter(pk=self.ga.pk).update(is_active=False)
            return row
        with patch.object(director,'audit_version',side_effect=revoke):
            response=self.client.get(self.pdf_url)
        self.assertEqual(response.status_code,403);self.assertNotIn(b'%PDF-',response.content)

    def test_revocation_during_html_render_discards_response(self):
        from . import director_views
        original=director_views.render
        def revoke(*args,**kwargs):
            response=original(*args,**kwargs)
            DirectorOfficeGrant.objects.filter(pk=self.ga.pk).update(is_active=False)
            return response
        with patch.object(director_views,'render',side_effect=revoke):
            response=self.client.get(self.list_url)
        self.assertEqual(response.status_code,403);self.assertNotIn(self.inquiry.traveller_label.encode(),response.content)

    def test_first_repeat_and_exact_pdf_links(self):
        response=self.client.get(self.detail_url)
        for phrase in ('Первый проверенный выпуск','Повторный проверенный выпуск 2','U-ON: не синхронизировано','SHA-256'):
            self.assertContains(response,phrase)
        self.assertEqual(self.client.get(self.pdf_url).content,bytes(self.first.pdf_bytes))
        self.assertContains(response,str(self.first.pk));self.assertContains(response,str(self.second.pk))
        self.assertEqual(InquiryOffice.objects.filter(inquiry=self.inquiry).count(),1)

    def test_corrupt_snapshot_never_exposes_its_internal_text(self):
        snap=copy.deepcopy(self.version.snapshot);snap['work']['code']='SECRET_SYNTHETIC_BAD_LINK'
        Version.objects.filter(pk=self.version.pk).update(snapshot=snap)
        response=self.client.get(self.detail_url)
        self.assertContains(response,director.ERROR);self.assertNotContains(response,'SECRET_SYNTHETIC_BAD_LINK')
        self.assertEqual(self.client.get(self.pdf_url).status_code,409)

    def test_false_release_link_even_rehashed_is_rejected(self):
        snap=copy.deepcopy(self.version.snapshot);snap['release']['id']=str(self.second.pk)
        Version.objects.filter(pk=self.version.pk).update(snapshot=snap,digest=digest(snap))
        self.assertEqual(self.client.get(self.pdf_url).status_code,409)

    def test_cross_inquiry_selection_link_is_rejected(self):
        SelectionLink.objects.filter(selection_id=self.first.request_id).update(inquiry=self.other_inquiry)
        self.assertEqual(self.client.get(self.pdf_url).status_code,409)

    def test_cross_inquiry_source_link_is_rejected(self):
        WorkLink.objects.filter(work_id=self.work.pk).update(inquiry=self.other_inquiry)
        self.assertEqual(self.client.get(self.pdf_url).status_code,409)

    def test_owner_binding_mismatch_hides_entire_inquiry(self):
        Inquiry.objects.filter(pk=self.inquiry.pk).update(owner=self.other)
        self.assertEqual(self.client.get(self.detail_url).status_code,403)
        self.assertEqual(self.client.get(self.pdf_url).status_code,403)

    def test_missing_or_corrupt_source_not_substituted(self):
        from worksheet_reviews.models import WorksheetReview
        WorksheetReview.objects.filter(pk=self.work.pk).update(worksheet_bytes=b'bad')
        self.assertContains(self.client.get(self.detail_url),director.ERROR)
        self.assertEqual(self.client.get(self.pdf_url).status_code,409)

    def test_corrupt_pdf_is_blocked(self):
        from presentations.models import Release
        Release.objects.filter(pk=self.first.pk).update(pdf_bytes=b'bad')
        self.assertEqual(self.client.get(self.pdf_url).status_code,409)

    def test_original_office_stays_when_membership_changes(self):
        ManagerOffice.objects.filter(user=self.owner).update(office=self.b)
        self.prepare(hotel_ids=[self.hotels[1].pk])
        self.assertEqual(InquiryOffice.objects.get(inquiry=self.inquiry).office_id,self.a.pk)

    def test_unassigned_inquiry_is_not_inferred_from_owner(self):
        legacy=Inquiry.objects.create(owner=self.owner,traveller_label='SYNTHETIC_UNASSIGNED')
        self.assertNotContains(self.client.get(self.list_url),legacy.traveller_label)
        self.assertEqual(self.client.get(reverse('inquiries:director_detail',args=[legacy.pk])).status_code,403)

    def test_private_headers_and_navigation(self):
        for url in (self.list_url,self.detail_url,self.pdf_url):
            response=self.client.get(url)
            self.assertEqual(response['Cache-Control'],'no-store, private')
            self.assertEqual(response['X-Frame-Options'],'DENY')
        self.assertContains(self.client.get(reverse('inquiries:list')),'Контроль директора')
        self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(reverse('inquiries:list')),'Контроль директора')

    def test_office_roles_and_internal_notes_absent_in_customer_files(self):
        marker='SYNTHETIC_INTERNAL_DIRECTOR_ONLY'
        from presentations.models import Hotel
        hotel=Hotel.objects.create(name='Новый синтетический отель',summary='Синтетика',pros='Пример',
            limitations='Не проверен',source_reference='Синтетика',internal_notes=marker)
        selection=self.prepare(hotel_ids=[hotel.pk])
        selection.internal_notes=marker;selection.save(update_fields=['internal_notes'])
        release=self.release(selection)
        pdf='\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(bytes(release.pdf_bytes))).pages)
        public=release.html_content+str(release.snapshot)+pdf
        for private in (marker,self.a.name,self.b.name,self.viewer.username,self.owner.username,str(self.inquiry.pk)):
            self.assertNotIn(private,public)
        # Director template does not dump snapshots, transcripts or private commentary either.
        self.assertNotContains(self.client.get(self.detail_url),marker)


from django.test import override_settings
from . import test_voice as voice_helpers


@override_settings(VOICE_INQUIRY_BOUNDARY_ENABLED=True)
class DirectorVoiceTests(TestCase):
    source = voice_helpers.VoiceLedgerTests.source
    draft = voice_helpers.VoiceLedgerTests.draft
    confirm_draft = voice_helpers.VoiceLedgerTests.confirm_draft
    prepare = voice_helpers.VoiceLedgerTests.prepare
    approve = voice_helpers.VoiceLedgerTests.approve
    release = voice_helpers.VoiceLedgerTests.release

    def setUp(self):
        voice_helpers.VoiceLedgerTests.setUp(self)
        self.office=Office.objects.create(name='Синтетический голосовой офис')
        ManagerOffice.objects.create(user=self.owner,office=self.office)
        self.viewer=get_user_model().objects.create_user('synthetic-voice-director')
        DirectorOfficeGrant.objects.create(user=self.viewer,office=self.office)

    def tearDown(self):
        from speech_workflow.models import VoiceSubmission
        for item in VoiceSubmission.objects.all():
            item.audio.delete(save=False)
        super().tearDown()

    def test_voice_first_repeat_sources_and_exact_pdf(self):
        first=self.release(self.prepare())
        self.release(self.prepare(hotel_ids=[self.hotels[1].pk]))
        inquiry=Inquiry.objects.get()
        self.assertEqual(InquiryOffice.objects.get(inquiry=inquiry).office_id,self.office.pk)
        _,rows=director.timeline(self.viewer,inquiry.pk)
        self.assertTrue(all(r['valid'] for r in rows))
        self.assertEqual([r['release']['order'] for r in rows if r['release']],[1,2])
        version=Version.objects.get(snapshot__release__id=str(first.pk))
        self.assertEqual(director.pdf(self.viewer,version.pk),bytes(first.pdf_bytes))
        self.client.force_login(self.viewer)
        self.assertContains(self.client.get(reverse('inquiries:director_detail',args=[inquiry.pk])),'Голосовой WAV')

    def test_missing_wav_is_not_replaced_by_pdf_or_mock(self):
        self.release(self.prepare())
        from pathlib import Path
        Path(self.item.audio.path).unlink()
        _,rows=director.timeline(self.viewer,Inquiry.objects.get().pk)
        self.assertTrue(all(not r['valid'] for r in rows))

    def test_cross_inquiry_voice_link_rejected(self):
        self.release(self.prepare())
        from .models import VoiceSourceLink
        original=Inquiry.objects.get()
        another=Inquiry.objects.create(owner=self.owner,traveller_label='Синтетический другой запрос')
        VoiceSourceLink.objects.filter(inquiry=original).update(inquiry=another)
        _,rows=director.timeline(self.viewer,original.pk)
        self.assertTrue(all(not r['valid'] for r in rows))

    def test_binding_failure_rolls_back_creation_without_partial_inquiry(self):
        from django.db import DatabaseError
        with patch('inquiries.director.InquiryOffice.objects.create',side_effect=DatabaseError('synthetic')):
            with self.assertRaises(DatabaseError):self.prepare()
        self.assertEqual(Inquiry.objects.count(),0)
        self.assertEqual(SelectionLink.objects.count(),0)

    def test_inactive_membership_does_not_guess_office(self):
        ManagerOffice.objects.filter(user=self.owner).update(is_active=False)
        self.prepare()
        self.assertEqual(InquiryOffice.objects.count(),0)
        self.assertEqual(director.scope(self.viewer).count(),0)
