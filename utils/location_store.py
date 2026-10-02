"""Scoped location samples. Legacy user-only rows never establish consent."""
from datetime import datetime, timezone

from database.connection import get_db
from utils.location_scope import LOCATION_TTL_SECONDS


async def initialize(conn):
    await conn.execute('''CREATE TABLE IF NOT EXISTS arti_scoped_locations (
        chat_id BIGINT NOT NULL, topic_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
        lat DOUBLE PRECISION NOT NULL, lng DOUBLE PRECISION NOT NULL,
        city TEXT, address TEXT, live BOOLEAN NOT NULL DEFAULT FALSE,
        sample_id TEXT NOT NULL, received_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY(chat_id, topic_id, user_id)
    );
    CREATE INDEX IF NOT EXISTS arti_scoped_locations_expiry
        ON arti_scoped_locations(expires_at);
    DELETE FROM arti_scoped_locations WHERE expires_at <= NOW();
    ''')
    # Legacy rows have no origin consent. Do not migrate them; purge once their
    # original timestamp expires without disturbing timezone inference code.
    if await conn.fetchval("SELECT to_regclass('user_locations') IS NOT NULL"):
        await _expire_legacy(conn)


async def _expire_legacy(conn):
    await conn.execute('''DELETE FROM user_locations
        WHERE updated_at <= LOCALTIMESTAMP - make_interval(secs => $1)''',
        float(LOCATION_TTL_SECONDS))


async def expire():
    async with get_db() as conn:
        await conn.execute('DELETE FROM arti_scoped_locations WHERE expires_at <= NOW()')
        await _expire_legacy(conn)


async def save(key, sample):
    received_at = datetime.fromtimestamp(sample['timestamp'], timezone.utc)
    expires_at = datetime.fromtimestamp(sample['timestamp'] + LOCATION_TTL_SECONDS, timezone.utc)
    async with get_db() as conn:
        return await conn.fetchval('''INSERT INTO arti_scoped_locations
            (chat_id,topic_id,user_id,lat,lng,city,address,live,sample_id,received_at,expires_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT(chat_id,topic_id,user_id) DO UPDATE SET
                lat=EXCLUDED.lat,lng=EXCLUDED.lng,city=EXCLUDED.city,address=EXCLUDED.address,
                live=EXCLUDED.live,sample_id=EXCLUDED.sample_id,
                received_at=EXCLUDED.received_at,expires_at=EXCLUDED.expires_at
            WHERE arti_scoped_locations.received_at <= EXCLUDED.received_at
            RETURNING sample_id''', *key, sample['lat'], sample['lng'],
            sample.get('city'), sample.get('address'), sample['live'], sample['sample_id'],
            received_at, expires_at)


async def get(key):
    async with get_db() as conn:
        row = await conn.fetchrow('''SELECT * FROM arti_scoped_locations
            WHERE chat_id=$1 AND topic_id=$2 AND user_id=$3
                AND received_at <= NOW() AND expires_at > NOW()''', *key)
    if row is None:
        return None
    sample = dict(row)
    sample['timestamp'] = row['received_at'].timestamp()
    return sample


async def update_address(key, sample_id, *, city, address):
    async with get_db() as conn:
        await conn.execute('''UPDATE arti_scoped_locations SET city=$5,address=$6
            WHERE chat_id=$1 AND topic_id=$2 AND user_id=$3 AND sample_id=$4
                AND expires_at > NOW()''', *key, sample_id, city, address)
