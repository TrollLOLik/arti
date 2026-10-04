"""Offline recorded-output regression for source-grounded group understanding.

The fixture is a hand-authored synthetic transcript and semantic-output corpus.
No classifier runs here: feeding authored outputs through the real adapter tests
schema, attribution, uncertainty, source-lineage and packet safeguards. A passing
report is NOT evidence of an LLM's semantic comprehension or live-model quality.
There is deliberately no live/provider option, secret loading or network path.
"""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import httpx

from ai.group_understanding import MAX_COMPLETION_TOKENS, SelectedModelGroupUnderstanding
from cognition.group_understanding import MESSAGE_FIELDS, normalize_messages


DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / 'tests/fixtures/group_conversation_understanding.json'
LIMITATION = ('Hand-authored synthetic semantic outputs replayed through deterministic '
              'validation; live-model semantic accuracy, real conversation quality, '
              'persistence and runtime delivery are not evaluated.')


class RecordedTransport:
    """A deterministic test double. It never derives labels from transcript text."""

    def __init__(self, output, *, status_code=200, finish_reason='stop', content=None):
        self.output = deepcopy(output)
        self.status_code = status_code
        self.finish_reason = finish_reason
        self.content = content
        self.selections = []
        self.requests = []
        self.closed = False

    async def model_for(self, chat_id):
        self.selections.append(chat_id)
        return 'recorded-synthetic-model'

    async def complete(self, model, messages, tokens, temperature=0):
        self.requests.append({'model': model, 'messages': deepcopy(messages),
                              'tokens': tokens, 'temperature': temperature})
        content = self.content if self.content is not None else json.dumps(self.output, ensure_ascii=False)
        return httpx.Response(self.status_code, json={
            'choices': [{'message': {'content': content}, 'finish_reason': self.finish_reason}],
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'cost': 0},
        })

    async def close(self):
        self.closed = True


def load_fixture(path=DEFAULT_FIXTURE):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('output_provenance') != 'hand_authored_synthetic_recordings':
        raise ValueError('fixture_must_disclose_recorded_output_provenance')
    ids = [case['id'] for case in data['episodes']]
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError('invalid_episode_ids')
    attacks = data.get('adversarial_cases', [])
    if len({attack['id'] for attack in attacks}) != len(attacks):
        raise ValueError('duplicate_adversarial_ids')
    if any(attack['episode_id'] not in ids or attack['expected'] != 'rejected' for attack in attacks):
        raise ValueError('invalid_adversarial_case')
    return data


def normalized_observation(result):
    """Compare concrete claims, not arbitrary wording of a topic label/summary.

    Arrays remain ordered and complete: no extra/missing interpretation can be
    hidden by a subset assertion. All actor IDs and transition attributions are
    checked exactly. The parser itself validates every exact evidence span.
    """
    return {
        'threads': [{key: row[key] for key in ('thread_id', 'source_ids')} for row in result['threads']],
        'links': [{key: row[key] for key in (
            'source_id', 'target_source_id', 'thread_id', 'relation', 'addressee_ids', 'confidence', 'source_ids'
        )} for row in result['links']],
        'items': [{**{key: row[key] for key in (
            'item_id', 'kind', 'thread_id', 'origin_source_id', 'actor_id', 'attribution',
            'initial_status', 'status', 'status_scope', 'confidence', 'source_ids'
        )}, 'updates': [{key: update[key] for key in (
            'source_id', 'status', 'actor_id', 'attribution', 'confidence'
        )} for update in row['updates']]} for row in result['items']],
    }


def differences(expected, actual, path='$'):
    """Return precise structural mismatches without reporting provider bodies."""
    if type(expected) is not type(actual):
        return [dict(path=path, expected=expected, actual=actual)]
    if isinstance(expected, dict):
        result = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                result.append(dict(path=f'{path}.{key}', expected=expected.get(key),
                                   actual=actual.get(key), missing_from='expected' if key not in expected else 'actual'))
            else:
                result.extend(differences(expected[key], actual[key], f'{path}.{key}'))
        return result
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [dict(path=path + '.length', expected=len(expected), actual=len(actual))]
        return [change for i, (left, right) in enumerate(zip(expected, actual))
                for change in differences(left, right, f'{path}[{i}]')]
    return [] if expected == actual else [dict(path=path, expected=expected, actual=actual)]


