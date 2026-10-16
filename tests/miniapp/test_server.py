from datetime import datetime, timedelta, timezone
import time
from unittest.mock import AsyncMock, patch
import uuid

from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer
from conversations.demo import DemoRepository
from miniapp.server import create_app, Settings, STATE
from miniapp.sessions import DemoSessions, digest
from tests.miniapp.test_auth import TOKEN, signed


class DemoTests(AioHTTPTestCase):
    async def get_application(self):
        return create_app(Settings(demo=True))

    async def asyncSetUp(self):
        await super().asyncSetUp()
        result = await self.client.post('/api/auth/demo', json={})
        self.assertEqual(200, result.status)
        self.auth = await result.json()
        self.csrf = {'X-CSRF-Token': self.auth['csrf_token']}

    async def create_todo(self, title='Новая мысль', **changes):
        body = dict(title=title, kind='todo', idempotency_key=str(uuid.uuid4()))
        body.update(changes)
        result = await self.client.post('/api/organizer', json=body, headers=self.csrf)
        self.assertEqual(201, result.status)
        return body, (await result.json())['item']

    async def test_explicit_empty_preview_and_browser_isolation(self):
        boot = await (await self.client.get('/api/bootstrap')).json()
        self.assertEqual('demo', boot['mode'])
        self.assertFalse(boot['capabilities']['chat_send'])
        self.assertEqual([], (await (await self.client.get('/api/organizer')).json())['items'])
        await self.create_todo('Только в моём просмотре')
        other = TestClient(TestServer(create_app(Settings(demo=True))))
        await other.start_server()
        try:
            self.assertEqual(401, (await other.get('/api/bootstrap?demo=true&user_id=42')).status)
            await other.post('/api/auth/demo', json={})
            self.assertEqual([], (await (await other.get('/api/organizer')).json())['items'])
            self.assertNotEqual(self.auth['user']['id'], (await (await other.get('/api/session')).json())['user']['id'])
        finally:
            await other.close()

    async def test_create_retry_conflict_versions_and_missing(self):
        body, first = await self.create_todo()
        retried = await self.client.post('/api/organizer', json=body, headers=self.csrf)
        self.assertEqual(first['id'], (await retried.json())['item']['id'])
        conflict = await self.client.post('/api/organizer', json={**body, 'title': 'Другая мысль'}, headers=self.csrf)
        self.assertEqual(409, conflict.status)
        path = '/api/organizer/'+first['id']
        change = dict(expected_version=1, state='done', idempotency_key=str(uuid.uuid4()))
        result = await self.client.patch(path, json=change, headers=self.csrf)
        changed = (await result.json())['item']
        self.assertEqual(('done', 2), (changed['state'], changed['version']))
        self.assertEqual(200, (await self.client.patch(path, json=change, headers=self.csrf)).status)
        self.assertEqual(409, (await self.client.patch(path, json={**change, 'idempotency_key': str(uuid.uuid4())}, headers=self.csrf)).status)
        self.assertEqual(404, (await self.client.patch('/api/organizer/missing', json=change, headers=self.csrf)).status)

    async def test_reminder_future_timezone_and_cancel_only(self):
        future = (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        _, item = await self.create_todo(kind='reminder', due_at=future, timezone='Asia/Yekaterinburg')
        body = dict(expected_version=1, state='done', idempotency_key=str(uuid.uuid4()))
        self.assertEqual(400, (await self.client.patch('/api/organizer/'+item['id'], json=body, headers=self.csrf)).status)
        body.update(state='cancelled', idempotency_key=str(uuid.uuid4()))
        self.assertEqual(200, (await self.client.patch('/api/organizer/'+item['id'], json=body, headers=self.csrf)).status)
        for date, zone in (('2000-01-01T00:00:00Z', 'UTC'), (future, 'Unknown/Nowhere'), ('2030-01-01T00:00:00Z', 'UTC')):
            result = await self.client.post('/api/organizer', json=dict(kind='reminder', title='Тест', due_at=date, timezone=zone, idempotency_key=str(uuid.uuid4())), headers=self.csrf)
            self.assertEqual(400, result.status)

    async def test_csrf_cross_origin_and_identity_injection(self):
        self.assertEqual(403, (await self.client.post('/api/organizer', json={})).status)
        self.assertEqual(403, (await self.client.post('/api/organizer', json={}, headers={**self.csrf, 'Origin': 'https://untrusted.example'})).status)
        for field in ('owner_id', 'user_id', 'chat_id', 'scope', '__proto__'):
            result = await self.client.post('/api/organizer', json=dict(kind='todo', title='x', idempotency_key=str(uuid.uuid4()), **{field: 999}), headers=self.csrf)
            self.assertEqual(400, result.status)

    async def test_conversations_are_private_and_sending_is_honest(self):
        items = (await (await self.client.get('/api/conversations')).json())['items']
        self.assertEqual(1, len(items))
        cid = items[0]['id']
        self.assertEqual('arti_private', items[0]['kind'])
        self.assertEqual([], (await (await self.client.get(f'/api/conversations/{cid}/messages')).json())['items'])
        result = await self.client.post(f'/api/conversations/{cid}/messages', json=dict(text='Не терять сообщение'), headers=self.csrf)
        self.assertEqual(409, result.status)
        self.assertEqual('feature_unavailable', (await result.json())['error']['code'])
        self.assertEqual(404, (await self.client.get('/api/conversations/'+str(uuid.uuid4()))).status)
        for query in ('limit=0', 'limit=101', 'before=-1', 'before=abc'):
            self.assertEqual(400, (await self.client.get(f'/api/conversations/{cid}/messages?{query}')).status)

    async def test_logout_revokes_and_expiry_requires_reentry(self):
        token = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))['arti_demo_session'].value
        result = await self.client.post('/api/auth/logout', json={}, headers=self.csrf)
        self.assertEqual(200, result.status)
        self.assertEqual(401, (await self.client.get('/api/session', headers={'Cookie': 'arti_demo_session='+token})).status)
        await self.client.post('/api/auth/demo', json={})
        token = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))['arti_demo_session'].value
        self.app[STATE]['sessions'].rows[digest(token)]['expires_at'] = datetime.now(timezone.utc)-timedelta(seconds=1)
        self.assertEqual(401, (await self.client.get('/api/bootstrap')).status)

    async def test_no_sample_data_or_cross_origin_access_headers(self):
        for path in ('/api/projects', '/api/library'):
            result = await self.client.get(path)
            self.assertEqual([], (await result.json())['items'])
            self.assertEqual('no-store', result.headers['Cache-Control'])
            self.assertNotIn('Access-Control-Allow-Origin', result.headers)
            self.assertIn('frame-ancestors', result.headers['Content-Security-Policy'])
        self.assertEqual(404, (await self.client.get('/api/library/demo-notes/download')).status)
        self.assertEqual(404, (await self.client.get('/static/index.html')).status)


