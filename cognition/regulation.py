"""Causal regulation decisions, distinct from display suppression."""
from dataclasses import dataclass, replace
from cognition.affect import expression


@dataclass(frozen=True)
class RegulationDecision:
    strategy: str
    expected_outcome: str
    evidence_ids: tuple[str,...]
    internally_revised: bool
    pending: bool


def regulate(state,situation,preferences=None,task_serious=False,*,own_action_review=None):
    plan = expression(state,task_serious=task_serious)
    observed = bool(situation and situation.modality not in ('quoted','hypothetical'))
    # A current request, loss or success must not inherit an old interpretation's
    # missing facts. Past affect can still influence manner and mixed expression.
    strategy = ('problem_solve' if task_serious else 'acknowledge') if situation is not None else plan.regulation
    uncertain = bool(observed and situation.kind == 'conflict' and situation.intention_evidence == 'ambiguous')
    needs_time = bool(observed and any(i['status']=='reminder' and not i['deadline'] for i in situation.intentions))
    review = own_action_review or {}
    reported_review = observed and review.get('status')=='reported_review' and review.get('confidence',0.)>=.6
    verified_error = observed and (review.get('status')=='verified_error' or (
        situation.modality=='interaction' and situation.social_signal_actor=='arti'
        and situation.social_signal=='breach' and situation.outcome=='confirmed'
        and situation.intention_evidence=='explicit'))
    if verified_error:
        strategy = 'problem_solve'
    elif reported_review or (observed and situation.revisions):
        strategy = 'reappraise'
    elif observed and situation.kind == 'loss':
        strategy = 'acknowledge'
    elif observed and situation.kind == 'threat':
        strategy = 'problem_solve'
    elif uncertain or needs_time:
        strategy = 'clarify'
    elif task_serious and any(e.emotion=='anger' and e.intensity>.04 for e in state.episodes):
        strategy = 'withhold_expression'
    elif state.effort_load>.8:
        strategy = 'attention_shift'

    # Current, grounded situation selects useful behavior. Affect changes the
    # manner and reserve, not what the user must feel or what happened.
    behaviors = list(plan.behaviors) if situation is None else ['answer_task' if task_serious else 'listen']
    if observed:
        if verified_error:
            behaviors = ['repair','practical_step']
        elif reported_review:
            # A complaint about an actual delivered reply is not proof of error.
            # The repair renderer requires checking evidence before admitting it.
            behaviors = ['revise_understanding','repair']
        elif situation.revisions:
            behaviors = ['revise_understanding','answer_task']
        elif situation.kind == 'loss':
            behaviors = ['acknowledge_loss','offer_choice']
        elif situation.kind == 'threat':
            behaviors = ['ask_one_question','practical_step']
        elif strategy == 'clarify':
            behaviors = ['ask_one_question']
        elif situation.kind == 'success':
            behaviors = ['recognize_progress']
            if plan.mixed_affect: behaviors.append('listen')
        elif situation.kind == 'conflict' and situation.intention_evidence == 'explicit':
            behaviors = ['boundary','practical_step']
        elif situation.kind in ('request','preference') or task_serious:
            behaviors = ['answer_task','practical_step']
        elif situation.social_signal == 'apology':
            behaviors = ['listen','practical_step']
        # Mention at most one useful, current source-backed detail. A historical
        # feeling or unsourced semantic label is not permission to invent recall.
        if any(d.get('confidence',0.)>=.75 and d.get('centrality',0.)>=.5
               and type(d.get('span')) is int and 0<=d['span']<len(situation.spans)
               for d in situation.details):
            behaviors.append('notice_detail')
    if task_serious and 'answer_task' not in behaviors and not (
            verified_error or reported_review or strategy=='clarify'
            or (observed and situation.kind in ('loss','threat','conflict'))):
        behaviors = ['answer_task','practical_step'] + [b for b in behaviors if b=='notice_detail']

    preferences = preferences or {}
    sensitive = bool(verified_error or reported_review or (observed and (
        situation.kind in ('loss','threat','conflict') or situation.revisions or situation.social_signal=='apology')))
    sticker = plan.sticker_mood
    if preferences.get('stickers') is False or sensitive or strategy in ('clarify','withhold_expression','attention_shift'):
        sticker = None
    tone = plan.tone
    if verified_error or reported_review or strategy=='withhold_expression' or (observed and situation.kind=='threat'):
        tone = 'calm and concrete'
    elif observed and situation.kind=='loss':
        tone = 'gentle and concrete'
    current_uncertain = (uncertain and strategy=='clarify') if situation is not None else plan.uncertain_intent
    playful_context = situation is None or (observed and situation.kind in ('neutral','success')
        and situation.social_signal in ('contact','care','cooperation','fulfilled'))
    plan = replace(plan,regulation=strategy,sticker_mood=sticker,tone=tone,
                   uncertain_intent=current_uncertain,
                   behaviors=tuple(behaviors[:4]),
                   disclosure=0. if sensitive or task_serious else plan.disclosure,
                   playfulness=0. if task_serious or sensitive or strategy=='clarify' or not playful_context else plan.playfulness)
    decision = RegulationDecision(strategy,dict(clarify='resolve current uncertainty',reappraise='revise supported interpretation',
        problem_solve='address the actual cause',withhold_expression='maintain useful delivery',
        attention_shift='recover attention',acknowledge='recognize the experience')[strategy],
        plan.cause_ids,strategy=='reappraise',strategy in ('clarify','problem_solve'))
    return decision,plan
