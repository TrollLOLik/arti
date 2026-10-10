"""H4c «Своя жизнь: проактивность» (I1, I3, I6, I7).

Synthetic data only: recorded interpreter, fake search and transport, disposable PostgreSQL.
"""
import json
import os
import re
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cognition import initiatives as ini
from cognition import reading as rd
from cognition.types import AudienceScope, Origin

AT = datetime(2026, 10, 5, 12, 40, tzinfo=timezone.utc)


class AnswerTests(unittest.TestCase):
    def test_clear_consent_answers(self):
        for text in ('да', 'Да!', 'конечно', 'можно)', 'давай', 'пиши', 'да, конечно', 'ок', 'буду рада'):
            self.assertIs(ini.consent_answer(text), True, text)
        for text in ('нет', 'не надо', 'лучше не', 'нет, спасибо', 'не пиши', 'стоп'):
            self.assertIs(ini.consent_answer(text), False, text)
        for text in ('а что ты думаешь о погоде', 'да, а ещё у меня новость: я переезжаю в другой город на следующей неделе', '', None):
            self.assertIsNone(ini.consent_answer(text), text)

    def test_stop_requests(self):
        for text in ('стоп', 'Хватит писать!', 'не пиши мне первой', 'больше не пиши', 'не надо'):
            self.assertTrue(ini.stop_request(text), text)
        for text in ('стоп, я не о том', 'давай дальше'):
            self.assertFalse(ini.stop_request(text), text)

    def test_local_time_becomes_an_offset(self):
        self.assertEqual(ini.parse_local_time('15:40', AT), 180)
        self.assertEqual(ini.parse_local_time('у нас 9 вечера', AT.replace(hour=18, minute=0)), 180)
        self.assertEqual(ini.parse_local_time('полночь', AT.replace(hour=21, minute=0)), 180)
        self.assertEqual(ini.parse_local_time('7.40', AT), -300)
        self.assertEqual(ini.parse_local_time('16:45', AT), 240)  # rounded to a quarter hour
        for text in ('не знаю', '25:00', 'около обеда где-то, может быть, точно не скажу', ''):
            self.assertIsNone(ini.parse_local_time(text, AT), text)

    def test_texts_are_in_character_without_pressure(self):
        self.assertTrue(all(ini.CONSENT_MARK in v for v in ini.CONSENT_VARIANTS))
        self.assertTrue(all(ini.TIME_MARK in v for v in ini.TIME_VARIANTS))
        self.assertIn(ini.question_text('consent', 'e1'), ini.CONSENT_VARIANTS)
        self.assertIn(ini.question_text('time', 'e1'), ini.TIME_VARIANTS)
        pressure = re.compile(r'почему (ты )?не пиш|скучал|обижа|последн|\bсрочно\b|жду ответ|ты пропал', re.I)
        for text in ini.RETURN_VARIANTS:
            self.assertNotRegex(text, pressure)
            self.assertNotIn('?', text)

    def test_shared_reading_names_its_source(self):
        note = dict(title='Пуэр: как стареет чай', opinion='Мне нравится, что время здесь — ингредиент.',
                    url='https://example.org/puer')
        self.assertIn('по твоей части: https://example.org/puer', ini.share_text(note, True))
        self.assertTrue(ini.share_text(note, False).endswith('https://example.org/puer'))
        self.assertNotIn('по твоей части', ini.share_text(note, False))


