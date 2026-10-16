"""Minimal disposable database without importing the bot/provider configuration."""
import os
import re
import uuid
from contextlib import asynccontextmanager

import asyncpg
from dotenv import dotenv_values

from conversations.schema import ensure_schema


@asynccontextmanager
async def isolated_database():
    if os.getenv('ARTI_TEST_DB') != '1':
        raise RuntimeError('Set ARTI_TEST_DB=1 to create a disposable Mini App database')
    cfg = {**dotenv_values('.env'), **os.environ}
    params = dict(host=cfg.get('DB_HOST', 'localhost'), port=int(cfg.get('DB_PORT', 5432)),
                  user=cfg.get('DB_USER', 'postgres'), password=cfg.get('DB_PASSWORD', ''), timeout=10)
    name = 'arti_miniapp_test_'+uuid.uuid4().hex
    assert re.fullmatch(r'arti_miniapp_test_[0-9a-f]{32}', name)
    control, pool, created = None, None, False
    try:
        control = await asyncpg.connect(database='postgres', **params)
        await control.execute(f'CREATE DATABASE "{name}"')
        created = True
        pool = await asyncpg.create_pool(database=name, min_size=1, max_size=5, **params)
        async with pool.acquire() as conn:
            assert await conn.fetchval('SELECT current_database()') == name
        await ensure_schema(pool)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        if created:
            await control.execute(f'DROP DATABASE "{name}"')
        if control is not None:
            await control.close()
