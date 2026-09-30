"""Synthetic-only group arbiter evaluation; no Telegram or working history."""
import argparse
import asyncio
import json
import time
import hashlib
import math
import statistics
from pathlib import Path
from datetime import datetime,timedelta,timezone
from dotenv import load_dotenv
from ai.group_participation import OpenRouterGroupJudge
from cognition.group_context import build_frame,candidate_kind
from cognition.group_policy import GroupPolicy


def enrich_report(report,fixture):
    """Small-sample uncertainty; expected labels are not human appropriateness ratings."""
    def rate(passed,total):
        if not total: return dict(count=passed,total=0,rate=None,wilson_95=None)
        z=1.95996398454; p=passed/total; denominator=1+z*z/total
        middle=(p+z*z/(2*total))/denominator
        half=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/denominator
        return dict(count=passed,total=total,rate=p,wilson_95=[max(0.,middle-half),min(1.,middle+half)])
    records=report['records']; positive=[r for r in records if r['expected']=='speak']; negative=[r for r in records if r['expected']=='abstain']
    spoken=[r for r in records if r['action']=='speak']
    report['fixture_sha256']=hashlib.sha256(Path(fixture).read_bytes()).hexdigest()
    report['decision_metrics']=dict(recall=rate(sum(r['action']=='speak' for r in positive),len(positive)),
        false_inclusions=rate(sum(r['action']=='speak' for r in negative),len(negative)),
        expected_label_precision=rate(sum(r['expected']=='speak' for r in spoken),len(spoken)),
        human_appropriateness_precision=None)
    latencies=sorted(r['seconds'] for r in records if 'seconds' in r)
    if latencies: report['latency_seconds']=dict(p50=statistics.median(latencies),p95=latencies[math.ceil(.95*len(latencies))-1])
    usage=report.get('provider_usage',[])
    report['usage_totals']={key:sum(u[key] for u in usage if isinstance(u.get(key),(int,float))) for key in ('prompt_tokens','completion_tokens','cost')}
    report['usage_totals']['calls_with_cost_reported']=sum(u.get('cost') is not None for u in usage)
    return report


async def evaluate(live=False,fixture='tests/fixtures/group_scenarios.json',output=None):
    fixtures=json.loads(Path(fixture).read_text(encoding='utf-8'))
    judge=OpenRouterGroupJudge(); records=[]; limiter=asyncio.Semaphore(2)
    async def one(case):
        async with limiter:
            now=datetime(2026,9,30,10,tzinfo=timezone.utc)
            messages=[dict(message_id=i+1,source_id='synthetic:'+case['id']+':'+str(i+1),event_id=i+1,
                           owner_id=m['owner_id'],text=m['text'],reply_to_id=m.get('reply_to_id'),sender_kind='user',
                           directed=m.get('directed',False),is_bot=m.get('is_bot',False),addressed_elsewhere=m.get('addressed_elsewhere',False),
                           at=(now+timedelta(seconds=i)).isoformat()) for i,m in enumerate(case['messages'])]
            frame=build_frame(1,-999,7,messages)
            candidate=dict(kind=case.get('candidate') or 'open_question',mode=case.get('mode','useful'),message_id=case.get('anchor',1),reactions=False,age_seconds=45)
            if not live:
                allowed=candidate_kind(frame,messages[-1],GroupPolicy(mode=candidate['mode'],full_visibility=True)) is not None
                return dict(id=case['id'],expected=case['decision'],action='speak' if allowed else 'abstain',passed=allowed==(case['decision']=='speak'),layer='candidate_rules')
            start=time.perf_counter()
            try:
                decision=await judge.assess(frame,candidate)
                effective='speak' if decision.action=='speak' and decision.confidence>=.7 and decision.usefulness>=.65 and decision.interruption<=.35 else 'abstain'
                text=await judge.compose(frame,candidate,decision) if effective=='speak' else ''
                return dict(id=case['id'],expected=case['decision'],action=effective,reason=decision.reason,passed=effective==case['decision'],
                            confidence=decision.confidence,usefulness=decision.usefulness,interruption=decision.interruption,seconds=round(time.perf_counter()-start,2),response=text,layer='live_arbiter')
            except Exception:
                return dict(id=case['id'],expected=case['decision'],action='abstain',passed=False,error='provider_or_schema_failure',seconds=round(time.perf_counter()-start,2),layer='live_arbiter')
    try: records=await asyncio.gather(*(one(c) for c in fixtures['scenarios']))
    finally: await judge.close()
    report=dict(version=fixtures['version'],model=judge.model if live else 'rules',live=live,scenarios=len(records),passed=sum(r['passed'] for r in records),
                provider_calls=judge.calls,provider_usage=judge.metrics,telegram_calls=0,real_history_used=False,human_ratings=False,records=records)
    enrich_report(report,fixture)
    dest=Path(output or ('docs/evaluation/group_live.json' if live else 'docs/evaluation/group_rules.json'))
    dest.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('records','provider_usage')},ensure_ascii=True))
    return 0 if report['passed']==report['scenarios'] else 1


if __name__=='__main__':
    load_dotenv(); parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--live',action='store_true')
    parser.add_argument('--fixture',default='tests/fixtures/group_scenarios.json'); parser.add_argument('--output')
    args=parser.parse_args()
    raise SystemExit(asyncio.run(evaluate(args.live,args.fixture,args.output)))
