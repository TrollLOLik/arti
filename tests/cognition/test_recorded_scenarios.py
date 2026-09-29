"""Replay observed outputs exactly, including the two failed held-out criteria."""
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cognition.affect import affect, appraise, expression, initial_state
from cognition.types import CognitiveEvent, ContextKey, EvidenceRef, Origin, Perception

ROOT = Path(__file__).resolve().parents[2]


class RecordedScenarioTests(unittest.TestCase):
    def test_live_observations_replay_without_provider(self):
        at = datetime(2026,9,30,12,tzinfo=timezone.utc)
        corpus = json.loads((ROOT/'tests/fixtures/cognitive_scenarios.json').read_text(encoding='utf-8'))
        for split in ('development','held_out'):
            path = ROOT/'docs/evaluation'/f'{split}_uncertainty_v2_live.json'
            report = json.loads(path.read_text(encoding='utf-8'))
            results = {r['id']:r for r in report['cases']}
            for case in corpus[split]:
                with self.subTest(split=split,case=case['id']):
                    p = Perception.from_dict(json.loads((ROOT/'tests/fixtures/perceptions'/split/'uncertainty_v2'/f"{case['id']}.json").read_text(encoding='utf-8')))
                    ev = CognitiveEvent(case['id'],ContextKey('arti',10),EvidenceRef(case['id'],case['id'],Origin.USER,1),at,at,case['text'],1)
                    state = appraise(initial_state(ev.context,at),ev,p)
                    for key,value in affect(state).items():
                        self.assertAlmostEqual(value,results[case['id']]['affect'][key],places=12)
                    self.assertEqual(expression(state).tone,results[case['id']]['expression']['tone'])

    def test_heldout_failures_remain_in_the_evaluation_record(self):
        report = json.loads((ROOT/'docs/evaluation/held_out_uncertainty_v2_live.json').read_text(encoding='utf-8'))
        self.assertEqual(report['total'],8)
        # Regression guard against silently relabeling failures as success.
        self.assertEqual(report['criteria_passed'],6)
        self.assertEqual({r['id'] for r in report['cases'] if not r['criteria_passed']},
                         {'heldout_ambiguous','heldout_boundary'})
