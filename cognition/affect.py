"""Deterministic appraisal dynamics. Coefficients are explicit engineering hypotheses.

Separate episode causes survive aggregation. Slow mood is integrated analytically;
sampling the same quiet interval more often must not create extra emotion.
"""
import math
from dataclasses import replace
from datetime import datetime

from cognition.types import (CognitiveEvent, CognitiveState, DEFAULT_GOALS, EmotionEpisode,
                             ExpressionPlan, Goal, Origin, Perception, Temperament, utc)

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
    mv = base_v + (state.mood_valence_latent - base_v) * em
    ma = base_a + (state.mood_arousal_latent - base_a) * em
    episodes = []
    for ep in state.episodes:
        v, a, _ = EMOTIONS[ep.emotion]
        ee = math.exp(-dt / ep.tau_seconds)
        rate = 1 / tau - 1 / ep.tau_seconds
        kernel = dt * em / tau if abs(rate) < 1e-12 else (ee - em) / (tau * rate)
        mv += .2 * v * ep.intensity * kernel
        ma += .2 * a * ep.intensity * kernel
        episodes.append(replace(ep, intensity=ep.intensity * ee))
    return replace(state, last_at=at, episodes=tuple(episodes),
                   mood_valence_latent=mv, mood_arousal_latent=ma)


def affect(state: CognitiveState, temperament=Temperament()) -> dict:
    v = math.atanh(temperament.baseline_valence)
    a = math.atanh(temperament.baseline_arousal)
    for ep in state.episodes:
        ev, ea, _ = EMOTIONS[ep.emotion]
        v += ev * ep.intensity
        a += ea * ep.intensity
    return dict(valence=math.tanh(v), arousal=math.tanh(a),
                mood_valence=math.tanh(state.mood_valence_latent),
                mood_arousal=math.tanh(state.mood_arousal_latent))


def appraise(state: CognitiveState, event: CognitiveEvent, perception: Perception,
             goals: tuple[Goal, ...] = DEFAULT_GOALS, temperament=Temperament()) -> CognitiveState:
    if state.context != event.context:
        raise ValueError('Context mismatch')
    perception.validate_for(event, goals)
    # Duplicate input is a no-op including its clock and revision.
    if event.evidence.independent_group in state.applied_groups:
        return state
    state = advance(state, event.observed_at, temperament)
    # A failed service or the agent's own delivery is not external evidence about a user.
    if event.evidence.origin in (Origin.SYSTEM, Origin.DELIVERED_ACTION, Origin.REPLAY):
        return replace(state, revision=state.revision + 1,
                       applied_groups=state.applied_groups | {event.evidence.independent_group})
    priorities = {g.id: g.priority for g in goals}
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
            'surprise': p.novelty * .45,
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
    # Do not silently delete old causes to satisfy an engineering quota.
    # Repository maintenance archives negligible pulses separately before the cap.
    episodes = state.episodes + tuple(new)
    if len(episodes) > 64:
        raise ValueError('Active episode budget exceeded; archive resolved pulses first')
    return replace(state, episodes=episodes, revision=state.revision + 1,
                   applied_groups=state.applied_groups | {event.evidence.independent_group})


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
    return ExpressionPlan(regulation, tone, .35 if negative else .65,
                          .8 if task_serious or negative else .5,
                          0. if task_serious or negative else .15, .3 * (1 - temperament.expression_reserve),
                          sticker, 'measured' if negative or task_serious else 'conversational',
                          tuple(dict.fromkeys(e.cause_id for e in strong)), uncertain)
