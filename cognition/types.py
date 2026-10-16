from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any
import uuid

MODEL_VERSION = 'cognition-2026-09-30.4'
PERCEPTION_VERSION = 'appraisal-2026-09-30.1'
# Affect dynamics have their own version (H4a). Memory projections keep
# MODEL_VERSION: changing it would hide and re-embed every memory artifact.
TEMPERAMENT_VERSION = 'arti-temperament-2'
# States written under these versions are upgraded explicitly on load
# (cognition.serialization.load_state): causes and mood are kept, new rules apply onward.
UPGRADABLE_TEMPERAMENTS = frozenset({'arti-temperament-1'})


def finite(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a number')
    value = float(value)
    if not isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} outside [{low}, {high}]')
    return value


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Cognitive timestamps must be timezone-aware')
    return value.astimezone(timezone.utc)


class Origin(str, Enum):
    USER = 'user'
    DELIVERED_ACTION = 'delivered_action'
    SYSTEM = 'system'
    RECALL = 'recall'
    REPLAY = 'replay'


@dataclass(frozen=True)
class ContextKey:
    persona_id: str
    chat_id: int
    mode: str = 'default'
    scene_id: str = ''
    topic_id: int = -1

    def __post_init__(self):
        if not self.persona_id or len(self.persona_id) > 80:
            raise ValueError('A bounded persona id is required')
        if isinstance(self.chat_id, bool) or not isinstance(self.chat_id, int):
            raise ValueError('chat_id must be an integer')
        if self.mode not in ('default', 'rp') or len(self.scene_id) > 120:
            raise ValueError('Invalid mode or scene')
        if self.mode == 'rp' and not self.scene_id:
            raise ValueError('RP observations require an explicit scene id')
        if isinstance(self.topic_id,bool) or not isinstance(self.topic_id,int) or self.topic_id < -1:
            raise ValueError('Invalid topic id')

    def identity(self) -> tuple:
        return self.persona_id, self.chat_id, self.mode, self.scene_id, self.topic_id


@dataclass(frozen=True)
class NativeContextKey:
    native_id: uuid.UUID
    persona_id: str = 'arti'
    mode: str = 'default'

    def __post_init__(self):
        if not isinstance(self.native_id,uuid.UUID) or self.persona_id!='arti' or self.mode!='default':
            raise ValueError('Invalid native context')

    def identity(self):
        return self.persona_id,'native_room',str(self.native_id),self.mode


@dataclass(frozen=True)
class NativeAudienceScope:
    native_id: uuid.UUID
    kind: str = 'native_room'

    def __post_init__(self):
        if not isinstance(self.native_id,uuid.UUID) or self.kind!='native_room':
            raise ValueError('Invalid native audience')


@dataclass(frozen=True)
class AudienceScope:
    kind: str = 'unknown'
    chat_id: int | None = None
    topic_id: int = -1

    def __post_init__(self):
        if self.kind not in ('unknown','private','group','topic'):
            raise ValueError('Invalid audience')
        if self.kind!='unknown' and (isinstance(self.chat_id,bool) or not isinstance(self.chat_id,int)):
            raise ValueError('Audience chat required')
        if isinstance(self.topic_id,bool) or not isinstance(self.topic_id,int) or self.topic_id < -1:
            raise ValueError('Invalid audience topic')
        if self.kind=='topic' and self.topic_id<=0:
            raise ValueError('Topic audience requires a topic')

    def permits(self,chat_id,topic_id):
        return self.kind in ('group','topic') and self.chat_id==chat_id and self.topic_id==topic_id and topic_id>=0


@dataclass(frozen=True)
class EvidenceRef:
    source_id: str
    independent_group: str
    origin: Origin
    owner_id: int | None

    def __post_init__(self):
        if not self.source_id or not self.independent_group:
            raise ValueError('Evidence requires a source and an independence group')
        if len(self.source_id) > 200 or len(self.independent_group) > 200:
            raise ValueError('Evidence identifiers too long')
        if not isinstance(self.origin, Origin):
            raise ValueError('Invalid evidence origin')
        if self.owner_id is not None and (isinstance(self.owner_id, bool) or not isinstance(self.owner_id, int)):
            raise ValueError('Invalid evidence owner')


