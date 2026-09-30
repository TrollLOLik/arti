"""Real timed observations. Speakers stay local; corrections retain source times."""
from dataclasses import asdict,dataclass,replace
from hashlib import sha256
import math
import re
from materials.types import MaterialError,canonical

TIMELINE_VERSION='timeline-1'


@dataclass(frozen=True)
class Word:
    text: str
    start_ms: int
    end_ms: int
    speaker: str | None=None
    score: float | None=None
    def __post_init__(self):
        if not isinstance(self.text,str) or len(self.text)>1000 or type(self.start_ms) is not int or type(self.end_ms) is not int or not 0<=self.start_ms<self.end_ms: raise MaterialError('invalid_timed_word')
        if self.speaker is not None and not re.fullmatch(r'speaker_[1-9][0-9]*',self.speaker): raise MaterialError('speaker_must_be_clip_local')
        if self.score is not None and not math.isfinite(self.score): raise MaterialError('invalid_asr_score')


@dataclass(frozen=True)
class Segment:
    id: str
    text: str
    start_ms: int
    end_ms: int
    words: tuple[Word,...]=()
    speaker: str | None=None
    status: str='uncertain'
    def __post_init__(self):
        if type(self.start_ms) is not int or type(self.end_ms) is not int or not 0<=self.start_ms<self.end_ms or len(self.text)>20000 or len(self.words)>4000:
            raise MaterialError('invalid_audio_segment')
        if self.status not in ('uncertain','unintelligible','confirmed','silence_observed'): raise MaterialError('invalid_audio_status')
        if any(w.start_ms<self.start_ms or w.end_ms>self.end_ms for w in self.words): raise MaterialError('word_outside_segment')
        if self.speaker is not None and not re.fullmatch(r'speaker_[1-9][0-9]*',self.speaker): raise MaterialError('speaker_must_be_clip_local')


@dataclass(frozen=True)
class Timeline:
    duration_ms: int
    segments: tuple[Segment,...]
    method: str
    limitations: tuple[str,...]=()
    acoustic: tuple[dict,...]=()
    parent_id: str | None=None
    confirmations: tuple[dict,...]=()
    version: str=TIMELINE_VERSION
    def __post_init__(self):
        if type(self.duration_ms) is not int or not 0<self.duration_ms<=86400000 or len(self.segments)>2000 or len({s.id for s in self.segments})!=len(self.segments) or any(s.end_ms>self.duration_ms for s in self.segments):
            raise MaterialError('invalid_audio_timeline')
        if self.version!=TIMELINE_VERSION: raise MaterialError('unsupported_timeline_version')
    @property
    def id(self): return sha256(canonical(asdict(self)).encode()).hexdigest()
    @classmethod
    def from_dict(cls,value):
        return cls(**{**value,'segments':tuple(Segment(**{**s,'words':tuple(Word(**w) for w in s.get('words',()))}) for s in value['segments']),
            'limitations':tuple(value.get('limitations',())),'acoustic':tuple(value.get('acoustic',())),'confirmations':tuple(value.get('confirmations',()))})
    def at(self,start_ms,end_ms):
        if not 0<=start_ms<end_ms<=self.duration_ms: raise MaterialError('invalid_timeline_interval')
        return tuple(s for s in self.segments if s.start_ms<end_ms and s.end_ms>start_ms)
    def overlaps(self):
        return tuple(dict(left=a.id,right=b.id,start_ms=max(a.start_ms,b.start_ms),end_ms=min(a.end_ms,b.end_ms),
            speakers=(a.speaker,b.speaker),interpretation='overlapping_provider_intervals_not_verified_separation')
            for i,a in enumerate(self.segments) for b in self.segments[i+1:] if a.start_ms<b.end_ms and b.start_ms<a.end_ms)
    def confirm(self,segment_id,text,*,actor_ref,expected_id):
        if expected_id!=self.id: raise MaterialError('stale_transcript_correction')
        segment=next((s for s in self.segments if s.id==segment_id),None)
        if segment is None or not text or len(text)>20000: raise MaterialError('invalid_transcript_correction')
        updated=replace(segment,text=text,status='confirmed')
        record=dict(segment_id=segment_id,original_text=segment.text,confirmed_text=text,actor_ref=actor_ref,
            timestamps='original_audio_interval; word_alignment_not_reinferred')
        return replace(self,segments=tuple(updated if s.id==segment_id else s for s in self.segments),parent_id=self.id,
            confirmations=self.confirmations+(record,),limitations=tuple(dict.fromkeys(self.limitations+('confirmed_text_word_alignment_unchanged',))))


def local_speakers(rows):
    labels={}
    for row in rows:
        label=row.get('speaker')
        if label is not None and str(label) not in labels: labels[str(label)]='speaker_'+str(len(labels)+1)
    return labels


def assembly_timeline(result,duration_ms):
    utterances=result.get('utterances') or []
    if not utterances and result.get('words'):
        # Provider word timestamps are retained; grouping by punctuation/gap does
        # not synthesize alignment or assign a human identity.
        groups=[]
        for word in result['words']:
            if not groups or word.get('start',0)-groups[-1][-1].get('end',0)>1500 or len(groups[-1])>=80: groups.append([])
            groups[-1].append(word)
        utterances=[dict(start=g[0]['start'],end=g[-1]['end'],text=' '.join(w['text'] for w in g),words=g,speaker=None) for g in groups]
    speakers=local_speakers(utterances); segments=[]; limitations=['asr_scores_not_calibrated','speaker_identity_unconfirmed','overlap_separation_unverified']
    for i,row in enumerate(utterances):
        start,end=int(row['start']),int(row['end'])
        if start>=end or start<0 or end>duration_ms: raise MaterialError('provider_timestamp_outside_audio')
        speaker=speakers.get(str(row.get('speaker')))
        words=[]
        for word in row.get('words') or []:
            ws,we=int(word['start']),int(word['end'])
            if ws>=we: limitations.append('zero_duration_word_omitted'); continue
            words.append(Word(word['text'],ws,we,speaker,word.get('confidence')))
        text=row.get('text') or ''
        segments.append(Segment('turn_'+str(i+1),text,start,end,tuple(words),speaker,'unintelligible' if not text.strip() or text.strip().lower() in ('[inaudible]','[unintelligible]') else 'uncertain'))
    if not segments: limitations.append('timed_transcript_unavailable')
    return Timeline(duration_ms,tuple(segments),'assemblyai:universal-3-pro/universal-2',tuple(dict.fromkeys(limitations)))


def groq_timeline(result,duration_ms):
    rows=result.get('segments') or []; segments=[]; limitations=['diarization_unavailable','speaker_identity_unconfirmed','asr_scores_not_calibrated']
    for i,row in enumerate(rows):
        start,end=round(float(row['start'])*1000),round(float(row['end'])*1000)
        if start>=end or start<0 or end>duration_ms: raise MaterialError('provider_timestamp_outside_audio')
        words=[]
        for word in result.get('words') or []:
            ws,we=round(float(word['start'])*1000),round(float(word['end'])*1000)
            if start<=ws<we<=end: words.append(Word(word.get('word',word.get('text','')),ws,we))
        segments.append(Segment('segment_'+str(i+1),row.get('text') or '',start,end,tuple(words)))
    if not segments: limitations.append('timed_transcript_unavailable')
    if not result.get('words'): limitations.append('word_timing_unavailable')
    return Timeline(duration_ms,tuple(segments),'groq:whisper-large-v3-turbo',tuple(limitations))
