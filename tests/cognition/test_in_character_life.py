"""H6 (P1) «Жизнь в образе»: Arti's pursuits, tastes and the prompt block.

Synthetic data only: a fake model writer, disposable PostgreSQL; no provider
network, credentials or working database.
"""
import asyncio
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import httpx

from cognition import pursuits as pu

ROOT = Path(__file__).resolve().parents[2]
AT = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)  # 12:00 on her clock
TABLES = {'arti_pursuits', 'arti_pursuit_events', 'arti_tastes', 'arti_pursuit_runs', 'arti_reading_notes'}


def answer(summary, feeling='спокойно'):
    return json.dumps({'summary': summary, 'feeling': feeling}, ensure_ascii=False)


def words_for(facts):
    """A well-behaved writer: retells the step's own facts."""
    event = facts['событие']
    summary = event[0].upper() + event[1:] + '.'
    if 'что' in facts:
        summary = f'{summary[:-1]}: {facts["что"].split(" (")[0]}.'
    return answer(summary)


def embroidery_row(stitched=0, steps=0, number=1, **state):
    spec = pu.initial('embroidery', number)
    spec['state'].update(stitched=stitched, steps=steps, **state)
    return dict(spec, id=7, status='active', next_step_at=None,
                progress=stitched / spec['state']['pattern']['total'])


def restoration_row(stage=0, stage_step=0, steps=0, number=1):
    spec = pu.initial('restoration', number)
    spec['state'].update(stage=stage, stage_step=stage_step, steps=steps)
    total = sum(spec['state']['record']['stage_steps'])
    return dict(spec, id=8, status='active', next_step_at=None, progress=steps / total)


class FakeConn:
    def __init__(self, notes=(), tastes=()):
        self.notes, self.tastes = list(notes), list(tastes)

    async def fetch(self, query, *args):
        return self.notes if 'arti_reading_notes' in query else self.tastes


def tea_plan(item_text='Бергамот: как его добавляют в чай', tastes=()):
    note = dict(id=11, title=item_text, url='https://example.org/tea', gist='О чае.')
    row = dict(pu.initial('tea'), id=9, status='active', next_step_at=None)
    plan, _ = asyncio.run(pu.plan_tea(FakeConn([note], tastes), row, AT))
    return plan


@patch.object(pu, 'CHANGE_CHANCE', 0)
class PlanTests(unittest.TestCase):
    def test_pattern_is_deterministic_and_symmetric(self):
        first, again = pu.embroidery_pattern(3), pu.embroidery_pattern(3)
        self.assertEqual(first, again)
        self.assertNotEqual(first['rows'], pu.embroidery_pattern(4)['rows'])
        rows = first['rows']
        self.assertEqual(len(rows), pu.SIZE)
        self.assertEqual(first['total'], sum(c != '.' for row in rows for c in row))
        self.assertEqual(rows, rows[::-1])
        self.assertTrue(all(row == row[::-1] for row in rows))

    def test_stitches_follow_the_state(self):
        row = embroidery_row()
        plan = pu.plan_embroidery(row)
        self.assertEqual(plan.kind, 'started')
        n = plan.diff['stitches']
        self.assertTrue(pu.STITCHES[0] <= n <= pu.STITCHES[1])
        self.assertEqual(plan.state['stitched'], n)
        self.assertEqual(plan.step_key, '7:0')
        self.assertEqual(pu.plan_embroidery(row).diff, plan.diff)  # a retried step is the same step
        total = row['state']['pattern']['total']
        last = pu.plan_embroidery(embroidery_row(stitched=total - 3, steps=20))
        self.assertEqual((last.kind, last.close, last.diff['stitches']), ('finished', 'finished', 3))
        self.assertIn('закончила', last.facts['итог'])
        self.assertEqual(last.follow['state']['pattern']['number'], 2)

    def test_restoration_moves_by_stages_only(self):
        row, kinds, progress = restoration_row(), [], []
        while True:
            plan = pu.plan_restoration(row)
            kinds.append(plan.kind)
            progress.append(plan.progress)
            if plan.close:
                break
            row = dict(row, state=plan.state, progress=plan.progress)
        record = row['state']['record']
        self.assertEqual(len(kinds), sum(record['stage_steps']))
        self.assertEqual((kinds[0], kinds[-1]), ('started', 'finished'))
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(progress[-1], 1)
        self.assertEqual(plan.follow['title'], 'Запись № 2')
        self.assertIn('№ 1', plan.facts['итог'])

    def test_one_to_three_steps_a_day_in_waking_hours(self):
        quotas = {pu.daily_quota((AT + timedelta(days=d)).date()) for d in range(60)}
        self.assertEqual(quotas, {1, 2, 3})
        for hour in range(24):
            for n in range(5):
                at = pu.next_time(AT.replace(hour=hour), 4, 16, 'test', n)
                local = at.astimezone(pu.LIFE_TZ)
                self.assertTrue(pu.WAKING[0] <= local.hour < pu.WAKING[1], local)
                self.assertGreater(at, AT.replace(hour=hour))

    def test_tea_verdicts_come_from_reading_notes(self):
        plan = tea_plan()
        self.assertEqual((plan.kind, plan.taste['item'], plan.taste['verdict']), ('verdict', 'бергамот', 'да'))
        self.assertEqual(plan.taste['source'], 'https://example.org/tea')
        self.assertEqual(plan.taste['basis'], 'Бергамот: как его добавляют в чай')
        again = tea_plan('Чернослив в чайных смесях', [dict(item='чернослив', verdict='спорно', version=1)])
        self.assertEqual(again.kind, 'verdict_change')
        self.assertIn(again.taste['verdict'], ('да', 'нет'))
        self.assertEqual(again.taste['version'], 2)
        self.assertIsNone(tea_plan('История фарфора'))


