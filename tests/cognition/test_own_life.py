"""H4a «Своя жизнь», ядро чувств: собственные цели Арти и полная палитра эмоций
(E4, E3), обида и прощение (E6), версионированная динамика и откат.

Synthetic data only: recorded interpreter, disposable PostgreSQL, no providers.
"""
import math
import os
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch

from cognition.affect import EMOTIONS, OWN_GOAL_BUDGET, advance, affect, appraise, expression, initial_state
from cognition.relationships import (RUPTURE_CLOSED, RUPTURE_TAU_SECONDS, initial_relationship, relationship_transition,
                                     relationship_view, rupture_severity)
from cognition.serialization import dump, load_state, object_value
from cognition.situations import Situation, calibrated_appraisals
from cognition.types import (AffectiveResidue, Appraisal, DEFAULT_GOALS, Origin, EMOTION_VOICE, ExpressionPlan, OWN_GOALS, RUPTURE_VISIBLE,
                             TEMPERAMENT_V1, TEMPERAMENT_VERSION, Temperament)
from tests.cognition.test_affect import AT, CONTEXT, appraisal, event, perception
from tests.cognition.test_full_model import situation

NEUTRAL = Situation(kind='neutral', modality='interaction')


def felt(app, sit=None, temperament=None, ev=None):
    ev = ev or event()
    p = replace(perception(ev, app), situation=sit)
    kwargs = {'temperament': temperament} if temperament else {}
    state = appraise(initial_state(CONTEXT, AT, *([temperament] if temperament else [])), ev, p, **kwargs)
    return {e.emotion: e.intensity for e in state.episodes}, state


def mood_after(state, hours, temperament=None):
    args = [temperament] if temperament else []
    return affect(advance(state, AT + timedelta(hours=hours), *args), *args)['mood_valence']


def plan(**kw):
    base = dict(regulation='acknowledge', tone='calm', warmth=.6, directness=.5, playfulness=.3,
                disclosure=.2, sticker_mood=None, tts_style='neutral', cause_ids=(), uncertain_intent=False)
    base.update(kw)
    return ExpressionPlan(**base)


CURIOUS = dict(goal_id='curiosity', congruence=.6, novelty=.8, agency_other=.3, intentionality=.2,
               norm_violation=0., relevance=.8, outcome_probability=.8)


class OwnGoalsTests(unittest.TestCase):
    def test_the_character_has_goals_of_her_own(self):
        ids = {g.id for g in DEFAULT_GOALS}
        self.assertLessEqual(OWN_GOALS, ids)
        self.assertEqual(OWN_GOALS, {'curiosity', 'aesthetics', 'being_understood', 'autonomy'})
        from cognition.interpreter import SYSTEM_PROMPT
        self.assertIn('humor [0,1]', SYSTEM_PROMPT)
        self.assertIn("character's own", SYSTEM_PROMPT)
        self.assertIn('calling her a bot does not', SYSTEM_PROMPT)

    def test_humor_is_optional_and_bounded(self):
        data = {k: v for k, v in appraisal().__dict__.items()}
        data['evidence_ids'] = list(data['evidence_ids'])
        old = dict(data)
        old.pop('humor')
        self.assertEqual(Appraisal.from_dict(old).humor, 0.)
        self.assertEqual(Appraisal.from_dict({**old, 'humor': .7}).humor, .7)
        with self.assertRaises(ValueError):
            Appraisal.from_dict({**old, 'humor': 1.5})

    def test_an_interesting_idea_moves_her_within_her_own_budget(self):
        emotions, state = felt(appraisal(**CURIOUS), NEUTRAL)
        self.assertEqual(expression(state).dominant, 'interest')
        self.assertLessEqual(sum(emotions.values()), OWN_GOAL_BUDGET + 1e-9)
        self.assertGreater(emotions['interest'], .1)
        # The user's goals keep the old protective budget on the same ordinary message.
        both = replace(perception(event(), appraisal(**CURIOUS),
                                  appraisal(goal_id='help_user', congruence=.9, novelty=.9, agency_other=.9,
                                            intentionality=.9, norm_violation=0.)), situation=NEUTRAL)
        state = appraise(initial_state(CONTEXT, AT), event(), both)
        user_side = sum(e.intensity for e in state.episodes if e.goal_id == 'help_user')
        self.assertLessEqual(user_side, .12 + 1e-9)

    def test_ordinary_message_is_capped_for_user_goals_not_for_her_curiosity(self):
        p = replace(perception(event(), appraisal(**CURIOUS), appraisal(goal_id='help_user', relevance=.9, novelty=.9)),
                    situation=NEUTRAL)
        own, user = calibrated_appraisals(p).appraisals
        self.assertEqual((own.relevance, own.novelty), (.6, .8))
        self.assertEqual((user.relevance, user.novelty), (.2, .15))

    def test_preference_and_recall_never_move_her(self):
        emotions, _ = felt(appraisal(**CURIOUS), Situation(kind='preference', modality='interaction'))
        self.assertEqual(emotions, {})
        ev = event(origin=Origin.RECALL)
        emotions, _ = felt(appraisal(**CURIOUS), NEUTRAL, ev=ev)
        self.assertLessEqual(sum(emotions.values()), .08 + 1e-9)


