"""Deterministic appraisal dynamics. Coefficients are explicit engineering hypotheses.

Separate episode causes survive aggregation. Slow mood is integrated analytically;
sampling the same quiet interval more often must not create extra emotion.
"""
import math
from dataclasses import replace
from datetime import datetime

from cognition.types import (CognitiveEvent, CognitiveState, DEFAULT_GOALS, EmotionEpisode,
                             ExpressionPlan, Goal, Origin, Perception, Temperament, utc,AffectiveResidue)

# Valence, activation, pulse time constant. These are configurable model priors,
# not measurements of human neurobiology.
EMOTIONS = {
    'joy': (1., .5, 1200.), 'gratitude': (.8, .25, 3600.),
    'pride': (.7, .4, 2400.), 'interest': (.35, .35, 900.),
    'surprise': (0., .8, 240.), 'sadness': (-.85, .2, 7200.),
    'anger': (-.7, .85, 1800.), 'fear': (-.8, .8, 2400.),
    'disappointment': (-.55, .25, 3600.), 'guilt': (-.6, .45, 3600.),
    'embarrassment': (-.45, .65, 900.),
}


def initial_state(context, at: datetime, temperament=Temperament()) -> CognitiveState:
    return CognitiveState(context, utc(at), temperament_version=temperament.version,
                          mood_valence_latent=math.atanh(temperament.baseline_valence),
                          mood_arousal_latent=math.atanh(temperament.baseline_arousal))


def advance(state: CognitiveState, at: datetime, temperament=Temperament()) -> CognitiveState:
    at = utc(at)
    if temperament.version != state.temperament_version:
        raise ValueError('Temperament migrations must be explicit')
    dt = (at - utc(state.last_at)).total_seconds()
    if dt < 0:
        raise ValueError('Out-of-order time; replay observations in their original order')
    if not dt:
        return state
    tau = temperament.mood_tau_seconds
    em = math.exp(-dt / tau)
    base_v = math.atanh(temperament.baseline_valence)
    base_a = math.atanh(temperament.baseline_arousal)
    sustained = min(.12,sum(c['weight'] for c in state.concerns if c['status']=='open'))
    base_v -= sustained
    base_a += sustained*.35
    mv = base_v + (state.mood_valence_latent - base_v) * em
    ma = base_a + (state.mood_arousal_latent - base_a) * em
    episodes = []
    residues = []
    for ep in state.episodes+state.residues:
        v, a, _ = EMOTIONS[ep.emotion]
        ee = math.exp(-dt / ep.tau_seconds)
        rate = 1 / tau - 1 / ep.tau_seconds
        kernel = dt * em / tau if abs(rate) < 1e-12 else (ee - em) / (tau * rate)
        mv += .2 * v * ep.intensity * kernel
        ma += .2 * a * ep.intensity * kernel
        (residues if isinstance(ep,AffectiveResidue) else episodes).append(replace(ep, intensity=ep.intensity * ee))
    return replace(state, last_at=at, episodes=tuple(episodes),
                   residues=tuple(residues),
                   mood_valence_latent=mv, mood_arousal_latent=ma,
                   effort_load=state.effort_load * math.exp(-dt / 5400))


def affect(state: CognitiveState, temperament=Temperament()) -> dict:
    v = math.atanh(temperament.baseline_valence)
    a = math.atanh(temperament.baseline_arousal)
    for ep in state.episodes+state.residues:
        ev, ea, _ = EMOTIONS[ep.emotion]
        v += ev * ep.intensity
        a += ea * ep.intensity
    hour = state.last_at.hour+state.last_at.minute/60+state.last_at.second/3600
    circadian = .06 * math.cos(2 * math.pi * (hour - 15) / 24)
    return dict(valence=math.tanh(v), arousal=math.tanh(a),
                mood_valence=math.tanh(state.mood_valence_latent),
                mood_arousal=math.tanh(state.mood_arousal_latent),
                attention=min(1.,max(.4,1 - .5 * state.effort_load+circadian)),
                circadian=circadian)