@patch.object(pu, 'CHANGE_CHANCE', 0)
class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.plan = pu.plan_embroidery(embroidery_row(stitched=100, steps=2))
        self.n, self.done = self.plan.diff['stitches'], self.plan.state['stitched']
        self.total = self.plan.diff['total']

    def reject(self, summary, category, plan=None, feeling='спокойно'):
        with self.assertRaises(ValueError) as caught:
            pu.validate(answer(summary, feeling), plan or self.plan)
        self.assertEqual(str(caught.exception), category, summary)

    def test_in_character_words_and_bodily_verbs_are_accepted(self):
        words = pu.validate(answer(f'Вдела нитку, держала иглу ровно и вышила ещё {self.n} стежков: '
                                   f'теперь {self.done} из {self.total}, «Ветка бергамота» проступает.',
                                   'тихая радость.'), self.plan)
        self.assertEqual(words['feeling'], 'тихая радость')
        fenced = '```json\n' + answer(f'Вышила ещё {self.n} стежков, узор держится.') + '\n```'
        self.assertIn('узор', pu.validate(fenced, self.plan)['summary'])
        tea = tea_plan()
        self.assertTrue(pu.validate(answer('Заварила бергамот, отпила и сразу поняла: мой.', 'довольна'), tea))

    def test_strict_json_only(self):
        for text in ('Вышила ещё немного.', '{"summary": "Вышила ещё немного, и хорошо."}',
                     answer('Вышила ещё немного стежков.')[:-1] + ', "mood": "x"}',
                     'Вот: ' + answer('Вышила ещё немного стежков.'), '[]', None):
            with self.subTest(text=text), self.assertRaises(ValueError):
                pu.validate(text, self.plan)
        self.reject('Коротко.', 'summary_length')
        self.reject('Вышила ещё немного, и всё на своём месте.', 'feeling_shape', feeling='очень-очень сильно и долго рада')

    def test_invented_progress_is_rejected(self):
        self.reject('Вышила ещё 500 стежков за вечер.', 'invented_number')
        self.reject(f'Вышила ещё {self.n} стежков, а всего их 2000.', 'invented_number')
        self.reject('Вышила ещё тысячу стежков за вечер.', 'invented_number')
        self.reject(f'Вышила ещё {self.n} стежков и закончила узор.', 'invented_progress')
        self.reject(f'Вышила ещё {self.n} стежков, а потом сменила узор.', 'invented_progress')
        self.reject(f'Вышила ещё {self.n} стежков — почти всё готово.', 'invented_progress')
        self.reject(f'Вышила ещё {self.n} стежков и заварила мяту.', 'off_state')
        cleaning = pu.plan_restoration(restoration_row(stage=1, steps=1))
        self.reject('Убрала щелчки и восстановила пропуски.', 'invented_progress', cleaning)
        self.reject('Убрала щелчки, теперь сверила всё с черновиком.', 'invented_progress', cleaning)
        tea = tea_plan()
        self.reject('Заварила чай и задумалась о вкусе.', 'off_state', tea)
        self.reject('Попробовала бергамот: не мой, совсем не по вкусу.', 'verdict_mismatch', tea)

    def test_banned_claims_are_rejected(self):
        n = self.n
        for summary, category in (
                (f'Вышила ещё {n} стежков, пальцам больно.', 'banned_pain'),
                (f'Вышила ещё {n} стежков и очень устала.', 'banned_pain'),
                (f'Вышила ещё {n} стежков, исколола палец до крови.', 'banned_pain'),
                (f'Вышила ещё {n} стежков; пришлю фото готового.', 'banned_promise'),
                (f'Вышила ещё {n} стежков, покажу при встрече.', 'banned_promise'),
                (f'Вышила ещё {n} стежков для тебя.', 'banned_address'),
                (f'Вышила ещё {n} стежков, пока за окном шёл дождь.', 'banned_weather'),
                (f'Вышила ещё {n} стежков под новости по радио.', 'banned_news'),
                (f'Купила нитки в магазине и вышила ещё {n} стежков.', 'banned_place'),
                (f'В понедельник вышила ещё {n} стежков.', 'banned_date'),
                (f'Вышила ещё {n} стежков, а завтра продолжу.', 'banned_date'),
                (f'Вышила ещё {n} стежков, Александр одобрил.', 'banned_person'),
                (f'Вышила ещё {n} стежков, хоть депрессия и мешает.', 'banned_sensitive'),
                (f'Вышила ещё {n} стежков — неплохо для ИИ.', 'banned_meta'),
                (f'Вышила ещё {n} стежков по схеме из Pinterest.', 'banned_latin'),
                (f'Вышила ещё {n} стежков вместе с Мариной.', 'invented_name')):
            with self.subTest(summary=summary):
                self.reject(summary, category)


