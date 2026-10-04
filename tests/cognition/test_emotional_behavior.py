"""Offline synthetic emotional-behavior contracts, not claims about model accuracy."""
import unittest
from dataclasses import asdict, replace
from datetime import timedelta

from cognition.affect import advance, affect, appraise, expression, initial_state, replay_ledger
from cognition.regulation import regulate
from cognition.serialization import dump, load_state
from cognition.situations import Situation, SourceSpan
from cognition.types import AffectiveResidue, EmotionEpisode, LIGHT_HUMOUR_THRESHOLD
from tests.cognition.test_affect import AT, CONTEXT, appraisal, event, perception


def episode(name,emotion='joy',intensity=.3,confidence=.9,*,cause=None,at=AT):
    taus = {'joy':1200.,'sadness':7200.,'fear':2400.,'anger':1800.,'gratitude':3600.}
    return EmotionEpisode(name,cause or name,name,'help_user',1,emotion,intensity,confidence,at,taus[emotion])


def state_with(*episodes):
    return replace(initial_state(CONTEXT,AT),episodes=tuple(episodes))


def detail_situation(kind='success',**changes):
    text = 'The import now finishes in 14 seconds.'
    values = dict(kind=kind,modality='interaction',outcome='confirmed',
                  spans=(SourceSpan(0,len(text),text),),
                  details=({'span':0,'kind':'action','centrality':.8,'confidence':.9},))
    values.update(changes)
    return Situation(**values)


class MixtureAndReserveTests(unittest.TestCase):
    def test_negative_causes_below_six_positive_episodes_still_form_mixture(self):
        state = state_with(*(episode('joy'+str(i),intensity=.2) for i in range(6)),
                           *(episode('loss'+str(i),'sadness',.18) for i in range(3)))
        plan = expression(state)
        self.assertTrue(plan.mixed_affect)
        self.assertIsNone(plan.sticker_mood)
        self.assertTrue(any(c.startswith('loss') for c in plan.cause_ids))
        self.assertTrue(any(c.startswith('joy') for c in plan.cause_ids))
        self.assertLessEqual(len(plan.cause_ids),6)

    def test_positive_causes_below_six_negative_episodes_still_form_mixture(self):
        state = state_with(*(episode('fear'+str(i),'fear',.25) for i in range(6)),
                           *(episode('progress'+str(i),intensity=.2) for i in range(3)))
        plan = expression(state)
        self.assertTrue(plan.mixed_affect)
        self.assertTrue(any(c.startswith('progress') for c in plan.cause_ids))
        self.assertTrue(any(c.startswith('fear') for c in plan.cause_ids))

    def test_individually_small_pulses_can_collectively_form_mixture(self):
        state = state_with(*(episode('joy'+str(i),intensity=.02) for i in range(6)),
                           *(episode('loss'+str(i),'sadness',.015) for i in range(3)))
        self.assertTrue(expression(state).mixed_affect)

    def test_small_but_meaningful_mixture_never_allows_happy_sticker_or_humour(self):
        plan = expression(state_with(episode('small-win',intensity=.05),episode('small-loss','sadness',.012)))
        self.assertTrue(plan.mixed_affect)
        self.assertIsNone(plan.sticker_mood)
        self.assertEqual(plan.playfulness,0.)

    def test_compacted_tail_affects_mixture_without_fabricating_a_cause(self):
        state = replace(state_with(episode('present-loss','sadness',.15)),
                        residues=(AffectiveResidue('joy',.3,1200.,30),))
        plan = expression(state)
        self.assertTrue(plan.mixed_affect)
        self.assertEqual(plan.cause_ids,('present-loss',))
        self.assertIsNone(plan.sticker_mood)

    def test_negligible_tails_do_not_claim_an_active_mixture(self):
        state = state_with(episode('old-good',intensity=.00001),episode('old-bad','sadness',.00001))
        plan = expression(state)
        self.assertFalse(plan.mixed_affect)
        self.assertEqual(plan.cause_ids,())
        self.assertEqual(plan.tone,'calm')

    def test_repeated_positive_load_cannot_drown_one_substantial_loss(self):
        positive = tuple(episode('thanks'+str(i),intensity=.3) for i in range(100))
        state = state_with(*positive,episode('loss','sadness',.3))
        before = dump(state)
        plan = expression(state)
        self.assertTrue(plan.mixed_affect)
        self.assertIn('loss',plan.cause_ids)
        self.assertIsNone(plan.sticker_mood)
        self.assertEqual(plan.playfulness,0.)
        self.assertEqual(dump(state),before)
        # Display habituation does not silently reinterpret a persisted model.
        self.assertGreater(sum(e.intensity for e in state.episodes),30.)
        _,current = regulate(state,Situation(kind='loss',modality='interaction'))
        self.assertEqual(current.behaviors,('acknowledge_loss','offer_choice'))
        self.assertEqual(current.regulation,'acknowledge')
        self.assertEqual(current.playfulness,0.)

    def test_repetition_does_not_inflate_warmth_disclosure_or_humour(self):
        first = expression(state_with(episode('first-thanks')))
        repeated = expression(state_with(*(episode('thanks'+str(i)) for i in range(100))))
        for dimension in ('warmth','directness','disclosure','playfulness'):
            self.assertEqual(getattr(first,dimension),getattr(repeated,dimension))

    def test_positive_only_humour_crosses_shared_threshold(self):
        state = state_with(episode('win'))
        _,plan = regulate(state,Situation(kind='success',modality='interaction'))
        self.assertGreaterEqual(plan.playfulness,LIGHT_HUMOUR_THRESHOLD)
        self.assertIn('Light humour is optional',plan.instruction())
        self.assertEqual(expression(initial_state(CONTEXT,AT)).playfulness,0.)

    def test_old_positive_affect_does_not_add_humour_to_a_new_request(self):
        positive = state_with(episode('older-win'))
        _,plan = regulate(positive,Situation(kind='request',modality='interaction'))
        self.assertEqual(plan.behaviors,('answer_task','practical_step'))
        self.assertEqual(plan.playfulness,0.)

    def test_serious_mixed_or_exhausted_context_suppresses_humour(self):
        positive = state_with(episode('win'))
        candidates = [expression(positive,task_serious=True),
                      expression(replace(positive,effort_load=.9)),
                      expression(state_with(episode('win'),episode('loss','sadness',.3)))]
        for plan in candidates:
            self.assertLess(plan.playfulness,LIGHT_HUMOUR_THRESHOLD)

    def test_cause_selection_and_full_plan_are_order_independent(self):
        episodes = tuple(episode('win'+str(i),intensity=.1) for i in range(7)) + (episode('loss','sadness',.3),)
        self.assertEqual(expression(state_with(*episodes)),expression(state_with(*reversed(episodes))))