class PaletteTests(unittest.TestCase):
    def test_every_emotion_has_a_voice_and_fits_the_state(self):
        self.assertEqual(set(EMOTION_VOICE), set(EMOTIONS) | {'indignation'})
        self.assertTrue(all(activation >= 0 for _, activation, _ in EMOTIONS.values()))
        self.assertLessEqual(len(EMOTIONS), 16)

    def test_a_joke_amuses_but_mockery_never_does(self):
        joke = appraisal(goal_id='connection', congruence=.4, novelty=.4, agency_other=.6, intentionality=.6,
                         norm_violation=0., humor=.9, relevance=.7)
        emotions, state = felt(joke, NEUTRAL)
        self.assertEqual(expression(state).dominant, 'amusement')
        self.assertGreaterEqual(expression(state).playfulness, .2)
        mockery, _ = felt(appraisal(humor=.9))
        self.assertNotIn('amusement', mockery)
        self.assertIn('anger', mockery)

    def test_the_other_persons_achievement_is_admired_not_thanked(self):
        success = appraisal(goal_id='user_wellbeing', congruence=.8, agency_other=.9, intentionality=.8,
                            norm_violation=0., novelty=.3)
        sit = Situation(kind='success', modality='interaction', outcome='confirmed')
        now, _ = felt(success, sit)
        self.assertIn('admiration', now)
        self.assertNotIn('gratitude', now)
        before, _ = felt(success, sit, TEMPERAMENT_V1)
        self.assertIn('gratitude', before)
        self.assertNotIn('admiration', before)

    def test_closeness_brings_tenderness_with_gratitude(self):
        care = appraisal(goal_id='connection', congruence=.8, novelty=.1, agency_other=.9, intentionality=.9,
                         norm_violation=0.)
        emotions, _ = felt(care, Situation(kind='neutral', modality='interaction', social_signal='care',
                                           intention_evidence='explicit'))
        self.assertIn('tenderness', emotions)
        self.assertIn('gratitude', emotions)

    def test_boredom_turns_the_topic_not_the_warmth(self):
        bored = appraisal(goal_id='curiosity', congruence=-.5, novelty=.05, agency_other=.2, intentionality=.1,
                          norm_violation=0.)
        emotions, state = felt(bored, NEUTRAL)
        self.assertIn('boredom', emotions)
        self.assertNotIn('disappointment', emotions)
        result = expression(state)
        self.assertEqual((result.dominant, result.tone), ('boredom', 'calm'))
        self.assertFalse(result.serious)
        self.assertGreaterEqual(result.warmth, .6)
        self.assertIn(EMOTION_VOICE['boredom'], result.instruction())

    def test_relief_needs_an_actual_resolution(self):
        safe = appraisal(goal_id='safety', congruence=.7, agency_other=0., intentionality=0., norm_violation=0.,
                         future_threat=.1)
        resolved, _ = felt(safe, Situation(kind='threat', modality='interaction', outcome='resolved'))
        self.assertIn('relief', resolved)
        pending, _ = felt(safe, Situation(kind='threat', modality='interaction', outcome='pending'))
        self.assertNotIn('relief', pending)


