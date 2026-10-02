"""Decorative variants carry provenance and may never become numeric evidence."""
from materials.derivatives import DerivativeRepository
from materials.types import MaterialError

class IllustrationRepository(DerivativeRepository):
    async def generated(self,actor,pixels,*,prompt,provider,parameters,sources,inputs=()):
        import base64
        from materials.validation import inspect_bytes
        from hashlib import sha256
        if len(pixels)>2*1024**2 or not prompt or len(prompt)>4000: raise MaterialError('illustration_budget')
        mime=inspect_bytes(pixels,'illustration')
        if not mime.startswith('image/'): raise MaterialError('illustration_mime_required')
        return await self.save(actor,'illustration',dict(pixels=base64.b64encode(pixels).decode(),sha256=sha256(pixels).hexdigest(),mime=mime,prompt=prompt,provider=provider,parameters=parameters,role='decoration_not_evidence'),sources,inputs=inputs)
    async def record(self,actor,*,asset_id,prompt,provider,parameters,reference_ids=()):
        if not prompt or len(prompt)>4000 or len(reference_ids)>8: raise MaterialError('illustration_budget')
        rows=[]
        for id in [asset_id,*reference_ids]:
            row,_=await self.materials.read(id,actor); rows.append(dict(asset_id=id,asset_version=row['current_version']))
        # Inputs as asset dependencies; no image model output is fact proof.
        from materials.types import canonical
        from hashlib import sha256
        payload=dict(contract='derivative-envelope-1',body=dict(asset_id=asset_id,prompt=prompt,provider=provider,parameters=parameters,references=list(reference_ids),role='decoration_not_evidence'),sources=rows,inputs=[])
        id=sha256(canonical([actor.realm,'illustration',payload]).encode()).hexdigest()
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor); assets=await self._sources(conn,actor,rows)
            await self._insert(conn,id,actor,'illustration',payload,assets)
        return id
