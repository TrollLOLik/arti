"""Opaque revocable sessions. Only a SHA-256 cookie digest reaches PostgreSQL."""
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets

from .auth import AuthError


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def csrf(token):
    return hashlib.sha256(('arti-csrf-v1:'+token).encode()).hexdigest()


def valid_token(token):
    return isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', token) is not None


def check_csrf(token, supplied):
    if not valid_token(token) or not isinstance(supplied, str) or not hmac.compare_digest(csrf(token), supplied):
        raise AuthError('invalid_csrf')


class SessionStore:
    def __init__(self, pool):
        self.pool = pool

    async def create(self, user, expires_at):
        token = secrets.token_urlsafe(32)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute('DELETE FROM app_sessions WHERE expires_at<=now() OR revoked_at IS NOT NULL')
            await conn.execute('INSERT INTO app_sessions(token_hash,user_id,expires_at) VALUES($1,$2,$3)', digest(token), user['id'], expires_at)
        return token

    async def read(self, token):
        if not valid_token(token):
            raise AuthError('missing_session')
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('''SELECT s.expires_at,u.id,u.first_name,u.username FROM app_sessions s
                JOIN app_users u ON u.id=s.user_id WHERE s.token_hash=$1 AND s.revoked_at IS NULL AND s.expires_at>now()''', digest(token))
        if row is None:
            raise AuthError('expired_session')
        return dict(user={k: row[k] for k in ('id', 'first_name', 'username')}, expires_at=row['expires_at'])

    async def revoke(self, token):
        if valid_token(token):
            async with self.pool.acquire() as conn:
                await conn.execute('UPDATE app_sessions SET revoked_at=now() WHERE token_hash=$1', digest(token))


class DemoSessions:
    def __init__(self):
        self.rows = OrderedDict()

    async def create(self, user, expires_at):
        now = datetime.now(timezone.utc)
        self.rows = OrderedDict((k, v) for k, v in self.rows.items() if v['expires_at'] > now)
        if len(self.rows) >= 1000:
            raise RuntimeError('preview_session_limit')
        token = secrets.token_urlsafe(32)
        self.rows[digest(token)] = dict(user=user, expires_at=expires_at, demo=dict(items=[], actions={}))
        return token

    async def read(self, token):
        if not valid_token(token):
            raise AuthError('missing_session')
        row = self.rows.get(digest(token))
        if row is None or row['expires_at'] <= datetime.now(timezone.utc):
            raise AuthError('expired_session')
        return row

    async def revoke(self, token):
        if valid_token(token):
            self.rows.pop(digest(token), None)
