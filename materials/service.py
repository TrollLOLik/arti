"""Intake and extraction outside DB transactions, with final generation checks."""
import asyncio
from hashlib import sha256
from materials.repository import MaterialRepository
from materials.storage import LocalBlobStore
from materials.validation import inspect_bytes


class MaterialService:
    def __init__(self, repository, store):
        self.repository, self.store = repository, store

    async def ingest(self, data, filename, actor, source_key, source_id, declared_mime=None):
        mime = await asyncio.to_thread(inspect_bytes, data, filename, declared_mime, self.repository.quotas.max_file_bytes)
        key = await self.repository.reserve_blob(actor)
        retained = None
        try:
            await asyncio.to_thread(self.store.put, data, key)
            row, retained = await self.repository.register(actor, source_key, source_id, filename, key, sha256(data).hexdigest(), len(data), mime)
            return row
        finally:
            # A cancelled/unknown COMMIT may already reference this blob: check it
            # before deleting. If the DB is down, orphan maintenance will recover.
            if retained != key:
                try:
                    async with self.repository.pool.acquire() as conn:
                        registered = await conn.fetchval('SELECT 1 FROM material_blobs WHERE id=$1', key)
                    if not registered:
                        await asyncio.to_thread(self.store.delete, key)
                    async with self.repository.pool.acquire() as conn:
                        await conn.execute('DELETE FROM material_blob_reservations WHERE id=$1', key)
                except Exception:
                    pass
            else:
                async with self.repository.pool.acquire() as conn:
                    await conn.execute('DELETE FROM material_blob_reservations WHERE id=$1', key)

    async def revise(self, aid, data, filename, actor, expected_version, declared_mime=None):
        mime = await asyncio.to_thread(inspect_bytes, data, filename, declared_mime, self.repository.quotas.max_file_bytes)
        key = await self.repository.reserve_blob(actor)
        try:
            await asyncio.to_thread(self.store.put, data, key)
            version, blob = await self.repository.revise(aid, actor, expected_version, key, sha256(data).hexdigest(), len(data), mime)
            return version
        finally:
            try:
                async with self.repository.pool.acquire() as conn:
                    registered = await conn.fetchval('SELECT 1 FROM material_blobs WHERE id=$1', key)
                    if not registered:
                        await asyncio.to_thread(self.store.delete, key)
                    await conn.execute('DELETE FROM material_blob_reservations WHERE id=$1', key)
            except Exception:
                pass

    async def read_bytes(self, aid, actor, version=None):
        row, rev = await self.repository.read(aid, actor, version)
        data = await asyncio.to_thread(self.store.read, rev['blob_id'], rev['sha256'], self.repository.quotas.max_file_bytes)
        await self.repository.read(aid, actor, rev['version'])
        return row, rev, data

    async def extract(self, aid, actor, extractor):
        cached = await self.repository.extraction(aid, actor, getattr(extractor, 'cache_version', extractor.version))
        if cached:
            return cached
        row, rev, data = await self.read_bytes(aid, actor)
        bundle = await asyncio.to_thread(extractor.extract, aid, rev['version'], data, rev['mime'])
        eid = await self.repository.save_extraction(actor, bundle, row['generation'])
        return eid, bundle
