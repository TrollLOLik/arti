"""Disposable-DB regressions for complete, restartable private source indexing."""
import asyncio
import os
import time
import unittest

from cognition.runtime import CognitiveRuntime, CURRENT_TURN
from cognition.semantic import SemanticIndex, VERSION
from cognition.serialization import dump, object_value
from cognition.source_chunks import source_chunk_count, source_chunks
from tests.cognition.test_full_model import RecordedInterpreter


class Encoder:
    def __init__(self):
        self.batches = []

    async def encode(self, texts, timeout=.6):
        self.batches.append(list(texts))
        return [[1.] + [0.] * 383 for _ in texts]

    async def close(self):
        pass


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class SemanticCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.encoder = Encoder()
        self.index = SemanticIndex(self.pool, self.encoder)
        self.runtime = await CognitiveRuntime(self.pool, RecordedInterpreter(), semantic=self.index).initialize(False)

    async def asyncTearDown(self):
        CURRENT_TURN.set(None)
        await self.runtime.close()
        await self.db.__aexit__(None, None, None)

    async def add(self, text, message=1, owner=1, chat=801):
        cid, eid, event = await self.runtime.ingest(chat, owner, text, message)
        await self.runtime.process(cid, eid)
        async with self.pool.acquire() as conn:
            aid = await conn.fetchval("SELECT id FROM cognitive_artifacts WHERE kind='trace' AND payload->>'event_id'=$1", str(eid))
        return cid, eid, aid

    async def drain(self, index=None, batch_chunks=11):
        index = index or self.index
        calls = 0
        while await index.backfill(16, batch_chunks=batch_chunks):
            calls += 1
            self.assertLess(calls, 100, 'coverage must converge instead of resetting forever')
        return calls

    async def progress(self, aid):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow('SELECT * FROM cognitive_semantic_progress WHERE artifact_id=$1 AND embedding_model=$2', aid, VERSION)

    async def count(self, table):
        self.assertIn(table, ('cognitive_semantic_vectors', 'cognitive_semantic_progress'))
        async with self.pool.acquire() as conn:
            return await conn.fetchval(f'SELECT count(*) FROM {table}')

    async def delayed(self, mutate):
        original = self.index.encoder.encode
        entered, release = asyncio.Event(), asyncio.Event()
        async def encode(texts, timeout=5):
            entered.set()
            await release.wait()
            return await original(texts, timeout)
        self.index.encoder.encode = encode
        task = asyncio.create_task(self.index.backfill(batch_chunks=4))
        await asyncio.wait_for(entered.wait(), 3)
        try:
            await mutate()
        finally:
            release.set()
        result = await task
        self.index.encoder.encode = original
        return result

    async def test_long_source_every_window_and_restart_resume(self):
        text = ''.join(f'Observation {n:04d}. ' + ('x' * 480) for n in range(90))
        cid, _, aid = await self.add(text)
        total = source_chunk_count(text)
        self.assertGreater(total, 32)
        self.assertEqual(await self.index.backfill(batch_chunks=7), 1)
        progress = await self.progress(aid)
        self.assertEqual(progress['next_chunk'], 7)
        partial = await self.index.search(cid, 1, 'observation')
        self.assertEqual(partial.diagnostics['status'], 'incomplete')
        self.assertEqual(partial.diagnostics['indexed_chunks'], 7)
        self.assertEqual(partial.diagnostics['remaining_chunks'], total - 7)
        restarted = SemanticIndex(self.pool, Encoder())
        await self.drain(restarted, batch_chunks=9)
        self.assertEqual(await restarted.backfill(), 0)
        complete = await restarted.search(cid, 1, 'observation')
        self.assertEqual(complete.diagnostics['status'], 'complete')
        self.assertEqual(complete.diagnostics['indexed_sources'], 1)
        self.assertEqual(complete.diagnostics['indexed_chunks'], total)
        self.assertEqual(complete.diagnostics['remaining_chunks'], 0)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('SELECT chunk_index,chunk_start,chunk_end FROM cognitive_semantic_vectors WHERE artifact_id=$1 ORDER BY chunk_index', aid)
        expected = source_chunks(text)
        self.assertEqual([r['chunk_index'] for r in rows], list(range(total)))
        self.assertEqual([(r['chunk_start'], r['chunk_end']) for r in rows], [(a, b) for a, b, _ in expected])
        self.assertEqual(rows[0]['chunk_start'], 0)
        self.assertEqual(rows[-1]['chunk_end'], len(text))
        self.assertTrue(all(right['chunk_start'] <= left['chunk_end'] for left, right in zip(rows, rows[1:])))
        self.assertLessEqual(max(map(len, self.encoder.batches)), 32)
        self.assertLessEqual(max(map(len, restarted.encoder.batches)), 32)

    async def test_round_robin_prevents_large_source_starvation(self):
        rows = [await self.add(('text ' * 6000) + str(n), message=n + 1) for n in range(3)]
        for _ in range(3):
            self.assertEqual(await self.index.backfill(limit=1, batch_chunks=2), 1)
        for _, _, aid in rows:
            self.assertEqual((await self.progress(aid))['next_chunk'], 2)
        self.assertEqual(await self.index.backfill(limit=3, batch_chunks=3), 3)
        for _, _, aid in rows:
            self.assertEqual((await self.progress(aid))['next_chunk'], 3)

    async def test_rehearsal_revision_does_not_reset_or_reject_pending_batch(self):
        _, _, aid = await self.add('neutral source text ' * 500)
        async def rehearse():
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE cognitive_artifacts SET revision=revision+1,payload=jsonb_set(payload,'{replay_count}','9'::jsonb) WHERE id=$1", aid)
        self.assertEqual(await self.delayed(rehearse), 1)
        before = await self.progress(aid)
        await rehearse()
        self.assertEqual((await self.progress(aid))['generation'], before['generation'])
        self.assertEqual(await self.index.backfill(batch_chunks=3), 1)
        self.assertEqual((await self.progress(aid))['next_chunk'], 7)

    async def test_semantic_revision_rejects_pending_batch_and_restarts(self):
        _, _, aid = await self.add('neutral source text ' * 500)
        async def correct():
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE cognitive_artifacts SET revision=revision+1,payload=jsonb_set(payload,'{interpretation}','\"corrected meaning\"'::jsonb) WHERE id=$1", aid)
        self.assertEqual(await self.delayed(correct), 0)
        self.assertIsNone(await self.progress(aid))
        self.assertEqual(await self.count('cognitive_semantic_vectors'), 0)
        self.assertEqual(await self.index.backfill(batch_chunks=3), 1)
        self.assertEqual((await self.progress(aid))['next_chunk'], 3)

    async def test_direct_source_mutation_even_restored_rejects_late_batch(self):
        _, eid, aid = await self.add('neutral source text ' * 500)
        async def mutate_restore():
            async with self.pool.acquire() as conn:
                payload = await conn.fetchval('SELECT payload FROM cognitive_events WHERE id=$1', eid)
                await conn.execute("UPDATE cognitive_events SET payload=jsonb_set(payload,'{text}','\"changed\"'::jsonb) WHERE id=$1", eid)
                await conn.execute('UPDATE cognitive_events SET payload=$2::jsonb WHERE id=$1', eid, dump(object_value(payload)))
        self.assertEqual(await self.delayed(mutate_restore), 0)
        self.assertIsNone(await self.progress(aid))
        self.assertEqual(await self.index.backfill(batch_chunks=2), 1)
        self.assertEqual((await self.progress(aid))['next_chunk'], 2)

    async def test_direct_source_suppression_and_delete_erase_cache(self):
        cid, eid, aid = await self.add('neutral source text ' * 500)
        await self.index.backfill(batch_chunks=3)
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_events SET payload=NULL,suppressed_at=NOW() WHERE id=$1', eid)
        self.assertIsNone(await self.progress(aid))
        self.assertEqual(await self.count('cognitive_semantic_vectors'), 0)
        self.assertFalse(await self.index.search(cid, 1, 'neutral'))
        _, eid2, aid2 = await self.add('a second source', message=2)
        await self.index.backfill()
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM cognitive_events WHERE id=$1', eid2)
        self.assertIsNone(await self.progress(aid2))
        self.assertEqual(await self.count('cognitive_semantic_vectors'), 0)

    async def test_late_direct_source_deletion_and_epoch_advance(self):
        cid, eid, _ = await self.add('neutral source text ' * 500)
        async def delete():
            async with self.pool.acquire() as conn:
                await conn.execute('DELETE FROM cognitive_events WHERE id=$1', eid)
        self.assertEqual(await self.delayed(delete), 0)
        self.assertEqual(await self.count('cognitive_semantic_progress'), 0)
        cid, _, _ = await self.add('second source ' * 500, message=2, chat=802)
        async def epoch():
            async with self.pool.acquire() as conn:
                await conn.execute('UPDATE cognitive_contexts SET suppression_epoch=suppression_epoch+1 WHERE id=$1', cid)
        self.assertEqual(await self.delayed(epoch), 0)
        self.assertEqual(await self.count('cognitive_semantic_vectors'), 0)
        self.assertEqual(await self.count('cognitive_semantic_progress'), 0)

    async def test_dependency_null_payload_fences_selection_and_commit(self):
        cid, _, aid = await self.add('main neutral source ' * 500)
        _, dependency, _ = await self.add('dependency', message=2)
        async with self.pool.acquire() as conn:
            await conn.execute('INSERT INTO cognitive_provenance VALUES($1,$2,$3)', cid, aid, dependency)
        async def invalidate():
            async with self.pool.acquire() as conn:
                await conn.execute('UPDATE cognitive_events SET payload=NULL WHERE id=$1', dependency)
        self.assertEqual(await self.delayed(invalidate), 0)
        self.assertEqual(await self.index.backfill(), 0)
        self.assertEqual(await self.count('cognitive_semantic_vectors'), 0)
        self.assertFalse(await self.index.search(cid, 1, 'main'))

    async def test_missing_mismatched_or_wrong_offset_vector_repairs_coverage(self):
        cid, _, aid = await self.add('neutral source text ' * 200)
        await self.drain()
        for corruption in (
            'DELETE FROM cognitive_semantic_vectors WHERE artifact_id=$1 AND chunk_index=2',
            "UPDATE cognitive_semantic_vectors SET source_fingerprint='stale' WHERE artifact_id=$1 AND chunk_index=2",
            'UPDATE cognitive_semantic_vectors SET chunk_end=chunk_end-1 WHERE artifact_id=$1 AND chunk_index=2',
        ):
            with self.subTest(corruption=corruption):
                before = await self.progress(aid)
                async with self.pool.acquire() as conn:
                    await conn.execute(corruption, aid)
                result = await self.index.search(cid, 1, 'neutral')
                self.assertEqual(result.diagnostics['status'], 'incomplete')
                self.assertEqual(result.diagnostics['indexed_sources'], 0)
                await self.drain()
                after = await self.progress(aid)
                self.assertNotEqual(before['generation'], after['generation'])
                self.assertEqual(after['next_chunk'], after['total_chunks'])
                self.assertEqual((await self.index.search(cid, 1, 'neutral')).diagnostics['status'], 'complete')

    async def test_short_or_invalid_encoder_response_never_advances(self):
        cid, _, aid = await self.add('neutral source text ' * 500)
        original = self.encoder.encode
        for response in ([[1.] + [0.] * 383], [[0.] * 384] * 4, None):
            async def broken(texts, timeout=5):
                return response
            self.encoder.encode = broken
            self.assertEqual(await self.index.backfill(batch_chunks=4), 0)
            self.assertEqual((await self.progress(aid))['next_chunk'], 0)
            self.assertEqual(await self.count('cognitive_semantic_vectors'), 0)
        self.encoder.encode = original
        await self.drain()
        self.assertEqual((await self.index.search(cid, 1, 'neutral')).diagnostics['status'], 'complete')

    async def test_multiple_distinct_snippets_and_no_sampled_tail_gap(self):
        text = 'a' * 1500 + 'b' * 1500 + 'c' * 1500
        cid, _, _ = await self.add(text)
        await self.drain()
        result = await self.index.search(cid, 1, 'different wording')
        self.assertEqual(len(result), 3)
        self.assertEqual(len({r['payload']['gist'] for r in result}), 3)
        for row in result:
            payload = row['payload']
            self.assertEqual(payload['gist'], text[payload['record_start']:payload['record_end']])
            self.assertTrue(payload['source_chunk'])
            self.assertEqual(payload['author_id'], 1)
            self.assertEqual(payload['evidence_status'], 'observed_message_excerpt_not_personal_fact')

    async def test_pool_wait_and_encoder_timeout_are_bounded_and_reported(self):
        cid, _, _ = await self.add('neutral source')
        connections = [await self.pool.acquire() for _ in range(5)]
        started = time.monotonic()
        try:
            result = await self.index.search(cid, 1, 'neutral')
        finally:
            for connection in connections:
                await self.pool.release(connection)
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertEqual(result.diagnostics['status'], 'timeout')
        async def stalled(texts, timeout=.6):
            await asyncio.sleep(5)
        self.encoder.encode = stalled
        self.index.SEARCH_TIMEOUT = .08
        started = time.monotonic()
        result = await self.index.search(cid, 1, 'neutral')
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(result.diagnostics['status'], 'timeout')

    async def test_rebuild_authority_version_and_owner_mutations_remove_progress(self):
        cid, eid, aid = await self.add('neutral source text ' * 500)
        await self.index.backfill(batch_chunks=3)
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_contexts SET rebuilding=TRUE WHERE id=$1', cid)
        self.assertIsNone(await self.progress(aid))
        self.assertEqual(await self.index.backfill(), 0)
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_contexts SET rebuilding=FALSE WHERE id=$1', cid)
        await self.index.backfill(batch_chunks=3)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_contexts SET authority='shadow' WHERE id=$1", cid)
        self.assertIsNone(await self.progress(aid))
        self.assertEqual(await self.index.backfill(), 0)
        self.assertFalse(await self.index.search(cid, 1, 'neutral'))
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_contexts SET authority='active' WHERE id=$1", cid)
        await self.index.backfill(batch_chunks=3)
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_events SET owner_id=2 WHERE id=$1', eid)
        self.assertIsNone(await self.progress(aid))
        self.assertEqual(await self.index.backfill(), 0)
        self.assertFalse(await self.index.search(cid, 1, 'neutral'))
        self.assertFalse(await self.index.search(cid, 2, 'neutral'))
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_events SET owner_id=1 WHERE id=$1', eid)
        await self.index.backfill(batch_chunks=3)
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE cognitive_artifacts SET model_version='untrusted-version' WHERE id=$1", aid)
        self.assertIsNone(await self.progress(aid))
        self.assertEqual(await self.index.backfill(), 0)
        self.assertFalse(await self.index.search(cid, 1, 'neutral'))

    async def test_short_source_embedding_covers_omitted_words_without_promoting_details(self):
        text = 'The appointment location is Observatory Annex. Bring the blue notebook.'
        cid, _, aid = await self.add(text)
        async with self.pool.acquire() as conn:
            payload = object_value(await conn.fetchval('SELECT payload FROM cognitive_artifacts WHERE id=$1', aid))
            payload['gist'] = 'An appointment was mentioned.'
            payload['details'] = []
            await conn.execute('UPDATE cognitive_artifacts SET payload=$2::jsonb WHERE id=$1', aid, dump(payload))
        self.assertEqual(await self.index.backfill(), 1)
        self.assertEqual(self.encoder.batches[-1], [text])
        result = await self.index.search(cid, 1, 'the blue notebook')
        self.assertEqual(result.diagnostics['status'], 'complete')
        self.assertEqual(result[0]['payload']['gist'], payload['gist'])
        self.assertEqual(result[0]['payload']['details'], [])
        self.assertIsNone(result[0]['chunk_end'])
        async with self.pool.acquire() as conn:
            stored = object_value(await conn.fetchval('SELECT payload FROM cognitive_artifacts WHERE id=$1', aid))
        self.assertEqual(stored, payload)

    async def test_transitive_event_dependency_change_invalidates_source_generation(self):
        cid, dependency, _ = await self.add('dependency source')
        _, intermediate, _ = await self.add('intermediate source', message=2)
        _, _, aid = await self.add('dependent source ' * 500, message=3)
        # Normal runtime already records dependency edges; explicitly make a
        # transitive chain to verify cleanup does not rely on one-hop checks.
        async with self.pool.acquire() as conn:
            event_id = int(await conn.fetchval("SELECT payload->>'event_id' FROM cognitive_artifacts WHERE id=$1", aid))
            await conn.execute('DELETE FROM cognitive_event_dependencies WHERE context_id=$1', cid)
            await conn.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3),($1,$4,$2)', cid, intermediate, dependency, event_id)
        await self.index.backfill(batch_chunks=6)
        self.assertIsNotNone(await self.progress(aid))
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_events SET payload=NULL WHERE id=$1', dependency)
        self.assertIsNone(await self.progress(aid))
        self.assertEqual(await self.index.backfill(), 0)
        self.assertFalse(await self.index.search(cid, 1, 'dependent'))

    async def test_busy_old_context_does_not_starve_later_sources_and_resumes(self):
        # More old sources than the batch LIMIT all belong to one busy context.
        old = [await self.add('old source ' + str(n), message=n+1, chat=850) for n in range(5)]
        healthy_cid, _, healthy_aid = await self.add('healthy later source', chat=851)
        busy_cid = old[0][0]
        async with self.pool.acquire() as blocker, blocker.transaction():
            await blocker.fetchval('SELECT id FROM cognitive_contexts WHERE id=$1 FOR UPDATE', busy_cid)
            started = time.monotonic()
            self.assertEqual(await self.index.backfill(limit=2, batch_chunks=4), 1)
            self.assertLess(time.monotonic()-started, .8)
            self.assertEqual((await self.progress(healthy_aid))['next_chunk'], 1)
            self.assertTrue(all([await self.progress(aid) is None for _, _, aid in old]))
            # All remaining candidates are busy: no source is marked complete
            # merely because there was no immediately claimable work.
            self.assertEqual(await self.index.backfill(limit=2), 0)
            result = await self.index.search(busy_cid, 1, 'old source')
            self.assertEqual(result.diagnostics['status'], 'incomplete')
            self.assertEqual(result.diagnostics['total_sources'], 5)
            self.assertEqual(result.diagnostics['indexed_sources'], 0)
            self.assertEqual((await self.index.search(healthy_cid, 1, 'healthy')).diagnostics['status'], 'complete')
        await self.drain()
        self.assertEqual((await self.index.search(busy_cid, 1, 'old')).diagnostics['status'], 'complete')

    async def test_prepare_and_commit_lock_races_do_not_abort_healthy_rows(self):
        from asyncpg.exceptions import QueryCanceledError
        _, _, first = await self.add('first row', chat=860)
        _, _, second = await self.add('second row', chat=861)
        prepare = self.index._prepare
        async def prepare_race(row, allowance):
            if row['id'] == first:
                raise TimeoutError('synthetic concurrent lock')
            return await prepare(row, allowance)
        self.index._prepare = prepare_race
        self.assertEqual(await self.index.backfill(limit=2), 1)
        self.assertIsNone(await self.progress(first))
        self.assertIsNotNone(await self.progress(second))
        self.index._prepare = prepare
        _, _, third = await self.add('third row', chat=862)
        commit = self.index._commit
        async def commit_race(row, chunks, vectors):
            if row['id'] == first:
                raise QueryCanceledError('synthetic statement timeout')
            return await commit(row, chunks, vectors)
        self.index._commit = commit_race
        self.assertEqual(await self.index.backfill(limit=2), 1)
        self.assertEqual((await self.progress(first))['next_chunk'], 0)
        self.assertEqual((await self.progress(third))['next_chunk'], 1)
        self.index._commit = commit
        self.assertEqual(await self.index.backfill(), 1)
        self.assertEqual((await self.progress(first))['next_chunk'], 1)


