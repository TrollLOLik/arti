"""Reproducible mechanism contrasts and a disposable end-to-end trajectory."""
import asyncio
import json
import math
import random
import statistics
import time
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path

from cognition.affect import initial_state,appraise,advance,affect,expression
from cognition.memory_dynamics import encode_details,reconstruct,reactivate,detail_state
from cognition.relationships import initial_relationship,relationship_transition,relationship_view
from cognition.regulation import regulate
from cognition.reappraisal import explained_perception
from cognition.serialization import dump
from cognition.types import *
from tests.cognition.test_full_model import situation,RecordedInterpreter
from tests.cognition.test_affect import appraisal

AT = datetime(2026,9,30,12,tzinfo=timezone.utc)


def observation(name,text='Событие',at=AT,origin=Origin.USER,context=None):
    return CognitiveEvent(name,context or ContextKey('arti',10),EvidenceRef(name,name,origin,1),at,at,text,1 if origin==Origin.USER else None)


def perceived(ev,**changes):
    a = appraisal(evidence_ids=(ev.evidence.source_id,),target_id=1,**changes)
    return Perception(ev.event_id,PERCEPTION_VERSION,(a,),situation(ev,kind='conflict',intention_evidence='explicit',outcome='confirmed',social_signal='insult'))


def pure_contrasts():
    ev = observation('conflict')
    original = perceived(ev)
    state = appraise(initial_state(ev.context,AT),ev,original)
    def anger(s):
        return sum(e.intensity for e in s.episodes if e.emotion=='anger')
    accidental = replace(original,appraisals=tuple(replace(a,intentionality=0.) for a in original.appraisals))
    uncertain = replace(original,situation=replace(original.situation,intention_evidence='ambiguous',outcome='unknown'))
    variants = [appraise(initial_state(ev.context,AT),ev,p) for p in (original,accidental,uncertain)]
    later = advance(state,AT+timedelta(hours=3))
    r = relationship_transition(initial_relationship(),ev,original.situation)
    before_expression = dump(state)
    sticker_plan = expression(state)
    _,without_sticker = regulate(state,original.situation,{'stickers':False})
    noise,helped = initial_relationship(),initial_relationship()
    for i in range(100):
        n = observation('noise'+str(i),at=AT+timedelta(seconds=i))
        noise = relationship_transition(noise,n,situation(n))
    for i in range(5):
        n = observation('help'+str(i),at=AT+timedelta(hours=i))
        helped = relationship_transition(helped,n,situation(n,social_signal='fulfilled',intention_evidence='explicit',outcome='confirmed'))
    support_ev = observation('explanation','Это было случайно.')
    support = Perception(support_ev.event_id,PERCEPTION_VERSION,(),replace(situation(support_ev,kind='clarification'),
        revisions=({'span':0,'source_id':ev.event_id,'interpretation':'Случайность','confidence':.95,'attribution':'accidental'},)))
    revised = explained_perception(original,support)
    revision_state = appraise(initial_state(ev.context,AT),ev,revised)
    decision,restrained = regulate(state,original.situation,task_serious=True)
    mixed = Perception(ev.event_id,PERCEPTION_VERSION,(
        replace(original.appraisals[0],goal_id='help_user',congruence=1.),
        replace(original.appraisals[0],goal_id='user_wellbeing',loss=1.,irreversibility=1.)),original.situation)
    mixed_state = appraise(initial_state(ev.context,AT),ev,mixed)
    replay_ev = observation('replay',origin=Origin.REPLAY)
    replay_state = appraise(state,replay_ev,Perception(replay_ev.event_id,PERCEPTION_VERSION,()))
    m_ev = observation('detail','Мы начали проект 14 мая.')
    m_s = situation(m_ev,spans=[dict(start=0,end=len(m_ev.text),text=m_ev.text),dict(start=m_ev.text.index('14 мая.'),end=len(m_ev.text),text='14 мая.')],
                    details=[dict(span=0,kind='gist',centrality=.9,confidence=.9),dict(span=1,kind='date',centrality=.8,confidence=.9)])
    details = encode_details(m_ev,m_s)
    trace = dict(source_id=m_ev.event_id,details=details,observed_at=AT.isoformat(),modality='reported')
    old = AT+timedelta(days=90)
    broad,specific = reconstruct(trace,old),reconstruct(trace,old,cue=1.)
    reinforced = reactivate(details[0],AT,old)
    rows = {
        'E1':dict(passed=anger(variants[0])>anger(variants[1]) and anger(variants[0])>anger(variants[2]),anger=[anger(s) for s in variants]),
        'E2':dict(passed=anger(later)<anger(state) and affect(later)['mood_valence']!=affect(initial_state(ev.context,later.last_at))['mood_valence'],initial=affect(state),later=affect(later)),
        'E3':dict(passed=sticker_plan.sticker_mood!=without_sticker.sticker_mood and dump(state)==before_expression,expression_changes_state=False),
        'E4':dict(passed=noise['dimensions']['familiarity']['alpha']>helped['dimensions']['familiarity']['alpha'] and noise['dimensions']['reliability']['alpha']<helped['dimensions']['reliability']['alpha']),
        'E5':dict(passed=decision.strategy=='withhold_expression' and anger(revision_state)<anger(state),suppression_changes_feeling=False,reappraised_anger=anger(revision_state)),
        'E6':dict(passed={'joy','sadness'}<=set(e.emotion for e in mixed_state.episodes),emotions=sorted(set(e.emotion for e in mixed_state.episodes))),
        'E7':dict(passed=replay_state.episodes==state.episodes and replay_state.mood_valence_latent==state.mood_valence_latent,new_witness_from_replay=False),
        'E10':dict(passed=relationship_view(r,AT)['dimensions']['benevolence']['value']==relationship_view(r,AT+timedelta(days=7))['dimensions']['benevolence']['value']),
        'M1':dict(passed=len(specific['details'])>len(broad['details']),broad_details=len(broad['details']),specific_details=len(specific['details'])),
        'M2':dict(passed=detail_state(details[0],AT,old)['accessibility']>detail_state(details[1],AT,old)['accessibility']),
        'M3':dict(passed=reinforced['confidence']==details[0]['confidence'] and reinforced['strength']>details[0]['strength'])
    }
    ablations = {
        'without_appraisal':dict(full_anger=anger(state),ablated_anger=0.,lost='causal attribution'),
        'sentiment_only':dict(equal_negative_label_for_all_three=True,full_anger=[anger(s) for s in variants],lost='intentional versus accidental harm'),
        'without_slow_mood':dict(full_mood=affect(later)['mood_valence'],ablated_mood=affect(initial_state(ev.context,later.last_at))['mood_valence'],lost='lasting affect after a fast episode'),
        'collapsed_relationship':dict(full_dimensions=noise['dimensions'],scalar_cannot_separate_familiarity_and_reliability=True),
        'without_regulation':dict(full_tone=restrained.tone,ablated_tone=sticker_plan.tone,lost='appropriate expression under a serious task'),
        'without_replay':dict(full_stability=reactivate(details[0],AT,old,replay=True)['stability_days'],ablated_stability=details[0]['stability_days']),
    }
    rng = random.Random(728194)
    running = initial_state(ev.context,AT)
    timings = []
    for i in range(3000):
        at = AT+timedelta(minutes=i*31)
        item = observation('long:'+str(i),at=at)
        p = perceived(item,congruence=rng.choice((-.8,.6,0.)),loss=rng.choice((0.,.8)))
        started = time.perf_counter()
        running = replace(appraise(running,item,p),applied_groups=frozenset())
        timings.append(time.perf_counter()-started)
        if not all(math.isfinite(v) for v in affect(running).values()):
            raise RuntimeError('Numerical failure in long trajectory')
    return rows,ablations,dict(events=3000,seed=728194,simulated_days=3000*31/1440,maximum_working_episodes=128,
        final_working_episodes=len(running.episodes),pure_p50_ms=statistics.median(timings)*1000,
        pure_p95_ms=sorted(timings)[int(.95*len(timings))]*1000,final=affect(running))


