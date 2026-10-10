"""H1 «Голос»: persona, present time, prompt budget and initiative wording.

Synthetic data only: no provider network, credentials or working database.
"""
import ast
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import httpx

from cognition.private_followup import followup_text, reminder_text
from cognition.prompting import MEMORY_GUIDANCE, assemble_prompt, memory_for_prompt, prompt_budget_for
from cognition.temporal import day_phase, humanize_gap, temporal_block
from cognition.types import ExpressionPlan
from tests.support.model_connections import configured_models, model_row
from utils.text_processing import strip_voice_tags


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 10, 18, 40, tzinfo=timezone.utc)  # Saturday


def config_literal(name):
    tree = ast.parse((ROOT / 'config.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f'Missing config artifact: {name}')


def plan(**changes):
    values = dict(regulation='acknowledge', tone='calm', warmth=.65, directness=.5, playfulness=0.,
                  disclosure=.195, sticker_mood=None, tts_style='conversational', cause_ids=(),
                  uncertain_intent=False, behaviors=('listen',))
    values.update(changes)
    return ExpressionPlan(**values)


class PersonaContractTests(unittest.TestCase):
    def test_persona_is_feminine_honest_and_shows_its_voice(self):
        prompt = config_literal('ARTI_SYSTEM_PROMPT')
        self.assertIn('О себе говорит в женском роде', prompt)
        self.assertIn('честно говорит, что она ИИ', prompt)
        self.assertGreaterEqual(prompt.count('\nАрти: '), 6)
        self.assertIn('Собственную речь не оборачивает в цитатный блок', prompt)
        self.assertNotIn('<blockquote> — произносимая реплика', prompt)
        self.assertNotIn('Могу помочь с ближайшим шагом. Или просто послушать.', prompt)
        self.assertIn('Не упрекает за молчание и не удерживает в разговоре', prompt)

    def test_voice_anchor_is_short_and_in_character(self):
        anchor = config_literal('ARTI_VOICE_ANCHOR')
        self.assertTrue(anchor.startswith('[Голос]'))
        self.assertIn('в женском роде', anchor)
        self.assertLess(len(anchor), 600)

    def test_lore_no_longer_denies_ai_identity(self):
        lore = (ROOT / 'lore/default_personality.md').read_text(encoding='utf-8')
        self.assertNotIn('Не знает, что она', lore)
        self.assertIn('отвечает честно и в образе', lore)


class PresentTimeTests(unittest.TestCase):
    def test_private_block_uses_saved_timezone_and_previous_contact(self):
        block = temporal_block(NOW, user_timezone='Europe/Moscow',
                               previous_contact=NOW - timedelta(days=3, hours=2))
        self.assertTrue(block.startswith('[Сейчас]'))
        self.assertIn('Магнитогорск): 23:40, суббота, 10 октября, ночь', block)
        self.assertIn('У собеседника: 21:40, суббота, 10 октября, вечер', block)
        self.assertIn('Перерыв с прошлой переписки в этом чате: 3 дня', block)
        self.assertIn('сейчас 18:40 UTC', block)

    def test_unknown_or_invalid_timezone_is_never_guessed(self):
        for zone in (None, '', 'Mars/Base'):
            with self.subTest(zone=zone):
                block = temporal_block(NOW, user_timezone=zone)
                self.assertIn('Часовой пояс собеседника неизвестен', block)
                self.assertNotIn('У собеседника:', block)

    def test_active_conversation_and_group_scope(self):
        active = temporal_block(NOW, user_timezone='Europe/Moscow', previous_contact=NOW - timedelta(minutes=5))
        self.assertIn('без долгого перерыва', active)
        self.assertNotIn('Перерыв с прошлой', active)
        group = temporal_block(NOW, user_timezone='Europe/Moscow', private=False,
                               previous_contact=NOW - timedelta(days=9))
        self.assertNotIn('собеседник', group.casefold())
        self.assertNotIn('Перерыв', group)

    def test_naive_time_is_rejected(self):
        with self.assertRaises(ValueError):
            temporal_block(datetime(2026, 10, 10, 18, 40))

    def test_russian_plural_forms_and_day_phases(self):
        cases = {timedelta(minutes=1): '1 минута', timedelta(minutes=2): '2 минуты',
                 timedelta(minutes=5): '5 минут', timedelta(hours=21): '21 час',
                 timedelta(hours=22): '22 часа', timedelta(days=11): '11 дней',
                 timedelta(days=21): '3 недели', timedelta(days=65): '2 месяца',
                 timedelta(days=400): 'больше года'}
        for delta, text in cases.items():
            with self.subTest(delta=delta):
                self.assertEqual(humanize_gap(delta), text)
        self.assertEqual([day_phase(h) for h in (4, 5, 11, 17, 23)], ['ночь', 'утро', 'день', 'вечер', 'ночь'])


class PromptBudgetTests(unittest.TestCase):
    def test_memory_and_dialogue_survive_the_persona(self):
        # Regression: an unknown tokenizer counts UTF-8 bytes; with the fixed 32k
        # default the persona left room for about one memory and a few turns.
        system = '\n\n'.join((MEMORY_GUIDANCE, config_literal('ARTI_SYSTEM_PROMPT'), plan().instruction(),
                              temporal_block(NOW), config_literal('ARTI_VOICE_ANCHOR')))
        gist = 'Рассказал, что собаку зовут Бублик и что в субботу они ездили на дачу под Челябинском'
        records = [dict(artifact_id=100 + i, source_id=f'telegram:1:{i}:user', familiarity=.3,
                        details=[dict(text=gist, confidence=.8, kind='gist')], time_precision='day',
                        observed_at='2026-10-03T12:34:56+00:00', occurred_at='2026-10-03T12:34:56+00:00',
                        modality='reported', interpretation='', author_id=1,
                        audience=dict(kind='private', chat_id=1, topic_id=-1)) for i in range(8)]
        memory, ids = memory_for_prompt(records, [])
        self.assertGreaterEqual(len(ids), 5)
        dialogue = '\n'.join(f'[2026-10-10 1{i % 10}:00:00] {"Саша" if i % 2 == 0 else "Арти"}: '
                             f'Реплика номер {i} — обычная фраза о делах, планах и выходных.' for i in range(20))
        final, report = assemble_prompt(system, 'Саша: Помнишь, как зовут мою собаку?', dialogue, memory,
                                        model='gemini', budget=prompt_budget_for(None))
        # H2: memory reaches the model as prose; inclusion is reported by artifact id.
        self.assertEqual(report['memory_artifact_ids'], ids)
        self.assertEqual(final.count('собаку зовут Бублик'), len(ids))
        self.assertNotIn('"artifact_id"', final)
        for i in range(20):
            self.assertIn(f'Реплика номер {i} —', final)
        self.assertLessEqual(report['input_tokens'], report['input_limit'])

    def test_budget_follows_the_connection_with_floor_and_cap(self):
        def available(connection):
            b = prompt_budget_for(connection)
            return b.context_tokens - b.output_tokens - b.tool_tokens - b.transport_tokens
        self.assertEqual(available(None), 96000)
        self.assertEqual(available(NS(capabilities={'max_input_chars': 50000})), 50000)
        self.assertEqual(available(NS(capabilities={'max_input_chars': 1000})), 21504)
        self.assertEqual(available(NS(capabilities={'max_input_chars': 2000000})), 96000)
        self.assertEqual(available(NS(capabilities={'max_input_chars': True})), 96000)


class InitiativeWordingTests(unittest.TestCase):
    def test_followup_is_in_voice_gender_neutral_and_stable(self):
        payload = dict(description='Подготовить отчёт по орбите.', delivery_key='slot-1')
        text = followup_text(payload)
        self.assertIn('Подготовить отчёт по орбите', text)
        self.assertNotIn('(а)', text)
        self.assertNotIn('Ты планировал', text)
        self.assertEqual(text, followup_text(dict(payload)))
        variants = {followup_text(dict(description='x', delivery_key=f'k{i}')) for i in range(40)}
        self.assertGreater(len(variants), 1)

    def test_reminder_keeps_requested_content_verbatim(self):
        text = reminder_text(dict(description='позвонить маме в 10:00', delivery_key='r'))
        self.assertIn('позвонить маме в 10:00', text)
        self.assertFalse(text.startswith('Напоминание: '))
        self.assertIn('договаривались', reminder_text(dict(description='', delivery_key='r')))


class VoiceTagDisplayTests(unittest.TestCase):
    def test_only_english_voice_markers_are_hidden(self):
        self.assertEqual(strip_voice_tags('[warm aristocratic tone] Привет [laughing], как ты?'), 'Привет, как ты?')
        self.assertEqual(strip_voice_tags('Да [sigh] ладно'), 'Да ладно')
        self.assertEqual(strip_voice_tags('Источник [1], см. [выше]'), 'Источник [1], см. [выше]')
        self.assertEqual(strip_voice_tags('<code>arr[index]</code> [Uhm] ок'), '<code>arr[index]</code> ок')
        self.assertEqual(strip_voice_tags('<pre>x = d[key]</pre>'), '<pre>x = d[key]</pre>')
        self.assertEqual(strip_voice_tags(''), '')


class GenerationOrderTests(unittest.IsolatedAsyncioTestCase):
    async def generate(self, **kwargs):
        from ai import generation
        captured = []

        def handler(request):
            captured.append(request)
            return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 1,
                'model': 'upstream', 'choices': [{'index': 0, 'message': {'role': 'assistant',
                'content': 'Ответ'}, 'finish_reason': 'stop'}]})
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with configured_models(model_row('gemini')), patch.object(generation, 'provider_http_client', return_value=http):
            result = await generation.generate_response_stream(1, 'Привет', 'User', '', request_intent={}, **kwargs)
        self.assertEqual(result[0], 'Ответ')
        return json.loads(captured[0].content)['messages'][0]['content']

    async def test_service_rules_first_and_voice_last(self):
        memory = json.dumps(dict(artifact_id=1, source_id='telegram:1:1:user'), ensure_ascii=False)
        system = await self.generate(memory_context=memory, expression_plan=plan())
        markers = ('[Как читать память]', 'Ты — Арти.', '[Состояние и манера сейчас]', '[Сейчас]', '[Голос]')
        positions = [system.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(system.rstrip().endswith(config_literal('ARTI_VOICE_ANCHOR')))

    async def test_roleplay_has_no_real_clock_or_default_anchor(self):
        system = await self.generate(is_rp_mode=True, expression_plan=plan())
        self.assertIn('[Состояние и манера сейчас]', system)
        self.assertNotIn('[Сейчас]', system)
        self.assertNotIn('[Голос]', system)

    async def test_custom_prompt_without_persona_gets_no_anchor(self):
        system = await self.generate(custom_system_prompt='System')
        self.assertNotIn('[Голос]', system)


if __name__ == '__main__':
    unittest.main()
