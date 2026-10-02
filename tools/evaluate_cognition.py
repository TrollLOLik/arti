"""Run synthetic scenarios live, or replay saved perceptions without a provider call.

python -m tools.evaluate_cognition --live --split development
python -m tools.evaluate_cognition --split development  # recorded fixtures
No Telegram token is used; no production DB is initialized by this evaluator.
"""
import argparse
import asyncio
import hashlib
import json
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from cognition.affect import affect, appraise, expression, initial_state
from cognition.interpreter import InterpreterFailure, OpenRouterInterpreter, SYSTEM_PROMPT, environment_key
from cognition.serialization import dump
from cognition.types import CognitiveEvent, ContextKey, EvidenceRef, Origin, Perception

ROOT = Path(__file__).resolve().parents[1]
AT = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * p
    lower = int(position)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (position - lower)


def checks(case, state):
    emotions = {}
    for e in state.episodes:
        emotions[e.emotion] = emotions.get(e.emotion, 0) + e.intensity
    negative = sum(emotions.get(e, 0) for e in ('anger','sadness','fear','guilt','embarrassment','disappointment'))
    failures = []
    for metric, threshold in case.items():
        if metric.endswith(('_max', '_min')):
            name, bound = metric.rsplit('_', 1)
            value = negative if name == 'negative' else (sum(emotions.values()) if name == 'total' else emotions.get(name, 0))
            if (bound == 'max' and value > threshold) or (bound == 'min' and value < threshold):
                failures.append({'criterion':metric, 'actual':value, 'threshold':threshold})
    return emotions, failures


async def evaluate(args):
    corpus = json.loads((ROOT / 'tests/fixtures/cognitive_scenarios.json').read_text(encoding='utf-8'))
    if corpus.get('synthetic_only') is not True:
        raise ValueError('Live evaluation is restricted to the synthetic corpus')
    cases = corpus[args.split]
    if args.cases:
        ids = set(args.cases.split(','))
        cases = [case for case in cases if case['id'] in ids]
        if not cases or {case['id'] for case in cases} != ids:
            raise ValueError('Unknown synthetic scenario id')
    fixture_dir = ROOT / 'tests/fixtures/perceptions' / args.split / args.run_name
    fixture_dir.mkdir(parents=True, exist_ok=True)
    interpreter = None
    if args.live:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
        interpreter = OpenRouterInterpreter(environment_key())
    semaphore = asyncio.Semaphore(args.concurrency)

    async def one(case):
        ev = CognitiveEvent(case['id'], ContextKey('arti', 10), EvidenceRef(case['id'], case['id'], Origin.USER, 1), AT, AT, case['text'], 1)
        recorded = fixture_dir / (case['id'] + '.json')
        metrics = None
        async with semaphore:
            try:
                if interpreter:
                    result = await interpreter.interpret(ev)
                    p = result.perception
                    metrics = {k:v for k,v in asdict(result).items() if k != 'perception'}
                    recorded.write_text(dump(p) + '\n', encoding='utf-8')
                else:
                    p = Perception.from_dict(json.loads(recorded.read_text(encoding='utf-8')))
                start = time.perf_counter()
                state = appraise(initial_state(ev.context, AT), ev, p)
                plan = expression(state)
                numerical_ms = (time.perf_counter() - start) * 1000
                emotions, failures = checks(case, state)
                row = {'id':case['id'], 'schema_valid':True, 'criteria_passed':not failures,
                       'failures':failures, 'emotions':emotions, 'affect':affect(state),
                       'expression':asdict(plan), 'numerical_ms':numerical_ms, 'provider':metrics}
                print(json.dumps({'id':case['id'], 'passed':not failures, 'attempts':metrics['attempts'] if metrics else 0}, ensure_ascii=False), flush=True)
                return row
            except (InterpreterFailure, ValueError, FileNotFoundError) as exc:
                code = exc.code if isinstance(exc, InterpreterFailure) else type(exc).__name__
                print(json.dumps({'id':case['id'], 'failed':code}), flush=True)
                return {'id':case['id'], 'schema_valid':False, 'criteria_passed':False, 'error_code':code,
                        'provider':exc.metrics if isinstance(exc, InterpreterFailure) else None}
    try:
        rows = await asyncio.gather(*(one(case) for case in cases))
    finally:
        if interpreter:
            await interpreter.close()
    network = [r['provider']['latency_seconds'] for r in rows if r.get('provider')]
    numeric = [r['numerical_ms'] for r in rows if 'numerical_ms' in r]
    report = {
        'split':args.split, 'live':args.live, 'date':datetime.now(timezone.utc).isoformat(),
        'run_name':args.run_name,
        'model':'stealth/space-bunny-alpha' if args.live else 'recorded-perception',
        'provider_options':{'max_tokens':8192, 'reasoning_effort':'medium', 'timeout_seconds':90} if args.live else None,
        'prompt_sha256':hashlib.sha256(SYSTEM_PROMPT.encode('utf-8')).hexdigest(),
        'corpus_sha256':hashlib.sha256((ROOT / 'tests/fixtures/cognitive_scenarios.json').read_bytes()).hexdigest(),
        'total':len(rows), 'schema_valid':sum(r['schema_valid'] for r in rows),
        'criteria_passed':sum(r['criteria_passed'] for r in rows),
        'latency_seconds':{'p50':percentile(network,.5), 'p95':percentile(network,.95)},
        'numerical_ms':{'p50':percentile(numeric,.5), 'p95':percentile(numeric,.95)},
        'provider_attempts':sum(r['provider']['attempts'] for r in rows if r.get('provider')),
        'prompt_tokens':sum(r['provider']['prompt_tokens'] for r in rows if r.get('provider')),
        'completion_tokens':sum(r['provider']['completion_tokens'] for r in rows if r.get('provider')),
        'reported_cost_usd':sum(r['provider']['reported_cost_usd'] for r in rows if r.get('provider'))
                            if network and all(r['provider']['reported_cost_usd'] is not None for r in rows if r.get('provider')) else None,
        'limitations':'Synthetic engineering criteria; not a validated psychological scale or proof of human equivalence. Held-out results are not used to fit the current coefficients.',
        'cases':rows,
    }
    path = ROOT / 'docs/evaluation' / f'{args.split}_{args.run_name}_{"live" if args.live else "replay"}.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2) + '\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k != 'cases'},ensure_ascii=False),flush=True)
    return 0 if report['criteria_passed'] == report['total'] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--split', choices=('development','held_out'), default='development')
    parser.add_argument('--concurrency', type=int, choices=(1,2,3,4), default=3)
    parser.add_argument('--run-name', default='typed_8192')
    parser.add_argument('--cases', default='')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}',args.run_name):
        parser.error('run-name must be a short identifier')
    raise SystemExit(asyncio.run(evaluate(args)))


if __name__ == '__main__':
    main()
