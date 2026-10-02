"""Synthetic intake ordering, controls, ownership and repeated cancellation."""
import asyncio
import inspect
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from cognition.telegram_scope import CognitiveUpdateProcessor, cancel_pending_intake
from cognition.scope import CURRENT_SCOPE


def update(mid, chat=10, topic=None, owner=1, text=None):
    user=NS(id=owner,is_bot=False)
    message=NS(message_id=mid,from_user=user,text=text,caption=None,message_thread_id=topic,reply_text=AsyncMock())
    return NS(effective_message=message,effective_user=user,effective_chat=NS(id=chat,type='private' if chat>0 else 'supergroup',is_forum=topic is not None))


class IntakeSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime=patch('cognition.telegram_scope.get_runtime',return_value=None); self.runtime.start()
        self.menu=patch('bot.menu.is_menu_input',new=AsyncMock(return_value=False)); self.menu.start()
    async def asyncTearDown(self):
        self.menu.stop(); self.runtime.stop()

    async def test_order_reserved_before_preprocessing_other_lanes_and_controls_continue(self):
        processor=CognitiveUpdateProcessor(2)
        release=asyncio.Event(); started=asyncio.Event(); order=[]
        async def slow(): started.set(); await release.wait(); order.append(1)
        async def fast(value): order.append(value)
        first=asyncio.create_task(processor.process_update(update(1),slow()))
        await started.wait()
        waiting=[asyncio.create_task(processor.process_update(update(mid),fast(mid))) for mid in range(2,35)]
        await asyncio.sleep(0)
        await asyncio.wait_for(processor.process_update(update(90,chat=11),fast('other')),1)
        await asyncio.wait_for(processor.process_update(update(91,text='/request'),fast('status')),1)
        self.assertEqual(order,['other','status'])
        release.set(); await asyncio.gather(first,*waiting)
        self.assertEqual(order[2:],list(range(1,35)))
        self.assertFalse(processor._lanes); self.assertFalse(processor._intakes)

    async def test_different_topics_parallel_and_noncontrols_bounded(self):
        processor=CognitiveUpdateProcessor(2); release=asyncio.Event(); started=[]
        async def work(value): started.append(value); await release.wait()
        tasks=[asyncio.create_task(processor.process_update(update(i,chat=-100,topic=i),work(i))) for i in range(1,4)]
        for _ in range(10):
            if len(started)==2: break
            await asyncio.sleep(0)
        self.assertEqual(started,[1,2]); self.assertEqual(processor.current_concurrent_updates,2)
        release.set(); await asyncio.gather(*tasks)
        self.assertEqual(started,[1,2,3])

    async def test_owner_cancel_joins_old_intake_and_does_not_cancel_other_owner(self):
        processor=CognitiveUpdateProcessor(2); started=asyncio.Event(); submitted=[]
        async def slow(): started.set(); await asyncio.Event().wait(); submitted.append('old')
        async def follow(): submitted.append('queued')
        async def other(): submitted.append('other')
        a=asyncio.create_task(processor.process_update(update(1,chat=-100,topic=3),slow()))
        await started.wait()
        waiting=follow()
        b=asyncio.create_task(processor.process_update(update(2,chat=-100,topic=3),waiting))
        c=asyncio.create_task(processor.process_update(update(3,chat=-100,topic=3,owner=2),other()))
        await asyncio.sleep(0)
        async def cancel():
            self.assertEqual(CURRENT_SCOPE.get().topic_id,3)
            self.assertEqual(await cancel_pending_intake(-100,3,1),2)
        await processor.process_update(update(4,chat=-100,topic=3,text='/cancel'),cancel())
        await asyncio.gather(a,b,c,return_exceptions=True)
        self.assertEqual(submitted,['other'])
        self.assertEqual(inspect.getcoroutinestate(waiting),inspect.CORO_CLOSED)

    async def test_overflow_explicitly_refused_without_awaiting_handler(self):
        processor=CognitiveUpdateProcessor(1); processor.MAX_PENDING_PER_LANE=1
        release=asyncio.Event(); started=asyncio.Event()
        async def pending(): started.set(); await release.wait()
        task=asyncio.create_task(processor.process_update(update(1),pending())); await started.wait()
        refused=update(2); handler=AsyncMock(); coro=handler()
        await processor.process_update(refused,coro)
        handler.assert_not_awaited(); self.assertEqual(inspect.getcoroutinestate(coro),inspect.CORO_CLOSED)
        self.assertIn('не сохранено',refused.effective_message.reply_text.await_args.args[0])
        release.set(); await task

    async def test_controls_have_separate_bounded_permits_and_await_contract(self):
        from telegram.ext import BaseUpdateProcessor
        # The adapter deliberately wraps the public PTB method, even where PTB
        # marks it final. Keep supported major versions and awaited semantics.
        import telegram
        self.assertIn(int(telegram.__version__.split('.')[0]),(21,22))
        self.assertTrue(inspect.iscoroutinefunction(BaseUpdateProcessor.process_update))
        processor=CognitiveUpdateProcessor(1); release=asyncio.Event(); started=[]
        async def work(i): started.append(i); await release.wait()
        tasks=[asyncio.create_task(processor.process_update(update(i,text='/request'),work(i))) for i in range(5)]
        for _ in range(10):
            if len(started)==4: break
            await asyncio.sleep(0)
        self.assertEqual(len(started),4); self.assertTrue(all(not task.done() for task in tasks))
        release.set(); await asyncio.gather(*tasks)
        self.assertEqual(started,list(range(5)))

    async def test_shutdown_closes_queued_and_active_coroutines(self):
        processor=CognitiveUpdateProcessor(1); started=asyncio.Event()
        async def pending(): started.set(); await asyncio.Event().wait()
        first=pending(); second=pending()
        a=asyncio.create_task(processor.process_update(update(1),first)); await started.wait()
        b=asyncio.create_task(processor.process_update(update(2),second)); await asyncio.sleep(0)
        await processor.shutdown()
        await asyncio.gather(a,b,return_exceptions=True)
        self.assertEqual(inspect.getcoroutinestate(first),inspect.CORO_CLOSED)
        self.assertEqual(inspect.getcoroutinestate(second),inspect.CORO_CLOSED)
        self.assertFalse(processor._intakes); self.assertFalse(processor._lanes)


class OwnedCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancel_waits_for_owned_release(self):
        import bot.request_runtime as rr
        import bot.queue as q
        started=asyncio.Event(); releasing=asyncio.Event(); allow=asyncio.Event(); released=asyncio.Event()
        job=dict(id='synthetic',chat_id=9876,kind='text',token='fixture',created_at=datetime.now(timezone.utc),deadline_at=datetime.now(timezone.utc)+timedelta(seconds=30))
        class FakeStore:
            async def claim(self,*args): return job
            async def release(self,*args): releasing.set(); await allow.wait(); released.set()
        async def execute(*args): started.set(); await asyncio.Event().wait()
        with patch.object(rr,'store',return_value=FakeStore()),patch.object(rr,'_execute',execute):
            task=asyncio.create_task(rr.worker(NS(),['text']))
            await started.wait(); q.cancel_chat_tasks(9876); await releasing.wait()
            task.cancel(); await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0)
            self.assertFalse(task.done())
            allow.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(released.is_set())
        self.assertNotIn(9876,q._running_chat_tasks)
        self.assertFalse([t for t in asyncio.all_tasks() if 'await_owned' in t.get_coro().__qualname__])

    async def test_cleanup_deadline_cancels_and_joins_inner_operation(self):
        from utils.async_cleanup import await_owned
        stopped=asyncio.Event()
        async def never():
            try: await asyncio.Event().wait()
            finally: stopped.set()
        with self.assertRaises(TimeoutError): await await_owned(never(),timeout=.01)
        self.assertTrue(stopped.is_set())


import os
@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class OwnerCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from bot.request_runtime import store
        self.db=isolated_database(); self.pool=await self.db.__aenter__(); self.store=store()
    async def asyncTearDown(self): await self.db.__aexit__(None,None,None)
    async def enqueue(self,key,owner=1,topic=3):
        payload={'codec':1,'kind':'request','value':{'codec':1,'kind':'dict','items':{'user_id':owner}}}
        return await self.store.enqueue('text',-100,topic,key,payload)
    async def test_cancel_is_owner_chat_topic_scoped_and_never_whole_group(self):
        own=await self.enqueue('own'); other=await self.enqueue('other',owner=2); topic=await self.enqueue('topic',topic=4)
        self.assertIsNone(await self.store.cancel_owned(own['id'],-100,3,2))
        self.assertIsNone(await self.store.cancel_owned(own['id'],-100,4,1))
        self.assertIsNone(await self.store.cancel_owned(own['id'],-200,3,1))
        result=await self.store.cancel_owned(None,-100,3,1)
        self.assertEqual(result,{'id':own['id'],'state':'cancelled'})
        self.assertEqual((await self.store.status(other['id']))['state'],'queued')
        self.assertEqual((await self.store.status(topic['id']))['state'],'queued')
    async def test_cancel_inflight_send_preserves_ambiguity(self):
        own=await self.enqueue('own')
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE arti_requests SET available_at=NOW()')
        job=await self.store.claim(['text'])
        await self.store.prepare_send(job['id'],job['token'],1,{})
        await self.store.begin_send(job['id'],job['token'],1)
        result=await self.store.cancel_owned(own['id'],-100,3,1)
        self.assertEqual(result['state'],'delivery_unknown')
        self.assertFalse(await self.store.guard(job['id'],job['token']))
    async def test_nonadmin_cancel_command_uses_owner_path(self):
        from bot.commands import handle_cancel_command
        own=await self.enqueue('own'); other=await self.enqueue('other',owner=2)
        incoming=update(7,chat=-100,topic=3,text='/cancel')
        incoming.message=incoming.effective_message
        with patch('bot.commands.is_admin',new=AsyncMock(return_value=False)):
            await handle_cancel_command(incoming,NS(args=[]))
        self.assertEqual((await self.store.status(own['id']))['state'],'cancelled')
        self.assertEqual((await self.store.status(other['id']))['state'],'queued')
        self.assertIn(own['id'],incoming.message.reply_text.await_args.args[0])