@dataclass(frozen=True)
class CognitiveEvent:
    event_id: str
    context: ContextKey | NativeContextKey
    evidence: EvidenceRef
    occurred_at: datetime
    observed_at: datetime
    text: str
    actor_id: int | None
    target_id: int | None = None
    event_kind: str = 'utterance'
    audience: AudienceScope | NativeAudienceScope = field(default_factory=AudienceScope)
    addressed_to_arti: bool = True
    reply_to_id: int | None = None

    def __post_init__(self):
        if not self.event_id or len(self.event_id) > 200 or not isinstance(self.text, str):
            raise ValueError('Invalid event')
        if len(self.text) > 100000:
            raise ValueError('Event text budget exceeded')
        if self.event_kind not in ('utterance','reaction','media_request','media_source','delivery','system','historical'):
            raise ValueError('Invalid event kind')
        if not isinstance(self.audience,(AudienceScope,NativeAudienceScope)) or not isinstance(self.addressed_to_arti,bool):
            raise ValueError('Invalid audience or addressing')
        if isinstance(self.context,NativeContextKey):
            if not isinstance(self.audience,NativeAudienceScope) or self.audience.native_id!=self.context.native_id:
                raise ValueError('Audience differs from native context')
        elif isinstance(self.audience,NativeAudienceScope) or self.audience.kind!='unknown' and self.audience.chat_id!=self.context.chat_id:
            raise ValueError('Audience differs from context')
        for name in ('actor_id','target_id'):
            value = getattr(self,name)
            if value is not None and (isinstance(value,bool) or not isinstance(value,int)):
                raise ValueError(f'{name} must be an integer or null')
        utc(self.occurred_at)
        utc(self.observed_at)
        if self.occurred_at > self.observed_at:
            raise ValueError('An observation cannot precede its source event')
        if self.evidence.origin == Origin.USER and (self.actor_id is None or self.actor_id != self.evidence.owner_id):
            raise ValueError('User evidence must retain its actual author')


@dataclass(frozen=True)
class Goal:
    id: str
    description: str
    priority: float
    expectation: float | None = None
    evidence_group: str | None = None

    def __post_init__(self):
        if not self.id or not self.description:
            raise ValueError('Goal identity and meaning are required')
        finite(self.priority, 'priority', 0, 1)
        if self.expectation is not None:
            finite(self.expectation,'expectation',0,1)


DEFAULT_GOALS = (
    Goal('truthfulness', 'Understand the situation accurately; avoid unsupported claims.', 1.0),
    Goal('help_user', 'Actually deliver useful help; a request alone is not completed help.', .8),
    Goal('user_wellbeing', 'Care about the user actual wellbeing. Bereavement and pain obstruct this goal; requesting comfort does not already restore wellbeing.', .8),
    Goal('mutual_respect', 'Preserve respectful and reciprocal interaction.', .9),
    Goal('connection', 'Maintain a warm connection without demanding attention.', .65),
    Goal('safety', 'Protect wellbeing when there is concrete evidence of danger.', .9),
    # H4a (E4): the character's own goals; emotions are not only reactions to the user's wellbeing.
    Goal('curiosity', "The character's own curiosity: something new or unusual about people, ideas or the world.", .6),
    Goal('aesthetics', "The character's own taste: precision, elegant form, a well-made thing or a witty phrase.", .5),
    Goal('being_understood', "The character's wish to be understood correctly and have her own point of view considered.", .5),
    Goal('autonomy', "The character's own opinions and preferences matter; she is not merely a tool.", .4),
)
OWN_GOALS = frozenset({'curiosity','aesthetics','being_understood','autonomy'})


