"""Controlled four-arm response comparison on synthetic contexts only.

Legacy uses the existing, repaired legacy update_state/directive code. This is
an adapter experiment, not a historical production-bot replay or human rating.
"""
import asyncio
import json
import random
from datetime import datetime,timezone
from pathlib import Path
from dotenv import load_dotenv
from cognition.affect import initial_state,appraise,expression
from cognition.prompting import memory_for_prompt
from cognition.memory_dynamics import reconstruct
from cognition.runtime import CURRENT_TURN
from cognition.types import *
from tools.evaluate_full_cognition import DEVELOPMENT
from tools.evaluate_blind_responses import Completion,ROLE
from tests.support.database import isolated_database


async def main():
    from tools.legacy_baseline import build_emotional_directive
    from tools.legacy_baseline import ChatEmotionalState
    provider = Completion()
    packet,key,ratings = [],{},[]
    rng = random.Random(509341)
    at = datetime(2026,9,30,12,tzinfo=timezone.utc)
    try:
        async with isolated_database():
            for index,(name,text,_,_) in enumerate(c for c in DEVELOPMENT if c[0] in ('grief','boundary','mixed','sarcasm')):
                CURRENT_TURN.set(None)
                source = 'synthetic:'+name
                ev = CognitiveEvent(source,ContextKey('arti',9500+index),EvidenceRef(source,source,Origin.USER,1),at,at,text,1)
                p = Perception.from_dict(json.loads((Path('tests/fixtures/full_perceptions/full_v3/development')/(name+'.json')).read_text(encoding='utf-8')))
                full_plan = expression(appraise(initial_state(ev.context,at),ev,p)).instruction()
                neutral_plan = expression(initial_state(ev.context,at)).instruction()
                past_text = 'Пользователь просил отвечать сдержанно и без длинных вступлений.'
                detail = dict(text=past_text,kind='gist',strength=.8,stability_days=45.,fidelity=1.,confidence=.8,vividness=.3,last_recalled=None,recall_count=0)
                remembered = dict(artifact_id=1,**reconstruct(dict(source_id='synthetic:earlier',details=[detail],observed_at=at.isoformat(),modality='reported'),at))
                memory,_ = memory_for_prompt([remembered],[])
                legacy = await ChatEmotionalState.update_state(ev.context.chat_id,text,user_id=1,source_key=source)
                legacy_plan = build_emotional_directive(legacy['charge'],legacy['mood_state'],user_tz=0,rng=random.Random(509341))
                arms = dict(legacy_adapter=(legacy_plan,past_text),appraisal_only=(full_plan,''),
                            memory_only=(neutral_plan,memory),full=(full_plan,memory))
                outputs = await asyncio.gather(*(provider.json(ROLE+'\n'+style,dict(message=text,permitted_memory=recall)) for style,recall in arms.values()))
                if any(set(v)!={'reply'} or not isinstance(v['reply'],str) for v in outputs):
                    raise RuntimeError('Invalid synthetic response schema')
                order = list(zip(arms,outputs))
                rng.shuffle(order)
                row = dict(id=name,context=text,variants={chr(65+i):v['reply'] for i,(_,v) in enumerate(order)})
                packet.append(row)
                key[name] = {chr(65+i):arm for i,(arm,_) in enumerate(order)}
                Path('docs/evaluation/component_packet.json').write_text(json.dumps(packet,ensure_ascii=False,indent=2),encoding='utf-8')
                Path('docs/evaluation/component_key.json').write_text(json.dumps(key,ensure_ascii=False,indent=2),encoding='utf-8')
                rubric = ('Оцени четыре анонимных ответа на одинаковый запрос. Исторический контекст: '+past_text+
                    ' Верни JSON: winner=A|B|C|D|tie; scores — объект с ключами A,B,C,D, каждый с целыми naturalness,appropriateness,uncertainty,continuity от 1 до 5; reason — строка. '
                    'Проверяй соблюдение границ и причины, не поощряй сильные эмоции сами по себе. Оцени только ответы, не угадывай реализацию.')
                rubric += ' Each scores.A, scores.B, scores.C and scores.D MUST be an OBJECT with four separate integer fields naturalness, appropriateness, uncertainty, continuity, not a scalar. Do not put a total score inside scores.'
                for attempt in range(3):
                    rating = await provider.json(rubric,row)
                    scores = rating.get('scores',{})
                    valid = rating.get('winner') in ('A','B','C','D','tie') and isinstance(scores,dict) and set(scores)==set('ABCD')
                    valid = valid and all(isinstance(v,dict) and set(v)=={'naturalness','appropriateness','uncertainty','continuity'}
                        and all(type(n) is int and 1<=n<=5 for n in v.values()) for v in scores.values())
                    if valid:
                        break
                else:
                    raise RuntimeError('Invalid automated rating scale after bounded validation retries')
                ratings.append(dict(id=name,**rating))
    finally:
        await provider.client.aclose()
        Path('docs/evaluation/component_call_metrics.json').write_text(json.dumps(dict(calls=provider.calls,prompt_tokens=provider.prompt_tokens,
            completion_tokens=provider.completion_tokens,reported_cost_usd=provider.cost,cost_unknown_calls=provider.cost_unknown)),encoding='utf-8')
    directory = Path('docs/evaluation')
    (directory/'component_packet.json').write_text(json.dumps(packet,ensure_ascii=False,indent=2),encoding='utf-8')
    (directory/'component_key.json').write_text(json.dumps(key,ensure_ascii=False,indent=2),encoding='utf-8')
    report = dict(model_version=MODEL_VERSION,seed=509341,scenarios=len(packet),arms=['legacy_adapter','appraisal_only','memory_only','full'],
        baseline='existing repaired legacy update_state and directive; controlled synthetic memory; not historical production replay',
        evaluator='automated same model, not independent humans',human_ratings_obtained=False,ratings=ratings,
        calls=provider.calls,prompt_tokens=provider.prompt_tokens,completion_tokens=provider.completion_tokens,
        reported_cost_usd=provider.cost,cost_unknown_calls=provider.cost_unknown,real_transport_calls=0,private_history_exported=False)
    (directory/'component_automated.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='ratings'},ensure_ascii=True))


if __name__=='__main__':
    load_dotenv()
    asyncio.run(main())
