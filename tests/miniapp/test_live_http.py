"""Actual HTTP/SSE on disposable DB; no real auth credentials or remote calls."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
import os
import unittest
from unittest.mock import patch
import uuid

from aiohttp.test_utils import TestClient,TestServer
from cognition.runtime import CognitiveRuntime
from conversations.service import ArtiConversationService
from miniapp.server import create_app,Settings
from miniapp.sessions import SessionStore,csrf
from tests.cognition.test_full_model import RecordedInterpreter
from tests.miniapp.test_auth import TOKEN


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class LiveHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database()
        self.pool=await self.db.__aenter__()
        self.runtime=await CognitiveRuntime(self.pool,RecordedInterpreter()).initialize(False)
        self.rpatch=patch('cognition.runtime.get_runtime',return_value=self.runtime)
        self.rpatch.start()
        self.service=ArtiConversationService(self.pool,self.runtime)
        self.cid=await self.service.ensure_user(dict(id=42,first_name='Тест'))
        self.sessions=SessionStore(self.pool)
        self.token=await self.sessions.create(dict(id=42,first_name='Тест',username=''),datetime.now(timezone.utc)+timedelta(minutes=10))
        self.headers={'Cookie':'__Host-arti_session='+self.token,'Origin':'https://miniapp.example.test','X-CSRF-Token':csrf(self.token)}
        self.app=create_app(Settings(bot_token=TOKEN,bot_username='arti_testbot',public_url='https://miniapp.example.test'),
            pool=self.pool,sessions=self.sessions,service=self.service)
        self.client=TestClient(TestServer(self.app))
        await self.client.start_server()
        self.base='/api/conversations/'+str(self.cid)

    async def asyncTearDown(self):
        await self.client.close()
        self.rpatch.stop()
        await self.runtime.close()
        await self.db.__aexit__(None,None,None)

    async def frame(self,response):
        result={}
        async with asyncio.timeout(6):
            while True:
                line=(await response.content.readline()).decode().strip()
                if not line:
                    return result
                name,_,value=line.partition(':')
                result[name]=json.loads(value) if name=='data' else value.strip()

    async def test_post_idempotency_and_csrf_boundaries(self):
        boot=await self.client.get('/api/bootstrap',headers=self.headers)
        self.assertTrue((await boot.json())['capabilities']['chat_send'])
        payload=dict(text='Одно сообщение',client_message_id=str(uuid.uuid4()),history_revision=1)
        headers=dict(self.headers)
        headers.pop('X-CSRF-Token')
        self.assertEqual(403,(await self.client.post(self.base+'/messages',json=payload,headers=headers)).status)
        first=await self.client.post(self.base+'/messages',json=payload,headers=self.headers)
        self.assertEqual(202,first.status)
        result=await first.json()
        second=await self.client.post(self.base+'/messages',json=payload,headers=self.headers)
        self.assertEqual(result['message']['id'],(await second.json())['message']['id'])
        changed=await self.client.post(self.base+'/messages',json=dict(payload,text='Изменённый'),headers=self.headers)
        self.assertEqual(409,changed.status)
        status=await self.client.get(self.base+'/requests/'+result['request']['id'],headers=self.headers)
        self.assertEqual('queued',(await status.json())['request']['state'])

    async def test_reset_before_http_acceptance_rejects_stale_revision_even_empty(self):
        await self.service.synchronize(42,self.cid)
        await self.runtime.reset_history(42)
        payload=dict(text='Запрос начат до reset',client_message_id=str(uuid.uuid4()),history_revision=1)
        rejected=await self.client.post(self.base+'/messages',json=payload,headers=self.headers)
        self.assertEqual(409,rejected.status)
        self.assertEqual('history_reset',(await rejected.json())['error']['code'])
        page=await self.client.get(self.base+'/messages',headers=self.headers)
        self.assertEqual(2,(await page.json())['history_revision'])
        async with self.pool.acquire() as conn:
            self.assertEqual(0,await conn.fetchval('SELECT count(*) FROM arti_requests'))

    async def test_stream_reconnect_last_id_and_logout_revocation(self):
        for text in ('Первое','Второе'):
            await self.service.accept(42,self.cid,text,uuid.uuid4())
        stream=await self.client.get(self.base+'/events/stream?after=0',headers=dict(self.headers,**{'Last-Event-ID':'1'}))
        self.assertEqual('text/event-stream',stream.content_type)
        first=await self.frame(stream)
        self.assertEqual('state',first['event'])
        self.assertEqual([2],[r['seq'] for r in first['data']['items']])
        logout=await self.client.post('/api/auth/logout',json={},headers=self.headers)
        self.assertEqual(200,logout.status)
        revoked=await self.frame(stream)
        self.assertEqual('session.expired',revoked['event'])
        stream.close()
        self.assertEqual(401,(await self.client.get(self.base+'/events',headers=self.headers)).status)

    async def test_stream_revision_clears_erased_replay_and_future_cursor(self):
        await self.service.accept(42,self.cid,'Исчезнуть',uuid.uuid4())
        stream=await self.client.get(self.base+'/events/stream',headers=self.headers)
        initial=await self.frame(stream)
        await self.runtime.reset_history(42)
        reset=await self.frame(stream)
        self.assertGreater(reset['data']['history_revision'],initial['data']['history_revision'])
        self.assertIn('history.reset',[r['kind'] for r in reset['data']['items']])
        self.assertTrue(all(r['message'] is None for r in reset['data']['items']))
        await self.app.shutdown()
        async with asyncio.timeout(4):
            self.assertEqual(b'',await stream.content.read())
        stream.close()
        self.assertEqual(409,(await self.client.get(self.base+'/events?after=99999',headers=self.headers)).status)
