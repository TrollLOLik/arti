"""Product identities are separate from Telegram delivery addresses."""
from dataclasses import dataclass
import hashlib
import json
import uuid


class ConversationError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def user_id(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ConversationError('invalid_user')
    return value


def identifier(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ConversationError('invalid_id') from None


def profile(value):
    uid = user_id(value.get('id'))
    name, username = value.get('first_name', ''), value.get('username', '')
    if not isinstance(name, str) or len(name) > 128 or not isinstance(username, str) or len(username) > 64 or '\x00' in name or '\x00' in username:
        raise ConversationError('invalid_profile')
    return dict(id=uid, first_name=name, username=username)


@dataclass(frozen=True)
class ContextRef:
    kind: str
    persona_id: str = 'arti'
    telegram_chat_id: int | None = None
    native_id: uuid.UUID | None = None
    topic_id: int = -1
    mode: str = 'default'
    scene_id: str = ''

    def __post_init__(self):
        if self.kind == 'telegram_private':
            user_id(self.telegram_chat_id)
            if self.native_id is not None:
                raise ConversationError('invalid_context')
        elif self.kind == 'native_room':
            if not isinstance(self.native_id, uuid.UUID) or self.telegram_chat_id is not None:
                raise ConversationError('invalid_context')
        else:
            raise ConversationError('invalid_context')
        if self.persona_id != 'arti' or self.topic_id != -1 or self.mode != 'default' or self.scene_id:
            raise ConversationError('unsupported_context')


def private_context(actor_id):
    return ContextRef('telegram_private', telegram_chat_id=user_id(actor_id))


def message_input(text, client_message_id, reply_to_id=None):
    if not isinstance(text, str) or not text.strip() or len(text) > 8000 or len(text.encode('utf-8')) > 32000 or '\x00' in text:
        raise ConversationError('invalid_message')
    key = identifier(client_message_id)
    reply = identifier(reply_to_id) if reply_to_id is not None else None
    digest = hashlib.sha256(json.dumps([text, str(reply) if reply else None], ensure_ascii=False).encode()).hexdigest()
    return key, reply, digest


def page_size(limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ConversationError('invalid_limit')
    return limit
