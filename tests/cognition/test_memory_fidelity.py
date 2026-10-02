"""Pure regression tests for source-aged fidelity and recollection timestamps."""
import copy
import json
import math
import unittest
from datetime import datetime, timedelta, timezone

from cognition.memory_dynamics import detail_state, reactivate, reconstruct


AT = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)


def detail(**changes):
    value = dict(text='A source-backed detail', kind='gist', strength=.45,
                 stability_days=30., fidelity=1., confidence=.7, vividness=.3,
                 last_recalled=None, recall_count=0)
    return {**value, **changes}


def trace(item=None, **changes):
    value = dict(details=[detail() if item is None else item],
                 observed_at=AT.isoformat(), source_id='source', modality='reported')
    return {**value, **changes}


class MemoryFidelityTests(unittest.TestCase):
    def test_empty_old_recollection_cannot_be_resurrected_by_reactivation(self):
        at = AT + timedelta(days=400)
        for replay in (False, True):
            with self.subTest(replay=replay):
                original = detail()
                self.assertEqual(reconstruct(trace(original), at, cue=1)['details'], [])
                changed = reactivate(original, AT, at, replay=replay)
                self.assertGreater(detail_state(changed, AT, at)['accessibility'],
                                   detail_state(original, AT, at)['accessibility'])
                self.assertEqual(reconstruct(trace(changed), at, cue=1)['details'], [])
                self.assertEqual(reconstruct(trace(changed), at + timedelta(days=1), cue=1)['details'], [])

    def test_spaced_recall_improves_accessibility_without_quality_gain(self):
        original = detail()
        before_payload = copy.deepcopy(original)
        at = AT + timedelta(days=20)
        before = detail_state(original, AT, at)
        changed = reactivate(original, AT, at)
        after = detail_state(changed, AT, at)
        self.assertGreater(after['accessibility'], before['accessibility'])
        self.assertGreater(changed['stability_days'], original['stability_days'])
        for dimension in ('fidelity', 'vividness', 'confidence'):
            self.assertEqual(after[dimension], before[dimension])
        self.assertEqual(original, before_payload)
        self.assertEqual(changed['last_recalled'], at.isoformat())
        self.assertEqual(changed['recall_count'], 1)

    def test_repeated_recall_and_replay_preserve_source_quality_curve(self):
        original = detail()
        for replay in (False, True):
            with self.subTest(replay=replay):
                changed = original
                for day in (1, 3, 7, 20, 90, 400):
                    at = AT + timedelta(days=day)
                    changed = reactivate(changed, AT, at, replay=replay)
                    # Persisted JSON must preserve the fixed quality baseline.
                    changed = json.loads(json.dumps(changed))
                    expected = detail_state(original, AT, at)
                    actual = detail_state(changed, AT, at)
                    self.assertEqual(actual['fidelity'], expected['fidelity'])
                    self.assertEqual(actual['vividness'], expected['vividness'])
                    self.assertEqual(actual['confidence'], expected['confidence'])
                    self.assertEqual(changed['fidelity_stability_days'], 30.)
                self.assertEqual(changed['recall_count'], 6)

    def test_legacy_recall_timestamp_is_not_a_fidelity_timestamp(self):
        at = AT + timedelta(days=400)
        original = detail()
        original.update(last_recalled=(at - timedelta(days=1)).isoformat(), recall_count=5)
        state = detail_state(original, AT, at, cue=1)
        self.assertGreater(state['accessibility'], .5)
        self.assertAlmostEqual(state['fidelity'], math.exp(-400 / (30 * 12)))
        self.assertAlmostEqual(state['vividness'], .3 * math.exp(-400 / (30 * 3)))
        self.assertEqual(reconstruct(trace(original), at, cue=1)['details'], [])
        changed = reactivate(original, AT, at)
        self.assertEqual(detail_state(changed, AT, at)['fidelity'], state['fidelity'])
        self.assertEqual(detail_state(changed, AT, at)['vividness'], state['vividness'])

    def test_cues_and_competition_do_not_change_source_quality(self):
        original = detail()
        at = AT + timedelta(days=20)
        neutral = detail_state(original, AT, at)
        cued = detail_state(original, AT, at, competition=10, cue=1)
        for dimension in ('fidelity', 'vividness', 'confidence'):
            self.assertEqual(cued[dimension], neutral[dimension])

    def test_stored_quality_baseline_survives_larger_rehearsal_stability(self):
        original = detail(fidelity=.8, vividness=.2, fidelity_stability_days=8.,
                          stability_days=300., recall_count=10,
                          last_recalled=(AT + timedelta(days=90)).isoformat())
        at = AT + timedelta(days=100)
        before = detail_state(original, AT, at)
        self.assertAlmostEqual(before['fidelity'], .8 * math.exp(-100 / (8 * 12)))
        self.assertAlmostEqual(before['vividness'], .2 * math.exp(-100 / (8 * 3)))
        changed = reactivate(original, AT, at, replay=True)
        self.assertEqual(changed['fidelity_stability_days'], 8.)
        self.assertEqual(detail_state(changed, AT, at)['fidelity'], before['fidelity'])
        self.assertEqual(detail_state(changed, AT, at)['vividness'], before['vividness'])

    def test_replay_has_bounded_accessibility_benefit_without_new_evidence(self):
        original = detail()
        at = AT + timedelta(days=30)
        replayed = reactivate(original, AT, at, replay=True)
        recalled = reactivate(original, AT, at)
        self.assertGreater(replayed['strength'], original['strength'])
        self.assertLess(replayed['strength'], recalled['strength'])
        self.assertGreater(replayed['stability_days'], original['stability_days'])
        self.assertLess(replayed['stability_days'], recalled['stability_days'])
        for changed in (replayed, recalled):
            self.assertEqual(changed['confidence'], original['confidence'])
            self.assertEqual(changed['fidelity'], original['fidelity'])
            self.assertEqual(changed['vividness'], original['vividness'])

    def test_explicit_archive_read_does_not_rejuvenate_ordinary_recall(self):
        original = trace()
        before = copy.deepcopy(original)
        at = AT + timedelta(days=400)
        archive = reconstruct(original, at, archive=True)
        self.assertEqual(archive['details'][0]['text'], original['details'][0]['text'])
        self.assertTrue(archive['details'][0]['verbatim_verified'])
        self.assertEqual(archive['time_precision'], 'source_record')
        self.assertEqual(original, before)
        self.assertEqual(reconstruct(original, at, cue=1)['details'], [])


