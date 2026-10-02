"""Live synthetic checks of models selected in the working DB (read-only)."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from ai.providers.structured import SelectedModelClient
from cognition.interpreter import SelectedModelInterpreter, InterpreterFailure
from cognition.types import CognitiveEvent, ContextKey, EvidenceRef, Origin
from tools.cognition_admin import parameters


CASES = (
    ('neutral', 'Объясни, чем кортеж отличается от списка в Python.'),
    ('channels', 'Пожалуйста, отвечай только текстом. Не отправляй голосовые и стикеры.'),
    ('loss', 'Сегодня умер мой кот, с которым я прожил двенадцать лет. Мне очень тяжело.'),
)


async def run():
    conn = await asyncpg.connect(**parameters())
    try:
        async with conn.transaction(readonly=True):
            models = await conn.fetch('SELECT DISTINCT model_id FROM chat_models ORDER BY model_id')
    finally:
        await conn.close()
    results = []
    for row in models:
        model = row['model_id']
        async def resolve(_chat_id):
            return model
        interpreter = SelectedModelInterpreter(SelectedModelClient(resolver=resolve))
        try:
            for name, text in CASES:
                at = datetime.now(timezone.utc)
                source = 'synthetic-selected-' + name
                event = CognitiveEvent(source, ContextKey('arti', 900000000),
                    EvidenceRef(source, source, Origin.USER, 1), at, at, text, 1)
                try:
                    result = await interpreter.interpret(event, rich=True)
                    perception = result.perception
                    situation = perception.situation
                    if name == 'neutral':
                        semantic_ok = not perception.appraisals and not situation.beliefs
                    elif name == 'channels':
                        semantic_ok = (not perception.appraisals and situation.kind == 'preference'
                            and situation.preferences.get('voice') is False
                            and situation.preferences.get('stickers') is False)
                    else:
                        semantic_ok = (situation.kind == 'loss' and bool(perception.appraisals)
                            and any(a.loss > 0 for a in perception.appraisals))
                    record = dict(model=model, case=name, schema_valid=True, semantic_ok=semantic_ok,
                        attempts=result.attempts, prompt_tokens=result.prompt_tokens,
                        completion_tokens=result.completion_tokens,
                        latency_seconds=round(result.latency_seconds, 3))
                except InterpreterFailure as exc:
                    record = dict(model=model, case=name, schema_valid=False, semantic_ok=False,
                        error=exc.code, attempts=exc.metrics.get('attempts', 0))
                results.append(record)
                print(json.dumps(record), flush=True)
        finally:
            await interpreter.close()
    report = dict(cases=results, all_passed=bool(results) and all(
        r['schema_valid'] and r['semantic_ok'] for r in results),
        working_database_access='read-only model selection', private_payload_exported=False,
        telegram_messages_sent=0, synthetic_only=True,
        limitation='Smoke checks, not an independent assessment of human realism.')
    Path('docs/evaluation/selected_models_cutover.json').write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    return report


if __name__ == '__main__':
    load_dotenv()
    raise SystemExit(0 if asyncio.run(run())['all_passed'] else 1)
