"""Real local encoder smoke/coverage test on synthetic disposable public/private data."""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.scope import CURRENT_SCOPE,TransportScope
from cognition.semantic import LocalEncoder,SemanticIndex,VERSION,token_safe_segments
from cognition.source_chunks import source_chunk_count
from tests.cognition.test_full_model import RecordedInterpreter
from tests.support.database import isolated_database
from tools.evaluate_semantic_memory import CASES


async def evaluate(directory):
    started=time.perf_counter()
    encoder=LocalEncoder(directory)
    if await encoder.encode(['synthetic warmup'],timeout=30) is None:
        await encoder.close()
        raise RuntimeError('Pinned local encoder is unavailable')
    exposure=[]
    for label,text in [('russian',('Нейтральное описание облаков и камней. '*20)[:640]),
                       ('cjk',('記録された資料には重要な情報が含まれます。'*40)[:640]),
                       ('unicode_expansion','ﷺ'*640)]:
        pieces=token_safe_segments(text,encoder.tokenizer,encoder.max_tokens)
        token_counts=[len(encoder.tokenizer.encode(piece).ids) for _,_,piece in pieces]
        complete=''.join(piece for _,_,piece in pieces)==text and all(count<=encoder.max_tokens for count in token_counts)
        if not complete:
            raise AssertionError('Tokenizer coverage gap')
        exposure.append(dict(case=label,source_characters=len(text),original_tokens=len(encoder.tokenizer.encode(text).ids),
            encoded_parts=len(pieces),max_part_tokens=max(token_counts),model_token_limit=encoder.max_tokens,
            exact_character_coverage=complete))
    async with isolated_database() as pool:
        index=SemanticIndex(pool,encoder)
        runtime=await CognitiveRuntime(pool,RecordedInterpreter(),semantic=index).initialize(False)
        try:
            public_sources=[]
            named_cases=[(f'{name} утвердил бюджет проекта {project}.',f'{name} бюджет') for name,project in [('Алиса','Маяк'),('Борис','Кедр'),('Виктория','Янтарь'),('Дмитрий','Север'),('Елена','Берег')]]
            for message,(text,_) in enumerate(CASES+named_cases,1):
                scope=TransportScope(-940,3,'supergroup',1,message,True,'user')
                token=CURRENT_SCOPE.set(scope)
                try:
                    cid=await runtime.groups.observe(scope,text,'default')
                finally:
                    CURRENT_SCOPE.reset(token)
                async with pool.acquire() as conn:
                    public_sources.append(await conn.fetchval('SELECT event_id FROM group_observations WHERE context_id=$1 AND message_id=$2',cid,message))
            context=await runtime.context(-940,'default',3)
            public=runtime.public_memory.semantic
            batches=0
            from cognition.public_semantic import PUBLIC_VERSION
            async with asyncio.timeout(120):
                while True:
                    async with pool.acquire() as conn:
                        indexed=await conn.fetchval('SELECT coalesce(sum(next_chunk),0) FROM cognitive_public_semantic_progress WHERE context_id=$1 AND embedding_model=$2',cid,PUBLIC_VERSION)
                    if indexed>=len(public_sources):
                        break
                    batches+=bool(await public.backfill(limit=16))
                    await asyncio.sleep(.05)
            comparisons={}; diagnostics=[]
            for label in ('lexical','hybrid'):
                runtime.public_memory.semantic=public if label=='hybrid' else None
                ranked=[]; timings=[]
                for ordinal,(_,query) in enumerate(CASES+named_cases):
                    query_started=time.perf_counter()
                    result=await runtime.public_memory.retrieve(cid,context,query,runtime.clock(),f'{label}-{ordinal}',requester=2,limit=16)
                    timings.append((time.perf_counter()-query_started)*1000)
                    order=list(dict.fromkeys(row['event_id'] for row in result))
                    ranked.append(order.index(public_sources[ordinal])+1 if public_sources[ordinal] in order else None)
                    if label=='hybrid':
                        diagnostics.append(result.diagnostics)
                paraphrases,names=ranked[:len(CASES)],ranked[len(CASES):]
                comparisons[label]=dict(paraphrase_recall_at_1=sum(r==1 for r in paraphrases)/len(paraphrases),
                    paraphrase_recall_at_3=sum(r is not None and r<=3 for r in paraphrases)/len(paraphrases),
                    paraphrase_ranks=paraphrases,exact_name_ranks=names,
                    exact_name_recall_at_1=sum(r==1 for r in names)/len(names),
                    query_median_ms=round(sorted(timings)[len(timings)//2],3),query_max_ms=round(max(timings),3))
            ranks=comparisons['hybrid']['paraphrase_ranks']
            # The informative interior is beyond the old 32-window work cap.
            # Every offset is indexed and the index survives a fresh instance.
            filler='Нейтральное описание облаков и камней. '
            target=CASES[0][0]
            long_text=(filler*1050)+(target+' ')*8+(filler*1050)
            private_cid,eid,event=await runtime.ingest(940,1,long_text,1)
            await runtime.process(private_cid,eid)
            private_batches=0
            async with asyncio.timeout(120):
                while True:
                    async with pool.acquire() as conn:
                        indexed=await conn.fetchval('SELECT coalesce(sum(next_chunk),0) FROM cognitive_semantic_progress WHERE context_id=$1 AND embedding_model=$2',private_cid,VERSION)
                    if indexed>=source_chunk_count(long_text):
                        break
                    private_batches+=bool(await index.backfill(limit=4))
                    await asyncio.sleep(.05)
            resumed=SemanticIndex(pool,encoder)
            private_query_started=time.perf_counter()
            semantic=await resumed.search(private_cid,1,CASES[0][1])
            private_query_ms=round((time.perf_counter()-private_query_started)*1000,3)
            async with pool.acquire() as conn:
                progress=await conn.fetchrow('SELECT next_chunk,total_chunks FROM cognitive_semantic_progress WHERE context_id=$1 AND embedding_model=$2',private_cid,VERSION)
                windows=await conn.fetch('SELECT chunk_start,chunk_end FROM cognitive_semantic_vectors WHERE context_id=$1 AND embedding_model=$2 ORDER BY chunk_start',private_cid,VERSION)
            contiguous=bool(windows) and windows[0]['chunk_start']==0 and windows[-1]['chunk_end']==len(long_text) and all(b['chunk_start']<=a['chunk_end'] for a,b in zip(windows,windows[1:]))
            interior_found=any(target in row['payload']['gist'] for row in semantic)
            full=all(d.get('status')=='complete' for d in diagnostics)
            report=dict(encoder=VERSION,synthetic_only=True,provider_calls=0,production_database_mutated=False,
                tokenizer_exposure=exposure,
                public_cases=len(CASES),public_recall_at_1=sum(r==1 for r in ranks)/len(ranks),
                public_recall_at_3=sum(r is not None and r<=3 for r in ranks)/len(ranks),public_ranks=ranks,
                public_batches=batches,public_complete_coverage=full,public_comparison=comparisons,
                private_source_characters=len(long_text),private_expected_chunks=source_chunk_count(long_text),
                private_indexed_chunks=len(windows),private_batches=private_batches,
                private_contiguous_coverage=contiguous,private_restart_complete=bool(progress and progress['next_chunk']==progress['total_chunks']),
                private_interior_paraphrase_found=interior_found,private_query_diagnostics=semantic.diagnostics,
                private_query_matches=len(semantic),private_query_ms=private_query_ms,private_best_score=max((round(row['semantic_score'],4) for row in semantic),default=None),
                duration_seconds=round(time.perf_counter()-started,3),
                limitations=['Synthetic retrieval evaluation; no live interpretation, answer generation, Telegram, production-size latency or universal paraphrase guarantee.'])
            report['passed']=bool(full and contiguous and interior_found and report['private_restart_complete'] and len(windows)>32 and any(r is not None for r in ranks) and comparisons['hybrid']['exact_name_recall_at_1']==1.)
            return report
        finally:
            CURRENT_TURN.set(None)
            await runtime.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model-dir',required=True)
    parser.add_argument('--report',default='docs/evaluation/hybrid_memory_encoder.json')
    args=parser.parse_args()
    if os.getenv('ARTI_TEST_DB')!='1':
        raise SystemExit('Disposable PostgreSQL and ARTI_TEST_DB=1 are required')
    report=asyncio.run(evaluate(args.model_dir))
    Path(args.report).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report))
    return int(not report['passed'])


if __name__=='__main__':
    raise SystemExit(main())
