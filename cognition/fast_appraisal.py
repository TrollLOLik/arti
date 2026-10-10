"""Fast, conservative first impression of a message (H3, E9).

Used only to shape the expression plan while the grounded interpretation is
still running. It never writes cognitive state, evidence, relationships or
memory, and abstains on anything ambiguous: quotations and reported speech,
negation, sarcasm markers, long or mixed messages. Precision is preferred over recall: an
abstention keeps the previous neutral fallback.
"""
import re
from dataclasses import dataclass

from cognition.situations import Situation

MAX_CHARS = 280

_REPORTED = re.compile(r'[«»"“”„]|\b(сказал\w*|написал\w*|пишет|говорит|говорил\w*|цитат\w*|если бы|как будто|'
                       r'представь\w*|допустим|в фильме|в книге|в сериале|персонаж\w*|герой|героиня)\b')
_SARCASM = re.compile(r'\bну спасибо\b|удружил\w*|спасибо за ничего|спасибо,? кэп|очень смешно|ага,? конечно|ну да,? конечно')
_PERSONAL = re.compile(r'\b(мой|моя|мое|мои|моего|моей|моих|у меня|мама|мамы|папа|папы|отец|мать|бабушк\w*|дедушк\w*|'
                       r'брат\w*|сестр\w*|друг\w*|подруг\w*|кот|кошк\w*|котик\w*|собак\w*|пес|пёс|муж\w*|жена|жены|'
                       r'сын\w*|доч\w*|ребенок|ребёнок)\b')

RULES = (
    # label, pattern, kind, social signal, display emotion, needs personal anchor
    ('crisis', r'\b(не хочу жить|хочу умереть|покончить с собой|суицид\w*|убить себя|порезать себя)\b',
     'threat', 'contact', 'fear', False),
    ('loss', r'\b(умер|умерла|умерли|скончал(ся|ась|ись)|погиб(ла|ли)?|не стало|похоронил(а|и)?|похороны|'
             r'пришлось усыпить|усыпили)\b', 'loss', 'contact', 'sadness', True),
    ('setback', r'\b(меня уволили|меня бросил(а)?|мы расстались|я развожусь|мы разводимся|у нас развод)\b',
     'loss', 'contact', 'sadness', False),
    ('fear', r'\b(мне страшно|я боюсь|паническ\w+ атак\w*|у меня паника)\b', 'threat', 'contact', 'fear', False),
    ('insult', r'\b(ты (тупая|тупой|дура|дурак|идиотка|идиот|бесполезная|бесполезный|бездушная|глупая|никчемная|'
               r'никчёмная)|тупая железк\w*|заткнись|ненавижу тебя|ты меня бесишь|ты меня достала)\b',
     'conflict', 'insult', 'anger', False),
    ('apology', r'(^|\s)(прости|извини|сорри|прошу прощения|простите|извините)\b', 'clarification', 'apology', None, False),
    ('success', r'\b(сдал(а)?|защитил(ся|ась)?|меня взяли|получил(а)? (оффер|работу|повышение|грант|визу)|'
                r'у меня получилось|выиграл(а|и)?|победил(а|и)?|ура+)\b', 'success', 'contact', 'joy', False),
    ('care', r'\b(спасибо|благодарю|ты лучшая|ты чудо|обожаю тебя|люблю тебя|ты милая|ты умница|скучал(а)? по тебе)\b',
     'neutral', 'care', 'gratitude', False),
    ('amusement', r'(ахах\w*|хаха\w*|\bлол\b|😂|🤣|😆|\bржу\b|\bсмешно\b)', 'neutral', 'contact', 'amusement', False),
)
_COMPILED = tuple((label, re.compile(pattern), kind, social, emotion, anchor)
                  for label, pattern, kind, social, emotion, anchor in RULES)


@dataclass(frozen=True)
class FastSignal:
    label: str
    kind: str
    social: str
    emotion: str | None
    intensity: float = .2

    def situation(self):
        return Situation(kind=self.kind, modality='interaction',
                         intention_evidence='explicit' if self.social == 'insult' else 'unobserved',
                         outcome='confirmed' if self.kind in ('loss', 'success') else 'unknown',
                         social_signal=self.social)


def _normalize(text):
    return ' '.join(str(text or '').casefold().replace('ё', 'е').split())


def _negated(text, start):
    return bool(re.search(r'(^|\s)(не|ни|нет)\s+$', text[max(0, start - 8):start]))


def fast_signal(text):
    """Return a FastSignal for a clear, short, first-person message, otherwise None."""
    normalized = _normalize(text)
    if not normalized:
        return None
    for label, pattern, kind, social, emotion, anchor in _COMPILED:
        match = pattern.search(normalized)
        if not match or _negated(normalized, match.start()):
            continue
        if label != 'crisis':
            # Safety cues stay serious even when reported; everything else abstains.
            if len(normalized) > MAX_CHARS or _REPORTED.search(normalized) or _SARCASM.search(normalized):
                return None
            if anchor and not _PERSONAL.search(normalized):
                return None
        emotion = 'anger' if label == 'insult' else emotion
        return FastSignal(label, kind, social, emotion)
    return None
