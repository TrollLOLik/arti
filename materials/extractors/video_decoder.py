"""Fixed, bounded frame decoding with actual native PTS, never guessed times."""
import base64,json,os,re,subprocess,tempfile
from pathlib import Path
from materials.extractors.media import installation,metadata
from materials.types import MaterialError


def captured(command,timeout=60):
    # ffmpeg has a fixed frame cap; stderr is bounded on disk by the worker job.
    with tempfile.TemporaryFile() as log:
        try:
            r=subprocess.run(command,stdout=subprocess.DEVNULL,stderr=log,timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        except (OSError,subprocess.TimeoutExpired) as exc: raise MaterialError('video_decoder_failed') from exc
        if log.tell()>512000 or r.returncode: raise MaterialError('video_decoder_failed')
        log.seek(0); text=log.read().decode('utf-8',errors='replace')
    return [round(float(p)*1000) for p in re.findall(r'pts_time:([-+0-9.e]+)',text)]


def video_frames(data,mime,*,max_seconds=600,max_frames=24,ffmpeg=None,ffprobe=None,start_ms=0,end_ms=None,dense=False):
    if not 1<=max_seconds<=900 or not 3<=max_frames<=32 or type(start_ms) is not int or start_ms<0: raise MaterialError('video_frame_budget')
    configured=installation(); ffmpeg=ffmpeg or configured[0]; ffprobe=ffprobe or configured[1]
    if not ffmpeg or not Path(ffmpeg).is_file(): raise MaterialError('ffmpeg_unavailable')
    with tempfile.TemporaryDirectory(prefix='frames-') as directory:
        root=Path(directory); original=root/'original'; original.write_bytes(data)
        info,duration=metadata(original,ffprobe); streams=info.get('streams',[])
        if not any(s.get('codec_type')=='video' for s in streams): raise MaterialError('video_stream_unavailable')
        end=min(duration,start_ms+max_seconds*1000,end_ms if end_ms is not None else duration)
        if start_ms>=end: raise MaterialError('invalid_video_interval')
        origin=round(float(info['format'].get('start_time',0) or 0)*1000)
        base=[ffmpeg,'-nostdin','-v','info','-protocol_whitelist','file,pipe','-threads','1','-copyts']
        scale="scale=768:432:force_original_aspect_ratio=decrease,format=yuvj420p"
        frames=[]; encoded_size=0
        def add(path,pts,method):
            nonlocal encoded_size
            ms=pts-origin
            if not start_ms<=ms<end or any(f['timestamp_ms']==ms for f in frames): return
            content=path.read_bytes()
            if encoded_size+len(content)>2*1024**2: return
            encoded_size+=len(content)
            frames.append(dict(timestamp_ms=ms,mime='image/jpeg',image_base64=base64.b64encode(content).decode(),method=method))
        uniform=max_frames if dense else max(3,max_frames//2)
        for i in range(uniform):
            requested=start_ms+round(i*max(0,end-start_ms-100)/(uniform-1)); path=root/'frame.jpg'
            pts=captured(base+['-ss',str(requested/1000),'-i',str(original),'-map','0:v:0','-an','-vf',scale+',showinfo','-frames:v','1','-fps_mode','passthrough','-threads','1','-update','1','-q:v','3','-y',str(path)],timeout=25)
            if pts and path.is_file(): add(path,pts[0],'dense_interval' if dense else 'uniform')
        scenes=[]
        if not dense:
            pattern=str(root/'scene_%03d.jpg'); cap=max_frames-uniform
            pts=captured(base+['-ss',str(start_ms/1000),'-i',str(original),'-t',str((end-start_ms)/1000),'-map','0:v:0','-an',
                '-vf',"select='gt(scene,0.12)',"+scale+',showinfo','-frames:v',str(cap),'-fps_mode','passthrough','-threads','1','-q:v','3','-y',pattern])
            for path,ms in zip(sorted(root.glob('scene_*.jpg')),pts):
                if start_ms<=ms-origin<end: scenes.append(ms-origin)
                add(path,ms,'scene_boundary_heuristic')
        frames.sort(key=lambda f:f['timestamp_ms'])
        if not frames: raise MaterialError('video_frames_unavailable')
        limitations=['sparse_frames_do_not_prove_event_absence','scene_boundary_heuristic','speaker_visibility_unconfirmed','frame_byte_budget_2MiB']
        if end<duration or start_ms: limitations.append('video_duration_budget_reached')
        if not dense and len(scenes)>=max_frames-uniform: limitations.append('scene_boundary_budget_reached')
        return dict(duration_ms=duration,start_ms=start_ms,end_ms=end,frames=frames,scenes=scenes,streams=streams,
            has_audio=any(s.get('codec_type')=='audio' for s in streams),limitations=limitations,origin_pts_ms=origin)
