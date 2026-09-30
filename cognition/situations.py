"""Grounded semantic observations. Meaning is proposed; provenance is verified."""
from dataclasses import dataclass, field, replace
from datetime import datetime
import re
from cognition.types import finite, utc


@dataclass(frozen=True)
class SourceSpan:
    start: int
    end: int
    text: str

    def validate(self, text):
        if (type(self.start) is not int or type(self.end) is not int
                or not 0 <= self.start < self.end <= len(text)
                or text[self.start:self.end] != self.text):
            raise ValueError('Source span must match the original observation exactly')


@dataclass(frozen=True)
class Situation:
    topic: str = ''
    kind: str = 'neutral'
    modality: str = 'reported'
    intention_evidence: str = 'unobserved'
    outcome: str = 'unknown'
    spans: tuple[SourceSpan, ...] = ()
    details: tuple[dict, ...] = ()
    beliefs: tuple[dict, ...] = ()
    intentions: tuple[dict, ...] = ()
    revisions: tuple[dict, ...] = ()
    preferences: dict = field(default_factory=dict)
    social_signal: str = 'contact'
    social_signal_actor: int | str | None = None

    @classmethod
    def from_dict(cls, data, event, eligible_sources=()):
        required = set(cls.__dataclass_fields__)-{'social_signal_actor'}
        if not isinstance(data, dict) or set(data) not in (required,required|{'social_signal_actor'}):
            raise ValueError('Situation fields must match the supplied schema')
        if not isinstance(data['topic'], str) or len(data['topic']) > 160:
            raise ValueError('Invalid topic')
        choices = {
            'kind': ('neutral','preference','request','success','loss','threat','conflict','clarification'),
            'modality': ('reported','interaction','hypothetical','quoted'),
            'intention_evidence': ('explicit','ambiguous','unobserved'),
            'outcome': ('unknown','pending','confirmed','resolved'),
            'social_signal': ('contact','care','cooperation','fulfilled','breach','insult','apology'),
        }
        for name, values in choices.items():
            if data[name] not in values:
                raise ValueError('Invalid situation ' + name)
        spans = data['spans']
        if not isinstance(spans, list) or len(spans) > 16:
            raise ValueError('Invalid source spans')
        parsed = []
        for span in spans:
            if not isinstance(span, dict) or set(span) != {'start','end','text'}:
                raise ValueError('Invalid span schema')
            value = SourceSpan(**span)
            value.validate(event.text)
            parsed.append(value)
        prefs = data['preferences']
        if (not isinstance(prefs, dict) or set(prefs) - {'text','voice','stickers','proactive'}
                or any(type(v) is not bool for v in prefs.values())):
            raise ValueError('Invalid channel preference')
        if prefs and data['kind']!='preference':
            raise ValueError('Preferences require an explicit preference observation; omit unstated preferences')
        def items(name, keys,optional=frozenset()):
            values = data[name]
            if not isinstance(values, list) or len(values) > 8:
                raise ValueError('Invalid ' + name)
            for value in values:
                if not isinstance(value, dict) or not keys<=set(value) or set(value)-keys-optional:
                    raise ValueError('Invalid ' + name + ' fields')
                index = value['span']
                if type(index) is not int or not 0 <= index < len(parsed):
                    raise ValueError('Semantic item requires an exact source span')
                if 'confidence' in value:
                    finite(value['confidence'], 'confidence', 0, 1)
                for key in keys - {'span','confidence','centrality','deadline','subject'}:
                    if not isinstance(value[key], str) or len(value[key]) > 1000:
                        raise ValueError('Invalid semantic string')
            return tuple(dict(v) for v in values)
        details = items('details', {'span','kind','centrality','confidence'})
        for item in details:
            if item['kind'] not in ('gist','name','date','wording','place','action'):
                raise ValueError('Invalid detail kind')
            finite(item['centrality'], 'centrality', 0, 1)
        beliefs = items('beliefs', {'span','subject','predicate','value','condition','assertion','confidence'})
        if beliefs and data['kind']=='request':
            raise ValueError('An ordinary request is not a personal factual assertion; beliefs must be empty')
        for item in beliefs:
            if type(item['subject']) is not int and item['subject']!='arti':
                raise ValueError('Belief subject requires an integer participant or arti')
            if item['subject'] not in (event.actor_id,event.target_id,'arti'):
                raise ValueError('Belief subject is not observed')
            if item['assertion'] not in ('explicit','inferred','exception','correction'):
                raise ValueError('Invalid assertion kind')
            if item['assertion'] in ('explicit','correction','exception') and item['value'] not in parsed[item['span']].text:
                raise ValueError('Explicit belief value must occur in its source span')
        intentions = items('intentions', {'span','key','description','cue','deadline','status','confidence'}, {'actor'})
        for item in intentions:
            if 'actor' in item and type(item['actor']) is not int and item['actor']!='arti':
                raise ValueError('Intention actor requires an integer participant or arti')
            if 'actor' in item and item['actor'] not in (event.actor_id,event.target_id,'arti'):
                raise ValueError('Intention actor is not an observed participant')
            if event.evidence.origin.value=='user' and item.get('actor')=='arti' and item['status'] in ('open','reminder'):
                raise ValueError('A user request cannot establish Arti\'s own commitment')
            if item['status'] not in ('open','fulfilled','cancelled','reminder'):
                raise ValueError('Invalid intention status')
            if item['deadline'] is not None:
                try:
                    utc(datetime.fromisoformat(item['deadline']))
                except (TypeError,ValueError):
                    raise ValueError('Intention deadline requires an explicit UTC offset')
                anchor = parsed[item['span']].text
                explicit_zone = re.search(r'(?:UTC|GMT|Z\b|[+-]\d{2}:\d{2})',anchor,re.IGNORECASE)
                explicit_time = re.search(r'(?:^|[T\s])\d{1,2}:\d{2}\b',anchor)
                if not explicit_zone or not explicit_time:
                    raise ValueError('Operational deadlines require a stated time and timezone; otherwise use null and clarify')
        revisions = items('revisions', {'span','source_id','interpretation','confidence','attribution'})
        if any(r['attribution'] not in ('intentional','accidental','unknown','resolved') for r in revisions):
            raise ValueError('Invalid revised attribution')
        if event.event_kind=='reaction' and (beliefs or intentions or revisions):
            raise ValueError('Reaction metadata cannot establish personal facts or commitments')
        if any(v['source_id'] not in eligible_sources for v in revisions):
            raise ValueError('Reappraisal source is outside the eligible evidence registry')
        if (prefs or beliefs or intentions or revisions or data['social_signal'] != 'contact') and not parsed:
            raise ValueError('Learning requires source spans')
        social_actor = data.get('social_signal_actor')
        if social_actor is not None and type(social_actor) is not int and social_actor!='arti':
            raise ValueError('Social actor requires an integer participant or arti')
        if social_actor not in (None,event.actor_id,event.target_id,'arti'):
            raise ValueError('Social actor is not observed')
        return cls(data['topic'],data['kind'],data['modality'],data['intention_evidence'],
                   data['outcome'],tuple(parsed),details,beliefs,intentions,revisions,dict(prefs),data['social_signal'],social_actor)