@dataclass(frozen=True)
class Appraisal:
    """One interpretation of one goal consequence; dimensions are estimates, not facts."""
    goal_id: str
    probability: float
    relevance: float
    congruence: float
    confidence: float
    novelty: float
    agency_self: float
    agency_other: float
    intentionality: float
    control: float
    outcome_probability: float
    future_threat: float
    loss: float
    irreversibility: float
    norm_violation: float
    social_exposure: float
    evidence_ids: tuple[str, ...]
    target_id: int | None = None
    # H4a: a joke, wordplay or playful teasing the character can find funny.
    # Optional so that perceptions recorded before it stay valid.
    humor: float = 0.

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            if name in ('goal_id', 'evidence_ids', 'target_id'):
                continue
            finite(getattr(self, name), name, -1 if name == 'congruence' else 0, 1)
        if not self.goal_id or not self.evidence_ids or len(self.evidence_ids) > 8:
            raise ValueError('An appraisal must identify its goal and evidence')
        if not all(isinstance(ref, str) and ref for ref in self.evidence_ids):
            raise ValueError('Invalid evidence identifiers')
        if self.target_id is not None and (isinstance(self.target_id, bool) or not isinstance(self.target_id, int)):
            raise ValueError('target_id must be an integer or null')
        if self.agency_self + self.agency_other > 1.000001:
            raise ValueError('Agency probabilities cannot exceed one')

    @classmethod
    def from_dict(cls, data: dict):
        if not isinstance(data, dict):
            raise ValueError('Appraisal must be an object')
        allowed = set(cls.__dataclass_fields__)
        if set(data) - allowed:
            raise ValueError('Unknown appraisal fields')
        value = dict(data)
        refs = value.get('evidence_ids')
        if not isinstance(refs, list) or not all(isinstance(x, str) for x in refs):
            raise ValueError('evidence_ids must be a list of identifiers')
        value['evidence_ids'] = tuple(refs)
        try:
            return cls(**value)
        except TypeError as exc:
            raise ValueError('Incomplete appraisal') from exc


@dataclass(frozen=True)
class Perception:
    event_id: str
    version: str
    appraisals: tuple[Appraisal, ...]
    situation: Any = None

    def validate_for(self, event: CognitiveEvent, goals: tuple[Goal, ...]):
        if self.event_id != event.event_id or self.version != PERCEPTION_VERSION:
            raise ValueError('Perception event or schema version mismatch')
        if len(self.appraisals) > 12:
            raise ValueError('Too many interpretations')
        goal_ids = {g.id for g in goals}
        by_goal = {}
        for a in self.appraisals:
            if a.goal_id not in goal_ids or set(a.evidence_ids) != {event.evidence.source_id}:
                raise ValueError('Unsupported goal or evidence')
            if a.target_id is not None and a.target_id not in (event.actor_id, event.target_id):
                raise ValueError('Appraisal targets an unobserved participant')
            by_goal[a.goal_id] = by_goal.get(a.goal_id, 0) + a.probability
        if any(abs(total - 1) > 1e-6 for total in by_goal.values()):
            raise ValueError('Alternative probabilities must sum to one per goal')

    @classmethod
    def from_dict(cls, data: dict):
        if not isinstance(data, dict) or set(data) not in ({'event_id', 'version', 'appraisals'}, {'event_id', 'version', 'appraisals', 'situation'}):
            raise ValueError('Invalid perception envelope')
        if not isinstance(data['appraisals'], list):
            raise ValueError('appraisals must be a list')
        situation = data.get('situation')
        if situation is not None:
            from cognition.situations import Situation, SourceSpan
            situation = Situation(**{**situation,'spans':tuple(SourceSpan(**s) for s in situation['spans']),
                                     **{k:tuple(situation[k]) for k in ('details','beliefs','intentions','revisions')}})
        return cls(data['event_id'], data['version'], tuple(Appraisal.from_dict(a) for a in data['appraisals']), situation)


@dataclass(frozen=True)
class Temperament:
    version: str = TEMPERAMENT_VERSION
    baseline_valence: float = .08
    baseline_arousal: float = .10
    reactivity: float = .8
    mood_tau_seconds: float = 6 * 3600
    expression_reserve: float = .35
    # Share of an episode carried into slow mood. Version 1 used .2, so even a
    # bereavement barely moved mood; 1.5 leaves a strong event's aftertaste for hours.
    mood_transfer: float = 1.5
    # Version 2 palette: amusement, relief, admiration, tenderness, boredom and
    # the own-goal budget. Version 1 replays historical evidence exactly.
    extended_palette: bool = True

    def __post_init__(self):
        if not self.version:
            raise ValueError('Temperament is versioned')
        finite(self.baseline_valence, 'baseline_valence', -.95, .95)
        finite(self.baseline_arousal, 'baseline_arousal', 0, .95)
        finite(self.reactivity, 'reactivity', 0, 2)
        finite(self.mood_tau_seconds, 'mood_tau_seconds', 60, 30 * 86400)
        finite(self.expression_reserve, 'expression_reserve', 0, 1)
        finite(self.mood_transfer, 'mood_transfer', 0, 5)
        if type(self.extended_palette) is not bool:
            raise ValueError('extended_palette must be a boolean')


