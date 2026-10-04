"""Synthetic SQL-backed limits; no provider or transport calls."""
import asyncio
import os
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch

from cognition.initiative_policy import charge_delivery, private_reason, provider_slot
from cognition.runtime import CognitiveRuntime
from cognition.types import ContextKey
from tests.cognition.test_affect import AT
from tests.cognition.test_full_model import RecordedInterpreter


class InitiativeStatusTests(unittest.TestCase):
    def test_unknown_timezone_is_explained_without_assuming_a_zone(self):
        from bot.group_commands import policy_status
        from cognition.group_policy import GroupPolicy
        text = policy_status(GroupPolicy(mode='useful', execution='live', full_visibility=True), AT)
        self.assertIn('не задан', text)
        self.assertIn('/proactivity tz <IANA zone>', text)
        self.assertNotIn('разрешены', text)

    def test_quiet_status_reports_real_configured_zone(self):
        from bot.group_commands import policy_status
        from cognition.group_policy import GroupPolicy
        text = policy_status(GroupPolicy(mode='useful', full_visibility=True, timezone='America/New_York'), AT)
        self.assertIn('America/New_York', text)
        self.assertIn('тихие часы', text)


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class InitiativeLimitsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.at = AT
        self.runtime = await CognitiveRuntime(self.pool, RecordedInterpreter(), 'active', clock=lambda: self.at).initialize(False)
        self.cids = [await self.runtime.ensure_context(ContextKey('arti', n)) for n in range(1, 7)]

    async def asyncTearDown(self):
        await self.runtime.close()
        await self.db.__aexit__(None, None, None)

    def turn(self, index=0, owner=1, **limits):
        return NS(context_id=self.cids[index], initiative=dict(owner_id=owner, context_daily=2, spacing_seconds=3600, **limits))

    async def charge(self, turn, key):
        async with self.pool.acquire() as conn, conn.transaction():
            return await charge_delivery(conn, turn, key, self.at)

    async def test_context_spacing_and_owner_cross_context_spacing(self):
        self.assertTrue(await self.charge(self.turn(), 'one'))
        self.assertFalse(await self.charge(self.turn(owner=2), 'two'))
        self.assertFalse(await self.charge(self.turn(1), 'three'))
        self.at += timedelta(hours=1)
        self.assertTrue(await self.charge(self.turn(1), 'three'))

    async def test_context_daily_limit(self):
        self.assertTrue(await self.charge(self.turn(owner=1), 'one'))
        self.at += timedelta(hours=2)
        self.assertTrue(await self.charge(self.turn(owner=2), 'two'))
        self.at += timedelta(hours=2)
        self.assertFalse(await self.charge(self.turn(owner=3), 'three'))

    async def test_owner_daily_limit_shared_across_contexts(self):
        for i in range(3):
            self.assertTrue(await self.charge(self.turn(i), str(i)))
            self.at += timedelta(hours=2)
        self.assertFalse(await self.charge(self.turn(3), 'four'))

    async def test_global_burst_is_bounded_across_owners(self):
        for i in range(3):
            self.assertTrue(await self.charge(self.turn(i, i+1), str(i)))
        self.assertFalse(await self.charge(self.turn(3, 4), 'four'))
        self.at += timedelta(minutes=1, seconds=1)
        self.assertTrue(await self.charge(self.turn(3, 4), 'four'))

    async def test_unknown_or_retry_attempt_identity_is_charged_once(self):
        self.assertTrue(await self.charge(self.turn(), 'same:message:1'))
        self.assertTrue(await self.charge(self.turn(), 'same:message:1'))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_initiative_charges'), 1)

    async def test_ordinary_reply_and_reminder_never_consume_quota(self):
        self.assertTrue(await self.charge(NS(context_id=self.cids[0]), 'requested'))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_initiative_charges'), 0)

    async def test_rollback_removes_unsent_charge(self):
        async with self.pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            self.assertTrue(await charge_delivery(conn, self.turn(), 'one', self.at))
            await tx.rollback()
        self.assertTrue(await self.charge(self.turn(), 'two'))

    async def test_restart_keeps_delivery_budget(self):
        self.assertTrue(await self.charge(self.turn(), 'one'))
        other = await CognitiveRuntime(self.pool, RecordedInterpreter(), 'active', clock=lambda: self.at).initialize(False)
        try:
            self.assertFalse(await self.charge(NS(context_id=self.cids[1], initiative=dict(owner_id=1)), 'two'))
        finally:
            await other.close()

    async def test_delivery_lock_contention_does_not_wait(self):
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('arti:initiative:delivery',0))")
            started = time.monotonic()
            self.assertFalse(await self.charge(self.turn(), 'one'))
            self.assertLess(time.monotonic()-started, .5)

    async def test_expired_daily_window_reopens_budget(self):
        self.assertTrue(await self.charge(self.turn(), 'one'))
        self.at += timedelta(days=1, seconds=1)
        self.assertTrue(await self.charge(self.turn(), 'two'))

    async def test_unknown_timezone_fails_closed(self):
        async with self.pool.acquire() as conn:
            self.assertEqual(await private_reason(conn, 1, AT), 'unknown_timezone')
            await conn.execute("INSERT INTO arti_organizer_preferences VALUES(1,'Not/A_Zone')")
            self.assertEqual(await private_reason(conn, 1, AT), 'unknown_timezone')

    async def test_private_timezone_uses_dst_and_local_quiet_hours(self):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO arti_organizer_preferences VALUES(1,'America/New_York')")
            self.assertEqual(await private_reason(conn, 1, AT.replace(month=7, hour=12)), 'quiet_hours')
            self.assertIsNone(await private_reason(conn, 1, AT.replace(month=7, hour=13)))
            self.assertEqual(await private_reason(conn, 1, AT.replace(month=1, hour=13)), 'quiet_hours')
            self.assertIsNone(await private_reason(conn, 1, AT.replace(month=1, hour=14)))

    async def test_timezone_change_serializes_with_final_send_read(self):
        from organizer.repository import Repository
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO arti_organizer_preferences VALUES(1,'UTC')")
            async with conn.transaction():
                self.assertIsNone(await private_reason(conn, 1, AT))
                change = asyncio.create_task(Repository(self.pool).set_timezone(1, 1, 'America/New_York'))
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(change), .05)
            await asyncio.wait_for(change, 1)
            self.assertEqual(await private_reason(conn, 1, AT), 'quiet_hours')

    async def test_provider_global_concurrency_and_restart(self):
        async with provider_slot(self.runtime, self.cids[0], 1, 'group') as first:
            async with provider_slot(self.runtime, self.cids[1], 2, 'group') as second:
                self.assertTrue(first and second)
                restarted = NS(pool=self.pool, clock=lambda: self.at)
                async with provider_slot(restarted, self.cids[2], 3, 'continuation') as third:
                    self.assertFalse(third)
        async with provider_slot(self.runtime, self.cids[2], 3, 'continuation') as next_call:
            self.assertTrue(next_call)

    async def test_provider_abstention_or_failure_still_spends_call_quota(self):
        for _ in range(2):
            async with provider_slot(self.runtime, self.cids[0], 1, 'continuation', hourly_limit=2) as allowed:
                self.assertTrue(allowed)
        async with provider_slot(self.runtime, self.cids[0], 1, 'continuation', hourly_limit=2) as allowed:
            self.assertFalse(allowed)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_initiative_charges'), 0)

    async def test_provider_global_hourly_limit(self):
        with patch('cognition.initiative_policy.PROVIDER_HOURLY_LIMIT', 2):
            for i in range(2):
                async with provider_slot(self.runtime, self.cids[i], i+1, 'group') as allowed:
                    self.assertTrue(allowed)
            async with provider_slot(self.runtime, self.cids[2], 3, 'private') as allowed:
                self.assertFalse(allowed)

    async def test_provider_expired_crash_lease_recovers(self):
        async with self.pool.acquire() as conn:
            await conn.execute('''INSERT INTO cognitive_initiative_calls VALUES
                ('crashed-a',$1,1,'group',$2,$3,NULL),('crashed-b',$1,1,'group',$2,$3,NULL)''', self.cids[0], self.at, self.at+timedelta(seconds=30))
        async with provider_slot(self.runtime, self.cids[1], 2, 'group') as allowed:
            self.assertFalse(allowed)
        self.at += timedelta(seconds=31)
        async with provider_slot(self.runtime, self.cids[1], 2, 'group') as allowed:
            self.assertTrue(allowed)

    async def test_provider_pool_saturation_is_bounded(self):
        conns = [await self.pool.acquire() for _ in range(5)]
        try:
            started = time.monotonic()
            async with provider_slot(self.runtime, self.cids[0], 1, 'continuation') as allowed:
                self.assertFalse(allowed)
            self.assertLess(time.monotonic()-started, .8)
        finally:
            for conn in conns:
                await self.pool.release(conn)

    async def test_provider_cancel_releases_slot_but_not_hourly_charge(self):
        entered = asyncio.Event()
        async def work():
            async with provider_slot(self.runtime, self.cids[0], 1, 'group') as allowed:
                self.assertTrue(allowed)
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_initiative_calls'), 1)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_initiative_calls WHERE completed_at IS NULL'), 0)

    async def test_concurrent_final_attempts_cannot_overbook_owner(self):
        outcomes = await asyncio.gather(*(self.charge(self.turn(i), str(i)) for i in range(5)))
        self.assertEqual(sum(outcomes), 1)

    async def test_provider_lock_contention_is_nonblocking(self):
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('arti:initiative:providers',0))")
            started = time.monotonic()
            async with provider_slot(self.runtime, self.cids[0], 1, 'group') as allowed:
                self.assertFalse(allowed)
            self.assertLess(time.monotonic()-started, .5)

    async def test_upgrade_backfills_recent_attempts_without_charging_reminders(self):
        from cognition.repositories import ensure_schema
        from cognition.types import AudienceScope
        group, group_event, _ = await self.runtime.ingest(-20, 1, 'Synthetic group cause', 1,
            context=ContextKey('arti', -20, topic_id=0), audience=AudienceScope('group', -20, 0))
        private, private_event, _ = await self.runtime.ingest(1, 1, 'Synthetic private goal', 1,
            audience=AudienceScope('private', 1))
        async with self.pool.acquire() as conn, conn.transaction():
            # Only this test's disposable database is rewound to migration034.
            await conn.execute('DROP TABLE cognitive_initiative_calls, cognitive_initiative_charges')
            await conn.execute("DELETE FROM cognitive_schema_migrations WHERE name='035_initiative_limits.sql'")
            for kind in ('open_question', 'reminder'):
                outbox = await conn.fetchval('''INSERT INTO cognitive_outbox
                    (context_id,event_id,delivery_key,channel,status,payload,suppression_epoch)
                    VALUES($1,$2,$3,'message','delivered','{}',0) RETURNING id''', group, group_event, kind)
                await conn.execute('''INSERT INTO group_candidates
                    (context_id,candidate_key,kind,source_ids,status,created_at,due_at,expires_at,charged_at,outbox_id)
                    VALUES($1,$2,$2,$3,'delivered',NOW(),NOW(),NOW()+INTERVAL '1 day',NOW(),$4)''',
                    group, kind, [group_event], outbox)
            await self.runtime.memory._put(conn, private, 'intention', 'old-private-goal',
                dict(status='open', delivery_key='old-private-goal'), 1, [private_event])
            await conn.execute('''INSERT INTO cognitive_outbox
                (context_id,event_id,delivery_key,channel,status,payload,suppression_epoch)
                VALUES($1,$2,'old-private-goal:message:1','message','delivery_unknown','{}',0)''', private, private_event)
        await ensure_schema(self.pool)
        await ensure_schema(self.pool)
        async with self.pool.acquire() as conn:
            keys = await conn.fetch('SELECT delivery_key,owner_id FROM cognitive_initiative_charges ORDER BY delivery_key')
        self.assertEqual([(r['delivery_key'], r['owner_id']) for r in keys],
                         [('old-private-goal:message:1', 1), ('open_question', 1)])
