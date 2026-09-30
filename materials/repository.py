"""PostgreSQL boundary: scoped access, quotas, version CAS and erasure barriers."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import uuid
from materials.types import AccessContext, ExtractionBundle, MaterialError, canonical


@dataclass(frozen=True)
class Quotas:
    max_file_bytes: int = 10 * 1024 * 1024
    max_scope_bytes: int = 200 * 1024 * 1024
    max_user_bytes: int = 500 * 1024 * 1024
    max_scope_assets: int = 200
    retention_days: int = 30

    def __post_init__(self):
        if any(v <= 0 for v in asdict(self).values()):
            raise MaterialError('invalid_quota')


class MaterialRepository:
    def __init__(self, pool, quotas=None):
        self.pool = pool
        self.quotas = quotas or Quotas()

    async def reserve_blob(self, actor):
        key = uuid.uuid4().hex
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO material_blob_reservations VALUES($1,$2,NOW()+INTERVAL '5 minutes')", key, actor.realm)
        return key

    async def _reservation(self, conn, actor, key):
        valid = await conn.fetchval('SELECT 1 FROM material_blob_reservations WHERE id=$1 AND realm=$2 AND expires_at>NOW() FOR UPDATE', key, actor.realm)
        if not valid:
            raise MaterialError('upload_reservation_expired')

    async def _locks(self, conn, actor):
        for key in sorted({actor.realm, 'owner:' + (str(actor.user_id) if actor.user_id is not None else actor.sender_ref)}):
            await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1)::bigint)', 'materials:' + key)

    @staticmethod
    def check(row, actor, edit=False):
        if row is None or row['scope_key'] != actor.scope.key:
            raise MaterialError('material_unavailable')
        if actor.scope.chat_type == 'private' and row['owner_id'] != actor.user_id:
            raise MaterialError('material_unavailable')
        if edit and (row['owner_id'] != actor.user_id or row['sender_ref'] != actor.sender_ref):
            raise MaterialError('material_edit_denied')
        if row['erased_at'] is not None or row['expires_at'] <= datetime.now(timezone.utc):
            raise MaterialError('material_erased_or_expired')

    async def _source_allowed(self, conn, actor, source_id, *, owner_id=...,check_shares=True):
        source_owner=actor.user_id if owner_id is ... else owner_id
        suppressed = await conn.fetchval('''SELECT 1 FROM material_source_tombstones
            WHERE scope_key=$1 AND owner_id IS NOT DISTINCT FROM $2 AND source_id=$3''', actor.scope.identity_key, source_owner, source_id)
        if not suppressed:
            suppressed = await conn.fetchval('''SELECT 1 FROM cognitive_events e JOIN cognitive_contexts c ON c.id=e.context_id
                WHERE c.persona_id=$1 AND c.chat_id=$2 AND c.topic_id=$3 AND c.mode=$4 AND c.scene_id=$5
                AND e.source_id=$6 AND e.owner_id IS NOT DISTINCT FROM $7 AND e.suppressed_at IS NOT NULL''',
                actor.scope.persona_id, actor.scope.chat_id, actor.scope.topic_id, actor.scope.mode, actor.scope.scene_id, source_id, source_owner)
        if suppressed:
            raise MaterialError('source_erased')
        if check_shares:
            rows=await conn.fetch('''WITH RECURSIVE origins(id) AS (
                SELECT s.source_asset_id FROM material_shares s JOIN material_assets target ON target.id=s.target_asset_id
                    WHERE target.realm=$1 AND target.source_id=$2
                UNION SELECT s.source_asset_id FROM material_shares s JOIN origins o ON s.target_asset_id=o.id)
                SELECT a.* FROM material_assets a JOIN origins o ON o.id=a.id LIMIT 33''',actor.realm,source_id)
            if len(rows)>32: raise MaterialError('share_chain_budget')
            from materials.types import MaterialScope,AccessContext
            for origin in rows:
                if origin['erased_at'] is not None or origin['expires_at']<=datetime.now(timezone.utc): raise MaterialError('shared_source_unavailable')
                scope=json.loads(origin['scope']) if isinstance(origin['scope'],str) else origin['scope']
                original_actor=AccessContext(MaterialScope(**scope),origin['owner_id'],origin['sender_ref'])
                await self._source_allowed(conn,original_actor,origin['source_id'],owner_id=origin['owner_id'],check_shares=False)

    async def _quota(self, conn, actor, size, new_asset):
        values = await conn.fetchrow('''SELECT COUNT(DISTINCT a.id) AS assets, COALESCE(SUM(v.byte_size),0) AS bytes
            FROM material_assets a JOIN material_asset_versions v ON v.asset_id=a.id
            WHERE a.realm=$1 AND a.erased_at IS NULL AND a.expires_at>NOW()''', actor.realm)
        if values['bytes'] + size > self.quotas.max_scope_bytes or new_asset and values['assets'] >= self.quotas.max_scope_assets:
            raise MaterialError('scope_quota')
        used = await conn.fetchval('''SELECT COALESCE(SUM(v.byte_size),0) FROM material_assets a
            JOIN material_asset_versions v ON v.asset_id=a.id WHERE a.erased_at IS NULL AND a.expires_at>NOW()
            AND a.owner_id IS NOT DISTINCT FROM $1 AND ($1::bigint IS NOT NULL OR a.sender_ref=$2)''', actor.user_id, actor.sender_ref)
        if used + size > self.quotas.max_user_bytes:
            raise MaterialError('owner_quota')

    async def _blob(self, conn, actor, key, digest, size, mime):
        return await conn.fetchval('''INSERT INTO material_blobs(id,realm,sha256,byte_size,mime)
            VALUES($1,$2,$3,$4,$5) ON CONFLICT(realm,sha256) DO UPDATE SET sha256=EXCLUDED.sha256 RETURNING id''',
            key, actor.realm, digest, size, mime)

    async def register(self, actor, source_key, source_id, filename, key, digest, size, mime,*,share=None):
        if not source_key or not source_id or len(source_key) > 512 or len(source_id) > 512 or size > self.quotas.max_file_bytes:
            raise MaterialError('invalid_asset')
        async with self.pool.acquire() as conn, conn.transaction():
            await self._locks(conn, actor)
            await self._reservation(conn, actor, key)
            await self._source_allowed(conn, actor, source_id)
            if share:
                original_actor,grant=share
                original=await conn.fetchrow('SELECT * FROM material_assets WHERE id=$1 FOR UPDATE',grant.source_asset_id)
                self.check(original,original_actor,edit=True)
                await self._source_allowed(conn,original_actor,original['source_id'],owner_id=original['owner_id'])
                revision=await conn.fetchrow('SELECT * FROM material_asset_versions WHERE asset_id=$1 AND version=$2',grant.source_asset_id,grant.source_version)
                if revision is None: raise MaterialError('share_source_changed')
                grant.validate(original_actor,actor,original,revision)
                depth=await conn.fetchval('''WITH RECURSIVE ancestors(id) AS (SELECT $1::text
                    UNION SELECT s.source_asset_id FROM material_shares s JOIN ancestors a ON s.target_asset_id=a.id)
                    SELECT count(*) FROM ancestors''',grant.source_asset_id)
                if depth>32: raise MaterialError('share_chain_budget')
                if revision['sha256']!=digest or revision['mime']!=mime: raise MaterialError('share_content_changed')
            row = await conn.fetchrow('SELECT * FROM material_assets WHERE realm=$1 AND source_key=$2 FOR UPDATE', actor.realm, source_key)
            if row:
                self.check(row, actor, edit=True)
                if share and not await conn.fetchval('SELECT 1 FROM material_shares WHERE target_asset_id=$1 AND source_asset_id=$2 AND source_version=$3 AND authorized_by=$4 AND request_id=$5 AND destination_scope=$6',row['id'],grant.source_asset_id,grant.source_version,grant.author_id,grant.request_id,grant.destination_scope_key): raise MaterialError('share_identity_conflict')
                version = await conn.fetchrow('SELECT * FROM material_asset_versions WHERE asset_id=$1 AND version=1', row['id'])
                if version['sha256'] != digest or version['mime'] != mime or row['source_id'] != source_id:
                    raise MaterialError('source_identity_conflict')
                return dict(row), version['blob_id']
            await self._quota(conn, actor, size, True)
            blob = await self._blob(conn, actor, key, digest, size, mime)
            aid = uuid.uuid4().hex
            row = await conn.fetchrow('''INSERT INTO material_assets(id,realm,scope_key,identity_key,scope,owner_id,sender_ref,source_id,source_key,filename,expires_at)
                VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,NOW()+make_interval(days=>$11)) RETURNING *''',
                aid, actor.realm, actor.scope.key, actor.scope.identity_key, canonical(asdict(actor.scope)), actor.user_id, actor.sender_ref,
                source_id, source_key, filename[:256], self.quotas.retention_days)
            await conn.execute('INSERT INTO material_asset_versions(asset_id,version,blob_id,sha256,byte_size,mime) VALUES($1,1,$2,$3,$4,$5)', aid, blob, digest, size, mime)
            if share:
                await conn.execute('INSERT INTO material_shares(target_asset_id,source_asset_id,source_version,authorized_by,request_id,destination_scope) VALUES($1,$2,$3,$4,$5,$6)',aid,grant.source_asset_id,grant.source_version,grant.author_id,grant.request_id,grant.destination_scope_key)
            return dict(row), blob

    async def revise(self, aid, actor, expected_version, key, digest, size, mime):
        if size > self.quotas.max_file_bytes:
            raise MaterialError('file_size_limit')
        async with self.pool.acquire() as conn, conn.transaction():
            await self._locks(conn, actor)
            await self._reservation(conn, actor, key)
            row = await conn.fetchrow('SELECT * FROM material_assets WHERE id=$1 FOR UPDATE', aid)
            self.check(row, actor, edit=True)
            await self._source_allowed(conn, actor, row['source_id'],owner_id=row['owner_id'])
            if row['current_version'] != expected_version:
                raise MaterialError('stale_asset_version')
            await self._quota(conn, actor, size, False)
            blob = await self._blob(conn, actor, key, digest, size, mime)
            new_version = expected_version + 1
            await conn.execute('INSERT INTO material_asset_versions(asset_id,version,blob_id,sha256,byte_size,mime) VALUES($1,$2,$3,$4,$5,$6)', aid, new_version, blob, digest, size, mime)
            await conn.execute('UPDATE material_assets SET current_version=$2,generation=generation+1 WHERE id=$1', aid, new_version)
            await conn.execute('''UPDATE material_derivatives SET invalidated_at=NOW() WHERE id IN
                (SELECT derivative_id FROM material_dependencies WHERE asset_id=$1)''', aid)
            return new_version, blob

    async def read(self, aid, actor, version=None):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT * FROM material_assets WHERE id=$1', aid)
            self.check(row, actor)
            await self._source_allowed(conn, actor, row['source_id'],owner_id=row['owner_id'])
            value = await conn.fetchrow('SELECT * FROM material_asset_versions WHERE asset_id=$1 AND version=$2', aid, version or row['current_version'])
            if value is None or value['blob_id'] is None:
                raise MaterialError('version_unavailable')
            return dict(row), dict(value)

    async def save_extraction(self, actor, bundle, generation):
        payload = canonical(bundle.to_dict())
        digest = sha256(payload.encode()).hexdigest()
        async with self.pool.acquire() as conn, conn.transaction():
            await self._locks(conn, actor)
            row = await conn.fetchrow('SELECT * FROM material_assets WHERE id=$1 FOR UPDATE', bundle.asset_id)
            self.check(row, actor)
            await self._source_allowed(conn, actor, row['source_id'],owner_id=row['owner_id'])
            if row['generation'] != generation or row['current_version'] != bundle.asset_version:
                raise MaterialError('stale_extraction')
            existing = await conn.fetchrow('SELECT * FROM material_extractions WHERE asset_id=$1 AND asset_version=$2 AND extractor=$3', bundle.asset_id, bundle.asset_version, bundle.extractor)
            if existing:
                if existing['sha256'] != digest or existing['payload'] is None:
                    raise MaterialError('extractor_version_conflict')
                return existing['id']
            eid = uuid.uuid4().hex
            await conn.execute('''INSERT INTO material_extractions(id,asset_id,asset_version,extractor,payload,sha256,generation)
                VALUES($1,$2,$3,$4,$5::jsonb,$6,$7)''', eid, bundle.asset_id, bundle.asset_version, bundle.extractor, payload, digest, generation)
            return eid

    async def extraction(self, aid, actor, extractor, version=None):
        row, revision = await self.read(aid, actor, version)
        async with self.pool.acquire() as conn:
            result = await conn.fetchrow('SELECT * FROM material_extractions WHERE asset_id=$1 AND asset_version=$2 AND extractor=$3 AND payload IS NOT NULL', aid, revision['version'], extractor)
        if result is None:
            return None
        payload = result['payload']
        value=json.loads(payload) if isinstance(payload,str) else payload
        if sha256(canonical(value).encode()).hexdigest()!=result['sha256']: raise MaterialError('extraction_integrity_failure')
        await self.read(aid,actor,revision['version'])
        return result['id'], ExtractionBundle.from_dict(value)

    async def resolve(self, ref, actor):
        bundle=await self.evidence_bundle(ref,actor)
        block = next((b for b in bundle.blocks if b.block_id == ref.block_id and b.locator == ref.locator), None)
        if block is None:
            raise MaterialError('evidence_locator_mismatch')
        await self.read(ref.asset_id, actor, ref.asset_version)
        return block

    async def evidence_bundle(self,ref,actor):
        await self.read(ref.asset_id, actor, ref.asset_version)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT payload,sha256 FROM material_extractions WHERE id=$1 AND asset_id=$2 AND asset_version=$3 AND payload IS NOT NULL', ref.extraction_id, ref.asset_id, ref.asset_version)
        if row is None:
            raise MaterialError('evidence_unavailable')
        value = json.loads(row['payload']) if isinstance(row['payload'], str) else row['payload']
        if sha256(canonical(value).encode()).hexdigest()!=row['sha256']: raise MaterialError('extraction_integrity_failure')
        bundle = ExtractionBundle.from_dict(value)
        # A second barrier prevents returning a result invalidated while loading.
        await self.read(ref.asset_id, actor, ref.asset_version)
        return bundle

    async def own_search(self, actor, query, limit=5):
        if not query.strip() or not 1 <= limit <= 20:
            return []
        async with self.pool.acquire() as conn:
            return await conn.fetch('''SELECT DISTINCT a.id,a.filename FROM material_assets a LEFT JOIN material_extractions e ON e.asset_id=a.id
                WHERE a.scope_key=$1 AND a.owner_id=$2 AND a.erased_at IS NULL AND a.expires_at>NOW()
                AND (strpos(lower(a.filename),lower($3))>0 OR strpos(lower(e.payload::text),lower($3))>0) ORDER BY a.id LIMIT $4''',
                actor.scope.key, actor.user_id, query[:512], limit)
