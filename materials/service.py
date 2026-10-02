"""Intake and extraction outside DB transactions, with final generation checks."""
import asyncio
from hashlib import sha256
from materials.repository import MaterialRepository
from materials.storage import LocalBlobStore
from materials.validation import inspect_bytes


class MaterialService:
    def __init__(self, repository, store):
        self.repository, self.store = repository, store

    async def ingest(self, data, filename, actor, source_key, source_id, declared_mime=None,*,share=None):
        mime = await asyncio.to_thread(inspect_bytes, data, filename, declared_mime, self.repository.quotas.max_file_bytes)
        key = await self.repository.reserve_blob(actor)
        retained = None
        try:
            await asyncio.to_thread(self.store.put, data, key)
            row, retained = await self.repository.register(actor, source_key, source_id, filename, key, sha256(data).hexdigest(), len(data), mime,share=share)
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

    async def extract(self, aid, actor, extractor=None):
        if extractor is None:
            from materials.extractors.documents import configured_extractor
            _,revision=await self.repository.read(aid,actor)
            extractor=configured_extractor(revision['mime'])
        cached = await self.repository.extraction(aid, actor, getattr(extractor, 'cache_version', extractor.version))
        if cached:
            from materials.index import MaterialIndex
            await MaterialIndex(self.repository).save(actor,*cached)
            return cached
        row, rev, data = await self.read_bytes(aid, actor)
        if hasattr(extractor,'extract_authorized'):
            async def validate():
                current,_=await self.repository.read(aid,actor,rev['version'])
                if current['generation']!=row['generation'] or current['current_version']!=rev['version']:
                    from materials.types import MaterialError
                    raise MaterialError('stale_material_result')
            bundle=await extractor.extract_authorized(aid,rev['version'],data,rev['mime'],validate=validate)
        elif hasattr(extractor, 'extract_async'):
            bundle = await extractor.extract_async(aid, rev['version'], data, rev['mime'])
        else:
            bundle = await asyncio.to_thread(extractor.extract, aid, rev['version'], data, rev['mime'])
        try: eid = await self.repository.save_extraction(actor, bundle, row['generation'])
        except Exception as exc:
            # Concurrent visual requests may observe differently. The first
            # committed immutable observation wins; never return mismatched refs.
            if getattr(exc,'code',None)!='extractor_version_conflict' or not (getattr(extractor,'analyzer',None) or getattr(extractor,'transcriber',None)): raise
            cached=await self.repository.extraction(aid,actor,extractor.cache_version)
            if cached is None: raise
            from materials.index import MaterialIndex
            await MaterialIndex(self.repository).save(actor,*cached)
            return cached
        from materials.index import MaterialIndex
        await MaterialIndex(self.repository).save(actor,eid,bundle)
        return eid, bundle

    async def evidence_region(self, ref, actor, extractor, *, reread=False):
        """Resolve authorized evidence, render only its region, then recheck access.

        Rereading creates a separate immutable observation; it never changes an
        existing extraction or silently replaces a cited number.
        """
        from dataclasses import asdict
        from materials.types import ContentBlock, ExtractionBundle, ExtractionManifest, Locator, MaterialError, block_id
        block = await self.repository.resolve(ref, actor)
        if not hasattr(extractor, 'region_async'):
            raise MaterialError('regional_extractor_required')
        row, rev, data = await self.read_bytes(ref.asset_id, actor, ref.asset_version)
        locator = block.locator
        if locator.kind.value == 'page':
            locator = Locator('region', page=locator.page, bbox=(0,0,1,1))
        result = await extractor.region_async(data, rev['mime'], asdict(locator), reread=reread, resource=block.metadata.get('resource'),
            orientation_hint=block.metadata.get('ocr',{}).get('rotation_clockwise'))
        current, _ = await self.repository.read(ref.asset_id, actor, ref.asset_version)
        if current['generation'] != row['generation']:
            raise MaterialError('stale_material_result')
        await self.repository.resolve(ref, actor)
        if reread:
            observed = result.get('observation')
            if not observed:
                raise MaterialError('regional_ocr_unavailable')
            text = ' '.join(w['text'] for w in observed['words'])
            method = extractor.cache_version + ':region:' + ref.block_id
            new_block = ContentBlock(block_id(ref.asset_id,ref.asset_version,'text',locator),'text',locator,text,
                quality='uncertain',limitations=('regional_observation_only','ocr_not_human_verified'),
                metadata=dict(method='tesseract_region',supersedes_observation=asdict(ref),**observed))
            bundle = ExtractionBundle(ref.asset_id,ref.asset_version,method,(new_block,),
                ExtractionManifest(1,1,'unknown',('regional_observation_only',),'page'))
            result['extraction_id'] = await self.repository.save_extraction(actor,bundle,row['generation'])
            result['bundle'] = bundle.to_dict()
        return result

    async def observe_image_region(self,ref,actor,extractor,analyzer):
        """A separate visual observation, mapped back to the cited source region."""
        from dataclasses import asdict
        from materials.types import ContentBlock,ExtractionBundle,ExtractionManifest,block_id,MaterialError
        from materials.visual import observation_blocks
        import base64
        original,_=await self.repository.read(ref.asset_id,actor,ref.asset_version)
        preview=await self.evidence_region(ref,actor,extractor)
        await self.repository.resolve(ref,actor)
        observations=await analyzer.observe(base64.b64decode(preview['image_base64']),preview['mime'])
        current,_=await self.repository.read(ref.asset_id,actor,ref.asset_version)
        if current['generation']!=original['generation'] or current['current_version']!=ref.asset_version: raise MaterialError('stale_material_result')
        loc=ref.locator
        if loc.kind.value!='region': raise MaterialError('image_region_required')
        bid=block_id(ref.asset_id,ref.asset_version,'image',loc)
        parent=ContentBlock(bid,'image',loc,observations['summary'],observation='observed',quality='uncertain',
            limitations=('model_visual_observation_not_verified',),metadata=dict(role='regional_visual_observation',source=asdict(ref),method=analyzer.identity))
        blocks=(parent,)+observation_blocks(ref.asset_id,ref.asset_version,observations,bid,1,method=analyzer.identity,outer=loc.bbox)
        import uuid
        bundle=ExtractionBundle(ref.asset_id,ref.asset_version,'region-vision-1:'+analyzer.identity+':'+uuid.uuid4().hex,blocks,
            ExtractionManifest(1,1,'unknown',observations['limitations']+('regional_observation_only',),'image'))
        eid=await self.repository.save_extraction(actor,bundle,original['generation'])
        return eid,bundle

    async def datasets(self, aid, actor, *, policy=None, extractor=None):
        from materials.dataset_repository import DatasetRepository
        from materials.datasets import datasets_from_bundle
        from materials.extractors.documents import configured_extractor
        if extractor is None:
            _, rev = await self.repository.read(aid, actor)
            extractor = configured_extractor(rev['mime'])
        eid, bundle = await self.extract(aid, actor, extractor)
        snapshots = datasets_from_bundle(eid, bundle, policy)
        repository = DatasetRepository(self.repository)
        return tuple([await repository.save_dataset(actor,snapshot) for snapshot in snapshots])

    async def compute(self, dataset_id, actor, spec, *, formula_dataset_ids=()):
        from dataclasses import asdict,replace
        from artifacts.computation import compute
        from materials.dataset_repository import DatasetRepository
        from materials.datasets import ColumnPolicy, normalize, decimal
        from materials.types import EvidenceRef, Locator, MaterialError
        repository = DatasetRepository(self.repository)
        dataset = await repository.load_dataset(dataset_id,actor)
        snapshots = [dataset] + [await repository.load_dataset(id,actor) for id in formula_dataset_ids if id!=dataset_id]
        # A cross-sheet reference cannot pick arbitrary data from another file.
        identity={(ref.asset_id,ref.asset_version) for ref in dataset.source_refs}
        if any({(ref.asset_id,ref.asset_version) for ref in s.source_refs}!=identity for s in snapshots):
            raise MaterialError('formula_workbook_mismatch')
        environment={}
        for snapshot in snapshots:
            for cell in snapshot.cells:
                key=(snapshot.name,cell.address)
                if key in environment and environment[key]!=cell: raise MaterialError('formula_environment_conflict')
                environment[key]=cell
        rate_snapshots=[]; rate_warnings=[]
        for rate in spec.conversions:
            ref=EvidenceRef(**{**rate.source,'locator':Locator.from_dict(rate.source['locator'])})
            block=await self.repository.resolve(ref,actor)
            if block.metadata.get('role')!='table_cell': raise MaterialError('conversion_requires_cell_evidence')
            quality=block.quality
            if rate.dataset_id:
                rate_dataset=await repository.load_dataset(rate.dataset_id,actor)
                cell=next((c for c in rate_dataset.cells if c.source==ref),None)
                if cell is None: raise MaterialError('conversion_requires_cell_evidence')
                value=cell.normalized; quality=cell.quality; rate_snapshots.append(rate_dataset)
            else:
                raw=block.metadata.get('raw',block.text)
                value=normalize(raw,ColumnPolicy(0,locale='en'),native_kind=block.metadata.get('native_kind'))
            if value.kind!='number' or value.unit!='1' or decimal(value.value)!=decimal(rate.factor) or decimal(value.lower or value.value)!=decimal(rate.lower or rate.factor) or decimal(value.upper or value.value)!=decimal(rate.upper or rate.factor):
                raise MaterialError('conversion_rate_not_verified')
            if quality in ('uncertain','unreadable'):
                if not spec.allow_uncertain: raise MaterialError('uncertain_conversion_source')
                rate_warnings.append('uncertain_conversion_source')
        result=await asyncio.to_thread(compute,dataset,spec,formula_cells=environment)
        all_snapshots={s.id:s for s in snapshots+rate_snapshots}
        result=replace(result,dependency_datasets=tuple(sorted(all_snapshots)),warnings=tuple(dict.fromkeys(result.warnings+tuple(rate_warnings))))
        # Verify all current sheets after CPU work, including corrected formula leaves.
        for snapshot in all_snapshots.values():
            await repository.load_dataset(snapshot.id,actor)
        await repository.save_computation(actor,result,formula_dataset_ids=tuple(all_snapshots))
        return result

    async def propose_correction(self, dataset_id, actor, address, replacement):
        from materials.dataset_repository import DatasetRepository
        from materials.datasets import correction_proposal
        dataset=await DatasetRepository(self.repository).load_dataset(dataset_id,actor)
        return correction_proposal(dataset,address,replacement)

    async def transcript(self,aid,actor,*,extractor=None):
        from materials.observations import ObservationRepository
        from materials.extractors.documents import configured_extractor
        from materials.timeline import Timeline
        from materials.types import EvidenceRef,MaterialError
        _,rev=await self.repository.read(aid,actor)
        eid,bundle=await self.extract(aid,actor,extractor or configured_extractor(rev['mime']))
        root=next((b for b in bundle.blocks if b.metadata.get('role')=='audio_timeline'),None)
        if root is None: raise MaterialError('timed_transcript_unavailable')
        value=root.metadata['timeline']
        if value.get('segment_storage')=='timeline_chunks':
            value={k:v for k,v in value.items() if k!='segment_storage'}
            value['segments']=[s for b in bundle.blocks if b.metadata.get('role')=='timeline_chunk' for s in b.metadata['segments']]
        timeline=Timeline.from_dict(value)
        ref=EvidenceRef(aid,bundle.asset_version,eid,root.block_id,root.locator)
        id,current=await ObservationRepository(self.repository).save_timeline(actor,ref,timeline)
        from materials.index import MaterialIndex
        await MaterialIndex(self.repository).transcript(actor,id)
        return id,current,ref

    async def confirm_transcript(self,aid,actor,observation_id,segment_id,text):
        from materials.observations import ObservationRepository
        result=await ObservationRepository(self.repository).confirm(actor,aid,observation_id,segment_id,text)
        from materials.index import MaterialIndex
        await MaterialIndex(self.repository).transcript(actor,result[0])
        return result

    async def observe_video_interval(self,aid,actor,start_ms,end_ms,*,extractor=None):
        import uuid
        from dataclasses import replace
        from materials.extractors.documents import configured_extractor
        row,rev,data=await self.read_bytes(aid,actor)
        if not rev['mime'].startswith('video/'): raise MaterialError('video_required')
        if not 0<=start_ms<end_ms or end_ms-start_ms>30000: raise MaterialError('video_refinement_budget')
        async def validate():
            current,_=await self.repository.read(aid,actor,rev['version'])
            if current['generation']!=row['generation'] or current['current_version']!=rev['version']: raise MaterialError('stale_material_result')
        decoder=extractor or configured_extractor(rev['mime'])
        bundle=await decoder.extract_authorized(aid,rev['version'],data,rev['mime'],validate=validate,start_ms=start_ms,end_ms=end_ms,dense=True)
        bundle=replace(bundle,extractor=bundle.extractor+':interval:'+uuid.uuid4().hex)
        await validate()
        eid=await self.repository.save_extraction(actor,bundle,row['generation'])
        return eid,bundle

    async def video_frame(self,ref,actor,*,extractor=None):
        from materials.extractors.video import VideoExtractor
        block=await self.repository.resolve(ref,actor)
        if block.metadata.get('role')!='video_frame': raise MaterialError('video_frame_required')
        row,rev,data=await self.read_bytes(ref.asset_id,actor,ref.asset_version)
        result=await (extractor or VideoExtractor()).frame_async(data,rev['mime'],ref.locator.start_ms,ref.locator.end_ms)
        frame=next((f for f in result['frames'] if f['timestamp_ms']==ref.locator.start_ms),None)
        if not frame: raise MaterialError('video_frame_unavailable')
        import base64
        from hashlib import sha256
        if sha256(base64.b64decode(frame['image_base64'])).hexdigest()!=block.metadata['sha256']: raise MaterialError('video_frame_integrity_failure')
        await self.repository.resolve(ref,actor)
        current,_=await self.repository.read(ref.asset_id,actor,ref.asset_version)
        if current['generation']!=row['generation']: raise MaterialError('stale_material_result')
        return frame

    async def ingest_url(self,url,actor,source_key,source_id,*,fetcher=None):
        from datetime import datetime,timezone
        from utils.public_fetch import fetch_public
        from materials.derivatives import DerivativeRepository
        from materials.types import EvidenceRef
        from urllib.parse import urlsplit
        fetched=await (fetcher or fetch_public)(url,max_bytes=self.repository.quotas.max_file_bytes,allowed_mimes={'video/mp4','application/octet-stream','audio/mpeg','audio/wav','audio/ogg'})
        filename=urlsplit(fetched.url).path.rsplit('/',1)[-1] or 'remote.mp4'
        asset=await self.ingest(fetched.data,filename,actor,source_key,source_id,fetched.mime)
        eid,bundle=await self.extract(asset['id'],actor)
        root=bundle.blocks[0]; ref=EvidenceRef(asset['id'],bundle.asset_version,eid,root.block_id,root.locator)
        provenance=await DerivativeRepository(self.repository).save(actor,'url_origin',dict(url=fetched.url,read_at=datetime.now(timezone.utc).isoformat(),sha256=fetched.sha256,redirects=fetched.redirects,availability='downloaded_original',coverage=bundle.manifest.coverage),[ref])
        return asset,eid,bundle,provenance

    async def audio_clip(self,ref,actor,*,extractor=None):
        from materials.extractors.audio import AudioExtractor
        from materials.types import MaterialError
        await self.repository.resolve(ref,actor)
        if ref.locator.kind.value!='time': raise MaterialError('audio_interval_required')
        row,rev,data=await self.read_bytes(ref.asset_id,actor,ref.asset_version)
        result=await (extractor or AudioExtractor()).clip_async(data,rev['mime'],ref.locator.start_ms,ref.locator.end_ms)
        current,_=await self.repository.read(ref.asset_id,actor,ref.asset_version)
        if current['generation']!=row['generation']: raise MaterialError('stale_material_result')
        await self.repository.resolve(ref,actor)
        return result

    async def confirm_correction(self, actor, proposal):
        from materials.dataset_repository import DatasetRepository
        return await DatasetRepository(self.repository).confirm(actor,proposal)
