"""Explicit wire format; importing this module never initializes a provider client."""
import json
import uuid
from dataclasses import asdict
from datetime import datetime

from cognition.types import (CognitiveEvent, CognitiveState, ContextKey, EmotionEpisode,AffectiveResidue,
                             EvidenceRef, Origin, AudienceScope,NativeContextKey,NativeAudienceScope)


def dump(value) -> str:
    def encode(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj,uuid.UUID):
            return str(obj)
        if isinstance(obj, (frozenset, set)):
            return sorted(obj)
        raise TypeError(type(obj).__name__)
    return json.dumps(asdict(value) if hasattr(value, '__dataclass_fields__') else value,
                      default=encode, ensure_ascii=False, allow_nan=False, sort_keys=True)


def object_value(value):
    return json.loads(value) if isinstance(value, str) else value


def load_context(value):
    data=dict(value)
    if 'native_id' in data:
        data['native_id']=uuid.UUID(data['native_id'])
        return NativeContextKey(**data)
    return ContextKey(**data)


def load_audience(value):
    data=dict(value)
    if data.get('kind')=='native_room':
        data['native_id']=uuid.UUID(data['native_id'])
        return NativeAudienceScope(**data)
    return AudienceScope(**data)


def load_event(value) -> CognitiveEvent:
    data = object_value(value)
    return CognitiveEvent(data['event_id'], load_context(data['context']),
                          EvidenceRef(**{**data['evidence'], 'origin': Origin(data['evidence']['origin'])}),
                          datetime.fromisoformat(data['occurred_at']), datetime.fromisoformat(data['observed_at']),
                          data['text'], data['actor_id'], data.get('target_id'),data.get('event_kind','utterance'),
                          load_audience(data.get('audience',{})),data.get('addressed_to_arti',True),data.get('reply_to_id'))


def upgrade_state(data):
    """Explicit temperament upgrade (H4a): stored causes, mood and goals are kept;
    the new dynamics apply to events from now on. Unknown versions still fail."""
    from cognition.types import TEMPERAMENT_VERSION, UPGRADABLE_TEMPERAMENTS
    if data.get('temperament_version') in UPGRADABLE_TEMPERAMENTS:
        data = {**data, 'temperament_version': TEMPERAMENT_VERSION}
    return data


def load_state(value) -> CognitiveState:
    data = upgrade_state(dict(object_value(value)))
    data['context'] = load_context(data['context'])
    data['last_at'] = datetime.fromisoformat(data['last_at'])
    data['episodes'] = tuple(EmotionEpisode(**{**ep, 'created_at': datetime.fromisoformat(ep['created_at'])}) for ep in data['episodes'])
    data['applied_groups'] = frozenset(data['applied_groups'])
    data['archived_episode_ids'] = tuple(data.get('archived_episode_ids', ()))
    data['situational_goals'] = tuple(data.get('situational_goals',()))
    data['concerns'] = tuple(data.get('concerns',()))
    data['residues'] = tuple(AffectiveResidue(**r) for r in data.get('residues',()))
    return CognitiveState(**data)
