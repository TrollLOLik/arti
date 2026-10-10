"""H4b «Своя жизнь: память о себе и рефлексия» (P5, M3).

Synthetic data only: recorded interpreter, fake model transport, disposable PostgreSQL.
"""
import json
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace as NS

import httpx

from cognition.memory_render import SECTION_TITLES, render_card
from cognition.memory_repository import self_kind
from cognition.prompting import memory_for_prompt
from cognition.reflection import CONFIDENCE_CAP, MAX_HYPOTHESES, validate
from cognition.types import Origin
from tests.cognition.test_affect import AT

NOW = AT + timedelta(days=3)
REFS = {'T1', 'T2', 'T3', 'T4'}


def hypothesis(text='Похоже, собеседник чаще пишет поздно вечером, когда день был тяжёлым.',
               about='person', evidence=('T1', 'T3'), confidence=.5):
    return dict(text=text, about=about, evidence=list(evidence), confidence=confidence)


class SelfModelViewTests(unittest.TestCase):
    def test_kinds_follow_the_predicate(self):
        self.assertEqual([self_kind(p) for p in ('likes', 'dislikes', 'prefers', 'favorite_tea', 'opinion_books',
                                                  'story_childhood', 'running_joke_kettle', 'eyes_color')],
                         ['preference'] * 4 + ['opinion', 'story', 'running_joke', 'statement'])

    def test_what_she_said_renders_as_her_own_words(self):
        line = render_card(dict(card='self', predicate='likes', value='чай с бергамотом',
                                said_at=AT.isoformat(), first_said_at=(AT - timedelta(days=5)).isoformat(), times=3), NOW)
        self.assertIn('Тебе нравится: «чай с бергамотом»', line)
        self.assertIn('и снова', line)
        self.assertIn('уже 3 раза', line)
        self.assertIn('Твоё мнение: «Бродский', render_card(dict(card='self', predicate='opinion_books', value='Бродский переоценён'), NOW))
        previous = render_card(dict(card='self_previous', predicate='likes', value='жасмин',
                                    superseded_at=AT.isoformat()), NOW)
        self.assertIn('Раньше ты говорила иначе: «жасмин»', previous)
        self.assertIn('передумала', previous)

    def test_a_reflection_is_always_a_guess(self):
        line = render_card(dict(card='reflection', text='Похоже, вы часто спорите о книгах и обоим это нравится.',
                                about='relationship', confidence=.5, created_at=AT.isoformat()), NOW)
        self.assertIn('Твоя догадка о ваших разговорах (не факт', line)
        self.assertIn('не выдавай за факт', line)
        self.assertIn('о собеседнике', render_card(dict(card='reflection', text='Похоже, что-то.', about='person'), NOW))

    def test_self_section_follows_the_card_with_its_own_title(self):
        card = [dict(card='acquaintance', artifact_id=1, stage='familiar', messages=10, days=2),
                dict(card='self', section='self', artifact_id=2, predicate='likes', value='бергамот', said_at=AT.isoformat()),
                dict(card='reflection', artifact_id=3, text='Похоже, собеседник любит точные формулировки.', about='person',
                     confidence=.5, created_at=AT.isoformat())]
        memory, ids = memory_for_prompt([], [], card=card, owner_id=1, now=NOW)
        self.assertEqual(ids, [1, 3, 2])
        from cognition.memory_render import render_record
        view = '\n'.join(render_record(json.loads(line), NOW) for line in memory.splitlines())
        self.assertIn('бергамот', view)
        self.assertEqual(set(SECTION_TITLES), {'card', 'self', 'recall'})


class ReflectionValidationTests(unittest.TestCase):
    def test_supported_hypotheses_are_kept_and_capped(self):
        kept = validate({'hypotheses': [hypothesis(confidence=.9)]}, REFS)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['confidence'], CONFIDENCE_CAP)
        self.assertEqual(kept[0]['evidence'], ['T1', 'T3'])

    def test_unsupported_sensitive_or_malformed_items_are_dropped(self):
        bad = [hypothesis(evidence=('T1',)),
               hypothesis(evidence=('T1', 'T9')),
               hypothesis(text='Похоже, у собеседника депрессия: пишет ночью.'),
               hypothesis(text='Кажется, собеседник верующий и ходит в церковь.'),
               hypothesis(about='other'),
               hypothesis(text='коротко'),
               hypothesis(confidence=True),
               {**hypothesis(), 'extra': 1}]
        self.assertEqual(validate({'hypotheses': bad}, REFS), [])
        same = validate({'hypotheses': [hypothesis(), hypothesis()]}, REFS)
        self.assertEqual(len(same), 1)
        many = validate({'hypotheses': [hypothesis(text=f'Похоже, повторяется наблюдение номер {n}.') for n in range(6)]}, REFS)
        self.assertEqual(len(many), MAX_HYPOTHESES)
        for body in ({}, {'hypotheses': 'x'}, {'hypotheses': [], 'more': 1}, []):
            with self.assertRaises(ValueError):
                validate(body, REFS)


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def model_for(self, chat_id):
        return 'fake-model'

    async def complete(self, model, messages, tokens, temperature=0):
        self.calls.append(messages)
        item = self.responses.pop(0)
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json={'choices': [{'message': {'content': item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)}}]})


class SelfStatementInterpreterTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivered_reply_contract_and_grounded_self_belief(self):
        from cognition.interpreter import SelectedModelInterpreter
        from cognition.types import CognitiveEvent, ContextKey, EvidenceRef
        text = 'Я обожаю чай с бергамотом, это моя слабость.'
        ev = CognitiveEvent('d1', ContextKey('arti', 10), EvidenceRef('d1', 'd1', Origin.DELIVERED_ACTION, 1), AT, AT,
                            text, None, 1)
        start = text.index('чай с бергамотом')
        situation = dict(topic='чай', kind='neutral', modality='interaction', intention_evidence='unobserved', outcome='unknown',
                         spans=[dict(start=0, end=len(text), text=text)], details=[], intentions=[], revisions=[],
                         beliefs=[dict(span=0, subject='arti', predicate='likes', value='чай с бергамотом', condition='',
                                       assertion='explicit', confidence=.9)], preferences={}, social_signal='contact')
        transport = FakeTransport({'appraisals': [], 'situation': situation})
        result = await SelectedModelInterpreter(transport=transport).interpret(ev, rich=True)
        contract = json.loads(transport.calls[0][1]['content'])['action_contract']
        self.assertIn('subject "arti"', contract)
        self.assertIn('Never a belief about the person', contract)
        self.assertEqual(result.perception.situation.beliefs[0]['value'], text[start:start + len('чай с бергамотом')])


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class SelfAndReflectionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from bot.request_runtime import CURRENT_REQUEST
        from cognition.runtime import CognitiveRuntime, CURRENT_TURN
        from cognition.scope import CURRENT_SCOPE
        from tests.cognition.test_full_model import RecordedInterpreter
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.tokens = [(v, v.set(None)) for v in (CURRENT_TURN, CURRENT_SCOPE, CURRENT_REQUEST)]
        self.at = AT
        self.interpreter = RecordedInterpreter()
        self.runtime = await CognitiveRuntime(self.pool, self.interpreter, 'active', clock=lambda: self.at).initialize(False)
        self.message = 0

    async def asyncTearDown(self):
        await self.runtime.close()
        for var, token in reversed(self.tokens):
            var.reset(token)
        await self.db.__aexit__(None, None, None)

    async def say(self, text, origin=Origin.USER, chat=10, mode='default', beliefs=None):
        from tests.cognition.test_full_model import situation
        self.message += 1
        cid, eid, ev = await self.runtime.ingest(chat, 1, text, self.message, mode=mode, origin=origin)
        if beliefs:
            self.interpreter.frames[text] = situation(ev, beliefs=[
                dict(span=0, condition='', assertion='explicit', confidence=.9, **b) for b in beliefs])
        await self.runtime.process(cid, eid)
        self.at += timedelta(minutes=1)
        return cid, eid, ev

    async def arti(self, text, beliefs=None, **kwargs):
        return await self.say(text, origin=Origin.DELIVERED_ACTION, beliefs=beliefs, **kwargs)

    async def card(self, cid, query=''):
        return await self.runtime.memory.person_card(cid, 1, self.at, query=query)

    async def test_she_remembers_what_she_said_about_herself_and_owns_a_change(self):
        cid, _, _ = await self.arti('Я обожаю чай с бергамотом.', beliefs=[
            dict(subject='arti', predicate='likes', value='чай с бергамотом'),
            dict(subject=1, predicate='likes', value='обожаю')])
        mine = await self.runtime.memory.artifacts(cid, 1, 'self_belief')
        self.assertEqual([r['payload']['value'] for r in mine], ['чай с бергамотом'])
        # Her own reply never becomes a fact about the person.
        self.assertEqual(await self.runtime.memory.artifacts(cid, 1, 'belief'), [])
        await self.say('А какой чай ты любишь?')
        await self.arti('Если честно, теперь мне больше нравится жасминовый чай.', beliefs=[
            dict(subject='arti', predicate='likes', value='жасминовый чай')])
        view = [c for c in await self.card(cid, 'какой чай ты пьёшь') if c.get('section') == 'self']
        self.assertEqual([(c['card'], c['value']) for c in view],
                         [('self', 'жасминовый чай'), ('self_previous', 'чай с бергамотом')])
        memory, ids = memory_for_prompt([], [], card=await self.card(cid, 'чай'), owner_id=1, now=self.at)
        valid, _ = await self.runtime.memory.validate_retrieval(cid, 1, ids)
        self.assertEqual(sorted(ids), valid)
        # Forgetting her newer reply restores the earlier statement as current.
        from cognition.forgetting import forget_cognitive_sources
        newest = await self.pool.fetchval("SELECT max(id) FROM cognitive_events WHERE origin='delivered_action'")
        source = await self.pool.fetchval('SELECT source_id FROM cognitive_events WHERE id=$1', newest)
        await forget_cognitive_sources(self.pool, cid, 1, [source])
        view = [c for c in await self.card(cid, 'чай') if c.get('section') == 'self']
        self.assertEqual([(c['card'], c['value']) for c in view], [('self', 'чай с бергамотом')])
        self.assertNotIn('жасмин', str(await self.card(cid, 'чай')))

    async def test_repetition_counts_and_relevance_orders_the_view(self):
        cid, _, _ = await self.arti('Я люблю дождь.', beliefs=[dict(subject='arti', predicate='likes', value='дождь')])
        await self.arti('Я всё ещё люблю дождь.', beliefs=[dict(subject='arti', predicate='likes', value='дождь')])
        await self.arti('По-моему, Бродский великий поэт.', beliefs=[
            dict(subject='arti', predicate='opinion_brodsky', value='Бродский великий поэт')])
        view = [c for c in await self.card(cid, 'что думаешь про Бродский') if c['card'] == 'self']
        self.assertEqual(view[0]['value'], 'Бродский великий поэт')
        rain = next(c for c in view if c['value'] == 'дождь')
        self.assertEqual(rain['times'], 2)

    async def test_no_self_model_from_roleplay(self):
        cid, _, _ = await self.arti('Я, графиня, люблю балы.', mode='rp',
                                    beliefs=[dict(subject='arti', predicate='likes', value='балы')])
        self.assertEqual(await self.runtime.memory.artifacts(cid, 1, 'self_belief'), [])

    async def conversation(self, n=10):
        cid = None
        for i in range(n):
            cid, _, _ = await self.say(f'Сообщение номер {i}: опять засиделся над книгой допоздна.')
            await self.arti(f'Ответ номер {i}: книги ночью — это у тебя традиция.')
        self.at += timedelta(hours=1)
        return cid

    async def test_reflection_after_a_pause_adds_a_cited_guess_to_the_card(self):
        from cognition.reflection import Reflection
        cid = await self.conversation()
        transport = FakeTransport({'hypotheses': [
            hypothesis(text='Похоже, собеседник часто читает допоздна и любит об этом рассказывать.', evidence=('T1', 'T5')),
            hypothesis(text='Похоже, у собеседника бессонница как болезнь.', evidence=('T1', 'T3'))]})
        service = Reflection(self.runtime, transport)
        self.assertEqual(await service.due(), [(cid, 1)])
        stored = await service.reflect(cid, 1)
        self.assertEqual([s['text'] for s in stored], ['Похоже, собеседник часто читает допоздна и любит об этом рассказывать.'])
        self.assertEqual(stored[0]['assertion'], 'inferred')
        self.assertLessEqual(stored[0]['confidence'], CONFIDENCE_CAP)
        sent = json.loads(transport.calls[0][1]['content'])['observations']
        self.assertEqual({o['author'] for o in sent}, {'person', 'arti'})
        card = [c for c in await self.card(cid) if c['card'] == 'reflection']
        self.assertEqual(len(card), 1)
        self.assertEqual(await service.due(), [])
        # Forgetting a cited message hides the guess built on it.
        from cognition.forgetting import forget_cognitive_sources
        reflection = (await self.runtime.memory.artifacts(cid, 1, 'reflection'))[0]
        cited = await self.pool.fetch('''SELECT e.source_id FROM cognitive_provenance p JOIN cognitive_events e ON e.id=p.source_event_id
            WHERE p.artifact_id=$1''', reflection['id'])
        self.assertEqual(len(cited), 2)
        await forget_cognitive_sources(self.pool, cid, 1, [cited[0]['source_id']])
        self.assertEqual([c for c in await self.card(cid) if c['card'] == 'reflection'], [])

    async def test_reflection_waits_for_a_pause_and_enough_messages(self):
        from cognition.reflection import Reflection
        cid = None
        for i in range(3):
            cid, _, _ = await self.say(f'Короткое сообщение {i}')
        service = Reflection(self.runtime, FakeTransport())
        self.at += timedelta(hours=1)
        self.assertEqual(await service.due(), [])
        for i in range(8):
            await self.say(f'Ещё сообщение {i}')
        self.assertEqual(await service.due(), [])  # still talking
        self.at += timedelta(minutes=31)
        self.assertEqual(await service.due(), [(cid, 1)])

    async def test_bad_output_is_not_retried_and_provider_failure_backs_off(self):
        from cognition.reflection import Reflection
        cid = await self.conversation()
        transport = FakeTransport(httpx.Response(503, json={}), 'not json')
        service = Reflection(self.runtime, transport)
        self.assertEqual(await service.run_cycle(), 0)
        self.assertEqual(await service.run_cycle(), 0)  # backed off, no second call
        self.assertEqual(len(transport.calls), 1)
        self.at += timedelta(hours=2)
        self.assertEqual(await service.run_cycle(), 1)
        self.assertEqual(await service.due(), [])
        self.assertEqual(await self.runtime.memory.artifacts(cid, 1, 'reflection'), [])


if __name__ == '__main__':
    unittest.main()
