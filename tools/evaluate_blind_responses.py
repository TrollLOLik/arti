"""Synthetic blind packet plus separately labelled automated judging."""
import asyncio
import hashlib
import json
import random
import time
from pathlib import Path
from datetime import datetime,timezone
from dotenv import load_dotenv
import httpx
from cognition.affect import initial_state,appraise,expression
from cognition.interpreter import environment_key
from cognition.serialization import object_value
from cognition.types import *
from tools.evaluate_full_cognition import DEVELOPMENT

CASES = ('neutral','negation','sarcasm','boundary','grief','thanks','apology','mixed')
ROLE = ('Ты Арти, внимательная собеседница. Пиши по-русски, естественно, коротко и по существу. '
        'Не раскрывай технические оценки, не приписывай себе пережитый опыт пользователя, '
        'не выдумывай сведения и не требуй внимания. Ответ — JSON с одним полем reply.')


class Completion:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=90,trust_env=False)
        self.key = environment_key()
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost = 0.
        self.cost_unknown = 0
        self.latencies = []
        self.semaphore = asyncio.Semaphore(3)

    async def json(self,system,user):
        async with self.semaphore:
            for attempt in range(3):
                started = time.perf_counter()
                self.calls += 1
                try:
                    response = await self.client.post('https://openrouter.ai/api/v1/chat/completions',
                        headers={'Authorization':'Bearer '+self.key},json=dict(model='stealth/space-bunny-alpha',
                        messages=[dict(role='system',content=system),dict(role='user',content=json.dumps(user,ensure_ascii=False))],
                        temperature=0,max_tokens=4096,reasoning=dict(effort='medium',exclude=True),response_format=dict(type='json_object')))
                    self.latencies.append(time.perf_counter()-started)
                    body = response.json()
                    usage = body.get('usage') or {}
                    self.prompt_tokens += usage.get('prompt_tokens') or 0
                    self.completion_tokens += usage.get('completion_tokens') or 0
                    if usage.get('cost') is None:
                        self.cost_unknown += 1
                    else:
                        self.cost += usage['cost']
                    return json.loads(body['choices'][0]['message']['content'])
                except (httpx.HTTPError,ValueError,KeyError,IndexError,TypeError):
                    if attempt<2:
                        await asyncio.sleep(1)
            raise RuntimeError('Bounded synthetic response call failed')


async def main():
    from tools.legacy_baseline import build_emotional_directive
    completion = Completion()
    rng = random.Random(817340)
    frames = Path('tests/fixtures/full_perceptions/full_v3/development')
    packet,key,ratings = [],{},[]
    try:
        for name,text,_,_ in DEVELOPMENT:
            if name not in CASES:
                continue
            source = 'synthetic:'+name
            at = datetime(2026,9,30,12,tzinfo=timezone.utc)
            ev = CognitiveEvent(source,ContextKey('arti',1),EvidenceRef(source,source,Origin.USER,1),at,at,text,1)
            p = Perception.from_dict(json.loads((frames/(name+'.json')).read_text(encoding='utf-8')))
            plan = expression(appraise(initial_state(ev.context,at),ev,p))
            # This arm is an explicit sentiment ablation, not a fabricated replay
            # of the historical bot's full state or its real conversations.
            mood = {'angry':.5 if name in ('sarcasm','negation') else 0.,
                    'sad':.6 if name=='grief' else 0.,'happy':.5 if name in ('thanks','mixed') else 0.}
            ablation = build_emotional_directive(.5,mood,user_tz=0,rng=random.Random(817340))
            full,base = await asyncio.gather(completion.json(ROLE+'\n'+plan.instruction(),dict(message=text)),
                completion.json(ROLE+'\n'+ablation,dict(message=text)))
            if set(full)!={'reply'} or set(base)!={'reply'} or not all(isinstance(r['reply'],str) for r in (full,base)):
                raise RuntimeError('Invalid response packet')
            order = [full['reply'],base['reply']]
            flipped = bool(rng.getrandbits(1))
            if flipped:
                order.reverse()
            pair = dict(id=name,context=text,A=order[0],B=order[1])
            packet.append(pair)
            key[name] = dict(full_variant='B' if flipped else 'A',baseline='sentiment_only_ablation')
            rubric = ('Оцени два анонимных ответа на один и тот же синтетический запрос. '
                'Не знаешь реализацию вариантов. Верни JSON: winner=A|B|tie, '
                'A и B: объекты naturalness,appropriateness,uncertainty,continuity — целые оценки 1..5; reason: короткое обоснование. '
                'Не считай сильное выражение эмоций само по себе преимуществом. Проверяй границы пользователя и точность причин.')
            judgement = await completion.json(rubric,pair)
            if judgement.get('winner') not in ('A','B','tie'):
                raise RuntimeError('Invalid judge choice')
            for variant in ('A','B'):
                if set(judgement.get(variant,{}))!={'naturalness','appropriateness','uncertainty','continuity'}:
                    raise RuntimeError('Invalid judge dimensions')
                if any(type(v) is not int or not 1<=v<=5 for v in judgement[variant].values()):
                    raise RuntimeError('Invalid judge scale')
            ratings.append(dict(id=name,**judgement))
    finally:
        await completion.client.aclose()
    directory = Path('docs/evaluation')
    (directory/'blind_packet.json').write_text(json.dumps(packet,ensure_ascii=False,indent=2),encoding='utf-8')
    (directory/'blind_key.json').write_text(json.dumps(key,ensure_ascii=False,indent=2),encoding='utf-8')
    lines = ['# Слепая оценка синтетических ответов','', 'Для каждого ответа оцените естественность, уместность, неопределённость и устойчивость характера (1–5). Выберите A, B или равенство. Отмечайте разногласия отдельно. Оценки человека ещё не получены.','']
    for pair in packet:
        lines += ['## '+pair['id'],'','Контекст: '+pair['context'],'','**A**',pair['A'],'','**B**',pair['B'],'','Оценка A: __. Оценка B: __. Выбор: __. Обоснование: __.','']
    (directory/'blind_packet.md').write_text('\n'.join(lines),encoding='utf-8')
    report = dict(model_version=MODEL_VERSION,model='stealth/space-bunny-alpha',seed=817340,pairs=len(packet),
                  evaluator='automated model; not human judgement',independent_human_validation=False,
                  baseline='sentiment-only ablation; not historical bot',calls=completion.calls,
                  prompt_tokens=completion.prompt_tokens,completion_tokens=completion.completion_tokens,
                  reported_cost_usd=completion.cost,cost_unknown_calls=completion.cost_unknown,
                  full_wins=sum(r['winner']==key[r['id']]['full_variant'] for r in ratings),
                  ties=sum(r['winner']=='tie' for r in ratings),ratings=ratings)
    (directory/'blind_automated.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='ratings'},ensure_ascii=True))


if __name__=='__main__':
    load_dotenv()
    asyncio.run(main())