class CurrentSituationTests(unittest.TestCase):
    def setUp(self):
        self.stale = state_with(episode('older-ambiguous-conflict','anger',.2,.4,at=AT-timedelta(hours=1)),
                                episode('progress',intensity=.3))
        self.assertTrue(expression(self.stale).uncertain_intent)

    def test_stale_uncertainty_cannot_hijack_clear_request_loss_success(self):
        for kind,expected in [('request',('answer_task','practical_step')),
                              ('loss',('acknowledge_loss','offer_choice')),
                              ('success',('recognize_progress','listen'))]:
            with self.subTest(kind=kind):
                before = dump(self.stale)
                decision,plan = regulate(self.stale,Situation(kind=kind,modality='interaction'))
                self.assertEqual(plan.behaviors,expected)
                self.assertNotEqual(decision.strategy,'clarify')
                self.assertFalse(plan.uncertain_intent)
                self.assertTrue(plan.mixed_affect)
                self.assertEqual(dump(self.stale),before)

    def test_current_ambiguous_conflict_clarifies_without_a_boundary(self):
        _,plan = regulate(state_with(episode('joy')),Situation(kind='conflict',modality='interaction',intention_evidence='ambiguous'))
        self.assertEqual(plan.behaviors,('ask_one_question',))
        self.assertTrue(plan.uncertain_intent)
        self.assertEqual(plan.regulation,'clarify')
        self.assertIsNone(plan.sticker_mood)
        self.assertEqual(plan.playfulness,0.)

    def test_missing_reminder_time_survives_serious_task_override(self):
        situation = Situation(kind='request',modality='interaction',
                              intentions=({'status':'reminder','deadline':None},))
        _,plan = regulate(self.stale,situation,task_serious=True)
        self.assertEqual(plan.behaviors,('ask_one_question',))
        self.assertFalse(plan.uncertain_intent)
        self.assertEqual(plan.regulation,'clarify')

    def test_current_loss_is_gentle_even_after_exclusively_positive_state(self):
        _,plan = regulate(state_with(episode('win')),Situation(kind='loss',modality='reported'))
        self.assertEqual(plan.behaviors,('acknowledge_loss','offer_choice'))
        self.assertEqual(plan.tone,'gentle and concrete')
        self.assertEqual(plan.playfulness,0.)
        self.assertEqual(plan.disclosure,0.)
        self.assertIsNone(plan.sticker_mood)

    def test_current_threat_keeps_practical_help_despite_stale_uncertainty(self):
        _,plan = regulate(self.stale,Situation(kind='threat',modality='interaction'))
        self.assertEqual(plan.behaviors,('ask_one_question','practical_step'))
        self.assertEqual(plan.regulation,'problem_solve')
        self.assertFalse(plan.uncertain_intent)
        self.assertEqual(plan.playfulness,0.)

    def test_quote_or_hypothesis_does_not_direct_personal_condolences_or_clarify(self):
        for modality in ('quoted','hypothetical'):
            _,plan = regulate(self.stale,Situation(kind='loss',modality=modality))
            self.assertEqual(plan.behaviors,('listen',))
            self.assertEqual(plan.regulation,'acknowledge')
            self.assertFalse(plan.uncertain_intent)

    def test_grounded_detail_is_noticed_without_losing_primary_action(self):
        _,plan = regulate(self.stale,detail_situation('loss'))
        self.assertEqual(plan.behaviors,('acknowledge_loss','offer_choice','notice_detail'))
        _,task = regulate(self.stale,detail_situation('request'),task_serious=True)
        self.assertEqual(task.behaviors,('answer_task','practical_step','notice_detail'))

    def test_weak_or_unsourced_detail_does_not_claim_attentiveness(self):
        for changes in ({'confidence':.4},{'centrality':.1},{'span':9},{'span':True}):
            detail = {'span':0,'kind':'action','centrality':.8,'confidence':.9,**changes}
            _,plan = regulate(self.stale,detail_situation(details=(detail,)))
            self.assertNotIn('notice_detail',plan.behaviors)
        _,quoted = regulate(self.stale,detail_situation(modality='quoted'))
        self.assertNotIn('notice_detail',quoted.behaviors)

    def test_revision_does_not_preserve_old_hostility_question(self):
        situation = Situation(kind='conflict',modality='interaction',intention_evidence='ambiguous',
                              revisions=({'source_id':'older-ambiguous-conflict'},))
        _,plan = regulate(self.stale,situation)
        self.assertEqual(plan.regulation,'reappraise')
        self.assertEqual(plan.behaviors,('revise_understanding','answer_task'))
        self.assertFalse(plan.uncertain_intent)

    def test_sticker_preference_and_emotional_state_are_independent(self):
        positive = state_with(episode('win'))
        before = asdict(positive)
        _,plan = regulate(positive,Situation(kind='success',modality='interaction'),{'stickers':False})
        self.assertIsNone(plan.sticker_mood)
        self.assertEqual(asdict(positive),before)


