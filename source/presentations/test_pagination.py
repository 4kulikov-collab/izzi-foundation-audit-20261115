"""Actual PDF regressions: compact two-hotel release and long final content."""
import io
from django.test import SimpleTestCase
from pypdf import PdfReader
from .synthetic_layout import fixture, compact, expected_values
from .rendering import render_pdf


def two_hotel_snapshot():
    snapshot = fixture(2, False)
    snapshot.update(template_version='prototype-b-3', title='Подборка SYNTH-REAL-FLOW',
        traveller_summary='двое взрослых. · дети: Не указано',
        requirements='Даты поездки: Не указано\nБюджет в рублях: 200000\nТип поездки: Не указано\nПляж: Не указано\nПожелания: спокойный отдых.')
    for index, hotel in enumerate(snapshot['hotels']):
        hotel.update(name=f'Вымышленный отель {index}', summary='Синтетический вариант',
            pros=['Тестовые достоинства'], limitations=['Нет реального предложения'], source_reference='Синтетика')
    return snapshot


class PDFPaginationTests(SimpleTestCase):
    def test_short_two_hotels_do_not_create_footer_only_page(self):
        reader = PdfReader(io.BytesIO(render_pdf(two_hotel_snapshot())))
        self.assertEqual(len(reader.pages), 1)
        text = compact(reader.pages[0].extract_text())
        for value in expected_values(two_hotel_snapshot()):
            self.assertIn(compact(value), text)
        self.assertIn(compact('Стоимость и наличие тура не подтверждены.'), text)
        self.assertIn(compact('Позднейшие изменения каталога не меняют её содержание.'), text)

    def test_long_pdf_preserves_content_and_keeps_closing_with_hotel_details(self):
        snapshot = fixture(5, True)
        reader = PdfReader(io.BytesIO(render_pdf(snapshot)))
        self.assertGreater(len(reader.pages), 1)
        text = compact('\n'.join(page.extract_text() for page in reader.pages))
        for value in expected_values(snapshot):
            self.assertIn(compact(value), text)
        last = compact(reader.pages[-1].extract_text())
        for value in ('Начало ограничения 5.', 'Конец ограничения 5.',
                      'Стоимость и наличие тура не подтверждены.',
                      'Позднейшие изменения каталога не меняют её содержание.'):
            self.assertIn(compact(value), last)
