"""Private subprocess entrypoint; validate attempt paths before provider imports."""
import asyncio
import math
import re
import sys
from pathlib import Path
from ai.voice_clone_job import _work_directory,_inside_file,_read_json,_write_json,MAX_MANIFEST_BYTES


async def _duration(output):
    from utils.process_limits import stop_process
    probe=await asyncio.create_subprocess_exec('ffprobe','-v','error','-protocol_whitelist','file,pipe',
        '-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(output),
        stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(15):
            raw=b''
            while len(raw)<=256:
                chunk=await probe.stdout.read(257-len(raw))
                if not chunk: break
                raw+=chunk
            if len(raw)>256: raise RuntimeError('clone_probe_output_budget')
            await probe.wait()
        if probe.returncode!=0: raise RuntimeError('clone_probe_failed')
        try: duration=float(raw.decode('ascii').strip())
        except (ValueError,UnicodeError): raise RuntimeError('clone_probe_failed') from None
        if not math.isfinite(duration) or duration<=0: raise RuntimeError('clone_probe_failed')
        return duration
    finally:
        if probe.returncode is None: await asyncio.shield(stop_process(probe))


async def run(manifest):
    from utils.process_limits import communicate_bounded
    if not isinstance(manifest,Path) or manifest.name!='clone-input.json':
        raise ValueError('invalid_clone_manifest')
    work=_work_directory(manifest.parent)
    data=_read_json(_inside_file(manifest,work,maximum=MAX_MANIFEST_BYTES),MAX_MANIFEST_BYTES)
    if set(data)!={'version','reference','text'} or type(data['version']) is not int or data['version']!=1 or data['reference']!='reference.wav':
        raise ValueError('invalid_clone_manifest')
    text=data['text']
    if not isinstance(text,str) or not text.strip() or len(text)>20000: raise ValueError('invalid_clone_text')
    reference=_inside_file(work/'reference.wav',work)
    # A fresh attempt never overwrites either generated result or metadata.
    if any((work/leaf).exists() or (work/leaf).is_symlink() for leaf in ('result.ogg','result.mp3','clone-result.json')):
        raise ValueError('clone_output_already_exists')
    text=text.strip(); match=re.match(r'^\(([^)]+)\)\s*(.*)',text,re.S)
    direction,body=(match.group(1),match.group(2)) if match else ('',text)
    if not body.strip() or len(direction)>1000: raise ValueError('invalid_clone_text')
    from ai.voice_clone import normalize_text_via_llm,synthesize_with_clone
    normalized=await normalize_text_via_llm(body)
    if not isinstance(normalized,str) or not normalized.strip() or len(normalized)>60000:
        raise ValueError('invalid_clone_normalized_text')
    output=_inside_file(await synthesize_with_clone(reference,normalized,work,direction),work)
    duration=await _duration(output)
    short=duration<=30
    leaf,channel=('result.ogg','voice') if short else ('result.mp3','audio')
    codec=['-c:a','libopus','-b:a','64k'] if short else ['-c:a','libmp3lame','-b:a','128k']
    # The outer owned process tree contains these decoder children. No shell,
    # interactive stdin, external URL protocols, or overwrite of existing paths.
    converter=await asyncio.create_subprocess_exec('ffmpeg','-nostdin','-n','-v','error',
        '-protocol_whitelist','file,pipe','-i',str(output),'-map','0:a:0','-vn',*codec,str(work/leaf),
        stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    await communicate_bounded(converter,120)
    if converter.returncode!=0: raise RuntimeError('clone_conversion_failed')
    _inside_file(work/leaf,work)
    _write_json(work/'clone-result.json',dict(leaf=leaf,channel=channel),1024)


if __name__=='__main__':
    asyncio.run(run(Path(sys.argv[1])))
