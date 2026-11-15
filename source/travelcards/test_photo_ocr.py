"""Real printed OCR separately from unit checks of preparation and review evidence."""
import hashlib
import io
import json
import subprocess
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from PIL import Image

from .ocr import (_prepare_ocr_image, MAX_IMAGE_BYTES, parse_card_text,
                  recognize_card_photo, russian_tesseract_available)

FIXTURES = Path(__file__).resolve().parents[1] / 'docs/fixtures/photo-ocr'


def image_bytes(image=None, **save_args):
    buffer = io.BytesIO()
    (image or Image.new('RGB', (15, 12), 'white')).save(buffer, format='PNG', **save_args)
    return buffer.getvalue()


class OCRPreparationTests(SimpleTestCase):
    def test_all_eight_orientations_and_original_identity(self):
        upright = Image.new('RGB', (6, 4), 'white')
        upright.putpixel((0, 0), (255, 0, 0))
        upright.putpixel((5, 3), (0, 0, 255))
        inverses = [None, Image.Transpose.FLIP_LEFT_RIGHT, Image.Transpose.ROTATE_180,
                    Image.Transpose.FLIP_TOP_BOTTOM, Image.Transpose.TRANSPOSE,
                    Image.Transpose.ROTATE_90, Image.Transpose.TRANSVERSE, Image.Transpose.ROTATE_270]
        for orientation, inverse in enumerate(inverses, 1):
            with self.subTest(orientation=orientation):
                exif = Image.Exif(); exif[274] = orientation
                raw = image_bytes(upright if inverse is None else upright.transpose(inverse), exif=exif)
                before = bytes(raw)
                prepared, meta = _prepare_ocr_image(raw)
                with Image.open(io.BytesIO(prepared)) as result:
                    self.assertEqual(result.size, upright.size)
                    self.assertEqual(result.tobytes(), upright.tobytes())
                    self.assertEqual(dict(result.getexif()), {})
                self.assertEqual(raw, before)
                self.assertEqual(meta['source_sha256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual(meta['ocr_input_sha256'], hashlib.sha256(prepared).hexdigest())
                self.assertEqual(meta['applied'], orientation != 1)

    def test_metadata_not_passed_to_engine(self):
        exif = Image.Exif(); exif[270] = 'SYNTHETIC-INTERNAL-MARKER'; exif[274] = 1
        prepared, meta = _prepare_ocr_image(image_bytes(exif=exif))
        self.assertNotIn(b'SYNTHETIC-INTERNAL-MARKER', prepared)
        self.assertNotIn('SYNTHETIC-INTERNAL-MARKER', json.dumps(meta))

    def test_transparent_pixels_composited_on_white(self):
        prepared, _ = _prepare_ocr_image(image_bytes(Image.new('RGBA', (3, 3), (0, 0, 0, 0))))
        with Image.open(io.BytesIO(prepared)) as result:
            self.assertEqual(result.getpixel((0, 0)), (255, 255, 255))

    def test_invalid_or_oversized_input_never_reaches_ocr(self):
        exif = Image.Exif(); exif[274] = 9
        for raw in (b'', b'<svg>synthetic</svg>', b'x' * (MAX_IMAGE_BYTES + 1), image_bytes(exif=exif)):
            with self.subTest(length=len(raw)), \
                 patch('travelcards.ocr.shutil.which', return_value='tesseract'), \
                 patch('travelcards.ocr._probe_engine', return_value=('tesseract', 'synthetic')), \
                 patch('travelcards.ocr.subprocess.run') as run:
                fields, metadata = recognize_card_photo(raw)
                self.assertEqual(metadata['status'], 'error')
                self.assertTrue(all(f['state'] == 'unknown' and f['value'] == '' for f in fields.values()))
                run.assert_not_called()

    def test_excessive_pixels_rejected_before_loading(self):
        with patch('travelcards.ocr.MAX_IMAGE_PIXELS', 20):
            with self.assertRaises(ValueError):
                _prepare_ocr_image(image_bytes())

    def test_animated_png_rejected(self):
        first = Image.new('RGB', (8, 8), 'white')
        raw = image_bytes(first, save_all=True, append_images=[Image.new('RGB', (8, 8), 'black')])
        with self.assertRaises(ValueError):
            _prepare_ocr_image(raw)

    def test_truncated_image_rejected(self):
        with self.assertRaises((OSError, ValueError, SyntaxError)):
            _prepare_ocr_image(image_bytes()[:30])

    def test_safe_timeout_and_process_error(self):
        for effect in (subprocess.TimeoutExpired('SYNTHETIC-PRIVATE', 15), OSError('SYNTHETIC-PRIVATE')):
            with self.subTest(error=type(effect).__name__), \
                 patch('travelcards.ocr.shutil.which', return_value='tesseract'), \
                 patch('travelcards.ocr._probe_engine', return_value=('tesseract', 'synthetic')), \
                 patch('travelcards.ocr.subprocess.run', side_effect=effect):
                fields, meta = recognize_card_photo(image_bytes())
                self.assertIn(meta['status'], ('timeout', 'error'))
                self.assertNotIn('SYNTHETIC-PRIVATE', json.dumps(meta))
                self.assertTrue(all(f['value'] == '' for f in fields.values()))

    def test_nonzero_exit_does_not_reflect_stderr(self):
        with patch('travelcards.ocr.shutil.which', return_value='tesseract'), \
             patch('travelcards.ocr._probe_engine', return_value=('tesseract', 'synthetic')), \
             patch('travelcards.ocr.subprocess.run', return_value=subprocess.CompletedProcess([], 1, b'', b'SYNTHETIC-PRIVATE')):
            _, meta = recognize_card_photo(image_bytes())
            self.assertEqual(meta['status'], 'error')
            self.assertNotIn('SYNTHETIC-PRIVATE', json.dumps(meta))


class OCRUnassignedEvidenceTests(SimpleTestCase):
    def test_continuation_not_guessed_and_unknown_labels_visible(self):
        text = 'Пожелания: тихий номер\nвдали от дороги\nТрансфер: не указан\nДети: не указано'
        fields, meta = parse_card_text(text)
        self.assertEqual(fields['wishes']['value'], 'тихий номер')
        self.assertEqual(fields['wishes']['state'], 'ocr_candidate')
        self.assertEqual(fields['children_count']['value'], '')
        self.assertEqual(fields['children_count']['state'], 'unknown')
        self.assertEqual(meta['unassigned_lines'], [{'line': 2, 'text': 'вдали от дороги'},
                                                   {'line': 3, 'text': 'Трансфер: не указан'}])
        self.assertEqual(meta['raw_text'], text)

    def test_review_list_bounded_without_losing_count_or_available_text(self):
        text = '\n'.join(f'Неразмеченная строка {i}' for i in range(60))
        _, meta = parse_card_text(text)
        self.assertEqual(meta['unassigned_count'], 60)
        self.assertEqual(len(meta['unassigned_lines']), 40)
        self.assertEqual(meta['raw_text'], text)

    def test_absent_year_budget_and_children_not_invented(self):
        fields, meta = parse_card_text('Даты: 10-20 июня\nСостав: двое взрослых')
        self.assertEqual(fields['dates']['value'], '10-20 июня')
        self.assertEqual(fields['budget']['state'], 'unknown')
        self.assertEqual(fields['budget']['currency'], 'RUB')
        self.assertEqual(fields['children_count']['state'], 'unknown')
        self.assertIn('рублях', meta['clarifications'][0])

    def test_manager_notes_escape_image_text_and_keep_old_metadata_compatible(self):
        _, meta = parse_card_text('<script>alert("synthetic")</script>\nБюджет: 500 EUR')
        html = render_to_string('travelcards/ocr_review_notes.html', {'recognition': meta})
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('не конвертировать автоматически', html)
        self.assertIn('до ручных правок', html)
        self.assertEqual(render_to_string('travelcards/ocr_review_notes.html', {'recognition': {}}).strip(), '')

    def test_repeated_conflicts_not_silently_resolved(self):
        fields, meta = parse_card_text('Бюджет: 180000 рублей\nБюджет: 210000 рублей')
        self.assertEqual(fields['budget']['state'], 'conflict')
        self.assertIn('180000', fields['budget']['value'])
        self.assertIn('210000', fields['budget']['value'])
        self.assertEqual(meta['unassigned_count'], 0)


@skipUnless(russian_tesseract_available(), 'Local Tesseract rus required; not a mock substitute')
class RealPrintedPhotoOCRTests(SimpleTestCase):
    def test_all_rotated_jpegs_match_ground_truth_after_real_ocr(self):
        manifest = json.loads((FIXTURES / 'manifest.json').read_text())
        for orientation in (1, 3, 6, 8):
            name = f'printed-exif-{orientation}.jpg'
            with self.subTest(orientation=orientation):
                raw = (FIXTURES / name).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest['files'][name])
                fields, meta = recognize_card_photo(raw)
                self.assertEqual(meta['status'], 'recognized')
                for key in ('party', 'budget', 'dates'):
                    self.assertEqual(fields[key]['value'], manifest['expected'][key])
                    self.assertEqual(fields[key]['state'], 'ocr_candidate')
                self.assertEqual(fields['budget']['currency'], 'RUB')
                self.assertEqual(fields['children_count']['state'], 'unknown')
                self.assertEqual(fields['wishes']['value'], 'тихий номер')
                self.assertIn('вдали от дороги', [r['text'] for r in meta['unassigned_lines']])
                self.assertEqual(meta['preprocessing']['orientation'], orientation)
                self.assertFalse(any(f['state'] == 'known' for f in fields.values()))

    def test_real_blank_never_means_zero_or_known(self):
        fields, meta = recognize_card_photo((FIXTURES / 'blank.png').read_bytes())
        self.assertEqual(meta['status'], 'no_text')
        self.assertTrue(all(f['value'] == '' and f['state'] == 'unknown' for f in fields.values()))

    def test_real_conflicting_budget_and_unreadable_wishes(self):
        fields, _ = recognize_card_photo((FIXTURES / 'conflict.png').read_bytes())
        self.assertEqual(fields['budget']['state'], 'conflict')
        self.assertIn('180000', fields['budget']['value'])
        self.assertIn('210000', fields['budget']['value'])
        self.assertEqual(fields['dates']['state'], 'unknown')
        self.assertEqual(fields['wishes']['state'], 'unreadable')
