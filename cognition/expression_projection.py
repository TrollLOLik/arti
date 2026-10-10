"""Transport projections of an expression plan: sticker class and reaction (H3, E5, I5).

Deterministic per event, so a retried request repeats the same choice. Neither
changes cognitive state; sending still passes the ordinary delivery fences.
Rates keep stickers and reactions occasional rather than a reflex.
"""
import hashlib

from cognition.types import LIGHT_HUMOUR_THRESHOLD

STICKER_RATE = {'new': .35, 'familiar': .5, 'close': .6}
REACTION_RATE = .5


def _unit(event_id, salt):
    return int.from_bytes(hashlib.sha256(f'{salt}:{event_id}'.encode()).digest()[:4], 'big') / 2**32


def sticker_mood(plan, *, social=None, event_id=''):
    """Refine a sticker regulate() already allowed (clear, non-sensitive, wanted)."""
    if plan.sticker_mood is None or _unit(event_id, 'sticker') >= STICKER_RATE.get(plan.stage, .4):
        return None
    if social == 'care':
        return 'love' if plan.stage == 'close' and plan.warmth >= .7 else 'blush'
    if plan.dominant == 'interest':
        return 'thinking'
    if plan.dominant == 'surprise':
        return 'shock'
    if (plan.playfulness >= LIGHT_HUMOUR_THRESHOLD and plan.stage in ('familiar', 'close')
            and _unit(event_id, 'tease') < .4):
        return 'teasing'
    return 'happy'


def reaction(plan, *, kind=None, social=None, label=None, private=True, event_id=''):
    """An emoji on the person's message in a private chat, never on a sensitive turn."""
    if not private or plan.serious or plan.sticker_mood is not None or _unit(event_id, 'reaction') >= REACTION_RATE:
        return None
    if social == 'care' or plan.dominant == 'gratitude':
        return '❤'
    if label == 'amusement':
        return '😁'
    if kind == 'success' or plan.dominant == 'pride':
        return '🔥'
    return None
