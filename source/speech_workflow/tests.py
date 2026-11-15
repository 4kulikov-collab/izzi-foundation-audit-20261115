import io
import wave
import uuid
from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from django.test import Client, TestCase
from django.urls import reverse

from presentations.models import TravelRequest
from .adapters import (AdapterCancelled, AdapterDisabled, GPT6LunaAdapter,
                       MockLunaAdapter, MockSpeechKitAdapter, ObservationResult,
                       ProviderFailure, TranscriptResult, YandexSpeechKitAdapter,
                       run_stt_with_bounded_retries)
from .audio import MAX_AUDIO_BYTES
from .models import HotelObservationDraft, TranscriptRevision, VoiceSubmission
from .services import (StaleTranscript, apply_transcription_result, confirm_transcript,
                       create_observation_draft, create_submission, current_revision,
                       edit_transcript, cancel_submission, revoke_transcript_confirmation,
                       structure_confirmed_transcript, transcribe_submission)


def wav_upload(seconds=1, value=0, name='synthetic.wav'):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes((value.to_bytes(2, 'little', signed=True)) * (8000 * seconds))
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='audio/wav')


class SpeechWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user('voice-owner', password='synthetic-only')
        cls.other = get_user_model().objects.create_user('voice-other', password='synthetic-only')

    def setUp(self):
        self.client.force_login(self.owner)

    def tearDown(self):
        for item in VoiceSubmission.objects.all():
            if item.audio:
                item.audio.delete(save=False)
        super().tearDown()

    def upload(self, key=None, audio=None):
        return create_submission(self.owner, key or uuid.uuid4(), audio or wav_upload())

    def test_upload_is_private_to_owner_and_has_no_public_storage_url(self):
        item = self.upload()
        with self.assertRaises(ValueError):
            item.audio.url
        source_url = reverse('speech_workflow:audio', args=[item.pk])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('speech_workflow:detail', args=[item.pk])).status_code, 404)
        self.assertEqual(self.client.get(source_url).status_code, 404)
        self.client.force_login(self.owner)
        response = self.client.get(source_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['Content-Type'], 'audio/wav')

    def test_upload_rejects_damaged_unsupported_and_over_limit_files(self):
        for name, data, content_type in [
            ('bad.wav', b'RIFFbroken', 'audio/wav'),
            ('voice.mp3', b'ID3' + b'0' * 20, 'audio/mpeg'),
            ('large.wav', b'0' * (MAX_AUDIO_BYTES + 1), 'audio/wav'),
        ]:
            with self.subTest(name=name):
                with self.assertRaises(ValidationError):
                    self.upload(audio=SimpleUploadedFile(name, data, content_type=content_type))
        self.assertEqual(VoiceSubmission.objects.count(), 0)

    def test_test_duration_limit_is_enforced(self):
        with self.assertRaises(ValidationError):
            self.upload(audio=wav_upload(seconds=301))
        self.assertEqual(VoiceSubmission.objects.count(), 0)

    def test_idempotent_upload_reuses_record_but_key_cannot_change_audio(self):
        key = uuid.uuid4()
        first = self.upload(key=key)
        repeat = self.upload(key=key)
        self.assertEqual(first.pk, repeat.pk)
        self.assertEqual(VoiceSubmission.objects.count(), 1)
        self.assertTrue(first.audio.storage.exists(first.audio.name))
        with self.assertRaises(ValidationError):
            self.upload(key=key, audio=wav_upload(value=100))

    def test_failed_initial_revision_save_cleans_orphan_file_without_touching_existing_upload(self):
        saved = []

        def fail_revision_create(*args, **kwargs):
            item = kwargs['submission']
            saved.append((item.audio.storage, item.audio.name))
            raise DatabaseError('synthetic database write failure')

        from unittest.mock import patch
        with patch.object(TranscriptRevision.objects, 'create', side_effect=fail_revision_create):
            with self.assertRaises(DatabaseError):
                create_submission(self.owner, uuid.uuid4(), wav_upload())
        self.assertEqual(len(saved), 1)
        self.assertFalse(saved[0][0].exists(saved[0][1]))
        self.assertEqual(VoiceSubmission.objects.count(), 0)

        key = uuid.uuid4()
        existing = self.upload(key=key)
        replay = self.upload(key=key)
        self.assertEqual(existing.pk, replay.pk)
        self.assertTrue(existing.audio.storage.exists(existing.audio.name))

    def test_manual_edit_is_versioned_confirmation_binds_exact_content(self):
        item = self.upload()
        first = current_revision(item)
        changed = edit_transcript(item.pk, self.owner, first.number, first.content_hash,
                                  'Вымышленная заметка: тихий пляж.')
        self.assertEqual(changed.number, 2)
        confirmed = confirm_transcript(item.pk, self.owner, changed.number, changed.content_hash)
        self.assertIsNotNone(confirmed.confirmed_at)
        latest = edit_transcript(item.pk, self.owner, confirmed.number, confirmed.content_hash,
                                 'Вымышленная заметка с исправлением даты.')
        self.assertEqual(latest.number, 3)
        self.assertIsNone(latest.confirmed_at)
        with self.assertRaises(StaleTranscript):
            confirm_transcript(item.pk, self.owner, confirmed.number, confirmed.content_hash)
        with self.assertRaises(ValidationError):
            create_observation_draft(item.pk, self.owner, confirmed.number, confirmed.content_hash, {})
        self.assertEqual(item.revisions.count(), 3)

    def test_late_adapter_result_cannot_replace_a_new_manual_revision(self):
        item = self.upload()
        first = current_revision(item)
        manual = edit_transcript(item.pk, self.owner, first.number, first.content_hash, 'Ручная правка.')
        canned = MockSpeechKitAdapter(TranscriptResult(
            text='Поздняя распечатка.', segments=((0.0, 1.0, 'фрагмент'),),
            uncertainties=('название отеля неразборчиво',)))
        result = canned.transcribe(io.BytesIO(b'not read by mock'))
        with self.assertRaises(StaleTranscript):
            apply_transcription_result(item.pk, self.owner, first.number, first.content_hash, result)
        item.refresh_from_db()
        self.assertEqual(item.current_revision, manual.number)
        self.assertEqual(item.revisions.count(), 2)

    def test_mock_results_keep_uncertainties_and_do_not_claim_real_inference(self):
        item = self.upload()
        original = current_revision(item)
        transcript = TranscriptResult(text='[говорящий не определён] короткий фрагмент',
                                      segments=({'start': 0.0, 'end': 1.0, 'text': 'фрагмент 1', 'speaker': None},
                                                {'start': 1.0, 'end': 2.0, 'text': 'фрагмент 2', 'speaker': None}),
                                      uncertainties=('два голоса, авторы не определены',))
        result = MockSpeechKitAdapter(transcript).transcribe(io.BytesIO(b'not parsed'))
        stored = apply_transcription_result(item.pk, self.owner, original.number, original.content_hash, result)
        structured = ObservationResult(fields={'hotel_name': '', 'date': ''})
        llm_result = MockLunaAdapter(structured).structure_observation('не передаётся в сеть')
        self.assertTrue(result.is_mock)
        self.assertEqual(stored.segments[0]['speaker'], None)
        self.assertEqual(stored.uncertainties, ['два голоса, авторы не определены'])
        self.assertTrue(llm_result.is_mock)
        self.assertEqual(llm_result.fields['hotel_name'], '')

    def test_silence_is_not_guessed_into_transcript(self):
        item = self.upload(audio=wav_upload(seconds=2))
        original = current_revision(item)
        quiet = MockSpeechKitAdapter(TranscriptResult(
            text='', segments=(), uncertainties=('тишина: речи не обнаружено в тестовом примере',)))
        stored = apply_transcription_result(item.pk, self.owner, original.number, original.content_hash,
                                            quiet.transcribe(io.BytesIO(b'canned')))
        self.assertEqual(stored.text, '')
        self.assertEqual(stored.uncertainties[0][:6], 'тишина')
        self.assertFalse(stored.confirmed_at)

    def test_live_adapters_are_disabled_and_do_not_create_transcript_versions(self):
        item = self.upload()
        before = item.revisions.count()
        with self.assertRaises(AdapterDisabled):
            YandexSpeechKitAdapter().transcribe(item.audio)
        with self.assertRaises(AdapterDisabled):
            GPT6LunaAdapter().structure_observation('synthetic test text')
        self.assertEqual(item.revisions.count(), before)
        item.refresh_from_db()
        self.assertEqual(item.status, VoiceSubmission.Status.MANUAL_TRANSCRIPTION)

    def test_two_adapter_pipeline_uses_stt_for_audio_and_luna_for_confirmed_text_only(self):
        item = self.upload()
        original = current_revision(item)
        mock_stt = MockSpeechKitAdapter(TranscriptResult(
            text='Синтетическая расшифровка.',
            segments=({'start': 0.0, 'end': 1.0, 'text': 'Синтетическая расшифровка.'},),
            uncertainties=('отель нужно уточнить вручную',)))
        transcript = transcribe_submission(item.pk, self.owner, original.number, original.content_hash,
                                           adapter=mock_stt)
        self.assertEqual(transcript.processor, 'mock-speechkit')
        self.assertEqual(transcript.language_code, 'ru-RU')
        confirmed = confirm_transcript(item.pk, self.owner, transcript.number, transcript.content_hash)
        suggestion = ObservationResult(fields={'hotel_name': '', 'theme': '', 'experience': ''})
        mock_luna = MockLunaAdapter(suggestion)
        structured = structure_confirmed_transcript(item.pk, self.owner, confirmed.number,
                                                     confirmed.content_hash, adapter=mock_luna)
        self.assertTrue(structured.is_mock)
        self.assertEqual(structured.fields['hotel_name'], '')
        self.assertFalse(HotelObservationDraft.objects.exists())
        self.assertEqual(TravelRequest.objects.count(), 0)

    def test_stt_result_is_discarded_if_owner_is_deactivated_during_adapter_call(self):
        item = self.upload()
        original = current_revision(item)

        class DeactivateOwnerThenReturn:
            def transcribe(inner_self, _audio):
                get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
                return TranscriptResult(text='Late mock result')

        with self.assertRaises(PermissionDenied):
            transcribe_submission(item.pk, self.owner, original.number, original.content_hash,
                                  adapter=DeactivateOwnerThenReturn())
        item.refresh_from_db()
        self.assertEqual(item.current_revision, 1)
        self.assertEqual(item.revisions.count(), 1)

    def test_luna_result_is_rejected_if_confirmation_edit_cancel_or_access_changes_mid_call(self):
        for change in ('revoke', 'edit', 'cancel', 'deactivate'):
            with self.subTest(change=change):
                item = self.upload()
                revision = current_revision(item)
                revision = edit_transcript(item.pk, self.owner, revision.number, revision.content_hash,
                                           f'Synthetic transcript for {change}.')
                revision = confirm_transcript(item.pk, self.owner, revision.number, revision.content_hash)

                class MutatingLuna:
                    def structure_observation(inner_self, _text):
                        if change == 'revoke':
                            revoke_transcript_confirmation(item.pk, self.owner, revision.number,
                                                           revision.content_hash)
                        elif change == 'edit':
                            edit_transcript(item.pk, self.owner, revision.number, revision.content_hash,
                                            'Edited while mock was running.')
                        elif change == 'cancel':
                            cancel_submission(item.pk, self.owner)
                        elif change == 'deactivate':
                            get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
                        return ObservationResult(fields={'hotel_name': 'must not escape'})

                expected = PermissionDenied if change == 'deactivate' else ValidationError
                with self.assertRaises(expected):
                    structure_confirmed_transcript(item.pk, self.owner, revision.number,
                                                   revision.content_hash, adapter=MutatingLuna())
                self.assertFalse(HotelObservationDraft.objects.filter(transcript_revision=revision).exists())

    def test_cancel_post_unknown_uuid_returns_private_404(self):
        response = self.client.post(reverse('speech_workflow:cancel', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)
        self.assertIn('no-store', response['Cache-Control'])

    def test_default_service_pipeline_stays_offline(self):
        item = self.upload()
        original = current_revision(item)
        with self.assertRaises(AdapterDisabled):
            transcribe_submission(item.pk, self.owner, original.number, original.content_hash)
        text_version = edit_transcript(item.pk, self.owner, original.number, original.content_hash, 'Синтетический текст.')
        confirmed = confirm_transcript(item.pk, self.owner, text_version.number, text_version.content_hash)
        with self.assertRaises(AdapterDisabled):
            structure_confirmed_transcript(item.pk, self.owner, confirmed.number, confirmed.content_hash)
        item.refresh_from_db()
        self.assertEqual(item.revisions.count(), 2)
        self.assertEqual(item.status, VoiceSubmission.Status.CONFIRMED)

    def test_confirmed_transcript_creates_review_only_observation_without_crm_write(self):
        item = self.upload()
        revision = current_revision(item)
        revision = edit_transcript(item.pk, self.owner, revision.number, revision.content_hash,
                                    'Вымышленное наблюдение: номер рядом с лифтом.')
        revision = confirm_transcript(item.pk, self.owner, revision.number, revision.content_hash)
        observation = create_observation_draft(item.pk, self.owner, revision.number, revision.content_hash, {
            'hotel_name': 'Отель Пример', 'observed_on': date(2026, 11, 1), 'building_room': 'Корпус А, номер 101',
            'theme': 'Номер', 'experience': HotelObservationDraft.Experience.INSPECTION,
            'observation_text': 'Осмотр вымышленного номера; не личное проживание.',
        })
        repeated = create_observation_draft(item.pk, self.owner, revision.number, revision.content_hash, {
            'hotel_name': 'Отель Пример', 'observed_on': date(2026, 11, 1), 'building_room': 'Корпус А, номер 101',
            'theme': 'Номер', 'experience': HotelObservationDraft.Experience.INSPECTION,
            'observation_text': 'Осмотр вымышленного номера; не личное проживание.',
        })
        self.assertEqual(observation.pk, repeated.pk)
        self.assertEqual(observation.author_id, self.owner.pk)
        self.assertEqual(observation.status, HotelObservationDraft.Status.NEEDS_REVIEW)
        self.assertEqual(TravelRequest.objects.count(), 0)

    def test_authenticated_routes_have_private_headers_and_upload_reaches_manual_stage(self):
        response = self.client.post(reverse('speech_workflow:upload'), {
            'idempotency_key': str(uuid.uuid4()), 'audio': wav_upload(),
        })
        self.assertEqual(response.status_code, 302)
        item = VoiceSubmission.objects.get()
        page = self.client.get(reverse('speech_workflow:detail', args=[item.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertIn('no-store', page['Cache-Control'])
        self.assertEqual(item.status, VoiceSubmission.Status.MANUAL_TRANSCRIPTION)
        self.assertEqual(TranscriptRevision.objects.filter(submission=item).count(), 1)

    def test_web_flow_upload_edit_confirm_and_create_observation_draft(self):
        response = self.client.post(reverse('speech_workflow:upload'), {
            'idempotency_key': str(uuid.uuid4()), 'audio': wav_upload(),
        })
        self.assertEqual(response.status_code, 302)
        item = VoiceSubmission.objects.get()
        revision = current_revision(item)
        response = self.client.post(reverse('speech_workflow:edit_transcript', args=[item.pk]), {
            'expected_revision': revision.number, 'expected_hash': revision.content_hash,
            'text': 'Вымышленная ручная расшифровка.',
        })
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        revision = current_revision(item)
        response = self.client.post(reverse('speech_workflow:confirm_transcript', args=[item.pk]), {
            'expected_revision': revision.number, 'expected_hash': revision.content_hash,
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse('speech_workflow:observation', args=[item.pk]), {
            'expected_revision': revision.number, 'expected_hash': revision.content_hash,
            'hotel_name': 'Отель Тестовый', 'observed_on': '2026-11-01', 'building_room': 'Корпус А',
            'theme': 'Питание', 'experience': HotelObservationDraft.Experience.INSPECTION,
            'observation_text': 'Только вымышленный тестовый текст.',
        })
        self.assertEqual(response.status_code, 302)
        observation = HotelObservationDraft.objects.get()
        self.assertEqual(observation.status, HotelObservationDraft.Status.NEEDS_REVIEW)
        self.assertEqual(TravelRequest.objects.count(), 0)

    def test_deactivated_owner_loses_audio_and_transcript_access(self):
        item = self.upload()
        self.owner.is_active = False
        self.owner.save(update_fields=['is_active'])
        for path in (reverse('speech_workflow:detail', args=[item.pk]),
                     reverse('speech_workflow:audio', args=[item.pk])):
            response = self.client.get(path)
            self.assertIn(response.status_code, (302, 404))
            self.assertIn('no-store', response['Cache-Control'])

    def test_stale_edit_post_returns_conflict_and_keeps_latest_manual_version(self):
        item = self.upload()
        first = current_revision(item)
        latest = edit_transcript(item.pk, self.owner, first.number, first.content_hash, 'Новая редакция.')
        response = self.client.post(reverse('speech_workflow:edit_transcript', args=[item.pk]), {
            'expected_revision': first.number, 'expected_hash': first.content_hash, 'text': 'Устаревшая вкладка.',
        })
        self.assertEqual(response.status_code, 409)
        item.refresh_from_db()
        self.assertEqual(current_revision(item).pk, latest.pk)
        self.assertEqual(item.revisions.count(), 2)

    def test_timeout_and_429_have_at_most_two_mock_attempts_then_manual_fallback(self):
        item = self.upload()
        for category in ('timeout', 'http_429'):
            with self.subTest(category=category):
                class RetryThenFail:
                    calls = 0

                    def transcribe(self, _audio):
                        self.calls += 1
                        raise ProviderFailure(category, retryable=True)

                adapter = RetryThenFail()
                with self.assertRaises(ProviderFailure):
                    run_stt_with_bounded_retries(adapter, io.BytesIO(b'synthetic'))
                self.assertEqual(adapter.calls, 2)
        self.assertEqual(item.revisions.count(), 1)
        item.refresh_from_db()
        self.assertEqual(item.status, VoiceSubmission.Status.MANUAL_TRANSCRIPTION)

    def test_access_error_is_not_retried_and_cancel_discards_late_result(self):
        item = self.upload()

        class Denied:
            calls = 0

            def transcribe(self, _audio):
                self.calls += 1
                raise ProviderFailure('access_denied', retryable=False)

        denied = Denied()
        with self.assertRaises(ProviderFailure):
            run_stt_with_bounded_retries(denied, io.BytesIO(b'synthetic'))
        self.assertEqual(denied.calls, 1)

        class FinishesAfterCancel:
            def transcribe(self, _audio):
                return TranscriptResult(text='Поздний тестовый ответ')

        with self.assertRaises(AdapterCancelled):
            run_stt_with_bounded_retries(FinishesAfterCancel(), io.BytesIO(b'synthetic'), cancelled=lambda: True)
        self.assertEqual(item.revisions.count(), 1)
        item.refresh_from_db()
        self.assertEqual(item.status, VoiceSubmission.Status.MANUAL_TRANSCRIPTION)

    def test_revoked_confirmation_withdraws_draft_and_cancel_blocks_late_updates(self):
        item = self.upload()
        revision = current_revision(item)
        revision = edit_transcript(item.pk, self.owner, revision.number, revision.content_hash, 'Вымышленный текст.')
        revision = confirm_transcript(item.pk, self.owner, revision.number, revision.content_hash)
        observation = create_observation_draft(item.pk, self.owner, revision.number, revision.content_hash, {
            'hotel_name': 'Отель Пример', 'observed_on': date(2026, 11, 1), 'building_room': '',
            'theme': 'Пляж', 'experience': HotelObservationDraft.Experience.PERSONAL_STAY,
            'observation_text': 'Личное синтетическое наблюдение.',
        })
        revoke_transcript_confirmation(item.pk, self.owner, revision.number, revision.content_hash)
        observation.refresh_from_db()
        self.assertEqual(observation.status, HotelObservationDraft.Status.WITHDRAWN)
        with self.assertRaises(ValidationError):
            create_observation_draft(item.pk, self.owner, revision.number, revision.content_hash, {})
        cancel_submission(item.pk, self.owner)
        item.refresh_from_db()
        self.assertEqual(item.status, VoiceSubmission.Status.CANCELLED)
        with self.assertRaises(ValidationError):
            edit_transcript(item.pk, self.owner, revision.number, revision.content_hash, 'Поздняя правка')
