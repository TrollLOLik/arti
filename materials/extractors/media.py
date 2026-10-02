"""Fixed ffprobe/ffmpeg operations within the existing parser resource boundary."""
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from materials.types import MaterialError


def installation():
    ffmpeg=os.getenv('ARTI_FFMPEG_CMD') or shutil.which('ffmpeg') or ''
    ffprobe=os.getenv('ARTI_FFPROBE_CMD') or shutil.which('ffprobe') or ''
    return ffmpeg,ffprobe


def invoke(command,*,timeout=45,max_output=2*1024**2):
    try:
        result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    except (subprocess.TimeoutExpired,OSError) as exc: raise MaterialError('media_decoder_failed') from exc
    if result.returncode or len(result.stdout)>max_output: raise MaterialError('media_decoder_failed')
    return result.stdout


def metadata(path,ffprobe):
    if not ffprobe or not Path(ffprobe).is_file(): raise MaterialError('ffprobe_unavailable')
    result=invoke([ffprobe,'-v','error','-protocol_whitelist','file,pipe','-show_entries',
        'format=duration,start_time:stream=codec_type,codec_name,width,height,start_time,duration,sample_rate,channels','-of','json',str(path)])
    try:
        value=json.loads(result); duration=round(float(value['format']['duration'])*1000)
        if not 0<duration<=86400000: raise ValueError()
        return value,duration
    except (ValueError,KeyError,TypeError) as exc: raise MaterialError('invalid_media_duration') from exc


def audio_analysis(data,mime,*,max_seconds=600,ffmpeg=None,ffprobe=None,start_ms=0,end_ms=None):
    import numpy as np
    import wave
    import base64
    import math
    configured=installation(); ffmpeg=ffmpeg or configured[0]; ffprobe=ffprobe or configured[1]
    if not ffmpeg or not Path(ffmpeg).is_file(): raise MaterialError('ffmpeg_unavailable')
    if not 1<=max_seconds<=900 or type(start_ms) is not int or start_ms<0: raise MaterialError('audio_duration_budget')
    with tempfile.TemporaryDirectory(prefix='decode-') as directory:
        root=Path(directory); original=root/'original'; original.write_bytes(data)
        info,duration=metadata(original,ffprobe)
        if not any(s.get('codec_type')=='audio' for s in info.get('streams',[])): raise MaterialError('audio_stream_unavailable')
        end=min(duration,start_ms+max_seconds*1000,end_ms if end_ms is not None else duration)
        if start_ms>=end: raise MaterialError('invalid_audio_interval')
        pcm=root/'pcm.wav'; clip=root/'clip.mp3'
        common=[ffmpeg,'-nostdin','-v','error','-protocol_whitelist','file,pipe','-threads','1','-ss',str(start_ms/1000),'-i',str(original),'-t',str((end-start_ms)/1000),'-map','0:a:0','-vn']
        invoke(common+['-ac','1','-ar','16000','-c:a','pcm_s16le','-y',str(pcm)],timeout=60)
        invoke(common+['-ac','1','-ar','16000','-c:a','libmp3lame','-b:a','48k','-y',str(clip)],timeout=60)
        with wave.open(str(pcm),'rb') as audio:
            samples=np.frombuffer(audio.readframes(audio.getnframes()),dtype='<i2').astype(np.float64)/32768
        origin=float(info['format'].get('start_time',0) or 0)
        stream=next(s for s in info['streams'] if s.get('codec_type')=='audio')
        first=max(start_ms,round((float(stream.get('start_time',0) or 0)-origin)*1000))
        actual_end=min(duration,first+round(len(samples)/16),end)
        if actual_end<=first: raise MaterialError('audio_stream_unavailable')
        start_ms,end=first,actual_end
        window=1600; acoustic=[]; silence=[]; silence_start=None
        for i in range(0,len(samples),window):
            chunk=samples[i:i+window]; rms=float(np.sqrt(np.mean(chunk**2))) if len(chunk) else 0
            ms=start_ms+round(i/16); stop=min(end,start_ms+round((i+len(chunk))/16))
            quiet=rms<.01
            if quiet and silence_start is None: silence_start=ms
            if not quiet and silence_start is not None:
                if ms-silence_start>=400: silence.append(dict(start_ms=silence_start,end_ms=ms,status='low_energy_observed'))
                silence_start=None
            if len(acoustic)<600 and i%(window*10)==0:
                acoustic.append(dict(start_ms=ms,end_ms=stop,rms=round(rms,6),zero_crossing_rate=round(float(np.mean(np.diff(np.signbit(chunk)))) if len(chunk)>1 else 0,6),
                    interpretation='acoustic_observation_not_emotion',quality='unassessed'))
        if silence_start is not None and end-silence_start>=400: silence.append(dict(start_ms=silence_start,end_ms=end,status='low_energy_observed'))
        if clip.stat().st_size>5*1024**2: raise MaterialError('audio_clip_budget')
        return dict(duration_ms=duration,start_ms=start_ms,end_ms=end,mime='audio/mpeg',audio_base64=base64.b64encode(clip.read_bytes()).decode(),
            silence=silence[:600],acoustic=acoustic,streams=info['streams'],limitations=['energy_vad_heuristic','overlap_detection_unavailable'],
            processed_ms=end-start_ms)
