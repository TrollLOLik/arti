"""Scoped immutable derivatives, shared by datasets, observations and artifacts."""
from dataclasses import asdict
from hashlib import sha256
import json
from materials.types import MaterialError,canonical


class DerivativeRepository:
    def __init__(self,materials): self.materials=materials; self.pool=materials.pool

    async def _sources(self,conn,actor,refs,*,edit=False):
        versions={}
        for ref in refs:
            key=(ref['asset_id'],ref['asset_version']) if isinstance(ref,dict) else (ref.asset_id,ref.asset_version)
            versions[key]=True
        if not versions: raise MaterialError('derivative_without_sources')
        for aid,version in sorted(versions):
            row=await conn.fetchrow('SELECT * FROM material_assets WHERE id=$1 FOR UPDATE',aid)
            self.materials.check(row,actor,edit=edit)
            await self.materials._source_allowed(conn,actor,row['source_id'],owner_id=row['owner_id'])
            if row['current_version']!=version: raise MaterialError('stale_dataset_source')
        return sorted({aid for aid,_ in versions})

    async def _payload(self,conn,id,actor,kind):
        row=await conn.fetchrow('SELECT * FROM material_derivatives WHERE id=$1',id)
        if row is None or row['realm']!=actor.realm or (kind is not None and row['kind']!=kind) or row['payload'] is None or row['invalidated_at'] is not None:
            raise MaterialError('derivative_unavailable')
        payload=json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
        if sha256(canonical(payload).encode()).hexdigest()!=row['sha256']: raise MaterialError('derivative_integrity_failure')
        return payload

    async def _insert(self,conn,id,actor,kind,payload,assets,inputs=()):
        serialized=canonical(payload)
        if len(serialized.encode())>4*1024**2: raise MaterialError('derivative_payload_budget')
        digest=sha256(serialized.encode()).hexdigest()
        old=await conn.fetchrow('SELECT * FROM material_derivatives WHERE id=$1',id)
        if old:
            if old['realm']!=actor.realm or old['sha256']!=digest or old['invalidated_at'] is not None or old['payload'] is None:
                raise MaterialError('derivative_identity_conflict')
            return
        if await conn.fetchval('SELECT COUNT(*) FROM material_derivatives WHERE realm=$1 AND payload IS NOT NULL',actor.realm)>=2000:
            raise MaterialError('derivative_scope_quota')
        await conn.execute('INSERT INTO material_derivatives(id,kind,payload,realm,sha256) VALUES($1,$2,$3::jsonb,$4,$5)',id,kind,serialized,actor.realm,digest)
        await conn.executemany('INSERT INTO material_dependencies(derivative_id,asset_id) VALUES($1,$2)',[(id,aid) for aid in assets])
        if inputs: await conn.executemany('INSERT INTO material_derivative_links(derivative_id,input_id) VALUES($1,$2)',[(id,other) for other in inputs])

    async def _invalidate(self,conn,id):
        await conn.execute('''WITH RECURSIVE dependent(id) AS (
            SELECT derivative_id FROM material_derivative_links WHERE input_id=$1
            UNION SELECT l.derivative_id FROM material_derivative_links l JOIN dependent d ON l.input_id=d.id)
            UPDATE material_derivatives SET invalidated_at=NOW() WHERE id IN (SELECT id FROM dependent)''',id)

    async def _chain(self,conn,id,actor):
        nodes=await conn.fetch('''WITH RECURSIVE ancestors(id) AS (
            SELECT $1::text UNION SELECT l.input_id FROM material_derivative_links l JOIN ancestors a ON l.derivative_id=a.id)
            SELECT d.id,d.kind FROM material_derivatives d JOIN ancestors a ON a.id=d.id LIMIT 257''',id)
        if not nodes or len(nodes)>256: raise MaterialError('derivative_dependency_budget')
        refs=[]
        for node in sorted(nodes,key=lambda n:n['id']):
            payload=await self._payload(conn,node['id'],actor,node['kind'])
            if node['kind']=='dataset':
                from materials.dataset_repository import DatasetRepository
                await DatasetRepository(self.materials)._dataset(conn,node['id'],actor)
                refs.extend(payload['source_refs'])
            elif payload.get('contract')=='derivative-envelope-1':
                refs.extend(payload['sources'])
                if node['kind']=='transcript':
                    head=await conn.fetchval("SELECT observation_id FROM material_observation_heads WHERE realm=$1 AND asset_id=$2 AND kind='transcript'",actor.realm,payload['sources'][0]['asset_id'])
                    if head!=node['id']: raise MaterialError('stale_transcript_head')
            elif node['kind']=='computation':
                refs.extend(i['source'] for i in payload['inputs'])
                refs.extend(r['source'] for r in payload['spec']['conversions'])
            else: raise MaterialError('unsupported_derivative_dependency')
        return refs

    async def save(self,actor,kind,body,refs,*,inputs=()):
        from materials.types import EvidenceRef,Locator
        refs=[asdict(r) if isinstance(r,EvidenceRef) else r for r in refs]
        if not kind or len(kind)>80 or len(inputs)>32: raise MaterialError('invalid_derivative_kind')
        for ref in refs:
            # An original asset dependency (e.g. a decorative bitmap) carries
            # access/version/erasure provenance. It cannot verify a quote or value.
            if set(ref)=={'asset_id','asset_version'}: continue
            try: evidence=EvidenceRef(**{**ref,'locator':Locator.from_dict(ref['locator'])})
            except (TypeError,KeyError,ValueError): raise MaterialError('invalid_derivative_evidence') from None
            await self.materials.resolve(evidence,actor)
        envelope=dict(contract='derivative-envelope-1',body=body,sources=refs,inputs=sorted(set(inputs)))
        id=sha256(canonical([actor.realm,kind,envelope]).encode()).hexdigest()
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            all_refs=list(refs)
            for input in sorted(set(inputs)): all_refs.extend(await self._chain(conn,input,actor))
            # Flatten every ancestor asset into the lifecycle erasure boundary.
            assets=await self._sources(conn,actor,all_refs)
            await self._insert(conn,id,actor,kind,envelope,assets,sorted(set(inputs)))
        return id

    async def load(self,id,actor,kind=None):
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            payload=await self._payload(conn,id,actor,kind)
            if payload.get('contract')!='derivative-envelope-1': raise MaterialError('unsupported_derivative_envelope')
            refs=await self._chain(conn,id,actor)
            await self._sources(conn,actor,refs)
            return payload['body']

