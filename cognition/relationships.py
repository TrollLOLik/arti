"""Independent social dimensions with bounded evidence and outcome learning."""
import math
from datetime import datetime

DIMENSIONS = ('familiarity','warmth','reliability','benevolence','predictability','openness')


def initial_relationship():
    return dict(dimensions={k:dict(alpha=1.,beta=1.) for k in DIMENSIONS},
                groups=[],preferences={},associations={},last_at=None,expectations={})


def relationship_transition(previous, event, situation):
    import copy
    result = copy.deepcopy(previous or initial_relationship())
    group = event.evidence.independent_group
    if event.evidence.origin.value != 'user' or group in result['groups']:
        return result
    result['groups'].append(group)
    # Contact supplies familiarity, never reliability. Habituation bounds dense noise.
    last = datetime.fromisoformat(result['last_at']) if result['last_at'] else None
    spacing = 1. if last is None else min(1.,max(0.,(event.observed_at-last).total_seconds()) / 3600)
    changes = {'familiarity':(.03 + .07 * spacing,0.)}
    if situation.modality == 'interaction' and event.event_kind!='reaction' and situation.social_signal_actor in (None,event.actor_id):
        signal = situation.social_signal
        changes.update({
            'care': {'warmth':(.25,0.),'benevolence':(.25,0.),'openness':(.1,0.)},
            'cooperation': {'benevolence':(.2,0.),'predictability':(.1,0.)},
            'fulfilled': {'reliability':(.35,0.),'predictability':(.2,0.)},
            'breach': {'reliability':(0.,.3),'predictability':(0.,.15)},
            'insult': {'benevolence':(0.,.15),'openness':(0.,.1)},
            'apology': {'warmth':(.08,0.),'openness':(.08,0.)},
        }.get(signal,{}))
        if situation.intention_evidence != 'explicit' and signal == 'insult':
            changes = {'familiarity':changes['familiarity']}
    for name,(positive,negative) in changes.items():
        d = result['dimensions'][name]
        # Preserve mean while limiting effective evidence strength per dimension.
        mass = d['alpha'] + d['beta']
        if mass > 24:
            d['alpha'] *= 24 / mass
            d['beta'] *= 24 / mass
        d['alpha'] += positive
        d['beta'] += negative
    result['preferences'].update(situation.preferences)
    cue = situation.topic.casefold().strip()
    if cue and situation.modality == 'interaction' and situation.social_signal != 'contact':
        association = result['associations'].get(cue,dict(strength=0.,groups=[]))
        direction = -1 if situation.social_signal in ('insult','breach') else 1
        association['strength'] = max(-1.,min(1.,association['strength'] + .08 * direction))
        association['groups'].append(group)
        result['associations'][cue] = association
        result['associations'] = dict(list(result['associations'].items())[-64:])
    result['last_at'] = event.observed_at.isoformat()
    # Lifetime idempotence lives in cognitive_projection_effects. These bounded
    # working ledgers describe current associations rather than duplicating SQL.
    result['groups'] = result['groups'][-512:]
    for association in result['associations'].values():
        association['groups'] = association['groups'][-128:]
    return result


def relationship_view(model, at):
    model = model or initial_relationship()
    last = datetime.fromisoformat(model['last_at']) if model['last_at'] else at
    age = max(0.,(at-last).total_seconds())
    view = {}
    for name,d in model['dimensions'].items():
        mass = d['alpha'] + d['beta']
        view[name] = dict(value=d['alpha']/mass,
                          confidence=(1 - 2 / mass) * math.exp(-age/(180*86400)))
    # Silence lowers currency, never adds a negative observation.
    return dict(dimensions=view,preferences=model['preferences'],associations=model['associations'])