class TokenSegmentationTests(unittest.TestCase):
    class Tokenizer:
        def num_special_tokens_to_add(self, pair):
            return 2

        def encode(self, text, add_special_tokens=True):
            from types import SimpleNamespace
            offsets = [(i, i + 1) for i, char in enumerate(text) for _ in range(18 if char == 'ﷺ' else 1)]
            if add_special_tokens:
                offsets = [(0, 0)] + offsets + [(0, 0)]
            return SimpleNamespace(ids=[1] * len(offsets), offsets=offsets)

    def test_every_character_fits_after_unicode_expansion_without_truncation(self):
        from cognition.semantic import token_safe_segments
        tokenizer = self.Tokenizer()
        for text in ('hello world ' * 100, '中文' * 320, 'ﷺ ' * 320, '', ' ' * 640):
            with self.subTest(text_length=len(text)):
                segments = token_safe_segments(text, tokenizer, 128)
                self.assertEqual(''.join(piece for _, _, piece in segments), text)
                self.assertTrue(all(len(tokenizer.encode(piece).ids) <= 128 for _, _, piece in segments))
                self.assertTrue(all(left[1] == right[0] for left, right in zip(segments, segments[1:])))
                self.assertEqual(segments[0][0], 0)
                self.assertEqual(segments[-1][1], len(text))

    def test_pooling_consumes_every_safe_piece_and_keeps_one_vector_per_window(self):
        from cognition.semantic import LocalEncoder
        class Model:
            def __init__(self):
                self.pieces = []
            def embed(self, texts, batch_size):
                for text in texts:
                    self.pieces.append(text)
                    yield ([1., 1.] if 'TAIL' in text else [1., 0.]) + [0.] * 382
        encoder = LocalEncoder()
        encoder.model = Model()
        encoder.tokenizer = self.Tokenizer()
        encoder.max_tokens = 128
        text = ('ﷺ ' * 318) + 'TAIL'
        try:
            vectors = encoder._encode([text, 'short'])
            self.assertEqual(len(vectors), 2)
            self.assertGreater(vectors[0][1], 0, 'tail must contribute to the pooled source vector')
            self.assertEqual(''.join(encoder.model.pieces[:-1]), text)
            self.assertTrue(all(len(encoder.tokenizer.encode(piece).ids) <= 128 for piece in encoder.model.pieces))
        finally:
            encoder.executor.shutdown(wait=True)


class EncoderRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_timed_out_identical_work_is_reused_and_next_batch_adapts(self):
        from unittest.mock import patch
        from cognition.semantic import LocalEncoder
        calls = []
        encoder = LocalEncoder()
        def slow(texts):
            calls.append(texts)
            time.sleep(.025)
            return [[1.] + [0.] * 383 for _ in texts]
        encoder._encode = slow
        try:
            with patch('cognition.semantic.asyncio.wait_for', side_effect=asyncio.TimeoutError):
                self.assertIsNone(await encoder.encode(['same permitted input'], timeout=5))
            self.assertEqual(encoder.recommended_batch_size, 16)
            await asyncio.sleep(.05)
            self.assertEqual(len(await encoder.encode(['same permitted input'], timeout=5)), 1)
            self.assertEqual(len(calls), 1)
        finally:
            await encoder.close()


class EncoderCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_releases_completed_native_resources(self):
        from cognition.semantic import LocalEncoder
        encoder = LocalEncoder()
        encoder.model = object()
        encoder.tokenizer = object()
        encoder.intent_references = [[1.]]
        encoder.future = asyncio.get_running_loop().create_future()
        encoder.future.set_result([[1.] + [0.] * 383])
        await encoder.close()
        self.assertIsNone(encoder.model)
        self.assertIsNone(encoder.tokenizer)
        self.assertIsNone(encoder.future)
        self.assertIsNone(encoder.intent_references)
        self.assertIsNone(await encoder.encode(['closed']))

    async def test_close_defers_resource_release_until_active_worker_finishes(self):
        import threading
        from unittest.mock import patch
        from cognition.semantic import LocalEncoder
        encoder = LocalEncoder()
        model, tokenizer = object(), object()
        encoder.model, encoder.tokenizer = model, tokenizer
        entered, release = threading.Event(), threading.Event()
        def active(texts):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test worker was not released')
            self.assertIs(encoder.model, model)
            self.assertIs(encoder.tokenizer, tokenizer)
            return [[1.] + [0.] * 383]
        encoder._encode_impl = active
        task = asyncio.create_task(encoder.encode(['pending'], timeout=5))
        while not entered.is_set():
            await asyncio.sleep(.005)
        try:
            with patch('cognition.semantic.asyncio.wait_for', side_effect=asyncio.TimeoutError):
                await encoder.close()
            self.assertIs(encoder.model, model)
            self.assertIs(encoder.tokenizer, tokenizer)
        finally:
            release.set()
        self.assertEqual(len(await task), 1)
        self.assertIsNone(encoder.model)
        self.assertIsNone(encoder.tokenizer)
        self.assertIsNone(encoder.future)
