"""Explicit live probe using synthetic requests and a currently selected model."""
import asyncio
import json
import os
import time
from pathlib import Path
import asyncpg
from dotenv import load_dotenv
from ai.intents import resolve_intent
from ai.providers.structured import SelectedModelClient

CASES = [
    ('Хочу это в наглядном виде','artifact',True),
    ('Можешь разложить эти документы по отличиям в удобном виде?','artifact',True),
    ('Узнай, почем сейчас билет до Казани','search',False),
    ('Где здесь можно перекусить?','maps',False),
    ('Помоги мне успокоиться','chat',False),
    ('Хочу просто обсудить, как устроены инфографики','chat',False),
    ('Я запутался в этих договорах. Можно понятно показать, чем они различаются?','artifact',True),
]

async def main():
    load_dotenv()
    # Production initializes the SDK before handling user requests.
    import config
    conn=await asyncpg.connect(host=os.getenv('DB_HOST','localhost'),port=int(os.getenv('DB_PORT','5432')),
        database=os.getenv('DB_NAME','arti_bot'),user=os.getenv('DB_USER','postgres'),password=os.getenv('DB_PASSWORD',''))
    try:
        async with conn.transaction(readonly=True):
            model=await conn.fetchval("SELECT model_id FROM chat_models WHERE chat_id>0 ORDER BY updated_at DESC LIMIT 1")
        if not model:
            from config import DEFAULT_MODEL
            model=DEFAULT_MODEL
    finally:
        await conn.close()
    async def resolver(_): return model
    class MeasuredClient(SelectedModelClient):
        state='not_called'
        async def complete(self,*args,**kwargs):
            self.state='incomplete'
            response=await super().complete(*args,**kwargs)
            self.state='http_'+str(response.status_code)
            return response
    client=MeasuredClient(resolver=resolver,timeout=4,fast=True)
    from cognition.semantic import LocalEncoder
    from ai.intent_semantics import EXAMPLES
    encoder=LocalEncoder()
    encoder.intent_references=await encoder.encode(EXAMPLES,timeout=30)
    results=[]
    try:
        for i,(prompt,expected,materials) in enumerate(CASES,1):
            started=time.perf_counter()
            client.state='not_called'
            output=await resolve_intent(prompt,0,has_materials=materials,allow_work=True,client=client,local_encoder=encoder)
            route='maps' if output['maps'] else 'search' if output['web_search'] else output['work'] or 'chat'
            results.append(dict(case=i,expected=expected,actual=route,passed=route==expected,
                                provider=client.state,duration_ms=round((time.perf_counter()-started)*1000)))
    finally:
        await client.close()
        await encoder.close()
    report=dict(model=model,synthetic_only=True,suite='development_smoke_not_holdout',
        provider_calls=sum(r['provider']!='not_called' for r in results),working_database_mutated=False,
        passed=sum(r['passed'] for r in results),total=len(results),cases=results)
    Path('docs/evaluation/request_routing_live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report))

if __name__=='__main__':
    asyncio.run(main())
