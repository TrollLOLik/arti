"""Offline long trajectories through real SQL/state/delivery code.

Recorded meanings are declared test inputs, NOT measurements of model understanding.
Only a new disposable database is used; this runner never loads .env or transcripts.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from cognition.affect import affect, advance
from cognition.runtime import CognitiveRuntime, CURRENT_TURN
from cognition.scope import CURRENT_SCOPE, TransportScope
from cognition.types import Appraisal, Perception, PERCEPTION_VERSION
from cognition.situations import Situation
from cognition.forgetting import forget_cognitive_sources
from cognition.delivery import DeliveryUnknown
from bot.request_runtime import CURRENT_REQUEST, checkpoint, send, store

AT=datetime(2026,10,1,12,tzinfo=timezone.utc)
TURNS=24
SCENARIOS=('correction_and_ownership','affect_preferences_erasure','scene_boundaries','durable_delivery')


class RecordedMeanings:
    """No classifier/provider: feed explicitly declared meanings into the runtime."""
    def __init__(self): self.frames={}; self.calls=0
    async def interpret(self,event,**kwargs):
        self.calls+=1; spec=self.frames.get(event.text,{})
        data=dict(topic=spec.get('topic','synthetic'),kind=spec.get('kind','neutral'),
            modality='interaction',intention_evidence='unobserved',outcome='unknown',
            spans=[dict(start=0,end=len(event.text),text=event.text)],details=[],beliefs=[],
            intentions=[],revisions=[],preferences=spec.get('preferences',{}),social_signal='contact')
        if spec.get('belief'):
            data['beliefs']=[dict(span=0,subject=event.actor_id,predicate='city',condition='',
                confidence=.9,**spec['belief'])]
        situation=Situation.from_dict(data,event)
        appraisals=[]
        if spec.get('mixed'):
            base=dict(probability=1.,relevance=.9,confidence=.9,novelty=.2,agency_self=0.,
                agency_other=.9,intentionality=.9,control=.6,outcome_probability=1.,future_threat=0.,
                loss=0.,irreversibility=0.,social_exposure=0.,evidence_ids=(event.evidence.source_id,),target_id=event.actor_id)
            appraisals=[Appraisal(goal_id='help_user',congruence=.8,norm_violation=0.,**base),
                        Appraisal(goal_id='mutual_respect',congruence=-.8,norm_violation=.9,**base)]
        return NS(perception=Perception(event.event_id,PERCEPTION_VERSION,tuple(appraisals),situation))


class Trajectory:
    def __init__(self,pool,index):
        self.pool=pool; self.chat=9700+index; self.at=AT; self.turns=0
        self.interpreter=RecordedMeanings(); self.checks={}; self.measurements={}; self.runtime=None
    async def start(self):
        self.runtime=await CognitiveRuntime(self.pool,self.interpreter,'active',clock=lambda:self.at).initialize(False)
        return self
    def check(self,name,value): self.checks[name]=bool(value)
    async def turn(self,text,owner=1,mode='default',meaning=None,delay=1):
        self.turns+=1; self.at+=timedelta(seconds=delay)
        self.interpreter.frames[text]=meaning or {}
        CURRENT_SCOPE.set(TransportScope(self.chat,-1,'private',owner,self.turns))
        result=await self.runtime.prepare(self.chat,owner,text,self.turns,mode)
        state=await self.runtime.personal_state(result.context_id,owner)
        values=affect(state)
        self.check(f'finite_state_{self.turns}',all(math.isfinite(x) for x in values.values()))
        self.check(f'bounded_episodes_{self.turns}',all(0<=e.intensity<=1 for e in state.episodes))
        return result


async def correction_and_ownership(t):
    first=await t.turn('Owner one cobalt orchard is in Kazan',meaning={'belief':dict(value='Kazan',assertion='explicit')})
    other=await t.turn('Owner two ivory lighthouse is in Perm',owner=2,meaning={'belief':dict(value='Perm',assertion='explicit')})
    for n in range(3,19):
        await t.turn(f'Unrelated synthetic planning turn {n}',owner=1 if n%2 else 2,meaning={'topic':f'topic{n%4}'},delay=3600)
    corrected=await t.turn('Correction: owner one now lives in Tomsk',meaning={'belief':dict(value='Tomsk',assertion='correction')})
    for n in range(20,25): await t.turn(f'Later planning observation {n}',owner=1 if n%2 else 2)
    recalled=await t.runtime.memory.retrieve(first.context_id,1,'cobalt orchard',t.at,'long-cue',limit=8)
    t.check('delayed_exact_cue_recalls_original',any(r['source_id']==first.event.evidence.source_id for r in recalled))
    t.check('other_owner_never_recalled',all(r['source_id']!=other.event.evidence.source_id for r in recalled))
    beliefs=await t.runtime.memory.artifacts(first.context_id,1,'belief')
    current=[r['payload']['value'] for r in beliefs if r['payload'].get('predicate')=='city']
    t.check('correction_replaces_current_belief',current==['Tomsk'])
    versions=await t.runtime.memory.artifacts(first.context_id,1,'belief_version')
    t.check('old_claim_preserved_as_history',any(r['payload']['value']=='Kazan' and r['payload']['status']=='superseded' for r in versions))
    others=await t.runtime.memory.artifacts(first.context_id,2,'belief')
    t.check('correction_does_not_mutate_other_owner',any(r['payload']['value']=='Perm' for r in others))
    archive=await t.runtime.memory.retrieve(first.context_id,1,'ivory lighthouse',t.at,'cross-owner',archive=True)
    t.check('archive_owner_fence',not archive)
    t.check('correction_source_retained',any(r['payload']['source_id']==corrected.event.evidence.source_id for r in beliefs))


async def affect_preferences_erasure(t):
    secret=await t.turn('Synthetic violet observatory private detail')
    other=await t.turn('Synthetic silver bridge separate owner',owner=2)
    preference=await t.turn('Only text, without voice or stickers',meaning={'kind':'preference','preferences':{'text':True,'voice':False,'stickers':False,'proactive':False}})
    mixed=0
    for n in range(4,21):
        response=await t.turn(f'Synthetic progress and unresolved concern {n}',meaning={'kind':'success','mixed':n%3==0})
        mixed+=int(response.expression.mixed_affect)
        t.check(f'preference_honored_{n}',response.preferences.get('voice') is False and response.expression.sticker_mood is None)
    state=await t.runtime.personal_state(secret.context_id,1)
    before_groups=state.applied_groups
    after=advance(state,t.at+timedelta(days=30))
    t.check('silence_creates_no_evidence',after.applied_groups==before_groups)
    t.check('silence_decays_not_escalates',sum(e.intensity for e in after.episodes)<=sum(e.intensity for e in state.episodes))
    t.at+=timedelta(days=30)
    await t.turn('Synthetic return after a long quiet interval')
    await forget_cognitive_sources(t.pool,secret.context_id,1,[secret.event.evidence.source_id])
    for n in range(22,25): await t.turn(f'Synthetic post-erasure observation {n}',owner=2 if n==23 else 1)
    forgotten=await t.runtime.memory.retrieve(secret.context_id,1,'violet observatory',t.at,'erased',archive=True)
    t.check('forgotten_source_not_archivable',not forgotten)
    artifacts=await t.runtime.memory.artifacts(secret.context_id,1)
    t.check('forgotten_detail_not_in_projection','violet observatory' not in str(artifacts))
    survivors=await t.runtime.memory.retrieve(secret.context_id,2,'silver bridge',t.at,'survivor',archive=True)
    t.check('other_owner_survives_erasure',any(r['source_id']==other.event.evidence.source_id for r in survivors))
    t.check('mixed_affect_observed',mixed>0)
    relationship=await t.runtime.memory.relationship(secret.context_id,1)
    t.check('independent_preference_survives',relationship['preferences'].get('voice') is False)
    from cognition.repositories import SuppressedEvidence
    from bot.request_codec import encode_value,decode_value
    wire=await encode_value(secret)
    with patch('cognition.runtime.get_runtime',return_value=t.runtime):
        try: await decode_value(wire)
        except SuppressedEvidence: blocked=True
        else: blocked=False
    t.check('stale_prepared_turn_blocked',blocked)


async def scene_boundaries(t):
    first=await t.turn('Old roleplay captain aboard the amber vessel',mode='rp')
    for n in range(2,11): await t.turn(f'Synthetic old-scene dialogue {n}',mode='rp',meaning={'topic':'old_scene'})
    ordinary=await t.turn('Ordinary shopping list outside roleplay')
    t.check('default_separate_from_roleplay',ordinary.context_id!=first.context_id and 'amber vessel' not in ordinary.memory)
    await t.runtime.new_scene(t.chat)
    second=await t.turn('New roleplay librarian in the green archive',mode='rp')
    for n in range(13,25): await t.turn(f'Synthetic new-scene dialogue {n}',mode='rp',meaning={'topic':'new_scene'})
    t.check('scene_identity_changed',second.event.context.scene_id!=first.event.context.scene_id)
    candidates=await t.runtime.memory.retrieve(second.context_id,1,'amber vessel',t.at,'scene-recall',archive=True)
    t.check('new_scene_cannot_recall_old_source',not candidates)
    candidates=await t.runtime.memory.retrieve(second.context_id,1,'green archive',t.at,'scene-current',archive=True)
    t.check('current_scene_keeps_own_source',any(r['source_id']==second.event.evidence.source_id for r in candidates))
    from bot.request_codec import encode_value,decode_value
    from cognition.repositories import SuppressedEvidence
    with patch('cognition.runtime.get_runtime',return_value=t.runtime):
        try: await decode_value(await encode_value(first))
        except SuppressedEvidence: blocked=True
        else: blocked=False
    t.check('old_scene_turn_cannot_resume',blocked)


async def durable_delivery(t):
    delivered=0; unknown=0; replayed=0; transport_calls=0
    for n in range(1,25):
        turn=await t.turn(f'Synthetic request delivery turn {n}')
        job=await store().enqueue('text',t.chat,-1,f'long-delivery-{n}',{})
        current=await store().claim(['text']); CURRENT_REQUEST.set(current)
        async def result(): return f'Synthetic prepared answer {n}'
        value=await checkpoint('response',result)
        async def transport(**kwargs):
            nonlocal transport_calls
            transport_calls+=1
            if n in (8,20): raise TimeoutError('synthetic transport uncertainty')
            return NS(message_id=20000+n,text=kwargs['text'],chat=NS(id=t.chat))
        try: await send(transport,(),dict(chat_id=t.chat,text=value),'message')
        except DeliveryUnknown:
            unknown+=1
            await store().release(current['id'],current['token'])
            t.check(f'unknown_not_reclaimed_{n}',await store().claim(['text']) is None)
            t.check(f'unknown_terminal_{n}',(await store().status(job['id']))['state']=='delivery_unknown')
        else:
            delivered+=1
            if n in (6,12,18,24):
                await store().release(current['id'],current['token'])
                current=await store().claim(['text']); CURRENT_REQUEST.set(current); CURRENT_TURN.set(None)
                no_repeat=AsyncMock(side_effect=AssertionError('completed computation repeated'))
                with patch('cognition.runtime.get_runtime',return_value=t.runtime): restored=await checkpoint('response',no_repeat)
                before=transport_calls
                await send(transport,(),dict(chat_id=t.chat,text=restored),'message')
                replayed+=1
                t.check(f'replay_has_no_transport_{n}',before==transport_calls)
                t.check(f'checkpoint_not_recomputed_{n}',no_repeat.await_count==0)
            await store().finish(current['id'],current['token'],'completed')
        CURRENT_REQUEST.set(None); CURRENT_TURN.set(None)
    async with t.pool.acquire() as conn:
        rows=await conn.fetch("SELECT status,count(*) AS n FROM cognitive_outbox WHERE context_id=$1 GROUP BY status",turn.context_id)
    statuses={r['status']:r['n'] for r in rows}
    t.check('one_transport_per_attempted_delivery',transport_calls==TURNS)
    t.check('confirmed_receipts_count',statuses.get('delivered')==delivered==22)
    t.check('ambiguous_receipts_count',statuses.get('delivery_unknown')==unknown==2)
    t.check('checkpoint_restarts_exercised',replayed==4)
    t.measurements.update(synthetic_transport_attempts=transport_calls,confirmed_receipts=delivered,ambiguous_receipts=unknown,checkpoint_restarts=replayed)


async def run_scenario(pool,name):
    if name not in SCENARIOS: raise ValueError('unknown_synthetic_scenario')
    from materials.runtime import CURRENT_MATERIAL_USE,CURRENT_DERIVATIVE_USE,CURRENT_COMPUTATION_USE
    tokens=[(var,var.set(None if var in (CURRENT_REQUEST,CURRENT_TURN,CURRENT_SCOPE) else ())) for var in
        (CURRENT_REQUEST,CURRENT_TURN,CURRENT_SCOPE,CURRENT_MATERIAL_USE,CURRENT_DERIVATIVE_USE,CURRENT_COMPUTATION_USE)]
    trajectory=await Trajectory(pool,SCENARIOS.index(name)).start()
    try:
        await globals()[name](trajectory)
        trajectory.check('minimum_twenty_turns',trajectory.turns>=20)
        counts={}
        for key,passed in trajectory.checks.items():
            label=re.sub(r'_\d+$','',key)
            count=counts.setdefault(label,dict(assessed=0,passed=0,failed=0))
            count['assessed']+=1; count['passed']+=int(passed); count['failed']+=int(not passed)
        return dict(scenario=name,turns=trajectory.turns,invariants=len(trajectory.checks),
                    invariant_counts=counts,measurements=trajectory.measurements,
                    passed=sum(trajectory.checks.values()),failed=sum(not v for v in trajectory.checks.values()),
                    failed_invariants=[k for k,v in trajectory.checks.items() if not v],
                    recorded_interpretations=trajectory.interpreter.calls)
    finally:
        await trajectory.runtime.close()
        for var,token in reversed(tokens): var.reset(token)


async def evaluate():
    if os.getenv('ARTI_TEST_DB')!='1': raise RuntimeError('explicit_disposable_database_mode_required')
    from tests.support.database import isolated_database
    # The fixture creates a random temporary DB. Disable its optional .env read;
    # only the explicitly supplied test database environment may be consulted.
    with patch('dotenv.load_dotenv',return_value=False), patch('tests.support.database.dotenv_values',return_value={}), patch('httpx.AsyncClient.send',new=AsyncMock(side_effect=AssertionError('network_disabled_in_synthetic_evaluation'))) as network:
        async with isolated_database() as pool:
            rows=[await run_scenario(pool,name) for name in SCENARIOS]
    return dict(suite='long-trajectories-v1',synthetic_only=True,provider_calls=0,network_attempts=network.await_count,real_transport_calls=0,
        human_pilot_completed=False,model_quality_not_measured=True,turns=sum(r['turns'] for r in rows),
        scenarios=len(rows),invariants=sum(r['invariants'] for r in rows),passed=sum(r['passed'] for r in rows),
        failed=sum(r['failed'] for r in rows),results=rows,
        assessed=['state_bounds','source_backed_exact_cue_recall','correction_history','owner_isolation',
                  'preference_retention','mixed_affect_projection','silence_decay','source_erasure',
                  'roleplay_scene_isolation','checkpoint_recovery','confirmed_and_ambiguous_delivery'],
        unassessed=['semantic_interpretation_quality','paraphrase_retrieval_quality','naturalness',
                    'human_satisfaction','live_provider_latency','live_telegram_delivery','human_pilot'])


async def main():
    report=await evaluate()
    path=Path('docs/evaluation/long_conversations_synthetic.json')
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return int(bool(report['failed']))


if __name__=='__main__': raise SystemExit(asyncio.run(main()))