async def database_contrasts(rows,ablations):
    from tests.support.database import isolated_database
    from cognition.runtime import CognitiveRuntime,CURRENT_TURN
    from cognition.forgetting import forget_cognitive_sources
    async with isolated_database() as pool:
        at = AT
        interpreter = RecordedInterpreter()
        runtime = await CognitiveRuntime(pool,interpreter,'active',clock=lambda:at).initialize(start_worker=False)
        latencies = []
        for i in range(120):
            started = time.perf_counter()
            text = f'Общий проект. Контрольная точка {i}.'
            cid,eid,ev = await runtime.ingest(10,1,text,i)
            if i in (10,20):
                p = perceived(ev,congruence=1. if i==10 else -1.)
                interpreter.frames[text] = replace(p,situation=replace(p.situation,topic='project'))
            await runtime.process(cid,eid)
            await runtime.personal_state(cid,1)
            latencies.append(time.perf_counter()-started)
            at += timedelta(minutes=31)
        warm = await runtime.memory.retrieve(cid,1,'Общий проект',at,'warm',mood=1.)
        cold = await runtime.memory.retrieve(cid,1,'Общий проект',at,'cold',mood=-1.)
        neutral = await runtime.memory.retrieve(cid,1,'Общий проект',at,'neutral',mood=0.)
        rows['E8'] = dict(passed=[r['artifact_id'] for r in warm]!=[r['artifact_id'] for r in cold] and all(abs(r['score']-next(n['score'] for n in neutral if n['artifact_id']==r['artifact_id']))<=.11 for r in warm if any(n['artifact_id']==r['artifact_id'] for n in neutral)),
            warm_ids=[r['artifact_id'] for r in warm],cold_ids=[r['artifact_id'] for r in cold],bias_max=.05,counterevidence_slot=True)
        ablations['without_emotional_retrieval_bias'] = dict(full_order=rows['E8']['warm_ids'],ablated_order=[r['artifact_id'] for r in neutral],maximum_score_contribution=.05)
        cid2,eid2,other = await runtime.ingest(10,2,'OTHER_OWNER_SECRET',200)
        await runtime.process(cid2,eid2)
        cidrp,eidrp,rpev = await runtime.ingest(10,1,'RP_SECRET',201,'rp')
        await runtime.process(cidrp,eidrp)
        retrieved = await runtime.memory.retrieve(cid,1,'SECRET',at,'isolation')
        rows['E9'] = dict(passed='OTHER_OWNER_SECRET' not in str(retrieved) and 'RP_SECRET' not in str(retrieved))
        # A grounded clarification changes an old trace while retaining its source.
        cid,source_id,cause = await runtime.ingest(10,1,'Это твоя ошибка.',300)
        interpreter.frames[cause.text] = perceived(cause)
        await runtime.process(cid,source_id)
        at += timedelta(seconds=1)
        _,support_id,support_ev = await runtime.ingest(10,1,'Это была случайность.',301)
        s = replace(situation(support_ev,kind='clarification'),revisions=({'span':0,'source_id':cause.event_id,'interpretation':'Случайность','confidence':.9,'attribution':'accidental'},))
        interpreter.frames[support_ev.text] = Perception(support_ev.event_id,PERCEPTION_VERSION,(),s)
        await runtime.process(cid,support_id)
        versions = await runtime.memory.artifacts(cid,1,'memory_version')
        rows['M4'] = dict(passed=bool(versions) and any(r['payload']['interpretation']=='Случайность' for r in await runtime.memory.artifacts(cid,1,'trace')),historical_versions=len(versions))
        ablations['without_memory_in_appraisal'] = dict(full_reappraisal=bool(versions),ablated_reappraisal=False,lost='grounded connection to the earlier cause')
        await runtime.memory.replay(cid,source_id,at+timedelta(days=2))
        await forget_cognitive_sources(pool,cid,1,[cause.event_id])
        still = await runtime.memory.artifacts(cid,1)
        rows['M5'] = dict(passed='Это твоя ошибка.' not in str(still),deleted_after_replay=True)
        CURRENT_TURN.set(None)
        await runtime.close()
        return dict(events=124,interpreter_calls=interpreter.calls,external_provider_calls=0,
                    end_to_end_p50_ms=statistics.median(latencies)*1000,end_to_end_p95_ms=sorted(latencies)[int(.95*len(latencies))]*1000)


async def main():
    rows,ablations,long = pure_contrasts()
    db = await database_contrasts(rows,ablations)
    report = dict(model_version=MODEL_VERSION,criteria='predeclared-mechanism-contrasts-v1',hypotheses=rows,
                  ablations=ablations,long_trajectory=long,database_trajectory=db,
                  human_naturalness_validated=False,biological_validity_claimed=False,
                  passed=sum(r['passed'] for r in rows.values()),total=len(rows))
    Path('docs/evaluation/mechanisms.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('hypotheses','ablations')},ensure_ascii=True))
    return 0 if report['passed']==report['total'] else 1


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
