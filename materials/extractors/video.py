"""Synchronized frame/audio evidence, bounded targeted inspection and storyboard."""
import base64
from dataclasses import replace
from hashlib import sha256
from materials.extractors.audio import AudioExtractor
from materials.extractors.images import ImageExtractor
from materials.extractors.isolation import WorkerLimits,run_worker
from materials.types import ContentBlock,ExtractionBundle,ExtractionManifest,Locator,MaterialError,block_id,canonical


class VideoExtractor:
    version='structured-video-1'
    def __init__(self,*,max_seconds=600,max_frames=24,image=None,audio=None):
        self.audio=audio or AudioExtractor(max_seconds=max_seconds)
        self.image=image or ImageExtractor(max_lines=40)
        self.analyzer=self.image.analyzer; self.transcriber=self.audio.transcriber
        self.options={**self.audio.options,'max_frames':max_frames}
        self.limits=WorkerLimits(wall_seconds=240)
    @property
    def cache_version(self): return self.version+':'+sha256(canonical([self.options,self.image.cache_version,self.audio.cache_version]).encode()).hexdigest()[:24]
    async def extract_async(self,aid,version,data,mime):
        if self.analyzer or self.transcriber: raise MaterialError('video_authorization_callback_required')
        return await self.extract_authorized(aid,version,data,mime,validate=None)
    async def extract_authorized(self,aid,version,data,mime,*,validate,start_ms=0,end_ms=None,dense=False):
        if (self.analyzer or self.transcriber) and validate is None: raise MaterialError('video_authorization_callback_required')
        analysis=await run_worker(dict(operation='video_frames',options={**self.options,'start_ms':start_ms,'end_ms':end_ms,'dense':dense},mime=mime),data,self.limits)
        if validate: await validate()
        loc=Locator('time',start_ms=analysis['start_ms'],end_ms=analysis['end_ms']); root=block_id(aid,version,'video',loc)
        blocks=[ContentBlock(root,'video',loc,metadata={k:v for k,v in analysis.items() if k!='frames'}|dict(role='video_timeline',sampling='dense_interval' if dense else 'uniform_and_scenes',method=self.version))]
        used=sum(len(b.text)+len(canonical(b.metadata)) for b in blocks)
        limitations=list(analysis['limitations']); frame_fingerprints=[]
        for frame in analysis['frames']:
            frame_bytes=base64.b64decode(frame['image_base64']); ms=frame['timestamp_ms']; interval=Locator('time',start_ms=ms,end_ms=ms+1)
            fid=block_id(aid,version,'image',interval,len(blocks)); frame_fingerprints.append([ms,sha256(frame_bytes).hexdigest()])
            observed=await self.image.extract_authorized(aid,version,frame_bytes,frame['mime'],validate=validate)
            if validate: await validate()
            blocks.append(ContentBlock(fid,'image',interval,observed.blocks[0].text,root,len(blocks),'observed','uncertain',
                ('single_sampled_frame_not_continuous_view',),dict(role='video_frame',timestamp_ms=ms,method=frame['method'],sha256=sha256(frame_bytes).hexdigest(),visual_status=observed.blocks[0].metadata.get('visual_status','uninterpreted'))))
            used+=len(blocks[-1].text)+len(canonical(blocks[-1].metadata))
            for child in observed.blocks[1:]:
                if len(blocks)>=3000: limitations.append('video_observation_budget_reached'); break
                if used+len(child.text)+len(canonical(child.metadata))+300>750000:
                    limitations.append('video_visual_metadata_budget_reached'); break
                index=len(blocks)
                blocks.append(replace(child,block_id=block_id(aid,version,child.kind,interval,index),locator=interval,parent_id=fid,ordinal=index,
                    metadata={**child.metadata,'frame_region':child.locator.bbox,'timestamp_ms':ms,'role':'frame_'+child.metadata.get('role',child.kind)}))
                used+=len(blocks[-1].text)+len(canonical(blocks[-1].metadata))
            limitations.extend(observed.manifest.limitations)
        if analysis['has_audio'] and not dense:
            audio=await self.audio.extract_authorized(aid,version,data,mime,validate=validate)
            mapping={b.block_id:sha256(('video-audio:'+b.block_id).encode()).hexdigest()[:24] for b in audio.blocks}
            for b in audio.blocks:
                if len(blocks)>=3800: limitations.append('video_audio_block_budget_reached'); break
                if used+len(b.text)+len(canonical(b.metadata))>3_800_000:
                    limitations.append('video_audio_metadata_budget_reached'); break
                blocks.append(replace(b,block_id=mapping[b.block_id],parent_id=mapping.get(b.parent_id,root),ordinal=len(blocks)))
                used+=len(b.text)+len(canonical(b.metadata))
            limitations.extend(audio.manifest.limitations)
        else: limitations.append('audio_not_available' if not analysis['has_audio'] else 'targeted_frame_view_audio_not_repeated')
        # Absolute frame PTS and decoded audio interval share the container origin.
        blocks[0]=replace(blocks[0],metadata={**blocks[0].metadata,'frame_fingerprints':frame_fingerprints})
        if validate: await validate()
        return ExtractionBundle(aid,version,self.cache_version,tuple(blocks),ExtractionManifest(analysis['duration_ms'],analysis['end_ms']-analysis['start_ms'],'partial',tuple(dict.fromkeys(limitations)),'millisecond'))
    async def frame_async(self,data,mime,start_ms,end_ms):
        return await run_worker(dict(operation='video_frames',options={**self.options,'start_ms':start_ms,'end_ms':end_ms,'dense':True},mime=mime),data,self.limits)
