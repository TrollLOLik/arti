import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch


class FakePool:
    def __init__(self):
        self.close = AsyncMock()

    @asynccontextmanager
    async def acquire(self):
        yield object()


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_is_published_after_schema_completion(self):
        from database import connection
        pool = FakePool()
        started, release = asyncio.Event(), asyncio.Event()
        async def migrate(conn):
            started.set()
            await release.wait()
        old_pool, old_lock = connection._pool, connection._pool_init_lock
        connection._pool, connection._pool_init_lock = None, None
        try:
            with patch('database.connection.asyncpg.create_pool',new=AsyncMock(return_value=pool)) as create, patch('database.connection.create_tables',new=migrate):
                first = asyncio.create_task(connection.init_db())
                await started.wait()
                self.assertIsNone(connection._pool)
                second = asyncio.create_task(connection.init_db())
                release.set()
                await asyncio.gather(first,second)
                self.assertIs(connection._pool,pool)
                create.assert_awaited_once()
        finally:
            connection._pool, connection._pool_init_lock = old_pool, old_lock

    async def test_failed_schema_closes_unpublished_pool(self):
        from database import connection
        pool = FakePool()
        old_pool, old_lock = connection._pool, connection._pool_init_lock
        connection._pool, connection._pool_init_lock = None, None
        try:
            with patch('database.connection.asyncpg.create_pool',new=AsyncMock(return_value=pool)), patch('database.connection.create_tables',new=AsyncMock(side_effect=RuntimeError('synthetic migration failure'))):
                with self.assertRaises(RuntimeError):
                    await connection.init_db()
                self.assertIsNone(connection._pool)
                pool.close.assert_awaited_once()
        finally:
            connection._pool, connection._pool_init_lock = old_pool, old_lock