class VersionedDynamicsTests(unittest.TestCase):
    def test_states_of_the_previous_temperament_are_upgraded_explicitly(self):
        old = replace(appraise(initial_state(CONTEXT, AT, TEMPERAMENT_V1), event(),
                               perception(event(), appraisal()), temperament=TEMPERAMENT_V1))
        self.assertEqual(old.temperament_version, 'arti-temperament-1')
        upgraded = load_state(object_value(dump(old)))
        self.assertEqual(upgraded.temperament_version, TEMPERAMENT_VERSION)
        self.assertEqual(upgraded.episodes, old.episodes)
        self.assertEqual(upgraded.mood_valence_latent, old.mood_valence_latent)
        advance(upgraded, AT + timedelta(hours=1))
        with self.assertRaises(ValueError):
            advance(old, AT + timedelta(hours=1))
        unknown = {**object_value(dump(old)), 'temperament_version': 'arti-temperament-9'}
        with self.assertRaises(ValueError):
            advance(load_state(unknown), AT + timedelta(hours=1))

    def test_history_replays_under_its_own_dynamics(self):
        _, v1 = felt(appraisal(), temperament=TEMPERAMENT_V1)
        _, v2 = felt(appraisal())
        self.assertEqual([(e.emotion, e.intensity) for e in v1.episodes], [(e.emotion, e.intensity) for e in v2.episodes])
        self.assertNotAlmostEqual(mood_after(v1, 2, TEMPERAMENT_V1), mood_after(v2, 2), places=3)
        self.assertEqual(Temperament().version, TEMPERAMENT_VERSION)

    def test_grief_leaves_an_aftertaste_for_hours_then_fades(self):
        grief = appraisal(goal_id='user_wellbeing', congruence=-.9, loss=.9, irreversibility=.9, agency_other=0.,
                          intentionality=0., norm_violation=0.)
        sit = Situation(kind='loss', modality='reported', outcome='confirmed')
        _, now = felt(grief, sit)
        _, before = felt(grief, sit, TEMPERAMENT_V1)
        self.assertLess(mood_after(now, 3), .08 - .08)
        self.assertGreater(mood_after(before, 3, TEMPERAMENT_V1), .05)
        self.assertGreater(mood_after(now, 24), .04)

    def test_many_small_joys_do_not_make_her_euphoric(self):
        state = initial_state(CONTEXT, AT)
        joy = dict(goal_id='connection', congruence=.6, agency_other=.5, intentionality=.5, norm_violation=0.)
        for n in range(20):
            ev = event(f'j{n}', at=AT + timedelta(minutes=3 * n))
            state = appraise(state, ev, replace(perception(ev, appraisal(evidence_ids=(f'j{n}',), **joy)), situation=NEUTRAL))
        self.assertLess(affect(advance(state, AT + timedelta(hours=2)))['mood_valence'], .5)


def rel_event(n, text, hours=0.):
    return event(f'r{n}', at=AT + timedelta(hours=hours), text=text)


def step(relationship, n, text, hours=0., **changes):
    ev = rel_event(n, text, hours)
    return relationship_transition(relationship, ev, situation(ev, **changes)), ev


INSULT = dict(kind='conflict', intention_evidence='explicit', social_signal='insult')


