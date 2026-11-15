"""Synthetic versioned-template integration; existing inquiry/offer/release services, no network."""
import copy
import io
import json
from dataclasses import replace
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError,PermissionDenied
from django.test import TestCase,SimpleTestCase,Client
from django.urls import reverse
from pypdf import PdfReader
from inquiries.models import Inquiry,Version,SelectionLink
from inquiries.offers import read
from inquiries.test_offers import packet
from .models import Release
from .services import build_snapshot,snapshot_hash,approve_request,publish_request,select_template
from .themes import Theme,REGISTRY,registry,descriptor,choices
from .client_data import public_view
from .rendering import render_release_html,render_pdf

class ThemeFlowTests(TestCase):
    def setUp(self):
        from inquiries.test_manager import ManagerFlowTests
        ManagerFlowTests.setUp(self)
        from inquiries.manager import prepare
        self.selection=prepare(actor=self.owner,work_id=self.work.pk,expected_hash=self.rev.content_hash,
            hotel_ids=[self.hotels[0].pk],traveller_label='Вымышленная поездка',offer=packet(self.hotels[0]))
        self.network=patch('socket.socket.connect',side_effect=AssertionError('Network forbidden'))
        self.network.start();self.addCleanup(self.network.stop)

    def change(self,key,**kw):
        args=dict(selection_id=self.selection.pk,actor=self.owner,key=key,expected_hash=snapshot_hash(build_snapshot(self.selection)))
        args.update(kw);self.selection=select_template(**args);return self.selection

    def release(self):
        a=approve_request(self.selection,self.owner,snapshot_hash(build_snapshot(self.selection)))
        return publish_request(self.selection,self.owner,a.pk)

    def test_repeat_design_changes_no_offer_or_source_and_preserves_first_bytes(self):
        before=build_snapshot(self.selection);private=copy.deepcopy(read(self.selection));first,token=self.release()
        frozen=(copy.deepcopy(first.snapshot),first.html_content,bytes(first.pdf_bytes),first.pdf_hash)
        self.change('brief:1');after=build_snapshot(self.selection)
        for field in ('presentation','template_version'):before.pop(field);after.pop(field)
        self.assertEqual(before,after);self.assertEqual(read(self.selection),private)
        second,_=self.release();self.assertNotEqual(first.pk,second.pk)
        self.assertEqual(first.snapshot['presentation']['id'],'cards');self.assertEqual(second.snapshot['presentation']['id'],'brief')
        self.assertEqual(first.snapshot['offer'],second.snapshot['offer']);self.assertEqual(first.snapshot['hotels'],second.snapshot['hotels'])
        self.assertEqual(Inquiry.objects.count(),1);self.assertEqual(SelectionLink.objects.count(),1)
        versions=[v for v in Version.objects.all() if v.snapshot['release']]
        self.assertEqual(len(versions),2);self.assertEqual(len({v.inquiry_id for v in versions}),1)
        self.assertEqual({v.snapshot['release']['id'] for v in versions},{str(first.pk),str(second.pk)})
        first.refresh_from_db();self.assertEqual((first.snapshot,first.html_content,bytes(first.pdf_bytes),first.pdf_hash),frozen)
        tourist=Client();url=reverse('presentations:share',args=[token]);tourist.post(url)
        self.assertEqual(tourist.get(url).content.decode(),frozen[1])
        self.assertEqual(tourist.get(reverse('presentations:share_pdf',args=[token])).content,frozen[2])
        for release in (first,second):
            client=release.html_content+json.dumps(public_view(release.snapshot))+''.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(bytes(release.pdf_bytes))).pages)
            for forbidden in ('173421.13','173 421,13','base_price','source_context_hash'):self.assertNotIn(forbidden,client)
            self.assertIn('185 000,25',client);self.assertIn('Перелёт не заявлен',client)

    def test_switch_invalidates_pending_approval_and_old_pdf_survives_registry_changes(self):
        first,token=self.release();approval=approve_request(self.selection,self.owner,snapshot_hash(build_snapshot(self.selection)))
        self.change('brief:1')
        with self.assertRaises(ValidationError):publish_request(self.selection,self.owner,approval.pk)
        with patch('presentations.themes.REGISTRY',registry([REGISTRY['brief:1']])):
            c=Client();url=reverse('presentations:share',args=[token]);c.post(url)
            self.assertEqual(c.get(url).content.decode(),first.html_content)
            self.assertEqual(c.get(reverse('presentations:share_pdf',args=[token])).content,bytes(first.pdf_bytes))
        self.assertEqual(Release.objects.count(),1)

    def test_unknown_version_path_and_stale_selection_fail_without_mutation(self):
        before=build_snapshot(self.selection)
        for key in ('missing:1','cards:999','../cards:1','https://invalid',None,{},True):
            with self.subTest(key=key),self.assertRaises(ValidationError):self.change(key)
        with self.assertRaises(ValidationError):self.change('brief:1',expected_hash='0'*64)
        self.selection.refresh_from_db();self.assertEqual(build_snapshot(self.selection),before)

    def test_other_and_revoked_owner_cannot_select_or_read(self):
        first,_=self.release()
        with self.assertRaises(PermissionDenied):self.change('brief:1',actor=self.other)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('presentations:preview',args=[self.selection.pk])).status_code,404)
        self.assertEqual(self.client.post(reverse('presentations:change_template',args=[self.selection.pk]),{'template':'brief:1'}).status_code,404)
        self.assertEqual(self.client.get(reverse('presentations:archive_pdf',args=[first.pk])).status_code,404)
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):self.change('brief:1')

    def test_ui_select_preview_approve_and_release_use_same_template(self):
        self.client.force_login(self.owner);url=reverse('presentations:change_template',args=[self.selection.pk])
        self.assertEqual(self.client.get(url).status_code,405)
        locked=Client(enforce_csrf_checks=True);locked.force_login(self.owner)
        self.assertEqual(locked.post(url,{'template':'brief:1'}).status_code,403)
        response=self.client.post(url,{'template':'brief:1','content_hash':snapshot_hash(build_snapshot(self.selection))})
        self.assertEqual(response.status_code,302);self.selection.refresh_from_db()
        preview=self.client.get(response.url);self.assertContains(preview,'Краткий обзор');self.assertContains(preview,'дизайн не утверждён')
        snapshot=build_snapshot(self.selection)
        self.assertEqual(preview.context['client_preview'],__import__('presentations.rendering',fromlist=['render_release_content']).render_release_content(snapshot))
        approved=self.client.post(reverse('presentations:approve',args=[self.selection.pk]),{'content_hash':snapshot_hash(snapshot)})
        self.assertEqual(approved.status_code,200)
        released=self.client.post(reverse('presentations:publish',args=[self.selection.pk]),{'approval_id':approved.context['approval'].pk})
        self.assertEqual(released.status_code,200);self.assertEqual(Release.objects.get().snapshot['presentation']['id'],'brief')
        invalid=self.client.post(url,{'template':'missing:1','content_hash':snapshot_hash(snapshot)})
        self.assertEqual(invalid.status_code,422)

    def test_registered_extra_version_can_be_selected_without_changing_data_contract(self):
        extra=Theme('cards',2,'Новая синтетическая версия','presentations/themes/brief_v1.html')
        with patch('presentations.themes.REGISTRY',registry([*REGISTRY.values(),extra])):
            before=public_view(build_snapshot(self.selection));self.change('cards:2')
            self.assertIn(('cards:2',extra.label),choices());release,_=self.release()
            self.assertEqual(release.snapshot['presentation']['version'],2);self.assertEqual(public_view(release.snapshot),before)

    def test_changed_assets_invalidate_approval_and_snapshot_cannot_rerender(self):
        snap=build_snapshot(self.selection);approval=approve_request(self.selection,self.owner,snapshot_hash(snap))
        changed=replace(REGISTRY['cards:1'],style='presentations/themes/brief_v1.html')
        with patch('presentations.themes.REGISTRY',registry([changed,REGISTRY['brief:1']])):
            with self.assertRaises(ValidationError):publish_request(self.selection,self.owner,approval.pk)
            with self.assertRaises(ValidationError):render_release_html(snap)
        self.assertEqual(Release.objects.count(),0)

    def test_private_nested_fields_not_passed_to_any_renderer(self):
        snapshot=build_snapshot(self.selection);snapshot['internal_notes']='PRIVATE_ROOT'
        snapshot['hotels'][0]['internal_notes']='PRIVATE_HOTEL';snapshot['offer']['base_price']='PRIVATE_BASE'
        snapshot['offer']['flight']={'details':'Синтетический пример','evidence':'PRIVATE_EVIDENCE'}
        for key in REGISTRY:
            snapshot['presentation']=descriptor(key);snapshot['template_version']=key
            serialized=json.dumps(public_view(snapshot))+render_release_html(snapshot)
            for value in ('PRIVATE_ROOT','PRIVATE_HOTEL','PRIVATE_BASE','PRIVATE_EVIDENCE','source_context_hash','hotel_id'):self.assertNotIn(value,serialized)

    def test_unsafe_public_text_remains_escaped_for_each_theme(self):
        snap=build_snapshot(self.selection);snap['title']='<script>SYNTHETIC_ONLY</script>'
        for key in REGISTRY:
            snap['presentation']=descriptor(key)
            html=render_release_html(snap)
            self.assertNotIn('<script>SYNTHETIC_ONLY</script>',html);self.assertIn('&lt;script&gt;',html)

    def test_incomplete_offer_remains_incomplete_in_both_themes(self):
        from inquiries.manager import prepare
        from inquiries.offers import SCHEMA
        self.selection=prepare(actor=self.owner,work_id=self.work.pk,expected_hash=self.rev.content_hash,
            hotel_ids=[self.hotels[0].pk],offer={'schema':SCHEMA,'synthetic_only':True})
        for key in REGISTRY:
            self.change(key);release,_=self.release()
            self.assertIsNone(release.snapshot['offer']['client_price'])
            self.assertIn('Неполное предложение',release.html_content)
            self.assertIn('Перелёт не заявлен',release.html_content)
            self.assertNotIn('Параметры выбранного предложения указаны',release.html_content)

    def test_template_change_rollback_and_idempotent_same_choice(self):
        before=build_snapshot(self.selection)
        self.change('cards:1');self.assertEqual(build_snapshot(self.selection),before)
        with patch('presentations.models.TravelRequest.save',side_effect=RuntimeError('synthetic local failure')):
            with self.assertRaises(RuntimeError):self.change('brief:1')
        self.selection.refresh_from_db();self.assertEqual(build_snapshot(self.selection),before)

