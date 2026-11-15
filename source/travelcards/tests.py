"""Synthetic photo-card checks, including an optional real local Tesseract run."""
import copy
import io
import subprocess
import uuid
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from presentations.models import Hotel, Release, TravelRequest
from presentations.services import approve_request, build_snapshot, publish_request, snapshot_hash
from .forms import CardFieldsForm, FIELD_LABELS
from presentations.forms import TravelRequestForm
from .models import CardRevision, CardSource, PhotoCard
from .ocr import RECOGNITION_KEY, parse_card_text, recognize_card_photo, russian_tesseract_available
from .ocr_adapters import (MockVisionOCRAdapter, OCRAdapterDisabled, OCRBlock,
                           OCRResult, photo_ocr_adapter)
from .services import (confirm_card, create_card, current_revision, inspect_photo,
                       prepare_request, replace_photo, save_fields, digest)


def photo(color='white', file_format='PNG', declared=None, dimensions=(100, 80)):
    data = io.BytesIO()
    Image.new('RGB', dimensions, color).save(data, format=file_format)
    mime = declared or ('image/png' if file_format == 'PNG' else 'image/jpeg')
    return SimpleUploadedFile('synthetic.png', data.getvalue(), content_type=mime)


class PhotoCardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user('card-owner', password='synthetic-demo-only')
        cls.other = get_user_model().objects.create_user('card-other', password='synthetic-demo-only')
        cls.hotels = [Hotel.objects.create(name=f'Тестовый вариант {i}', summary='Вымышленный',
                                          pros='Тестовый плюс', limitations='Тестовое ограничение',
                                          source_reference='Синтетика') for i in range(3)]

    def setUp(self):
        self.card = create_card(self.owner, 'Тестовая карта', uuid.uuid4(), photo())
        self.client.force_login(self.owner)

    def fields(self):
        value = copy.deepcopy(current_revision(self.card).fields)
        value['party'] = {'value': 'Двое взрослых — вымышленные данные', 'state': 'known',
                          'source': {'kind': 'manual_review', 'page': 1, 'description': 'Строка 1 тестового фото'}}
        return value

    def confirm(self):
        self.card.refresh_from_db()
        rev = current_revision(self.card)
        return confirm_card(self.card, self.owner, rev.number, rev.content_hash)

    def replace(self, upload, revision=None):
        revision = revision or current_revision(self.card)
        return replace_photo(self.card, self.owner, revision.number, revision.content_hash, upload)

    def post_photo_replacement(self, upload, revision):
        response = self.client.post(reverse('travelcards:replace_photo', args=[self.card.pk]), {
            'expected_revision': revision.number,
            'expected_hash': revision.content_hash,
            'photo': upload,
        })
        self.card.refresh_from_db()
        return response

    def prepare(self):
        rev = self.confirm()
        request = prepare_request(self.card, self.owner, rev.number, rev.content_hash, self.hotels)
        self.card.refresh_from_db()
        from inquiries.models import SelectionLink
        if not SelectionLink.objects.filter(selection=request).exists():
            from worksheet_reviews.services import create_review
            from inquiries.services import preview, attach
            source = current_revision(self.card).source
            work = create_review(owner=self.owner, work_code='SYNTH-CARD', worksheet_bytes=bytes(source.image_bytes), worksheet_type=source.mime_type, hotel_choices=[])
            snapshot = preview(actor=self.owner, work_id=work.pk, selection_id=request.pk)
            attach(actor=self.owner, key='fixture-link', work_id=work.pk, selection_id=request.pk, expected_hash=snapshot['digest'], traveller_label='Synthetic')
        return request

    def test_true_photo_bytes_are_required_and_mime_must_match(self):
        for upload in [SimpleUploadedFile('fake.png', b'<svg>fake</svg>', content_type='image/png'),
                       photo(declared='image/jpeg'), photo(file_format='GIF', declared='image/gif')]:
            with self.assertRaises(ValidationError):
                inspect_photo(upload)
        self.assertEqual(inspect_photo(photo())['mime_type'], 'image/png')
        self.assertEqual(inspect_photo(photo(file_format='JPEG'))['mime_type'], 'image/jpeg')

    def test_limits_and_damaged_file_rejected(self):
        for upload in [SimpleUploadedFile('huge.png', b'x' * (5 * 1024 * 1024 + 1), content_type='image/png'),
                       photo(dimensions=(4000, 3001))]:
            with self.assertRaises(ValidationError):
                inspect_photo(upload)
        upload = photo()
        broken = SimpleUploadedFile('broken.png', upload.read()[:30], content_type='image/png')
        with self.assertRaises(ValidationError):
            inspect_photo(broken)

    def test_text_parser_keeps_only_explicit_fields_and_unknown_markers(self):
        fields, metadata = parse_card_text(
            'Заголовок: тест\nСостав: Двое взрослых\nДаты: не указаны\n'
            'Бюджет: запись неразборчива\nСторонний текст: не переносить',
            engine_version='synthetic Tesseract',
        )
        self.assertEqual(metadata['status'], 'recognized')
        self.assertEqual(fields['party']['value'], 'Двое взрослых')
        self.assertEqual(fields['party']['state'], 'ocr_candidate')
        self.assertEqual(fields['dates']['value'], '')
        self.assertEqual(fields['dates']['state'], 'unknown')
        self.assertEqual(fields['budget']['value'], '')
        self.assertEqual(fields['budget']['state'], 'unreadable')
        self.assertEqual(fields['wishes']['value'], '')
        self.assertEqual(fields['wishes']['state'], 'unknown')
        self.assertNotIn('Сторонний текст', str(fields))

    def test_ocr_keeps_repeated_values_and_marks_disagreement_without_last_value_wins(self):
        identical, identical_meta = parse_card_text('Бюджет: 80 000\nБюджет: 80 000')
        self.assertEqual(identical['budget']['value'], '80 000')
        self.assertEqual(identical['budget']['state'], 'ocr_candidate')
        self.assertEqual(identical['budget']['currency'], 'RUB')
        self.assertEqual(len(identical_meta['field_observations']['budget']), 2)
        self.assertIn('одинаковых строк', identical['budget']['source']['description'])

        different, different_meta = parse_card_text('Бюджет: 80 000\nБюджет: 90 000')
        self.assertEqual(different['budget']['state'], 'conflict')
        self.assertIn('80 000', different['budget']['value'])
        self.assertIn('90 000', different['budget']['value'])
        self.assertEqual(len(different_meta['field_observations']['budget']), 2)
        self.assertIn('сверить по фото', different_meta['clarifications'][0])

    def test_empty_repeated_value_does_not_erase_first_observation(self):
        fields, metadata = parse_card_text('Бюджет: 80 000\nБюджет:')
        self.assertEqual(fields['budget']['state'], 'conflict')
        self.assertEqual(fields['budget']['value'], '80 000')
        self.assertEqual(len(metadata['field_observations']['budget']), 2)
        self.assertIn('пустое значение', fields['budget']['source']['description'])

    def test_budget_defaults_to_rubles_and_never_converts_foreign_currency(self):
        default, default_meta = parse_card_text('Бюджет: 80 000')
        self.assertEqual(default['budget']['currency'], 'RUB')
        self.assertEqual(default['budget']['state'], 'ocr_candidate')
        self.assertEqual(default_meta['clarifications'], [])

        missing, missing_meta = parse_card_text('Состав: двое взрослых')
        self.assertEqual(missing['budget']['currency'], 'RUB')
        self.assertIn('рублях', missing_meta['clarifications'][0])
        unreadable, unreadable_meta = parse_card_text('Бюджет: запись неразборчива')
        self.assertEqual(unreadable['budget']['state'], 'unreadable')
        self.assertIn('рублях', unreadable_meta['clarifications'][0])

        foreign, foreign_meta = parse_card_text('Бюджет: 1 000 USD')
        self.assertEqual(foreign['budget']['value'], '1 000')
        self.assertEqual(foreign['budget']['currency'], 'USD')
        self.assertEqual(foreign['budget']['state'], 'conflict')
        self.assertIn('не конвертировать', foreign_meta['clarifications'][0])
        baht, _ = parse_card_text('Бюджет: 100000 бат')
        self.assertEqual((baht['budget']['value'], baht['budget']['currency'], baht['budget']['state']),
                         ('100000 бат', 'UNK', 'conflict'))

        for token, expected in [('CNY', 'CNY'), ('GBP', 'GBP'), ('AED', 'AED'), ('ZZZ', 'ZZZ')]:
            with self.subTest(currency=token):
                result, result_meta = parse_card_text(f'Бюджет: 100000 {token}')
                self.assertEqual(result['budget']['state'], 'conflict')
                self.assertEqual(result['budget']['currency'], expected)
                self.assertNotEqual(result['budget']['currency'], 'RUB')
                self.assertIn('не конвертировать', result_meta['clarifications'][0])
        ambiguous_symbol, _ = parse_card_text('Бюджет: 100000 ¥')
        self.assertEqual(ambiguous_symbol['budget']['currency'], 'UNK')
        self.assertEqual(ambiguous_symbol['budget']['state'], 'conflict')

    def test_dates_without_year_are_not_completed_or_asked(self):
        fields, metadata = parse_card_text('Даты: 12–19 июня')
        self.assertEqual(fields['dates']['value'], '12–19 июня')
        self.assertEqual(fields['dates']['state'], 'ocr_candidate')
        self.assertNotIn('год', fields['dates']['source']['description'].lower())
        self.assertFalse(any('год' in item.lower() for item in metadata['clarifications']))

    def test_whole_card_trip_classification_handles_excursion_mixed_and_conflicting_cues(self):
        excursion, _ = parse_card_text('Пожелания: экскурсионный тур без пляжа')
        self.assertEqual(excursion['trip_type']['value'], 'Экскурсионный')
        self.assertEqual(excursion['trip_type']['state'], 'ocr_candidate')
        self.assertEqual(excursion['beach']['state'], 'not_applicable')

        mixed, _ = parse_card_text('Пожелания: экскурсии и пляжный отдых')
        self.assertIn('Смешанный', mixed['trip_type']['value'])
        self.assertEqual(mixed['trip_type']['state'], 'ocr_candidate')
        self.assertEqual(mixed['beach']['state'], 'unknown')

        contradictory, _ = parse_card_text(
            'Тип поездки: пляжный отдых\nПожелания: экскурсионный тур без пляжа')
        self.assertEqual(contradictory['trip_type']['state'], 'conflict')
        self.assertEqual(contradictory['beach']['state'], 'unknown')
        for unknown_beach in ('не указано', 'неразборчиво'):
            with self.subTest(unknown_beach=unknown_beach):
                fields, _ = parse_card_text(f'Тип поездки: пляжный\nПляж: {unknown_beach}')
                self.assertEqual(fields['trip_type']['state'], 'ocr_candidate')
                self.assertEqual(fields['beach']['state'], 'unknown' if unknown_beach == 'не указано' else 'unreadable')

        empty_beach, _ = parse_card_text('Тип поездки: экскурсионный\nПляж:')
        self.assertEqual(empty_beach['trip_type']['state'], 'ocr_candidate')
        self.assertEqual(empty_beach['beach']['state'], 'not_applicable')

        negated_excursion, _ = parse_card_text('Пожелания: экскурсии не нужны, только пляж')
        self.assertEqual(negated_excursion['trip_type']['value'], 'Пляжный отдых')
        self.assertEqual(negated_excursion['trip_type']['state'], 'ocr_candidate')
        self.assertNotIn('Смешанный', negated_excursion['trip_type']['value'])

        negated_type, _ = parse_card_text('Тип поездки: не экскурсионный, пляжный отдых')
        self.assertEqual(negated_type['trip_type']['value'], 'Пляжный отдых')
        self.assertEqual(negated_type['trip_type']['state'], 'ocr_candidate')

        ambiguous, _ = parse_card_text('Тип поездки: отдых')
        self.assertEqual(ambiguous['trip_type']['value'], 'отдых')
        self.assertEqual(ambiguous['trip_type']['state'], 'conflict')

    def test_unknown_beach_is_neither_positive_nor_negative_type_evidence(self):
        for beach_state, marker in [('unknown', 'не указано'), ('unreadable', 'неразборчиво')]:
            with self.subTest(beach_state=beach_state):
                cases = [
                    ('', 'unknown'),
                    ('Тип поездки: экскурсионный\n', 'Экскурсионный'),
                    ('Тип поездки: пляжный\n', 'Пляжный отдых'),
                ]
                for prefix, expected in cases:
                    fields, _ = parse_card_text(f'{prefix}Пляж: {marker}')
                    self.assertEqual(fields['trip_type']['state'], 'unknown' if expected == 'unknown' else 'ocr_candidate')
                    if expected != 'unknown':
                        self.assertEqual(fields['trip_type']['value'], expected)
                    self.assertEqual(fields['beach']['state'], beach_state)

    def test_mixed_unknown_or_unreadable_wishes_keep_the_useful_text(self):
        cases = (
            'не указана детская анимация, нужен подогреваемый бассейн',
            'питание неразборчиво, нужен подогреваемый бассейн',
            'нужен подогреваемый бассейн, детская анимация не указана',
            'нужен подогреваемый бассейн, питание неразборчиво',
        )
        for candidate in cases:
            with self.subTest(candidate=candidate):
                fields, _ = parse_card_text(f'Пожелания: {candidate}')
                self.assertEqual(fields['wishes']['state'], 'ocr_candidate')
                self.assertIn('подогреваемый бассейн', fields['wishes']['value'])
                self.assertIn(candidate, fields['wishes']['value'])

    def test_children_absent_is_unknown_but_explicit_zero_is_review_candidate(self):
        absent, _ = parse_card_text('Состав: двое взрослых')
        self.assertEqual(absent['children_count']['value'], '')
        self.assertEqual(absent['children_count']['state'], 'unknown')
        explicit, _ = parse_card_text('Дети: 0')
        self.assertEqual(explicit['children_count']['value'], '0')
        self.assertEqual(explicit['children_count']['state'], 'ocr_candidate')

    def test_unknown_children_and_excursion_beach_status_reach_existing_presentation(self):
        revision = current_revision(self.card)
        fields = self.fields()
        fields['party']['state'] = 'known'
        fields['children_count'] = {'value': '', 'state': 'unknown',
                                    'source': {'kind': 'manual_review', 'page': 1,
                                               'description': 'Синтетическая карта не указывает детей.'}}
        fields['trip_type'] = {'value': 'Экскурсионный', 'state': 'known',
                               'source': {'kind': 'manual_review', 'page': 1,
                                          'description': 'Подтверждён синтетический запрос экскурсий.'}}
        fields['beach'] = {'value': '', 'state': 'not_applicable',
                           'source': {'kind': 'manual_review', 'page': 1,
                                      'description': 'Для экскурсионной поездки пляж не обязателен.'}}
        saved = save_fields(self.card, self.owner, revision.number, revision.content_hash, fields)
        confirmed = confirm_card(self.card, self.owner, saved.number, saved.content_hash)
        request = prepare_request(self.card, self.owner, confirmed.number,
                                  confirmed.content_hash, self.hotels)
        self.assertIn('дети: Не указано', request.traveller_summary)
        self.assertIn('Тип поездки: Экскурсионный', request.requirements)
        self.assertIn('Пляж: Неприменимо', request.requirements)

    def test_old_schema_card_can_be_opened_manually_edited_and_confirmed(self):
        current = current_revision(self.card)
        old_fields = {key: copy.deepcopy(current.fields[key]) for key in ('party', 'dates', 'budget', 'wishes')}
        old_fields['budget'].update(value='100000 CNY', state='known')
        old_fields['budget'].pop('currency', None)
        old_hash = digest(current.source, old_fields)
        legacy = CardRevision.objects.create(card=self.card, number=2, source=current.source,
                                             fields=old_fields, content_hash=old_hash)
        self.card.current_revision = legacy.number
        self.card.save(update_fields=['current_revision'])
        legacy_history_snapshot = copy.deepcopy(legacy.fields)

        detail = self.client.get(reverse('travelcards:detail', args=[self.card.pk]))
        self.assertEqual(detail.status_code, 200)
        form = CardFieldsForm(revision=legacy)
        self.assertEqual(form.initial['budget_state'], 'conflict')
        self.assertEqual(form.initial['budget_currency'], 'CNY')
        post = {'expected_revision': legacy.number, 'expected_hash': legacy.content_hash,
                'budget_currency': form.initial['budget_currency']}
        for key in FIELD_LABELS:
            post[key + '_value'] = form.initial[key + '_value']
            post[key + '_state'] = form.initial[key + '_state']
            post[key + '_source'] = form.initial[key + '_source']
        post.update({'party_value': 'Двое взрослых — синтетика', 'party_state': 'known',
                     'party_source': 'Сверено с вымышленной старой редакцией.'})

        saved = self.client.post(reverse('travelcards:detail', args=[self.card.pk]), post)
        self.assertEqual(saved.status_code, 302)
        legacy.refresh_from_db()
        self.assertEqual(legacy.fields, legacy_history_snapshot)
        self.assertEqual(legacy.content_hash, old_hash)
        self.card.refresh_from_db()
        edited = current_revision(self.card)
        self.assertEqual(edited.number, 3)
        self.assertEqual(edited.fields['children_count']['state'], 'unknown')
        confirmed = confirm_card(self.card, self.owner, edited.number, edited.content_hash)
        request = prepare_request(self.card, self.owner, confirmed.number,
                                  confirmed.content_hash, self.hotels)
        self.assertIn('дети: Не указано', request.traveller_summary)
        self.assertIn('Бюджет в рублях: Конфликт', request.requirements)
        self.assertNotIn('100000 CNY', request.requirements)

    def test_replacing_photo_on_old_schema_card_adds_safe_unknown_fields(self):
        current = current_revision(self.card)
        old_fields = {key: copy.deepcopy(current.fields[key]) for key in ('party', 'dates', 'budget', 'wishes')}
        old_fields['budget'].pop('currency', None)
        legacy = CardRevision.objects.create(card=self.card, number=2, source=current.source, fields=old_fields,
                                             content_hash=digest(current.source, old_fields))
        self.card.current_revision = legacy.number
        self.card.save(update_fields=['current_revision'])

        replaced = replace_photo(self.card, self.owner, legacy.number, legacy.content_hash, photo('yellow'))

        self.assertEqual(replaced.number, 3)
        self.assertEqual(replaced.fields['children_count']['state'], 'unknown')
        self.assertEqual(replaced.fields['trip_type']['state'], 'unknown')
        legacy.refresh_from_db()
        self.assertEqual(set(legacy.fields), {'party', 'dates', 'budget', 'wishes'})

    def test_owner_deactivated_during_ocr_cannot_create_card_source_or_revision(self):
        existing_counts = (PhotoCard.objects.count(), CardSource.objects.count(), CardRevision.objects.count())

        def deactivate_during_ocr(_image_bytes):
            get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
            return current_revision(self.card).fields, {'status': 'synthetic'}

        with patch('travelcards.services.recognize_card_photo', side_effect=deactivate_during_ocr):
            with self.assertRaises(PermissionDenied):
                create_card(self.owner, 'Отключённый владелец', uuid.uuid4(), photo())

        current_counts = (PhotoCard.objects.count(), CardSource.objects.count(), CardRevision.objects.count())
        self.assertEqual(current_counts, existing_counts)

    def test_missing_local_russian_ocr_keeps_manual_fallback(self):
        with patch('travelcards.ocr.shutil.which', return_value=None):
            fields, metadata = recognize_card_photo(b'synthetic bytes are not processed without the engine')
        self.assertEqual(metadata['status'], 'unavailable')
        self.assertEqual(fields['party']['state'], 'unknown')
        self.assertEqual(fields['party']['value'], '')
        self.assertEqual(fields['party']['source']['kind'], 'manual_fallback')

    def test_local_ocr_timeout_keeps_manual_fallback(self):
        with patch('travelcards.ocr.shutil.which', return_value='/usr/bin/tesseract'), \
             patch('travelcards.ocr._probe_engine', return_value=('/usr/bin/tesseract', 'Tesseract synthetic')), \
             patch('travelcards.ocr.subprocess.run', side_effect=subprocess.TimeoutExpired('tesseract', 15)):
            fields, metadata = recognize_card_photo(photo().read())
        self.assertEqual(metadata['status'], 'timeout')
        self.assertEqual(fields['party']['state'], 'unknown')
        self.assertEqual(fields['party']['source']['kind'], 'manual_fallback')

    def test_vision_ocr_is_separate_disabled_candidate_and_mock_cannot_fill_photo_card(self):
        result = OCRResult(text='Вымышленный текст с фото',
                           blocks=(OCRBlock(text='сумма: 100 000', page=1, bounds=((0, 0), (1, 1))),),
                           uncertainties=('цифры нужно перепроверить',))
        image = photo()
        raw = image.read()
        mocked = MockVisionOCRAdapter(result).recognize(raw, image.content_type)
        self.assertTrue(mocked.is_mock)
        self.assertEqual(mocked.adapter, 'mock-vision-ocr')
        self.assertEqual(mocked.uncertainties, ('цифры нужно перепроверить',))
        with self.assertRaises(OCRAdapterDisabled):
            photo_ocr_adapter().recognize(raw, image.content_type)
        # The unrelated disabled Vision mock cannot populate the local Tesseract path.
        card_fields = current_revision(self.card).fields
        self.assertEqual(card_fields['budget']['value'], '')
        self.assertNotEqual(card_fields['budget']['source']['kind'], 'mock-vision-ocr')

    @skipUnless(russian_tesseract_available(), 'Local Tesseract rus traineddata is not installed.')
    def test_russian_synthetic_photo_ocr_manager_review_and_existing_presentation(self):
        fixture = Path(__file__).resolve().parents[1] / 'docs' / 'fixtures' / 'synthetic-travel-card.png'
        upload_page = self.client.get(reverse('travelcards:upload'))
        upload_key = upload_page.context['form']['upload_key'].value()
        upload = SimpleUploadedFile('synthetic-card.png', fixture.read_bytes(), content_type='image/png')
        response = self.client.post(reverse('travelcards:upload'), {
            'title': 'Синтетическая OCR-карта', 'upload_key': upload_key, 'photo': upload,
        })
        self.assertEqual(response.status_code, 302)
        card = PhotoCard.objects.get(title='Синтетическая OCR-карта')
        revision = current_revision(card)
        recognition = revision.fields[RECOGNITION_KEY]
        self.assertEqual(recognition['status'], 'recognized')
        self.assertEqual(recognition['language'], 'rus')
        self.assertEqual(revision.fields['party']['value'], 'двое взрослых.')
        self.assertEqual(revision.fields['party']['state'], 'ocr_candidate')
        self.assertEqual(revision.fields['dates']['state'], 'unknown')
        self.assertEqual(revision.fields['budget']['state'], 'unreadable')
        self.assertEqual(revision.fields['wishes']['value'], 'спокойный отдых.')
        self.assertEqual(revision.fields['wishes']['state'], 'ocr_candidate')

        with self.assertRaisesRegex(ValidationError, 'кандидаты OCR'):
            confirm_card(card, self.owner, revision.number, revision.content_hash)

        form_data = {'expected_revision': revision.number, 'expected_hash': revision.content_hash}
        for key in FIELD_LABELS:
            value = revision.fields[key]
            form_data.update({key + '_value': value['value'], key + '_state': value['state'],
                              key + '_source': value['source']['description']})
        form_data['budget_currency'] = revision.fields['budget'].get('currency', 'RUB')
        form_data.update({
            'party_value': 'Двое взрослых — вымышленные данные', 'party_state': 'known',
            'party_source': 'Менеджер сверил строку «Состав» с синтетическим фото.',
            'wishes_value': 'Спокойный отдых', 'wishes_state': 'known',
            'wishes_source': 'Менеджер сверил строку «Пожелания» с синтетическим фото.',
        })
        saved = self.client.post(reverse('travelcards:detail', args=[card.pk]), form_data)
        self.assertEqual(saved.status_code, 302)
        card.refresh_from_db()
        revision = current_revision(card)
        self.assertEqual(revision.fields[RECOGNITION_KEY]['engine'], 'Tesseract')
        self.assertEqual(revision.fields['party']['state'], 'known')
        self.assertEqual(revision.fields['dates']['state'], 'unknown')
        self.assertEqual(revision.fields['budget']['state'], 'unreadable')

        confirmed = self.client.post(reverse('travelcards:confirm', args=[card.pk]), {
            'expected_revision': revision.number, 'expected_hash': revision.content_hash,
        })
        self.assertEqual(confirmed.status_code, 302)
        ready = self.client.post(reverse('travelcards:prepare', args=[card.pk]), {
            'expected_revision': revision.number, 'expected_hash': revision.content_hash,
            'hotels': [str(hotel.pk) for hotel in self.hotels],
        })
        self.assertEqual(ready.status_code, 302)
        travel_request = PhotoCard.objects.get(pk=card.pk).travel_request
        self.assertEqual(travel_request.selected_hotels.count(), 3)
        self.assertIn('Двое взрослых', travel_request.traveller_summary)
        self.assertIn('Неразборчиво', travel_request.requirements)
        self.assertIn('Не указано', travel_request.requirements)
        preview = self.client.get(reverse('presentations:preview', args=[travel_request.pk]))
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, 'Спокойный отдых')

    def test_new_upload_replay_is_idempotent_and_other_card_not_merged(self):
        repeat = create_card(self.owner, 'повтор', self.card.upload_key, photo())
        self.assertEqual(repeat.pk, self.card.pk)
        self.assertEqual(PhotoCard.objects.count(), 1)
        separate = create_card(self.owner, 'другой сценарий', uuid.uuid4(), photo())
        self.assertNotEqual(separate.pk, self.card.pk)
        self.assertEqual(PhotoCard.objects.count(), 2)

    def test_duplicate_photo_in_same_card_preserves_revision_and_confirmation(self):
        rev = self.confirm()
        repeated = self.replace(photo())
        self.assertEqual(repeated.pk, rev.pk)
        self.assertIsNotNone(repeated.confirmed_at)
        self.assertEqual(CardSource.objects.filter(card=self.card).count(), 1)

    def test_owner_acl_covers_source_draft_edit_confirm_and_prepare(self):
        revision = current_revision(self.card)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('travelcards:detail', args=[self.card.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('travelcards:source', args=[self.card.pk, revision.source_id])).status_code, 404)
        for route in ('detail', 'replace_photo', 'confirm', 'prepare'):
            self.assertEqual(self.client.post(reverse('travelcards:' + route, args=[self.card.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse('travelcards:list')), self.card.title)

    def test_source_not_public_and_private_headers(self):
        revision = current_revision(self.card)
        url = reverse('travelcards:source', args=[self.card.pk, revision.source_id])
        self.assertEqual(Client().get(url).status_code, 302)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['Content-Type'], 'image/png')

    def test_invalid_replacement_preserves_draft_history_and_confirmation(self):
        rev = current_revision(self.card)
        rev = save_fields(self.card, self.owner, rev.number, rev.content_hash, self.fields())
        self.confirm()
        bad = SimpleUploadedFile('bad.png', b'broken', content_type='image/png')
        response = self.client.post(reverse('travelcards:replace_photo', args=[self.card.pk]), {
            'expected_revision': current_revision(self.card).number,
            'expected_hash': current_revision(self.card).content_hash,
            'photo': bad,
        })
        self.assertEqual(response.status_code, 422)
        self.card.refresh_from_db()
        current = current_revision(self.card)
        self.assertEqual(current.pk, rev.pk)
        self.assertEqual(current.fields, rev.fields)
        self.assertIsNotNone(current.confirmed_at)

    def test_confirmation_preserves_unknown_unreadable_and_conflict_states(self):
        rev = current_revision(self.card)
        fields = self.fields()
        fields['dates']['state'] = 'unreadable'
        fields['budget']['state'] = 'conflict'
        fields['budget']['value'] = 'Неуверенное значение'
        save_fields(self.card, self.owner, rev.number, rev.content_hash, fields)
        confirmed = self.confirm()
        self.assertEqual(confirmed.fields, fields)
        request = self.prepare()
        self.assertIn('Неразборчиво', request.requirements)
        self.assertIn('Конфликт', request.requirements)
        self.assertNotIn('Неуверенное значение', request.requirements)
        self.assertIn('Не указано', request.requirements)

    def test_edit_preserves_old_confirmed_revision_and_rejects_stale_confirmation(self):
        previous = self.confirm()
        updated = save_fields(self.card, self.owner, previous.number, previous.content_hash, self.fields())
        self.assertIsNone(updated.confirmed_at)
        previous.refresh_from_db()
        self.assertIsNotNone(previous.confirmed_at)
        with self.assertRaises(ValidationError):
            confirm_card(self.card, self.owner, previous.number, previous.content_hash)

    def test_unconfirmed_card_cannot_prepare_presentation(self):
        rev = current_revision(self.card)
        with self.assertRaises(ValidationError):
            prepare_request(self.card, self.owner, rev.number, rev.content_hash, self.hotels)
        self.assertEqual(TravelRequest.objects.count(), 0)

    def test_prepare_replay_updates_same_request_without_duplicate(self):
        request = self.prepare()
        repeated = self.prepare()
        self.assertEqual(request.pk, repeated.pk)
        self.assertEqual(request.revision, repeated.revision)
        self.assertEqual(TravelRequest.objects.count(), 1)

    def test_linked_card_fields_are_disabled_in_presentation_edit_form(self):
        request = self.prepare()
        original = (request.traveller_summary, request.requirements)
        post = {
            'title': request.title,
            'traveller_summary': 'Пять взрослых — подмена из старой формы',
            'requirements': 'Подменённые требования',
            'selected_hotels': [str(hotel.pk) for hotel in self.hotels],
            'internal_notes': 'Только синтетика',
        }
        form = TravelRequestForm(post, instance=request)
        self.assertTrue(form.fields['traveller_summary'].disabled)
        self.assertTrue(form.fields['requirements'].disabled)
        self.assertTrue(form.is_valid(), form.errors)
        changed = form.save(commit=False)
        changed.save()
        form.save_m2m()
        request.refresh_from_db()
        self.assertEqual((request.traveller_summary, request.requirements), original)

    def test_card_request_drift_blocks_replay_and_new_approval_without_mutation(self):
        request = self.prepare()
        approval = approve_request(request, self.owner, snapshot_hash(build_snapshot(request)))
        release, _token = publish_request(request, self.owner, approval.pk)
        original_release = (copy.deepcopy(release.snapshot), release.html_content,
                            bytes(release.pdf_bytes), release.content_hash)
        original_requirements = request.requirements

        # Reproduce an older/manual edit path which predates the read-only fields.
        TravelRequest.objects.filter(pk=request.pk).update(
            traveller_summary='Пять взрослых — ошибочно расхождённая редакция')
        request.refresh_from_db()
        revision = current_revision(self.card)
        with self.assertRaisesRegex(ValidationError, 'отличаются от карты'):
            prepare_request(self.card, self.owner, revision.number,
                            revision.content_hash, self.hotels)
        with self.assertRaisesRegex(ValidationError, 'отличаются от карты'):
            approve_request(request, self.owner, snapshot_hash(build_snapshot(request)))

        request.refresh_from_db()
        release.refresh_from_db()
        self.assertIn('Пять взрослых', request.traveller_summary)
        self.assertEqual(request.requirements, original_requirements)
        self.assertEqual((release.snapshot, release.html_content, bytes(release.pdf_bytes),
                          release.content_hash), original_release)
        self.assertEqual(Release.objects.count(), 1)

    def test_photo_replace_stale_retry_does_not_roll_back_newer_photo(self):
        old_version = current_revision(self.card)
        first = self.post_photo_replacement(photo('yellow'), old_version)
        self.assertEqual(first.status_code, 302)
        version_b = current_revision(self.card)
        self.assertEqual(version_b.number, 2)

        second = self.post_photo_replacement(photo('green'), version_b)
        self.assertEqual(second.status_code, 302)
        version_c = current_revision(self.card)
        self.assertEqual(version_c.number, 3)
        newest_hash = version_c.source.sha256
        newest_fields = copy.deepcopy(version_c.fields)

        replay_b = self.post_photo_replacement(photo('yellow'), old_version)
        self.assertEqual(replay_b.status_code, 409)
        self.assertContains(replay_b, 'устаревшей вкладки', status_code=409)
        self.card.refresh_from_db()
        latest = current_revision(self.card)
        self.assertEqual(latest.number, 3)
        self.assertEqual(latest.source.sha256, newest_hash)
        self.assertEqual(latest.fields, newest_fields)
        self.assertEqual(CardSource.objects.filter(card=self.card).count(), 3)

    def test_identical_successful_photo_replacement_retry_is_idempotent(self):
        old_version = current_revision(self.card)
        first = self.post_photo_replacement(photo('yellow'), old_version)
        self.assertEqual(first.status_code, 302)
        current = current_revision(self.card)
        confirmed_at = current.confirmed_at

        replay = self.post_photo_replacement(photo('yellow'), old_version)

        self.assertEqual(replay.status_code, 302)
        self.card.refresh_from_db()
        latest = current_revision(self.card)
        self.assertEqual(latest.number, 2)
        self.assertEqual(latest.source.sha256, current.source.sha256)
        self.assertEqual(latest.confirmed_at, confirmed_at)
        self.assertEqual(CardRevision.objects.filter(card=self.card).count(), 2)

    def test_two_tabs_replacing_different_photos_from_same_revision_conflict(self):
        tab_a = current_revision(self.card)
        tab_b = current_revision(self.card)
        accepted = self.post_photo_replacement(photo('yellow'), tab_a)
        self.assertEqual(accepted.status_code, 302)
        current = current_revision(self.card)

        stale = self.post_photo_replacement(photo('green'), tab_b)

        self.assertEqual(stale.status_code, 409)
        self.assertContains(stale, 'устаревшей вкладки', status_code=409)
        self.card.refresh_from_db()
        latest = current_revision(self.card)
        self.assertEqual(latest.number, current.number)
        self.assertEqual(latest.source.sha256, current.source.sha256)
        self.assertEqual(CardSource.objects.filter(card=self.card).count(), 2)

    def test_changed_card_blocks_existing_presentation_approval_and_publish(self):
        request = self.prepare()
        approval = approve_request(request, self.owner, snapshot_hash(build_snapshot(request)))
        rev = current_revision(self.card)
        save_fields(self.card, self.owner, rev.number, rev.content_hash, self.fields())
        with self.assertRaises(ValidationError):
            publish_request(request, self.owner, approval.pk)
        request.refresh_from_db()
        with self.assertRaises(ValidationError):
            approve_request(request, self.owner, snapshot_hash(build_snapshot(request)))
        self.assertEqual(Release.objects.count(), 0)
        # Confirming alone is insufficient until the new card revision is transferred.
        self.confirm()
        with self.assertRaises(ValidationError):
            approve_request(request, self.owner, snapshot_hash(build_snapshot(request)))
        refreshed = self.prepare()
        new_approval = approve_request(refreshed, self.owner, snapshot_hash(build_snapshot(refreshed)))
        self.assertIsNotNone(new_approval.pk)

    def test_photo_replacement_keeps_values_but_marks_known_as_conflict(self):
        revision = current_revision(self.card)
        save_fields(self.card, self.owner, revision.number, revision.content_hash, self.fields())
        self.confirm()
        revision = self.replace(photo('yellow'))
        self.assertEqual(revision.fields['party']['value'], self.fields()['party']['value'])
        self.assertEqual(revision.fields['party']['state'], 'conflict')
        self.assertIsNone(revision.confirmed_at)

    def test_known_empty_or_unknown_with_value_rejected_by_form(self):
        rev = current_revision(self.card)
        data = {'expected_revision': rev.number, 'expected_hash': rev.content_hash}
        for key in FIELD_LABELS:
            field = rev.fields[key]
            data.update({key + '_value': field['value'], key + '_state': field['state'],
                         key + '_source': field['source']['description']})
        data['budget_currency'] = 'RUB'
        data['party_state'] = 'known'
        self.assertFalse(CardFieldsForm(data, revision=rev).is_valid())
        data['party_state'] = 'unknown'
        data['party_value'] = 'Нельзя выдавать за известное'
        self.assertFalse(CardFieldsForm(data, revision=rev).is_valid())

    def test_manual_foreign_currency_must_remain_conflict_without_conversion(self):
        rev = current_revision(self.card)
        data = {'expected_revision': rev.number, 'expected_hash': rev.content_hash}
        for key in FIELD_LABELS:
            field = rev.fields[key]
            data.update({key + '_value': field['value'], key + '_state': field['state'],
                         key + '_source': field['source']['description']})
        data.update({'budget_value': '1 000', 'budget_state': 'ocr_candidate', 'budget_currency': 'USD'})
        form = CardFieldsForm(data, revision=rev)
        self.assertFalse(form.is_valid())
        data['budget_state'] = 'conflict'
        form = CardFieldsForm(data, revision=rev)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.field_snapshot()['budget'], {
            'value': '1 000', 'state': 'conflict', 'currency': 'USD',
            'source': {'kind': 'manual_review', 'page': 1,
                       'description': rev.fields['budget']['source']['description']},
        })

        data.update({'budget_value': '100000 CNY', 'budget_state': 'known', 'budget_currency': 'RUB'})
        self.assertFalse(CardFieldsForm(data, revision=rev).is_valid())
        data.update({'budget_state': 'conflict', 'budget_currency': 'CNY'})
        form = CardFieldsForm(data, revision=rev)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.field_snapshot()['budget']['currency'], 'CNY')

        data.update({'budget_value': '100000 бат', 'budget_state': 'known', 'budget_currency': 'RUB'})
        self.assertFalse(CardFieldsForm(data, revision=rev).is_valid())

    def test_partial_unknown_marker_does_not_erase_other_wishes(self):
        fields, _ = parse_card_text('Пожелания: детская анимация не указана, нужен подогреваемый бассейн')
        self.assertEqual(fields['wishes']['state'], 'ocr_candidate')
        self.assertIn('детская анимация не указана', fields['wishes']['value'])
        self.assertIn('подогреваемый бассейн', fields['wishes']['value'])

    def test_mutating_routes_require_csrf_and_post(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        for name in ('replace_photo', 'confirm', 'prepare'):
            url = reverse('travelcards:' + name, args=[self.card.pk])
            self.assertEqual(client.get(url).status_code, 405)
            self.assertEqual(client.post(url).status_code, 403)

    def test_published_release_stays_frozen_after_photo_card_changes(self):
        request = self.prepare()
        approval = approve_request(request, self.owner, snapshot_hash(build_snapshot(request)))
        release, _ = publish_request(request, self.owner, approval.pk)
        before = (copy.deepcopy(release.snapshot), release.html_content, bytes(release.pdf_bytes))
        revision = current_revision(self.card)
        save_fields(self.card, self.owner, revision.number, revision.content_hash, self.fields())
        release.refresh_from_db()
        self.assertEqual((release.snapshot, release.html_content, bytes(release.pdf_bytes)), before)

    def test_stale_edit_is_rejected_without_overwriting_newer_fields(self):
        previous = current_revision(self.card)
        saved = save_fields(self.card, self.owner, previous.number, previous.content_hash, self.fields())
        with self.assertRaises(ValidationError):
            save_fields(self.card, self.owner, previous.number, previous.content_hash, previous.fields)
        self.card.refresh_from_db()
        self.assertEqual(current_revision(self.card).pk, saved.pk)
        self.assertEqual(current_revision(self.card).fields, self.fields())
