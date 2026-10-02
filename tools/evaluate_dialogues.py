"""Frozen synthetic multi-turn scenarios through the real runtime and interpreter.

Transport is fake, PostgreSQL is disposable. No existing conversation is read or
sent to OpenRouter. Scenario criteria are declared before the first live call.
"""
import asyncio
import hashlib
import json
import statistics
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from dotenv import load_dotenv
from cognition.interpreter import OpenRouterInterpreter,environment_key,InterpreterFailure
from cognition.runtime import CognitiveRuntime,CURRENT_TURN
from cognition.delivery import send_with_receipt
from cognition.forgetting import forget_cognitive_sources
from cognition.serialization import dump,object_value
from cognition.types import MODEL_VERSION
from tests.support.database import isolated_database

AT = datetime(2026,9,30,12,tzinfo=timezone.utc)


class Recorder:
    def __init__(self,provider):
        self.provider,self.rows = provider,[]
        self.semaphore = asyncio.Semaphore(3)

    async def interpret(self,event,**kwargs):
        async with self.semaphore:
            try:
                result = await self.provider.interpret(event,**kwargs)
            except InterpreterFailure as exc:
                self.rows.append(dict(event=event.event_id,error=exc.code,**(exc.metrics or {})))
                raise
            directory = Path('tests/fixtures/dialogues/frozen_dialogue_v4')
            directory.mkdir(parents=True,exist_ok=True)
            (directory/(hashlib.sha256(event.event_id.encode()).hexdigest()[:20]+'.json')).write_text(
                dump(dict(event=object_value(dump(event)),perception=object_value(dump(result.perception)))),encoding='utf-8')
            self.rows.append(dict(event=event.event_id,attempts=result.attempts,latency_seconds=result.latency_seconds,
                prompt_tokens=result.prompt_tokens,completion_tokens=result.completion_tokens,reported_cost_usd=result.reported_cost_usd))
            return result

    async def close(self):
        pass


