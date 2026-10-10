"""Human conversational rhythm for private chats (H3: R1, R2, I5).

Pure functions: how long to wait for the person's next fragment, how to pace
and split a conversational reply, and when a pure acknowledgement deserves a
reaction instead of a reply. Delays never stack on top of slow generation:
the first pause only fills the time a person would still be typing.
"""
import os
import re
from html import unescape

# R1: waiting for the rest of a message burst (seconds).
DEBOUNCE_MAX_SECONDS = 8.
QUICK, DEFAULT, SHORT, INCOMPLETE = 1.5, 2.5, 4., 6.
LEGACY_WAIT = .6  # groups keep the old short window
CONNECTIVES = frozenset('и а но или что чтобы потому короче слушай кстати типа ну в на с к по за из от у о'.split())

# R2: pacing and bubbles.
CONVERSATIONAL_LIMIT = 700
MAX_BUBBLES = 3
MAX_BUBBLE_CHARS = 400


def continuation_wait(text):
    """A short, unfinished-looking fragment earns a longer wait for its continuation."""
    t = ' '.join(str(text or '').split())
    if not t:
        return DEFAULT
    if len(t) >= 160:
        return QUICK
    if t.endswith(('...', '…', ',', ':', ';', '-', '—', '–', '(')):
        return INCOMPLETE
    words = re.findall(r'\w+', t.casefold())
    if words and words[-1] in CONNECTIVES:
        return INCOMPLETE
    if t[-1] in '?!.':
        return QUICK
    if len(t) < 25:
        return SHORT
    return DEFAULT


def plain_length(text):
    return len(unescape(re.sub(r'<[^>]+>', '', str(text or ''))).strip())


def read_seconds(user_text):
    return min(2., .5 + plain_length(user_text) / 150)


def typing_seconds(text):
    return min(6., .8 + plain_length(text) / 25)


def pause_seconds(text):
    return min(2.5, .4 + plain_length(text) / 60)


def conversational(text, *, task=False, card=False, markup=False):
    """Only short, plain conversational replies are paced and split."""
    if task or card or markup or not str(text or '').strip():
        return False
    if re.search(r'<(pre|code|blockquote)\b', text, re.IGNORECASE):
        return False
    if plain_length(text) > CONVERSATIONAL_LIMIT:
        return False
    return not re.search(r'(^|\n)\s*([-•*]|\d+[.)])\s', text)


def _balanced(fragment):
    opens = sorted(t.lower() for t in re.findall(r'<(b|i|u|s|a|tg-spoiler)\b[^>]*>', fragment, re.IGNORECASE))
    closes = sorted(t.lower() for t in re.findall(r'</(b|i|u|s|a|tg-spoiler)>', fragment, re.IGNORECASE))
    return opens == closes


def split_bubbles(text):
    """Two or three short paragraphs become separate messages; anything else stays whole."""
    whole = [str(text or '').strip()]
    parts = [p.strip() for p in re.split(r'\n\s*\n', str(text or '')) if p.strip()]
    if not 2 <= len(parts) <= MAX_BUBBLES or any(plain_length(p) > MAX_BUBBLE_CHARS for p in parts):
        return whole
    return parts if all(_balanced(p) for p in parts) else whole


def pacing_enabled():
    """Operators (and the test suite) can turn wall-clock delays off; bubbles stay."""
    return os.getenv('ARTI_HUMAN_PACING', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def plan_pacing(user_text, bubbles, elapsed):
    """Seconds before each bubble; the first one subtracts time already spent."""
    first = max(0., read_seconds(user_text) + typing_seconds(bubbles[0]) - max(0., elapsed))
    return [first] + [pause_seconds(b) for b in bubbles[1:]]


# I5: a pure acknowledgement gets a reaction, as people do in messengers.
_THANKS = re.compile(r'^(спасибо( большое| огромное)?|большое спасибо|спасибки|спс|благодарю|thanks|thank you|thx)[\s!.)]*$')
_OKAY = re.compile(r'^(ок|окей|ok|okay|понял|поняла|понятно|ясно)[\s!.)]*$')
_EMOJI = re.compile(r'^[\s❤️👍🙏👌🔥😊🥰💖♥]+$')


def acknowledgement_reaction(text, previous_reply=''):
    """Emoji for a pure acknowledgement, unless Arti's last message asked something."""
    t = ' '.join(str(text or '').casefold().split())
    if not t or len(t) > 24 or str(previous_reply or '').rstrip().endswith('?'):
        return None
    if _THANKS.match(t):
        return '❤'
    if _OKAY.match(t):
        return '👍'
    if _EMOJI.match(t):
        return '👍' if ('👍' in t or '👌' in t) else '❤'
    return None
