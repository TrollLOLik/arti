"""Shared, content-free limits for optional initiative work and delivery.

These limits never grant consent. Ordinary requested replies and explicit
reminders do not use the unsolicited-delivery ledger. All timestamps are aware;
the user's timezone is read only from their explicit organizer setting.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROVIDER_CONCURRENCY = 2
PROVIDER_HOURLY_LIMIT = 60
PROVIDER_LEASE_SECONDS = 30
PROVIDER_ADMISSION_SECONDS = .35
OWNER_DAILY_LIMIT = 3
OWNER_SPACING_SECONDS = 3600
GLOBAL_BURST_LIMIT = 3


async def private_reason(conn, owner_id, now):
    """Fail closed when a personal timezone was never explicitly supplied."""
    name = await conn.fetchval('SELECT timezone FROM arti_organizer_preferences WHERE owner_id=$1 FOR SHARE', owner_id)
    if not name or now.tzinfo is None:
        return 'unknown_timezone'
    try:
        hour = now.astimezone(ZoneInfo(name)).hour
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return 'unknown_timezone'
    return 'quiet_hours' if hour >= 23 or hour < 9 else None


@asynccontextmanager
async def provider_slot(runtime, context_id, owner_id, kind, *, hourly_limit=24):
    """Try once, never queue behind proactive providers or a busy database.

Each actual assess/compose opportunity is charged, including failures and
abstentions. Leases recover after restart. A cancelled call releases its lease;
an unavailable SQL pool fails closed within the admission budget.
    """
    token = None
    try:
        async with asyncio.timeout(PROVIDER_ADMISSION_SECONDS):
            async with runtime.pool.acquire() as conn, conn.transaction():
                locked = await conn.fetchval("SELECT pg_try_advisory_xact_lock(hashtextextended('arti:initiative:providers',0))")
                if locked:
                    now = runtime.clock()
                    await conn.execute('DELETE FROM cognitive_initiative_calls WHERE started_at<$1 AND lease_until<=$2', now-timedelta(days=2), now)
                    counts = await conn.fetchrow('''SELECT count(*) AS total,
                        count(*) FILTER (WHERE context_id=$2 AND kind=$3) AS scoped,
                        count(*) FILTER (WHERE completed_at IS NULL AND lease_until>$4) AS active
                        FROM cognitive_initiative_calls WHERE started_at>$1''', now-timedelta(hours=1), context_id, kind, now)
                    if counts['total'] < PROVIDER_HOURLY_LIMIT and counts['scoped'] < max(0, hourly_limit) and counts['active'] < PROVIDER_CONCURRENCY:
                        token = uuid.uuid4().hex
                        await conn.execute('''INSERT INTO cognitive_initiative_calls
                            (token,context_id,owner_id,kind,started_at,lease_until) VALUES($1,$2,$3,$4,$5,$6)''',
                            token, context_id, owner_id, kind, now, now+timedelta(seconds=PROVIDER_LEASE_SECONDS))
    except (TimeoutError, OSError):
        token = None
    try:
        yield token is not None
    finally:
        if token is not None:
            async def release():
                async with runtime.pool.acquire() as conn:
                    await conn.execute('UPDATE cognitive_initiative_calls SET completed_at=$2 WHERE token=$1', token, runtime.clock())
            from utils.async_cleanup import await_owned
            try:
                await await_owned(release(), timeout=.5)
            except (TimeoutError, OSError):
                # The bounded lease remains conservative until it expires.
                pass


async def charge_delivery(conn, turn, delivery_key, now):
    """Atomically charge a final, validated outbox attempt on its SQL connection.

Call only inside the outbox transaction, after all source/policy guards. A
definite transport rejection is conservatively charged; retrying the exact
identity consumes no second slot. Unknown delivery is never treated as silence.
    """
    limits = getattr(turn, 'initiative', None)
    if limits is None:
        return True
    if not await conn.fetchval("SELECT pg_try_advisory_xact_lock(hashtextextended('arti:initiative:delivery',0))"):
        return False
    await conn.execute('DELETE FROM cognitive_initiative_charges WHERE charged_at<$1', now-timedelta(days=2))
    if await conn.fetchval('SELECT 1 FROM cognitive_initiative_charges WHERE context_id=$1 AND delivery_key=$2', turn.context_id, delivery_key):
        return True
    owner = limits.get('owner_id')
    rows = await conn.fetchrow('''SELECT
        count(*) FILTER (WHERE context_id=$2) AS context_count,
        max(charged_at) FILTER (WHERE context_id=$2) AS context_last,
        count(*) FILTER (WHERE owner_id=$3 AND $3::bigint IS NOT NULL) AS owner_count,
        max(charged_at) FILTER (WHERE owner_id=$3 AND $3::bigint IS NOT NULL) AS owner_last,
        count(*) FILTER (WHERE charged_at>$4) AS burst
        FROM cognitive_initiative_charges WHERE charged_at>$1''',
        now-timedelta(days=1), turn.context_id, owner, now-timedelta(minutes=1))
    if rows['context_count'] >= max(0, limits.get('context_daily', 2)) or rows['owner_count'] >= OWNER_DAILY_LIMIT or rows['burst'] >= GLOBAL_BURST_LIMIT:
        return False
    if rows['context_last'] and (now-rows['context_last']).total_seconds() < max(60, limits.get('spacing_seconds', 3600)):
        return False
    if rows['owner_last'] and (now-rows['owner_last']).total_seconds() < OWNER_SPACING_SECONDS:
        return False
    await conn.execute('INSERT INTO cognitive_initiative_charges(context_id,delivery_key,owner_id,charged_at) VALUES($1,$2,$3,$4)',
                       turn.context_id, delivery_key, owner, now)
    return True
