"""One scheduling authority per private native request, with real disposable SQL."""
import os
import unittest
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.scope import CURRENT_SCOPE,TransportScope
from cognition.types import Origin
from cognition.serialization import dump
from cognition.intentions import due_intentions,run_intention_cycle
from organizer.repository import Repository
from organizer.runtime import dispatch_once
from organizer.ownership import initialize,claim_source,link_source,source_key,owns_event,owns_intention
from tests.cognition.test_full_model import RecordedInterpreter,situation


@unittest.skipUnless(os.getenv('ARTI_TEST_DB')=='1','disposable PostgreSQL required')
class OrganizerOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        self.db=isolated_database(); self.pool=await self.db.__aenter__()
        self.now=datetime.now(timezone.utc)
        self.interpreter=RecordedInterpreter()
        self.runtime=await CognitiveRuntime(self.pool,self.interpreter,'active',clock=lambda:self.now).initialize(False)
        async with self.pool.acquire() as conn: await initialize(conn)
        self.repo=Repository(self.pool)
        self.tokens=[(v,v.set(value)) for v,value in ((CURRENT_TURN,None),(CURRENT_SCOPE,TransportScope(1,-1,'private',1,1)))]
        self.cognitive=NS(send_message=AsyncMock(return_value=NS(message_id=900,chat=NS(id=1))))
        self.native=AsyncMock(return_value=NS(message_id=901))

    async def asyncTearDown(self):
        for var,token in reversed(self.tokens): var.reset(token)
        await self.runtime.close(); await self.db.__aexit__(None,None,None)

    async def source(self,mid,text=None,origin=Origin.USER,reply_to_id=None):
        text=text or f'Synthetic reminder {mid} at {(self.now-timedelta(minutes=1)).isoformat()}'
        cid,eid,event=await self.runtime.ingest(1,1,text,mid,origin=origin,reply_to_id=reply_to_id)
        return cid,eid,event

    async def interpret_reminder(self,cid,eid,event,key='fixture'):
        self.interpreter.frames[event.text]=situation(event,kind='request',intentions=[dict(span=0,
            key=key,description='Synthetic reminder',cue='',deadline=(self.now-timedelta(minutes=1)).isoformat(),
            status='reminder',confidence=.9)])
        await self.runtime.process(cid,eid)
        return (await self.runtime.memory.artifacts(cid,1,'intention'))[-1]

    async def native_item(self,key='native-1'):
        row=await self.repo.create(1,1,'reminder','Synthetic reminder',key,due_at=self.now+timedelta(minutes=1),timezone_name='UTC')
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_organizer_items SET due_at=NOW()-INTERVAL '1 second' WHERE id=$1",row['id'])
        return row

    async def run_both(self):
        await run_intention_cycle(self.runtime,self.cognitive)
        await dispatch_once(NS(),repo=self.repo,sender=self.native)
        await run_intention_cycle(self.runtime,self.cognitive)
        await dispatch_once(NS(),repo=self.repo,sender=self.native)

    async def test_original_native_request_is_delivered_only_by_native_scheduler(self):
        await claim_source(self.pool,1,1,1)
        cid,eid,event=await self.source(1); await self.interpret_reminder(cid,eid,event)
        await self.native_item(); await self.run_both()
        self.native.assert_awaited_once(); self.cognitive.send_message.assert_not_awaited()
        self.assertEqual(await due_intentions(self.runtime),[])

    async def test_cancelled_native_remains_owned_after_late_interpretation(self):
        await claim_source(self.pool,1,1,1)
        cid,eid,event=await self.source(1)
        item=await self.native_item(); await self.repo.change(1,1,item['id'],'cancelled')
        # Interpretation arrives after cancellation, as after a slow provider/restart.
        await self.interpret_reminder(cid,eid,event); await self.run_both()
        self.native.assert_not_awaited(); self.cognitive.send_message.assert_not_awaited()
        async with self.pool.acquire() as conn:
            self.assertTrue(await owns_event(conn,cid,eid))
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM arti_organizer_source_routes'),1)

    async def test_clarification_and_bot_ack_descendants_are_not_second_reminders(self):
        await claim_source(self.pool,1,1,1)
        cid,root,event=await self.source(1,'Synthetic native request awaiting its time')
        await link_source(self.pool,1,1,source_key(1,1),source_key(1,2))
        _,clarification,clarified=await self.source(2)
        await self.interpret_reminder(cid,clarification,clarified,'clarified')
        _,ack,acknowledged=await self.source(900,origin=Origin.DELIVERED_ACTION)
        # A source chain can be longer than one edge and must preserve the root.
        _,later,later_event=await self.source(3,reply_to_id=900)
        async with self.pool.acquire() as conn:
            await conn.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3) ON CONFLICT DO NOTHING',cid,ack,clarification)
            await conn.execute('INSERT INTO cognitive_event_dependencies VALUES($1,$2,$3) ON CONFLICT DO NOTHING',cid,later,ack)
        await self.interpret_reminder(cid,ack,acknowledged,'ack')
        await self.interpret_reminder(cid,later,later_event,'late')
        await self.native_item(); await self.run_both()
        self.native.assert_awaited_once(); self.cognitive.send_message.assert_not_awaited()
        async with self.pool.acquire() as conn:
            self.assertTrue(await owns_event(conn,cid,later))
            rows=await conn.fetch("SELECT id FROM cognitive_artifacts WHERE context_id=$1 AND kind='intention'",cid)
            self.assertTrue(all([await owns_intention(conn,cid,r['id']) for r in rows]))

    async def test_unrelated_cognitive_reminder_still_delivers(self):
        await claim_source(self.pool,1,1,1)
        cid,eid,event=await self.source(2)
        await self.interpret_reminder(cid,eid,event,'independent')
        await run_intention_cycle(self.runtime,self.cognitive)
        await run_intention_cycle(self.runtime,self.cognitive)
        self.cognitive.send_message.assert_awaited_once()

    async def test_marker_claimed_after_discovery_is_checked_again_before_send(self):
        cid,eid,event=await self.source(1); await self.interpret_reminder(cid,eid,event)
        discovered=await due_intentions(self.runtime); self.assertEqual(len(discovered),1)
        await claim_source(self.pool,1,1,1)
        with patch('cognition.intentions.due_intentions',new=AsyncMock(return_value=discovered)):
            await run_intention_cycle(self.runtime,self.cognitive)
        self.cognitive.send_message.assert_not_awaited()

    async def test_native_marker_is_owner_private_and_idempotent(self):
        from organizer.time import OrganizerError
        await claim_source(self.pool,1,1,1); await claim_source(self.pool,1,1,1)
        with self.assertRaises(OrganizerError): await claim_source(self.pool,1,2,1)
        with self.assertRaises(OrganizerError): await claim_source(self.pool,1,1,2,source_key(2,1))
        with self.assertRaises(OrganizerError): await claim_source(self.pool,1,1,2,source_key(1,99))
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM arti_organizer_source_routes'),1)

    async def test_owned_intentions_cannot_fill_scan_limit_and_starve_unrelated(self):
        await claim_source(self.pool,1,1,1)
        cid,eid,event=await self.source(1); artifact=await self.interpret_reminder(cid,eid,event,'native')
        async with self.pool.acquire() as conn:
            await conn.execute('''INSERT INTO cognitive_artifacts(context_id,owner_id,kind,artifact_key,model_version,payload,projection_epoch)
                SELECT context_id,owner_id,kind,'native-limit-'||n,model_version,payload,projection_epoch
                FROM cognitive_artifacts CROSS JOIN generate_series(1,513) AS n WHERE id=$1''',artifact['id'])
            await conn.execute('''INSERT INTO cognitive_provenance(context_id,artifact_id,source_event_id)
                SELECT context_id,id,$2 FROM cognitive_artifacts WHERE context_id=$1 AND artifact_key LIKE 'native-limit-%' ''',cid,eid)
        _,other,other_event=await self.source(2); unrelated=await self.interpret_reminder(cid,other,other_event,'unrelated')
        rows=await due_intentions(self.runtime)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][0]['payload']['source_id'],other_event.evidence.source_id)