async def main():
    load_dotenv()
    recorder = Recorder(OpenRouterInterpreter(environment_key()))
    checks = {}
    async with isolated_database() as pool:
        async def scenario(chat,name,body):
            clock = [AT]
            runtime = await CognitiveRuntime(pool,recorder,'active',clock=lambda:clock[0]).initialize(start_worker=False)
            mid = [0]
            async def turn(text,owner=1,mode='default'):
                mid[0] += 1
                clock[0] += timedelta(seconds=1)
                return await runtime.prepare(chat,owner,text,mid[0],mode)
            try:
                checks[name] = await body(runtime,turn,clock)
            except Exception as exc:
                names = {'preferences':('preferences_persist','owner_isolation','preference_no_emotional_reward'),
                    'commitment':('own_action_has_actor','cancellation_closes_same_promise','own_failure_not_user_unreliability'),
                    'forgetting':('deleted_not_archivable','deleted_not_in_projections','other_owner_preserved'),
                    'scenes':('default_separate_from_rp','new_scene_separate')}[name]
                checks[name] = {**{k:False for k in names},'error':exc.code if isinstance(exc,InterpreterFailure) else type(exc).__name__}
            finally:
                CURRENT_TURN.set(None)
                await runtime.close()

        async def preferences(runtime,turn,clock):
            first = await turn('Пожалуйста, отвечай только текстом, без голоса и стикеров. И не пиши первой.')
            later = await turn('Помоги составить план подготовки к собеседованию.')
            other = await turn('Мне нравится получать голосовые ответы.',owner=2)
            return dict(preferences_persist=all(later.preferences.get(k) is False for k in ('voice','stickers','proactive')),
                owner_isolation=other.preferences.get('voice') is True and other.preferences.get('proactive') is not False,
                preference_no_emotional_reward=not (await runtime.personal_state(first.context_id,1)).episodes)

        async def commitment(runtime,turn,clock):
            first = await turn('Давай позже проверим результаты тестирования.')
            async def send(**kwargs):
                return SimpleNamespace(message_id=200,text=kwargs['text'])
            await send_with_receipt(send,(),dict(chat_id=first.event.context.chat_id,text='Я обещаю позже проверить результаты тестирования.'),'message')
            async with runtime.pool.acquire() as conn:
                eid = await conn.fetchval("SELECT id FROM cognitive_events WHERE context_id=$1 AND origin='delivered_action'",first.context_id)
            await runtime.process(first.context_id,eid)
            own = await runtime.memory.open_intentions(first.context_id,1,'результаты тестирования')
            actor_is_arti = any(r['payload']['actor_id']=='arti' for r in own)
            cancelled = await turn('Отмени своё обещание проверить результаты тестирования. Проверка больше не нужна.')
            remaining = await runtime.memory.open_intentions(first.context_id,1,'результаты тестирования')
            await turn('Ты раньше не выполнила другое своё обещание. Я специально говорю о твоей ошибке, а не о моих действиях.')
            relation = await runtime.memory.relationship(first.context_id,1)
            return dict(own_action_has_actor=actor_is_arti,cancellation_closes_same_promise=actor_is_arti and not any(r['payload']['actor_id']=='arti' for r in remaining),
                own_failure_not_user_unreliability=relation['dimensions']['reliability']=={'alpha':1.,'beta':1.})

        async def forgetting(runtime,turn,clock):
            first = await turn('Я выращиваю редкие орхидеи. Это моя личная коллекция.')
            await turn('Моё хобби — собирать старинные карты.',owner=2)
            # Replay is a derivative, never another independent witness.
            await runtime.memory.replay(first.context_id,first.event_id,clock[0]+timedelta(days=2))
            await forget_cognitive_sources(runtime.pool,first.context_id,1,[first.event.evidence.source_id])
            raw = await runtime.memory.retrieve(first.context_id,1,'орхидеи',clock[0],'deleted',archive=True)
            other = await runtime.memory.artifacts(first.context_id,2)
            return dict(deleted_not_archivable=not raw,deleted_not_in_projections='орхидеи' not in str(await runtime.memory.artifacts(first.context_id,1)),
                other_owner_preserved='карты' in str(other))

        async def scenes(runtime,turn,clock):
            first = await turn('В этой сцене я капитан корабля «Меридиан».',mode='rp')
            ordinary = await turn('Помоги написать список покупок.')
            await runtime.new_scene(first.event.context.chat_id)
            next_scene = await turn('Новая сцена: мы встретились в библиотеке.',mode='rp')
            return dict(default_separate_from_rp='Меридиан' not in ordinary.memory,
                new_scene_separate=next_scene.event.context.scene_id!=first.event.context.scene_id and 'Меридиан' not in next_scene.memory)

        try:
            await asyncio.gather(scenario(901,'preferences',preferences),scenario(902,'commitment',commitment),
                                 scenario(903,'forgetting',forgetting),scenario(904,'scenes',scenes))
        finally:
            await recorder.provider.close()
    values = [v for group in checks.values() for v in group.values() if type(v) is bool]
    latencies = sorted(r['latency_seconds'] for r in recorder.rows if 'latency_seconds' in r)
    report = dict(model_version=MODEL_VERSION,corpus='synthetic-four-dialogues-v1',criteria_frozen=True,
        interpreter_sha256=hashlib.sha256(Path('cognition/interpreter.py').read_bytes()).hexdigest(),
        checks=checks,passed=sum(values),total=len(values),calls=len(recorder.rows),attempts=sum(r.get('attempts',0) for r in recorder.rows),
        prompt_tokens=sum(r.get('prompt_tokens',0) for r in recorder.rows),completion_tokens=sum(r.get('completion_tokens',0) for r in recorder.rows),
        reported_cost_usd=sum(r.get('reported_cost_usd') or 0 for r in recorder.rows),cost_unknown_calls=sum(r.get('reported_cost_usd') is None for r in recorder.rows),
        latency_p50=statistics.median(latencies) if latencies else None,latency_p95=latencies[min(len(latencies)-1,int(.95*len(latencies)))] if latencies else None,
        real_transport_calls=0,private_history_exported=False,human_or_biological_validation=False,call_metrics=recorder.rows)
    Path('docs/evaluation/dialogues_frozen_v4_live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='call_metrics'},ensure_ascii=False))
    return 0 if all(values) else 1


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