def calibrated_appraisals(perception):
    """Intent attribution is limited by evidence, independently of observed loss."""
    s = perception.situation
    if s is None:
        return perception
    values = []
    for a in perception.appraisals:
        if s.kind == 'preference' or s.modality in ('hypothetical','quoted'):
            a = replace(a, relevance=0., congruence=0., novelty=0.)
        elif s.intention_evidence != 'explicit':
            limit = .35 if s.intention_evidence == 'ambiguous' else .1
            a = replace(a, intentionality=min(a.intentionality,limit))
            if a.goal_id == 'mutual_respect' and s.outcome == 'unknown':
                a = replace(a, confidence=min(a.confidence,.45), norm_violation=min(a.norm_violation,.4))
        if s.modality=='reported':
            # "Self" is the character, not the narrator. A user's embarrassment
            # is evidence for empathy, not proof the character made that mistake.
            a = replace(a,agency_self=0.,social_exposure=0.)
        if s.kind in ('neutral','request','clarification') and s.outcome!='confirmed':
            a = replace(a,relevance=min(a.relevance,.2),novelty=min(a.novelty,.15))
        if s.social_signal=='apology':
            # An admission refers to the earlier cause; it is not a fresh insult.
            a = replace(a,norm_violation=0.,intentionality=min(a.intentionality,.1),future_threat=0.)
        values.append(a)
    return replace(perception,appraisals=tuple(values))


@dataclass(frozen=True)
class GoalState:
    key: str
    description: str
    priority: float
    status: str
    confidence: float
    evidence_group: str
    expectation: float = .5


def goal_transition(previous, situation, event):
    result = dict(previous)
    for item in situation.intentions:
        old = result.get(item['key'], {})
        confidence = min(item['confidence'],.95)
        result[item['key']] = dict(description=item['description'],priority=.7,
                                  status=item['status'],confidence=confidence,
                                  evidence_group=event.evidence.independent_group,
                                  expectation=old.get('expectation',.5))
    # Bounded active goal stack; resolved goals remain in intention history.
    return dict(list(result.items())[-32:])
