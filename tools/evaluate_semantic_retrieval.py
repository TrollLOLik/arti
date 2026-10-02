"""Compare complete retrieval on identical synthetic ledgers in disposable SQL."""
import asyncio
import json
from pathlib import Path
from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.semantic import SemanticIndex
from cognition.serialization import dump
from tests.support.database import isolated_database
from tests.cognition.test_full_model import RecordedInterpreter
from tools.evaluate_semantic_memory import CASES


async def main():
    async with isolated_database() as pool:
        index=SemanticIndex(pool)
        await index.encoder.encode(['Warm local model'],timeout=30)
        runtime=await CognitiveRuntime(pool,RecordedInterpreter(),semantic=index).initialize(start_worker=False)
        try:
            source_ids=[]
            for message,(source,_) in enumerate(CASES,1):
                cid,eid,ev=await runtime.ingest(10,1,source,message)
                await runtime.process(cid,eid)
                source_ids.append(ev.evidence.source_id)
            while await index.backfill(limit=16): pass
            original=await runtime.memory.artifacts(cid,1,'trace',limit=100)
            reports={}
            for label in ('lexical_hybrid','semantic_hybrid'):
                runtime.memory.semantic=index if label=='semantic_hybrid' else None
                ranks=[]
                for i,(_,query) in enumerate(CASES):
                    # Accessibility/rehearsal start identically for each comparison.
                    async with pool.acquire() as conn:
                        await conn.executemany('UPDATE cognitive_artifacts SET payload=$2::jsonb WHERE id=$1',
                            [(r['id'],dump(r['payload'])) for r in original])
                    recalled=await runtime.memory.retrieve(cid,1,query,runtime.clock(),label+str(i),limit=20)
                    order=[r['source_id'] for r in recalled]
                    ranks.append(order.index(source_ids[i])+1 if source_ids[i] in order else None)
                reports[label]=dict(recall_at_1=sum(r==1 for r in ranks)/len(CASES),
                    recall_at_3=sum(r is not None and r<=3 for r in ranks)/len(CASES),ranks=ranks)
            report=dict(cases=len(CASES),synthetic_only=True,working_database_mutated=False,
                comparison='identical_trace_accessibility_before_each_query',results=reports)
            Path('docs/evaluation/semantic_retrieval_sql.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(report))
        finally:
            CURRENT_TURN.set(None)
            await runtime.close()

if __name__=='__main__':
    asyncio.run(main())