class RuptureTests(unittest.TestCase):
    def test_an_explicit_insult_opens_a_rupture_that_outlives_the_anger(self):
        r, ev = step(initial_relationship(), 1, 'Ты тупая железка.', **INSULT)
        self.assertEqual(r['rupture']['status'], 'open')
        self.assertAlmostEqual(rupture_severity(r['rupture'], AT), .6)
        day = relationship_view(r, AT + timedelta(days=1))['rupture']
        self.assertAlmostEqual(day['severity'], .6 * math.exp(-86400 / RUPTURE_TAU_SECONDS))
        self.assertAlmostEqual(day['days'], 1.)
        self.assertIsNone(relationship_view(r, AT + timedelta(days=7))['rupture'])

    def test_ambiguous_or_reported_hostility_opens_nothing(self):
        r, _ = step(initial_relationship(), 1, 'Ничего себе эксперт.', kind='conflict', intention_evidence='ambiguous',
                    social_signal='insult')
        self.assertNotIn('rupture', r)
        r, _ = step(initial_relationship(), 2, 'Он сказал мне: ты тупая.', modality='reported', **INSULT)
        self.assertNotIn('rupture', r)

    def test_apology_repairs_and_care_softens(self):
        r, _ = step(initial_relationship(), 1, 'Ты тупая железка.', **INSULT)
        repaired, _ = step(r, 2, 'Прости, я был неправ.', hours=1, social_signal='apology')
        self.assertEqual(repaired['rupture']['status'], 'repaired')
        self.assertIsNone(relationship_view(repaired, AT + timedelta(hours=2))['rupture'])
        softened, _ = step(r, 3, 'Спасибо, ты мне правда помогла.', hours=1, social_signal='care')
        self.assertEqual(softened['rupture']['status'], 'open')
        self.assertLess(rupture_severity(softened['rupture'], AT + timedelta(hours=1)), .31)
        again, _ = step(softened, 4, 'Ты лучшая.', hours=2, social_signal='care')
        self.assertEqual(again['rupture']['status'], 'open')
        healed, _ = step(again, 5, 'Правда, спасибо.', hours=3, social_signal='care')
        self.assertEqual(healed['rupture']['status'], 'repaired')
        self.assertEqual(healed['rupture']['repaired_by'], 'care')

    def test_repeated_insults_escalate_but_stay_bounded(self):
        r = initial_relationship()
        for n in range(6):
            r, _ = step(r, n, f'Ты тупая {n}.', hours=n * .1, **INSULT)
        self.assertLessEqual(r['rupture']['severity'], 1.)
        self.assertGreater(r['rupture']['severity'], .9)
        self.assertEqual(r['rupture']['opened_at'], AT.isoformat())

    def test_breach_is_a_milder_rupture_and_old_models_need_nothing(self):
        r, _ = step(initial_relationship(), 1, 'Я обещал и специально не сделал.', kind='conflict',
                    intention_evidence='explicit', social_signal='breach')
        self.assertAlmostEqual(r['rupture']['severity'], .35)
        legacy = initial_relationship()
        self.assertIsNone(relationship_view(legacy, AT)['rupture'])

    def test_rupture_shapes_the_manner_without_stopping_help(self):
        hurt = plan(rupture=.5, rupture_days=1.3)
        text = hurt.instruction()
        self.assertIn('вчера', text)
        self.assertIn('помощь как обычно', text)
        self.assertTrue(hurt.humour().startswith('Без игривости'))
        self.assertIn('сегодня', plan(rupture=.5, rupture_days=.2).instruction())
        self.assertNotIn('задел тебя', plan(rupture=RUPTURE_VISIBLE / 2).instruction())
        from cognition import expression_projection as projection
        event_id = next(f'e{n}' for n in range(1000) if projection._unit(f'e{n}', 'sticker') < .35
                        and projection._unit(f'e{n}', 'reaction') < .5)
        self.assertIsNone(projection.sticker_mood(plan(sticker_mood='happy', stage='familiar', rupture=.4), event_id=event_id))
        self.assertIsNone(projection.reaction(plan(rupture=.4), social='care', event_id=event_id))
        self.assertEqual(projection.reaction(plan(), social='care', event_id=event_id), '❤')
        with self.assertRaises(ValueError):
            plan(rupture=1.5)


