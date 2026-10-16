"""A person's own setting «Жизнь Арти в образе» (H6, P2).

Two values per Telegram user: whether Arti mentions her in-character life to
them (her pursuits block in the reply prompt, the in-character parts of the
Mini App room) and whether they have seen the explainer «Как живёт Арти».

This is an explicit settings source, not memory: only the person's own action
writes it (today the Mini App), never a chat message. Forgetting a message,
clearing the dialogue or rebuilding cognition does not touch it. It goes away
only by the person's own reset of the setting or by erasure of their data.
Without a row the defaults apply: mentions on, explainer not yet seen.
"""
from cognition.types import utc

DEFAULTS = {'mentions': True, 'explainer_seen': False}
SOURCES = ('miniapp',)


def public(row):
    if row is None:
        return dict(DEFAULTS)
    return {'mentions': row['mentions'], 'explainer_seen': row['explainer_seen_at'] is not None}


def person_of(event):
    """The person a reply is for: the author of the turn; a private chat is its person."""
    owner = event.evidence.owner_id
    if owner is None and event.context.chat_id > 0:
        owner = event.context.chat_id
    return owner


async def read(conn, user_id):
    return public(await conn.fetchrow(
        'SELECT mentions,explainer_seen_at FROM arti_life_settings WHERE user_id=$1', user_id))


async def mentions_enabled(pool, user_id):
    """True unless this person chose «Не упоминать». No person (a group turn without an author) keeps the default."""
    if user_id is None:
        return DEFAULTS['mentions']
    async with pool.acquire() as conn:
        value = await conn.fetchval('SELECT mentions FROM arti_life_settings WHERE user_id=$1', user_id)
    return DEFAULTS['mentions'] if value is None else value


async def update(conn, user_id, now, *, mentions=None, explainer_seen=None, source='miniapp'):
    """The person's own change; a value left as None stays as it is."""
    if source not in SOURCES:
        raise ValueError('invalid_settings_source')
    if mentions is None and explainer_seen is None:
        raise ValueError('empty_settings_change')
    row = await conn.fetchrow('''INSERT INTO arti_life_settings(user_id,mentions,explainer_seen_at,source,updated_at)
        VALUES($1,COALESCE($2,TRUE),CASE WHEN $3 THEN $4::timestamptz END,$5,$4)
        ON CONFLICT(user_id) DO UPDATE SET
            mentions=COALESCE($2,arti_life_settings.mentions),
            explainer_seen_at=CASE WHEN $3 IS NULL THEN arti_life_settings.explainer_seen_at
                                   WHEN $3 THEN COALESCE(arti_life_settings.explainer_seen_at,$4) END,
            source=$5,version=arti_life_settings.version+1,updated_at=$4
        RETURNING mentions,explainer_seen_at''', user_id, mentions, explainer_seen, utc(now), source)
    return public(row)


async def reset(conn, user_id):
    """The person's own reset: back to the defaults."""
    await conn.execute('DELETE FROM arti_life_settings WHERE user_id=$1', user_id)
    return dict(DEFAULTS)


async def erase(conn, user_id):
    """Part of erasing a person's data; the setting holds nothing else about them."""
    await conn.execute('DELETE FROM arti_life_settings WHERE user_id=$1', user_id)
