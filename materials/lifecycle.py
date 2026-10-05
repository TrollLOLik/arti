"""Erasure is a DB barrier first; physical deletion is resumable maintenance."""
from materials.repository import MaterialRepository
from materials.types import MaterialError


async def erase_locked(conn, ids):
    if not ids:
        return 0
    # Match durable checkpoint lock order: assets before request rows.
    await conn.fetch('SELECT id FROM material_assets WHERE id=ANY($1::text[]) ORDER BY id FOR UPDATE', ids)
    from bot.request_store import RequestStore
    await RequestStore(None).erase_materials(ids, conn)
    await conn.execute('UPDATE material_assets SET erased_at=COALESCE(erased_at,NOW()),generation=generation+1,filename=\'\' WHERE id=ANY($1::text[])', ids)
    await conn.execute('UPDATE material_extractions SET payload=NULL WHERE asset_id=ANY($1::text[])', ids)
    await conn.execute('''UPDATE material_derivatives SET payload=NULL,invalidated_at=NOW() WHERE id IN
        (SELECT derivative_id FROM material_dependencies WHERE asset_id=ANY($1::text[]))''', ids)
    await conn.execute('UPDATE material_asset_versions SET blob_id=NULL WHERE asset_id=ANY($1::text[])', ids)
    await conn.execute('''INSERT INTO material_cognitive_cleanup(asset_id)
        SELECT id FROM material_assets WHERE id=ANY($1::text[]) AND owner_id IS NOT NULL ON CONFLICT DO NOTHING''', ids)
    return len(ids)


async def forget_sources(pool, scope_key, owner, sources):
    """Also tombstone sources that have not finished uploading yet."""
    if owner is None or not sources:
        return 0
    async with pool.acquire() as conn, conn.transaction():
        # Same scope/owner ordering as intake prevents quota/deletion deadlocks.
        for key in sorted({'owner:' + str(owner), scope_key}):
            await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1)::bigint)', 'materials:' + key)
        await conn.executemany('INSERT INTO material_source_tombstones(scope_key,owner_id,source_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING', [(scope_key, owner, s) for s in set(sources)])
        rows = await conn.fetch('SELECT id FROM material_assets WHERE identity_key=$1 AND owner_id=$2 AND source_id=ANY($3::text[]) FOR UPDATE', scope_key, owner, list(sources))
        total = await erase_locked(conn, [r['id'] for r in rows])
    from materials.runtime import invalidate_pending
    invalidate_pending({r['id'] for r in rows})
    return total


class MaterialLifecycle:
    def __init__(self, repository, store):
        self.repository, self.store = repository, store

    async def forget(self, aid, actor):
        async with self.repository.pool.acquire() as conn, conn.transaction():
            await self.repository._locks(conn, actor)
            row = await conn.fetchrow('SELECT * FROM material_assets WHERE id=$1', aid)
            MaterialRepository.check(row, actor, edit=True)
            from organizer.ownership import lock_material_source_context
            await lock_material_source_context(conn,row['owner_id'],row['identity_key'],row['source_id'])
            row = await conn.fetchrow('SELECT * FROM material_assets WHERE id=$1 FOR UPDATE', aid)
            MaterialRepository.check(row, actor, edit=True)
            if row['owner_id'] is not None:
                await conn.execute('INSERT INTO material_source_tombstones(scope_key,owner_id,source_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING', row['identity_key'], row['owner_id'], row['source_id'])
            await erase_locked(conn, [aid])
        from materials.runtime import invalidate_pending
        invalidate_pending({aid})
        await self._forget_cognition(row)

    async def _forget_cognition(self, row):
        if row['owner_id'] is not None:
            # Existing cognition provenance removes delivered derivatives and
            # legacy projections as well; material erasure remains the first fence.
            import json
            scope = json.loads(row['scope']) if isinstance(row['scope'], str) else row['scope']
            async with self.repository.pool.acquire() as conn:
                cids = await conn.fetch('''SELECT c.id FROM cognitive_contexts c JOIN cognitive_events e ON e.context_id=c.id
                    WHERE c.persona_id=$1 AND c.chat_id=$2 AND c.topic_id=$3 AND c.mode=$4 AND c.scene_id=$5
                    AND e.source_id=$6 AND e.owner_id=$7 AND e.suppressed_at IS NULL''',
                    scope['persona_id'],scope['chat_id'],scope['topic_id'],scope['mode'],scope['scene_id'],row['source_id'],row['owner_id'])
            from cognition.forgetting import forget_cognitive_sources
            for context in cids:
                await forget_cognitive_sources(self.repository.pool,context['id'],row['owner_id'],[row['source_id']])
        async with self.repository.pool.acquire() as conn:
            await conn.execute('DELETE FROM material_cognitive_cleanup WHERE asset_id=$1', row['id'])

    async def collect(self, limit=100):
        if not 1 <= limit <= 1000:
            raise MaterialError('invalid_gc_limit')
        async with self.repository.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('materials:gc')::bigint)")
            rows = await conn.fetch('SELECT * FROM material_assets WHERE erased_at IS NULL AND expires_at<=NOW() ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT $1', limit)
            await erase_locked(conn, [r['id'] for r in rows])
            # Registration locks the dedup row; GC's row lock serializes reuse.
            blobs = await conn.fetch('''SELECT b.id FROM material_blobs b WHERE NOT EXISTS
                (SELECT 1 FROM material_asset_versions v WHERE v.blob_id=b.id) FOR UPDATE OF b SKIP LOCKED LIMIT $1''', limit)
            for blob in blobs:
                self.store.delete(blob['id'])
                await conn.execute('DELETE FROM material_blobs WHERE id=$1', blob['id'])
            abandoned = await conn.fetch('SELECT id FROM material_blob_reservations WHERE expires_at<=NOW() FOR UPDATE SKIP LOCKED LIMIT $1', limit)
            for upload in abandoned:
                if not await conn.fetchval('SELECT 1 FROM material_blobs WHERE id=$1', upload['id']):
                    self.store.delete(upload['id'])
                await conn.execute('DELETE FROM material_blob_reservations WHERE id=$1', upload['id'])
            orphans = 0
            for key in self.store.old_keys()[:limit]:
                protected = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM material_blobs WHERE id=$1)
                    OR EXISTS(SELECT 1 FROM material_blob_reservations WHERE id=$1)''', key)
                if not protected:
                    self.store.delete(key)
                    orphans += 1
        from materials.runtime import invalidate_pending
        invalidate_pending({r['id'] for r in rows})
        async with self.repository.pool.acquire() as conn:
            cleanup = await conn.fetch('''SELECT a.* FROM material_cognitive_cleanup q JOIN material_assets a ON a.id=q.asset_id
                ORDER BY q.created_at LIMIT $1''', limit)
        for row in cleanup:
            await self._forget_cognition(row)
        return dict(expired=len(rows), deleted_blobs=len(blobs), abandoned_uploads=len(abandoned), orphans=orphans, cognitive_cleanups=len(cleanup))
