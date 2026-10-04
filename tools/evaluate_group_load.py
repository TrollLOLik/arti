"""Synthetic PostgreSQL load; recorded arbiter, no provider/Telegram/working writes."""
import asyncio
import json
import time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ai.group_participation import GroupJudgement
from cognition.runtime import CognitiveRuntime
from cognition.scope import TransportScope
from tests.support.database import isolated_database


class RecordedAbstention:
    def __init__(self): self.calls=0
    async def assess(self,frame,candidate):
        self.calls+=1
        return GroupJudgement('abstain','no_added_value',.1,.1,.9,())
    async def compose(self,*args): raise AssertionError('Abstention must not compose')
    async def close(self): pass


async def evaluate():
    clock=[datetime(2026,10,4,12,tzinfo=timezone.utc)]; judge=RecordedAbstention(); started=time.perf_counter()
    peak_pending=0; peak_messages=0; peak_branches=0; peak_questions=0; ingest_latencies=[]; contexts=set()
    bot=SimpleNamespace(send_message=AsyncMock(side_effect=AssertionError('No Telegram allowed')))
    async with isolated_database() as pool:
        runtime=await CognitiveRuntime(pool,None,'active',clock=lambda:clock[0]).initialize(False)
        runtime.groups.judge=judge
        try:
            async with pool.acquire() as conn:
                await conn.execute('INSERT INTO response_status(chat_id,enabled) VALUES(-999,TRUE)')
            await runtime.groups.policies.set(-999,dict(mode='useful',execution='shadow',full_visibility=True,timezone='UTC'))
            for i in range(1,1001):
                clock[0]+=timedelta(milliseconds=100)
                scope=TransportScope(-999,1+i%5,'supergroup',1+i%40,i,False)
                at=time.perf_counter()
                cid=await runtime.groups.observe(scope,(f'Who can explain public example number {i}?' if i%3==0 else f'We need to schedule public task number {i}'),at=clock[0])
                ingest_latencies.append((time.perf_counter()-at)*1000); contexts.add(cid)
                if i%64==0 or i==1000:
                    async with pool.acquire() as conn:
                        pending=await conn.fetchval("SELECT count(*) FROM group_candidates WHERE status IN ('pending','deferred','claimed')")
                    peak_pending=max(peak_pending,pending)
                    for context in contexts:
                        frame=await runtime.groups.frame(context)
                        peak_messages=max(peak_messages,len(frame.messages)); peak_branches=max(peak_branches,len(frame.branches)); peak_questions=max(peak_questions,len(frame.questions))
                if i%256==0 or i==1000:
                    clock[0]+=timedelta(seconds=45)
                    for _ in range(7): await runtime.groups.run_cycle(bot)
            async with pool.acquire() as conn:
                observations=await conn.fetchval('SELECT count(*) FROM group_observations')
                attempts=await conn.fetchval('SELECT coalesce(sum(attempts),0) FROM group_candidates')
                statuses=await conn.fetch('SELECT status,count(*) AS total FROM group_candidates GROUP BY status')
            assert observations==1000 and peak_pending<=128 and peak_messages<=64 and peak_branches<=8 and peak_questions<=16
            assert 0<judge.calls<=12 and bot.send_message.await_count==0
            values=sorted(ingest_latencies)
            report=dict(version='group-load-2026-10-04.2',messages=observations,participants=40,topics=5,
                peak_pending_candidates=peak_pending,peak_frame_messages=peak_messages,peak_branches=peak_branches,peak_questions=peak_questions,
                recorded_assessments=judge.calls,reserved_attempts=attempts,provider_calls=0,telegram_calls=0,
                candidate_statuses={r['status']:r['total'] for r in statuses},
                ingest_p50_ms=round(values[len(values)//2],2),ingest_p95_ms=round(values[949],2),
                seconds=round(time.perf_counter()-started,2),working_database_mutated=False,real_history_used=False,
                cost_measurement='Offline recorded judge; actual provider latency and cost are not measured.',passed=True)
        finally: await runtime.close()
    Path('docs/evaluation/group_load.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__': asyncio.run(evaluate())
