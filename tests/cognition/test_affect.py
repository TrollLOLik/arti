import math
import random
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

from cognition.affect import advance, affect, appraise, expression, initial_state
from cognition.types import (Appraisal, CognitiveEvent, ContextKey, DEFAULT_GOALS, EvidenceRef,
                             Origin, Perception, PERCEPTION_VERSION, Temperament)

AT = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
CONTEXT = ContextKey('arti', 10)


def event(event_id='e1', context=CONTEXT, origin=Origin.USER, group=None, at=AT, text='Synthetic input'):
    return CognitiveEvent(event_id, context, EvidenceRef(event_id, group or event_id, origin, 1),
                          at, at, text, 1)


def appraisal(**changes):
    values = dict(goal_id='mutual_respect', probability=1., relevance=.9, congruence=-.8,
                  confidence=.9, novelty=.2, agency_self=0., agency_other=.9, intentionality=.9,
                  control=.6, outcome_probability=1., future_threat=0., loss=0.,
                  irreversibility=0., norm_violation=.9, social_exposure=0., evidence_ids=('e1',), target_id=1)
    values.update(changes)
    return Appraisal(**values)


def perception(ev, *appraisals):
    return Perception(ev.event_id, PERCEPTION_VERSION, tuple(appraisals))


class AffectTests(unittest.TestCase):
    def setUp(self):
        self.ev = event()
        self.state = initial_state(CONTEXT, AT)

    def test_duplicate_observation_is_exact_noop(self):
        p = perception(self.ev, appraisal())
        result = appraise(self.state, self.ev, p)
        self.assertEqual(appraise(result, self.ev, p), result)

    def test_same_independent_evidence_cannot_multiply_reaction(self):
        result = appraise(self.state, self.ev, perception(self.ev, appraisal()))
        e2 = event('e2', group='e1', at=AT + timedelta(minutes=2))
        p2 = perception(e2, appraisal(evidence_ids=('e2',)))
        self.assertEqual(result, appraise(result, e2, p2))

    def test_frequent_polling_does_not_change_decay(self):
        result = appraise(self.state, self.ev, perception(self.ev, appraisal()))
        direct = advance(result, AT + timedelta(hours=8))
        polled = result
        for n in range(1, 481):
            polled = advance(polled, AT + timedelta(minutes=n))
        for key, value in affect(direct).items():
            self.assertAlmostEqual(value, affect(polled)[key], places=12)
        self.assertAlmostEqual(direct.episodes[0].intensity, polled.episodes[0].intensity, places=12)

    def test_equal_time_constants_use_continuous_limit(self):
        traits = Temperament(mood_tau_seconds=1800.)
        state = initial_state(CONTEXT, AT, traits)
        state = appraise(state, self.ev, perception(self.ev, appraisal()), temperament=traits)
        direct = advance(state, AT + timedelta(hours=2), traits)
        split = advance(advance(state, AT + timedelta(hours=1), traits), AT + timedelta(hours=2), traits)
        self.assertAlmostEqual(direct.mood_valence_latent, split.mood_valence_latent, places=12)

    def test_pause_returns_to_temperament_not_rejection(self):
        state = appraise(self.state, self.ev, perception(self.ev, appraisal()))
        future = advance(state, AT + timedelta(days=30))
        values = affect(future)
        self.assertAlmostEqual(values['valence'], .08)
        self.assertAlmostEqual(values['mood_valence'], .08)
        self.assertEqual(state.applied_groups, future.applied_groups)

    def test_time_cannot_go_backward(self):
        with self.assertRaises(ValueError):
            advance(self.state, AT - timedelta(seconds=1))

    def test_different_context_rejected(self):
        other = event(context=ContextKey('arti', 11))
        with self.assertRaises(ValueError):
            appraise(self.state, other, perception(other, appraisal()))

    def test_rp_requires_scene(self):
        with self.assertRaises(ValueError):
            ContextKey('arti', 10, 'rp')

    def test_system_failure_does_not_blame_user(self):
        ev = event(origin=Origin.SYSTEM)
        state = appraise(self.state, ev, perception(ev, appraisal()))
        self.assertEqual(state.episodes, ())
        self.assertEqual(affect(state), affect(self.state))

    def test_self_output_does_not_create_evidence_of_user_emotion(self):
        for origin in (Origin.DELIVERED_ACTION, Origin.REPLAY):
            ev = event(origin=origin)
            result = appraise(self.state, ev, perception(ev, appraisal()))
            self.assertEqual(result.episodes, ())

    def test_recall_affect_budget(self):
        ev = event(origin=Origin.RECALL)
        result = appraise(self.state, ev, perception(ev, appraisal()))
        self.assertLessEqual(sum(e.intensity for e in result.episodes), .08000000001)

    def test_ambiguous_intent_less_anger_than_confirmed_harm(self):
        certain = appraise(self.state, self.ev, perception(self.ev, appraisal()))
        uncertain = appraise(self.state, self.ev, perception(self.ev,
                              appraisal(confidence=.45, intentionality=.25, norm_violation=.3)))
        anger = lambda state: sum(e.intensity for e in state.episodes if e.emotion == 'anger')
        self.assertLess(anger(uncertain), anger(certain) / 4)
        self.assertTrue(expression(uncertain).uncertain_intent)
        self.assertEqual(expression(uncertain).regulation, 'clarify')

    def test_mixed_emotions_retain_distinct_goals(self):
        state = appraise(self.state, self.ev, perception(self.ev,
                        appraisal(goal_id='help_user', congruence=.8, norm_violation=0.),
                        appraisal(goal_id='mutual_respect')))
        self.assertTrue(any(e.emotion == 'joy' and e.goal_id == 'help_user' for e in state.episodes))
        self.assertTrue(any(e.emotion == 'anger' and e.goal_id == 'mutual_respect' for e in state.episodes))
        self.assertTrue(all(e.cause_id == 'e1' for e in state.episodes))

    def test_suppression_changes_expression_only(self):
        state = appraise(self.state, self.ev, perception(self.ev, appraisal(congruence=.8)))
        before = asdict(state)
        plan = expression(state, task_serious=True)
        self.assertEqual(plan.playfulness, 0)
        self.assertIsNone(plan.sticker_mood)
        self.assertEqual(before, asdict(state))

    def test_no_extra_emotion_from_length_caps_or_cyrillic(self):
        short = self.ev
        long = replace(self.ev, text='НЕЙТРАЛЬНОЕ ТЕХНИЧЕСКОЕ ОПИСАНИЕ!!! ' * 1000)
        p = perception(self.ev, appraisal(congruence=0, novelty=0, norm_violation=0))
        self.assertEqual(appraise(self.state, short, p), appraise(self.state, long, p))

    def test_untrusted_dimensions_rejected(self):
        for value in (math.nan, math.inf, -math.inf, True, '0.5', 1.1, -.1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                appraisal(confidence=value)

    def test_unknown_evidence_rejected(self):
        with self.assertRaises(ValueError):
            appraise(self.state, self.ev, perception(self.ev, appraisal(evidence_ids=('invented',))))

    def test_alternatives_must_form_distribution(self):
        with self.assertRaises(ValueError):
            appraise(self.state, self.ev, perception(self.ev, appraisal(probability=.7), appraisal(probability=.7)))

    def test_nonfinite_json_is_rejected_by_schema(self):
        data = asdict(appraisal())
        data['evidence_ids'] = list(data['evidence_ids'])
        data['novelty'] = float('nan')
        with self.assertRaises(ValueError):
            Appraisal.from_dict(data)

    def test_randomized_time_semigroup(self):
        rng = random.Random(7319)
        state = appraise(self.state, self.ev, perception(self.ev, appraisal()))
        for _ in range(80):
            first, second = rng.uniform(0, 36000), rng.uniform(0, 36000)
            direct = advance(state, AT + timedelta(seconds=first + second))
            split = advance(advance(state, AT + timedelta(seconds=first)), AT + timedelta(seconds=first + second))
            self.assertAlmostEqual(direct.mood_valence_latent, split.mood_valence_latent, places=11)


if __name__ == '__main__':
    unittest.main()