class ThemeContractTests(SimpleTestCase):
    def test_registry_rejects_duplicate_key_and_paths(self):
        with self.assertRaises(ValueError):registry([REGISTRY['cards:1'],REGISTRY['cards:1']])
        for changes in ({'style':'../../private.html'},{'body':'https://invalid'},{'version':True},{'id':'../bad'}):
            with self.assertRaises(ValueError):registry([replace(REGISTRY['cards:1'],**changes)])

    def test_public_contract_rejects_nested_data_in_public_text(self):
        with self.assertRaises(ValidationError):public_view({'title':{'internal_notes':'synthetic'}})
        with self.assertRaises(ValidationError):public_view({'hotels':[{'pros':[{'private':'synthetic'}]}]})

    def test_brief_long_and_short_pdf_preserve_all_synthetic_content(self):
        from .synthetic_layout import fixture,expected_values,compact
        from .test_pagination import two_hotel_snapshot
        for snapshot in (two_hotel_snapshot(),fixture(5,True)):
            snapshot['presentation']=descriptor('brief:1')
            reader=PdfReader(io.BytesIO(render_pdf(snapshot)));text=compact(''.join(p.extract_text() or '' for p in reader.pages))
            for value in expected_values(snapshot):self.assertIn(compact(value),text)
            if len(snapshot['hotels'])==2:self.assertEqual(len(reader.pages),1)
            else:
                self.assertGreater(len(reader.pages),1)
                last=compact(reader.pages[-1].extract_text())
                self.assertIn(compact('Конец ограничения 5.'),last)
                self.assertIn(compact('Стоимость и наличие тура не подтверждены.'),last)
