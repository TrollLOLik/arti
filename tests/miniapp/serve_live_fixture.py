"""Opt-in, loopback-only browser fixture: real API/SQL/worker; synthetic AI/Telegram.

Run with ARTI_TEST_DB=1 and ARTI_MINIAPP_LIVE_FIXTURE=1. Never a deployment entrypoint.
"""
import asyncio
from contextlib import ExitStack
from datetime import datetime,timedelta,timezone
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

from aiohttp import web


async def main():
    if os.getenv('ARTI_MINIAPP_LIVE_FIXTURE')!='1' or os.getenv('ARTI_TEST_DB')!='1':
        raise RuntimeError('Explicit test-only opt-in required')
    from tests.support.database import isolated_database
    async with isolated_database() as pool:
        from cognition.runtime import CognitiveRuntime
        from cognition.scope import CURRENT_SCOPE,TransportScope
        from cognition.types import AudienceScope
        from conversations.service import ArtiConversationService
        from conversations.mirror import MirrorWorker
        from miniapp.server import create_app,Settings
        from miniapp.sessions import SessionStore
        from tests.cognition.test_full_model import RecordedInterpreter
        from bot.request_runtime import worker
        runtime=await CognitiveRuntime(pool,RecordedInterpreter()).initialize(False)
        service=ArtiConversationService(pool,runtime)
        user=dict(id=99000002,first_name='Тест API',username='offline_fixture')
        cid=await service.ensure_user(user)
        CURRENT_SCOPE.set(TransportScope(user['id'],chat_type='private',user_id=user['id']))
        for index in range(3):
            await runtime.ingest(user['id'],user['id'],'Тестовая история Telegram '+str(index+1),index+1,
                audience=AudienceScope('private',user['id']))
        sessions=SessionStore(pool)
        token=await sessions.create(user,datetime.now(timezone.utc)+timedelta(hours=1))
        app=create_app(Settings(bot_token='123456:offline-fixture-only',bot_username='offline_fixture'),pool=pool,sessions=sessions,service=service)
        @web.middleware
        async def fixture_identity(request,handler):
            if request.remote not in ('127.0.0.1','::1'):
                raise web.HTTPForbidden()
            if request.path.startswith('/api/'):
                # Only this test module bypasses the external Telegram login.
                request=request.clone(headers={**request.headers,'Cookie':'__Host-arti_session='+token})
            return await handler(request)
        app.middlewares.insert(0,fixture_identity)
        async def control(request):
            if request.remote not in ('127.0.0.1','::1'):
                raise web.HTTPForbidden()
            body=await request.json()
            if body.get('reset'):
                await runtime.reset_history(user['id'])
            if body.get('telegram_text'):
                import secrets
                await runtime.ingest(user['id'],user['id'],str(body['telegram_text'])[:1000],10000+secrets.randbelow(1000000),
                    audience=AudienceScope('private',user['id']))
            return web.json_response(dict(ok=True,conversation_id=str(cid)))
        app.router.add_post('/__fixture',control)
        async def generate(*args,**kwargs):
            await asyncio.sleep(.4)
            return ('Тестовый ответ через настоящий обработчик Арти.\nВнешняя модель в этой проверке заменена; история и очередь работают с одноразовой БД.',False,[],[])
        with ExitStack() as patches:
            patches.enter_context(patch('cognition.runtime.get_runtime',return_value=runtime))
            patches.enter_context(patch('bot.queue.generate_response_stream',generate))
            patches.enter_context(patch('ai.intents.resolve_intent',AsyncMock(return_value={})))
            patches.enter_context(patch('bot.queue.get_chat_model',AsyncMock(return_value='offline-model')))
            patches.enter_context(patch('bot.queue._initiative_question',AsyncMock()))
            patches.enter_context(patch('bot.queue._conversational_reply',return_value=False))
            bot=SimpleNamespace(send_message=AsyncMock(),send_chat_action=AsyncMock())
            fake_send=AsyncMock(return_value=SimpleNamespace(message_id=900001))
            tasks=[asyncio.create_task(worker(bot,['text'])),asyncio.create_task(MirrorWorker(pool,fake_send).run())]
            runner=web.AppRunner(app,access_log=None)
            try:
                await runner.setup()
                await web.TCPSite(runner,'127.0.0.1',8780).start()
                print('TEST ONLY real API fixture: http://127.0.0.1:8780/#chats/arti',flush=True)
                await asyncio.Event().wait()
            finally:
                await runner.cleanup()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                await runtime.close()


if __name__=='__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
