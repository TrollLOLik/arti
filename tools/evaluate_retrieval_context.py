"""Frozen synthetic ranking/scope checks. No model weights or provider calls.

The deterministic encoder supplies declared semantic equivalences to isolate
retrieval/ranking behavior. Results do NOT measure real MiniLM language quality.
"""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from cognition.semantic import SemanticIndex,query_terms
from cognition.memory_repository import MemoryRepository
from cognition.repositories import CognitiveRepository,ensure_schema
from cognition.situations import Situation,SourceSpan
from cognition.types import Perception,PERCEPTION_VERSION
from tests.cognition.test_affect import event,AT
from tests.support.database import isolated_database


class DeclaredEncoder:
    concepts=(frozenset('budget spending expenditure funded finance'.split()),
              frozenset('flight travel journey airline'.split()),
              frozenset('violin recital music'.split()),frozenset('telescope astronomy'.split()))
    async def encode(self,texts,timeout=.6):
        result=[]
        for text in texts:
            words=query_terms(text); vector=[0.]*384
            for i,concept in enumerate(self.concepts):
                if words & concept: vector[i]=1.
            if not any(vector): vector[383]=1.
            result.append(vector)
        return result
    async def close(self): pass


async def evaluate(pool):
    await ensure_schema(pool)
    index=SemanticIndex(pool,DeclaredEncoder()); memory=MemoryRepository(pool,index); repo=CognitiveRepository(pool)
    corpus=(('alice_budget','Alice approved the budget.','Alice','approved the budget','finance',1),
            ('bob_budget','Bob rejected the budget.','Bob','rejected the budget','finance',1),
            ('alice_flight','Alice cancelled the flight.','Alice','cancelled the flight','travel',1),
            ('music','Clara rehearsed violin.','Clara','rehearsed violin','music',1),
            ('private','Alice funded expenditure secretly.','Alice','funded expenditure secretly','finance',2))
    source_ids={}; artifact_ids={}
    for identity,text,name,action,topic,owner in corpus:
        ev=event(identity,text=text)
        ev=replace(ev,evidence=replace(ev.evidence,owner_id=owner),actor_id=owner)
        cid,eid=await repo.observe(ev)
        spans=tuple(SourceSpan(text.index(value),text.index(value)+len(value),value) for value in (text,name,action))
        situation=Situation(topic=topic,spans=spans,details=tuple(dict(span=i,kind=kind,centrality=.9,confidence=.9) for i,kind in enumerate(('gist','name','action'))))
        await memory.encode(cid,eid,Perception(ev.event_id,PERCEPTION_VERSION,(),situation=situation))
        source_ids[identity]=eid
    while await index.backfill(limit=16): pass
    original=await memory.artifacts(cid,1,'trace',limit=100)
    from cognition.serialization import dump
    for row in original: artifact_ids[row['payload']['source_id']]=row['id']
    cases=(('named_finance','What did Alice decide about spending?','alice_budget',{'bob_budget','alice_flight','private'}),
           ('named_travel','How did Alice organize her journey?','alice_flight',{'bob_budget','alice_budget','private'}),
           ('zero_overlap_paraphrase','Who funded expenditure?',None,{'alice_flight','music','private'}),
           ('wrong_event_same_name','Alice violin recital',None,{'alice_budget','alice_flight','private'}),
           ('unrelated_abstention','Who repaired the telescope?',None,{'alice_budget','bob_budget','alice_flight','music','private'}))
    results=[]
    try:
        for key,query,expected,excluded in cases:
            async with pool.acquire() as conn:
                await conn.executemany('UPDATE cognitive_artifacts SET payload=$2::jsonb WHERE id=$1',[(r['id'],dump(r['payload'])) for r in original])
            recalled=await memory.retrieve(cid,1,query,AT,key,limit=8)
            actual=[r['source_id'] for r in recalled]
            if key=='zero_overlap_paraphrase': passed=bool(set(actual)&{'alice_budget','bob_budget'}) and not set(actual)&excluded
            elif key in ('wrong_event_same_name','unrelated_abstention'): passed=not actual
            else: passed=bool(actual) and actual[0]==expected and not set(actual)&excluded
            results.append(dict(case=key,query=query,expected_first=expected,retrieved=actual,passed=passed))
        # Dense graph/high mood must not introduce unsupported neighbors.
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO cognitive_memory_links(context_id,source_id,target_id,kind,weight) VALUES($1,$2,$3,'semantic',1) ON CONFLICT DO NOTHING",cid,artifact_ids['alice_budget'],artifact_ids['music'])
        actual=[r['source_id'] for r in await memory.retrieve(cid,1,'budget',AT,'neighbor',mood=1.,limit=8)]
        results.append(dict(case='irrelevant_link_neighbor',retrieved=actual,passed='music' not in actual and bool(actual)))
        async with pool.acquire() as conn:
            await conn.execute('UPDATE cognitive_events SET suppressed_at=NOW() WHERE id=$1',source_ids['alice_budget'])
        actual=[r['source_id'] for r in await memory.retrieve(cid,1,'Alice spending',AT,'suppressed',limit=8)]
        results.append(dict(case='suppressed_source_excluded',retrieved=actual,passed='alice_budget' not in actual and 'private' not in actual))
        return dict(contract='retrieval-context-synthetic-v1',encoder='declared-384d-concept-equivalences',
                    actual_embedding_quality_measured=False,provider_calls=0,telegram_calls=0,
                    private_history_used=False,disposable_database=True,cases=results,
                    passed=sum(r['passed'] for r in results),total=len(results),
                    limitations=['Synthetic finite corpus; no claim about real multilingual embedding accuracy.',
                                 'Participant cues use exact source-backed name tokens; aliases and inflections are unmeasured.',
                                 'Scoring weights are engineering heuristics, not calibrated probabilities.'])
    finally: await index.close()


async def main():
    async with isolated_database() as pool: report=await evaluate(pool)
    path=Path('docs/evaluation/retrieval_context_synthetic.json')
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['passed']==report['total'] else 1


if __name__=='__main__': raise SystemExit(asyncio.run(main()))