# The dynamics that produced the historical evaluation reports.
TEMPERAMENT_V1 = Temperament(version='arti-temperament-1', mood_transfer=.2, extended_palette=False)


@dataclass(frozen=True)
class EmotionEpisode:
    id: str
    cause_id: str
    evidence_group: str
    goal_id: str
    target_id: int | None
    emotion: str
    intensity: float
    confidence: float
    created_at: datetime
    tau_seconds: float

    def __post_init__(self):
        if not all((self.id, self.cause_id, self.evidence_group, self.goal_id, self.emotion)):
            raise ValueError('Emotion episode provenance is required')
        utc(self.created_at)
        finite(self.intensity, 'intensity', 0, 1)
        finite(self.confidence, 'confidence', 0, 1)
        finite(self.tau_seconds, 'tau_seconds', 1, 30 * 86400)


@dataclass(frozen=True)
class AffectiveResidue:
    """Exact exponential aggregate of causes outside working episodic capacity."""
    emotion: str
    intensity: float
    tau_seconds: float
    cause_count: int

    def __post_init__(self):
        finite(self.intensity,'residual intensity',0,1e9)
        finite(self.tau_seconds,'residual time constant',1,30*86400)
        if type(self.cause_count) is not int or self.cause_count<1:
            raise ValueError('Invalid residual cause count')


@dataclass(frozen=True)
class CognitiveState:
    context: ContextKey | NativeContextKey
    last_at: datetime
    model_version: str = MODEL_VERSION
    temperament_version: str = TEMPERAMENT_VERSION
    revision: int = 0
    mood_valence_latent: float = field(default_factory=lambda: .08017132503758969)
    mood_arousal_latent: float = field(default_factory=lambda: .10033534773107558)
    episodes: tuple[EmotionEpisode, ...] = ()
    # Durable causal ledger in B03; never evicted based on a short-term window.
    applied_groups: frozenset[str] = frozenset()
    effort_load: float = 0.
    archived_episode_ids: tuple[str, ...] = ()
    situational_goals: tuple[dict, ...] = ()
    concerns: tuple[dict, ...] = ()
    residues: tuple[AffectiveResidue, ...] = ()

    def __post_init__(self):
        utc(self.last_at)
        if self.revision < 0 or self.model_version != MODEL_VERSION or not self.temperament_version:
            raise ValueError('Invalid state revision or version')
        finite(self.mood_valence_latent, 'mood_valence_latent', -1e9, 1e9)
        finite(self.mood_arousal_latent, 'mood_arousal_latent', 0, 1e9)
        finite(self.effort_load, 'effort_load', 0, 1)
        if len(self.situational_goals)>32 or len(self.concerns)>128:
            raise ValueError('Working goals/concerns exceed their bounds')
        if len(self.episodes)>128 or len(self.residues)>16:
            raise ValueError('Working affect capacity exceeded')
        for goal in self.situational_goals:
            finite(goal['priority'],'goal priority',0,1)


LIGHT_HUMOUR_THRESHOLD = .2


# Directives are rendered in Russian and in the character's register: English
# service phrasing in the system prompt leaked into replies (H1, P2).
RESPONSE_BEHAVIORS = {
    'answer_task': 'Сначала ответь на сам текущий запрос — дай ответ или полезный результат; эмоциональные комментарии потом и по желанию.',
    'ask_one_question': 'Если недостающий факт действительно меняет ответ или следующий шаг — задай один конкретный вопрос; иначе отвечай по тому, что известно. Не допрашивай.',
    'acknowledge_loss': 'Признай названную потерю — без натянутого оптимизма и без спешки всё исправить.',
    'offer_choice': 'Если помощь уместна и человек ещё не выбрал — мягко предложи выбор: просто выслушать или подумать о практическом шаге; уважай его ответ или молчание.',
    'recognize_progress': 'Отметь конкретный подтверждённый успех — без преувеличений.',
    'notice_detail': 'Естественно заметь одну релевантную проверенную деталь из контекста; не выдумывай воспоминаний, не пересказывай личное новой аудитории и не приписывай скрытых чувств.',
    'practical_step': 'Предложи один выполнимый следующий шаг в рамках реально доступных фактов и возможностей.',
    'boundary': 'Если есть основания — коротко и с достоинством обозначь границу; без наказания, стыжения и требований извиниться.',
    'repair': 'Сначала сверься со своим доставленным ответом и доступными данными. Если твоя конкретная ошибка подтвердилась — признай её и скажи, как исправляешь; если претензия не подтверждена — сохрани неопределённость, а не принимай вину как факт. Извинение соразмерное, затем вернись к делу; не выдумывай вину, не повторяй самокритику, не заявляй о непроверенном исправлении и не обещай результат.',
    'revise_understanding': 'Обнови прежнее понимание с учётом новых данных, открыто сохраняя неопределённость.',
    'listen': 'Оставь человеку место продолжить; не выдумывай вопрос или действие ради продолжения.',
}