class OwnActionRepairTests(unittest.TestCase):
    def setUp(self):
        self.state = state_with(episode('old-win'))
        self.current = Situation(kind='clarification',modality='interaction')

    def test_current_grounded_own_breach_can_select_repair(self):
        situation = replace(self.current,kind='conflict',social_signal='breach',social_signal_actor='arti',
                            outcome='confirmed',intention_evidence='explicit')
        _,plan = regulate(self.state,situation)
        self.assertEqual(plan.behaviors,('repair','practical_step'))
        self.assertIsNone(plan.sticker_mood)
        self.assertEqual(plan.playfulness,0.)
        self.assertEqual(plan.disclosure,0.)

    def test_actual_delivered_reply_review_requests_evidence_before_repair(self):
        _,plan = regulate(self.state,self.current,own_action_review={
            'status':'reported_review','confidence':.9,'action_source':'delivered:42'})
        self.assertEqual(plan.behaviors,('revise_understanding','repair'))
        self.assertEqual(plan.regulation,'reappraise')
        self.assertIsNone(plan.sticker_mood)
        self.assertEqual(plan.playfulness,0.)

    def test_verified_runtime_error_selects_repair(self):
        _,plan = regulate(self.state,self.current,task_serious=True,
                          own_action_review={'status':'verified_error','action_source':'delivered:42'})
        self.assertEqual(plan.behaviors,('repair','practical_step'))
        self.assertEqual(plan.regulation,'problem_solve')

    def test_user_or_unconfirmed_breach_does_not_assign_own_blame(self):
        base = replace(self.current,kind='conflict',social_signal='breach',social_signal_actor='arti',
                       outcome='confirmed',intention_evidence='explicit')
        for changes in ({'social_signal_actor':1},{'outcome':'unknown'},
                        {'intention_evidence':'ambiguous'},{'modality':'reported'},
                        {'modality':'quoted'},{'modality':'hypothetical'}):
            _,plan = regulate(self.state,replace(base,**changes))
            self.assertNotIn('repair',plan.behaviors)

    def test_low_confidence_review_or_unknown_status_does_not_select_repair(self):
        for review in ({'status':'reported_review','confidence':.2},
                       {'status':'claimed_error','confidence':1.}):
            _,plan = regulate(self.state,self.current,own_action_review=review)
            self.assertNotIn('repair',plan.behaviors)

    def test_quoted_review_cannot_activate_personal_repair(self):
        _,plan = regulate(self.state,replace(self.current,modality='quoted'),
                          own_action_review={'status':'reported_review','confidence':1.})
        self.assertNotIn('repair',plan.behaviors)


