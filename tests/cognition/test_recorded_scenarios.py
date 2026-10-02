"""Preserve historic appraisal evidence; assert the current expression contract."""
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
                    # Historical reports cover the four original affect axes.
                    # New resource/circadian observables have separate scenarios.
                    for key,value in results[case['id']]['affect'].items():
                        self.assertAlmostEqual(value,affect(state)[key],places=12)
                    # Expression policy intentionally evolved; old live reports
                    # remain evidence of the old version, never relabeled success.
                    plan=expression(state)
                    self.assertEqual(plan.behaviors,('ask_one_question',) if case['id']=='ambiguity' else ('listen',))
                    self.assertEqual(plan.mixed_affect,case['id'] in {'user_loss','service_failure','goal_conflict','heldout_grief'})
                    self.assertTrue(set(plan.cause_ids)<={ev.event_id})
                    if plan.mixed_affect: self.assertIsNone(plan.sticker_mood)

    def test_heldout_failures_remain_in_the_evaluation_record(self):
        report = json.loads((ROOT/'docs/evaluation/held_out_uncertainty_v2_live.json').read_text(encoding='utf-8'))
        self.assertEqual(report['total'],8)
        # Regression guard against silently relabeling failures as success.
        self.assertEqual(report['criteria_passed'],6)
        self.assertEqual({r['id'] for r in report['cases'] if not r['criteria_passed']},
                         {'heldout_ambiguous','heldout_boundary'})

    def test_frozen_final_rich_results_replay_and_previous_failures_are_preserved(self):
        from tools.evaluate_full_cognition import FINAL_HELD_OUT
        report = json.loads((ROOT/'docs/evaluation/full_final_held_out_frozen_final_v4_live.json').read_text(encoding='utf-8'))
        rows = {r['id']:r for r in report['results']}
        at = datetime(2026,9,30,12,tzinfo=timezone.utc)
        for name,text,_,_ in FINAL_HELD_OUT:
            with self.subTest(case=name):
                source = 'synthetic:'+name
                ev = CognitiveEvent(source,ContextKey('arti',1),EvidenceRef(source,source,Origin.USER,1),at,at,text,1)
                p = Perception.from_dict(json.loads((ROOT/'tests/fixtures/full_perceptions/frozen_final_v4/final_held_out'/f'{name}.json').read_text(encoding='utf-8')))
                state = appraise(initial_state(ev.context,at),ev,p)
                self.assertAlmostEqual(sum(e.intensity for e in state.episodes),rows[name]['impulse'],places=12)
                plan=expression(state)
                self.assertEqual(plan.behaviors,('listen',))
                self.assertEqual(plan.mixed_affect,name=='final_loss')
                self.assertTrue(set(plan.cause_ids)<={ev.event_id})
                if name=='final_loss':
                    self.assertIn('do not force one mood',plan.instruction())
                    self.assertIsNone(plan.sticker_mood)
        previous = json.loads((ROOT/'docs/evaluation/full_held_out_frozen_full_v3_live.json').read_text(encoding='utf-8'))
        self.assertEqual((previous['passed'],previous['total']),(14,16))
