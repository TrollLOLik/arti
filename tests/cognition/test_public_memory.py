"""Public audience retrieval must never widen the private owner boundary."""
import asyncio
import os
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cognition.public_memory import PublicMemoryRepository
from cognition.repositories import SuppressedEvidence
from cognition.runtime import CognitiveRuntime, CURRENT_TURN
from cognition.scope import CURRENT_SCOPE, TransportScope
from cognition.serialization import dump, object_value
from cognition.types import AudienceScope, ContextKey, Origin, Perception, PERCEPTION_VERSION
from tests.cognition.test_affect import AT
from tests.cognition.test_full_model import RecordedInterpreter, situation


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1','disposable PostgreSQL required')
class PublicMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.at = AT
        self.runtime = await CognitiveRuntime(self.pool,RecordedInterpreter(),clock=lambda:self.at).initialize(False)
        self.memory = PublicMemoryRepository(self.pool)
        self.scope_token = CURRENT_SCOPE.set(None)
        self.turn_token = CURRENT_TURN.set(None)

    async def asyncTearDown(self):
        CURRENT_SCOPE.reset(self.scope_token)
        CURRENT_TURN.reset(self.turn_token)
        await self.runtime.close()
        await self.db.__aexit__(None,None,None)

    async def observe(self, text='We agreed that the launch codename is Amber Harbor.', *,
                      owner=1, message=1, chat=-710, topic=5, mode='default', at=None,
                      edited=False, is_bot=False, sender_kind='user', sender_ref=None):
        scope = TransportScope(chat,topic,'supergroup',owner,message,True,sender_kind,
                               sender_ref=sender_ref)
        token = CURRENT_SCOPE.set(scope)
        try:
            cid = await self.runtime.groups.observe(scope,text,mode,at=at,edited=edited,is_bot=is_bot)
            context = await self.runtime.context(chat,mode,topic)
        finally:
            CURRENT_SCOPE.reset(token)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT e.* FROM cognitive_events e JOIN group_observations o ON o.context_id=e.context_id AND o.event_id=e.id WHERE o.context_id=$1 AND o.message_id=$2',cid,message)
        return cid,context,row

    async def retrieve(self, cid, context, query='launch codename', **kwargs):
        return await self.memory.retrieve(cid,context,query,self.at,'test',requester=2,**kwargs)

    async def execute(self, sql, *args):
        async with self.pool.acquire() as conn:
            return await conn.execute(sql,*args)

    async def test_retrieves_other_author_beyond_recent_dialogue_without_private_access(self):
        cid,context,original = await self.observe()
        await self.runtime.process(cid,original['id'])
        for i in range(2,72):
            self.at += timedelta(seconds=1)
            await self.observe(f'Unrelated daily discussion number {i}.',owner=2,message=i)
        history = await self.runtime.groups.history(TransportScope(-710,5,'supergroup',2))
        self.assertNotIn('Amber Harbor',history)
        rows = await self.retrieve(cid,context)
        self.assertEqual(len(rows),1)
        self.assertIn('Amber Harbor',rows[0]['details'][0]['text'])
        self.assertEqual(rows[0]['author_id'],1)
        self.assertEqual(rows[0]['owner_id'],1)
        self.assertEqual(rows[0]['event_id'],original['id'])
        self.assertEqual(rows[0]['occurred_at'],original['occurred_at'].isoformat())
        self.assertEqual(rows[0]['audience'],dict(kind='topic',chat_id=-710,topic_id=5))
        self.assertFalse(await self.runtime.memory.retrieve(cid,2,'launch codename',self.at,'private-test'))
        allowed,sources = await self.memory.record_retrieval(cid,context,2,'test','included',
            [rows[0]['artifact_id']],self.at,expected_epoch=rows[0]['projection_epoch'])
        self.assertEqual(allowed,[rows[0]['artifact_id']])
        self.assertEqual(sources,[original['id']])

    async def test_public_sources_work_without_semantic_projection_or_full_visibility(self):
        cid,context,_ = await self.observe()
        policy,_ = await self.runtime.groups.policies.get(-710,5)
        self.assertFalse(policy.full_visibility)
        self.assertEqual(len(await self.retrieve(cid,context)),1)
        self.assertEqual(self.runtime.interpreter.calls,0)

    async def test_exact_context_and_audience_never_promote_unknown_or_private(self):
        cid,context,source = await self.observe()
        for audience in (AudienceScope(),AudienceScope('private',-710,5),AudienceScope('topic',-710,6)):
            payload = object_value(source['payload'])
            payload['audience'] = dict(kind=audience.kind,chat_id=audience.chat_id,topic_id=audience.topic_id)
            await self.execute('UPDATE cognitive_events SET payload=$2::jsonb WHERE id=$1',source['id'],dump(payload))
            self.assertEqual(await self.retrieve(cid,context),[])
        await self.execute('UPDATE cognitive_events SET payload=$2::jsonb WHERE id=$1',source['id'],source['payload'])
        for wrong in (replace(context,chat_id=-711),replace(context,topic_id=6),
                      replace(context,mode='rp',scene_id='some-scene'),replace(context,persona_id='other')):
            self.assertEqual(await self.retrieve(cid,wrong),[])
        self.assertEqual(len(await self.retrieve(cid,context)),1)

    async def test_other_chat_topic_and_dm_are_not_candidates(self):
        cid,context,_ = await self.observe('No related information here.')
        await self.observe('launch codename OTHER_CHAT',chat=-711)
        await self.observe('launch codename OTHER_TOPIC',topic=6)
        await self.runtime.ingest(700,1,'launch codename PRIVATE_DM',1,
                                  audience=AudienceScope('private',700,-1))
        await self.runtime.ingest(-710,1,'launch codename UNOBSERVED',90,context=context,
                                  audience=AudienceScope('topic',-710,5))
        self.assertEqual(await self.retrieve(cid,context),[])

    async def test_rp_scene_and_mode_are_isolated_and_retired_scene_cannot_read(self):
        _,_,_ = await self.observe('launch codename REAL')
        cid,context,_ = await self.observe('launch codename FICTION',mode='rp')
        rows = await self.retrieve(cid,context)
        self.assertEqual(len(rows),1)
        self.assertIn('FICTION',str(rows))
        self.assertNotIn('REAL',str(rows))
        await self.execute('UPDATE cognitive_scenes SET scene_id=$1 WHERE chat_id=-710 AND topic_id=5','new-scene')
        self.assertEqual(await self.retrieve(cid,context),[])

    async def test_retention_applies_to_retrieval_and_existing_record(self):
        cid,context,_ = await self.observe()
        rows = await self.retrieve(cid,context)
        self.at += timedelta(days=2)
        await self.runtime.groups.policies.set(-710,dict(retention_days=1))
        self.assertEqual(await self.retrieve(cid,context),[])
        self.assertEqual(await self.memory.validate(cid,context,[rows[0]['artifact_id']],self.at),([],[]))

    async def test_history_reset_fences_old_public_records(self):
        cid,context,_ = await self.observe()
        rows = await self.retrieve(cid,context)
        await self.execute('UPDATE cognitive_contexts SET history_after_event_id=$2 WHERE id=$1',cid,rows[0]['event_id'])
        self.assertEqual(await self.retrieve(cid,context),[])
        with self.assertRaises(SuppressedEvidence):
            await self.memory.record_retrieval(cid,context,2,'test','included',[rows[0]['artifact_id']],self.at)

    async def test_author_requester_opt_out_and_disabled_policy_block_access(self):
        cid,context,_ = await self.observe()
        rows = await self.retrieve(cid,context)
        ids = [rows[0]['artifact_id']]
        for user in (1,2):
            await self.runtime.groups.policies.opt_out(-710,user,True)
            self.assertEqual(await self.retrieve(cid,context),[])
            self.assertEqual(await self.memory.validate(cid,context,ids,self.at,requester=2),([],[]))
            await self.runtime.groups.policies.opt_out(-710,user,False)
        await self.runtime.groups.policies.set(-710,dict(disabled=True))
        self.assertEqual(await self.retrieve(cid,context),[])
        self.assertEqual(await self.memory.validate(cid,context,ids,self.at),([],[]))

    async def test_response_status_disabled_blocks_public_memory(self):
        cid,context,_ = await self.observe()
        await self.execute('INSERT INTO response_status(chat_id,enabled) VALUES(-710,FALSE)')
        self.assertEqual(await self.retrieve(cid,context),[])

    async def test_authority_rebuilding_and_epoch_fences(self):
        cid,context,_ = await self.observe()
        rows = await self.retrieve(cid,context)
        epoch = rows[0]['projection_epoch']
        for authority in ('shadow','legacy'):
            await self.execute('UPDATE cognitive_contexts SET authority=$2 WHERE id=$1',cid,authority)
            self.assertEqual(await self.retrieve(cid,context),[])
        await self.execute("UPDATE cognitive_contexts SET authority='active',rebuilding=TRUE WHERE id=$1",cid)
        self.assertEqual(await self.retrieve(cid,context),[])
        await self.execute('UPDATE cognitive_contexts SET rebuilding=FALSE,suppression_epoch=suppression_epoch+1 WHERE id=$1',cid)
        with self.assertRaises(SuppressedEvidence):
            await self.retrieve(cid,context,expected_epoch=epoch)
        with self.assertRaises(SuppressedEvidence):
            await self.memory.validate(cid,context,[rows[0]['artifact_id']],self.at,expected_epoch=epoch)
        self.assertEqual(await self.memory.validate(cid,context,[rows[0]['artifact_id']],self.at),([],[]))
        self.assertEqual(len(await self.retrieve(cid,context,expected_epoch=epoch+1)),1)

    async def test_suppressed_or_missing_source_and_observation_never_resurface(self):
        cid,context,source = await self.observe()
        rows = await self.retrieve(cid,context)
        ids = [rows[0]['artifact_id']]
        for table,column in (('group_observations','suppressed_at'),('cognitive_events','suppressed_at')):
            where = 'event_id' if table == 'group_observations' else 'id'
            await self.execute(f'UPDATE {table} SET {column}=NOW() WHERE {where}=$1',source['id'])
            self.assertEqual(await self.retrieve(cid,context),[])
            self.assertEqual(await self.memory.validate(cid,context,ids,self.at),([],[]))
            await self.execute(f'UPDATE {table} SET {column}=NULL WHERE {where}=$1',source['id'])
        await self.execute('UPDATE group_observations SET payload=NULL WHERE event_id=$1',source['id'])
        self.assertEqual(await self.retrieve(cid,context),[])

    async def test_public_record_itself_can_be_suppressed(self):
        cid,context,_ = await self.observe()
        rows = await self.retrieve(cid,context)
        await self.execute('UPDATE cognitive_artifacts SET payload=NULL,suppressed_at=NOW() WHERE id=$1',rows[0]['artifact_id'])
        self.assertEqual(await self.retrieve(cid,context),[])

    async def test_only_current_edited_message_is_readable(self):
        cid,context,_ = await self.observe()
        rows = await self.retrieve(cid,context)
        self.at += timedelta(seconds=1)
        await self.observe('launch codename Blue River',at=self.at,edited=True)
        updated = await self.retrieve(cid,context)
        self.assertIn('Blue River',str(updated))
        self.assertNotIn('Amber Harbor',str(updated))
        self.assertEqual(await self.memory.validate(cid,context,[rows[0]['artifact_id']],self.at),([],[]))

    async def test_quoted_reported_modality_and_original_author_dates_preserved(self):
        for i,modality in enumerate(('quoted','reported','hypothetical'),1):
            self.at += timedelta(seconds=1)
            cid,context,source = await self.observe('Alex said: "launch codename Amber Harbor". This is a report.',message=i,at=AT)
            from cognition.serialization import load_event
            event = load_event(source['payload'])
            perception = Perception(event.event_id,PERCEPTION_VERSION,(),situation(event,modality=modality))
            await self.execute('UPDATE cognitive_events SET perception=$2::jsonb WHERE id=$1',source['id'],dump(perception))
            rows = await self.retrieve(cid,context)
            record = next(r for r in rows if r['event_id'] == source['id'])
            self.assertEqual(record['modality'],modality)
            self.assertEqual(record['author_id'],1)
            self.assertEqual(record['occurred_at'],AT.isoformat())
            self.assertEqual(record['observed_at'],self.at.isoformat())
            self.assertEqual(record['details'][0]['text'],event.text)
            self.assertTrue(record['details'][0]['verbatim_verified'])
            self.assertTrue(record['uncertainty'])

    async def test_source_requires_permitted_recursive_dependencies(self):
        cid,context,support = await self.observe('launch codename Original source')
        _,_,middle = await self.observe('I repeated the launch codename.',owner=2,message=2)
        _,_,last = await self.observe('launch codename Final repeat',owner=2,message=3)
        await self.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3)',cid,middle['id'],support['id'])
        await self.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3)',cid,last['id'],middle['id'])
        rows = await self.retrieve(cid,context)
        record = next(r for r in rows if r['event_id'] == last['id'])
        self.assertEqual(record['author_id'],2)
        self.assertEqual(record['modality'],'source_utterance')
        allowed,sources = await self.memory.validate(cid,context,[record['artifact_id']],self.at,requester=2)
        self.assertEqual(set(sources),{support['id'],middle['id'],last['id']})
        await self.runtime.groups.policies.opt_out(-710,1,True)
        self.assertEqual(await self.retrieve(cid,context),[])
        self.assertEqual(await self.memory.validate(cid,context,allowed,self.at),([],[]))

    async def test_private_dependency_cannot_be_laundered_through_public_record(self):
        cid,context,source = await self.observe('launch codename Public repeat',owner=2)
        _,private_id,_ = await self.runtime.ingest(-710,2,'launch codename PRIVATE',99,context=context,
                                                  audience=AudienceScope('private',-710,5))
        await self.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3)',cid,source['id'],private_id)
        self.assertEqual(await self.retrieve(cid,context),[])

    async def test_bot_records_are_excluded_even_without_dependencies(self):
        cid,context,_ = await self.observe('launch codename Unsupported bot assertion',owner=2,is_bot=True)
        self.assertEqual(await self.retrieve(cid,context),[])

    async def test_anonymous_chat_author_retains_original_sender_ref(self):
        cid,context,_ = await self.observe(owner=None,sender_kind='chat',sender_ref='chat:-450')
        rows = await self.retrieve(cid,context)
        self.assertEqual(len(rows),1)
        self.assertIsNone(rows[0]['author_id'])
        self.assertEqual(rows[0]['sender_ref'],'chat:-450')
        self.assertEqual(rows[0]['sender_kind'],'chat')
        self.assertEqual(rows[0]['time_basis'],'source_event')

    async def test_source_without_observed_legacy_topic_is_not_public(self):
        context = ContextKey('arti',-710,topic_id=-1)
        cid = await self.runtime.ensure_context(context)
        await self.runtime.ingest(-710,1,'launch codename Legacy unknown',1,context=context)
        self.assertEqual(await self.retrieve(cid,context),[])

    async def test_private_or_tampered_artifacts_cannot_be_logged_as_public(self):
        cid,context,source = await self.observe()
        await self.runtime.process(cid,source['id'])
        private = await self.runtime.memory.artifacts(cid,1,'trace')
        with self.assertRaises(SuppressedEvidence):
            await self.memory.record_retrieval(cid,context,2,'bad','included',[private[0]['id']],self.at)
        rows = await self.retrieve(cid,context)
        await self.execute("UPDATE cognitive_artifacts SET payload=jsonb_set(payload,'{details,0,text}','\"FAKE\"'::jsonb) WHERE id=$1",rows[0]['artifact_id'])
        self.assertEqual(await self.memory.validate(cid,context,[rows[0]['artifact_id']],self.at),([],[]))

    async def test_connection_aware_validation_and_excluded_request_source(self):
        cid,context,source = await self.observe()
        self.assertEqual(await self.retrieve(cid,context,exclude_event_ids=[source['id']]),[])
        rows = await self.retrieve(cid,context)
        async with self.pool.acquire() as conn,conn.transaction():
            allowed,sources = await self.memory.validate(cid,context,[rows[0]['artifact_id']],self.at,
                requester=2,expected_epoch=rows[0]['projection_epoch'],connection=conn)
        self.assertEqual(allowed,[rows[0]['artifact_id']])
        self.assertEqual(sources,[source['id']])

    async def test_no_relevant_source_returns_empty_instead_of_recent_padding(self):
        cid,context,_ = await self.observe('Lunch was delicious.')
        self.assertEqual(await self.retrieve(cid,context),[])
        self.assertEqual(await self.retrieve(cid,context,query='what did we'),[])

    async def test_concurrent_retrieve_direct_delivery_and_optout_are_bounded(self):
        from cognition.delivery import DeliverySuppressed, send_with_receipt
        cid,context,_ = await self.observe()
        await self.execute('INSERT INTO response_status(chat_id,enabled) VALUES(-710,TRUE)')
        await self.observe('What launch codename did we agree?',owner=2,message=2)
        scope = CURRENT_SCOPE.set(TransportScope(-710,5,'supergroup',2,2,True))
        try:
            turn = await self.runtime.prepare(-710,2,'What launch codename did we agree?',2)
            self.assertTrue(turn.public_memory_ids)
            await self.runtime.mark_included(turn,turn.public_memory_ids)
            method = AsyncMock(return_value=SimpleNamespace(message_id=900,text='Amber Harbor',chat=SimpleNamespace(id=-710)))
            retrieved,sent = await asyncio.wait_for(asyncio.gather(
                self.retrieve(cid,context),
                send_with_receipt(method,(),dict(chat_id=-710,text='Amber Harbor'),'text')),
                timeout=10)
            self.assertTrue(retrieved)
            self.assertEqual(sent.message_id,900)
            method.assert_awaited_once()

            # A retrieval may finish before or after opt-out, but a send that
            # starts after revocation commits must never use its stale packet.
            revoked = asyncio.Event()
            blocked_method = AsyncMock()
            async def opt_out():
                await self.runtime.groups.policies.opt_out(-710,1,True)
                revoked.set()
            async def stale_send():
                await revoked.wait()
                with self.assertRaises(DeliverySuppressed):
                    await send_with_receipt(blocked_method,(),dict(chat_id=-710,text='Amber Harbor'),'text')
            await asyncio.wait_for(asyncio.gather(self.retrieve(cid,context),opt_out(),stale_send()),timeout=10)
            blocked_method.assert_not_awaited()
        finally:
            CURRENT_SCOPE.reset(scope)

    async def test_slow_query_rolls_back_partial_records_before_empty_result(self):
        cid,context,_ = await self.observe()
        async def slow_log(conn,*args):
            # retrieve has already inserted the source-backed record here.
            await conn.execute('SELECT pg_sleep(2)')
        start = asyncio.get_running_loop().time()
        with patch.object(self.memory,'_log',side_effect=slow_log):
            self.assertEqual(await self.retrieve(cid,context),[])
        self.assertLess(asyncio.get_running_loop().time()-start,1.5)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM cognitive_artifacts WHERE context_id=$1 AND kind='public_record'",cid),0)
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM cognitive_retrievals WHERE context_id=$1',cid),0)
        # The pool connection's transaction-local budget does not poison reuse.
        self.assertEqual(len(await self.retrieve(cid,context)),1)

    async def test_whole_operation_budget_includes_non_query_waits(self):
        cid,context,_ = await self.observe()
        async def slow_scope(*args):
            await asyncio.sleep(2)
        start = asyncio.get_running_loop().time()
        with patch.object(self.memory,'_scope',side_effect=slow_scope):
            self.assertEqual(await self.retrieve(cid,context),[])
        self.assertLess(asyncio.get_running_loop().time()-start,1.5)
        self.assertEqual(len(await self.retrieve(cid,context)),1)

    async def test_contended_chat_lock_times_out_without_partial_records(self):
        cid,context,_ = await self.observe()
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',context.chat_id)
            start = asyncio.get_running_loop().time()
            self.assertEqual(await self.retrieve(cid,context),[])
            self.assertLess(asyncio.get_running_loop().time()-start,1.5)
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM cognitive_artifacts WHERE context_id=$1 AND kind='public_record'",cid),0)
        self.assertEqual(len(await self.retrieve(cid,context)),1)

    async def test_validation_and_inclusion_timeouts_fail_closed(self):
        cid,context,_ = await self.observe()
        rows = await self.retrieve(cid,context)
        ids = [rows[0]['artifact_id']]
        async def slow_validation(conn,*args):
            await conn.execute('SELECT pg_sleep(2)')
        with patch.object(self.memory,'_validate_locked',side_effect=slow_validation):
            with self.assertRaises(SuppressedEvidence):
                await self.memory.validate(cid,context,ids,self.at)
            with self.assertRaises(SuppressedEvidence):
                await self.memory.record_retrieval(cid,context,2,'included-timeout','included',ids,self.at)
            async with self.pool.acquire() as conn:
                before = await conn.fetchval('SELECT projection_revision FROM cognitive_contexts WHERE id=$1',cid)
                with self.assertRaises(SuppressedEvidence):
                    async with conn.transaction():
                        await conn.execute('UPDATE cognitive_contexts SET projection_revision=projection_revision+7 WHERE id=$1',cid)
                        await self.memory.validate(cid,context,ids,self.at,connection=conn)
                self.assertEqual(await conn.fetchval('SELECT projection_revision FROM cognitive_contexts WHERE id=$1',cid),before)
                self.assertIsNone(await conn.fetchval("SELECT id FROM cognitive_retrievals WHERE context_id=$1 AND stage='included'",cid))
        self.assertEqual((await self.memory.validate(cid,context,ids,self.at))[0],ids)
