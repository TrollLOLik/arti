"""Validate raw Telegram initData, never a client-supplied user identity."""
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


class AuthError(ValueError):
    pass


def validate_init_data(raw, bot_token, *, now=None, max_age=3600):
    if not isinstance(raw, str) or not raw or len(raw) > 16384 or not bot_token:
        raise AuthError('missing_init_data')
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError('duplicate_fields')
        supplied = fields.pop('hash', '')
        if not re.fullmatch(r'[0-9a-fA-F]{64}', supplied):
            raise ValueError('invalid_hash')
        check = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
        secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied.lower()):
            raise ValueError('invalid_signature')
        age = (time.time() if now is None else now) - int(fields['auth_date'])
        if not -30 <= age <= max_age:
            raise ValueError('expired_init_data')
        user = json.loads(fields['user'])
        if not isinstance(user, dict) or type(user.get('id')) is not int or not 0 < user['id'] < 2**63 or user.get('is_bot'):
            raise ValueError('invalid_user')
        for field, bound in (('first_name', 128), ('username', 64)):
            if field in user and (not isinstance(user[field], str) or len(user[field]) > bound or '\x00' in user[field]):
                raise ValueError('invalid_profile')
        return {key: user[key] for key in ('id', 'first_name', 'username') if key in user}
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        raise AuthError('invalid_init_data') from exc
