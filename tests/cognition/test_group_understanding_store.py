"""Provider-free integration tests of public semantic storage and race fences."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
import os
import unittest
from unittest.mock import patch

from cognition.group_understanding import parse_understanding
from cognition.group_understanding_store import ConversationUnderstanding, INPUT_BYTE_LIMIT
from cognition.runtime import CognitiveRuntime, CURRENT_TURN
from cognition.scope import CURRENT_SCOPE
from cognition.serialization import dump, object_value
from cognition.types import AudienceScope
from tests.cognition.test_affect import AT
from tests.cognition.test_full_model import RecordedInterpreter
from tests.cognition import test_public_memory as public_memory_tests


def recorded_result(messages):
    """Small transparent recorded contract, deliberately ignores noisy inputs."""
    first = next((m for m in messages if m['text'] == 'Plan?'), None)
    if first is None:
        return dict(threads=[], links=[], items=[])
    def span(m):
        return dict(source_id=m['source_id'], start=0, end=len(m['text']), quote=m['text'])
    updates = [dict(source_id=m['source_id'], status={'Solved.': 'resolved', 'Reopen.': 'reopened'}[m['text']],
                    actor_id=m['owner_id'] if m['sender_kind'] == 'user' else None,
                    attribution='speaker' if m['sender_kind'] == 'user' else 'unknown', confidence=.99, evidence=[span(m)])
               for m in messages if m['text'] in ('Solved.', 'Reopen.')]
    raw = dict(threads=[dict(thread_id=first['source_id'], label='Planning', confidence=.99, evidence=[span(first)])],
        links=[], items=[dict(kind='question', thread_id=first['source_id'], origin_source_id=first['source_id'],
          summary='A participant asks about the plan.', actor_id=first['owner_id'], attribution='speaker',
          status='open', confidence=.99, evidence=[span(first)], updates=updates)])
    return parse_understanding(raw, messages)


class Analyzer:
    def __init__(self):
        self.calls = []
        self.entered = None
        self.release = None
        self.fail = False

    async def analyze(self, messages, chat_id):
        self.calls.append([dict(m) for m in messages])
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        if self.fail:
            raise RuntimeError('offline provider')
        return recorded_result(messages)


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class GroupUnderstandingStoreTests(unittest.IsolatedAsyncioTestCase):
    observe = public_memory_tests.PublicMemoryTests.observe
    execute = public_memory_tests.PublicMemoryTests.execute

    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.at = AT
        self.runtime = await CognitiveRuntime(self.pool, RecordedInterpreter(), clock=lambda: self.at).initialize(False)
        self.analyzer = Analyzer()
        self.store = ConversationUnderstanding(self.runtime, self.analyzer)
        self.scope_token = CURRENT_SCOPE.set(None)
        self.turn_token = CURRENT_TURN.set(None)

    async def asyncTearDown(self):
        CURRENT_SCOPE.reset(self.scope_token)
        CURRENT_TURN.reset(self.turn_token)
        await self.runtime.close()
        await self.db.__aexit__(None, None, None)

    async def scalar(self, sql, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(sql, *args)

    async def read(self, cid, context, requester=None):
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)', context.chat_id)
            return await self.store.read_locked(conn, cid, context, self.at, requester)

    async def test_public_only_scope_complete_input_lineage_and_no_ingress_call(self):
        cid, context, source = await self.observe('Plan?')
        _, _, noise = await self.observe('Uncited but consulted.', message=2, owner=2)
        await self.observe('OTHER_TOPIC', topic=6)
        await self.observe('OTHER_CHAT', chat=-711)
        await self.runtime.ingest(700, 1, 'PRIVATE', 1, audience=AudienceScope('private', 700, -1))
        await self.runtime.ingest(-710, 1, 'UNOBSERVED', 90, context=context, audience=AudienceScope('topic', -710, 5))
        self.assertEqual(self.analyzer.calls, [])
        self.assertEqual(await self.store.refresh(cid), 1)
        packet = await self.read(cid, context)
        self.assertEqual([m['text'] for m in self.analyzer.calls[0]], ['Plan?', 'Uncited but consulted.'])
        self.assertEqual(packet['source_event_ids'], [source['id'], noise['id']])
        self.assertEqual(packet['payload']['items'][0]['status'], 'open')
        self.assertTrue(packet['current'])
        self.assertFalse(packet['history_complete'])
        for wrong in (replace(context, topic_id=6), replace(context, chat_id=-711), replace(context, persona_id='other')):
            self.assertIsNone((await self.read(cid, wrong))['payload'])
        # A denied request for the wrong context must not erase the correct one.
        self.assertEqual(await self.store.refresh(cid), 0)
        await self.runtime.groups.policies.opt_out(-710, 55, True)
        self.assertIsNone((await self.read(cid, context, requester=55))['payload'])
        self.assertIsNotNone((await self.read(cid, context, requester=1))['payload'])

    async def test_sequential_rollover_restart_and_generation_preserve_old_open_anchor(self):
        cid, context, first = await self.observe('Plan?')
        for mid in range(2, 102):
            self.at += timedelta(seconds=1)
            await self.observe(f'Unrelated conversation {mid}', message=mid, owner=2)
        generations = []
        for _ in range(5):
            self.store = ConversationUnderstanding(self.runtime, self.analyzer)
            self.assertEqual(await self.store.refresh(cid), 1)
            packet = await self.read(cid, context)
            generations.append(packet['generation'])
        self.assertEqual(len(set(generations)), 5)
        self.assertEqual(len(packet['source_event_ids']), 101)
        self.assertEqual(packet['payload']['items'][0]['origin_source_id'], first['source_id'])
        self.assertTrue(packet['current'])
        self.assertEqual(await self.store.refresh(cid), 0)
        self.assertTrue(all(len(batch) <= 72 for batch in self.analyzer.calls))
        self.assertTrue(all('summary' not in message for batch in self.analyzer.calls for message in batch))
        # Even an old *uncited* input influenced selection. Erase it all.
        uncited = packet['source_event_ids'][1]
        await self.execute('DELETE FROM group_observations WHERE context_id=$1 AND event_id=$2', cid, uncited)
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state WHERE context_id=$1', cid), 0)
        self.assertIsNone((await self.read(cid, context))['payload'])
        self.assertEqual(await self.store.refresh(cid), 1)
        replay = self.analyzer.calls[-1]
        self.assertEqual(replay[0]['text'], 'Plan?')
        self.assertNotIn('Unrelated conversation 2', [m['text'] for m in replay])

    async def test_append_only_churn_commits_prefix_and_two_workers_make_one_call(self):
        cid, context, source = await self.observe('Plan?')
        self.analyzer.entered, self.analyzer.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.store.refresh(cid))
        await self.analyzer.entered.wait()
        other = Analyzer()
        self.assertEqual(await ConversationUnderstanding(self.runtime, other).refresh(cid), 0)
        self.assertEqual(other.calls, [])
        self.at += timedelta(seconds=1)
        await self.observe('New message after input snapshot.', message=2, owner=2)
        self.analyzer.release.set()
        self.assertEqual(await task, 1)
        packet = await self.read(cid, context)
        self.assertEqual(packet['as_of_event_id'], source['id'])
        self.assertFalse(packet['current'])
        first_generation = packet['generation']
        self.assertEqual(await self.store.refresh(cid), 1)
        packet = await self.read(cid, context)
        self.assertTrue(packet['current'])
        self.assertNotEqual(first_generation, packet['generation'])

    async def test_nonappend_revision_churn_retries_without_cursor_advance(self):
        cid, context, _ = await self.observe('Plan?')
        self.analyzer.entered, self.analyzer.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.store.refresh(cid))
        await self.analyzer.entered.wait()
        await self.execute('UPDATE group_topic_runtime SET revision=revision+1 WHERE context_id=$1', cid)
        self.analyzer.release.set()
        self.assertEqual(await task, 0)
        self.assertEqual(await self.scalar('SELECT cursor_event_id FROM group_understanding_state WHERE context_id=$1', cid), 0)
        self.assertEqual(await self.store.refresh(cid), 1)

    async def test_before_dispatch_mutation_never_reaches_analyzer(self):
        cid, context, _ = await self.observe('Plan?')
        @asynccontextmanager
        async def slot(*args, **kwargs):
            await self.execute('UPDATE group_observations SET payload=NULL WHERE context_id=$1', cid)
            yield True
        with patch('cognition.group_understanding_store.provider_slot', slot):
            self.assertEqual(await self.store.refresh(cid), 0)
        self.assertEqual(self.analyzer.calls, [])
        self.assertIsNone((await self.read(cid, context))['payload'])

    async def test_delayed_results_cannot_restore_revoked_or_changed_input(self):
        for index, action in enumerate(('delete', 'edit', 'optout_restore', 'reset', 'epoch', 'retention',
                                       'response', 'dependency', 'edge_aba', 'policy'), 1):
            with self.subTest(action=action):
                self.at = AT
                cid, context, source = await self.observe('Plan?', chat=-800-index)
                analyzer = Analyzer()
                analyzer.entered, analyzer.release = asyncio.Event(), asyncio.Event()
                store = ConversationUnderstanding(self.runtime, analyzer)
                task = asyncio.create_task(store.refresh(cid))
                await analyzer.entered.wait()
                if action == 'delete':
                    await self.execute('DELETE FROM group_observations WHERE context_id=$1', cid)
                elif action == 'edit':
                    await self.observe('Changed question?', chat=context.chat_id, edited=True, at=self.at+timedelta(seconds=1))
                elif action == 'optout_restore':
                    await self.runtime.groups.policies.opt_out(context.chat_id, 1, True)
                    await self.runtime.groups.policies.opt_out(context.chat_id, 1, False)
                elif action == 'reset':
                    await self.execute('UPDATE cognitive_contexts SET history_after_event_id=$2 WHERE id=$1', cid, source['id'])
                elif action == 'epoch':
                    await self.execute('UPDATE cognitive_contexts SET suppression_epoch=suppression_epoch+1 WHERE id=$1', cid)
                elif action == 'retention':
                    self.at += timedelta(days=31)
                elif action == 'response':
                    await self.execute('INSERT INTO response_status(chat_id,enabled) VALUES($1,FALSE)', context.chat_id)
                elif action in ('dependency', 'edge_aba'):
                    _, dep, _ = await self.runtime.ingest(context.chat_id, 2, 'private support', 99, context=context,
                        audience=AudienceScope('private', context.chat_id, context.topic_id))
                    await self.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3)', cid, source['id'], dep)
                    if action == 'edge_aba':
                        await self.execute('DELETE FROM cognitive_event_dependencies WHERE context_id=$1', cid)
                else:
                    await self.runtime.groups.policies.set(context.chat_id, dict(retention_days=1))
                analyzer.release.set()
                self.assertEqual(await task, 0)
                self.assertIsNone((await self.read(cid, context))['payload'])
                self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state WHERE context_id=$1 AND payload IS NOT NULL', cid), 0)

    async def test_recursive_dependency_erasure_invalidates_uncited_lineage(self):
        cid, context, base = await self.observe('Raw supporting record.', owner=2)
        _, _, middle = await self.observe('Public derived record.', owner=3, message=2)
        _, _, question = await self.observe('Plan?', owner=1, message=3)
        await self.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3),($1,$4,$2)', cid, middle['id'], base['id'], question['id'])
        self.assertEqual(await self.store.refresh(cid), 1)
        await self.runtime.groups.policies.opt_out(context.chat_id, 2, True)
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)
        self.assertEqual(await self.store.refresh(cid), 0)
        self.assertIsNone((await self.read(cid, context))['payload'])

    async def test_late_observation_behind_cursor_invalidates_and_replays(self):
        context = await self.runtime.context(-710, 'default', 5)
        cid, eid, event = await self.runtime.ingest(-710, 2, 'Late source', 'late', context=context,
            audience=AudienceScope('topic', -710, 5))
        await self.observe('Plan?', message=2)
        self.assertEqual(await self.store.refresh(cid), 1)
        payload = dict(message_id=1, owner_id=2, text='Late source', reply_to_id=None, sender_kind='user',
            sender_ref=None, directed=False, is_bot=False, addressed_elsewhere=False)
        await self.execute('''INSERT INTO group_observations(context_id,event_id,message_id,owner_id,payload,observed_at)
            VALUES($1,$2,1,2,$3::jsonb,$4)''', cid, eid, dump(payload), event.observed_at)
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)
        self.assertEqual(await self.store.refresh(cid), 1)
        self.assertEqual([m['text'] for m in self.analyzer.calls[-1]], ['Late source', 'Plan?'])

    async def test_failures_timeout_and_restart_lease_do_not_advance(self):
        cid, context, source = await self.observe('Plan?')
        self.analyzer.fail = True
        self.assertEqual(await self.store.refresh(cid), 0)
        self.assertEqual(await self.scalar('SELECT cursor_event_id FROM group_understanding_state'), 0)
        self.assertIsNone(await self.scalar('SELECT lease_token FROM group_understanding_state'))
        self.analyzer.fail = False
        self.analyzer.entered, self.analyzer.release = asyncio.Event(), asyncio.Event()
        with patch('cognition.group_understanding_store.ANALYZE_TIMEOUT_SECONDS', .03):
            self.assertEqual(await self.store.refresh(cid), 0)
        self.assertEqual(await self.scalar('SELECT cursor_event_id FROM group_understanding_state'), 0)
        await self.execute("UPDATE group_understanding_state SET lease_token='crashed-worker',lease_until=$1", self.at+timedelta(seconds=30))
        self.assertEqual(await ConversationUnderstanding(self.runtime, Analyzer()).refresh(cid), 0)
        self.at += timedelta(seconds=31)
        self.store = ConversationUnderstanding(self.runtime, Analyzer())
        self.assertEqual(await self.store.refresh(cid), 1)
        self.assertEqual((await self.read(cid, context))['as_of_event_id'], source['id'])

    async def test_hourly_limit_is_durable_across_instances_and_failures(self):
        cid, context, _ = await self.observe('Plan?')
        self.analyzer.fail = True
        for _ in range(8):
            await ConversationUnderstanding(self.runtime, self.analyzer).refresh(cid)
        self.assertEqual(len(self.analyzer.calls), 6)
        self.assertEqual(await self.scalar('SELECT cursor_event_id FROM group_understanding_state'), 0)
        self.at += timedelta(hours=1, seconds=1)
        self.analyzer.fail = False
        self.assertEqual(await self.store.refresh(cid), 1)

    async def test_input_bytes_and_unicode_truncation_are_explicit(self):
        for mid in range(1, 25):
            cid, context, _ = await self.observe('日本語🦉' * 1600, message=mid, owner=mid)
        self.assertEqual(await self.store.refresh(cid), 1)
        import json
        batch = self.analyzer.calls[0]
        self.assertLessEqual(len(json.dumps(batch, ensure_ascii=False).encode()), INPUT_BYTE_LIMIT)
        self.assertEqual(len(batch), 24)
        self.assertTrue(all(m['text_truncated'] for m in batch))
        self.assertEqual(len((await self.read(cid, context))['source_event_ids']), 24)

    async def test_lineage_cap_starts_independent_raw_prefix_with_explicit_loss(self):
        for mid in range(1, 49):
            cid, context, _ = await self.observe('Plan?' if mid == 1 else f'Noise {mid}', message=mid)
        with patch('cognition.group_understanding_store.LINEAGE_LIMIT', 30):
            self.assertEqual(await self.store.refresh(cid), 1)
            self.assertEqual(await self.store.refresh(cid), 1)
        packet = await self.read(cid, context)
        self.assertTrue(packet['lineage_reset'])
        self.assertEqual(len(packet['source_event_ids']), 24)
        self.assertNotIn('Plan?', [m['text'] for m in self.analyzer.calls[-1]])
        self.assertEqual(packet['payload']['items'], [])

    async def test_fair_rotation_survives_locked_chat_and_worker_restart(self):
        first, blocked, _ = await self.observe('Plan?')
        second, context, _ = await self.observe('Plan?', chat=-711)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)', blocked.chat_id)
            self.assertEqual(await self.store.sweep(limit=1), 0)
            self.store = ConversationUnderstanding(self.runtime, self.analyzer)
            self.assertEqual(await self.store.sweep(limit=1), 1)
            self.assertIsNotNone((await self.read(second, context))['payload'])
        self.assertEqual(await self.store.sweep(limit=1), 1)

    async def test_exhausted_pool_returns_with_bounded_wait_and_then_recovers(self):
        cid, context, _ = await self.observe('Plan?')
        held = [await self.pool.acquire() for _ in range(self.pool.get_max_size())]
        try:
            start = asyncio.get_running_loop().time()
            self.assertEqual(await self.store.refresh(cid), 0)
            self.assertLess(asyncio.get_running_loop().time() - start, 1.5)
        finally:
            for conn in held:
                await self.pool.release(conn)
        self.assertEqual(await self.store.refresh(cid), 1)

    async def test_bot_anonymous_and_speaker_ownership_remain_exact(self):
        cid, context, _ = await self.observe('Plan?', owner=1)
        await self.observe('Solved.', owner=99, message=2, is_bot=True)
        await self.observe('An anonymous report.', owner=None, message=3, sender_kind='chat', sender_ref='channel:-101')
        self.assertEqual(await self.store.refresh(cid), 1)
        batch = self.analyzer.calls[-1]
        self.assertEqual([(m['owner_id'], m['sender_kind']) for m in batch], [(1, 'user'), (99, 'bot'), (None, 'chat')])
        self.assertEqual((await self.read(cid, context))['payload']['items'][0]['status'], 'open')

    async def test_full_untruncated_source_hash_and_observation_hash_guard_hidden_text(self):
        cid, context, source = await self.observe('Plan?')
        _, _, long_source = await self.observe('Background ' + 'x' * 6000, message=2)
        self.assertEqual(await self.store.refresh(cid), 1)
        raw = object_value(long_source['payload'])
        raw['text'] = raw['text'][:-1] + 'y'
        await self.execute('UPDATE cognitive_events SET payload=$2::jsonb WHERE id=$1', long_source['id'], dump(raw))
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)
        self.assertEqual(await self.store.refresh(cid), 1)
        await self.execute("UPDATE group_observations SET payload=jsonb_set(payload,'{directed}','false'::jsonb) WHERE context_id=$1 AND message_id=1", cid)
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)
        self.assertIsNone((await self.read(cid, context))['payload'])

    async def test_read_erases_expired_snapshot_and_corrupt_support_metadata(self):
        cid, context, _ = await self.observe('Plan?')
        self.assertEqual(await self.store.refresh(cid), 1)
        await self.execute('DELETE FROM group_understanding_dependencies WHERE context_id=$1', cid)
        self.assertIsNone((await self.read(cid, context))['payload'])
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)
        self.assertEqual(await self.store.refresh(cid), 1)
        self.at += timedelta(days=31)
        self.assertIsNone((await self.read(cid, context))['payload'])
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)

    async def test_scene_and_authority_transitions_erase_persisted_content(self):
        cid, context, _ = await self.observe('Plan?')
        self.assertEqual(await self.store.refresh(cid), 1)
        await self.execute("UPDATE cognitive_contexts SET authority='shadow' WHERE id=$1", cid)
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)
        self.assertEqual(await self.store.refresh(cid), 0)
        await self.execute("UPDATE cognitive_contexts SET authority='active' WHERE id=$1", cid)
        self.assertEqual(await self.store.refresh(cid), 1)
        await self.execute('UPDATE cognitive_contexts SET rebuilding=TRUE WHERE id=$1', cid)
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)

    async def test_cancelled_provider_releases_context_and_shared_admission_leases(self):
        cid, context, _ = await self.observe('Plan?')
        self.analyzer.entered, self.analyzer.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.store.refresh(cid))
        await self.analyzer.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(await self.scalar('SELECT lease_token FROM group_understanding_state'))
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_initiative_calls WHERE completed_at IS NULL'), 0)
        self.assertEqual(await ConversationUnderstanding(self.runtime, Analyzer()).refresh(cid), 1)

    async def test_transitive_foreign_owner_ancestors_are_returned_even_outside_new_prefix(self):
        cid, context, base = await self.observe('Raw ancestor owned by Alice.', owner=1)
        _, _, middle = await self.observe('Independent human report owned by Bob.', owner=2, message=2)
        await self.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3)', cid, middle['id'], base['id'])
        self.assertEqual(await self.store.refresh(cid), 1)
        _, _, question = await self.observe('Plan?', owner=3, message=3)
        await self.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3)', cid, question['id'], middle['id'])
        # Force independent compression. The current model call sees only the
        # question; downstream provenance still contains both foreign owners.
        with patch('cognition.group_understanding_store.LINEAGE_LIMIT', 3):
            await self.execute("UPDATE group_understanding_state SET payload=NULL,input_event_ids='{}' WHERE context_id=$1", cid)
            await self.execute('DELETE FROM group_understanding_dependencies WHERE context_id=$1', cid)
            self.assertEqual(await self.store.refresh(cid), 1)
        packet = await self.read(cid, context)
        self.assertEqual(packet['input_event_ids'], [question['id']])
        self.assertEqual(packet['source_event_ids'], [base['id'], middle['id'], question['id']])
        await self.execute('UPDATE cognitive_events SET suppressed_at=$2 WHERE id=$1', base['id'], self.at)
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)

    async def test_malformed_candidate_is_skipped_before_provider_and_does_not_pin_prefix(self):
        cid, context, invalid = await self.observe('Malformed observation.', message=1)
        await self.execute("UPDATE group_observations SET payload=jsonb_set(payload,'{reply_to_id}','true'::jsonb) WHERE context_id=$1", cid)
        _, _, valid = await self.observe('Plan?', message=2)
        self.assertEqual(await self.store.refresh(cid), 1)
        self.assertEqual([m['text'] for m in self.analyzer.calls[-1]], ['Plan?'])
        self.assertEqual((await self.read(cid, context))['as_of_event_id'], valid['id'])
        self.assertEqual(await self.store.refresh(cid), 0)

    async def test_old_schema_is_hidden_and_replayed(self):
        cid, context, _ = await self.observe('Plan?')
        self.assertEqual(await self.store.refresh(cid), 1)
        await self.execute("UPDATE group_understanding_state SET schema_version='obsolete' WHERE context_id=$1", cid)
        self.assertIsNone((await self.read(cid, context))['payload'])
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)
        self.assertEqual(await self.store.refresh(cid), 1)

    async def test_terminal_status_survives_later_model_omission_or_optimistic_regeneration(self):
        cid, context, _ = await self.observe('Plan?')
        self.at += timedelta(seconds=1)
        await self.observe('Solved.', message=2)
        self.assertEqual(await self.store.refresh(cid), 1)
        for mid, variant in [(3, 'empty'), (4, 'optimistic')]:
            self.at += timedelta(seconds=1)
            await self.observe('Unrelated noise.', message=mid, owner=2)
            async def drift(messages, chat_id):
                if variant == 'empty':
                    return dict(threads=[], links=[], items=[])
                return recorded_result([m for m in messages if m['text'] != 'Solved.'])
            with patch.object(self.analyzer, 'analyze', side_effect=drift):
                self.assertEqual(await self.store.refresh(cid), 1)
            self.assertEqual((await self.read(cid, context))['payload']['items'][0]['status'], 'resolved')
        self.at += timedelta(seconds=1)
        await self.observe('Reopen.', message=5)
        self.assertEqual(await self.store.refresh(cid), 1)
        self.assertEqual((await self.read(cid, context))['payload']['items'][0]['status'], 'reopened')

    async def test_recorded_russian_episode_survives_rollover_and_uncited_noise_erasure(self):
        from tools.evaluate_group_understanding import load_fixture
        case = next(case for case in load_fixture()['episodes'] if case['id'] == 'ru_resolved_then_reopened')
        source_map = {}
        for message in case['messages']:
            self.at += timedelta(seconds=1)
            cid, context, source = await self.observe(message['text'], owner=message['owner_id'], message=message['message_id'])
            source_map[message['source_id']] = source['source_id']
        def remap(value):
            if isinstance(value, str):
                return source_map.get(value, value)
            if isinstance(value, list):
                return [remap(item) for item in value]
            if isinstance(value, dict):
                return {key: remap(item) for key, item in value.items()}
            return value
        recording = remap(case['recorded_output'])
        # Recorded, hand-authored expected semantics. No paid/live inference.
        async def replay(messages, chat_id):
            return parse_understanding(recording, messages)
        self.analyzer.analyze = replay
        for mid in range(7, 103):
            self.at += timedelta(seconds=1)
            await self.observe(f'Unrelated noise {mid}.', owner=9, message=mid)
        for _ in range(5):
            self.assertEqual(await self.store.refresh(cid), 1)
        packet = await self.read(cid, context)
        self.assertEqual(packet['payload']['items'][0]['status'], 'reopened')
        self.assertEqual(len(packet['source_event_ids']), 102)
        # Noise affected prior extraction and subsequent anchor selection.
        await self.execute('DELETE FROM group_observations WHERE context_id=$1 AND message_id=10', cid)
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)

    async def test_readonly_snapshot_is_nonblocking_coherent_and_final_read_rechecks(self):
        cid, context, source = await self.observe('Plan?')
        self.assertEqual(await self.store.refresh(cid), 1)
        async with self.pool.acquire() as locked, locked.transaction():
            await locked.fetchval('SELECT id FROM cognitive_contexts WHERE id=$1 FOR UPDATE', cid)
            async with self.pool.acquire() as reader, reader.transaction(isolation='repeatable_read', readonly=True):
                packet = await asyncio.wait_for(self.store.read_snapshot(reader, cid, context, self.at), 1)
                self.assertIsNotNone(packet['payload'])
        async with self.pool.acquire() as reader, reader.transaction(isolation='repeatable_read', readonly=True):
            first = await self.store.read_snapshot(reader, cid, context, self.at)
            await self.execute('UPDATE cognitive_events SET suppressed_at=$2 WHERE id=$1', source['id'], self.at)
            snapshot = await self.store.read_snapshot(reader, cid, context, self.at)
            self.assertEqual(snapshot['generation'], first['generation'])
            self.assertIsNotNone(snapshot['payload'])
        self.assertIsNone((await self.read(cid, context))['payload'])

    async def test_readonly_stale_version_hides_without_mutating_until_locked_cleanup(self):
        cid, context, _ = await self.observe('Plan?')
        self.assertEqual(await self.store.refresh(cid), 1)
        await self.execute("UPDATE group_understanding_state SET schema_version='obsolete' WHERE context_id=$1", cid)
        async with self.pool.acquire() as reader, reader.transaction(isolation='repeatable_read', readonly=True):
            packet = await self.store.read_snapshot(reader, cid, context, self.at)
        self.assertIsNone(packet['payload'])
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 1)
        self.assertIsNone((await self.read(cid, context))['payload'])
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)

    async def test_transitive_lineage_cap_never_silently_drops_ancestor(self):
        cid, context, first = await self.observe('Ancestor one', owner=1)
        _, _, second = await self.observe('Ancestor two', owner=2, message=2)
        self.assertEqual(await self.store.refresh(cid), 1)
        _, _, root = await self.observe('Plan?', owner=3, message=3)
        await self.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3),($1,$2,$4)', cid, root['id'], first['id'], second['id'])
        await self.execute("UPDATE group_understanding_state SET payload=NULL,input_event_ids='{}' WHERE context_id=$1", cid)
        await self.execute('DELETE FROM group_understanding_dependencies WHERE context_id=$1', cid)
        calls = len(self.analyzer.calls)
        with patch('cognition.group_understanding_store.LINEAGE_LIMIT', 2):
            self.assertEqual(await self.store.refresh(cid), 0)
        self.assertEqual(len(self.analyzer.calls), calls)
        self.assertEqual(await self.scalar('SELECT cursor_event_id FROM group_understanding_state'), root['id'])
        self.assertEqual(await self.scalar('SELECT reason FROM group_understanding_skips'), 'lineage_limit')
        _, _, healthy = await self.observe('Independent later source.', owner=4, message=4)
        with patch('cognition.group_understanding_store.LINEAGE_LIMIT', 2):
            self.assertEqual(await self.store.refresh(cid), 1)
        packet = await self.read(cid, context)
        self.assertEqual(packet['source_event_ids'], [healthy['id']])
        self.assertEqual(packet['skipped_source_count'], 1)
        self.assertTrue(packet['coverage_gaps'])
        self.assertFalse(packet['current'])
        # Removing the excessive closure starts clean replay, rather than
        # restoring the skipped source from stale generated material.
        await self.execute('DELETE FROM cognitive_event_dependencies WHERE context_id=$1', cid)
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_skips'), 0)
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)

    async def test_recursive_dependency_cycle_is_bounded_and_fully_recorded(self):
        cid, context, first = await self.observe('Plan?')
        _, _, second = await self.observe('A human response.', owner=2, message=2)
        await self.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3),($1,$3,$2)', cid, first['id'], second['id'])
        self.assertEqual(await asyncio.wait_for(self.store.refresh(cid), 2), 1)
        self.assertEqual((await self.read(cid, context))['source_event_ids'], [first['id'], second['id']])
        await self.execute('DELETE FROM cognitive_event_dependencies WHERE context_id=$1', cid)
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_state'), 0)

    async def test_all_invalid_prefix_has_explicit_gap_and_later_valid_source_progresses(self):
        cid, context, invalid = await self.observe('Malformed directed metadata.')
        await self.execute("UPDATE group_observations SET payload=jsonb_set(payload,'{directed}','1'::jsonb) WHERE context_id=$1", cid)
        self.assertEqual(await self.store.refresh(cid), 0)
        self.assertEqual(self.analyzer.calls, [])
        packet = await self.read(cid, context)
        self.assertIsNone(packet['payload'])
        self.assertEqual(packet['as_of_event_id'], invalid['id'])
        self.assertTrue(packet['coverage_gaps'])
        self.assertEqual(packet['status'], 'incomplete')
        await self.observe('Plan?', message=2)
        self.assertEqual(await self.store.refresh(cid), 1)
        self.assertEqual((await self.read(cid, context))['payload']['items'][0]['status'], 'open')
        self.assertFalse((await self.read(cid, context))['current'])
        await self.execute("UPDATE group_observations SET payload=jsonb_set(payload,'{directed}','true'::jsonb) WHERE context_id=$1 AND message_id=1", cid)
        self.assertEqual(await self.scalar('SELECT count(*) FROM group_understanding_skips'), 0)
        self.assertEqual(await self.store.refresh(cid), 1)
        self.assertFalse((await self.read(cid, context))['coverage_gaps'])