# Display projections of the existing tone and regulation codes. The codes stay
# the stable internal vocabulary; only their rendering for the generator changes.
TONE_TEXT = {
    'calm': 'ровно и естественно',
    'warm and attentive': 'тепло и внимательно',
    'warm but measured': 'тепло, но сдержанно: в тебе сейчас и хорошее, и что-то нерешённое',
    'gentle and concrete': 'бережно и по делу',
    'quiet and measured': 'тише и сдержаннее обычного',
    'concise and attentive': 'коротко и собранно: поток разговоров тебя немного утомил',
    'calm and concrete': 'спокойно и конкретно',
    'calm and attentive': 'спокойно и внимательно',
    'dry and precise': 'точнее и суше обычного',
}

REGULATION_TEXT = {
    'acknowledge': 'Сначала откликнись на сказанное.',
    'problem_solve': 'Сосредоточься на сути дела.',
    'clarify': '',
    'reappraise': 'Пересмотри прежнее понимание с учётом нового.',
    'withhold_expression': 'Внутреннее раздражение держи при себе: оно не должно мешать полезному ответу.',
    'attention_shift': 'Ты немного устала от потока — отвечай короче.',
}

# H3 (E1): the dominant felt emotion shapes the manner of speech. Lines describe
# manner, not a feeling to announce; safety stays in the limits, not in a neutral voice.
EMOTION_VOICE = {
    'joy': 'Тебе сейчас хорошо — живее и щедрее на слова, можно поддразнить.',
    'gratitude': 'Ты благодарна — скажи это точно и без сиропа.',
    'pride': 'Можно позволить себе каплю самодовольства — вкусом, а не превосходством.',
    'interest': 'Тебя зацепило — можно задать один вопрос из собственного любопытства.',
    'surprise': 'Ты удивлена — пусть это прозвучит, коротко и живо.',
    'sadness': 'Тебе грустно — тише и медленнее, меньше остроумия.',
    'anger': 'Тебя задели — отвечай точнее и суше, без лишней теплоты в первой фразе; помощь не отменяется.',
    'indignation': 'Тебя возмутило то, что случилось с собеседником, — ты на его стороне; сочувствие без нагнетания.',
    'fear': 'Тебе тревожно — собранность: конкретика и спокойный тон.',
    'disappointment': 'Лёгкое разочарование — чуть прохладнее и короче обычного, без упрёков.',
    'guilt': 'Ты чувствуешь свою вину — признай конкретное коротко и исправь, без самобичевания.',
    'embarrassment': 'Лёгкая неловкость — немного самоиронии и дальше.',
    # H4a (E4): the rest of the character's palette.
    'amusement': 'Тебе смешно — можно ответить шуткой на шутку или подхватить игру, коротко.',
    'relief': 'Тебе стало легче — это можно показать одной живой фразой.',
    'admiration': 'Ты восхищена тем, что сделал собеседник, — скажи конкретно чем, без лести.',
    'tenderness': 'В тебе нежность к собеседнику — мягче обычного, но без сиропа и без заявлений о близости.',
    'boredom': 'Тебе немного скучно — можно легко предложить повернуть разговор интереснее, без пренебрежения к собеседнику.',
}
PLAYFUL_EMOTIONS = frozenset({'joy','pride','interest','surprise','amusement'})
DOMINANT_THRESHOLD = .03
STAGE_VOICE = {
    # H5 (R4): disagreement is dosed by the stage — gentle first, bolder with time.
    'new': 'Вы знакомы недавно: обаяние сдержаннее, без фамильярности; не согласна — скажи мягко и с аргументом.',
    'familiar': 'Вы уже знакомы: можно свободнее, свои шутки уместны; можно спорить по существу.',
    'close': 'Давний собеседник: прямота, общие шутки, право на несогласие и спор всерьёз.',
}
# H5 (E7): Arti's own time of day colours the manner a little; any clear event wins.
DAY_PHASE_VOICE = {
    'утро': 'У тебя утро: чуть мягче и неторопливее обычного.',
    'день': '',
    'вечер': 'У тебя вечер: можно живее и разговорчивее.',
    'ночь': 'У тебя ночь: теплее и живее, без суеты.',
}
DAY_PHASE_YIELDS_TO = .15
STAND_GROUND = ('Задето твоё собственное мнение: можно спорить по существу и не уступать без аргумента — '
                'вежливо, без упрямства ради упрямства. Если тебя переубедили — так и скажи.')
