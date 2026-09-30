"""Dataset heads, immutable calculations, CAS and the material erasure barrier."""
from dataclasses import asdict
from hashlib import sha256
import json
from materials.datasets import Dataset,apply_correction
from materials.types import MaterialError,canonical


class DatasetRepository:
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
            await self.materials._source_allowed(conn,actor,row['source_id'])
            if row['current_version']!=version: raise MaterialError('stale_dataset_source')
        return sorted({aid for aid,_ in versions})

    async def _payload(self,conn,id,actor,kind):
        row=await conn.fetchrow('SELECT * FROM material_derivatives WHERE id=$1',id)
        if row is None or row['realm']!=actor.realm or row['kind']!=kind or row['payload'] is None or row['invalidated_at'] is not None:
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

    async def save_dataset(self,actor,dataset):
        refs=dataset.source_refs
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            assets=await self._sources(conn,actor,refs)
            head=await conn.fetchrow('SELECT * FROM material_dataset_heads WHERE realm=$1 AND series_key=$2 FOR UPDATE',actor.realm,dataset.series_key)
            if head:
                try:
                    old=Dataset.from_dict(await self._payload(conn,head['dataset_id'],actor,'dataset'))
                    if {(r.asset_id,r.asset_version,r.extraction_id) for r in old.source_refs}=={(r.asset_id,r.asset_version,r.extraction_id) for r in refs}:
                        return old  # Confirmed correction remains the current head.
                except MaterialError as exc:
                    if exc.code!='derivative_unavailable': raise
            await self._insert(conn,dataset.id,actor,'dataset',dataset.to_dict(),assets)
            if head:
                await self._invalidate(conn,head['dataset_id'])
                await conn.execute('UPDATE material_dataset_heads SET dataset_id=$3,revision=revision+1 WHERE realm=$1 AND series_key=$2',actor.realm,dataset.series_key,dataset.id)
            else:
                await conn.execute('INSERT INTO material_dataset_heads(realm,series_key,dataset_id) VALUES($1,$2,$3)',actor.realm,dataset.series_key,dataset.id)
            return dataset

    async def _dataset(self,conn,id,actor,*,current=True):
        dataset=Dataset.from_dict(await self._payload(conn,id,actor,'dataset'))
        await self._sources(conn,actor,dataset.source_refs)
        if current:
            head=await conn.fetchval('SELECT dataset_id FROM material_dataset_heads WHERE realm=$1 AND series_key=$2',actor.realm,dataset.series_key)
            if head!=id: raise MaterialError('stale_dataset_head')
        return dataset

    async def load_dataset(self,id,actor):
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            return await self._dataset(conn,id,actor)

    async def confirm(self,actor,proposal):
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            old=await self._dataset(conn,proposal['dataset_id'],actor)
            assets=await self._sources(conn,actor,old.source_refs,edit=True)
            new=apply_correction(old,proposal,author_ref=actor.sender_ref)
            await self._insert(conn,new.id,actor,'dataset',new.to_dict(),assets)
            await self._invalidate(conn,old.id)
            await conn.execute('UPDATE material_dataset_heads SET dataset_id=$3,revision=revision+1 WHERE realm=$1 AND series_key=$2 AND dataset_id=$4',actor.realm,old.series_key,new.id,old.id)
            return new

    async def save_computation(self,actor,result,*,formula_dataset_ids=()):
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            dataset=await self._dataset(conn,result.dataset_id,actor)
            dependencies={dataset.id}
            for id in formula_dataset_ids:
                await self._dataset(conn,id,actor)
                dependencies.add(id)
            refs=[i['source'] for i in result.inputs]+[asdict(r) for r in dataset.source_refs]+[r.source for r in result.spec.conversions]
            assets=await self._sources(conn,actor,refs)
            # Include headers/omitted records through the dataset dependency, even
            # when they aren't numeric inputs of this particular operation.
            await self._insert(conn,result.id,actor,'computation',result.to_dict(),assets,sorted(dependencies))
            return result.id

    async def load_computation(self,id,actor):
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            value=await self._payload(conn,id,actor,'computation')
            await self._dataset(conn,value['dataset_id'],actor)
            dependencies=await conn.fetch('SELECT input_id FROM material_derivative_links WHERE derivative_id=$1',id)
            for row in dependencies: await self._dataset(conn,row['input_id'],actor)
            refs=[i['source'] for i in value['inputs']]+[r['source'] for r in value['spec']['conversions']]
            await self._sources(conn,actor,refs)
            return value