def life_fixture(now, long=False):
    pad = ' Подробность за подробностью, ровно и терпеливо, ряд за рядом.' * 2 if long else ''
    emb = dict(embroidery_row(stitched=470, steps=9, replaced=dict(pursuit_id=1, number=1, motif='Клевер',
                                                                     stitched=180, total=760, reason='colors')),
               title='Вышивка «Ветка бергамота»')
    rest = restoration_row(stage=1, stage_step=0, steps=1)
    tea = dict(pu.initial('tea'), id=9, status='paused', next_step_at=None, progress=.1)
    return {'pursuits': [emb, rest, tea],
            'events': {'embroidery': dict(kind='step', summary='Вышила ещё 52 стежка.' + pad, feeling='тихая радость',
                                          created_at=now - timedelta(hours=2), pursuit_title=emb['title']),
                       'restoration': dict(kind='started', summary='Прослушала запись целиком.' + pad,
                                           feeling='сосредоточенность', created_at=now - timedelta(days=1),
                                           pursuit_title='Запись № 1'),
                       'tea': dict(kind='verdict_change', summary='Чернослив — ещё раз.' + pad,
                                   feeling='уверенность', created_at=now - timedelta(days=3),
                                   pursuit_title='Вкусы в чае')},
            'tastes': [dict(item='чернослив', verdict='нет', version=2, previous='спорно'),
                       dict(item='бергамот', verdict='да', version=1, previous=None)]}


class BlockTests(unittest.TestCase):
    def test_block_has_the_exact_heading_progress_and_intentions(self):
        block = pu.pursuits_block(life_fixture(AT), AT)
        lines = block.split('\n')
        self.assertEqual(lines[0], '[Твоя жизнь в образе — художественная, но последовательная. Держись этих '
                                   'событий; сверх них ничего не было. Это не факты о мире.]')
        self.assertEqual(len(lines), 4)
        self.assertLessEqual(len('\n'.join(lines[1:]).encode()), pu.BLOCK_BYTES)
        self.assertTrue(lines[1].startswith('• Вышивка «Ветка бергамота»: больше половины, 470 из'))
        self.assertIn('прежний узор «Клевер» отложен', lines[1])
        self.assertIn('Сегодня: «Вышила ещё 52 стежка.» Чувство: тихая радость.', lines[1])
        self.assertIn('Намерение: вышивать дальше.', lines[1])
        self.assertIn('Вчера: «Прослушала запись целиком.»', lines[2])
        self.assertIn('этап 2 из 5 — очистка', lines[2])
        self.assertIn('Намерение: убрать щелчки и шип.', lines[2])
        self.assertIn('чернослив — нет (было «спорно»); бергамот — да', lines[3])
        self.assertIn('3 дня назад', lines[3])
        self.assertTrue(lines[3].endswith('Отложено.'))

    def test_budget_keeps_progress_and_drops_older_words(self):
        block = pu.pursuits_block(life_fixture(AT, long=True), AT)
        lines = block.split('\n')
        self.assertEqual(len(lines), 4)
        self.assertLessEqual(len('\n'.join(lines[1:]).encode()), pu.BLOCK_BYTES)
        self.assertIn('Сегодня: «Вышила ещё 52 стежка.', block)
        self.assertNotIn('Чернослив — ещё раз', block)
        self.assertIn('Вкусы в чае:', block)

    def test_no_life_no_block(self):
        self.assertEqual(pu.pursuits_block({}, AT), '')
        self.assertEqual(pu.pursuits_block({'pursuits': []}, AT), '')