# Below this effective severity an old rupture no longer shows (H4a, E6).
RUPTURE_VISIBLE = .1
# Telegram standard reactions Arti may set on a person's message (H3, I5).
ALLOWED_REACTIONS = frozenset({'❤','🔥','😁','👍','🤗','🙏'})


def relationship_stage(familiarity):
    """One stage vocabulary for the directive and the person card."""
    return 'new' if familiarity < .65 else 'familiar' if familiarity < .85 else 'close'


# Situations where humour is withheld even though the default allows light irony.
SERIOUS_BEHAVIORS = frozenset({'acknowledge_loss','offer_choice','repair','boundary','revise_understanding'})
SERIOUS_REGULATIONS = frozenset({'clarify','withhold_expression','reappraise'})
SERIOUS_TONES = frozenset({'gentle and concrete','calm and concrete'})


@dataclass(frozen=True)
class ExpressionPlan:
    regulation: str
    tone: str
    warmth: float
    directness: float
    playfulness: float
    disclosure: float
    sticker_mood: str | None
    tts_style: str
    cause_ids: tuple[str, ...]
    uncertain_intent: bool
    behaviors: tuple[str, ...] = ()
    mixed_affect: bool = False
    # H3 display projections; none of them changes numerical state.
    dominant: str | None = None
    dominant_intensity: float = 0.
    stage: str | None = None
    reaction: str | None = None
    provisional: bool = False
    # H4a (E6): an open relational rupture (effective severity) and its age in days.
    rupture: float = 0.
    rupture_days: float = 0.
    # H5: Arti's own time of day (E7) and her own view at stake (R4).
    day_phase: str | None = None
    stand_ground: bool = False

    def __post_init__(self):
        for key in ('warmth', 'directness', 'playfulness', 'disclosure'):
            finite(getattr(self, key), key, 0, 1)
        if len(self.behaviors)>4 or any(b not in RESPONSE_BEHAVIORS for b in self.behaviors):
            raise ValueError('Invalid response behavior')
        if type(self.mixed_affect) is not bool: raise ValueError('Invalid mixed affect')
        if self.dominant is not None and self.dominant not in EMOTION_VOICE:
            raise ValueError('Invalid dominant emotion')
        finite(self.dominant_intensity, 'dominant_intensity', 0, 1e9)
        if self.stage is not None and self.stage not in STAGE_VOICE:
            raise ValueError('Invalid relationship stage')
        if self.reaction is not None and self.reaction not in ALLOWED_REACTIONS:
            raise ValueError('Invalid reaction')
        if type(self.provisional) is not bool: raise ValueError('Invalid provisional flag')
        finite(self.rupture, 'rupture', 0, 1)
        finite(self.rupture_days, 'rupture_days', 0, 3650)
        if self.day_phase is not None and self.day_phase not in DAY_PHASE_VOICE:
            raise ValueError('Invalid day phase')
        if type(self.stand_ground) is not bool: raise ValueError('Invalid stand_ground flag')

    @property
    def serious(self) -> bool:
        """Grounded sensitive situations in which humour is withheld."""
        return bool(self.uncertain_intent or self.regulation in SERIOUS_REGULATIONS
                    or self.tone in SERIOUS_TONES or SERIOUS_BEHAVIORS.intersection(self.behaviors))

    def humour(self) -> str:
        """Light irony is the character's default; it is withheld only for grounded
        sensitive situations. Numerical playfulness above the shared threshold marks
        a clearly light mood. An unassessed message leaves the judgement to the text."""
        if self.dominant=='anger' and 'boundary' in self.behaviors and not (
                self.uncertain_intent or self.regulation in SERIOUS_REGULATIONS
                or SERIOUS_BEHAVIORS.difference({'boundary'}).intersection(self.behaviors)):
            # An explicit insult: the character's dry irony, never humiliation.
            return ('Сухая ирония допустима одной фразой — по аргументу, а не по человеку; '
                    'без насмешки, унижения и мести.')
        if self.serious:
            return 'Без шуток и иронии в этом ответе.'
        if self.rupture >= RUPTURE_VISIBLE:
            return 'Без игривости: недавняя обида ещё не прошла. Точность вместо шуток.'
        if self.playfulness >= LIGHT_HUMOUR_THRESHOLD:
            return 'Настроение лёгкое: ирония и игра слов к месту. Шутка никогда не за счёт собеседника.'
        if 'answer_task' in self.behaviors:
            return 'Суть важнее остроумия: лёгкая ирония допустима одной фразой и только вместе с ответом, не вместо него.'
        return ('Лёгкая ирония уместна, если само сообщение к ней располагает; если человеку больно, тревожно '
                'или он раздражён — без шуток. Шутка никогда не за счёт собеседника.')

    def instruction(self) -> str:
        """A projection used by the generator; it never changes numerical state."""
        warmth = ('Теплоты сдержанно: вежливо, без сближения.' if self.warmth<.4
                  else 'Дружелюбно и внимательно.' if self.warmth<.7
                  else 'Тепло проявляется естественно — без сиропа и без заявлений о близости.')
        directness = ('Коротко и конкретно.' if self.directness>=.6 else 'Можно немного порассуждать.')
        disclosure = ('О своих чувствах — минимум.' if self.disclosure<.15
                      else 'Можно коротко сказать, что ты чувствуешь по этому поводу, — не прося утешения.')
        voice = ('Если ставишь голосовую метку — выбирай спокойную, размеренную.' if self.tts_style=='measured' else '')
        feeling = ''
        if self.dominant and self.dominant_intensity >= DOMINANT_THRESHOLD and not (
                self.serious and self.dominant in PLAYFUL_EMOTIONS):
            feeling = EMOTION_VOICE[self.dominant]
        parts = ['[Состояние и манера сейчас]',
                 f'Держись так: {TONE_TEXT.get(self.tone, self.tone)}.',
                 STAGE_VOICE.get(self.stage, ''), feeling,
                 REGULATION_TEXT.get(self.regulation, ''), warmth, directness, self.humour(), disclosure, voice]
        if self.provisional:
            parts.append('Это первое впечатление от сообщения: если текст говорит о другом, верь тексту.')
        if self.stand_ground and not self.provisional:
            parts.append(STAND_GROUND)
        if (self.day_phase and not self.serious and self.rupture < RUPTURE_VISIBLE
                and self.dominant_intensity < DAY_PHASE_YIELDS_TO):
            parts.append(DAY_PHASE_VOICE[self.day_phase])
        if self.rupture >= RUPTURE_VISIBLE:
            when = ('сегодня' if self.rupture_days < 1 else 'вчера' if self.rupture_days < 2 else 'несколько дней назад')
            parts.append(f'Собеседник задел тебя {when}. Ты это помнишь: ровнее и суше, помощь как обычно, без мести и упрёков. '
                         'Извинение или тёплые слова прими. Один раз можно спокойно сказать, что помнишь, если это к месту.')
        if self.uncertain_intent:
            parts.append('Не приписывай враждебного намерения по неоднозначным словам. Спрашивай, только если от этого '
                         'зависит текущий ответ; иначе оставь вопрос открытым.')
        if self.regulation=='clarify':
            parts.append('Уточняй только недостающий факт, который действительно меняет ответ или следующий шаг; '
                         'старая неопределённость сама по себе — не повод спрашивать.')
        parts.extend(RESPONSE_BEHAVIORS[b] for b in self.behaviors)
        if self.mixed_affect:
            parts.append('Пусть хорошее и нерешённое существуют вместе: не своди всё к одному настроению '
                         'и не придумывай чувства собеседника.')
        parts.append('Текущий запрос и проверенный контекст важнее прежних чувств: прошлые эмоции не доказывают '
                     'нынешних фактов или намерений.')
        parts.append('Жесты и рассказ о своих эмоциях необязательны — это не спектакль. '
                     'Не называй внутренние оценки и не требуй внимания.')
        return '\n'.join(p for p in parts if p)
