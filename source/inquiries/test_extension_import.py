"""Synthetic existing extension validator -> exact offer -> immutable real local PDFs."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError, PermissionDenied
from django.test import TestCase
from django.urls import reverse
from pypdf import PdfReader
from presentations.models import Hotel, TravelRequest, Release
from presentations.services import build_snapshot, snapshot_hash, approve_request, publish_request
from .extension_import import prepare_import, checked_source
from .offers import read, reprice
from .models import Inquiry, SelectedOffer, SelectionLink, Version

FIXTURES = Path(__file__).parent/'fixtures/extension'


def raw(name='first'):
    return (FIXTURES/(name+'.json')).read_text()


class ExtensionImportTests(TestCase):
    def setUp(self):
        from .test_manager import ManagerFlowTests
        ManagerFlowTests.setUp(self)
        from .management.commands.seed_manager_demo import demo_fields
        from worksheet_reviews.ocr_services import save_fields
        self.rev = save_fields(self.work, self.owner, self.rev.number, self.rev.content_hash, demo_fields(), confirm=True)
        self.client.force_login(self.owner)
        self.hotel = Hotel.objects.create(name='Синтетический отель ALFA',
            summary='PRIVATE_CATALOG_TEXT', pros='PRIVATE_CATALOG_PROS',
            limitations='PRIVATE_CATALOG_NOTE', source_reference='PRIVATE_CATALOG_SOURCE')

    def prepare(self, name='first', **changes):
        args = dict(actor=self.owner, work_id=self.work.pk, expected_hash=self.rev.content_hash,
                    hotel_id=self.hotel.pk, raw=raw(name), manager_confirmed=True,
                    traveller_label='Синтетическое обращение ALFA')
        args.update(changes)
        return prepare_import(**args)

    def release(self, selection):
        approval = approve_request(selection, self.owner, snapshot_hash(build_snapshot(selection)))
        return publish_request(selection, self.owner, approval.pk)[0]

    def assert_empty(self):
        self.assertEqual(Inquiry.objects.count(), 0)
        self.assertEqual(SelectedOffer.objects.count(), 0)
        self.assertEqual(TravelRequest.objects.count(), 0)

    def test_selected_similar_row_and_repeat_pdf_keep_exact_private_source(self):
        one=self.prepare('similar');first=self.release(one)
        original=(first.snapshot, first.html_content, bytes(first.pdf_bytes))
        two=self.prepare('repeat');second=self.release(two)
        self.assertEqual(Inquiry.objects.count(),1)
        self.assertNotEqual(one.pk,two.pk)
        self.assertEqual(self.prepare('similar').pk,one.pk)
        self.assertEqual(self.prepare('repeat').pk,two.pk)
        a,b=read(one),read(two)
        self.assertEqual((a['room_type'],b['room_type']),('Стандарт','Стандарт'))
        self.assertEqual((a['base_price'],b['base_price']),('173456.79','173456.78'))
        self.assertEqual((a['client_price'],b['client_price']),('180000.26','181234.25'))
        self.assertEqual(a['import_source']['selected']['meal'],'Полупансион')
        self.assertEqual(b['import_source']['selected']['meal'],'Завтрак')
        rows=[]
        for selection,release,offer in ((one,first,a),(two,second,b)):
            pages=PdfReader(io.BytesIO(bytes(release.pdf_bytes))).pages
            self.assertEqual(len(pages),1, 'Краткий синтетический импорт не должен давать пустую вторую страницу')
            pdf='\n'.join(p.extract_text() for p in pages)
            content=release.html_content+json.dumps(release.snapshot,ensure_ascii=False)+pdf
            self.assertEqual(release.snapshot['offer']['status'],'incomplete')
            self.assertIsNone(release.snapshot['offer']['tariff'])
            self.assertIsNone(release.snapshot['offer']['price_basis'])
            self.assertIsNone(release.snapshot['offer']['flight'])
            for private in ('173456.78','173456,78','173 456,78','173456.79','173 456,79',
                            'sourcePriceKopecks','import_source','Синтетический оператор',
                            'Синтетическая заметка','PRIVATE_CATALOG','for-operators.php','th-local-sha256'):
                self.assertNotIn(private,content)
            self.assertIn('неполное предложение',pdf.lower())
            self.assertIn('Перелёт не заявлен',pdf)
            self.assertIn(offer['import_source']['selected']['meal'],pdf)
            version=Version.objects.get(snapshot__release__id=str(release.pk))
            self.assertEqual(version.inquiry_id,Inquiry.objects.get().pk)
            self.assertEqual(version.snapshot['selection']['exact_offer']['import_source'],offer['import_source'])
            self.assertEqual(self.client.get(reverse('presentations:archive_pdf',args=[release.pk])).content,bytes(release.pdf_bytes))
            rows.append({'inquiry_id':str(version.inquiry_id),'release_id':str(release.pk),
                'pdf_sha256':release.pdf_hash,'offer':offer})
        self.assertNotIn('Завтрак',first.html_content)
        self.assertNotIn('Полупансион',second.html_content)
        Hotel.objects.filter(pk=self.hotel.pk).update(name='CHANGED',summary='CHANGED')
        first.refresh_from_db()
        self.assertEqual((first.snapshot,first.html_content,bytes(first.pdf_bytes)),original)
        self.assertEqual(build_snapshot(one)['hotels'][0]['name'],'Синтетический отель ALFA')
        output=os.environ.get('IZZI_IMPORT_PROOF_DIR')
        if output:
            dest=Path(output);dest.mkdir(parents=True,exist_ok=False)
            for i,rel in enumerate((first,second),1):
                (dest/f'release-{i}.pdf').write_bytes(bytes(rel.pdf_bytes))
                (dest/f'release-{i}.html').write_text(rel.html_content)
            (dest/'proof-internal.json').write_text(json.dumps({'synthetic_only':True,'method':'Django test client and existing services; not browser acceptance', 'rows':rows},ensure_ascii=False,indent=2))

    def test_first_and_adjusted_price_preserve_base_and_source(self):
        one=self.prepare();a=read(one)
        two=reprice(actor=self.owner,selection_id=one.pk,expected_hash=snapshot_hash(build_snapshot(one)),client_price='199999.99')
        b=read(two)
        self.assertEqual(a['import_source'],b['import_source'])
        self.assertEqual(a['base_price'],b['base_price'])
        self.assertEqual(a['client_price'],'180000.25')
        self.assertEqual(b['client_price'],'199999.99')
        self.assertEqual(Inquiry.objects.count(),1)

    def test_selected_index_is_not_first_and_confirmed_price_not_observed(self):
        one=self.prepare('first');two=self.prepare('similar')
        self.assertNotEqual(one.pk,two.pk)
        self.assertEqual(read(two)['base_price'],'173456.79')
        self.assertEqual(read(two)['client_price'],'180000.26')
        self.assertNotEqual(read(two)['client_price'],read(one)['client_price'])

    def test_both_supported_versions_reuse_existing_validator(self):
        for name in ('legacy07','first'):
            source=checked_source(raw(name))
            self.assertIn(source['extension_version'],('0.7.0','0.8.0'))
            self.assertEqual(source['selected']['room'],'Стандарт')
        self.prepare('legacy07')
        # Same confirmation ID with a changed reader/version is not quietly overwritten.
        with self.assertRaises(ValidationError):self.prepare('first')

    def test_missing_room_price_tariff_schema_are_not_guessed(self):
        original=json.loads(raw())
        for key in ('room','sourcePriceKopecks','currency'):
            p=deepcopy(original);del p['record']['data']['lastConfirmed']['entries'][0]['snapshot']['normalized'][key]
            with self.subTest(key=key),self.assertRaisesMessage(ValidationError,'неполон'):
                self.prepare(raw=json.dumps(p))
        self.assert_empty()
        one=self.prepare();self.assertIsNone(read(one)['tariff'])
        self.assertIsNone(read(one)['price_basis'])

    def test_unknown_version_private_fields_and_flight_rejected(self):
        original=json.loads(raw())
        values=[]
        p=deepcopy(original);p['extensionVersion']='0.9.0';values.append(p)
        p=deepcopy(original);p['customer_phone']='FAKE_PRIVATE';values.append(p)
        p=deepcopy(original);p['record']['data']['lastConfirmed']['entries'][0]['snapshot']['normalized']['flightType']='Подтверждён';values.append(p)
        p=deepcopy(original);p['record']['data']['items'][0]['snapshot']['provenance']['sourceUrl']='https://elsewhere.invalid';values.append(p)
        for p in values:
            with self.assertRaises(ValidationError):self.prepare(raw=json.dumps(p))
        self.assert_empty()

    def test_ambiguous_wrong_selected_stale_or_swapped_rows_rejected(self):
        original=json.loads(raw());values=[]
        p=deepcopy(original);p['selectedOfferId']='th-local-sha256:'+'0'*64;values.append(p)
        p=deepcopy(original);d=p['record']['data'];c=d['lastConfirmed'];d['items'][1]=deepcopy(d['items'][0]);c['entries'][1]=deepcopy(c['entries'][0]);c['publicView'][1]=deepcopy(c['publicView'][0]);values.append(p)
        p=deepcopy(original);p['record']['data']['dirty']=True;values.append(p)
        p=deepcopy(original);p['record']['data']['lastConfirmed']['publicView'].reverse();values.append(p)
        p=deepcopy(original);p['record']['data']['items'][0]['priceText']='1.00';values.append(p)
        for p in values:
            with self.assertRaises(ValidationError):self.prepare(raw=json.dumps(p))
        self.assert_empty()

    def test_explicit_mapping_confirmation_and_current_worksheet_required(self):
        for kw in ({'hotel_id':self.hotels[0].pk},{'manager_confirmed':False},{'manager_confirmed':'true'},{'expected_hash':'0'*64}):
            with self.subTest(kw=kw),self.assertRaises(ValidationError):self.prepare(**kw)
        self.assert_empty()

    def test_other_and_revoked_manager_cannot_import_or_read_private_source(self):
        with patch('inquiries.extension_import.checked_source') as parser:
            with self.assertRaises(PermissionDenied):self.prepare(actor=self.other)
            parser.assert_not_called()
        one=self.prepare();release=self.release(one)
        self.client.force_login(self.other)
        for url in (reverse('presentations:preview',args=[one.pk]),reverse('presentations:archive_pdf',args=[release.pk])):
            response=self.client.get(url);self.assertEqual(response.status_code,404)
            self.assertNotIn(b'173456',response.content)
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):self.prepare()

    def test_revocation_during_validation_rechecked_before_mutation(self):
        real=checked_source
        def revoke(value):
            data=real(value);get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False);return data
        with patch('inquiries.extension_import.checked_source',side_effect=revoke):
            with self.assertRaises(PermissionDenied):self.prepare()
        self.assert_empty()

    def test_changed_confirmation_rejected_and_draft_cannot_move_inquiry(self):
        self.prepare()
        p=json.loads(raw('repeat'));p['record']['data']['lastConfirmed']['id']=json.loads(raw())['record']['data']['lastConfirmed']['id']
        with self.assertRaisesMessage(ValidationError,'повторно использовано'):self.prepare(raw=json.dumps(p))
        from .extension_import import check_binding
        from types import SimpleNamespace
        other=Inquiry.objects.create(owner=self.owner,traveller_label='Синтетическое другое')
        with self.assertRaisesMessage(ValidationError,'другим обращением'):
            check_binding(self.owner,SimpleNamespace(pk='00000000-0000-4000-8000-000000000000'),other.pk,checked_source(raw()))
        self.assertEqual(TravelRequest.objects.count(),1)

    def test_failed_ledger_does_not_register_source_or_create_partial_selection(self):
        with patch('inquiries.manager.attach',side_effect=ValidationError('synthetic failure')):
            with self.assertRaises(ValidationError):self.prepare()
        self.assert_empty()
        self.prepare();self.assertEqual(SelectedOffer.objects.count(),1)

    def test_old_approval_cannot_publish_changed_selected_row(self):
        one=self.prepare();approval=approve_request(one,self.owner,snapshot_hash(build_snapshot(one)))
        two=self.prepare('similar')
        with self.assertRaises(ValidationError):publish_request(two,self.owner,approval.pk)
        self.assertEqual(Release.objects.count(),0)

    def test_corrupt_source_or_private_price_rejected(self):
        one=self.prepare();data=read(one)
        data['import_source']['selected']['room']='Люкс'
        from .services import digest
        SelectedOffer.objects.filter(selection=one).update(data=data,digest=digest(data))
        with self.assertRaises(ValidationError):build_snapshot(one)

    def test_runtime_failure_timeout_or_duplicate_keys_do_not_leak_input(self):
        for fault in (FileNotFoundError('PRIVATE'),subprocess.TimeoutExpired('PRIVATE',5)):
            with patch('inquiries.extension_import.subprocess.run',side_effect=fault):
                with self.assertRaises(ValidationError) as error:self.prepare()
                self.assertNotIn('PRIVATE',str(error.exception))
        with patch('inquiries.extension_import.shutil.which',return_value=None):
            with self.assertRaisesMessage(ValidationError,'недоступен'):self.prepare()
        for data in ('{"schema":1,"schema":2}', 'x'*(128*1024+1), '\ud800'):
            with self.assertRaises(ValidationError):self.prepare(raw=data)
        self.assert_empty()
