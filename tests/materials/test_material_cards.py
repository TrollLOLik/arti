"""Offline synthetic previews and source-fenced, mock Telegram delivery."""
import asyncio
import os
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageFont

from artifacts.computation import ComputationSpec, compute
from artifacts.material_cards import (MaterialCard, card_scene, computation_card,
    dataset_card, extraction_card, locator_text, render_card)
from artifacts.rendering import FONT
from materials.dataset_quality import diagnose
from materials.datasets import datasets_from_bundle
from materials.extractors.basic import BasicExtractor
from materials.extractors.tables import TableExtractor, XLSX
from materials.runtime import CURRENT_MATERIAL_USE, CURRENT_COMPUTATION_USE, CURRENT_DERIVATIVE_USE
from materials.types import ExtractionManifest, MaterialError
from tests.materials.table_fixtures import xlsx


def fixtures():
    bundle = asyncio.run(TableExtractor().extract_async('synthetic-workbook', 1, xlsx(), XLSX))
    datasets = datasets_from_bundle('synthetic-extraction', bundle)
    environment = {(d.name, c.address): c for d in datasets for c in d.cells}
    reports = tuple(diagnose(d, formula_cells=environment) for d in datasets)
    result = compute(datasets[0], ComputationSpec('sum', 'B2:B3'), formula_cells=environment)
    return bundle, datasets, reports, result


class MaterialCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle, cls.datasets, cls.reports, cls.result = fixtures()

    def test_dataset_counts_are_diagnostics_not_a_quality_score(self):
        card = dataset_card(self.datasets, self.reports)
        self.assertEqual(str(sum(len(d.cells) for d in self.datasets)), card.metrics[1][0])
        self.assertEqual(str(sum(len(r['findings']) for r in self.reports)), card.metrics[2][0])
        self.assertIn('заголов', card.metrics[1][1].casefold())
        self.assertIn('не доказывает', card.caution)
        self.assertNotIn('%', str(card))
        with self.assertRaisesRegex(MaterialError, 'material_card_dataset_mismatch'):
            dataset_card(self.datasets, self.reports[::-1])

    def test_calculation_uses_exact_value_and_input_limits(self):
        card = computation_card(self.datasets[0], self.result)
        self.assertEqual('1500.3 RUB', card.title)
        self.assertEqual(str(len(self.result.inputs)), card.metrics[0][0])
        self.assertIn(self.result.id[:12], card.source)
        bounded = replace(self.result, result=dict(value='1500.3', unit='RUB', lower='1499', upper='1502'),
                          warnings=('uncertain_source_input', 'missing_inputs_excluded'))
        card = computation_card(self.datasets[0], bounded)
        self.assertIn('Границы: 1499…1502 RUB', card.details)
        self.assertIn('Пропуски исключены', card.details)
        self.assertIn('не вероятность', card.caution)
        with self.assertRaisesRegex(MaterialError, 'material_card_dataset_mismatch'):
            computation_card(self.datasets[1], self.result)

    def test_unknown_and_failed_coverage_never_becomes_complete_from_counts(self):
        for coverage in ('unknown', 'partial', 'failed'):
            bundle = replace(self.bundle, manifest=ExtractionManifest(10, 10, coverage, ('sampled',), 'page'))
            card = extraction_card(bundle, 'synthetic.pdf')
            self.assertNotIn('Все заявленные', str(card))
            self.assertIn('манифест', str(card))
            self.assertIn('не подтверждает', card.caution)
        card = extraction_card(self.bundle)
        self.assertNotIn('100%', str(card))

    def test_source_locator_never_guesses_coordinates(self):
        self.assertEqual('документ', locator_text({'kind': 'document'}))
        self.assertEqual('Лист!B2', locator_text({'sheet': 'Лист', 'cell': 'B2'}))
        self.assertEqual('1000–2000 мс', locator_text({'start_ms': 1000, 'end_ms': 2000}))

    def test_raster_palette_and_measured_layout(self):
        cards = [dataset_card(self.datasets, self.reports), computation_card(self.datasets[0], self.result),
                 extraction_card(self.bundle, 'Длинное название ' * 35)]
        cards.append(replace(cards[1], title='9' * 105 + ' RUB', details=tuple('Очень длинное пояснение ' * 30 for _ in range(20))))
        for card in cards:
            with self.subTest(kind=card.kind):
                items, size, theme = card_scene(card)
                self.assertLessEqual(size[1], 1440)
                bounds = []
                for item in items:
                    if item['kind'] != 'text':
                        continue
                    self.assertGreaterEqual(item['size'], 22)
                    l, t, r, b = ImageFont.truetype(str(FONT), item['size']).getbbox(item['text'], anchor='lt')
                    box = item['x']+l, item['y']+t, item['x']+r, item['y']+b
                    self.assertLessEqual(box[2], size[0]-32)
                    self.assertLessEqual(box[3], size[1]-20)
                    self.assertFalse(any(box[0] < right and box[2] > left and box[1] < bottom and box[3] > top
                                         for left, top, right, bottom in bounds), item['text'])
                    bounds.append(box)
                pixels = render_card(card)
                with Image.open(BytesIO(pixels)) as image:
                    self.assertEqual(size, image.size)
                    self.assertEqual((228, 216, 216), image.getpixel((0, 0)))
                    colors = {c for _, c in image.getcolors(image.width*image.height)}
                    self.assertIn((132, 0, 24), colors)
                    self.assertNotIn((228, 120, 84), colors)


class MaterialCardDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.card = MaterialCard('ТЕСТ', 'Исходные данные', 'Только синтетика', (('2', 'Ячейки'),),
                                 ('Полнота неизвестна',), 'Не оценка качества модели.', 'Источник: fixture')
        self.message = NS(chat_id=55, message_id=9, message_thread_id=None, reply_text=AsyncMock())
        self.context = NS(bot=NS(send_photo=AsyncMock(return_value=NS(message_id=10))))

    async def test_one_preview_plus_copyable_text(self):
        from bot.material_search import send_material_card
        guard = AsyncMock()
        with patch('bot.material_search.guard_current', guard):
            await send_material_card(self.message, self.context, self.card, '2 ячейки; источник fixture')
        self.context.bot.send_photo.assert_awaited_once()
        kwargs = self.context.bot.send_photo.await_args.kwargs
        self.assertLessEqual(len(kwargs['caption'].encode('utf-16-le'))//2, 1024)
        self.assertEqual(9, kwargs['reply_parameters'].message_id)
        self.assertTrue(kwargs['photo'].startswith(b'\x89PNG'))
        self.message.reply_text.assert_awaited_once_with('2 ячейки; источник fixture', parse_mode=None)
        self.assertGreaterEqual(guard.await_count, 4)

    async def test_render_failure_keeps_text(self):
        from bot.material_search import send_material_card
        with patch('bot.material_search.render_card', side_effect=ValueError('synthetic render failure')):
            await send_material_card(self.message, self.context, self.card, 'copyable')
        self.context.bot.send_photo.assert_not_awaited()
        self.message.reply_text.assert_awaited_once()

    async def test_revoked_during_render_sends_nothing(self):
        from bot.material_search import send_material_card
        validator = AsyncMock(side_effect=[None, MaterialError('source_erased')])
        with self.assertRaises(MaterialError):
            await send_material_card(self.message, self.context, self.card, 'secret', validate=validator)
        self.context.bot.send_photo.assert_not_awaited()
        self.message.reply_text.assert_not_awaited()

    async def test_revoked_after_receipt_preparation_sends_nothing(self):
        from bot.material_search import send_material_card
        validator = AsyncMock(side_effect=[None, None, MaterialError('source_erased')])
        with self.assertRaises(MaterialError):
            await send_material_card(self.message, self.context, self.card, 'secret', validate=validator)
        self.context.bot.send_photo.assert_not_awaited()
        self.message.reply_text.assert_not_awaited()

    async def test_revoked_between_photo_and_text_suppresses_text(self):
        from bot.material_search import send_material_card
        validator = AsyncMock(side_effect=[None, None, None, MaterialError('source_erased')])
        with self.assertRaises(MaterialError):
            await send_material_card(self.message, self.context, self.card, 'secret', validate=validator)
        self.context.bot.send_photo.assert_awaited_once()
        self.message.reply_text.assert_not_awaited()

    async def test_unknown_photo_delivery_does_not_retry_or_fallback(self):
        from bot.material_search import send_material_card
        from telegram.error import TimedOut
        self.context.bot.send_photo.side_effect = TimedOut()
        with self.assertRaises(TimedOut):
            await send_material_card(self.message, self.context, self.card, 'secret')
        self.context.bot.send_photo.assert_awaited_once()
        self.message.reply_text.assert_not_awaited()

    async def test_known_topic_preserved(self):
        from bot.material_search import send_material_card
        from cognition.scope import CURRENT_SCOPE, TransportScope
        token = CURRENT_SCOPE.set(TransportScope(55, 4, 'supergroup', 7))
        try:
            await send_material_card(self.message, self.context, self.card, 'copyable')
            self.assertEqual(4, self.context.bot.send_photo.await_args.kwargs['message_thread_id'])
        finally:
            CURRENT_SCOPE.reset(token)


class MaterialSummaryCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bundle = BasicExtractor().extract('source-asset', 1,
            'Буквальная строка.\nЕщё один фрагмент.\nТретий.\nЧетвёртый.'.encode(), 'text/plain')
        self.service = NS(repository=object(), extract=AsyncMock(return_value=('extract-id', self.bundle)))
        self.use = NS(asset_id=self.bundle.asset_id, version=1, service=self.service, actor=NS(scope=NS(chat_id=55)),
                      validate=AsyncMock())
        self.capture = NS(material_uses=(self.use,))
        self.message = NS(chat_id=55, message_id=9, text='/material_summary', reply_text=AsyncMock(),
                          reply_to_message=NS(document=NS(file_name='synthetic.txt')))
        self.context = NS(bot=NS(send_photo=AsyncMock(return_value=NS(message_id=10))))
        self.update = NS(effective_message=self.message)

    async def run_command(self):
        from bot.material_search import material_summary_command
        with patch('bot.material_search.enabled', return_value=True), \
                patch('materials.runtime.capture_document', AsyncMock(return_value=self.capture)), \
                patch('materials.retrieval.verify_quote', AsyncMock()) as quote:
            await material_summary_command(self.update, self.context)
        return quote

    async def test_explicit_summary_quotes_are_verified_and_not_paraphrased(self):
        quote = await self.run_command()
        self.assertEqual(3, quote.await_count)
        for call, block in zip(quote.await_args_list, self.bundle.blocks):
            self.assertEqual(block.text, call.args[3])
            self.assertEqual(block.block_id, call.args[2].block_id)
        text = self.message.reply_text.await_args.args[0]
        self.assertIn('Буквальная строка.', text)
        self.assertNotIn('Четвёртый.', text)
        self.assertIn('не смысловой конспект', text)
        self.assertIn('качество не оценено', text)
        self.context.bot.send_photo.assert_awaited_once()
        self.assertEqual((), CURRENT_MATERIAL_USE.get())

    async def test_changed_version_is_not_mixed_with_capture(self):
        self.service.extract.return_value = ('extract-new', replace(self.bundle, asset_version=2))
        quote = await self.run_command()
        quote.assert_not_awaited()
        self.context.bot.send_photo.assert_not_awaited()
        self.assertIn('Источник недоступен', self.message.reply_text.await_args.args[0])

    async def test_interpreted_block_not_called_a_literal_extract(self):
        self.service.extract.return_value = ('extract-id', replace(self.bundle,
            blocks=(replace(self.bundle.blocks[0], observation='interpreted'),)))
        quote = await self.run_command()
        quote.assert_not_awaited()
        self.assertIn('Буквальных текстовых фрагментов нет', self.message.reply_text.await_args.args[0])

    async def test_no_document_preserves_help_path(self):
        self.message.reply_to_message = None
        quote = await self.run_command()
        quote.assert_not_awaited()
        self.context.bot.send_photo.assert_not_awaited()
        self.assertIn('Ответь на документ', self.message.reply_text.await_args.args[0])

    async def test_routing_registered(self):
        from bot.menu.bridge import HANDLERS
        self.assertEqual(('bot.material_search', 'material_summary_command'), HANDLERS['material_summary'])
        main = Path(__file__).parents[2].joinpath('main.py').read_text()
        self.assertIn("CommandHandler('material_summary',material_summary_command)", main)

    async def test_copyable_text_limit_counts_surrogate_pairs(self):
        from bot.material_search import _bounded
        text = _bounded('😀'*4000, 3900)
        self.assertLessEqual(len(text.encode('utf-16-le'))//2, 3900)
        self.assertTrue(text.endswith('…'))

    async def test_wide_report_preserves_limitations_and_source_footer(self):
        from bot.material_search import bounded_report
        footer = '\nОграничения: неполное извлечение.\nИсточник: dataset-123; оригинал над командой.'
        text = bounded_report('😀 Строка данных\n'*1000, footer)
        self.assertLessEqual(len(text.encode('utf-16-le'))//2, 3900)
        self.assertIn('Список сокращён', text)
        self.assertTrue(text.endswith(footer))


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL')
class MaterialCardDatabaseTests(unittest.IsolatedAsyncioTestCase):
    from tests.materials.test_dataset_persistence import DatasetPersistenceTests as _Fixture
    asyncSetUp = _Fixture.asyncSetUp
    asyncTearDown = _Fixture.asyncTearDown

    async def command(self, name, *, before_photo=None, render=None):
        from bot.table_commands import dataset_command, calc_command
        from bot.material_search import material_summary_command
        from cognition.scope import CURRENT_SCOPE, TransportScope
        # A repeated Telegram file ID must keep the exact ZIP bytes, including
        # its archive timestamps, across both commands.
        if not hasattr(self, '_source_bytes'):
            self._source_bytes = xlsx()
        data = self._source_bytes
        document = NS(file_id='file', file_name='synthetic.xlsx', file_size=len(data), mime_type=None)
        original = NS(chat_id=-100, message_id=42, message_thread_id=4, document=document,
                      sender_chat=None, from_user=NS(id=7))
        message = NS(chat_id=-100, message_id=43, message_thread_id=4, text=name,
                     reply_to_message=original, reply_text=AsyncMock())
        async def photo(**kwargs):
            if before_photo:
                await before_photo()
            return NS(message_id=44)
        context = NS(bot=NS(send_photo=AsyncMock(side_effect=photo), get_file=AsyncMock(return_value=NS(
            file_size=len(data), download_as_bytearray=AsyncMock(return_value=bytearray(data))))))
        token = CURRENT_SCOPE.set(TransportScope(-100, 4, 'supergroup', 7, sender_ref='user:7'))
        try:
            from contextlib import ExitStack
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {'ARTI_MATERIALS_ENABLED': '1'}))
                for module in ('materials.runtime', 'bot.table_commands'):
                    stack.enter_context(patch(module+'.actor_for_current', AsyncMock(return_value=self.actor)))
                    stack.enter_context(patch(module+'.service_for_bot', AsyncMock(return_value=self.service)))
                stack.enter_context(patch('cognition.runtime.get_runtime', return_value=None))
                if render:
                    stack.enter_context(patch('bot.material_search.asyncio.to_thread', side_effect=render))
                method = material_summary_command if name.startswith('/material_summary') else calc_command if name.startswith('/calc') else dataset_command
                await method(NS(effective_message=message), context)
        finally:
            CURRENT_SCOPE.reset(token)
        return message, context

    async def test_real_dataset_and_calculation_send_photo_and_text(self):
        for command, expected in (('/dataset', 'Таблицы:'), ('/calc sum B2:B3 sheet=1', '1500.3 RUB')):
            with self.subTest(command=command):
                message, context = await self.command(command)
                self.assertEqual(1, context.bot.send_photo.await_count, message.reply_text.await_args)
                self.assertEqual(4, context.bot.send_photo.await_args.kwargs['message_thread_id'])
                self.assertIn(expected, message.reply_text.await_args.args[0])
                self.assertEqual((), CURRENT_DERIVATIVE_USE.get())
                self.assertEqual((), CURRENT_COMPUTATION_USE.get())

    async def test_real_summary_keeps_quote_validator_and_source(self):
        message, context = await self.command('/material_summary')
        context.bot.send_photo.assert_awaited_once()
        self.assertIn('Буквальные фрагменты', message.reply_text.await_args.args[0])
        self.assertIn('synthetic.xlsx', message.reply_text.await_args.args[0])

    async def test_correction_during_render_suppresses_stale_photo(self):
        original = asyncio.to_thread
        async def render_then_correct(function, *args, **kwargs):
            value = await original(function, *args, **kwargs)
            if function.__name__ == 'render_card':
                # Capture ingestion has its own material; target the actual
                # snapshot from the active derivative guard, not this fixture.
                use = CURRENT_DERIVATIVE_USE.get()[0]
                proposal = await self.service.propose_correction(use.id, self.actor, 'B2', '1300')
                await self.service.confirm_correction(self.actor, proposal)
            return value
        message, context = await self.command('/dataset', render=render_then_correct)
        context.bot.send_photo.assert_not_awaited()
        self.assertIn('Данные уже изменились', message.reply_text.await_args.args[0])

    async def test_erasure_after_photo_prevents_copyable_data(self):
        async def erase():
            await self.lifecycle.forget(CURRENT_MATERIAL_USE.get()[0].asset_id, self.actor)
        message, context = await self.command('/calc sum B2:B3 sheet=1', before_photo=erase)
        context.bot.send_photo.assert_awaited_once()
        self.assertNotIn('1500.3 RUB', message.reply_text.await_args.args[0])
        self.assertIn('Не удалось', message.reply_text.await_args.args[0])

    async def test_dataset_guard_survives_request_codec_roundtrip(self):
        from bot.request_codec import encode_value, decode_value
        from materials.runtime import DerivativeUse
        use = DerivativeUse(self.budget.id, self.actor, self.derivatives, 'dataset')
        with patch('materials.runtime.service_for_bot', AsyncMock(return_value=self.service)):
            restored = await decode_value(await encode_value(use))
            await restored.validate()
            proposal = await self.service.propose_correction(self.budget.id, self.actor, 'B2', '1300')
            await self.service.confirm_correction(self.actor, proposal)
            with self.assertRaisesRegex(MaterialError, 'stale_dataset_head'):
                await restored.validate()
