"""Synthetic scoped location shares; all geocoding/provider calls mocked."""
import asyncio
import os
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from cognition.scope import CURRENT_SCOPE, TransportScope
from utils import location_manager as manager, location_store
from utils.location_scope import (LOCATION_TTL_SECONDS, location_scope_key,
    pop_pending_map_request, set_pending_map_request)


PRIVATE = TransportScope(1, -1, 'private', 1, 11)
GROUP = TransportScope(-100, 7, 'supergroup', 1, 22)
OTHER_TOPIC = TransportScope(-100, 8, 'supergroup', 1, 23)


class LocationPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from config import pending_map_requests
        self.old_cache = dict(manager._location_cache)
        self.old_geocode = dict(manager._last_geocode_at)
        self.old_geocoding_enabled = manager._geocoding_enabled
        manager._geocoding_enabled = True
        self.old_pending = dict(pending_map_requests)
        manager._location_cache.clear(); manager._last_geocode_at.clear(); pending_map_requests.clear()
        self.token = CURRENT_SCOPE.set(None)
        for name, value in (('save', 'saved'), ('get', None), ('update_address', None)):
            p = patch.object(location_store, name, new=AsyncMock(return_value=value))
            p.start(); self.addCleanup(p.stop)
        p = patch.object(manager, '_reverse_geocode', new=AsyncMock(return_value={}))
        p.start(); self.addCleanup(p.stop)

    async def asyncTearDown(self):
        from config import pending_map_requests
        await asyncio.sleep(0)
        manager._location_cache.clear(); manager._location_cache.update(self.old_cache)
        manager._last_geocode_at.clear(); manager._last_geocode_at.update(self.old_geocode)
        manager._geocoding_enabled = self.old_geocoding_enabled
        pending_map_requests.clear(); dict.update(pending_map_requests, self.old_pending)
        CURRENT_SCOPE.reset(self.token)

    async def share(self, scope=PRIVATE, **kwargs):
        CURRENT_SCOPE.set(scope)
        result = await manager.set_user_location(1, 12.34567, 76.54321, **kwargs)
        await asyncio.sleep(0)
        return result

    async def test_location_does_not_cross_chat_topic_or_user(self):
        self.assertTrue(await self.share())
        self.assertIsNotNone(await manager.get_user_location(1))
        for scope in (GROUP, OTHER_TOPIC, TransportScope(2, -1, 'private', 1),
                      TransportScope(1, -1, 'private', 2), None):
            CURRENT_SCOPE.set(scope)
            self.assertIsNone(await manager.get_user_location(1))
        self.assertTrue(await self.share(GROUP))
        self.assertIsNotNone(await manager.get_user_location(1, chat_id=-100))
        self.assertIsNone(await manager.get_user_location(1, chat_id=1))
        CURRENT_SCOPE.set(OTHER_TOPIC)
        self.assertIsNone(await manager.get_user_location(1))

    async def test_legacy_cache_and_unknown_group_scope_fail_closed(self):
        manager._location_cache[1] = dict(lat=12.34567, lng=76.54321, timestamp=time.time(), ttl=7200)
        CURRENT_SCOPE.set(GROUP)
        self.assertIsNone(await manager.get_user_location(1))
        self.assertNotIn(1, manager._location_cache)
        unknown = TransportScope(-100, -1, 'supergroup', 1)
        self.assertIsNone(location_scope_key(1, scope=unknown))
        self.assertFalse(await manager.set_user_location(1, 12, 34, scope=unknown))

    async def test_pending_prompt_requires_exact_chat_topic_user_mode_and_original_age(self):
        prompt = 'Synthetic private map query'
        with patch('utils.location_scope.time.time', return_value=1000):
            self.assertTrue(set_pending_map_request(1, prompt, scope=PRIVATE))
            self.assertIsNone(pop_pending_map_request(1, scope=GROUP))
            self.assertIsNone(pop_pending_map_request(1, scope=PRIVATE, mode='rp'))
            self.assertEqual(prompt, pop_pending_map_request(1, scope=PRIVATE))
            self.assertTrue(set_pending_map_request(1, prompt, scope=GROUP))
            self.assertIsNone(pop_pending_map_request(1, scope=OTHER_TOPIC))
            self.assertIsNone(pop_pending_map_request(2, scope=GROUP))
        with patch('utils.location_scope.time.time', return_value=1000 + LOCATION_TTL_SECONDS):
            self.assertIsNone(pop_pending_map_request(1, scope=GROUP))

    async def test_malformed_pending_entries_fail_closed(self):
        from config import pending_map_requests
        for value in ({}, {'created_at': time.time()}, {'prompt': [], 'created_at': time.time()}):
            pending_map_requests[('map', 1, -1, 1, 'default')] = value
            self.assertIsNone(pop_pending_map_request(1, scope=PRIVATE))
        self.assertFalse(set_pending_map_request(1, '', scope=PRIVATE))

    async def test_shutdown_cancels_and_joins_geocoder_even_when_cancelled_twice(self):
        started, cleaning, release, entered = (asyncio.Event() for _ in range(4))
        async def geocode(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        async def expire():
            entered.set()
        manager._reverse_geocode.side_effect = geocode
        worker = None
        try:
            await self.share()
            await started.wait()
            with patch.object(location_store, 'expire', side_effect=expire):
                worker = asyncio.create_task(manager.maintenance_worker())
                await entered.wait()
                worker.cancel()
                await cleaning.wait()
                worker.cancel()
                await asyncio.sleep(0)
                self.assertFalse(worker.done())
                release.set()
                await asyncio.gather(worker, return_exceptions=True)
            self.assertFalse(manager._geocode_tasks)
            location_store.update_address.assert_not_awaited()
        finally:
            release.set()
            if worker is not None and not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def test_static_and_live_use_exact_30_minute_limit(self):
        for live in (False, True):
            with patch('utils.location_manager.time.time', return_value=1000):
                self.assertTrue(await self.share(is_live=live))
            with patch('utils.location_manager.time.time', return_value=2799):
                self.assertIsNotNone(await manager.get_user_location(1))
            with patch('utils.location_manager.time.time', return_value=2800):
                self.assertIsNone(await manager.get_user_location(1))
                self.assertFalse(await self.share(is_live=live, shared_at=1000))

    async def test_restore_keeps_original_age_and_geocode_does_not_renew(self):
        sample = dict(lat=12, lng=34, city=None, address=None, live=False, timestamp=1000, sample_id='old')
        CURRENT_SCOPE.set(PRIVATE)
        location_store.get.return_value = sample
        with patch('utils.location_manager.time.time', return_value=2790):
            self.assertIsNotNone(await manager.get_user_location(1))
            manager._reverse_geocode.return_value = {'city': 'Synthetic city'}
            await manager._do_geocoding((1, -1, 1), sample)
            self.assertEqual(1000, manager._location_cache[(1, -1, 1)]['timestamp'])
        with patch('utils.location_manager.time.time', return_value=2800):
            self.assertIsNone(await manager.get_user_location(1))

    async def test_late_geocoder_cannot_overwrite_new_share(self):
        await self.share()
        old = manager._location_cache[(1, -1, 1)]
        await self.share()
        newer = manager._location_cache[(1, -1, 1)]
        manager._reverse_geocode.return_value = {'city': 'Old city'}
        await manager._do_geocoding((1, -1, 1), old)
        self.assertIsNone(newer['city'])
        location_store.update_address.assert_not_awaited()

    async def test_unrelated_location_does_not_consume_or_resume_private_prompt(self):
        from bot.handlers import handle_location_message
        set_pending_map_request(1, 'Synthetic private map query', scope=PRIVATE)
        CURRENT_SCOPE.set(GROUP)
        user = NS(id=1, first_name='Synthetic', username=None)
        message = NS(from_user=user, message_id=22, message_thread_id=7,
                     location=NS(latitude=12, longitude=34, live_period=None), reply_text=AsyncMock())
        update = NS(message=message, edited_message=None, effective_chat=NS(id=-100, type='supergroup'))
        with patch('bot.handlers.is_responses_enabled', new=AsyncMock(return_value=True)), \
             patch('bot.handlers.enqueue_reply', new=AsyncMock()) as enqueue:
            await handle_location_message(update, NS())
            await asyncio.sleep(0)
        enqueue.assert_not_awaited()
        self.assertEqual('Synthetic private map query', pop_pending_map_request(1, scope=PRIVATE))

    async def test_same_scope_location_resumes_once_and_expired_share_does_not_resume(self):
        from bot.handlers import handle_location_message
        CURRENT_SCOPE.set(GROUP)
        user = NS(id=1, first_name='Synthetic', username=None)
        message = NS(from_user=user, message_id=22, message_thread_id=7, date=None,
                     location=NS(latitude=12, longitude=34, live_period=None), reply_text=AsyncMock())
        update = NS(message=message, edited_message=None, effective_chat=NS(id=-100, type='supergroup'))
        set_pending_map_request(1, 'Synthetic same-topic query', scope=GROUP)
        with patch('bot.handlers.is_responses_enabled', new=AsyncMock(return_value=True)), \
             patch('bot.handlers.enqueue_reply', new=AsyncMock()) as enqueue, \
             patch('bot.handlers.asyncio.sleep', new=AsyncMock()):
            await handle_location_message(update, NS())
            await handle_location_message(update, NS())
            enqueue.assert_awaited_once()
            self.assertEqual((-100, 1, 'Synthetic', 'Synthetic same-topic query'), enqueue.await_args.args[:4])
        set_pending_map_request(1, 'Still pending', scope=GROUP)
        message.date = time.time() - LOCATION_TTL_SECONDS - 1
        with patch('bot.handlers.enqueue_reply', new=AsyncMock()) as enqueue:
            await handle_location_message(update, NS())
        enqueue.assert_not_awaited()
        self.assertEqual('Still pending', pop_pending_map_request(1, scope=GROUP))

    async def test_generation_rejects_private_cache_and_passed_in_coordinates_in_group(self):
        from ai import generation
        from cognition.runtime import CURRENT_TURN
        from materials.runtime import CURRENT_MATERIAL_USE, CURRENT_DERIVATIVE_USE, CURRENT_COMPUTATION_USE
        await self.share()
        private_loc = await manager.get_user_location(1)
        CURRENT_SCOPE.set(GROUP)
        tokens = [(var, var.set(value)) for var, value in ((CURRENT_TURN, None),
                  (CURRENT_MATERIAL_USE, ()), (CURRENT_DERIVATIVE_USE, ()), (CURRENT_COMPUTATION_USE, ()))]
        try:
            mock = AsyncMock(return_value=NS(text='Synthetic answer', candidates=[]))
            with patch.object(generation.genai_client.aio.models, 'generate_content', new=mock):
                await generation.generate_response_stream(-100, 'Hello', 'Synthetic', '', user_id=1,
                    user_location=private_loc, request_intent={}, custom_system_prompt='Synthetic system')
            cfg = mock.await_args.kwargs['config']
            self.assertNotIn('12.34567', cfg.system_instruction)
            self.assertIsNone(cfg.tool_config)
            self.assertFalse(any(getattr(tool, 'google_maps', None) is not None for tool in cfg.tools or ()))
            # A fresh explicit share in that same topic enables the real tool.
            await self.share(GROUP)
            mock.reset_mock()
            with patch.object(generation.genai_client.aio.models, 'generate_content', new=mock):
                await generation.generate_response_stream(-100, 'Places nearby', 'Synthetic', '', user_id=1,
                    user_location=private_loc, request_intent={}, custom_system_prompt='Synthetic system')
            cfg = mock.await_args.kwargs['config']
            self.assertIn('12.34567', cfg.system_instruction)
            self.assertAlmostEqual(12.34567, cfg.tool_config.retrieval_config.lat_lng.latitude)
        finally:
            for var, token in reversed(tokens): var.reset(token)


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class LocationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db = isolated_database(); self.pool = await self.db.__aenter__()

    async def asyncTearDown(self):
        await self.db.__aexit__(None, None, None)

    def sample(self, seconds_old=0, sample_id='synthetic', live=False):
        return dict(lat=12, lng=34, city=None, address=None, live=live,
                    timestamp=time.time()-seconds_old, sample_id=sample_id)

    async def test_persisted_scope_original_expiry_and_address_cas(self):
        key = (-100, 7, 1); sample = self.sample(1790)
        self.assertEqual('synthetic', await location_store.save(key, sample))
        loaded = await location_store.get(key)
        self.assertAlmostEqual(sample['timestamp'], loaded['timestamp'], places=4)
        self.assertEqual(1800, (loaded['expires_at']-loaded['received_at']).total_seconds())
        for wrong in ((1, -1, 1), (-100, 8, 1), (-100, 7, 2)):
            self.assertIsNone(await location_store.get(wrong))
        original_expiry = loaded['expires_at']
        await location_store.update_address(key, 'synthetic', city='Synthetic city', address='Synthetic address')
        self.assertEqual(original_expiry, (await location_store.get(key))['expires_at'])
        newer = self.sample(sample_id='new', live=True)
        await location_store.save(key, newer)
        await location_store.update_address(key, 'synthetic', city='Old city', address='Old address')
        self.assertIsNone((await location_store.get(key))['city'])
        self.assertIsNone(await location_store.save(key, sample))

    async def test_expired_scoped_and_legacy_coordinates_are_purged(self):
        await location_store.save((1, -1, 1), self.sample(1801))
        self.assertIsNone(await location_store.get((1, -1, 1)))
        async with self.pool.acquire() as conn:
            await conn.execute('''INSERT INTO user_locations(user_id,lat,lng,updated_at)
                VALUES(1,12,34,LOCALTIMESTAMP - INTERVAL '31 minutes'),
                      (2,56,78,LOCALTIMESTAMP)''')
        await location_store.expire()
        async with self.pool.acquire() as conn:
            self.assertEqual(0, await conn.fetchval('SELECT count(*) FROM arti_scoped_locations'))
            self.assertEqual([2], [r['user_id'] for r in await conn.fetch('SELECT user_id FROM user_locations')])

    async def test_restart_read_uses_original_age_and_ignores_legacy(self):
        token = CURRENT_SCOPE.set(PRIVATE)
        old_cache = dict(manager._location_cache); manager._location_cache.clear()
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('INSERT INTO user_locations(user_id,lat,lng) VALUES(1,56,78)')
            self.assertIsNone(await manager.get_user_location(1))
            sample = self.sample(1790)
            await location_store.save((1, -1, 1), sample)
            self.assertIsNotNone(await manager.get_user_location(1))
            restored_at = manager._location_cache[(1, -1, 1)]['timestamp']
            self.assertAlmostEqual(sample['timestamp'], restored_at, places=4)
            with patch('utils.location_manager.time.time', return_value=restored_at+1800):
                self.assertIsNone(await manager.get_user_location(1))
        finally:
            manager._location_cache.clear(); manager._location_cache.update(old_cache)
            CURRENT_SCOPE.reset(token)