class RecollectionTimeTests(unittest.TestCase):
    def test_occurrence_and_observation_remain_distinct(self):
        occurred = AT - timedelta(days=400)
        original = trace(occurred_at=occurred.isoformat())
        recalled = reconstruct(original, AT + timedelta(days=1), cue=1)
        self.assertEqual(recalled['observed_at'], AT.isoformat())
        self.assertEqual(recalled['occurred_at'], occurred.isoformat())
        self.assertEqual(recalled['time_basis'], 'occurred_at')
        self.assertEqual(recalled['time_precision'], 'year')
        # This old event was only just observed, so its encoded detail is fresh.
        self.assertGreater(recalled['details'][0]['fidelity'], .99)

    def test_legacy_unknown_occurrence_is_not_replaced_by_observation(self):
        for original in (trace(), trace(occurred_at=None)):
            with self.subTest(original=original):
                recalled = reconstruct(original, AT + timedelta(days=40), cue=1)
                self.assertEqual(recalled['observed_at'], AT.isoformat())
                self.assertIsNone(recalled['occurred_at'])
                self.assertEqual(recalled['time_basis'], 'observed_at')
                self.assertEqual(recalled['time_precision'], 'month')

    def test_temporal_precision_boundaries_and_archive_identity(self):
        original = trace(occurred_at=AT.isoformat())
        for days, precision in ((0, 'day'), (29, 'day'), (30, 'month'),
                                (364, 'month'), (365, 'year')):
            with self.subTest(days=days):
                at = AT + timedelta(days=days)
                recalled = reconstruct(original, at, cue=1)
                self.assertEqual(recalled['time_precision'], precision)
                archive = reconstruct(original, at, archive=True)
                self.assertEqual(archive['time_precision'], 'source_record')
                self.assertEqual(archive['observed_at'], AT.isoformat())
                self.assertEqual(archive['occurred_at'], AT.isoformat())


if __name__ == '__main__':
    unittest.main()