def appraise(state: CognitiveState, event: CognitiveEvent, perception: Perception,
             goals: tuple[Goal, ...] = None, temperament=Temperament()) -> CognitiveState:
    if state.context != event.context:
        raise ValueError('Context mismatch')
    goals = goals or available_goals(state,event.actor_id)
    perception.validate_for(event, goals)
    from cognition.situations import calibrated_appraisals
    perception = calibrated_appraisals(perception)
    # Duplicate input is a no-op including its clock and revision.
    if event.evidence.independent_group in state.applied_groups:
        return state
    state = advance(state, event.observed_at, temperament)
    # A failed service or the agent's own delivery is not external evidence about a user.
    if event.evidence.origin in (Origin.SYSTEM, Origin.DELIVERED_ACTION, Origin.REPLAY) or event.event_kind=='historical':
        dynamic = updated_goals(state,event,perception.situation) if event.evidence.origin==Origin.DELIVERED_ACTION else state.situational_goals
        return replace(state, revision=state.revision + 1,
                       situational_goals=dynamic,
                       applied_groups=state.applied_groups | {event.evidence.independent_group})
    priorities = {g.id: g.priority for g in goals}
    expectations = {g.id:g.expectation for g in goals if g.expectation is not None}
    pulses = {}
    confidence = {}
    for p in perception.appraisals:
        weight = p.probability * p.relevance * p.confidence * priorities[p.goal_id]
        weight *= temperament.reactivity
        pos, neg = max(p.congruence, 0), max(-p.congruence, 0)
        values = {
            'joy': pos * p.outcome_probability,
            'gratitude': pos * p.agency_other * p.intentionality,
            'pride': pos * p.agency_self,
            'interest': p.novelty * (1 - p.future_threat) * (1 - p.norm_violation) * .4,
            'surprise': max(p.novelty,abs(p.outcome_probability-expectations[p.goal_id]) if p.goal_id in expectations else 0.) * .45,
            'sadness': neg * p.loss * (.4 + .6 * p.irreversibility),
            'anger': neg * p.agency_other * p.intentionality * p.norm_violation,
            'fear': neg * p.future_threat * (.4 + .6 * (1 - p.control)),
            'disappointment': neg * (1 - p.future_threat) * (1 - p.loss) * .6,
            'guilt': neg * p.agency_self * p.norm_violation,
            'embarrassment': neg * p.agency_self * p.social_exposure,
        }
        for emotion, value in values.items():
            key = p.goal_id, p.target_id, emotion
            pulses[key] = pulses.get(key, 0) + weight * value
            confidence[key] = max(confidence.get(key, 0), p.confidence * p.probability)
    # Recall cues have a bounded emotional impact and never count as new learning.
    total = sum(pulses.values())
    budget = .08 if event.evidence.origin == Origin.RECALL else .85
    if perception.situation:
        s = perception.situation
        budget = min(budget,{'preference':0.,'neutral':.12,'request':.05,'clarification':.2}.get(s.kind,.85))
        if s.modality=='reported' and s.kind=='conflict':
            budget = min(budget,.45)
        if s.kind=='conflict' and s.outcome=='unknown' and s.intention_evidence!='explicit':
            budget = min(budget,.12 if s.modality=='interaction' else .45)
        if s.social_signal=='apology':
            budget = min(budget,.25)
    scale = min(1., budget / total) if total else 1.
    new = []
    for (goal_id, target, emotion), intensity in sorted(pulses.items(), key=lambda x: (x[0][0], str(x[0][1]), x[0][2])):
        intensity *= scale
        if intensity < .0001:
            continue
        new.append(EmotionEpisode(f'{event.event_id}:{goal_id}:{target}:{emotion}', event.event_id,
                                  event.evidence.independent_group, goal_id, target, emotion,
                                  intensity, confidence[(goal_id, target, emotion)],
                                  utc(event.observed_at), EMOTIONS[emotion][2]))
    # Historical causes remain in the event/perception ledger. Resolved pulses
    # leave the working snapshot at event boundaries, not on clock polling.
    resolved = tuple(e.id for e in state.episodes if e.intensity < .005)
    episodes = tuple(e for e in state.episodes if e.intensity >= .005) + tuple(new)
    # Keep addressed recent causes in working memory. The event ledger retains
    # the full causal history; the exact exponential tail still affects mood.
    overflow = episodes[:-128] if len(episodes)>128 else ()
    episodes = episodes[-128:]
    residues = {r.emotion:r for r in state.residues}
    for ep in overflow:
        old = residues.get(ep.emotion)
        residues[ep.emotion] = AffectiveResidue(ep.emotion,ep.intensity+(old.intensity if old else 0.),ep.tau_seconds,(old.cause_count if old else 0)+1)
    dynamic = {g['id']:dict(g) for g in state.situational_goals}
    concerns = {c['source_id']:dict(c) for c in state.concerns}
    if perception.situation:
        s = perception.situation
        dynamic = {g['id']:g for g in updated_goals(state,event,s)}
        if s.kind in ('loss','threat','conflict') and s.outcome!='resolved':
            certainty = max((a.confidence*a.relevance for a in perception.appraisals),default=0.)
            concerns[event.evidence.source_id] = dict(source_id=event.evidence.source_id,owner=event.actor_id,
                weight=min(.04,.04*certainty),status='open',since=event.observed_at.isoformat())
        for revision in s.revisions:
            if revision['source_id'] in concerns and s.outcome=='resolved':
                concerns[revision['source_id']]['status'] = 'resolved'
    return replace(state, episodes=episodes, revision=state.revision + 1,
                   residues=tuple(residues.values()),
                   archived_episode_ids=(state.archived_episode_ids + resolved)[-128:],
                   situational_goals=tuple(list(dynamic.values())[-32:]),
                   concerns=tuple(list(concerns.values())[-128:]),
                   effort_load=min(1.,state.effort_load + (.025 if perception.appraisals else .005)),
                   applied_groups=state.applied_groups | {event.evidence.independent_group})


