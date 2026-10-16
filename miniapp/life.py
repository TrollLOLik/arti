"""«Сейчас у Арти» and «Комната Арти» for the Mini App (H6, P2).

Three kinds of data, kept apart and labelled apart by the client:
- her in-character life (cognition.pursuits.room): global declared fiction,
  the same for everyone; never returned to a person who chose «Не упоминать»,
  and then the pursuit tables are not even read;
- the latest real reading note with its link (H4c);
- her mood in this person's own private context only, labelled from a fixed
  Russian dictionary. Null when there is no state yet, the context is not
  live, or an open rupture with this person lasts: a grudge is never shown.
"""
from cognition import life_settings

PERSONA = 'arti'
# Fixed words only: the label never carries generated or personal text.
EMOTION_WORDS = {
    'joy': 'с радостью', 'gratitude': 'с благодарностью', 'pride': 'с гордостью', 'interest': 'с интересом',
    'surprise': 'с удивлением', 'sadness': 'с грустью', 'indignation': 'на вашей стороне',
    'fear': 'с тревогой', 'disappointment': 'с разочарованием', 'guilt': 'с чувством вины',
    'embarrassment': 'со смущением', 'amusement': 'с улыбкой', 'relief': 'с облегчением',
    'admiration': 'с восхищением', 'tenderness': 'с нежностью', 'boredom': 'с лёгкой скукой',
}
# Anger aimed at this person is the hurt itself; like a rupture, it is not shown.
HIDDEN_EMOTIONS = frozenset({'anger'})


def tone_word(valence, arousal):
    """Her slow mood on two axes, in one fixed word."""
    if valence >= .2:
        return 'приподнятое' if arousal >= .35 else 'ясное'
    if valence <= -.12:
        return 'беспокойное' if arousal >= .35 else 'тихое'
    return 'оживлённое' if arousal >= .35 else 'ровное'


async def mood(conn, user_id, now):
    """Her mood in the private conversation with this person, or None."""
    from cognition.affect import advance, affect, expression
    from cognition.memory_repository import key
    from cognition.relationships import RUPTURE_CLOSED, rupture_severity
    from cognition.serialization import load_state, object_value
    from cognition.types import utc

    context = await conn.fetchrow('''SELECT id,suppression_epoch FROM cognitive_contexts
        WHERE persona_id=$1 AND chat_id=$2 AND mode='default' AND scene_id='' AND topic_id=-1
        AND authority='active' AND NOT rebuilding''', PERSONA, user_id)
    if context is None:
        return None

    async def artifact(kind):
        value = await conn.fetchval('''SELECT payload FROM cognitive_artifacts WHERE context_id=$1 AND artifact_key=$2
            AND owner_id=$3 AND kind=$4 AND suppressed_at IS NULL AND projection_epoch=$5''',
            context['id'], key(kind, user_id), user_id, kind, context['suppression_epoch'])
        return object_value(value) if value is not None else None

    now = utc(now)
    relationship = await artifact('relationship')
    if relationship and rupture_severity(relationship.get('rupture'), now) >= RUPTURE_CLOSED:
        return None
    cached = await artifact('personal_state')
    if not cached:
        return None
    try:
        state = load_state(cached['state'])
    except (KeyError, TypeError, ValueError):
        # Another model version or a damaged projection: say nothing rather than guess.
        return None
    state = advance(state, max(state.last_at, now))
    plan = expression(state)
    if plan.dominant in HIDDEN_EMOTIONS:
        return None
    values = affect(state)
    valence, arousal = values['mood_valence'], values['mood_arousal']
    label = tone_word(valence, arousal)
    if plan.dominant in EMOTION_WORDS:
        label += ', ' + EMOTION_WORDS[plan.dominant]
    return {'label': label, 'emotion': plan.dominant if plan.dominant in EMOTION_WORDS else None,
            'valence': round(valence, 1), 'arousal': round(arousal, 1)}


async def reading(conn, now):
    from cognition.reading import recent_notes
    notes = await recent_notes(None, now, limit=1, conn=conn)
    if not notes:
        return None
    note = notes[0]
    return {'title': note['title'], 'url': note['url'], 'opinion': note['opinion'],
            'read_on': note['read_on'].isoformat(), 'at': note['created_at'].isoformat()}


async def view(pool, user_id, now):
    """Everything the strip and the room show to this person."""
    from cognition import pursuits
    async with pool.acquire() as conn:
        settings = await life_settings.read(conn, user_id)
        in_character = settings['mentions'] and pursuits.enabled()
        room = (await pursuits.room(pool, now, conn=conn) if in_character
                else {'pursuits': [], 'embroidery': None, 'tastes': []})
        return dict(settings=settings, in_character=in_character, **room,
                    reading=await reading(conn, now), mood=await mood(conn, user_id, now))


def empty_view(settings):
    """Local preview: no database, no life, no mood."""
    return dict(settings=settings, in_character=False, pursuits=[], embroidery=None, tastes=[], reading=None, mood=None)


def settings_change(body):
    """Validated PUT body: {mentions?: bool, explainer_seen?: bool} or {reset: true}."""
    if not body or set(body) - {'mentions', 'explainer_seen', 'reset'}:
        raise ValueError('invalid_settings')
    if 'reset' in body:
        if body['reset'] is not True or len(body) > 1:
            raise ValueError('invalid_settings')
        return None
    if any(type(value) is not bool for value in body.values()):
        raise ValueError('invalid_settings')
    return body
