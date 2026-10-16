"""H6 (P2): «Сейчас у Арти», «Комната Арти» and the person's «Не упоминать».

Synthetic accounts and a fake model writer only; the database tests use a
disposable PostgreSQL. No provider network, Telegram or working database.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import math
import os
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import httpx
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from cognition import life_settings, pursuits
from conversations.demo import DemoRepository
from miniapp import life
from miniapp.server import Settings, create_app
from miniapp.sessions import DemoSessions, SessionStore, csrf
from tests.miniapp.test_auth import TOKEN, signed

ORIGIN = 'https://miniapp.example.test'


def fixture_life(now):
    row = dict(pursuits.initial('embroidery'), id=1, status='active', next_step_at=None)
    row['state'].update(stitched=40, steps=1)
    return {'pursuits': [row], 'events': {'embroidery': dict(kind='step', summary='Вышила ещё 40 стежков.',
            feeling='спокойно', created_at=now - timedelta(hours=1), pursuit_kind='embroidery',
            pursuit_title=row['title'])}, 'tastes': []}


class SettingsPool:
    """Only the settings table: mentions per user, as the database would answer."""

    def __init__(self, mentions):
        self.mentions = mentions

    def acquire(self):
        pool = self

        class Acquire:
            async def __aenter__(self):
                return NS(fetchval=pool.fetchval)

            async def __aexit__(self, *exc):
                return False
        return Acquire()

    async def fetchval(self, query, user_id):
        assert 'arti_life_settings' in query
        return self.mentions.get(user_id)


async def reply_system_prompt(pool, owner, chat_id=None, now=None):
    """The real reply assembly with a mocked provider; returns the system prompt it sent."""
    from ai import generation
    from cognition.runtime import CURRENT_TURN
    from tests.cognition.test_humanlike_voice import plan
    from tests.support.model_connections import configured_models, model_row
    now = now or datetime.now(timezone.utc)
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 1, 'model': 'upstream',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Ответ'}, 'finish_reason': 'stop'}]})

    chat_id = owner if chat_id is None else chat_id
    turn = NS(event=NS(context=NS(chat_id=chat_id, topic_id=-1), audience=NS(kind='private'),
                       evidence=NS(owner_id=owner), occurred_at=now),
              runtime=NS(pool=pool, clock=lambda: now), uses_cognition=False, active=False,
              expression=None, context_id=1, event_id=1)
    token = CURRENT_TURN.set(turn)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with configured_models(model_row('gemini')), \
                patch.object(generation, 'provider_http_client', return_value=http), \
                patch('cognition.reading.recent_notes', AsyncMock(return_value=[])), \
                patch('cognition.pursuits.recent_life', AsyncMock(side_effect=lambda pool, now: fixture_life(now))):
            result = await generation.generate_response_stream(chat_id, 'Привет', 'User', '', request_intent={},
                                                               expression_plan=plan())
    finally:
        CURRENT_TURN.reset(token)
    assert result[0] == 'Ответ'
    return json.loads(captured[0].content)['messages'][0]['content']


class PromptGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_mentions_off_hides_the_block_for_that_person_only(self):
        pool = SettingsPool({1: False, 2: True})
        self.assertNotIn(pursuits.HEADING, await reply_system_prompt(pool, 1))
        shown = await reply_system_prompt(pool, 2)
        self.assertIn(pursuits.HEADING, shown)
        self.assertIn('Вышила ещё 40 стежков.', shown)
        # No row is the default: mentions on.
        self.assertIn(pursuits.HEADING, await reply_system_prompt(pool, 3))

    async def test_private_chat_without_author_uses_its_person_and_failure_hides(self):
        self.assertNotIn(pursuits.HEADING, await reply_system_prompt(SettingsPool({5: False}), None, chat_id=5))

        class Broken:
            def acquire(self):
                raise ConnectionError('settings unreadable')
        self.assertNotIn(pursuits.HEADING, await reply_system_prompt(Broken(), 2))

    def test_person_of_a_turn(self):
        event = lambda owner, chat: NS(evidence=NS(owner_id=owner), context=NS(chat_id=chat))
        self.assertEqual(7, life_settings.person_of(event(7, -100)))
        self.assertEqual(9, life_settings.person_of(event(None, 9)))
        self.assertIsNone(life_settings.person_of(event(None, -100)))


class ViewTests(unittest.TestCase):
    def test_settings_body(self):
        self.assertEqual({'mentions': False}, life.settings_change({'mentions': False}))
        self.assertIsNone(life.settings_change({'reset': True}))
        for body in ({}, {'mentions': 'false'}, {'mentions': 0}, {'explainer_seen': None}, {'reset': False},
                     {'reset': True, 'mentions': True}, {'user_id': 1}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                life.settings_change(body)

    def test_mood_words_are_a_fixed_dictionary(self):
        from cognition.types import EMOTION_VOICE
        self.assertEqual(set(EMOTION_WORDS_EXPECTED), set(life.EMOTION_WORDS))
        self.assertLessEqual(set(life.EMOTION_WORDS) | life.HIDDEN_EMOTIONS, set(EMOTION_VOICE) | {'anger'})
        self.assertEqual('ровное', life.tone_word(.08, .1))
        self.assertEqual('ясное', life.tone_word(.3, .2))
        self.assertEqual('тихое', life.tone_word(-.3, .1))

    def test_room_pattern_and_stages(self):
        row = dict(pursuits.initial('embroidery', 3), id=1, status='active', next_step_at=None)
        row['state']['stitched'] = 12
        pattern = pursuits.room_pattern(row)
        self.assertEqual(40, len(pattern['rows']))
        self.assertTrue(all(len(r) == 40 and set(r) <= set('.abcd') for r in pattern['rows']))
        self.assertEqual(pattern['total'], sum(c != '.' for r in pattern['rows'] for c in r))
        self.assertEqual(len(pattern['palette']), len({c for r in pattern['rows'] for c in r} - {'.'}))
        self.assertTrue(all(p['color'].startswith('#') for p in pattern['palette']))
        rec = dict(pursuits.initial('restoration', 2), id=2, status='active', next_step_at=None)
        rec['state'].update(stage=2, steps=3)
        view = pursuits.room_pursuit(rec, None)['restoration']
        self.assertEqual(['осмотр', 'очистка', 'пропуски', 'выравнивание', 'сверка'], [s['name'] for s in view['stages']])
        self.assertEqual(['done', 'done', 'current', 'next', 'next'], [s['state'] for s in view['stages']])
        self.assertEqual(3, view['stage'])
        tea = dict(pursuits.initial('tea'), id=3, status='paused', next_step_at=None)
        self.assertIsNone(pursuits.room_pursuit(tea, None)['intention'])


EMOTION_WORDS_EXPECTED = ('joy', 'gratitude', 'pride', 'interest', 'surprise', 'sadness', 'indignation', 'fear',
                          'disappointment', 'guilt', 'embarrassment', 'amusement', 'relief', 'admiration',
                          'tenderness', 'boredom')


class DemoLifeTests(AioHTTPTestCase):
    async def get_application(self):
        return create_app(Settings(demo=True))

    async def test_session_csrf_origin_and_preview_flag(self):
        self.assertEqual(401, (await self.client.get('/api/arti/life')).status)
        self.assertEqual(401, (await self.client.get('/api/arti/life/settings')).status)
        auth = await (await self.client.post('/api/auth/demo', json={})).json()
        headers = {'X-CSRF-Token': auth['csrf_token']}
        boot = await (await self.client.get('/api/bootstrap')).json()
        self.assertFalse(boot['capabilities']['arti_life'])
        view = await (await self.client.get('/api/arti/life')).json()
        self.assertEqual(dict(mentions=True, explainer_seen=False), view['settings'])
        self.assertEqual(([], None, [], None, None), (view['pursuits'], view['embroidery'], view['tastes'], view['reading'], view['mood']))
        self.assertEqual(403, (await self.client.put('/api/arti/life/settings', json={'explainer_seen': True})).status)
        bad_origin = dict(headers, Origin='https://untrusted.example')
        self.assertEqual(403, (await self.client.put('/api/arti/life/settings', json={'explainer_seen': True}, headers=bad_origin)).status)
        self.assertEqual(400, (await self.client.put('/api/arti/life/settings', data='mentions=false', headers=headers)).status)
        self.assertEqual(400, (await self.client.put('/api/arti/life/settings', json={'user_id': 1, 'mentions': False}, headers=headers)).status)
        result = await self.client.put('/api/arti/life/settings', json={'explainer_seen': True}, headers=headers)
        self.assertEqual(dict(mentions=True, explainer_seen=True), (await result.json())['settings'])
        self.assertTrue((await (await self.client.get('/api/arti/life/settings')).json())['settings']['explainer_seen'])
        result = await self.client.put('/api/arti/life/settings', json={'reset': True}, headers=headers)
        self.assertEqual(dict(life_settings.DEFAULTS), (await result.json())['settings'])


class LiveLifeBoundaryTests(AioHTTPTestCase):
    async def get_application(self):
        return create_app(Settings(bot_token=TOKEN, bot_username='arti_test_bot'), pool=object(),
                          sessions=DemoSessions(), conversations=DemoRepository())

    async def test_identity_never_comes_from_headers_or_query(self):
        for path in ('/api/arti/life?user_id=43', '/api/arti/life/settings?user_id=43'):
            result = await self.client.get(path, headers={'X-Telegram-Init-Data': signed(date=int(time.time()))})
            self.assertEqual(401, result.status)
        origin = str(self.client.make_url('')).rstrip('/')
        login = await self.client.post('/api/auth/telegram', json=dict(init_data=signed(date=int(time.time()))), headers={'Origin': origin})
        cookie = {'Cookie': '__Host-arti_session=' + login.cookies['__Host-arti_session'].value}
        # The mutation is refused before any database work: no CSRF, then a foreign origin.
        self.assertEqual(403, (await self.client.put('/api/arti/life/settings', json={'mentions': False}, headers=dict(cookie, Origin=origin))).status)
        token = (await login.json())['csrf_token']
        self.assertEqual(403, (await self.client.put('/api/arti/life/settings', json={'mentions': False},
                                                     headers=dict(cookie, Origin='https://untrusted.example', **{'X-CSRF-Token': token}))).status)


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class LifeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from cognition.runtime import CognitiveRuntime
        from conversations.service import ArtiConversationService
        from tests.cognition.test_full_model import RecordedInterpreter
        from tests.support.database import isolated_database
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.now = datetime.now(timezone.utc) - timedelta(minutes=5)
        self.runtime = await CognitiveRuntime(self.pool, RecordedInterpreter(), 'active',
                                              clock=lambda: self.now).initialize(start_worker=False)
        self.rpatch = patch('cognition.runtime.get_runtime', return_value=self.runtime)
        self.rpatch.start()
        self.service = ArtiConversationService(self.pool, self.runtime)
        self.sessions = SessionStore(self.pool)
        self.app = create_app(Settings(bot_token=TOKEN, bot_username='arti_testbot', public_url=ORIGIN),
                              pool=self.pool, sessions=self.sessions, service=self.service)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.events = {}
        for user in (42, 43, 44):
            await self.service.ensure_user(dict(id=user, first_name=f'Тест {user}'))
        for user in (42, 43):
            cid, eid, _ = await self.runtime.ingest(user, user, 'Привет, как ты?', 1)
            await self.runtime.process(cid, eid)
            self.events[user] = (cid, eid)

    async def asyncTearDown(self):
        await self.client.close()
        self.rpatch.stop()
        await self.runtime.close()
        await self.db.__aexit__(None, None, None)

    async def headers(self, user):
        token = await self.sessions.create(dict(id=user, first_name='Тест', username=''), datetime.now(timezone.utc) + timedelta(minutes=10))
        return {'Cookie': '__Host-arti_session=' + token, 'Origin': ORIGIN, 'X-CSRF-Token': csrf(token)}

    async def get(self, user, path='/api/arti/life'):
        result = await self.client.get(path, headers=await self.headers(user))
        self.assertEqual(200, result.status)
        return await result.json()

    async def put(self, user, body, status=200):
        result = await self.client.put('/api/arti/life/settings', json=body, headers=await self.headers(user))
        self.assertEqual(status, result.status)
        return await result.json()

    async def feel(self, user, emotion, intensity=.6, valence=.5, cid=None):
        """Set this person's state in a context directly: one strong, slow episode."""
        from cognition.memory_repository import key
        from cognition.serialization import dump, load_state, object_value
        from cognition.types import EmotionEpisode
        cid = cid or self.events[user][0]
        async with self.pool.acquire() as conn:
            payload = object_value(await conn.fetchval('SELECT payload FROM cognitive_artifacts WHERE context_id=$1 AND artifact_key=$2',
                                                       cid, key('personal_state', user)))
            state = load_state(payload['state'])
            now = datetime.now(timezone.utc)
            episode = EmotionEpisode(f'ep-{user}', 'cause', 'group', 'connection', user if emotion == 'anger' else None,
                                     emotion, intensity, .9, now, 30 * 86400)
            state = replace(state, last_at=now, episodes=(episode,), mood_valence_latent=math.atanh(valence))
            await conn.execute('UPDATE cognitive_artifacts SET payload=$3::jsonb WHERE context_id=$1 AND artifact_key=$2',
                               cid, key('personal_state', user), dump(dict(state=object_value(dump(state)))))

    async def grudge(self, user):
        from cognition.memory_repository import key
        from cognition.serialization import object_value
        cid, _ = self.events[user]
        at = datetime.now(timezone.utc).isoformat()
        async with self.pool.acquire() as conn:
            payload = object_value(await conn.fetchval('SELECT payload FROM cognitive_artifacts WHERE context_id=$1 AND artifact_key=$2',
                                                       cid, key('relationship', user)))
            payload['rupture'] = dict(status='open', severity=.6, at=at, opened_at=at, cause='insult', group='g')
            await conn.execute('UPDATE cognitive_artifacts SET payload=$3::jsonb WHERE context_id=$1 AND artifact_key=$2',
                               cid, key('relationship', user), json.dumps(payload))

    async def live_pursuits(self):
        from tests.cognition.test_in_character_life import words_for
        async with self.pool.acquire() as conn:
            await conn.execute('''INSERT INTO arti_reading_notes(read_on,topic,title,url,gist,opinion,created_at)
                VALUES($1,'чай','Бергамот: как его добавляют в чай','https://example.org/bergamot','О чае.',
                'Цитрус в чае — смелая мысль.',$2)''', self.now.date(), self.now - timedelta(minutes=30))

        async def write(system, facts):
            return words_for(facts)
        service = pursuits.Pursuits(NS(pool=self.pool, clock=lambda: self.now, mode='active'), write)
        for kind in ('embroidery', 'restoration', 'tea'):
            self.assertEqual('stored', (await service.tick(kind=kind, manual=True))['outcome'])

    async def test_auth_csrf_and_origin_on_the_real_store(self):
        self.assertEqual(401, (await self.client.get('/api/arti/life')).status)
        headers = await self.headers(42)
        for drop, status in (('X-CSRF-Token', 403), ('Origin', 403), ('Cookie', 401)):
            partial = {k: v for k, v in headers.items() if k != drop}
            self.assertEqual(status, (await self.client.put('/api/arti/life/settings', json={'mentions': False}, headers=partial)).status)
        self.assertEqual(403, (await self.client.put('/api/arti/life/settings', json={'mentions': False},
                                                     headers=dict(headers, Origin='https://untrusted.example'))).status)
        self.assertEqual(dict(mentions=True, explainer_seen=False), (await self.get(42, '/api/arti/life/settings'))['settings'])
        boot = await self.get(42, '/api/bootstrap')
        self.assertTrue(boot['capabilities']['arti_life'])

    async def test_mood_is_this_persons_own_and_never_a_grudge(self):
        await self.feel(42, 'interest', valence=.4)
        await self.feel(43, 'sadness', valence=-.5)
        mine = (await self.get(42))['mood']
        self.assertEqual('ясное, с интересом', mine['label'])
        self.assertEqual('interest', mine['emotion'])
        other = (await self.get(43))['mood']
        self.assertEqual('тихое, с грустью', other['label'])
        self.assertNotIn('груст', json.dumps(await self.get(42), ensure_ascii=False))
        # No private conversation: no mood, never someone else's and never a group's.
        group, eid, _ = await self.runtime.ingest(-100777, 44, 'Всем привет', 2)
        await self.runtime.process(group, eid)
        await self.feel(44, 'joy', valence=.6, cid=group)
        self.assertIsNone((await self.get(44))['mood'])
        await self.grudge(42)
        self.assertIsNone((await self.get(42))['mood'])
        self.assertEqual('тихое, с грустью', (await self.get(43))['mood']['label'])
        # Anger at the person is the hurt itself and is not shown either.
        await self.feel(43, 'anger', valence=-.3)
        self.assertIsNone((await self.get(43))['mood'])

    async def test_room_data_and_mentions_off_for_one_person_only(self):
        await self.live_pursuits()
        room = await self.get(43)
        self.assertTrue(room['in_character'])
        self.assertEqual(['embroidery', 'restoration', 'tea'], [p['kind'] for p in room['pursuits']])
        embroidery, restoration, tea = room['pursuits']
        self.assertEqual(embroidery['embroidery']['stitched'], room['embroidery']['stitched'])
        self.assertGreater(room['embroidery']['stitched'], 0)
        self.assertEqual(40, len(room['embroidery']['rows']))
        self.assertTrue(embroidery['last_event']['summary'].startswith('Начала узор'))
        self.assertEqual(1, restoration['restoration']['step'])
        self.assertEqual('осмотр', restoration['restoration']['stages'][0]['name'])
        self.assertEqual(pursuits.ROOM_TEA_INTENTION, tea['intention'])
        self.assertEqual([('бергамот', 'да', None)], [(t['item'], t['verdict'], t['previous']) for t in room['tastes']])
        self.assertEqual('https://example.org/bergamot', room['tastes'][0]['source'])
        self.assertEqual('Бергамот: как его добавляют в чай', room['reading']['title'])
        self.assertEqual('Цитрус в чае — смелая мысль.', room['reading']['opinion'])

        await self.feel(42, 'interest', valence=.4)
        changed = await self.put(42, {'mentions': False})
        self.assertEqual(dict(mentions=False, explainer_seen=False), changed['settings'])
        hidden = await self.get(42)
        self.assertFalse(hidden['in_character'])
        self.assertEqual(([], None, []), (hidden['pursuits'], hidden['embroidery'], hidden['tastes']))
        self.assertEqual('Бергамот: как его добавляют в чай', hidden['reading']['title'])
        self.assertEqual('ясное, с интересом', hidden['mood']['label'])
        self.assertNotIn('стеж', json.dumps(hidden, ensure_ascii=False))
        self.assertEqual(3, len((await self.get(43))['pursuits']))

        # The same choice reaches the reply prompt in Telegram and the Mini App alike.
        self.assertNotIn(pursuits.HEADING, await reply_system_prompt(self.pool, 42))
        self.assertIn(pursuits.HEADING, await reply_system_prompt(self.pool, 43))
        await self.put(42, {'mentions': True})
        self.assertIn(pursuits.HEADING, await reply_system_prompt(self.pool, 42))

    async def test_explainer_flag_persists_through_sessions_forgetting_and_reset(self):
        from cognition.forgetting import forget_cognitive_sources
        await self.put(42, {'explainer_seen': True})
        await self.put(42, {'mentions': False})
        # A new session (another device) sees the same choice.
        self.assertEqual(dict(mentions=False, explainer_seen=True), (await self.get(42, '/api/arti/life/settings'))['settings'])
        cid, eid = self.events[42]
        async with self.pool.acquire() as conn:
            source = await conn.fetchval('SELECT source_id FROM cognitive_events WHERE id=$1', eid)
        await forget_cognitive_sources(self.pool, cid, 42, [source])
        await self.runtime.reset_history(42)
        self.assertEqual(dict(mentions=False, explainer_seen=True), (await self.get(42))['settings'])
        # Seeing it again does not move the first moment; switching it off is the person's own choice.
        async with self.pool.acquire() as conn:
            first = await conn.fetchval('SELECT explainer_seen_at FROM arti_life_settings WHERE user_id=42')
            await self.put(42, {'explainer_seen': True})
            self.assertEqual(first, await conn.fetchval('SELECT explainer_seen_at FROM arti_life_settings WHERE user_id=42'))
            self.assertEqual('miniapp', await conn.fetchval('SELECT source FROM arti_life_settings WHERE user_id=42'))
        self.assertEqual(dict(life_settings.DEFAULTS), (await self.put(42, {'reset': True}))['settings'])
        await self.put(43, {'mentions': False})
        async with self.pool.acquire() as conn:
            self.assertEqual(0, await conn.fetchval('SELECT count(*) FROM arti_life_settings WHERE user_id=42'))
            await life_settings.erase(conn, 43)
            self.assertEqual(0, await conn.fetchval('SELECT count(*) FROM arti_life_settings'))