def updated_goals(state,event,situation):
    import hashlib,json
    dynamic = {g['id']:dict(g) for g in state.situational_goals}
    for intention in situation.intentions if situation else ():
        actor = intention.get('actor','arti' if event.evidence.origin==Origin.DELIVERED_ACTION else event.actor_id)
        parts = [event.evidence.owner_id,'arti',intention['key']] if actor=='arti' else [actor,intention['key']]
        identity = 'goal:' + hashlib.sha256(json.dumps(parts,ensure_ascii=False).encode()).hexdigest()[:40]
        previous = dynamic.get(identity,{})
        expectation = previous.get('expectation',.5)
        if intention['status'] in ('fulfilled','cancelled') and previous.get('status') in ('open','reminder'):
            outcome = float(intention['status']=='fulfilled')
            expectation += .2*(outcome-expectation)
        dynamic[identity] = dict(id=identity,owner=event.evidence.owner_id,actor=actor,description=intention['description'],priority=.7,
            status=intention['status'],source_group=event.evidence.independent_group,expectation=expectation)
    return tuple(list(dynamic.values())[-32:])


def available_goals(state,owner):
    return DEFAULT_GOALS + tuple(Goal(g['id'],g['description'],g['priority'],g.get('expectation',.5),g.get('source_group')) for g in state.situational_goals
                                 if g['owner']==owner and g['status'] in ('open','reminder'))


def replay_ledger(context, entries, empty_at):
    """Replay the shared accumulator using each owner's independent goal registry."""
    state = initial_state(context, entries[0][0].observed_at if entries else empty_at)
    personal = {}
    for event, perception in entries:
        owner = event.evidence.owner_id
        before = personal.get(owner) or initial_state(context,event.observed_at)
        goals = available_goals(before,owner)
        state = replace(appraise(state,event,perception,goals=goals),applied_groups=frozenset())
        personal[owner] = replace(appraise(before,event,perception,goals=goals),applied_groups=frozenset())
    return state


def expression(state: CognitiveState, *, task_serious: bool = False,
               temperament=Temperament()) -> ExpressionPlan:
    active = sorted((e for e in state.episodes if e.intensity >= .025),
                    key=lambda e: (-e.intensity, e.id))
    strong = active[:3]
    uncertain = any(e.confidence < .6 and EMOTIONS[e.emotion][0] < 0 for e in strong)
    dominant = strong[0].emotion if strong else 'neutral'
    tone = {'anger': 'firm and measured', 'fear': 'careful and attentive', 'sadness': 'gentle and quiet',
            'disappointment': 'calm and candid', 'guilt': 'responsible and concrete',
            'embarrassment': 'reserved', 'joy': 'warm', 'gratitude': 'warm and appreciative',
            'interest': 'curious', 'surprise': 'attentive', 'pride': 'pleased'}.get(dominant, 'calm')
    regulation = 'clarify' if uncertain else ('problem_solve' if task_serious or dominant in ('fear', 'guilt') else 'acknowledge')
    negative = dominant in ('anger', 'fear', 'sadness', 'disappointment', 'guilt', 'embarrassment')
    sticker = {'joy': 'happy', 'gratitude': 'happy', 'sadness': 'sad', 'anger': 'angry',
               'interest': 'thinking', 'surprise': 'thinking', 'pride': 'happy'}.get(dominant)
    if task_serious or uncertain:
        sticker = None
    slow = affect(state)['mood_valence']
    if not strong and slow<-.12:
        tone = 'quiet and measured'
    if state.effort_load>.8:
        tone = 'concise and attentive'
    return ExpressionPlan(regulation, tone, max(0.,min(1.,(.35 if negative else .65)+slow*.08)),
                          .8 if task_serious or negative else .5,
                          0. if task_serious or negative else .15, .3 * (1 - temperament.expression_reserve),
                          sticker, 'measured' if negative or task_serious else 'conversational',
                          tuple(dict.fromkeys(e.cause_id for e in strong)), uncertain)
