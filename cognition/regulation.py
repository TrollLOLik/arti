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
    if situation and situation.revisions:
        strategy = 'reappraise'
    elif situation and situation.intention_evidence == 'ambiguous' and situation.kind == 'conflict':
        strategy = 'clarify'
    elif situation and situation.kind == 'threat':
        strategy = 'problem_solve'
    elif situation and any(i['status']=='reminder' and not i['deadline'] for i in situation.intentions):
        strategy = 'clarify'
    elif task_serious and any(e.emotion=='anger' and e.intensity>.04 for e in state.episodes):
        strategy = 'withhold_expression'
    elif state.effort_load>.8:
        strategy = 'attention_shift'
    decision = RegulationDecision(strategy,dict(clarify='resolve uncertainty',reappraise='revise supported interpretation',
        problem_solve='address the actual cause',withhold_expression='maintain useful delivery',
        attention_shift='recover attention',acknowledge='recognize the experience')[strategy],
        plan.cause_ids,strategy=='reappraise',strategy in ('clarify','problem_solve'))
    preferences = preferences or {}
    sticker = plan.sticker_mood
    if preferences.get('stickers') is False or strategy in ('clarify','withhold_expression','attention_shift'):
        sticker = None
    plan = replace(plan,regulation=strategy,sticker_mood=sticker,
                   tone='calm and concrete' if strategy=='withhold_expression' else plan.tone,
                   playfulness=0. if task_serious else plan.playfulness)
    return decision,plan
