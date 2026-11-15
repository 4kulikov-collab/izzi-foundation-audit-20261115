import hashlib
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone
from presentations.models import Approval, Release, TravelRequest
from worksheet_reviews.models import HotelChoice, WorksheetReview
from . import services as s
from .adapters import ADAPTERS
from .models import Confirmation, Inquiry, Operation, SelectionLink, Version, WorkLink


class InquiryContractTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username='synthetic-manager')
        self.other = get_user_model().objects.create_user(username='synthetic-other')
        self.work = self.make_work('SYNTH-A')
        self.selection = TravelRequest.objects.create(owner=self.owner, title='SYNTH selection',
            traveller_summary='Вымышленный турист', requirements='Тест')

    def make_work(self, code):
        data = b'synthetic worksheet, no personal data'
        return WorksheetReview.objects.create(owner=self.owner, work_code=code,
            worksheet_bytes=data, worksheet_type='image/png', worksheet_sha256=hashlib.sha256(data).hexdigest())

    def attach(self, key='first', **kwargs):
        args = {'actor': self.owner, 'work_id': self.work.pk, 'selection_id': self.selection.pk}
        args.update({k: v for k, v in kwargs.items() if k in ('actor', 'work_id', 'selection_id', 'release_id')})
        preview = s.preview(**args)
        return s.attach(key=key, expected_hash=preview['digest'], **args,
                        **{k: v for k, v in kwargs.items() if k not in args})

    def test_new_inquiry_reuses_existing_entities(self):
        v = self.attach(traveller_label='Синтетический турист')
        self.assertEqual(Inquiry.objects.count(), 1)
        self.assertEqual(v.snapshot['work']['id'], str(self.work.pk))
        self.assertEqual(v.snapshot['selection']['id'], str(self.selection.pk))
        self.assertEqual(WorksheetReview.objects.count(), 1)
        self.assertEqual(s.source_file(actor=self.owner, version_id=v.pk)[0], bytes(self.work.worksheet_bytes))
        self.assertEqual(v.inquiry.currency, 'RUB')
        self.assertEqual(v.inquiry.sync_status, 'not_synchronized')

    def test_repeat_request_and_semantic_repeat(self):
        v = self.attach()
        self.assertEqual(self.attach().pk, v.pk)
        self.assertEqual(self.attach(key='retry-new-key').pk, v.pk)
        self.assertEqual(Inquiry.objects.count(), 1)
        self.assertEqual(Version.objects.count(), 1)

    def test_repeated_selection_same_inquiry_and_confirmation(self):
        first = self.attach()
        s.confirm(actor=self.owner, version_id=first.pk, expected_hash=first.digest)
        self.selection.title = 'Вторая версия'
        self.selection.revision = 2
        self.selection.save()
        second = self.attach(key='second', inquiry_id=first.inquiry_id)
        self.assertEqual(second.inquiry_id, first.inquiry_id)
        self.assertEqual(second.number, 2)
        self.assertFalse(Confirmation.objects.filter(version=second).exists())
        self.assertTrue(Confirmation.objects.filter(version=first).exists())
        with self.assertRaises(ValidationError):
            s.confirm(actor=self.owner, version_id=second.pk, expected_hash=first.digest)
        self.assertEqual(Inquiry.objects.count(), 1)

    def test_attach_another_work_and_selection_explicitly(self):
        v = self.attach()
        work = self.make_work('SYNTH-B')
        selection = TravelRequest.objects.create(owner=self.owner, title='Другая подборка')
        second = self.attach(key='another', work_id=work.pk, selection_id=selection.pk, inquiry_id=v.inquiry_id)
        self.assertEqual(second.inquiry_id, v.inquiry_id)
        self.assertEqual(WorkLink.objects.count(), 2)
        self.assertEqual(SelectionLink.objects.count(), 2)

    def test_same_name_is_not_identity(self):
        first = self.attach(traveller_label='Вымышленный турист')
        work = self.make_work('SYNTH-B')
        second = self.attach(key='different', work_id=work.pk, selection_id=None, traveller_label='Вымышленный турист')
        self.assertNotEqual(first.inquiry_id, second.inquiry_id)

    def test_key_collision_and_retry_after_source_changed(self):
        v = self.attach()
        self.work.expert_overview = 'Новый разбор'
        self.work.save()
        with self.assertRaises(ValidationError):
            self.attach()
        # A replay of the exact old request returns its committed result.
        old = s.attach(actor=self.owner, key='first', work_id=self.work.pk,
            selection_id=self.selection.pk, expected_hash=v.digest)
        self.assertEqual(old.pk, v.pk)

    def test_owner_checks_all_entrypoints(self):
        v = self.attach()
        calls = [lambda: s.preview(actor=self.other, work_id=self.work.pk),
            lambda: s.attach(actor=self.other, key='x', work_id=self.work.pk, expected_hash=v.digest),
            lambda: s.confirm(actor=self.other, version_id=v.pk, expected_hash=v.digest),
            lambda: s.read(actor=self.other, inquiry_id=v.inquiry_id),
            lambda: s.source_file(actor=self.other, version_id=v.pk),
            lambda: s.sync_status(actor=self.other, inquiry_id=v.inquiry_id)]
        for call in calls:
            with self.assertRaises(PermissionDenied):
                call()

    def test_stale_active_user_object_rejected(self):
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.attach()
        self.assertFalse(Inquiry.objects.exists())

    def test_foreign_selection_and_foreign_inquiry(self):
        self.selection.owner = self.other
        self.selection.save()
        with self.assertRaises(PermissionDenied):
            self.attach()
        foreign = Inquiry.objects.create(owner=self.other, traveller_label='test')
        with self.assertRaises(PermissionDenied):
            self.attach(selection_id=None, inquiry_id=foreign.pk)

    def test_stale_preview_rolls_back(self):
        p = s.preview(actor=self.owner, work_id=self.work.pk)
        self.work.expert_overview = 'Изменено'
        self.work.save()
        with self.assertRaises(ValidationError):
            s.attach(actor=self.owner, key='stale', work_id=self.work.pk, expected_hash=p['digest'])
        self.assertFalse(Inquiry.objects.exists())

    def test_rebinding_work_and_selection_is_rejected(self):
        v = self.attach()
        other_inquiry = Inquiry.objects.create(owner=self.owner, traveller_label='same')
        with self.assertRaises(ValidationError):
            self.attach(key='move', inquiry_id=other_inquiry.pk)
        work = self.make_work('SYNTH-B')
        with self.assertRaises(ValidationError):
            self.attach(key='move-selection', work_id=work.pk, inquiry_id=other_inquiry.pk)
        self.assertEqual(WorkLink.objects.count(), 1)
        self.assertEqual(SelectionLink.objects.get().inquiry_id, v.inquiry_id)

    def test_snapshot_and_binding_immutable_and_source_not_substituted(self):
        v = self.attach()
        original = v.snapshot
        self.work.worksheet_bytes = b'new synthetic file'
        self.work.worksheet_sha256 = hashlib.sha256(bytes(self.work.worksheet_bytes)).hexdigest()
        self.work.save()
        v.refresh_from_db()
        self.assertEqual(v.snapshot, original)
        with self.assertRaises(ValidationError):
            s.source_file(actor=self.owner, version_id=v.pk)
        v.snapshot = {}
        with self.assertRaises(ValidationError):
            v.save()
        link = WorkLink.objects.get()
        with self.assertRaises(ValidationError):
            link.save()

    def test_deleted_work_keeps_provenance_without_breaking_delete(self):
        v = self.attach()
        original_id = str(self.work.pk)
        self.work.delete()
        v.refresh_from_db()
        self.assertEqual(v.snapshot['work']['id'], original_id)
        with self.assertRaises(ValidationError):
            s.source_file(actor=self.owner, version_id=v.pk)

    def test_transaction_interruption_no_partial_result(self):
        with patch('inquiries.services.Operation.objects.create', side_effect=RuntimeError('synthetic interruption')):
            with self.assertRaises(RuntimeError):
                self.attach()
        for model in (Inquiry, Version, WorkLink, SelectionLink, Operation):
            self.assertFalse(model.objects.exists())
        self.assertEqual(self.attach().inquiry.sync_status, 'not_synchronized')

    def test_external_reference_is_manual_not_sync(self):
        v = self.attach(crm_system='uon', external_id='SYNTH-CRM-42')
        self.assertNotEqual(str(v.inquiry_id), v.inquiry.external_id)
        self.assertEqual(s.sync_status(actor=self.owner, inquiry_id=v.inquiry_id)['status'], 'not_synchronized')
        for adapter in ADAPTERS.values():
            self.assertEqual(adapter.synchronize().status, 'not_synchronized')
        work = self.make_work('SYNTH-B')
        with self.assertRaises(ValidationError):
            self.attach(key='external-dupe', work_id=work.pk, selection_id=None,
                        crm_system='uon', external_id='SYNTH-CRM-42')
        self.assertEqual(Inquiry.objects.count(), 1)

    def test_release_bound_to_exact_immutable_content(self):
        payload = {'schema': 1, 'synthetic': True, 'request_revision': 1}
        approval = Approval.objects.create(request=self.selection, approved_by=self.owner, content_hash=s.digest(payload))
        release = Release.objects.create(request=self.selection, approval=approval, snapshot=payload,
            content_hash=s.digest(payload), html_content='<p>Synthetic</p>', pdf_bytes=b'%PDF-synthetic',
            pdf_hash=hashlib.sha256(b'%PDF-synthetic').hexdigest(), token_digest='a'*64, expires_at=timezone.now())
        v = self.attach(release_id=release.pk)
        self.assertEqual(v.snapshot['release']['id'], str(release.pk))
        self.assertEqual(v.snapshot['release']['snapshot'], payload)
        self.assertEqual(Release.objects.count(), 1)

    def test_work_without_selection_supported(self):
        self.assertIsNone(self.attach(selection_id=None).snapshot['selection'])

    def test_invalid_external_pair_and_key(self):
        for kwargs in ({'crm_system': 'uon'}, {'external_id': '42'}, {'crm_system': 'wrong', 'external_id': '42'}):
            with self.assertRaises(ValidationError):
                self.attach(**kwargs)
        with self.assertRaises(ValidationError):
            self.attach(key='')

    def test_contract_has_no_three_hotel_limit(self):
        for position in range(1, 7):
            HotelChoice.objects.create(review=self.work, position=position, name=f'Synthetic {position}')
        version = self.attach()
        self.assertEqual(len(version.snapshot['work']['hotels']), 6)

    def test_status_never_calls_network_adapter(self):
        version = self.attach()
        with patch.object(ADAPTERS['uon'], 'synchronize', side_effect=RuntimeError('must not call')) as call:
            self.assertEqual(s.sync_status(actor=self.owner, inquiry_id=version.inquiry_id)['status'], 'not_synchronized')
            call.assert_not_called()