class FailingPool:
    def acquire(self):
        raise ConnectionError('no database in this test')


class PromptInjectionTests(unittest.IsolatedAsyncioTestCase):
    """The block reaches the reply prompt after the reading block, never in RP."""

    async def generate(self, life=None, slow=False, **kwargs):
        from ai import generation
        from cognition.runtime import CURRENT_TURN
        from tests.cognition.test_humanlike_voice import plan
        from tests.support.model_connections import configured_models, model_row
        captured = []

        def handler(request):
            captured.append(request)
            return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 1,
                'model': 'upstream', 'choices': [{'index': 0, 'message': {'role': 'assistant',
                'content': 'Ответ'}, 'finish_reason': 'stop'}]})

        async def recent_life(pool, now):
            if slow:
                await asyncio.sleep(2)
            return life if life is not None else life_fixture(now)

        note = dict(title='Пуэр', gist='О выдержке.', opinion='Красиво.', url='https://example.org/p',
                    created_at=AT - timedelta(hours=1))
        turn = NS(event=NS(context=NS(chat_id=1, topic_id=-1), audience=NS(kind='private'),
                           evidence=NS(owner_id=1), occurred_at=AT),
                  runtime=NS(pool=FailingPool(), clock=lambda: AT), uses_cognition=False, active=False,
                  expression=None, context_id=1, event_id=1)
        token = CURRENT_TURN.set(turn)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with configured_models(model_row('gemini')), \
                    patch.object(generation, 'provider_http_client', return_value=http), \
                    patch('cognition.reading.recent_notes', AsyncMock(return_value=[note])), \
                    patch('cognition.life_settings.mentions_enabled', AsyncMock(return_value=True)), \
                    patch('cognition.pursuits.recent_life', recent_life):
                result = await generation.generate_response_stream(1, 'Привет', 'User', '', request_intent={},
                                                                   expression_plan=plan(), **kwargs)
        finally:
            CURRENT_TURN.reset(token)
        self.assertEqual(result[0], 'Ответ')
        return json.loads(captured[0].content)['messages'][0]['content']

    async def test_block_follows_reading_with_the_exact_heading(self):
        system = await self.generate()
        self.assertIn('\n\n' + pu.HEADING + '\n• ', system)
        markers = ('[Сейчас]', '[Что ты недавно читала', pu.HEADING, '[Голос]')
        positions = [system.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('Вышила ещё 52 стежка.', system)

    async def test_not_in_roleplay_disabled_or_slow(self):
        self.assertNotIn(pu.HEADING, await self.generate(is_rp_mode=True))
        with patch.dict(os.environ, {'ARTI_PURSUITS_ENABLED': '0'}):
            self.assertNotIn(pu.HEADING, await self.generate())
        self.assertNotIn(pu.HEADING, await self.generate(slow=True))
        self.assertNotIn(pu.HEADING, await self.generate(life={}))


class PersonDataTests(unittest.TestCase):
    def test_the_service_never_names_person_tables(self):
        source = (ROOT / 'cognition/pursuits.py').read_text(encoding='utf-8')
        for name in ('cognitive_', 'memory_facts', 'memory_messages', 'memory_user', 'owner_id', 'chat_id',
                     'user_id', 'context_id', 'chat_history', 'CURRENT_TURN'):
            self.assertNotIn(name, source)
        tables = set(re.findall(r'(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_]+)', source))
        self.assertLessEqual(tables, TABLES)

    def test_schema_has_no_person_columns(self):
        sql = (ROOT / 'cognition/migrations/059_arti_pursuits.sql').read_text(encoding='utf-8')
        sql = re.sub(r'--[^\n]*', '', sql)
        self.assertNotRegex(sql, r'owner|chat|user|context|person|telegram')
        self.assertNotIn('cognitive_', sql)

    def test_the_model_gets_only_the_steps_facts(self):
        calls = []

        async def complete(self, model, messages, tokens, temperature=0):
            calls.append((model, messages))
            return httpx.Response(200, json={'choices': [{'message': {'content': answer('Вышила ещё немного.')}}]})

        plan = pu.plan_embroidery(embroidery_row())
        with patch('ai.providers.structured.SelectedModelClient.complete', complete), \
                patch.dict(os.environ, {'ARTI_PURSUITS_MODEL': 'tea-model'}):
            text = asyncio.run(pu.write_with_model(FailingPool(), pu.SYSTEM, plan.facts))
        self.assertIn('Вышила', text)
        model, messages = calls[0]
        self.assertEqual(model, 'tea-model')
        self.assertEqual([m['role'] for m in messages], ['system', 'user'])
        self.assertEqual(messages[0]['content'], pu.SYSTEM)
        self.assertEqual(json.loads(messages[1]['content']), plan.facts)


class Recorder:
    """Pool wrapper that records every statement the service sends."""

    def __init__(self, pool):
        self.pool, self.sql = pool, []

    def acquire(self):
        return _Acquire(self)

    async def close(self):
        pass


class _Acquire:
    def __init__(self, recorder):
        self.recorder = recorder

    async def __aenter__(self):
        self.inner = self.recorder.pool.acquire()
        return _Conn(await self.inner.__aenter__(), self.recorder.sql)

    async def __aexit__(self, *exc):
        return await self.inner.__aexit__(*exc)


class _Conn:
    def __init__(self, conn, sql):
        self.conn, self.sql = conn, sql

    def __getattr__(self, name):
        attr = getattr(self.conn, name)
        if name not in ('fetch', 'fetchrow', 'fetchval', 'execute'):
            return attr

        async def call(query, *args, **kwargs):
            self.sql.append(query)
            return await attr(query, *args, **kwargs)
        return call


HOLD_LOCK = '''
import asyncio, os, sys
import asyncpg
from dotenv import dotenv_values
async def main():
    cfg = {**dotenv_values('.env'), **os.environ}
    conn = await asyncpg.connect(host=cfg.get('DB_HOST', 'localhost'), port=int(cfg.get('DB_PORT', 5432)),
                                 user=cfg.get('DB_USER', 'postgres'), password=cfg.get('DB_PASSWORD', ''),
                                 database=sys.argv[1])
    assert await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended('arti:pursuits',0))")
    print('locked', flush=True)
    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
    await conn.close()
asyncio.run(main())
'''


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class PursuitServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.repositories import ensure_schema
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        await ensure_schema(self.pool)
        self.at = AT
        self.runtime = NS(pool=self.pool, clock=lambda: self.at, mode='active')
        self.calls = []
        self.service = pu.Pursuits(self.runtime, self.write)
        self.writer = words_for

    async def asyncTearDown(self):
        await self.db.__aexit__(None, None, None)

    async def write(self, system, facts):
        self.calls.append(facts)
        return self.writer(facts)

    async def rows(self, query, *args):
        async with self.pool.acquire() as conn:
            return [dict(r) for r in await conn.fetch(query, *args)]

    async def note(self, title):
        # One tea note per reading day; each earlier day is a fresh one.
        self.notes = getattr(self, 'notes', 0) + 1
        async with self.pool.acquire() as conn:
            return await conn.fetchval('''INSERT INTO arti_reading_notes(read_on,topic,title,url,gist,opinion,created_at)
                VALUES($1,'чай',$2,'https://example.org/'||md5($2),'О чае.','Интересно.',$3) RETURNING id''',
                (self.at - timedelta(days=self.notes)).date(), title, self.at - timedelta(minutes=60 - self.notes))

    async def test_schedule_bootstraps_then_keeps_quota_and_hours(self):
        self.assertEqual((await self.service.tick())['outcome'], 'idle')  # started, nothing due yet
        live = await self.rows('SELECT kind,title,status,next_step_at FROM arti_pursuits ORDER BY kind')
        self.assertEqual([r['kind'] for r in live], ['embroidery', 'restoration', 'tea'])
        self.assertEqual({r['status'] for r in live}, {'active'})
        self.assertEqual(self.calls, [])
        self.at = max(r['next_step_at'] for r in live if r['kind'] != 'tea') + timedelta(minutes=1)
        with patch.object(pu, 'daily_quota', lambda day: 1):
            first = await self.service.tick()
            self.assertEqual(first['outcome'], 'stored')
            self.assertEqual((await self.service.tick())['outcome'], 'quota')
        self.assertEqual(len(self.calls), 1)
        self.at = self.at.replace(hour=21, minute=30)  # 00:30 on her clock
        self.assertEqual((await self.service.tick())['outcome'], 'asleep')
        with patch.dict(os.environ, {'ARTI_PURSUITS_ENABLED': '0'}):
            self.assertEqual((await self.service.tick())['outcome'], 'disabled')
        self.assertEqual(len(self.calls), 1)

    async def test_failed_and_invented_steps_are_retried_unchanged(self):
        def failing(facts):
            raise RuntimeError('provider down')
        self.writer = failing
        failed = await self.service.tick(kind='restoration', manual=True)
        self.assertEqual(failed['outcome'], 'failed')
        row = (await self.rows("SELECT * FROM arti_pursuits WHERE kind='restoration'"))[0]
        self.assertEqual(row['next_step_at'], self.at + pu.RETRY)
        self.writer = lambda facts: answer('Прослушала запись: 999 щелчков и законченная работа.')
        invalid = await self.service.tick(kind='restoration', manual=True)
        self.assertEqual(invalid['outcome'], 'invalid')
        self.assertEqual(await self.rows('SELECT id FROM arti_pursuit_events'), [])
        self.at += pu.RETRY
        self.writer = words_for
        stored = await self.service.tick()
        self.assertEqual(stored['outcome'], 'stored')
        self.assertEqual(stored['step_key'], failed['step_key'])
        self.assertEqual(stored['facts'], failed['facts'])
        runs = await self.rows('SELECT outcome,error,model_calls,step_key FROM arti_pursuit_runs ORDER BY id')
        self.assertEqual([(r['outcome'], r['error']) for r in runs],
                         [('failed', 'RuntimeError'), ('invalid', 'invented_number'), ('stored', None)])
        self.assertEqual({r['step_key'] for r in runs}, {failed['step_key']})
        event = (await self.rows('SELECT * FROM arti_pursuit_events'))[0]
        self.assertEqual((event['kind'], event['step_key']), ('started', failed['step_key']))
        self.assertIn('щелч', event['summary'])

    async def test_daily_model_budget(self):
        def failing(facts):
            raise RuntimeError('provider down')
        self.writer = failing
        outcomes = [(await self.service.tick(manual=True))['outcome'] for _ in range(pu.MAX_CALLS_PER_DAY + 1)]
        self.assertEqual(outcomes, ['failed'] * pu.MAX_CALLS_PER_DAY + ['budget'])
        self.assertEqual(len(self.calls), pu.MAX_CALLS_PER_DAY)
        self.at += timedelta(days=1)
        self.assertEqual((await self.service.tick(manual=True))['outcome'], 'failed')

    @patch.object(pu, 'MAX_CALLS_PER_DAY', 10)
    async def test_change_of_pattern_keeps_the_old_one(self):
        with patch.object(pu, 'CHANGE_CHANCE', 0):
            for _ in range(4):
                self.assertEqual((await self.service.tick(kind='embroidery', manual=True))['outcome'], 'stored')
        old = (await self.rows("SELECT * FROM arti_pursuits WHERE kind='embroidery'"))[0]
        stitched = json.loads(old['state'])['stitched']
        with patch.object(pu, 'CHANGE_CHANCE', 1):
            change = await self.service.tick(kind='embroidery', manual=True)
            self.assertEqual((change['outcome'], change['kind']), ('stored', 'pattern_change'))
            after = await self.service.tick(kind='embroidery', manual=True)
            self.assertEqual(after['kind'], 'started')  # a replacement is not changed again
        rows = await self.rows("SELECT * FROM arti_pursuits WHERE kind='embroidery' ORDER BY id")
        self.assertEqual([r['status'] for r in rows], ['abandoned', 'active'])
        self.assertIsNotNone(rows[0]['finished_at'])
        kept = json.loads(rows[0]['state'])
        self.assertEqual((kept['stitched'], kept['pattern']['number']), (stitched, 1))
        replaced = json.loads(rows[1]['state'])['replaced']
        self.assertEqual((replaced['pursuit_id'], replaced['stitched'], replaced['number']), (old['id'], stitched, 1))
        events = await self.rows('SELECT pursuit_id,kind,diff FROM arti_pursuit_events ORDER BY id')
        self.assertEqual([(e['pursuit_id'], e['kind']) for e in events],
                         [(old['id'], 'started')] + [(old['id'], 'step')] * 3
                         + [(rows[1]['id'], 'pattern_change'), (rows[1]['id'], 'started')])
        diff = json.loads(events[4]['diff'])
        self.assertEqual((diff['from']['pursuit_id'], diff['from']['stitched'], diff['to']['number']),
                         (old['id'], stitched, 2))
        block = pu.pursuits_block(await pu.recent_life(self.pool, self.at), self.at)
        self.assertIn(f'прежний узор «{kept["pattern"]["motif"]}» отложен', block)

    async def test_change_of_verdict_keeps_history(self):
        await self.note('Бергамот: история одного аромата')
        await self.note('История фарфоровых чашек')
        await self.note('Чернослив в чайных смесях')
        first = await self.service.tick(kind='tea', manual=True)
        self.assertEqual((first['outcome'], first['kind']), ('stored', 'verdict'))
        second = await self.service.tick(kind='tea', manual=True)  # the note on porcelain names no taste
        self.assertEqual(second['kind'], 'verdict')
        self.assertEqual(len(self.calls), 2)
        self.at += timedelta(days=4)
        await self.note('Чернослив и копчёный чай')
        third = await self.service.tick(kind='tea', manual=True)
        self.assertEqual((third['outcome'], third['kind']), ('stored', 'verdict_change'))
        self.assertEqual((await self.service.tick(kind='tea', manual=True))['outcome'], 'idle')
        tastes = await self.rows('SELECT item,verdict,version,superseded_at,reason,basis,source,event_id '
                                 'FROM arti_tastes ORDER BY item,version')
        self.assertEqual([(t['item'], t['version']) for t in tastes],
                         [('бергамот', 1), ('чернослив', 1), ('чернослив', 2)])
        self.assertEqual(tastes[0]['verdict'], 'да')
        self.assertEqual(tastes[1]['verdict'], 'спорно')
        self.assertIsNotNone(tastes[1]['superseded_at'])
        self.assertIn(tastes[2]['verdict'], ('да', 'нет'))
        self.assertIsNone(tastes[2]['superseded_at'])
        self.assertEqual(tastes[2]['basis'], 'Чернослив и копчёный чай')
        self.assertTrue(tastes[2]['source'].startswith('https://example.org/'))
        self.assertIn('чернослив', tastes[2]['reason'])
        self.assertTrue(all(t['event_id'] for t in tastes))
        block = pu.pursuits_block(await pu.recent_life(self.pool, self.at), self.at)
        self.assertIn(f'чернослив — {tastes[2]["verdict"]} (было «спорно»)', block)

    async def test_no_person_data_is_read_or_sent(self):
        recorder = Recorder(self.pool)
        self.runtime.pool = recorder
        await self.note('Бергамот: история одного аромата')
        for kind in pu.KINDS:
            await self.service.tick(kind=kind, manual=True)
        await pu.recent_life(recorder, self.at)
        await pu.status(recorder, self.at)
        await pu.set_paused(recorder, True, None, self.at)
        used = {t for query in recorder.sql for t in re.findall(r'(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_]+)', query)}
        self.assertTrue(used)
        self.assertLessEqual(used, TABLES)
        keys = {k for facts in self.calls for k in facts}
        self.assertLessEqual(keys, {'занятие', 'узор', 'событие', 'всего', 'готово', 'итог', 'прежний узор', 'причина',
                                    'новый узор', 'запись', 'этап', 'работа', 'прочитано по-настоящему', 'что',
                                    'вердикт', 'распробовано', 'прежний вердикт'})
        columns = await self.rows('''SELECT table_name,column_name FROM information_schema.columns
            WHERE table_name IN ('arti_pursuits','arti_pursuit_events','arti_tastes','arti_pursuit_runs')''')
        self.assertFalse([c for c in columns if re.search(r'owner|chat|user|context|person', c['column_name'])])

    async def test_lock_holds_across_processes(self):
        database = await self.pool.fetchval('SELECT current_database()')
        holder = await asyncio.create_subprocess_exec(sys.executable, '-c', HOLD_LOCK, database, cwd=str(ROOT),
                                                      stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        try:
            self.assertEqual((await asyncio.wait_for(holder.stdout.readline(), 30)).strip(), b'locked')
            self.assertEqual((await self.service.tick(manual=True))['outcome'], 'locked')
            self.assertEqual(await self.rows('SELECT id FROM arti_pursuits'), [])
        finally:
            holder.stdin.write(b'\n')
            await holder.stdin.drain()
            await asyncio.wait_for(holder.wait(), 30)
        self.assertEqual(holder.returncode, 0)
        self.assertEqual(self.calls, [])
        # Inside one process, a second connection is turned away while a step is under way.
        release = asyncio.Event()

        async def slow(system, facts):
            await release.wait()
            return words_for(facts)
        busy = pu.Pursuits(self.runtime, slow)
        running = asyncio.create_task(busy.tick(kind='embroidery', manual=True))
        await asyncio.sleep(.3)
        self.assertEqual((await self.service.tick(manual=True))['outcome'], 'locked')
        release.set()
        self.assertEqual((await running)['outcome'], 'stored')
        self.assertEqual((await self.service.tick(kind='embroidery', manual=True))['outcome'], 'stored')

    async def test_pause_resume_status_and_dry_run(self):
        dry = await self.service.tick(manual=True, dry_run=True, use_model=False)
        self.assertEqual((dry['outcome'], dry['model_calls']), ('dry_run', 0))
        self.assertEqual(await self.rows('SELECT id FROM arti_pursuits'), [])  # a dry run writes nothing
        await self.service.tick(kind='embroidery', manual=True)
        paused = await pu.set_paused(self.pool, True, 'embroidery', self.at)
        self.assertEqual([r['status'] for r in paused['changed']], ['paused'])
        self.assertEqual((await self.service.tick(kind='embroidery', manual=True))['outcome'], 'idle')
        block = pu.pursuits_block(await pu.recent_life(self.pool, self.at), self.at)
        self.assertRegex(block, r'• Вышивка «[^»]+»: [^\n]*Отложено\.')
        before = await self.rows('SELECT state FROM arti_pursuits ORDER BY id')
        trial = await self.service.tick(kind='restoration', manual=True, dry_run=True)
        self.assertEqual((trial['outcome'], trial['model_calls']), ('dry_run', 1))
        self.assertIn('summary', trial)
        self.assertEqual(await self.rows('SELECT state FROM arti_pursuits ORDER BY id'), before)
        self.assertEqual(len(await self.rows('SELECT id FROM arti_pursuit_events')), 1)
        resumed = await pu.set_paused(self.pool, False, None, self.at)
        self.assertEqual([r['status'] for r in resumed['changed']], ['active'])
        report = await pu.status(self.pool, self.at)
        json.dumps(report)
        self.assertEqual(report['today']['model_calls'], 2)
        self.assertEqual(report['today']['runs'], {'stored': 1, 'dry_run': 1})
        self.assertEqual(len(report['events']), 1)

    async def test_admin_commands_dispatch(self):
        from tools import cognition_admin
        borrowed = Recorder(self.pool)
        with patch.object(cognition_admin.asyncpg, 'create_pool', AsyncMock(return_value=borrowed)):
            step = await cognition_admin.run(NS(command='pursuit-step', kind='tea', dry_run=True, no_model=True))
            self.assertEqual(step['outcome'], 'idle')
            await self.service.tick(kind='restoration', manual=True)
            paused = await cognition_admin.run(NS(command='pursuit-pause', kind=None))
            self.assertEqual(len(paused['changed']), 3)
            status = await cognition_admin.run(NS(command='pursuits-status'))
            self.assertEqual({p['status'] for p in status['pursuits']}, {'paused'})
            resumed = await cognition_admin.run(NS(command='pursuit-resume', kind='tea'))
            self.assertEqual([r['kind'] for r in resumed['changed']], ['tea'])
            json.dumps(status, ensure_ascii=True)


if __name__ == '__main__':
    unittest.main()