class LiveBoundaryTests(AioHTTPTestCase):
    async def get_application(self):
        return create_app(Settings(bot_token=TOKEN, bot_username='arti_test_bot'), pool=object(), sessions=DemoSessions(), conversations=DemoRepository())

    async def login(self, user=None):
        result = await self.client.post('/api/auth/telegram', json=dict(init_data=signed(user, date=int(time.time()))),
                                        headers={'Origin': str(self.client.make_url('')).rstrip('/')})
        self.assertEqual(200, result.status)
        cookie = result.cookies['__Host-arti_session']
        self.assertTrue(cookie['secure'])
        self.assertTrue(cookie['httponly'])
        self.assertEqual('None', cookie['samesite'])
        body = await result.json()
        return {'Cookie': '__Host-arti_session='+cookie.value, 'X-CSRF-Token': body['csrf_token'],
                'Origin': str(self.client.make_url('')).rstrip('/')}

    async def test_header_or_query_never_bypasses_session(self):
        for path in ('/api/bootstrap?demo=true&user_id=42', '/api/organizer', '/api/projects'):
            result = await self.client.get(path, headers={'X-Telegram-Init-Data': signed(date=int(time.time()))})
            self.assertEqual(401, result.status)
        origin = {'Origin': str(self.client.make_url('')).rstrip('/')}
        self.assertEqual(404, (await self.client.post('/api/auth/demo', json={}, headers=origin)).status)
        self.assertEqual(403, (await self.client.post('/api/auth/telegram', json={})).status)

    async def test_signed_login_and_private_organizer_owner(self):
        headers = await self.login()
        repo = AsyncMock()
        repo.list.return_value = []
        with patch('miniapp.server.organizer', return_value=repo):
            result = await self.client.get('/api/organizer', headers=headers)
        self.assertEqual(200, result.status)
        repo.list.assert_awaited_once_with(42, 42, include_closed=False, limit=100)

    async def test_two_verified_users_cannot_access_each_other(self):
        first = await self.login()
        cid = (await (await self.client.get('/api/conversations', headers=first)).json())['items'][0]['id']
        second = await self.login(dict(id=43, first_name='Другой'))
        for suffix in ('', '/messages'):
            self.assertEqual(404, (await self.client.get('/api/conversations/'+cid+suffix, headers=second)).status)
        self.assertEqual(404, (await self.client.post('/api/conversations/'+cid+'/messages', json={}, headers=second)).status)

    async def test_bad_signature_expiry_and_profile_do_not_create_session(self):
        origin = {'Origin': str(self.client.make_url('')).rstrip('/')}
        for raw in ('', signed(date=int(time.time())-3601), signed(date=int(time.time()))+'&user={}', signed(dict(id=42, first_name=123), date=int(time.time()))):
            result = await self.client.post('/api/auth/telegram', json=dict(init_data=raw), headers=origin)
            self.assertEqual(401, result.status)
            self.assertNotIn('__Host-arti_session', result.cookies)

    async def test_old_cookie_is_revoked_on_rotation(self):
        old = await self.login()
        await self.client.post('/api/auth/telegram', json=dict(init_data=signed(date=int(time.time()))), headers=old)
        self.assertEqual(401, (await self.client.get('/api/session', headers=old)).status)

    async def test_optional_handoff_uses_verified_owner(self):
        headers = await self.login()
        with patch('miniapp.handoffs.prepare', new_callable=AsyncMock, return_value='A'*32) as prepare:
            result = await self.client.post('/api/handoff', json=dict(action='talk', prompt='Привет', idempotency_key=str(uuid.uuid4())), headers=headers)
        self.assertEqual(200, result.status)
        self.assertEqual(42, prepare.await_args.args[1])
