"""Deterministic appraisal dynamics. Coefficients are explicit engineering hypotheses.

Separate episode causes survive aggregation. Slow mood is integrated analytically;
sampling the same quiet interval more often must not create extra emotion.
"""
import math
from dataclasses import replace
from datetime import datetime

from cognition.types import (CognitiveEvent, CognitiveState, DEFAULT_GOALS, EmotionEpisode,
                             ExpressionPlan, Goal, Origin, Perception, Temperament, utc,AffectiveResidue,
                             LIGHT_HUMOUR_THRESHOLD, OWN_GOALS)

# Valence, activation, pulse time constant. These are configurable model priors,
# not measurements of human neurobiology.
EMOTIONS = {
    'joy': (1., .5, 1200.), 'gratitude': (.8, .25, 3600.),
    'pride': (.7, .4, 2400.), 'interest': (.35, .35, 900.),
    'surprise': (0., .8, 240.), 'sadness': (-.85, .2, 7200.),
    'anger': (-.7, .85, 1800.), 'fear': (-.8, .8, 2400.),
    'disappointment': (-.55, .25, 3600.), 'guilt': (-.6, .45, 3600.),
    'embarrassment': (-.45, .65, 900.),
    # H4a (E4). Activation stays non-negative: slow arousal is a non-negative latent.
    'amusement': (.8, .55, 900.), 'relief': (.6, .05, 1800.),
    'admiration': (.7, .35, 2400.), 'tenderness': (.75, .1, 3600.),
    'boredom': (-.3, .02, 1800.),
}
# The character's own goals get their own bounded budget per event (H4a, E3):
# an interesting idea may move her even when it is an ordinary request for the user.
OWN_GOAL_BUDGET = .3
# Boredom is mild and changes the topic, not the warmth or seriousness of a reply.
DETACHED_EMOTIONS = frozenset({'boredom'})

