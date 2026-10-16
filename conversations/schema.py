"""Standalone foundation migration runner, compatible with cognition's checksum ledger."""
import hashlib
from pathlib import Path


async def ensure_schema(pool):
    # Standalone tests/HTTP bootstrap never import config or construct AI clients.
    # The bot's normal migration runner discovers this same additive migration.
    for name in ('055_miniapp_conversations.sql', '056_miniapp_live_chat.sql'):
        await _apply(pool, name)


async def _apply(pool, name):
    path = Path(__file__).resolve().parents[1] / 'cognition/migrations' / name
    sql = path.read_text(encoding='utf-8')
    checksum = hashlib.sha256(sql.encode()).hexdigest()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('arti_cognitive_schema')::bigint)")
        await conn.execute('''CREATE TABLE IF NOT EXISTS cognitive_schema_migrations (
            name TEXT PRIMARY KEY,sha256 TEXT NOT NULL,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        previous = await conn.fetchval('SELECT sha256 FROM cognitive_schema_migrations WHERE name=$1', path.name)
        if previous is not None:
            if previous != checksum:
                raise ValueError('An applied migration changed; add a migration')
            return
        await conn.execute(sql)
        await conn.execute('INSERT INTO cognitive_schema_migrations(name,sha256) VALUES($1,$2)', path.name, checksum)
