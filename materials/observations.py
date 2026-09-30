"""Current transcript heads, author confirmations and dependent-result revocation."""
from dataclasses import asdict
from materials.derivatives import DerivativeRepository
from materials.timeline import Timeline
from materials.types import MaterialError


class ObservationRepository(DerivativeRepository):
    async def save_timeline(self,actor,ref,timeline):
        # The first observation for an unchanged source remains current even
        # after a user correction. Re-extraction does not undo confirmation.
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            await self._sources(conn,actor,[ref])
            head=await conn.fetchval("SELECT observation_id FROM material_observation_heads WHERE realm=$1 AND asset_id=$2 AND kind='transcript'",actor.realm,ref.asset_id)
            if head:
                try:
                    old=await self._payload(conn,head,actor,'transcript')
                    if old['sources'][0]['asset_version']==ref.asset_version:
                        return head,Timeline.from_dict(old['body']['timeline'])
                except MaterialError as exc:
                    if exc.code!='derivative_unavailable': raise
        id=await self.save(actor,'transcript',dict(timeline=asdict(timeline)),[ref])
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor); await self._sources(conn,actor,[ref])
            head=await conn.fetchval("SELECT observation_id FROM material_observation_heads WHERE realm=$1 AND asset_id=$2 AND kind='transcript' FOR UPDATE",actor.realm,ref.asset_id)
            if head:
                try: old=await self._payload(conn,head,actor,'transcript') if head!=id else None
                except MaterialError as exc:
                    if exc.code!='derivative_unavailable': raise
                    old=None
                if old and old['sources'][0]['asset_version']==ref.asset_version: return head,Timeline.from_dict(old['body']['timeline'])
                await self._invalidate(conn,head)
            await conn.execute("""INSERT INTO material_observation_heads(realm,asset_id,kind,observation_id) VALUES($1,$2,'transcript',$3)
                ON CONFLICT(realm,asset_id,kind) DO UPDATE SET observation_id=$3,revision=material_observation_heads.revision+1""",actor.realm,ref.asset_id,id)
        return id,timeline

    async def current(self,actor,asset_id):
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            id=await conn.fetchval("SELECT observation_id FROM material_observation_heads WHERE realm=$1 AND asset_id=$2 AND kind='transcript'",actor.realm,asset_id)
            if not id: raise MaterialError('transcript_unavailable')
            value=await self._payload(conn,id,actor,'transcript'); await self._sources(conn,actor,value['sources'])
            return id,Timeline.from_dict(value['body']['timeline'])

    async def confirm(self,actor,asset_id,observation_id,segment_id,text):
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            head=await conn.fetchval("SELECT observation_id FROM material_observation_heads WHERE realm=$1 AND asset_id=$2 AND kind='transcript' FOR UPDATE",actor.realm,asset_id)
            if head!=observation_id: raise MaterialError('stale_transcript_correction')
            original=await self._payload(conn,head,actor,'transcript'); assets=await self._sources(conn,actor,original['sources'],edit=True)
            old=Timeline.from_dict(original['body']['timeline'])
            new=old.confirm(segment_id,text,actor_ref=actor.sender_ref,expected_id=old.id)
            envelope={**original,'body':dict(timeline=asdict(new))}
            from hashlib import sha256
            from materials.types import canonical
            id=sha256(canonical([actor.realm,'transcript',envelope]).encode()).hexdigest()
            await self._insert(conn,id,actor,'transcript',envelope,assets)
            await self._invalidate(conn,head)
            await conn.execute("UPDATE material_observation_heads SET observation_id=$3,revision=revision+1 WHERE realm=$1 AND asset_id=$2 AND kind='transcript' AND observation_id=$4",actor.realm,asset_id,id,head)
            return id,new
