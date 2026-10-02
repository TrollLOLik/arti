"""Synthetic transport and real disposable database; never calls Telegram/providers."""
import asyncio
import logging
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from bot.request_runtime import CURRENT_REQUEST, checkpoint, send, store, submit, worker
from cognition.runtime import CURRENT_TURN
from cognition.delivery import DeliveryUnknown, DeliverySuppressed
from cognition.logging import PrivatePayloadFilter


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class RequestRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.turn = CURRENT_TURN.set(None)
        from materials.runtime import CURRENT_MATERIAL_USE, CURRENT_DERIVATIVE_USE, CURRENT_COMPUTATION_USE
        self.guards=[(var,var.set(())) for var in (CURRENT_MATERIAL_USE,CURRENT_DERIVATIVE_USE,CURRENT_COMPUTATION_USE)]
        self.context = CURRENT_REQUEST.set(None)

    async def asyncTearDown(self):
        CURRENT_REQUEST.reset(self.context)
        CURRENT_TURN.reset(self.turn)
        for var, token in self.guards: var.reset(token)
        await self.db.__aexit__(None,None,None)

    async def claim(self):
        await store().enqueue('text',1,-1,'fixture',{})
        job = await store().claim(['text'])
        CURRENT_REQUEST.set(job)
        return job

    async def test_checkpoint_survives_worker_recreation(self):
        job=await self.claim()
        provider=AsyncMock(return_value=(b'media','response'))
        self.assertEqual(await checkpoint('result',provider),(b'media','response'))
        self.assertTrue(await store().release(job['id'],job['token']))
        CURRENT_REQUEST.set(await store().claim(['text']))
        self.assertEqual(await checkpoint('result',provider),(b'media','response'))
        provider.assert_awaited_once()

    async def test_confirmed_send_replayed_without_second_transport(self):
        job=await self.claim(); transport=AsyncMock(return_value=NS(message_id=27))
        result=await send(transport,(),{'chat_id':1,'text':'private'},'message')
        self.assertEqual(result.message_id,27)
        await store().release(job['id'],job['token'])
        CURRENT_REQUEST.set(await store().claim(['text']))
        result=await send(transport,(),{'chat_id':1,'text':'regenerated'},'message')
        self.assertEqual(result.message_id,27); transport.assert_awaited_once()
        async with self.pool.acquire() as conn:
            receipt=await conn.fetchval('SELECT receipt::text FROM arti_request_sends')
            self.assertNotIn('private',receipt)

    async def test_unknown_send_blocks_fallback_and_restart(self):
        job=await self.claim(); transport=AsyncMock(side_effect=TimeoutError('private error'))
        with self.assertRaises(TimeoutError):
            await send(transport,(),{'chat_id':1,'text':'private'},'message')
        with self.assertRaises(DeliverySuppressed):
            await send(transport,(),{'chat_id':1,'text':'fallback'},'message')
        await store().release(job['id'],job['token'])
        self.assertIsNone(await store().claim(['text']))
        self.assertEqual((await store().status(job['id']))['state'],'delivery_unknown')
        transport.assert_awaited_once()

    async def test_stale_worker_cannot_checkpoint_or_send(self):
        job=await self.claim(); await store().cancel_chat(1)
        with self.assertRaises(DeliverySuppressed): await checkpoint('result',AsyncMock(return_value='private'))
        transport=AsyncMock()
        with self.assertRaises(DeliverySuppressed): await send(transport,(),{'chat_id':1,'text':'private'},'message')
        transport.assert_not_awaited()

    async def test_ambiguous_cosmetic_notice_does_not_poison_final_reply(self):
        job=await self.claim()
        CURRENT_REQUEST.set(dict(job, _ordinal=-1))
        with self.assertRaises(TimeoutError):
            await send(AsyncMock(side_effect=TimeoutError()),(),{'chat_id':1,'text':'wait'},'message')
        CURRENT_REQUEST.set(job)
        transport=AsyncMock(return_value=NS(message_id=28))
        self.assertEqual((await send(transport,(),{'chat_id':1,'text':'final'},'message')).message_id,28)
        await store().finish(job['id'],job['token'],'completed')
        self.assertEqual((await store().status(job['id']))['state'],'completed')

    async def test_partial_agent_handoff_is_not_replayed(self):
        from bot.request_runtime import agent_handoff
        from materials.types import MaterialError
        job=await self.claim()
        with self.assertRaises(RuntimeError):
            await agent_handoff(AsyncMock(side_effect=RuntimeError()))
        await store().release(job['id'],job['token'])
        CURRENT_REQUEST.set(await store().claim(['text']))
        action=AsyncMock()
        with self.assertRaisesRegex(MaterialError,'interrupted_agent_handoff'):
            await agent_handoff(action)
        action.assert_not_awaited()

    async def test_cancel_interrupts_execution_without_stopping_worker(self):
        from bot.queue import cancel_chat_generation, _running_chat_tasks
        job=await store().enqueue('text',1,-1,'fixture',{})
        started=asyncio.Event(); stopped=asyncio.Event()
        async def pending(*args):
            started.set()
            try: await asyncio.Event().wait()
            finally: stopped.set()
        with patch('bot.request_runtime._execute',pending):
            task=asyncio.create_task(worker(NS(),['text']))
            try:
                await asyncio.wait_for(started.wait(),2)
                await cancel_chat_generation(1)
                await asyncio.wait_for(stopped.wait(),.5)
                self.assertFalse(task.done())
                self.assertEqual((await store().status(job['id']))['state'],'cancelled')
            finally:
                task.cancel(); await asyncio.gather(task,return_exceptions=True)
        self.assertNotIn(1,_running_chat_tasks)

    async def test_status_is_scoped_and_latest_is_discoverable(self):
        from bot.request_runtime import request_status
        own=await store().enqueue('text',1,-1,'own',{})
        foreign=await store().enqueue('text',2,-1,'foreign',{})
        message=NS(message_thread_id=None,reply_text=AsyncMock())
        update=NS(effective_message=message,effective_chat=NS(id=1,type='private'))
        await request_status(update,NS(args=[]))
        self.assertIn(own['id'],message.reply_text.await_args.args[0])
        await request_status(update,NS(args=[foreign['id']]))
        self.assertEqual(message.reply_text.await_args.args[0],'Запрос не найден в этом чате.')

    async def test_shutdown_releases_unfinished_request(self):
        job=await store().enqueue('text',1,-1,'fixture',{})
        started=asyncio.Event()
        async def pending(*args): started.set(); await asyncio.Event().wait()
        with patch('bot.request_runtime._execute',pending):
            task=asyncio.create_task(worker(NS(),['text']))
            await asyncio.wait_for(started.wait(),2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        resumed=await store().claim(['text'])
        self.assertEqual(job['id'],resumed['id']); self.assertEqual(resumed['attempts'],2)

    async def test_deadline_expires_and_unblocks_next_request(self):
        first=await store().enqueue('text',1,-1,'first',{},budget_seconds=1)
        second=await store().enqueue('text',1,-1,'second',{})
        finished=asyncio.Event()
        async def execute(job,bot):
            if job['id']==first['id']: await asyncio.Event().wait()
            else:
                await store().finish(job['id'],job['token'],'completed')
                finished.set()
        with patch('bot.request_runtime._execute',execute):
            task=asyncio.create_task(worker(NS(),['text']))
            try: await asyncio.wait_for(finished.wait(),3)
            finally:
                task.cancel(); await asyncio.gather(task,return_exceptions=True)
        self.assertIn((await store().status(first['id']))['state'],('expired','failed'))
        self.assertEqual((await store().status(second['id']))['state'],'completed')


class RequestLoggingTests(unittest.TestCase):
    def test_filter_preserves_only_safe_lifecycle_fields(self):
        record=logging.LogRecord('bot.request_runtime',logging.INFO,'',0,'PRIVATE',(),None)
        record.request_diagnostic=dict(request_id='a'*32,kind='text',stage='response',duration_ms=42,
                                       queue_ms=3,attempt=1,prompt='PRIVATE',error_code='secret with spaces')
        PrivatePayloadFilter().filter(record)
        self.assertIn('request_id='+'a'*32,record.getMessage())
        self.assertIn('duration_ms=42',record.getMessage())
        self.assertNotIn('PRIVATE',record.getMessage()); self.assertNotIn('secret',record.getMessage())
