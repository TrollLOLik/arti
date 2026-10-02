"""Location consent belongs to the receiving user, chat and observed topic."""
import time

from cognition.scope import CURRENT_SCOPE

LOCATION_TTL_SECONDS = 30 * 60


def location_scope_key(user_id, *, chat_id=None, scope=None):
    scope = CURRENT_SCOPE.get() if scope is None else scope
    if (scope is None or type(user_id) is not int or user_id <= 0
            or scope.user_id != user_id or scope.sender_kind != 'user'
            or (chat_id is not None and scope.chat_id != chat_id)):
        return None
    if scope.chat_type == 'private':
        topic_id = -1
    elif scope.group and scope.topic_id >= 0:
        topic_id = scope.topic_id
    else:
        # An unknown historic scope cannot establish permission to share.
        return None
    return (scope.chat_id, topic_id, user_id)


def _pending_key(user_id, *, chat_id=None, scope=None, mode='default'):
    key = location_scope_key(user_id, chat_id=chat_id, scope=scope)
    if key is None or mode not in ('default', 'rp'):
        return None
    return ('map', *key, mode)


def expire_pending_map_requests():
    from config import pending_map_requests
    now = time.time()
    for key, value in list(pending_map_requests.items()):
        if (not isinstance(key, tuple) or len(key) != 5 or key[0] != 'map'
                or not isinstance(value, dict)
                or not isinstance(value.get('prompt'), str) or not value['prompt'].strip()
                or not isinstance(value.get('created_at'), (int, float))
                or not 0 <= now - value['created_at'] < LOCATION_TTL_SECONDS):
            pending_map_requests.pop(key, None)


def set_pending_map_request(user_id, prompt, *, chat_id=None, scope=None, mode='default'):
    from config import pending_map_requests
    expire_pending_map_requests()
    key = _pending_key(user_id, chat_id=chat_id, scope=scope, mode=mode)
    if key is None or not isinstance(prompt, str) or not prompt.strip():
        return False
    pending_map_requests[key] = {'prompt': prompt, 'created_at': time.time()}
    return True


def pop_pending_map_request(user_id, *, chat_id=None, scope=None, mode='default'):
    from config import pending_map_requests
    expire_pending_map_requests()
    key = _pending_key(user_id, chat_id=chat_id, scope=scope, mode=mode)
    pending = pending_map_requests.pop(key, None) if key is not None else None
    return pending['prompt'] if pending else None