# A bounded display prior, not a change to versioned appraisal dynamics.
# Repeated praise or wins should not force escalating exuberance or hide grief.
EXPRESSIVE_POSITIVE_CAP = .85


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
        mv += temperament.mood_transfer * v * ep.intensity * kernel
        ma += temperament.mood_transfer * a * ep.intensity * kernel
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
    s = perception.situation
    # Relief needs an actual resolution of something threatening or disputed.
    resolved = bool(s and s.outcome=='resolved' and (s.kind in ('threat','conflict') or s.revisions))
    pulses = {}
    confidence = {}
    for p in perception.appraisals:
        weight = p.probability * p.relevance * p.confidence * priorities[p.goal_id]
        weight *= temperament.reactivity
        pos, neg = max(p.congruence, 0), max(-p.congruence, 0)
        # The other person's achievement for their own good is admired, not a favour to thank for.
        extended = temperament.extended_palette
        achievement = extended and p.goal_id == 'user_wellbeing'
        curiosity = extended and p.goal_id == 'curiosity'
        values = {
            # Curiosity is satisfied as interest rather than as general joy.
            'joy': pos * p.outcome_probability * (.5 if curiosity else 1.),
            'gratitude': 0. if achievement else pos * p.agency_other * p.intentionality,
            'pride': pos * p.agency_self,
            'interest': p.novelty * (1 - p.future_threat) * (1 - p.norm_violation) * (.8 if curiosity else .4),
            'surprise': max(p.novelty,abs(p.outcome_probability-expectations[p.goal_id]) if p.goal_id in expectations else 0.) * .45,
            'sadness': neg * p.loss * (.4 + .6 * p.irreversibility),
            'anger': neg * p.agency_other * p.intentionality * p.norm_violation,
            'fear': neg * p.future_threat * (.4 + .6 * (1 - p.control)),
            'disappointment': 0. if curiosity else neg * (1 - p.future_threat) * (1 - p.loss) * .6,
            'guilt': neg * p.agency_self * p.norm_violation,
            'embarrassment': neg * p.agency_self * p.social_exposure,
            # H4a (E4). Mockery is a norm violation, never something funny.
            'amusement': (p.humor * max(0., 1 - 2 * p.norm_violation) * (1 - p.future_threat) * (.5 + .5 * pos)
                          if extended else 0.),
            'relief': pos * (1 - p.future_threat) * .6 if extended and resolved else 0.,
            'admiration': pos * p.agency_other * p.outcome_probability * .5 if achievement else 0.,
            'tenderness': (pos * p.agency_other * p.intentionality * (1 - p.novelty) * .5
                           if extended and p.goal_id == 'connection' else 0.),
            'boredom': neg * (1 - p.novelty) * (1 - p.loss) * (1 - p.future_threat) if curiosity else 0.,
        }
        for emotion, value in values.items():
            key = p.goal_id, p.target_id, emotion
            pulses[key] = pulses.get(key, 0) + weight * value
            confidence[key] = max(confidence.get(key, 0), p.confidence * p.probability)
    # Recall cues have a bounded emotional impact and never count as new learning.
    # Safety caps bind every goal: recall, reported or unclear conflict, apology.
    cap = .08 if event.evidence.origin == Origin.RECALL else .85
    kind_budget = cap
    if s:
        kind_budget = {'preference':0.,'neutral':.12,'request':.05,'clarification':.2}.get(s.kind,.85)
        if s.modality=='reported' and s.kind=='conflict':
            cap = min(cap,.45)
        if s.kind=='conflict' and s.outcome=='unknown' and s.intention_evidence!='explicit':
            cap = min(cap,.12 if s.modality=='interaction' else .45)
        if s.social_signal=='apology':
            cap = min(cap,.25)
    own_budget = OWN_GOAL_BUDGET if temperament.extended_palette else kind_budget
    budgets = {False: min(cap,kind_budget), True: min(cap,own_budget if not s or s.kind!='preference' else 0.)}
    totals = {own: sum(v for (g,_,_),v in pulses.items() if (g in OWN_GOALS)==own) for own in (False,True)}
    scales = {own: min(1., budgets[own] / totals[own]) if totals[own] else 1. for own in (False,True)}
    new = []
    for (goal_id, target, emotion), intensity in sorted(pulses.items(), key=lambda x: (x[0][0], str(x[0][1]), x[0][2])):
        intensity *= scales[goal_id in OWN_GOALS]
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
    # Affect is computed before presentation truncation. Small independent pulses
    # and compacted tails can collectively matter even when none is in the top six.
    working = state.episodes + state.residues
    positive = sum(e.intensity for e in working if EMOTIONS[e.emotion][0] > .1)
    negative_weight = sum(e.intensity for e in working if EMOTIONS[e.emotion][0] < -.1
                          and e.emotion not in DETACHED_EMOTIONS)
    # Expression has finite reserve even if many equivalent positive causes have
    # accumulated. This deliberately leaves persisted pulses/mood unchanged: true
    # numerical habituation requires a new model version and a ledger migration.
    positive = min(positive,EXPRESSIVE_POSITIVE_CAP)
    total = positive + negative_weight
    mixed = bool(total >= .05 and min(positive,negative_weight) / total >= .18)
    active = sorted((e for e in state.episodes if e.intensity >= .005),
                    key=lambda e: (-e.intensity, e.id))
    # Bound explanatory causes, not the affect calculation. Ensure both sides of
    # a mixture survive the display cap, and do not spend six slots on one cause.
    cause_weights = {}
    for ep in active:
        cause_weights[ep.cause_id] = cause_weights.get(ep.cause_id,0.) + ep.intensity
    selected = []
    if mixed:
        for sign in (1,-1):
            representative = next((e for e in active if EMOTIONS[e.emotion][0] * sign > .1),None)
            if representative and representative.cause_id not in selected:
                selected.append(representative.cause_id)
    for cause in sorted(cause_weights,key=lambda c: (-cause_weights[c],c)):
        if cause not in selected:
            selected.append(cause)
    # This fallback describes affect without a current situation. regulate()
    # replaces it with current grounded uncertainty before selecting an action.
    uncertain = any(e.confidence < .6 and e.intensity >= .025 and EMOTIONS[e.emotion][0] < 0
                    for e in active)
    negative = mixed or (negative_weight >= .025 and negative_weight > positive)
    dominant,dominant_intensity = dominant_emotion(active)
    # H5 (R4): her own view was dismissed or misread — she may hold her ground.
    stand_ground = any(e.goal_id in ('autonomy','being_understood') and e.intensity >= .02
                       and EMOTIONS[e.emotion][0] < 0 for e in active)
    if mixed: tone = 'warm but measured'
    # Anger addressed to the person sharpens the voice (H3, E1); other negative
    # states, including indignation on the person's behalf, stay gentle.
    elif negative: tone = 'dry and precise' if dominant=='anger' else 'gentle and concrete'
    elif positive >= .025: tone = 'warm and attentive'
    else: tone = 'calm'
    regulation = 'clarify' if uncertain else 'problem_solve' if task_serious else 'acknowledge'
    clear_positive = positive >= .025 and negative_weight < .025 and not mixed
    sticker = 'happy' if clear_positive and not task_serious and not uncertain else None
    slow = affect(state,temperament)['mood_valence']
    if total < .025 and slow < -.12: tone = 'quiet and measured'
    if state.effort_load > .8: tone = 'concise and attentive'
    behaviors = ('ask_one_question',) if uncertain else ('answer_task',) if task_serious else ('listen',)
    playful = clear_positive and not task_serious and not uncertain and state.effort_load <= .8

    return ExpressionPlan(regulation, tone, max(0.,min(1.,(.35 if negative else .65)+slow*.08)),
                          .8 if task_serious or negative else .5,
                          LIGHT_HUMOUR_THRESHOLD + .05 if playful else 0.,
                          .3 * (1 - temperament.expression_reserve),
                          sticker, 'measured' if negative or task_serious else 'conversational',
                          tuple(selected[:6]), uncertain, behaviors, mixed,
                          dominant=dominant, dominant_intensity=dominant_intensity, stand_ground=stand_ground)


def dominant_emotion(active):
    """Strongest felt emotion among working episodes, order-independent.

    Anger without an observed target (for example at a third party in the
    person's story) is indignation on the person's behalf, not anger at them.
    """
    from cognition.types import DOMINANT_THRESHOLD
    totals = {}
    for ep in active:
        name = 'indignation' if ep.emotion=='anger' and ep.target_id is None else ep.emotion
        totals[name] = totals.get(name,0.) + ep.intensity
    if not totals:
        return None,0.
    name = min(totals,key=lambda e:(-totals[e],e))
    return (name,totals[name]) if totals[name] >= DOMINANT_THRESHOLD else (None,0.)
