"""Provider-free public hybrid indexing, coverage and revocation regressions."""
import asyncio
import os
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from cognition.public_memory import PublicMemoryRepository
from cognition.public_semantic import PublicSemanticIndex, PUBLIC_VERSION, MAX_BATCH_CHUNKS
from cognition.repositories import SuppressedEvidence
from cognition.runtime import CURRENT_TURN, CognitiveRuntime
from cognition.scope import CURRENT_SCOPE
from cognition.serialization import dump, object_value
from cognition.source_chunks import source_chunk_count, source_chunks
from cognition.types import AudienceScope
from tests.cognition.test_affect import AT
from tests.cognition.test_full_model import RecordedInterpreter
from tests.cognition import test_public_memory as public_memory_tests


class Encoder:
    """Transparent mocked paraphrases; no provider or model download."""
    def __init__(self):
        self.batches = []
        self.unavailable = False

    async def encode(self, texts, timeout=.6):
        self.batches.append(list(texts))
        result = []
        for text in texts:
            vector = [0.] * 384
            terms = text.casefold()
            vector[0 if any(t in terms for t in ('celebration','birthday venue','birthday place')) else 1] = 1.
            result.append(vector)
        return result

    async def close(self):
        pass


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1','disposable PostgreSQL required')
class PublicSemanticTests(unittest.IsolatedAsyncioTestCase):
    observe = public_memory_tests.PublicMemoryTests.observe
    retrieve = public_memory_tests.PublicMemoryTests.retrieve
    execute = public_memory_tests.PublicMemoryTests.execute

    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.at = AT
        self.runtime = await CognitiveRuntime(self.pool,RecordedInterpreter(),clock=lambda:self.at).initialize(False)
        self.encoder = Encoder()
        self.index = PublicSemanticIndex(self.pool,self.encoder,clock=lambda:self.at)
        self.memory = PublicMemoryRepository(self.pool,self.index)
        self.scope_token = CURRENT_SCOPE.set(None)
        self.turn_token = CURRENT_TURN.set(None)

    async def asyncTearDown(self):
        CURRENT_SCOPE.reset(self.scope_token)
        CURRENT_TURN.reset(self.turn_token)
        await self.runtime.close()
        await self.db.__aexit__(None,None,None)

    async def scalar(self, sql, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(sql,*args)

    async def finish(self):
        # A finite synthetic backlog must converge. Fail, never silently accept
        # a first vector as coverage of an arbitrarily long source.
        for _ in range(100):
            if not await self.index.backfill():
                return
        self.fail('public backfill did not finish the finite source backlog')

    async def test_public_paraphrase_retains_original_attribution_and_scope(self):
        cid,context,source = await self.observe('At our celebration, meet by the riverside.',owner=10)
        before = await self.retrieve(cid,context,query='birthday venue')
        self.assertEqual(before,[])
        self.assertEqual(before.diagnostics['status'],'incomplete')
        self.assertEqual(before.diagnostics['total_sources'],1)
        self.assertEqual(await self.index.backfill(),1)
        rows = await self.retrieve(cid,context,query='birthday venue')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows.diagnostics['status'],'complete')
        self.assertEqual(rows[0]['owner_id'],10)
        self.assertEqual(rows[0]['author_id'],10)
        self.assertEqual(rows[0]['event_id'],source['id'])
        self.assertEqual(rows[0]['occurred_at'],AT.isoformat())
        self.assertEqual(rows[0]['details'][0]['text'],'At our celebration, meet by the riverside.')
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_semantic_vectors'),0)
        self.assertEqual((await self.memory.validate(cid,context,[rows[0]['artifact_id']],self.at,requester=2))[0],
            [rows[0]['artifact_id']])

    async def test_private_unobserved_bot_and_other_audiences_never_enter_public_recall(self):
        cid,context,_ = await self.observe('The quarterly report is filed.')
        await self.observe('celebration OTHER_CHAT',chat=-711)
        await self.observe('celebration OTHER_TOPIC',topic=6)
        await self.observe('celebration BOT',is_bot=True,message=2)
        await self.runtime.ingest(700,1,'celebration PRIVATE',1,audience=AudienceScope('private',700,-1))
        _,hidden,_ = await self.runtime.ingest(-710,1,'celebration UNOBSERVED',90,context=context,
            audience=AudienceScope('topic',-710,5))
        await self.finish()
        rows = await self.retrieve(cid,context,query='birthday venue')
        self.assertEqual(rows,[])
        self.assertEqual(rows.diagnostics['total_sources'],1)
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors WHERE event_id=$1',hidden),0)
        for wrong in (replace(context,chat_id=-711),replace(context,topic_id=6),replace(context,persona_id='other')):
            self.assertEqual(await self.retrieve(cid,wrong,query='birthday venue'),[])

    async def test_full_coverage_resumes_and_is_fair_without_permanent_sampling(self):
        text = ('background text ' * 6500)[:99000] + ' celebration at the far riverside '
        cid,context,source = await self.observe(text)
        _,_,short = await self.observe('celebration SHORT',message=2,owner=2)
        self.assertEqual(await self.index.backfill(),2)
        self.assertLessEqual(len(self.encoder.batches[-1]),MAX_BATCH_CHUNKS)
        self.assertEqual(await self.scalar('SELECT next_chunk FROM cognitive_public_semantic_progress WHERE event_id=$1',short['id']),1)
        rows = await self.retrieve(cid,context,query='birthday venue')
        self.assertEqual(rows.diagnostics['status'],'incomplete')
        self.assertEqual(rows.diagnostics['indexed_sources'],1)
        self.assertEqual(rows.diagnostics['total_sources'],2)
        # Restarting the index resumes its persisted exact next chunk.
        self.index = PublicSemanticIndex(self.pool,self.encoder,clock=lambda:self.at)
        self.memory.semantic = self.index
        await self.finish()
        rows = await self.retrieve(cid,context,query='birthday venue')
        self.assertEqual(rows.diagnostics['status'],'complete')
        self.assertEqual(rows.diagnostics['remaining_chunks'],0)
        self.assertEqual(rows.diagnostics['indexed_chunks'],source_chunk_count(text)+1)
        self.assertTrue(any(r['event_id']==source['id'] and 'riverside' in r['details'][0]['text'] for r in rows))
        async with self.pool.acquire() as conn:
            stored = await conn.fetch('SELECT chunk_start,chunk_end FROM cognitive_public_semantic_vectors WHERE event_id=$1 ORDER BY chunk_index',source['id'])
        self.assertEqual([(r['chunk_start'],r['chunk_end']) for r in stored],[(a,b) for a,b,_ in source_chunks(text)])
        self.assertEqual(await self.index.backfill(),0)

    async def test_three_separate_exact_nonoverlapping_windows_and_total_result_limit(self):
        text = 'Opening report framing. ' + ('filler ' * 180 + 'celebration riverside ') * 5
        cid,context,source = await self.observe(text)
        await self.finish()
        rows = await self.retrieve(cid,context,query='birthday venue')
        self.assertEqual(len(rows),3)
        spans = sorted((r['record_start'],r['record_end']) for r in rows)
        self.assertTrue(all(max(0,min(b,d)-max(a,c)) < .75*min(b-a,d-c)
            for i,(a,b) in enumerate(spans) for c,d in spans[i+1:]))
        for row in rows:
            self.assertEqual(row['details'][0]['text'],text[row['record_start']:row['record_end']])
            self.assertEqual(row['source_prefix'],text[:256] if row['record_start'] else '')
            self.assertEqual(row['event_id'],source['id'])
        self.assertEqual(len(await self.retrieve(cid,context,query='birthday venue',limit=2)),2)

    async def test_optout_reset_retention_and_authority_invalidate_vectors(self):
        cid,context,_ = await self.observe('celebration riverside')
        await self.finish()
        rows = await self.retrieve(cid,context,query='birthday venue')
        for user in (1,2):
            await self.runtime.groups.policies.opt_out(-710,user,True)
            self.assertEqual(await self.retrieve(cid,context,query='birthday venue'),[])
            self.assertEqual(await self.memory.validate(cid,context,[rows[0]['artifact_id']],self.at,requester=2),([],[]))
            await self.runtime.groups.policies.opt_out(-710,user,False)
            await self.finish()
        for update in ("authority='shadow'","authority='active',rebuilding=TRUE"):
            await self.execute('UPDATE cognitive_contexts SET '+update+' WHERE id=$1',cid)
            self.assertEqual(await self.retrieve(cid,context,query='birthday venue'),[])
            self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors'),0)
            await self.execute("UPDATE cognitive_contexts SET authority='active',rebuilding=FALSE WHERE id=$1",cid)
            await self.finish()
        self.at += timedelta(days=2)
        await self.runtime.groups.policies.set(-710,dict(retention_days=1))
        self.assertEqual(await self.retrieve(cid,context,query='birthday venue'),[])
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors'),0)
        self.at = AT
        await self.finish()
        await self.execute('UPDATE cognitive_contexts SET history_after_event_id=$2 WHERE id=$1',cid,rows[0]['event_id'])
        self.assertEqual(await self.retrieve(cid,context,query='birthday venue'),[])
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors'),0)

    async def test_dependency_revocation_erases_all_dependent_public_vectors(self):
        cid,context,support = await self.observe('A source document.')
        _,_,dependent = await self.observe('celebration riverside',owner=2,message=2)
        await self.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3)',cid,dependent['id'],support['id'])
        await self.finish()
        rows = await self.retrieve(cid,context,query='birthday venue')
        self.assertEqual(len(rows),1)
        await self.runtime.groups.policies.opt_out(-710,1,True)
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors'),0)
        self.assertEqual(await self.retrieve(cid,context,query='birthday venue'),[])
        self.assertEqual((await self.memory.validate(cid,context,[rows[0]['artifact_id']],self.at,requester=2)),([],[]))

    async def test_correction_deletion_and_observation_erasure_remove_vectors(self):
        cid,context,source = await self.observe('celebration old riverside')
        await self.finish()
        rows = await self.retrieve(cid,context,query='birthday venue')
        self.at += timedelta(seconds=1)
        await self.observe('celebration new hilltop',edited=True,at=self.at)
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors'),0)
        await self.finish()
        fresh = await self.retrieve(cid,context,query='birthday venue')
        self.assertIn('new hilltop',str(fresh))
        self.assertNotIn('old riverside',str(fresh))
        self.assertEqual(await self.memory.validate(cid,context,[rows[0]['artifact_id']],self.at),([],[]))
        await self.execute('DELETE FROM group_observations WHERE context_id=$1',cid)
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors'),0)
        self.assertEqual(await self.retrieve(cid,context,query='birthday venue'),[])

    async def test_late_encoder_cannot_restore_revoked_source_or_dependency(self):
        cid,context,source = await self.observe('celebration riverside')
        entered,release = asyncio.Event(),asyncio.Event()
        original = self.encoder.encode
        async def delayed(texts,timeout=5):
            entered.set()
            await release.wait()
            return await original(texts,timeout=timeout)
        with patch.object(self.encoder,'encode',side_effect=delayed):
            task = asyncio.create_task(self.index.backfill())
            await entered.wait()
            await self.runtime.groups.policies.opt_out(-710,1,True)
            release.set()
            self.assertEqual(await task,0)
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors'),0)
        self.assertEqual(await self.retrieve(cid,context,query='birthday venue'),[])

    async def test_late_encoder_generation_fences_revoke_restore_aba(self):
        cid,context,source = await self.observe('celebration riverside')
        entered,release = asyncio.Event(),asyncio.Event()
        original = self.encoder.encode
        calls = 0
        async def delayed(texts,timeout=5):
            nonlocal calls
            calls += 1
            if calls==1:
                entered.set()
                await release.wait()
            return await original(texts,timeout=timeout)
        with patch.object(self.encoder,'encode',side_effect=delayed):
            old = asyncio.create_task(self.index.backfill())
            await entered.wait()
            generation = await self.scalar('SELECT generation FROM cognitive_public_semantic_progress WHERE event_id=$1',source['id'])
            await self.runtime.groups.policies.opt_out(-710,1,True)
            await self.runtime.groups.policies.opt_out(-710,1,False)
            # A newer batch may recreate the same hash/epoch/offset; the old
            # batch must not match its distinct invalidation generation.
            newer = PublicSemanticIndex(self.pool,Encoder(),clock=lambda:self.at)
            await newer.backfill()
            new_generation = await self.scalar('SELECT generation FROM cognitive_public_semantic_progress WHERE event_id=$1',source['id'])
            self.assertNotEqual(generation,new_generation)
            await self.execute('UPDATE cognitive_public_semantic_progress SET next_chunk=0 WHERE event_id=$1',source['id'])
            await self.execute('DELETE FROM cognitive_public_semantic_vectors WHERE event_id=$1',source['id'])
            release.set()
            self.assertEqual(await old,0)
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors'),0)

    async def test_late_encoder_rechecks_deletion_reset_retention_and_new_private_dependency(self):
        for index,action in enumerate(('delete','reset','retention','dependency','epoch','rebuild','observation'),1):
            with self.subTest(action=action):
                self.at = AT
                cid,context,source = await self.observe('celebration riverside',chat=-800-index)
                entered,release = asyncio.Event(),asyncio.Event()
                original = self.encoder.encode
                async def delayed(texts,timeout=5):
                    entered.set()
                    await release.wait()
                    return await original(texts,timeout=timeout)
                with patch.object(self.encoder,'encode',side_effect=delayed):
                    task = asyncio.create_task(self.index.backfill())
                    await entered.wait()
                    if action=='delete':
                        await self.execute('DELETE FROM group_observations WHERE context_id=$1',cid)
                        await self.execute('DELETE FROM cognitive_events WHERE context_id=$1',cid)
                    elif action=='observation':
                        await self.execute('UPDATE group_observations SET payload=NULL WHERE context_id=$1',cid)
                    elif action=='reset':
                        await self.execute('UPDATE cognitive_contexts SET history_after_event_id=$2 WHERE id=$1',cid,source['id'])
                    elif action=='retention':
                        self.at += timedelta(days=31)
                    elif action=='epoch':
                        await self.execute('UPDATE cognitive_contexts SET suppression_epoch=suppression_epoch+1 WHERE id=$1',cid)
                    elif action=='rebuild':
                        await self.execute('UPDATE cognitive_contexts SET rebuilding=TRUE WHERE id=$1',cid)
                    else:
                        _,private,_ = await self.runtime.ingest(context.chat_id,1,'private source',99,context=context,
                            audience=AudienceScope('private',context.chat_id,context.topic_id))
                        await self.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3)',cid,source['id'],private)
                    release.set()
                    self.assertEqual(await task,0)
                self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors WHERE context_id=$1',cid),0)
                await self.execute('UPDATE cognitive_events SET suppressed_at=NOW() WHERE context_id=$1',cid)

    async def test_missing_and_malformed_vectors_do_not_certify_complete_coverage(self):
        cid,context,source = await self.observe('celebration ' + 'background '*300)
        await self.finish()
        await self.execute('DELETE FROM cognitive_public_semantic_vectors WHERE event_id=$1 AND chunk_index=1',source['id'])
        rows = await self.retrieve(cid,context,query='birthday venue')
        self.assertEqual(rows.diagnostics['status'],'incomplete')
        self.assertEqual(rows.diagnostics['remaining_chunks'],1)
        await self.finish()
        rows = await self.retrieve(cid,context,query='birthday venue')
        self.assertEqual(rows.diagnostics['status'],'complete')
        await self.execute('UPDATE cognitive_public_semantic_vectors SET chunk_end=chunk_end-1 WHERE event_id=$1 AND chunk_index=1',source['id'])
        rows = await self.retrieve(cid,context,query='birthday venue')
        self.assertEqual(rows.diagnostics['status'],'incomplete')
        await self.finish()
        self.assertEqual((await self.retrieve(cid,context,query='birthday venue')).diagnostics['status'],'complete')

    async def test_missing_slow_and_invalid_encoder_preserve_lexical_evidence(self):
        cid,context,_ = await self.observe('launch codename Amber Harbor')
        async def absent(*args,**kwargs):
            return None
        self.encoder.unavailable = True
        with patch.object(self.encoder,'encode',side_effect=absent):
            rows = await self.retrieve(cid,context)
            self.assertEqual(len(rows),1)
            self.assertEqual(rows.diagnostics['semantic_status'],'unavailable')
            self.assertEqual(rows.diagnostics['lexical_status'],'complete')
        async def slow(*args,**kwargs):
            await asyncio.sleep(2)
        start = asyncio.get_running_loop().time()
        with patch.object(self.encoder,'encode',side_effect=slow):
            rows = await self.retrieve(cid,context)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows.diagnostics['semantic_status'],'timeout')
        self.assertLess(asyncio.get_running_loop().time()-start,1.5)
        with patch.object(self.encoder,'encode',side_effect=RuntimeError('offline')):
            rows = await self.retrieve(cid,context)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows.diagnostics['semantic_status'],'unavailable')

    async def test_optional_semantic_sql_timeout_preserves_lexical_results(self):
        cid,context,_ = await self.observe('launch codename Amber Harbor')
        await self.finish()
        async def slow(conn,*args,**kwargs):
            await conn.execute('SELECT pg_sleep(2)')
        with patch.object(self.index,'search_locked',side_effect=slow):
            rows = await self.retrieve(cid,context)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows.diagnostics['semantic_status'],'timeout')
        self.assertEqual(rows.diagnostics['lexical_status'],'complete')
        self.assertEqual(len(await self.retrieve(cid,context)),1)

    async def test_optional_semantic_failure_preserves_lexical_results(self):
        cid,context,_ = await self.observe('launch codename Amber Harbor')
        with patch.object(self.index,'search_locked',side_effect=RuntimeError('offline')):
            rows = await self.retrieve(cid,context)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows.diagnostics['status'],'unavailable')
        self.assertEqual(rows.diagnostics['lexical_status'],'complete')

    async def test_background_pool_wait_is_bounded_and_recovers(self):
        cid,context,_ = await self.observe('celebration riverside')
        held = [await self.pool.acquire() for _ in range(self.pool.get_max_size())]
        try:
            start = asyncio.get_running_loop().time()
            self.assertEqual(await self.index.backfill(),0)
            self.assertLess(asyncio.get_running_loop().time()-start,1.5)
        finally:
            for conn in held:
                await self.pool.release(conn)
        self.assertEqual(await self.index.backfill(),1)
        self.assertEqual((await self.retrieve(cid,context,query='birthday venue')).diagnostics['status'],'complete')

    async def test_whole_background_deadline_keeps_restartable_checkpoint(self):
        cid,context,_ = await self.observe('celebration riverside')
        async def stalled(*args,**kwargs):
            await asyncio.sleep(2)
        with patch.object(self.encoder,'encode',side_effect=stalled),patch('cognition.public_semantic.BACKFILL_BUDGET_SECONDS',.1):
            self.assertEqual(await self.index.backfill(),0)
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors'),0)
        self.assertEqual(await self.scalar('SELECT next_chunk FROM cognitive_public_semantic_progress'),0)
        self.assertEqual(await self.index.backfill(),1)
        self.assertEqual((await self.retrieve(cid,context,query='birthday venue')).diagnostics['status'],'complete')

    async def test_adaptive_batch_budget_remains_bounded_and_eventually_complete(self):
        self.encoder.recommended_batch_size = 2
        cid,context,_ = await self.observe('celebration ' + 'background '*500)
        await self.observe('celebration short',message=2)
        self.assertEqual(await self.index.backfill(),2)
        self.assertEqual(len(self.encoder.batches[-1]),2)
        await self.finish()
        self.assertTrue(all(len(batch)<=2 for batch in self.encoder.batches))
        self.assertEqual((await self.retrieve(cid,context,query='birthday venue')).diagnostics['status'],'complete')

    async def test_busy_context_does_not_starve_another_public_audience(self):
        first,context,_ = await self.observe('celebration blocked')
        second,other,_ = await self.observe('celebration available',chat=-711)
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',context.chat_id)
            self.assertEqual(sum([await self.index.backfill() for _ in range(2)]),1)
            self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors WHERE context_id=$1',first),0)
            self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors WHERE context_id=$1',second),1)
        self.assertEqual(await self.index.backfill(),1)
        self.assertEqual((await self.retrieve(first,context,query='birthday venue')).diagnostics['status'],'complete')

    async def test_full_query_lexical_specificity_beats_named_topical_distractors(self):
        fixtures = [('Алиса','Маяк'),('Борис','Кедр'),('Виктория','Янтарь'),('Дмитрий','Север'),('Елена','Берег')]
        sources = []
        for message,(name,project) in enumerate(fixtures,1):
            cid,context,source = await self.observe(f'{name} утвердил бюджет проекта {project}.',message=message,owner=message+10)
            sources.append(source['id'])
        await self.finish()
        # All mock vectors have equal topical similarity and newest IDs sort
        # first semantically, intentionally challenging early named sources.
        for semantic in (None,self.index):
            self.memory.semantic = semantic
            for expected,(name,_) in zip(sources,fixtures):
                with self.subTest(semantic=semantic is not None,name=name):
                    rows = await self.retrieve(cid,context,query=f'{name} бюджет')
                    self.assertTrue(rows)
                    self.assertEqual(rows[0]['event_id'],expected)
                    self.assertIn(name,rows[0]['details'][0]['text'])

    async def test_full_query_specificity_preserves_russian_inflection(self):
        cid,context,target = await self.observe('Алиса утвердила бюджет проекта Маяк.')
        await self.observe('Борис утвердил бюджет проекта Кедр.',message=2)
        await self.finish()
        for semantic in (None,self.index):
            self.memory.semantic = semantic
            rows = await self.retrieve(cid,context,query='Алисе бюджета')
            self.assertEqual(rows[0]['event_id'],target['id'])
            self.assertEqual(rows[0]['details'][0]['text'],'Алиса утвердила бюджет проекта Маяк.')

    async def test_prepared_healthy_source_progresses_ahead_of_many_locked_contexts(self):
        cid,context,_ = await self.observe('celebration healthy')
        locked = []
        for index in range(8):
            _,blocked,_ = await self.observe('celebration waiting',chat=-900-index)
            locked.append(blocked.chat_id)
        async with self.pool.acquire() as conn,conn.transaction():
            for chat_id in locked:
                await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',chat_id)
            self.assertEqual(await asyncio.wait_for(self.index.backfill(),6),1)
            self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors WHERE context_id=$1',cid),1)
        self.assertEqual((await self.retrieve(cid,context,query='birthday venue')).diagnostics['status'],'complete')

    async def test_cyrillic_name_only_lexical_search_preserves_original_case_and_offsets(self):
        text = 'Начало отчёта. ' + 'фон ' * 300 + 'АлиСА согласовала проект Маяк.'
        cid,context,target = await self.observe(text)
        await self.observe('Борис согласовал другой проект.',message=2)
        self.memory.semantic = None
        rows = await self.retrieve(cid,context,query='АЛИСА')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['event_id'],target['id'])
        self.assertIn('АлиСА',rows[0]['details'][0]['text'])
        self.assertEqual(rows[0]['details'][0]['text'],text[rows[0]['record_start']:rows[0]['record_end']])

    async def test_healthy_last_context_eventually_progresses_despite_many_locks(self):
        locked = []
        for index in range(7):
            _,blocked,_ = await self.observe('celebration waiting',chat=-950-index)
            locked.append(blocked.chat_id)
        cid,context,_ = await self.observe('celebration healthy',chat=-960)
        original = self.encoder.encode
        async def delayed(texts,timeout=5):
            await asyncio.sleep(2)
            return await original(texts,timeout=timeout)
        async with self.pool.acquire() as conn,conn.transaction():
            for chat_id in locked:
                await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',chat_id)
            with patch.object(self.encoder,'encode',side_effect=delayed):
                advanced = 0
                for _ in range(9):
                    advanced += await asyncio.wait_for(self.index.backfill(),6)
                    if advanced:
                        break
                self.assertEqual(advanced,1)
            self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors WHERE context_id=$1',cid),1)

    async def test_context_row_locks_do_not_block_first_time_scheduler_progress(self):
        blocked_ids = []
        for index in range(2):
            cid,_,_ = await self.observe('celebration waiting',chat=-980-index)
            blocked_ids.append(cid)
        cid,context,_ = await self.observe('celebration healthy',chat=-982)
        self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_work'),0)
        async with self.pool.acquire() as conn,conn.transaction():
            # Real tuple locks also block the scheduler work-row FK key-share
            # check, unlike the advisory-lock cases covered above.
            await conn.fetch('SELECT id FROM cognitive_contexts WHERE id=ANY($1::bigint[]) FOR UPDATE',blocked_ids)
            self.assertEqual(await asyncio.wait_for(self.index.backfill(),2),1)
            self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_vectors WHERE context_id=$1',cid),1)
            self.assertEqual(await self.scalar('SELECT count(*) FROM cognitive_public_semantic_work WHERE context_id=ANY($1::bigint[])',blocked_ids),0)
        self.assertEqual(await self.index.backfill(),2)
        self.assertEqual((await self.retrieve(cid,context,query='birthday venue')).diagnostics['status'],'complete')

    async def test_semantic_no_evidence_is_distinct_from_incomplete_and_timeout(self):
        cid,context,_ = await self.observe('The quarterly report is filed.')
        rows = await self.retrieve(cid,context,query='birthday venue')
        self.assertEqual(rows,[])
        self.assertEqual(rows.diagnostics['status'],'incomplete')
        await self.finish()
        rows = await self.retrieve(cid,context,query='birthday venue')
        self.assertEqual(rows,[])
        self.assertEqual(rows.diagnostics['status'],'complete')
        async def slow(*args,**kwargs):
            await asyncio.sleep(2)
        with patch.object(self.memory,'_scope',side_effect=slow):
            rows = await self.retrieve(cid,context,query='birthday venue')
        self.assertEqual(rows,[])
        self.assertEqual(rows.diagnostics['status'],'timeout')
        self.assertEqual(rows.diagnostics['lexical_status'],'timeout')
