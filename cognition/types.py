from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any

MODEL_VERSION = 'cognition-2026-09-30.4'
PERCEPTION_VERSION = 'appraisal-2026-09-30.1'


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
    context: ContextKey
    evidence: EvidenceRef
    occurred_at: datetime
    observed_at: datetime
    text: str
    actor_id: int | None
    target_id: int | None = None
    event_kind: str = 'utterance'
    audience: AudienceScope = field(default_factory=AudienceScope)
    addressed_to_arti: bool = True
    reply_to_id: int | None = None

    def __post_init__(self):
        if not self.event_id or len(self.event_id) > 200 or not isinstance(self.text, str):
            raise ValueError('Invalid event')
        if len(self.text) > 100000:
            raise ValueError('Event text budget exceeded')
        if self.event_kind not in ('utterance','reaction','media_request','delivery','system','historical'):
            raise ValueError('Invalid event kind')
        if not isinstance(self.audience,AudienceScope) or not isinstance(self.addressed_to_arti,bool):
            raise ValueError('Invalid audience or addressing')
        if self.audience.kind!='unknown' and self.audience.chat_id!=self.context.chat_id:
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
)


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
    version: str = 'arti-temperament-1'
    baseline_valence: float = .08
    baseline_arousal: float = .10
    reactivity: float = .8
    mood_tau_seconds: float = 6 * 3600
    expression_reserve: float = .35

    def __post_init__(self):
        if not self.version:
            raise ValueError('Temperament is versioned')
        finite(self.baseline_valence, 'baseline_valence', -.95, .95)
        finite(self.baseline_arousal, 'baseline_arousal', 0, .95)
        finite(self.reactivity, 'reactivity', 0, 2)
        finite(self.mood_tau_seconds, 'mood_tau_seconds', 60, 30 * 86400)
        finite(self.expression_reserve, 'expression_reserve', 0, 1)


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
    context: ContextKey
    last_at: datetime
    model_version: str = MODEL_VERSION
    temperament_version: str = 'arti-temperament-1'
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
        if len(self.episodes)>128 or len(self.residues)>11:
            raise ValueError('Working affect capacity exceeded')
        for goal in self.situational_goals:
            finite(goal['priority'],'goal priority',0,1)


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

    def __post_init__(self):
        for key in ('warmth', 'directness', 'playfulness', 'disclosure'):
            finite(getattr(self, key), key, 0, 1)

    def instruction(self) -> str:
        """A projection used by the generator; it never changes numerical state."""
        warmth = ('Use restrained, respectful warmth.' if self.warmth<.4 else 'Use a friendly, attentive manner.' if self.warmth<.7 else 'Express care naturally, without claiming intimacy.')
        directness = ('Be concise and concrete.' if self.directness>=.6 else 'Allow a little reflective explanation.')
        play = ('Avoid jokes in this reply.' if self.playfulness<.2 else 'Light humour is welcome when relevant.')
        disclosure = ('Keep personal emotional disclosure minimal.' if self.disclosure<.15 else 'A brief cause-related feeling may be expressed without demanding reassurance.')
        return (f'Tone: {self.tone}. Regulation: {self.regulation}. Voice: {self.tts_style}. '
                + warmth+' '+directness+' '+play+' '+disclosure+' '
                + ('Ask briefly before attributing a hostile intention. ' if self.uncertain_intent else '')
                + ('Ask for the missing information needed to carry out this request. ' if self.regulation=='clarify' and not self.uncertain_intent else '')
                + 'Keep the reply relevant. Do not describe internal scores or demand attention.')
