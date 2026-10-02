"""Isolated accepted voice synthesis with bounded owned-process lifetime."""
import asyncio
import json
import os
import sys
from pathlib import Path

MAX_MEDIA_BYTES=512*1024**2
MAX_MANIFEST_BYTES=128*1024


def _work_directory(path):
    from bot.media_spool import _check_path
    if not isinstance(path,Path): raise ValueError('invalid_clone_work_directory')
    _check_path(path,directory=True)
    return path.resolve(strict=True)


def _inside_file(path,work,*,maximum=MAX_MEDIA_BYTES):
    from bot.media_spool import _check_path
    if not isinstance(path,Path): raise ValueError('invalid_clone_output')
    path=Path(os.path.abspath(path))
    if not path.is_relative_to(work): raise ValueError('clone_path_outside_attempt')
    info=_check_path(path)
    import stat
    if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=maximum:
        raise ValueError('invalid_clone_file')
    return path


def _read_json(path,maximum):
    from bot.media_spool import _open_regular
    with _open_regular(path) as handle:
        raw=handle.read(maximum+1)
    if len(raw)>maximum: raise ValueError('clone_metadata_budget')
    try: value=json.loads(raw)
    except (ValueError,UnicodeDecodeError): raise ValueError('invalid_clone_metadata') from None
    if not isinstance(value,dict): raise ValueError('invalid_clone_metadata')
    return value


def _write_json(path,value,maximum):
    from bot.media_spool import _open_regular
    raw=json.dumps(value,ensure_ascii=False).encode('utf-8')
    if len(raw)>maximum: raise ValueError('clone_metadata_budget')
    with _open_regular(path,os.O_RDWR|os.O_CREAT|os.O_EXCL) as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())


async def _copy_reference(reference,work):
    """Yield between bounded chunks; cancellation leaves no background writer."""
    from bot.media_spool import _open_regular
    if not isinstance(reference,Path): raise ValueError('invalid_clone_reference')
    with _open_regular(reference) as source:
        initial=os.fstat(source.fileno())
        if not 0<initial.st_size<=MAX_MEDIA_BYTES: raise ValueError('invalid_clone_reference')
        with _open_regular(work/'reference.wav',os.O_RDWR|os.O_CREAT|os.O_EXCL) as target:
            total=0
            while chunk:=source.read(1024*1024):
                total+=len(chunk)
                if total>MAX_MEDIA_BYTES: raise ValueError('clone_reference_budget')
                target.write(chunk)
                await asyncio.sleep(0)
            final=os.fstat(source.fileno())
            if total!=initial.st_size or (initial.st_size,initial.st_mtime_ns)!=(final.st_size,final.st_mtime_ns):
                raise ValueError('clone_reference_changed')
            target.flush(); os.fsync(target.fileno())


async def generate_clone(reference: Path,text: str,work_dir: Path):
    from utils.process_limits import create_owned_subprocess_exec,communicate_bounded
    if not isinstance(text,str) or not text.strip() or len(text)>20000:
        raise ValueError('invalid_clone_text')
    budget=float(os.getenv('ARTI_VCLONE_TIMEOUT_SECONDS','1800'))
    if not 1<=budget<=14400: raise ValueError('invalid_clone_timeout')
    work=_work_directory(work_dir)
    await _copy_reference(reference,work)
    manifest=work/'clone-input.json'
    # Only a fixed relative leaf crosses into the worker, never a restored path.
    _write_json(manifest,dict(version=1,reference='reference.wav',text=text),MAX_MANIFEST_BYTES)
    process=await create_owned_subprocess_exec(sys.executable,'-m','ai.voice_clone_worker',str(manifest),
        cwd=str(Path(__file__).resolve().parent.parent),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    await communicate_bounded(process,budget)
    if process.returncode!=0: raise RuntimeError('clone_generation_failed')
    metadata=_read_json(work/'clone-result.json',1024)
    if set(metadata)!={'channel','leaf'}: raise ValueError('invalid_clone_output')
    channel,leaf=metadata.get('channel'),metadata.get('leaf')
    if (channel,leaf) not in (('voice','result.ogg'),('audio','result.mp3')):
        raise ValueError('invalid_clone_output')
    return _inside_file(work/leaf,work),channel
