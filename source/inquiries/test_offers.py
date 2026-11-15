"""Exact synthetic offers through existing selection, approval, release and access paths."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError, PermissionDenied
from django.test import TestCase
from django.urls import reverse
from pypdf import PdfReader
from presentations.models import Release, Hotel, TravelRequest
from presentations.services import build_snapshot, snapshot_hash, approve_request, publish_request
from .models import Inquiry, SelectedOffer, SelectionLink, Version
from .manager import prepare
from .offers import SCHEMA, normalize, public, read, reprice


def packet(hotel, **changes):
    data = {'schema': SCHEMA, 'synthetic_only': True, 'hotel_id': str(hotel.pk),
            'room_type': 'Синтетический номер Стандарт', 'tariff': 'Синтетический тариф Завтрак',
            'price_basis': 'За двоих взрослых, 7 ночей; проживание и завтрак',
            'base_price': '173421.13', 'client_price': '185000.25', 'currency': 'RUB',
            'selection_confirmed': True, 'flight': None}
    data.update(changes)
    return data


class OfferContractTests(TestCase):
    def setUp(self):
        from .test_manager import ManagerFlowTests
        ManagerFlowTests.setUp(self)
        self.data = packet(self.hotels[0])

    def selection(self, offer=None, **kw):
        args = dict(actor=self.owner, work_id=self.work.pk, expected_hash=self.rev.content_hash,
                    hotel_ids=[self.hotels[0].pk], traveller_label='Вымышленная поездка',
                    offer=self.data if offer is None else offer)
        args.update(kw)
        return prepare(**args)

    def release(self, selection):
        approval = approve_request(selection,self.owner,snapshot_hash(build_snapshot(selection)))
        return publish_request(selection,self.owner,approval.pk)[0]

    def client_text(self, release):
        return release.html_content + json.dumps(release.snapshot,ensure_ascii=False) + '\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(bytes(release.pdf_bytes))).pages)

    def test_first_and_repriced_repeat_keep_base_private_and_one_inquiry(self):
        one=self.selection(); first=self.release(one)
        original=(first.snapshot,first.html_content,bytes(first.pdf_bytes))
        two=reprice(actor=self.owner,selection_id=one.pk,expected_hash=snapshot_hash(build_snapshot(one)),client_price='192500.50')
        second=self.release(two)
        self.assertNotEqual(one.pk,two.pk)
        self.assertEqual(Inquiry.objects.count(),1)
        self.assertEqual(SelectionLink.objects.count(),2)
        self.assertEqual(read(one)['base_price'],read(two)['base_price'])
        self.assertEqual(read(one)['client_price'],'185000.25')
        self.assertEqual(read(two)['client_price'],'192500.50')
        self.assertEqual(first.snapshot['offer']['client_price'],'185000.25')
        self.assertEqual(second.snapshot['offer']['client_price'],'192500.50')
        for release in (first,second):
            self.assertNotIn('173421.13',self.client_text(release))
            self.assertNotIn('base_price',self.client_text(release))
            version=Version.objects.get(snapshot__release__id=str(release.pk))
            self.assertEqual(version.snapshot['selection']['exact_offer']['base_price'],'173421.13')
            self.assertEqual(version.inquiry_id,Inquiry.objects.get().pk)
        Hotel.objects.filter(pk=self.hotels[0].pk).update(name='CHANGED CATALOG')
        first.refresh_from_db()
        self.assertEqual((first.snapshot,first.html_content,bytes(first.pdf_bytes)),original)

    def test_same_hotel_distinct_room_or_tariff_is_distinct_selection(self):
        one=self.selection()
        two=self.selection(packet(self.hotels[0],room_type='Синтетический Семейный',tariff='Синтетический Всё включено'))
        self.assertNotEqual(one.pk,two.pk)
        self.assertEqual(Inquiry.objects.count(),1)
        self.assertEqual(self.selection().pk,one.pk)
        first=self.release(one);second=self.release(two)
        self.assertEqual(first.snapshot['offer']['room_type'],'Синтетический номер Стандарт')
        self.assertEqual(second.snapshot['offer']['tariff'],'Синтетический Всё включено')

    def test_repricing_idempotent_and_stale_hash_rejected(self):
        one=self.selection();checksum=snapshot_hash(build_snapshot(one))
        args=dict(actor=self.owner,selection_id=one.pk,expected_hash=checksum,client_price='190000')
        two=reprice(**args);self.assertEqual(reprice(**args).pk,two.pk)
        with self.assertRaises(ValidationError):reprice(**dict(args,expected_hash='0'*64))
        self.assertEqual(TravelRequest.objects.count(),2)

    def test_other_or_revoked_manager_cannot_prepare_reprice_or_read_http(self):
        one=self.selection();first=self.release(one)
        with self.assertRaises(PermissionDenied):self.selection(actor=self.other)
        with self.assertRaises(PermissionDenied):reprice(actor=self.other,selection_id=one.pk,expected_hash=snapshot_hash(build_snapshot(one)),client_price='200000')
        self.client.force_login(self.other)
        for url in (reverse('presentations:preview',args=[one.pk]),reverse('presentations:archive_pdf',args=[first.pk])):
            self.assertEqual(self.client.get(url).status_code,404)
        self.assertEqual(self.client.post(reverse('presentations:reprice',args=[one.pk]),{}).status_code,404)
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):reprice(actor=self.owner,selection_id=one.pk,expected_hash=snapshot_hash(build_snapshot(one)),client_price='200000')

    def test_unknown_private_keys_rejected_at_each_boundary(self):
        for extra in ({'internal_notes':'PRIVATE'}, {'customer':{'phone':'synthetic'}}, {'operator':'PRIVATE'}, {'basePrice':100}):
            with self.subTest(extra=extra),self.assertRaises(ValidationError):self.selection(dict(self.data,**extra))
        self.assertEqual(Inquiry.objects.count(),0)
        self.assertEqual(SelectedOffer.objects.count(),0)

    def test_invalid_money_currency_version_and_types_rejected(self):
        values=[('base_price','NaN'),('base_price',True),('base_price',180000.0),('base_price','-1'),
                ('base_price','1.001'),('base_price','0'),('base_price','1e3'),('base_price','1000000001'),
                ('client_price','180 000'),('currency','USD'),('currency',False),('selection_confirmed','true'),
                ('synthetic_only',False),('schema','future')]
        for field,value in values:
            with self.subTest(field=field,value=value),self.assertRaises(ValidationError):self.selection(dict(self.data,**{field:value}))
        self.assertEqual(Inquiry.objects.count(),0)

    def test_missing_each_required_value_is_incomplete_not_filled_from_budget(self):
        for field in ('hotel_id','room_type','tariff','price_basis','base_price','client_price','currency','selection_confirmed'):
            data=deepcopy(self.data);data.pop(field)
            normalized=normalize(data,[self.hotels[0].pk]);view=public(normalized)
            self.assertEqual(view['status'],'incomplete',field)
            if field=='client_price':self.assertIsNone(view['client_price'])
        for value in ('Не указано','неизвестно','?',''):
            self.assertEqual(public(normalize(dict(self.data,room_type=value),[self.hotels[0].pk]))['status'],'incomplete')

    def test_incomplete_release_explicit_and_no_invented_price_or_flight(self):
        one=self.selection({'schema':SCHEMA,'synthetic_only':True})
        release=self.release(one);view=release.snapshot['offer']
        self.assertEqual(view['status'],'incomplete')
        self.assertIsNone(view['client_price']);self.assertIsNone(view['flight'])
        self.assertIn('Неполное предложение',release.html_content)
        self.assertIn('Перелёт не заявлен',release.html_content)
        self.assertNotIn('Параметры выбранного предложения указаны',release.html_content)

    def test_wrong_hotel_multiple_hotels_and_unconfirmed_flight_rejected(self):
        with self.assertRaises(ValidationError):self.selection(dict(self.data,hotel_id=str(self.hotels[1].pk)))
        with self.assertRaises(ValidationError):self.selection(hotel_ids=[h.pk for h in self.hotels])
        for flight in ({'confirmed':False,'details':'unknown','evidence':'none'},
                       {'confirmed':True,'details':'Синтетический рейс','evidence':''},
                       {'confirmed':True,'details':'Синтетический рейс','evidence':'fixture','url':'https://invalid'}):
            with self.subTest(flight=flight),self.assertRaises(ValidationError):self.selection(dict(self.data,flight=flight))
        self.assertEqual(Inquiry.objects.count(),0)

    def test_confirmed_synthetic_flight_excludes_private_evidence(self):
        one=self.selection(dict(self.data,flight={'confirmed':True,'details':'Синтетический рейс А → Б, 10 июня 2027', 'evidence':'PRIVATE_FLIGHT_EVIDENCE'}))
        first=self.release(one);text=self.client_text(first)
        self.assertIn('Синтетический рейс',text)
        self.assertNotIn('PRIVATE_FLIGHT_EVIDENCE',text)
        self.assertIn('проверка поставщика не выполнялась',text)

    def test_corruption_and_catalog_change_block_approval(self):
        one=self.selection();checksum=snapshot_hash(build_snapshot(one))
        Hotel.objects.filter(pk=self.hotels[0].pk).update(name='CHANGED')
        with self.assertRaises(ValidationError):approve_request(one,self.owner,checksum)
        SelectedOffer.objects.filter(selection=one).update(digest='0'*64)
        with self.assertRaises(ValidationError):self.release(one)
        self.assertEqual(Release.objects.count(),0)

    def test_private_base_change_invalidates_existing_approval_even_with_same_client_price(self):
        from .services import digest
        one=self.selection();approval=approve_request(one,self.owner,snapshot_hash(build_snapshot(one)))
        data=read(one);data['base_price']='170000.00'
        # Deliberate synthetic direct-DB corruption probes the approval boundary.
        SelectedOffer.objects.filter(selection=one).update(data=data,digest=digest(data))
        with self.assertRaises(ValidationError):publish_request(one,self.owner,approval.pk)
        self.assertEqual(Release.objects.count(),0)

    def test_offer_record_cannot_be_saved_over_existing_and_pdf_failure_atomic(self):
        one=self.selection();row=SelectedOffer.objects.get(selection=one)
        row.data['base_price']='1.00'
        with self.assertRaises(ValidationError):row.save()
        before=Version.objects.count()
        approval=approve_request(one,self.owner,snapshot_hash(build_snapshot(one)))
        with patch('presentations.services.render_pdf',side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):publish_request(one,self.owner,approval.pk)
        self.assertEqual(Release.objects.count(),0);self.assertEqual(Version.objects.count(),before)

    def test_prepare_rolls_back_offer_and_selection_if_ledger_fails(self):
        with patch('inquiries.manager.attach',side_effect=RuntimeError('synthetic ledger failure')):
            with self.assertRaises(RuntimeError):self.selection()
        self.assertEqual(SelectedOffer.objects.count(),0);self.assertEqual(TravelRequest.objects.count(),0)
        self.assertEqual(Inquiry.objects.count(),0)

    def test_form_and_reprice_http_use_existing_pipeline(self):
        self.client.force_login(self.owner)
        data=dict(expected_hash=self.rev.content_hash,hotels=[str(self.hotels[0].pk)],traveller_label='Вымышленная поездка',
                  exact_offer='on',selection_confirmed='on',**{k:self.data[k] for k in ('room_type','tariff','price_basis','base_price','client_price','currency')})
        response=self.client.post(reverse('inquiries:selection',args=[self.work.pk]),data)
        self.assertEqual(response.status_code,302)
        one=TravelRequest.objects.get();html=self.client.get(response.url).content.decode()
        self.assertIn('173421.13',html);self.assertIn('185 000,25',html)
        response=self.client.post(reverse('presentations:reprice',args=[one.pk]),{'content_hash':snapshot_hash(build_snapshot(one)),'client_price':'190000.00'})
        self.assertEqual(response.status_code,302)
        two=TravelRequest.objects.exclude(pk=one.pk).get()
        self.assertEqual(read(two)['base_price'],'173421.13')
        self.assertEqual(Inquiry.objects.count(),1)

    def test_form_cannot_silently_drop_unchecked_offer_or_flight(self):
        self.client.force_login(self.owner)
        base={'expected_hash':self.rev.content_hash,'hotels':[str(self.hotels[0].pk)],'traveller_label':'Synthetic'}
        for extra in ({'room_type':'Стандарт'}, {'exact_offer':'on','flight_details':'Unverified'}):
            self.assertEqual(self.client.post(reverse('inquiries:selection',args=[self.work.pk]),dict(base,**extra)).status_code,422)
        self.assertEqual(Inquiry.objects.count(),0)

    def test_reprice_requires_post_and_csrf(self):
        from django.test import Client
        one=self.selection();client=Client(enforce_csrf_checks=True);client.force_login(self.owner)
        url=reverse('presentations:reprice',args=[one.pk])
        self.assertEqual(client.get(url).status_code,405)
        self.assertEqual(client.post(url,{'client_price':'190000'}).status_code,403)

    def test_public_share_has_only_client_price_and_old_approval_cannot_publish_repeat(self):
        from django.test import Client
        one=self.selection()
        approval=approve_request(one,self.owner,snapshot_hash(build_snapshot(one)))
        first,token=publish_request(one,self.owner,approval.pk)
        tourist=Client()
        url=reverse('presentations:share',args=[token])
        self.assertEqual(tourist.post(url).status_code,302)
        text=tourist.get(url).content.decode()
        pdf=tourist.get(reverse('presentations:share_pdf',args=[token])).content
        text+=''.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(pdf)).pages)
        self.assertNotIn('173421.13',text);self.assertNotIn('base_price',text)
        self.assertIn('185 000,25',text)
        two=reprice(actor=self.owner,selection_id=one.pk,expected_hash=snapshot_hash(build_snapshot(one)),client_price='190000')
        with self.assertRaises(ValidationError):publish_request(two,self.owner,approval.pk)
        self.assertEqual(Release.objects.count(),1)

    def test_ruble_precision_and_untrusted_text_remain_literal(self):
        data=dict(self.data,base_price='0.1',client_price='0.29',room_type='<script>SYNTHETIC</script>')
        selection=self.selection(data);offer=read(selection)
        self.assertEqual((offer['base_price'],offer['client_price']),('0.10','0.29'))
        release=self.release(selection)
        self.assertNotIn('<script>SYNTHETIC</script>',release.html_content)
        self.assertIn('&lt;script&gt;SYNTHETIC&lt;/script&gt;',release.html_content)
        self.assertEqual(release.snapshot['offer']['client_price'],'0.29')
        with self.assertRaises(ValidationError):self.selection(dict(self.data,tariff='line\nbreak'))

    def test_explicit_unknown_markers_never_become_complete_or_confirmed_flight(self):
        for marker in ('нет данных','не подтверждено','не определено','N/A','TBD'):
            for field in ('room_type','tariff','price_basis'):
                with self.subTest(marker=marker,field=field):
                    data=normalize(dict(self.data,**{field:marker}),[self.hotels[0].pk])
                    self.assertEqual(public(data)['status'],'incomplete')
            for field in ('details','evidence'):
                flight={'confirmed':True,'details':'Синтетический рейс','evidence':'Синтетическое основание'}
                flight[field]=marker
                with self.subTest(marker=marker,flight_field=field),self.assertRaises(ValidationError):
                    normalize(dict(self.data,flight=flight),[self.hotels[0].pk])
