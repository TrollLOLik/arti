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


def regulate(state,situation,preferences=None,task_serious=False):
    plan = expression(state,task_serious=task_serious)
    strategy = plan.regulation
    observed = situation and situation.modality not in ('quoted','hypothetical')
    if observed and situation.revisions:
        strategy = 'reappraise'
    elif observed and situation.intention_evidence == 'ambiguous' and situation.kind == 'conflict':
        strategy = 'clarify'
    elif observed and situation.kind == 'threat':
        strategy = 'problem_solve'
    elif observed and any(i['status']=='reminder' and not i['deadline'] for i in situation.intentions):
        strategy = 'clarify'
    elif task_serious and any(e.emotion=='anger' and e.intensity>.04 for e in state.episodes):
        strategy = 'withhold_expression'
    elif state.effort_load>.8:
        strategy = 'attention_shift'
    decision = RegulationDecision(strategy,dict(clarify='resolve uncertainty',reappraise='revise supported interpretation',
        problem_solve='address the actual cause',withhold_expression='maintain useful delivery',
        attention_shift='recover attention',acknowledge='recognize the experience')[strategy],
        plan.cause_ids,strategy=='reappraise',strategy in ('clarify','problem_solve'))
    # Current, grounded situation selects useful behavior. Affect changes the
    # manner and reserve, not what the user must feel or what happened.
    behaviors = list(plan.behaviors)
    factual = observed
    if factual:
        if situation.revisions:
            behaviors = ['revise_understanding','answer_task']
        elif strategy == 'clarify':
            behaviors = ['ask_one_question']
        elif situation.kind == 'loss':
            behaviors = ['acknowledge_loss','offer_choice']
        elif situation.kind == 'threat':
            behaviors = ['ask_one_question','practical_step']
        elif situation.kind == 'success':
            behaviors = ['recognize_progress']
            if plan.mixed_affect: behaviors.append('listen')
        elif situation.kind == 'conflict' and situation.intention_evidence == 'explicit':
            behaviors = ['boundary','practical_step']
        elif situation.kind in ('request','preference') or task_serious:
            behaviors = ['answer_task','practical_step']
        elif situation.social_signal == 'apology':
            behaviors = ['listen','practical_step']
    if task_serious and 'answer_task' not in behaviors and not (factual and situation.kind in ('loss','threat','conflict')):
        behaviors = ['answer_task','practical_step']
    preferences = preferences or {}
    sticker = plan.sticker_mood
    if preferences.get('stickers') is False or strategy in ('clarify','withhold_expression','attention_shift'):
        sticker = None
    plan = replace(plan,regulation=strategy,sticker_mood=sticker,
                   tone='calm and concrete' if strategy=='withhold_expression' else plan.tone,
                   behaviors=tuple(behaviors[:4]),
                   playfulness=0. if task_serious else plan.playfulness)
    return decision,plan