class RollbackTests(unittest.TestCase):
    def test_states_and_perceptions_return_to_the_previous_format(self):
        from cognition.temperament_rollback import downgrade_perception, downgrade_state
        joke = appraisal(goal_id='connection', congruence=.4, agency_other=.6, intentionality=.6, norm_violation=0., humor=.9)
        _, state = felt(joke, NEUTRAL)
        data = object_value(dump(replace(state, residues=(
            AffectiveResidue('amusement', .2, 900., 2), AffectiveResidue('joy', .1, 1200., 1)))))
        old, changed = downgrade_state(data)
        self.assertTrue(changed)
        self.assertEqual(old['temperament_version'], 'arti-temperament-1')
        self.assertTrue(set(e['emotion'] for e in old['episodes']) <= set(EMOTIONS) - {'amusement', 'relief', 'admiration', 'tenderness', 'boredom'})
        self.assertEqual([(r['emotion'], round(r['intensity'], 6), r['cause_count']) for r in old['residues']], [('joy', .3, 3)])
        self.assertFalse(downgrade_state(old)[1])
        p = object_value(dump(replace(perception(event(), appraisal(**CURIOUS), joke))))
        q, changed = downgrade_perception(p)
        self.assertTrue(changed)
        self.assertEqual([a['goal_id'] for a in q['appraisals']], ['connection'])
        self.assertNotIn('humor', q['appraisals'][0])
        self.assertFalse(downgrade_perception(q)[1])

    def test_fast_impression_of_laughter_is_amusement(self):
        from cognition.fast_appraisal import fast_signal
        self.assertEqual(fast_signal('ахахах').emotion, 'amusement')


