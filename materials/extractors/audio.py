"""Bounded audio timelines from actual decoder and provider timestamps."""
from dataclasses import asdict,replace
from hashlib import sha256
from pathlib import Path
import base64
from materials.extractors.isolation import WorkerLimits,run_worker
from materials.extractors.media import installation,audio_analysis
from materials.timeline import Timeline,Segment
from materials.types import ContentBlock,ExtractionBundle,ExtractionManifest,Locator,MaterialError,block_id,canonical
from materials.validation import inspect_bytes


class AudioExtractor:
    version='structured-audio-1'
    def __init__(self,*,max_seconds=600,transcriber=None,ffmpeg=None,ffprobe=None):
        if not 1<=max_seconds<=900: raise MaterialError('audio_duration_budget')
        tools=installation(); self.options=dict(max_seconds=max_seconds,ffmpeg=ffmpeg or tools[0],ffprobe=ffprobe or tools[1])
        self.transcriber=transcriber; self.limits=WorkerLimits(wall_seconds=150)
    @property
    def cache_version(self):
        tools=[]
        for key in ('ffmpeg','ffprobe'):
            path=Path(self.options[key])
            tools.append([str(path),path.stat().st_size,path.stat().st_mtime_ns] if path.is_file() else ['unavailable'])
        return self.version+':'+sha256(canonical([self.options,tools,getattr(self.transcriber,'identity','acoustic_only')]).encode()).hexdigest()[:24]
    async def extract_async(self,aid,version,data,mime):
        if self.transcriber is not None: raise MaterialError('audio_authorization_callback_required')
        return await self.extract_authorized(aid,version,data,mime,validate=None)
    async def extract_authorized(self,aid,version,data,mime,*,validate):
        analysis=await run_worker(dict(operation='audio_analysis',options=self.options,mime=mime),data,self.limits)
        timeline=Timeline(analysis['processed_ms'],(),method='acoustic_only',limitations=('transcript_unavailable','diarization_unavailable'))
        if self.transcriber is not None:
            if validate is None: raise MaterialError('audio_authorization_callback_required')
            await validate()
            timeline=await self.transcriber.transcribe(base64.b64decode(analysis['audio_base64']),analysis['mime'],analysis['processed_ms'],validate=validate)
            await validate()
        return self.bundle(aid,version,analysis,timeline)
    def analyze(self,data,mime):
        inspect_bytes(data,'audio',mime)
        if not mime.startswith('audio/'): raise MaterialError('audio_mime_required')
        return audio_analysis(data,mime,**self.options)
    def bundle(self,aid,version,analysis,timeline):
        start,end=analysis['start_ms'],analysis['end_ms']; duration=analysis['duration_ms']
        locator=Locator('time',start_ms=start,end_ms=end); parent=block_id(aid,version,'audio',locator)
        limitations=list(analysis['limitations'])+list(timeline.limitations)
        selected=[]; used=0
        for segment in timeline.segments:
            if len(segment.words)>600:
                segment=replace(segment,words=segment.words[:600]); limitations.append('segment_word_alignment_budget_reached')
            size=len(canonical(asdict(segment)))
            if used+size>1_200_000:
                limitations.append('timeline_metadata_budget_reached'); break
            selected.append(segment); used+=size
        timeline=replace(timeline,segments=tuple(selected),limitations=tuple(dict.fromkeys(limitations)))
        header={**asdict(timeline),'segments':[],'acoustic':analysis['acoustic'],'segment_storage':'timeline_chunks'}
        blocks=[ContentBlock(parent,'audio',locator,'',metadata=dict(role='audio_timeline',timeline=header,
            duration_ms=duration,processed_interval=[start,end],decoder_streams=analysis['streams'],speaker_identity='unconfirmed',method=timeline.method))]
        chunks=[]; chunk=[]; size=0
        for segment in timeline.segments:
            value=asdict(segment); length=len(canonical(value))
            if size+length>120000 and chunk: chunks.append(chunk); chunk=[]; size=0
            chunk.append(value); size+=length
        if chunk: chunks.append(chunk)
        for chunk in chunks:
            index=len(blocks)
            blocks.append(ContentBlock(block_id(aid,version,'audio',locator,index),'audio',locator,'',parent,index,metadata=dict(role='timeline_chunk',segments=chunk)))
        count=0
        for segment in timeline.segments:
            if len(blocks)>=3800:
                limitations.append('turn_block_budget_reached'); break
            loc=Locator('time',start_ms=start+segment.start_ms,end_ms=start+segment.end_ms); index=len(blocks); bid=block_id(aid,version,'audio',loc,index)
            blocks.append(ContentBlock(bid,'audio',loc,segment.text,parent,index,'observed','verified' if segment.status=='confirmed' else 'uncertain',
                ('transcript_observation_not_request_by_speaker','speaker_identity_unconfirmed'),
                dict(role='speaker_turn',segment_id=segment.id,speaker=segment.speaker,words=[asdict(w) for w in segment.words],status=segment.status,method=timeline.method)))
            for word in segment.words:
                if count>=2500 or len(blocks)>=3800: limitations.append('word_block_budget_reached'); break
                wloc=Locator('time',start_ms=start+word.start_ms,end_ms=start+word.end_ms); index=len(blocks)
                blocks.append(ContentBlock(block_id(aid,version,'text',wloc,index),'text',wloc,word.text,bid,index,'observed','uncertain',
                    ('asr_score_not_probability',),dict(role='timed_word',speaker=word.speaker,engine_score=word.score,method=timeline.method)))
                count+=1
        for silence in analysis['silence']:
            if len(blocks)>=3800: limitations.append('silence_block_budget_reached'); break
            loc=Locator('time',start_ms=silence['start_ms'],end_ms=silence['end_ms']); index=len(blocks)
            blocks.append(ContentBlock(block_id(aid,version,'audio',loc,index),'audio',loc,'',parent,index,'observed','unassessed',
                ('low_energy_does_not_prove_silence',),dict(role='low_energy_interval',**silence)))
        if timeline.overlaps(): limitations.append('overlapping_speaker_turns_unverified')
        if end<duration or start: limitations.append('audio_duration_budget_reached')
        coverage='partial' if end<duration or start or 'timeline_metadata_budget_reached' in limitations else 'unknown'
        return ExtractionBundle(aid,version,self.cache_version,tuple(blocks),ExtractionManifest(duration,end-start,coverage,tuple(dict.fromkeys(limitations)),'millisecond'))
    async def clip_async(self,data,mime,start_ms,end_ms):
        return await run_worker(dict(operation='audio_analysis',options={**self.options,'start_ms':start_ms,'end_ms':end_ms},mime=mime),data,self.limits)