class ReadingParseTests(unittest.TestCase):
    def test_two_different_interests_per_day(self):
        for day in (date(2026, 10, d) for d in range(1, 15)):
            topics = rd.topics_for(day)
            self.assertEqual(len(topics), rd.READS_PER_DAY)
            self.assertEqual(len({t for t, _ in topics}), rd.READS_PER_DAY)
            self.assertEqual(topics, rd.topics_for(day))
        seen = {t for d in range(1, 9) for t, _ in rd.topics_for(date(2026, 10, d))}
        self.assertEqual(seen, {t for t, _ in rd.INTERESTS})

    def test_a_note_needs_a_verified_link(self):
        body = '```json\n{"title": "Пуэр", "gist": "О выдержке чая.", "opinion": "Красиво."}\n```'
        note = rd.parse_note(body, [('https://example.org/a', 'Example')])
        self.assertEqual((note['title'], note['url']), ('Пуэр', 'https://example.org/a'))
        with self.assertRaises(ValueError):
            rd.parse_note(body, [])
        with self.assertRaises(ValueError):
            rd.parse_note(body, [('javascript:alert(1)', 'x')])
        with self.assertRaises(ValueError):
            rd.parse_note('{"title": "x", "gist": "y"}', [('https://example.org/a', '')])
        with self.assertRaises(ValueError):
            rd.parse_note(json.dumps({'title': 'x' * 301, 'gist': 'y', 'opinion': 'z'}), [('https://example.org/a', '')])

    def test_reading_block_is_true_and_sourced(self):
        notes = [dict(title='Пуэр', gist='О выдержке чая.', opinion='Красиво.', url='https://example.org/a', created_at=AT)]
        block = rd.reading_block(notes, AT + timedelta(days=1))
        self.assertIn('Что ты недавно читала', block)
        self.assertIn('вчера: «Пуэр»', block)
        self.assertIn('https://example.org/a', block)
        self.assertEqual(rd.reading_block([], AT), '')


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class InitiativeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from bot.request_runtime import CURRENT_REQUEST
        from cognition.runtime import CognitiveRuntime, CURRENT_TURN
        from cognition.scope import CURRENT_SCOPE
        from tests.cognition.test_full_model import RecordedInterpreter
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.tokens = [(v, v.set(None)) for v in (CURRENT_TURN, CURRENT_SCOPE, CURRENT_REQUEST)]
        self.at = AT - timedelta(days=2)
        self.interpreter = RecordedInterpreter()
        self.runtime = await CognitiveRuntime(self.pool, self.interpreter, 'active', clock=lambda: self.at).initialize(False)
        self.mid = 0
        self.sent = []
        async def send_message(**kwargs):
            self.sent.append(kwargs['text'])
            return NS(message_id=5000 + len(self.sent), chat=NS(id=kwargs['chat_id']), text=kwargs['text'])
        self.bot = NS(send_message=send_message)
        async with self.pool.acquire() as conn:
            await conn.execute('INSERT INTO response_status(chat_id,enabled) VALUES(1,true) ON CONFLICT DO NOTHING')

    async def asyncTearDown(self):
        await self.runtime.close()
        for var, token in reversed(self.tokens):
            var.reset(token)
        await self.db.__aexit__(None, None, None)

    async def say(self, text, origin=Origin.USER, **frame):
        from tests.cognition.test_full_model import situation
        self.mid += 1
        cid, eid, ev = await self.runtime.ingest(1, 1, text, self.mid, origin=origin, audience=AudienceScope('private', 1))
        if frame:
            self.interpreter.frames[text] = situation(ev, **frame)
        await self.runtime.process(cid, eid)
        self.at += timedelta(minutes=1)
        return cid, eid, ev

    async def acquaintance(self):
        cid = None
        for day in range(2):
            for n in range(5):
                cid, _, _ = await self.say(f'Сообщение {day}-{n}: рассказываю про день')
            self.at += timedelta(days=1)
        return cid

    async def light_turn(self, text='Как настроение?'):
        self.mid += 1
        turn = await self.runtime.prepare(1, 1, text, self.mid)
        self.at += timedelta(minutes=1)
        return turn

    async def permission(self, cid):
        async with self.pool.acquire() as conn:
            return await ini.permission(conn, cid, 1)

    async def reason(self, cid):
        from cognition.initiative_policy import private_reason
        async with self.pool.acquire() as conn:
            return await private_reason(conn, 1, self.at, context_id=cid)

    async def test_consent_is_asked_once_in_character_and_a_clear_yes_is_kept(self):
        few, _, _ = await self.say('Привет')
        self.assertIsNone(await ini.question_due(self.runtime, await self.light_turn(), light=True))
        cid = await self.acquaintance()
        turn = await self.light_turn()
        self.assertIsNone(await ini.question_due(self.runtime, turn, light=False))
        self.assertEqual(await ini.question_due(self.runtime, turn, light=True), 'consent')
        await self.say(ini.question_text('consent', 'x'), origin=Origin.DELIVERED_ACTION)
        await self.say('да, конечно')
        self.assertIs(await self.permission(cid), True)
        # Consent given, local time unknown: she asks the time once.
        self.assertEqual(await ini.question_due(self.runtime, await self.light_turn(), light=True), 'time')
        self.assertEqual(await self.reason(cid), 'unknown_timezone')
        self.at = self.at.replace(hour=12, minute=40)
        await self.say(ini.question_text('time', 'x'), origin=Origin.DELIVERED_ACTION)
        await self.say('15:40')
        self.assertIsNone(await self.reason(cid))
        self.at = self.at.replace(hour=21, minute=0)  # 00:00 for the person
        self.assertEqual(await self.reason(cid), 'quiet_hours')
        self.assertIsNone(await ini.question_due(self.runtime, await self.light_turn(), light=True))
        from cognition.temporal import temporal_context
        self.assertIn('У собеседника: 00:', await temporal_context(await self.light_turn()))

    async def test_no_or_silence_is_never_consent(self):
        cid = await self.acquaintance()
        await self.say(ini.question_text('consent', 'y'), origin=Origin.DELIVERED_ACTION)
        await self.say('а что ты думаешь о погоде')
        self.assertIsNone(await self.permission(cid))
        self.assertIsNone(await ini.question_due(self.runtime, await self.light_turn(), light=True))
        await self.say('Слушай, давай без этого')
        self.assertIsNone(await self.permission(cid))
        cid2_text = ini.question_text('consent', 'z')
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_events SET suppressed_at=NOW() WHERE origin='delivered_action'")
        await self.say(cid2_text, origin=Origin.DELIVERED_ACTION)
        await self.say('нет')
        self.assertIs(await self.permission(cid), False)
        self.assertEqual(await ini.due(self.runtime), [])

    async def grant(self):
        cid = await self.acquaintance()
        await self.say(ini.question_text('consent', 'g'), origin=Origin.DELIVERED_ACTION)
        await self.say('да')
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO arti_organizer_preferences VALUES(1,'Europe/Moscow')")
        return cid

    async def test_quiet_hello_after_a_week_of_silence_then_stop(self):
        cid = await self.grant()
        self.at = self.at.replace(hour=11) + timedelta(days=3)
        self.assertEqual(await ini.due(self.runtime), [])  # too soon
        self.at += timedelta(days=5)
        found = await ini.due(self.runtime)
        self.assertEqual([c['kind'] for c in found], ['return'])
        self.assertTrue(await ini.send(self.runtime, self.bot, found[0]))
        self.assertIn(self.sent[-1], ini.RETURN_VARIANTS)
        self.at += timedelta(hours=1)
        self.assertEqual(await ini.due(self.runtime), [])  # one at a time, awaiting an answer
        await self.say('стоп')
        self.assertIs(await self.permission(cid), False)

    async def test_a_stale_occasion_is_not_sent(self):
        await self.grant()
        self.at = self.at.replace(hour=11) + timedelta(days=8)
        found = await ini.due(self.runtime)
        await self.say('О, привет, я тут')
        self.at += timedelta(hours=1)
        self.assertFalse(await ini.send(self.runtime, self.bot, found[0]))
        self.assertEqual(self.sent, [])

    async def test_shared_reading_matches_interests_and_is_sent_once(self):
        cid = await self.grant()
        await self.say('Я обожаю китайский чай пуэр', beliefs=[dict(span=0, subject=1, predicate='likes',
                       value='китайский чай пуэр', condition='', assertion='explicit', confidence=.9)])
        self.at = self.at.replace(hour=11) + timedelta(days=1)
        async with self.pool.acquire() as conn:
            await conn.execute('''INSERT INTO arti_reading_notes(read_on,topic,title,url,gist,opinion,created_at)
                VALUES($1,'чай','Как стареет китайский пуэр','https://example.org/puer','О выдержке пуэра.','Время здесь — ингредиент.',$2)''',
                self.at.date(), self.at - timedelta(hours=2))
        found = await ini.due(self.runtime)
        self.assertEqual([c['kind'] for c in found], ['reading'])
        self.assertIn('по твоей части: https://example.org/puer', found[0]['text'])
        self.assertTrue(await ini.send(self.runtime, self.bot, found[0]))
        self.at += timedelta(days=3)
        await self.say('Спасибо, прочитаю')
        self.at += timedelta(hours=2)
        self.assertEqual(await ini.due(self.runtime), [])

    async def test_no_article_after_a_recent_loss(self):
        await self.grant()
        await self.say('Мой кот умер, я пью китайский чай пуэр и плачу', kind='loss', outcome='confirmed',
                       beliefs=[dict(span=0, subject=1, predicate='likes', value='китайский чай пуэр', condition='',
                                     assertion='explicit', confidence=.9)])
        self.at = self.at.replace(hour=11) + timedelta(days=1)
        async with self.pool.acquire() as conn:
            await conn.execute('''INSERT INTO arti_reading_notes(read_on,topic,title,url,gist,opinion,created_at)
                VALUES($1,'чай','Как стареет китайский пуэр','https://example.org/puer','О выдержке пуэра.','Время здесь — ингредиент.',$2)''',
                self.at.date(), self.at - timedelta(hours=2))
        self.assertEqual(await ini.due(self.runtime), [])

    async def test_unmatched_reading_is_not_pushed_on_a_new_acquaintance(self):
        await self.grant()
        self.at = self.at.replace(hour=11) + timedelta(days=1)
        async with self.pool.acquire() as conn:
            await conn.execute('''INSERT INTO arti_reading_notes(read_on,topic,title,url,gist,opinion,created_at)
                VALUES($1,'небо','Новая карта Млечного Пути','https://example.org/sky','О звёздах.','Красиво.',$2)''',
                self.at.date(), self.at - timedelta(hours=2))
        self.assertEqual(await ini.due(self.runtime), [])

    async def outbox(self, cid, sent_at, n):
        async with self.pool.acquire() as conn:
            eid = await conn.fetchval("SELECT max(id) FROM cognitive_events WHERE context_id=$1", cid)
            await conn.execute('''INSERT INTO cognitive_outbox(context_id,event_id,delivery_key,channel,status,payload,suppression_epoch,updated_at,created_at)
                VALUES($1,$2,$3,'message','delivered','{}',0,$4,$4)''', cid, eid, f'initiative:test:{n}:message:1', sent_at)

    async def test_receptivity_widens_narrows_and_pauses(self):
        cid = await self.grant()
        now = self.at + timedelta(days=60)
        async with self.pool.acquire() as conn:
            base = await ini.receptivity(conn, cid, now)
        self.assertEqual((base['weekly_budget'], base['allowed']), (2, True))
        for n, days in enumerate((20, 10)):
            await self.outbox(cid, now - timedelta(days=days), n)
        async with self.pool.acquire() as conn:
            paused = await ini.receptivity(conn, cid, now)
        self.assertEqual(paused['ignored_in_a_row'], 2)
        self.assertEqual(paused['paused_until'], now - timedelta(days=10) + timedelta(days=14))
        self.assertFalse(paused['allowed'])
        await self.outbox(cid, now - timedelta(hours=3), 9)
        async with self.pool.acquire() as conn:
            pending = await ini.receptivity(conn, cid, now)
        self.assertTrue(pending['pending'])
        self.assertFalse(pending['allowed'])

    async def test_answered_initiatives_widen_the_budget(self):
        cid = await self.grant()
        start = self.at
        for n in range(3):
            await self.outbox(cid, self.at, n)
            self.at += timedelta(hours=1)
            await self.say(f'Ответ на инициативу {n}')
            self.at += timedelta(days=3)
        async with self.pool.acquire() as conn:
            warm = await ini.receptivity(conn, cid, self.at)
        self.assertEqual(warm['weekly_budget'], 3)
        self.assertGreater(self.at, start)


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class ReadingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from cognition.repositories import ensure_schema
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        await ensure_schema(self.pool)
        self.at = AT
        self.runtime = NS(pool=self.pool, clock=lambda: self.at, mode='active')

    async def asyncTearDown(self):
        await self.db.__aexit__(None, None, None)

    async def test_daily_reading_keeps_only_sourced_notes(self):
        calls = []
        async def search(query, system):
            calls.append(query)
            if len(calls) == 1:
                return '{"title": "Пуэр", "gist": "О выдержке.", "opinion": "Красиво."}', [('https://example.org/a', 'A')]
            return '{"title": "Без ссылки", "gist": "x", "opinion": "y"}', []
        service = rd.Reading(self.runtime, search)
        stored = await service.read_today()
        self.assertEqual([n['title'] for n in stored], ['Пуэр'])
        async def failing(query, system):
            raise RuntimeError('provider down')
        service.search = failing
        self.assertEqual(await service.read_today(), [])
        service.search = search
        calls.append('x')  # next call returns a sourced note again
        calls.clear()
        again = await service.read_today()
        self.assertEqual(len(again), 1)
        self.assertEqual(await service.read_today(), [])  # both topics read today
        notes = await rd.recent_notes(self.pool, self.at + timedelta(days=1))
        self.assertEqual(len(notes), 2)
        self.assertEqual(await rd.recent_notes(self.pool, self.at + timedelta(days=8)), [])


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class InitiativeQuestionReplyTests(unittest.IsolatedAsyncioTestCase):
    """The consent question rides after a light private reply, never after a task."""
    from tests.cognition import test_request_delivery as _fixtures
    from tests.cognition.test_humanlike_feelings import ReplyPathTests as _reply_path
    asyncSetUp = _fixtures.ActiveRequestDeliveryTests.asyncSetUp
    asyncTearDown = _fixtures.ActiveRequestDeliveryTests.asyncTearDown
    reply = _reply_path.reply

    async def test_question_follows_small_talk(self):
        from unittest.mock import patch
        due = AsyncMock(return_value='consent')
        with patch('cognition.initiatives.question_due', due):
            await self.reply('Ого!\n\nКак прошёл день?')
        texts = [c['text'] for c in self.calls]
        self.assertEqual(texts[:2], ['Ого!', 'Как прошёл день?'])
        self.assertEqual(len(texts), 3)
        self.assertIn(ini.CONSENT_MARK, texts[2])
        self.assertIs(due.await_args.kwargs['light'], True)

    async def test_no_question_after_a_task(self):
        from unittest.mock import patch
        due = AsyncMock(return_value='consent')
        with patch('cognition.initiatives.question_due', due):
            await self.reply('Вот план:\n1. Билеты\n2. Отель', text='Составь план поездки')
        self.assertEqual(len(self.calls), 1)
        due.assert_not_awaited()

    async def test_no_question_on_a_resumed_request(self):
        from unittest.mock import patch
        due = AsyncMock(return_value='consent')
        self.job['attempts'] = 2
        with patch('cognition.initiatives.question_due', due):
            await self.reply('Ого!\n\nКак прошёл день?', text='Сдала экзамен')
        self.assertEqual(len(self.calls), 2)
        due.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