@unittest.skipUnless(os.getenv('ARTI_TEST_DB') == '1', 'disposable PostgreSQL required')
class OwnLifeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.support.database import isolated_database
        from bot.request_runtime import CURRENT_REQUEST
        from cognition.runtime import CognitiveRuntime, CURRENT_TURN
        from cognition.scope import CURRENT_SCOPE
        from tests.cognition.test_full_model import RecordedInterpreter
        self.db = isolated_database()
        self.pool = await self.db.__aenter__()
        self.tokens = [(v, v.set(None)) for v in (CURRENT_TURN, CURRENT_SCOPE, CURRENT_REQUEST)]
        self.at = AT
        self.interpreter = RecordedInterpreter()
        self.runtime = await CognitiveRuntime(self.pool, self.interpreter, 'active', clock=lambda: self.at).initialize(False)
        self.message = 0

    async def asyncTearDown(self):
        await self.runtime.close()
        for var, token in reversed(self.tokens):
            var.reset(token)
        await self.db.__aexit__(None, None, None)

    async def observe(self, text, frame=None, **changes):
        self.message += 1
        cid, eid, ev = await self.runtime.ingest(10, 1, text, self.message)
        if frame is not None or changes:
            self.interpreter.frames[text] = frame(ev) if frame else situation(ev, **changes)
        await self.runtime.process(cid, eid)
        self.at += timedelta(minutes=1)
        return cid, eid, ev

    async def reply_plan(self, text='Помоги разобрать список дел'):
        self.message += 1
        turn = await self.runtime.prepare(10, 1, text, self.message)
        self.at += timedelta(minutes=1)
        return turn.expression

    async def test_after_an_insult_she_is_cooler_until_an_apology(self):
        calm = await self.reply_plan('Привет')
        await self.observe('Ты тупая железка.', **INSULT)
        hurt = await self.reply_plan()
        self.assertGreaterEqual(hurt.rupture, RUPTURE_VISIBLE)
        self.assertEqual(hurt.playfulness, 0.)
        self.assertLess(hurt.warmth, calm.warmth - .09)
        self.assertIn('задел тебя сегодня', hurt.instruction())
        # A day later the rupture is still remembered, though weaker.
        self.at += timedelta(days=1)
        later = await self.reply_plan('Ну что, продолжим?')
        self.assertTrue(RUPTURE_VISIBLE <= later.rupture < hurt.rupture)
        self.assertIn('вчера', later.instruction())
        await self.observe('Прости, я вчера был неправ.', social_signal='apology')
        repaired = await self.reply_plan('Давай дальше')
        self.assertEqual(repaired.rupture, 0.)
        self.assertNotIn('задел тебя', repaired.instruction())

    async def test_a_stored_state_of_the_previous_temperament_keeps_working(self):
        cid, _, _ = await self.observe('Привет')
        async with self.pool.acquire() as conn:
            await conn.execute("""UPDATE cognitive_contexts SET state=jsonb_set(state,'{temperament_version}','"arti-temperament-1"')
                WHERE id=$1""", cid)
            await conn.execute("""UPDATE cognitive_artifacts SET payload=jsonb_set(payload,'{state,temperament_version}','"arti-temperament-1"')
                WHERE kind='personal_state' AND context_id=$1""", cid)
        await self.observe('Как дела?')
        self.assertIsNotNone(await self.reply_plan())
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT state->>'temperament_version' FROM cognitive_contexts WHERE id=$1", cid),
                             TEMPERAMENT_VERSION)

    async def test_rollback_tool_prepares_stored_data_for_older_code(self):
        from cognition.temperament_rollback import prepare_rollback
        from cognition.types import Perception, PERCEPTION_VERSION

        def witty(ev):
            refs = (ev.evidence.source_id,)
            return Perception(ev.event_id, PERCEPTION_VERSION, (
                appraisal(goal_id='aesthetics', congruence=.5, agency_other=.6, intentionality=.6, norm_violation=0.,
                          humor=.9, evidence_ids=refs),
                appraisal(goal_id='connection', congruence=.4, agency_other=.6, intentionality=.6, norm_violation=0.,
                          humor=.5, evidence_ids=refs)), situation(ev))
        cid, eid, _ = await self.observe('Каламбур дня: железная логика у железки.', frame=witty)
        async with self.pool.acquire() as conn:
            emotions = await conn.fetchval("SELECT state->'episodes' FROM cognitive_contexts WHERE id=$1", cid)
        self.assertIn('amusement', str(emotions))
        counted = await prepare_rollback(self.pool)
        self.assertGreaterEqual(counted['contexts'], 1)
        self.assertGreaterEqual(counted['personal_states'], 1)
        self.assertGreaterEqual(counted['perceptions'], 1)
        self.assertFalse(counted['applied'])
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT state->>'temperament_version' FROM cognitive_contexts WHERE id=$1", cid),
                             TEMPERAMENT_VERSION)
        await prepare_rollback(self.pool, apply=True)
        again = await prepare_rollback(self.pool)
        self.assertEqual((again['contexts'], again['personal_states'], again['perceptions'], again['reappraisals']), (0, 0, 0, 0))
        async with self.pool.acquire() as conn:
            state = object_value(await conn.fetchval('SELECT state FROM cognitive_contexts WHERE id=$1', cid))
            stored = object_value(await conn.fetchval('SELECT perception FROM cognitive_events WHERE id=$1', eid))
        self.assertEqual(state['temperament_version'], 'arti-temperament-1')
        self.assertNotIn('amusement', {e['emotion'] for e in state['episodes']})
        self.assertEqual([a['goal_id'] for a in stored['appraisals']], ['connection'])
        self.assertNotIn('humor', stored['appraisals'][0])


if __name__ == '__main__':
    unittest.main()
