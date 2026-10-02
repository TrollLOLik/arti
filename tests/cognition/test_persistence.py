import asyncio
import os
import unittest
from dataclasses import replace
from datetime import timedelta

from cognition.affect import affect, appraise
from cognition.jobs import JobQueue
from cognition.repositories import CognitiveRepository, StaleRevision, SuppressedEvidence, ensure_schema
from cognition.types import ContextKey
from tests.cognition.test_affect import AT, appraisal, event, perception


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'set ARTI_TEST_DB=1 for disposable PostgreSQL')
class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        await ensure_schema(self.pool)
        self.repo = CognitiveRepository(self.pool)
        self.jobs = JobQueue(self.pool)

    async def asyncTearDown(self):
        await self.db.__aexit__(None, None, None)

    async def apply(self, ev, p=None):
        cid, eid = await self.repo.observe(ev)
        p = p or perception(ev, appraisal(evidence_ids=(ev.evidence.source_id,)))
        prior = await self.repo.state(cid)
        result = appraise(prior, ev, p)
        committed = await self.repo.commit(cid, eid, p, prior.revision, result)
        return cid, eid, committed

    async def test_additive_migration_repeat(self):
        await ensure_schema(self.pool)
        await ensure_schema(self.pool)
        async with self.pool.acquire() as conn:
            self.assertTrue(await conn.fetchval("SELECT to_regclass('memory_facts') IS NOT NULL"))

    async def test_registration_and_commit_idempotent_after_restart(self):
        ev = event()
        cid, eid, applied = await self.apply(ev)
        self.assertTrue(applied)
        self.repo = CognitiveRepository(self.pool)
        self.assertEqual((cid, eid), await self.repo.observe(ev))
        state = await self.repo.state(cid)
        p = perception(ev, appraisal())
        self.assertFalse(await self.repo.commit(cid, eid, p, state.revision, state))
        self.assertEqual(state, await self.repo.state(cid))

    async def test_stale_result_rejected(self):
        e1, e2 = event(), event('e2', at=AT + timedelta(seconds=1))
        cid, eid1 = await self.repo.observe(e1)
        _, eid2 = await self.repo.observe(e2)
        state = await self.repo.state(cid)
        p1, p2 = perception(e1, appraisal()), perception(e2, appraisal(evidence_ids=('e2',)))
        result1, result2 = appraise(state, e1, p1), appraise(state, e2, p2)
        self.assertTrue(await self.repo.commit(cid, eid1, p1, 0, result1))
        with self.assertRaises(StaleRevision):
            await self.repo.commit(cid, eid2, p2, 0, result2)

    async def test_cannot_commit_arbitrary_mood(self):
        ev = event()
        cid, eid = await self.repo.observe(ev)
        prior = await self.repo.state(cid)
        p = perception(ev, appraisal())
        result = replace(appraise(prior, ev, p), mood_valence_latent=42)
        with self.assertRaises(ValueError):
            await self.repo.commit(cid, eid, p, 0, result)

    async def test_event_identity_cannot_change_payload(self):
        ev = event()
        await self.repo.observe(ev)
        with self.assertRaises(ValueError):
            await self.repo.observe(replace(ev, text='Changed source text'))

    async def test_contexts_are_independent(self):
        cid1, _, _ = await self.apply(event())
        cid2, _ = await self.repo.observe(event(context=ContextKey('arti', 10, 'rp', 'scene-a')))
        cid3, _ = await self.repo.observe(event(context=ContextKey('arti', 11)))
        self.assertNotEqual(cid1, cid2)
        self.assertEqual((await self.repo.state(cid2)).episodes, ())
        self.assertEqual((await self.repo.state(cid3)).episodes, ())

    async def test_forgetting_rebuilds_affect_and_erases_all_derivatives(self):
        cid, eid, _ = await self.apply(event())
        aid = await self.repo.artifact(cid, 'episode', {'private':'ERASE_ME'}, 1, [eid])
        bid = await self.repo.artifact(cid, 'belief', {'private':'ERASE_ME'}, 1, [], [aid])
        jid = await self.jobs.enqueue(cid, eid)
        claimed = await self.jobs.claim()
        before = await self.repo.state(cid)
        result = await self.repo.forget(cid, 'e1', 1)
        self.assertEqual(result, {'events':1, 'artifacts':2})
        after = await self.repo.state(cid)
        self.assertNotEqual(affect(before), affect(after))
        self.assertEqual(after.episodes, ())
        async with self.pool.acquire() as conn:
            self.assertEqual(0, await conn.fetchval('SELECT count(*) FROM cognitive_events WHERE payload IS NOT NULL OR perception IS NOT NULL OR fingerprint IS NOT NULL'))
            self.assertEqual(0, await conn.fetchval('SELECT count(*) FROM cognitive_artifacts WHERE payload IS NOT NULL'))
            self.assertEqual('cancelled', await conn.fetchval('SELECT status FROM cognitive_jobs WHERE id=$1', jid))
        self.assertFalse(await self.jobs.finish(jid, claimed['lease_token']))
        with self.assertRaises(SuppressedEvidence):
            await self.repo.observe(event())
        with self.assertRaises(SuppressedEvidence):
            await self.repo.artifact(cid, 'belief', {'private':'ERASE_ME'}, 1, [], [bid])

    async def test_forgetting_one_owner_keeps_another(self):
        cid, eid, _ = await self.apply(event())
        other = event('e2', at=AT + timedelta(minutes=1))
        other = replace(other, evidence=replace(other.evidence, owner_id=2), actor_id=2)
        p = perception(other, appraisal(congruence=.8, target_id=2, norm_violation=0., evidence_ids=('e2',)))
        await self.apply(other, p)
        self.assertEqual({'events':0, 'artifacts':0}, await self.repo.forget(cid, 'e1', 2))
        await self.repo.forget(cid, 'e1', 1)
        state = await self.repo.state(cid)
        self.assertTrue(state.episodes)
        self.assertTrue(all(ep.cause_id == 'e2' for ep in state.episodes))

    async def test_forgetting_blocks_worker_commit(self):
        ev = event()
        cid, eid = await self.repo.observe(ev)
        old = await self.repo.state(cid)
        p = perception(ev, appraisal())
        pending = appraise(old, ev, p)
        await self.repo.forget(cid, 'e1', 1)
        with self.assertRaises(SuppressedEvidence):
            await self.repo.commit(cid, eid, p, old.revision, pending)

    async def test_forgetting_suppresses_same_evidence_under_new_id(self):
        cid, eid, _ = await self.apply(event())
        await self.repo.observe(event('e2', group='e1', at=AT + timedelta(minutes=1)))
        self.assertEqual(2, (await self.repo.forget(cid, 'e1', 1))['events'])
        with self.assertRaises(SuppressedEvidence):
            await self.repo.observe(event('e3', group='e1', at=AT + timedelta(minutes=2)))

    async def test_provenance_cross_context_and_empty_rejected(self):
        cid, eid = await self.repo.observe(event())
        cid2, eid2 = await self.repo.observe(event('e2', context=ContextKey('arti', 11)))
        with self.assertRaises(SuppressedEvidence):
            await self.repo.artifact(cid, 'belief', {}, 1, [eid2])
        with self.assertRaises(ValueError):
            await self.repo.artifact(cid, 'belief', {}, 1, [])

    async def test_concurrent_job_claim_is_exclusive(self):
        cid, eid = await self.repo.observe(event())
        await self.jobs.enqueue(cid, eid)
        claims = await asyncio.gather(*(self.jobs.claim() for _ in range(8)))
        self.assertEqual(sum(c is not None for c in claims), 1)

    async def test_context_allows_only_one_running_job(self):
        cid, eid = await self.repo.observe(event())
        _, eid2 = await self.repo.observe(event('e2', at=AT + timedelta(seconds=1)))
        await self.jobs.enqueue(cid,eid)
        await self.jobs.enqueue(cid,eid2)
        claims = await asyncio.gather(*(self.jobs.claim() for _ in range(8)))
        self.assertEqual(sum(c is not None for c in claims),1)
        row = next(c for c in claims if c)
        await self.jobs.finish(row['id'],row['lease_token'])
        self.assertIsNotNone(await self.jobs.claim())

    async def test_later_observation_waits_for_prior_retry(self):
        cid, eid = await self.repo.observe(event())
        _, eid2 = await self.repo.observe(event('e2', at=AT + timedelta(seconds=1)))
        await self.jobs.enqueue(cid,eid)
        await self.jobs.enqueue(cid,eid2)
        row = await self.jobs.claim()
        self.assertEqual(row['event_id'],eid)
        await self.jobs.fail(row['id'],row['lease_token'],'timeout')
        self.assertIsNone(await self.jobs.claim())

    async def test_expired_lease_cannot_finish_new_attempt(self):
        cid, eid = await self.repo.observe(event())
        jid = await self.jobs.enqueue(cid, eid)
        a = await self.jobs.claim()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_jobs SET lease_until=NOW()-INTERVAL '1 second' WHERE id=$1", jid)
            await conn.execute("UPDATE cognitive_contexts SET worker_lease_until=NOW()-INTERVAL '1 second' WHERE id=$1", cid)
        b = await self.jobs.claim()
        self.assertNotEqual(a['lease_token'], b['lease_token'])
        self.assertFalse(await self.jobs.finish(jid, a['lease_token']))
        self.assertTrue(await self.jobs.finish(jid, b['lease_token']))

    async def test_dead_letter_after_last_lease_crash(self):
        cid, eid = await self.repo.observe(event())
        jid = await self.jobs.enqueue(cid, eid, max_attempts=1)
        await self.jobs.claim()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_jobs SET lease_until=NOW()-INTERVAL '1 second' WHERE id=$1", jid)
            await conn.execute("UPDATE cognitive_contexts SET worker_lease_until=NOW()-INTERVAL '1 second' WHERE id=$1", cid)
        self.assertIsNone(await self.jobs.claim())
        async with self.pool.acquire() as conn:
            self.assertEqual('dead', await conn.fetchval('SELECT status FROM cognitive_jobs WHERE id=$1', jid))

    async def test_job_errors_do_not_retain_text(self):
        cid, eid = await self.repo.observe(event())
        jid = await self.jobs.enqueue(cid, eid)
        row = await self.jobs.claim()
        with self.assertRaises(ValueError):
            await self.jobs.fail(jid, row['lease_token'], 'private user text')
        self.assertTrue(await self.jobs.fail(jid, row['lease_token'], 'timeout'))
        self.assertIsNone(await self.jobs.claim())

    async def test_orchestrator_retry_does_not_repeat_interpretation(self):
        from unittest.mock import AsyncMock
        from cognition.orchestrator import CognitiveOrchestrator
        from cognition.interpreter import InterpretationResult
        ev = event()
        interpreter = AsyncMock()
        interpreter.interpret.return_value = InterpretationResult(perception(ev,appraisal()),.1,1,10,10,0)
        controller = CognitiveOrchestrator(self.repo,interpreter)
        first = await controller.observe(ev)
        second = await controller.observe(ev)
        self.assertEqual(first,second)
        interpreter.interpret.assert_awaited_once()

    async def test_interpreter_failure_has_no_emotional_effect(self):
        from unittest.mock import AsyncMock
        from cognition.orchestrator import CognitiveOrchestrator
        from cognition.interpreter import InterpreterFailure
        ev = event()
        interpreter = AsyncMock()
        interpreter.interpret.side_effect = InterpreterFailure('timeout')
        with self.assertRaises(InterpreterFailure):
            await CognitiveOrchestrator(self.repo,interpreter).observe(ev)
        cid,_ = await self.repo.observe(ev)
        state = await self.repo.state(cid)
        self.assertEqual(state.episodes,())
        self.assertEqual(state.revision,0)


if __name__ == '__main__':
    unittest.main()