def packet_checks(transport, messages):
    if len(transport.requests) != 1 or transport.selections != [-900001]:
        return False
    request = transport.requests[0]
    packet = request['messages']
    if (request['model'] != 'recorded-synthetic-model' or request['tokens'] != MAX_COMPLETION_TOKENS
            or request['temperature'] != 0 or len(packet) != 2
            or [entry.get('role') for entry in packet] != ['system', 'user']):
        return False
    body = json.loads(packet[1]['content'])
    return (set(body) == {'messages'} and body['messages'] == normalize_messages(messages)
            and all(set(message) == MESSAGE_FIELDS for message in body['messages']))


async def evaluate_case(case):
    transport = RecordedTransport(case['recorded_output'])
    analyzer = SelectedModelGroupUnderstanding(transport=transport)
    try:
        result = await analyzer.analyze(deepcopy(case['messages']), chat_id=-900001)
        mismatches = differences(case['expected'], normalized_observation(result))
        bounded_packet = packet_checks(transport, case['messages'])
        if not bounded_packet:
            mismatches.append(dict(path='$.provider_packet', expected='one raw-only selected-model call', actual='mismatch'))
        return dict(id=case['id'], name=case['name'], language=case['language'], tags=case['tags'],
                    passed=not mismatches, mismatches=mismatches, packet_passed=bounded_packet,
                    mocked_calls=analyzer.calls)
    except ValueError as exc:
        return dict(id=case['id'], name=case['name'], language=case['language'], tags=case['tags'],
                    passed=False, error=str(exc), packet_passed=False, mocked_calls=analyzer.calls)
    finally:
        await analyzer.close()


def inject_malformed_output(case, attack):
    """Apply an explicit fixture mutation, never a transcript-derived label."""
    value = deepcopy(case['recorded_output'])
    target = value
    for key in attack['path'][:-1]:
        target = target[key]
    key = attack['path'][-1]
    if attack.get('operation') == 'reverse_updates':
        target[key] = list(reversed(target[key]))
    else:
        target[key] = deepcopy(attack['value'])
    return value


async def evaluate_adversarial_case(case, attack):
    transport = RecordedTransport(inject_malformed_output(case, attack))
    analyzer = SelectedModelGroupUnderstanding(transport=transport)
    try:
        await analyzer.analyze(deepcopy(case['messages']), chat_id=-900001)
        rejected = False
    except ValueError as exc:
        # Specific internals/provider content must not escape the adapter.
        rejected = str(exc) == 'group_understanding_failed'
    finally:
        await analyzer.close()
    return dict(id=attack['id'], episode_id=case['id'], expected='rejected',
                passed=rejected and packet_checks(transport, case['messages']),
                rejected=rejected, mocked_calls=analyzer.calls)


async def evaluate_fixture(fixture=DEFAULT_FIXTURE, output=None):
    corpus = load_fixture(fixture)
    records = [await evaluate_case(case) for case in corpus['episodes']]
    episodes_by_id = {case['id']: case for case in corpus['episodes']}
    negative_records = [await evaluate_adversarial_case(episodes_by_id[attack['episode_id']], attack)
                        for attack in corpus.get('adversarial_cases', [])]
    tag_totals = {}
    for record in records:
        for tag in record['tags']:
            metric = tag_totals.setdefault(tag, {'passed': 0, 'total': 0})
            metric['total'] += 1
            metric['passed'] += int(record['passed'])
    report = dict(
        schema_version=corpus['schema_version'], fixture_sha256=hashlib.sha256(Path(fixture).read_bytes()).hexdigest(),
        evaluation_layer='offline_recorded_output_contract', output_provenance=corpus['output_provenance'],
        limitation=LIMITATION, semantic_model_quality_assessed=False, human_ratings=False,
        live_provider_calls=0, telegram_calls=0, production_data_used=False,
        episodes=len(records), passed=sum(record['passed'] for record in records),
        adversarial_cases=len(negative_records), adversarial_passed=sum(row['passed'] for row in negative_records),
        mocked_calls=sum(record['mocked_calls'] for record in [*records, *negative_records]),
        tag_totals=tag_totals, records=records, adversarial_records=negative_records,
    )
    if output is not None:
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument('--output', type=Path, help='Optional local JSON report; no file is written by default.')
    args = parser.parse_args(argv)
    report = asyncio.run(evaluate_fixture(args.fixture, args.output))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (report['passed'] == report['episodes']
                 and report['adversarial_passed'] == report['adversarial_cases']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
