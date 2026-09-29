"""Disposable PostgreSQL database; never initialize the configured bot database."""
import os
import re
import uuid
from contextlib import asynccontextmanager

import asyncpg
from dotenv import dotenv_values


@asynccontextmanager
async def isolated_database():
    from database import connection

    if connection._pool is not None:
        raise RuntimeError("Tests must run in a separate process without a bot pool")
    cfg = {**dotenv_values('.env'), **os.environ}
    # Legacy config eagerly creates provider clients. Tests use dummy credentials
    # and mock every provider method; a missing mock cannot consume a real key.
    os.environ['GROQ_API_KEY'] = 'offline-test-placeholder'
    os.environ['GEMINI_API_KEY'] = 'offline-test-placeholder'
    params = dict(host=cfg.get('DB_HOST', 'localhost'), port=int(cfg.get('DB_PORT', 5432)),
                  user=cfg.get('DB_USER', 'postgres'), password=cfg.get('DB_PASSWORD', ''), timeout=10)
    name = 'arti_cognition_test_' + uuid.uuid4().hex
    assert re.fullmatch(r'arti_cognition_test_[0-9a-f]{32}', name)
    control = await asyncpg.connect(database=cfg.get('DB_NAME', 'arti_bot'), **params)
    created = False
    try:
        await control.execute(f'CREATE DATABASE "{name}"')
        created = True
        pool = await asyncpg.create_pool(database=name, min_size=1, max_size=5, **params)
        connection._pool = pool
        async with pool.acquire() as conn:
            assert await conn.fetchval('SELECT current_database()') == name
            await connection.create_tables(conn)
        yield pool
    finally:
        if connection._pool is not None:
            await connection._pool.close()
            connection._pool = None
        if created:
            # The exact name was generated above, checked, and never supplied by a caller.
            await control.execute(f'DROP DATABASE "{name}"')
        await control.close()