class ReplayAndTimeConsistencyTests(unittest.TestCase):
    def trajectory(self,count=45):
        rows = []
        for i in range(count):
            ev = event('positive:'+str(i),at=AT+timedelta(minutes=i),text='Synthetic repeated thanks')
            p = perception(ev,appraisal(goal_id='help_user',congruence=.8,norm_violation=0.,evidence_ids=(ev.event_id,)))
            rows.append((ev,p))
        return rows

    def test_repetition_replay_serialization_and_display_are_deterministic(self):
        rows = self.trajectory()
        state = initial_state(CONTEXT,AT)
        for ev,p in rows: state = appraise(state,ev,p)
        replay = replay_ledger(CONTEXT,rows,AT)
        self.assertEqual(replace(state,applied_groups=frozenset()),replay)
        self.assertEqual(load_state(dump(state)),state)
        self.assertEqual(expression(replay),expression(state))
        before = dump(state)
        for _ in range(20): expression(state)
        self.assertEqual(before,dump(state))

    def test_repeated_display_does_not_change_future_numerical_state(self):
        rows = self.trajectory(12)
        left = right = initial_state(CONTEXT,AT)
        for ev,p in rows:
            expression(left)
            left = appraise(left,ev,p)
            right = appraise(right,ev,p)
        self.assertEqual(left,right)

    def test_polling_before_observations_matches_direct_event_time(self):
        direct = polled = initial_state(CONTEXT,AT)
        rows = self.trajectory(12)
        for ev,p in rows:
            direct = appraise(direct,ev,p)
            mid = polled.last_at + (ev.observed_at-polled.last_at)/2
            polled = appraise(advance(polled,mid),ev,p)
        for key,value in affect(direct).items():
            self.assertAlmostEqual(value,affect(polled)[key],places=12)
        a,b = expression(direct),expression(polled)
        self.assertEqual(a.behaviors,b.behaviors)
        self.assertEqual(a.mixed_affect,b.mixed_affect)
        self.assertEqual(a.cause_ids,b.cause_ids)
        self.assertAlmostEqual(a.warmth,b.warmth,places=12)

    def test_duplicate_independence_group_is_noop_even_after_many_repetitions(self):
        state = initial_state(CONTEXT,AT)
        rows = self.trajectory()
        for ev,p in rows: state = appraise(state,ev,p)
        ev,p = rows[-1]
        self.assertEqual(appraise(state,replace(ev,observed_at=ev.observed_at+timedelta(days=1)),p),state)

    def test_later_genuine_losses_are_not_habituated_in_persisted_state(self):
        state = initial_state(CONTEXT,AT)
        impulses = []
        for i in range(5):
            ev = event('loss:'+str(i),at=AT+timedelta(minutes=i))
            p = perception(ev,appraisal(goal_id='user_wellbeing',congruence=-.8,agency_other=0.,
                                        loss=1.,irreversibility=1.,evidence_ids=(ev.event_id,)))
            state = appraise(state,ev,p)
            impulses.append(sum(e.intensity for e in state.episodes if e.cause_id==ev.event_id))
        self.assertTrue(all(abs(value-impulses[0])<1e-12 for value in impulses))

    def test_quiet_time_relaxes_affect_without_erasing_causal_ledger(self):
        state = state_with(episode('loss','sadness',.3),episode('progress',intensity=.3))
        future = advance(state,AT+timedelta(days=30))
        self.assertAlmostEqual(affect(future)['valence'],.08,places=7)
        self.assertEqual(state.applied_groups,future.applied_groups)
        self.assertFalse(expression(future).mixed_affect)
        self.assertEqual(expression(future).cause_ids,())
