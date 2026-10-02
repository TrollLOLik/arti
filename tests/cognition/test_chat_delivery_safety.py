"""Offline Telegram limits, explicit rejections and durable part recovery."""
import asyncio
import html
import os
import unittest
from contextlib import ExitStack
from datetime import timedelta
from html.parser import HTMLParser
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telegram.error import BadRequest, RetryAfter
from telegram.ext import ExtBot
from bot.text_delivery import html_chunks, send_html_reply
from bot.request_runtime import CURRENT_REQUEST, checkpoint, store
from cognition.runtime import CURRENT_TURN
from cognition.delivery import DeliveryRejected, DeliverySuppressed
from tests.cognition import test_request_delivery as fixtures


class ChunkTests(unittest.TestCase):
    def test_long_unicode_nested_tags_and_entities(self):
        text='A & B < literal '+('😀é<& '*4000)
        parts=html_chunks('<b><i>'+html.escape(text)+'</i></b>')
        class Reader(HTMLParser):
            def __init__(self): super().__init__(convert_charrefs=True); self.stack=[]; self.text=''
            def handle_starttag(self,tag,attrs): self.stack.append(tag)
            def handle_endtag(self,tag):
                assert self.stack.pop()==tag
            def handle_data(self,data): self.text+=data
        joined=''
        for part in parts:
            reader=Reader(); reader.feed(part); reader.close()
            self.assertFalse(reader.stack)
            self.assertLessEqual(len(reader.text.encode('utf-16-le'))//2,4000)
            joined+=reader.text
        self.assertEqual(joined,text)

    def test_lengths_and_broken_markup_are_safe(self):
        self.assertEqual([len(p) for p in html_chunks('x'*5000)],[4000,1000])
        self.assertEqual([len(p) for p in html_chunks('x'*20000)],[4000]*5)
        self.assertEqual(html_chunks('<b>1 & 2 < 3<i>x</b>z'),['<b>1 &amp; 2 &lt; 3<i>x</i></b>z'])
        self.assertEqual(html_chunks('<script>text</script><a href="javascript:bad">link</a>'),['textlink'])
        self.assertEqual(html_chunks('&bogus; &amp; &#128512;'),['&amp;bogus; &amp; 😀'])

    def test_invalid_entity_nesting_is_flattened(self):
        self.assertEqual(html_chunks('<pre><b>x</b><code class="language-python">y</code></pre>'),['<pre>x<code class="language-python">y</code></pre>'])
        self.assertEqual(html_chunks('<b><code>x</code></b>'),['<b>x</b>'])
        self.assertEqual(html_chunks('<a href="https://a.test"><a href="https://b.test">x</a>y</a>'),['<a href="https://a.test">xy</a>'])
        self.assertEqual(html_chunks('<blockquote>a<blockquote>b</blockquote>c</blockquote>'),['<blockquote>abc</blockquote>'])
        self.assertEqual(html_chunks('<code class="language-python">x</code>'),['<code>x</code>'])


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class DeliverySafetyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.ActiveRequestDeliveryTests.asyncSetUp
    asyncTearDown=fixtures.ActiveRequestDeliveryTests.asyncTearDown

    async def test_rate_limit_preserves_delay_and_both_send_identities_on_restart(self):
        rejected=asyncio.Event()
        async def send_message(bot,*args,**kwargs):
            rejected.set()
            raise RetryAfter(timedelta(seconds=30))
        with patch.object(ExtBot,'send_message',new=send_message):
            task=asyncio.create_task(self.bot.send_message(chat_id=10,text='original'))
            await rejected.wait()
            for _ in range(100):
                async with self.pool.acquire() as conn:
                    prepared=await conn.fetchval("SELECT count(*) FROM arti_request_sends WHERE state='prepared' AND retry_at>NOW()")
                if prepared: break
                await asyncio.sleep(.01)
            self.assertEqual(prepared,1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.turn.delivery_blocked)
        self.assertEqual(self.turn.send_ordinal,0)
        await store().release(self.job['id'],self.job['token'])
        self.assertIsNone(await store().claim(['text']))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT status FROM cognitive_outbox'),'prepared')
            await conn.execute("UPDATE arti_requests SET available_at=NOW()-INTERVAL '1 second'")
            await conn.execute("UPDATE arti_request_sends SET retry_at=NOW()-INTERVAL '1 second'")
            await conn.execute("UPDATE cognitive_outbox SET retry_at=NOW()-INTERVAL '1 second'")
        CURRENT_REQUEST.set(await store().claim(['text']))
        result=await self.bot.send_message(chat_id=10,text='changed on restart')
        self.assertEqual(result.message_id,101)
        self.assertEqual(self.calls[0]['text'],'original')
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_outbox'),1)
            self.assertEqual(await conn.fetchval('SELECT status FROM cognitive_outbox'),'delivered')
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM arti_request_sends'),1)
            self.assertEqual(await conn.fetchval('SELECT state FROM arti_request_sends'),'delivered')

    async def test_cognitive_only_rate_limit_is_persisted_and_blocks_early_retry(self):
        CURRENT_REQUEST.set(None)
        calls=[]
        async def send_message(bot,*args,**kwargs):
            calls.append(kwargs)
            raise RetryAfter(30)
        with patch.object(ExtBot,'send_message',new=send_message):
            with self.assertRaises(RetryAfter): await self.bot.send_message(chat_id=10,text='same')
            with self.assertRaises(RetryAfter): await self.bot.send_message(chat_id=10,text='same')
        self.assertEqual(len(calls),1)
        self.assertEqual(self.turn.send_ordinal,0)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_outbox WHERE retry_at>NOW()'),1)
            await conn.execute('UPDATE cognitive_outbox SET retry_at=NOW()')
        await self.bot.send_message(chat_id=10,text='same')
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_outbox'),1)
            self.assertEqual(await conn.fetchval('SELECT status FROM cognitive_outbox'),'delivered')

    async def test_markup_only_answer_has_safe_fallback(self):
        await send_html_reply(self.bot,chat_id=10,text='<b></b>')
        self.assertEqual(len(self.calls),1)
        self.assertIn('Не удалось подготовить',self.calls[0]['text'])

    async def test_definite_bad_request_is_failed_not_unknown(self):
        async def send_message(bot,*args,**kwargs): raise BadRequest('Synthetic invalid input')
        with patch.object(ExtBot,'send_message',new=send_message):
            with self.assertRaises(DeliveryRejected):
                await self.bot.send_message(chat_id=10,text='invalid')
        with self.assertRaises(DeliverySuppressed):
            await self.bot.send_message(chat_id=10,text='must not send a fallback')
        self.assertFalse(self.calls)
        await store().finish(self.job['id'],self.job['token'],'failed','telegram_rejected')
        self.assertEqual((await store().status(self.job['id']))['state'],'failed')
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT status FROM cognitive_outbox'),'cancelled')
            self.assertEqual(await conn.fetchval('SELECT state FROM arti_request_sends'),'cancelled')

    async def test_chunk_recovery_does_not_duplicate_confirmed_first_part(self):
        provider=AsyncMock(return_value='x'*9000)
        text=await checkpoint('response',provider)
        sent=0
        async def send_message(bot,*args,**kwargs):
            nonlocal sent
            sent+=1
            if sent==2: raise RetryAfter(30)
            self.calls.append(kwargs)
            return NS(message_id=100+len(self.calls),chat=NS(id=10),text=kwargs['text'])
        with patch.object(ExtBot,'send_message',new=send_message):
            task=asyncio.create_task(send_html_reply(self.bot,chat_id=10,text=text))
            for _ in range(100):
                async with self.pool.acquire() as conn:
                    ready=await conn.fetchval("SELECT count(*) FROM arti_request_sends WHERE ordinal=2 AND state='prepared' AND retry_at IS NOT NULL")
                if ready: break
                await asyncio.sleep(.01)
            self.assertEqual(ready,1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        await store().release(self.job['id'],self.job['token'])
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE arti_requests SET available_at=NOW()')
            await conn.execute('UPDATE arti_request_sends SET retry_at=NOW()')
            await conn.execute('UPDATE cognitive_outbox SET retry_at=NOW()')
        CURRENT_REQUEST.set(await store().claim(['text'])); CURRENT_TURN.set(None)
        text=await checkpoint('response',provider)
        await send_html_reply(self.bot,chat_id=10,text=text)
        self.assertEqual([len(call['text']) for call in self.calls],[4000,4000,1000])
        provider.assert_awaited_once()
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_outbox'),3)
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE origin='delivered_action'"),3)
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM cognitive_events WHERE origin='user'"),1)

    async def test_actual_reply_path_splits_twenty_thousand_characters(self):
        from bot.queue import process_user_reply
        from cognition.scope import TransportScope
        request=dict(chat_id=10,user_id=1,user_name='Synthetic',user_message='Long explanation',message_id=1,
                     context=NS(bot=self.bot),is_voice=False,_telegram_scope=TransportScope(10,-1,'private',1,1))
        with ExitStack() as st:
            for target,value in [('materials.runtime.enabled',False),('bot.request_runtime.prepare_turn',self.turn),
                ('bot.organizer_commands.handle_natural',False),('bot.intent_context.routing_context',[]),
                ('ai.intents.resolve_intent',{}),('bot.queue.get_chat_context',''),('bot.queue.get_chat_model','fake'),
                ('bot.queue.generate_response_stream',('x'*20000,False,[],[]))]:
                st.enter_context(patch(target,return_value=value) if target=='materials.runtime.enabled' else patch(target,new=AsyncMock(return_value=value)))
            st.enter_context(patch.object(ExtBot,'send_chat_action',new=AsyncMock(return_value=True)))
            st.enter_context(patch('bot.queue.TTS_ENABLED',False))
            await process_user_reply(request,self.bot)
        self.assertEqual([len(call['text']) for call in self.calls],[4000]*5)
