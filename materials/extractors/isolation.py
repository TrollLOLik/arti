"""Parser process boundary: bounded lifetime/memory/disk, no inherited secrets.

This is resource isolation, not a security sandbox for arbitrary executable code.
Only the repository's fixed worker is invoked; document data is never executed.
"""
import asyncio
from dataclasses import asdict, dataclass
import json
import logging
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from weakref import WeakKeyDictionary
from materials.types import MaterialError


@dataclass(frozen=True)
class WorkerLimits:
    wall_seconds: float = 90
    memory_mb: int = 768
    disk_mb: int = 48
    output_mb: int = 8

    def __post_init__(self):
        if not 0.1 <= self.wall_seconds <= 300 or not 128 <= self.memory_mb <= 2048 or not 8 <= self.disk_mb <= 128 or not 1 <= self.output_mb <= 16:
            raise MaterialError('invalid_worker_limits')


_slots = WeakKeyDictionary()
logger = logging.getLogger(__name__)


def safe_environment(directory):
    allowed = ('SystemRoot', 'WINDIR', 'COMSPEC', 'PATH', 'LANG', 'LC_ALL')
    env = {k: os.environ[k] for k in allowed if k in os.environ}
    env.update(TEMP=directory, TMP=directory, TMPDIR=directory,
               OMP_THREAD_LIMIT='1', OPENBLAS_NUM_THREADS='1', PYTHONIOENCODING='utf-8')
    return env


async def run_worker(request, data, limits, *, worker_path=None, trace=None):
    loop = asyncio.get_running_loop()
    slots = _slots.setdefault(loop, asyncio.Semaphore(2))
    async with slots:
        with tempfile.TemporaryDirectory(prefix='arti-parser-') as directory:
            root = Path(directory)
            (root / 'original').write_bytes(data)
            request = {**request, 'limits': asdict(limits)}
            (root / 'request.json').write_text(json.dumps(request), encoding='utf-8')
            worker = Path(worker_path or Path(__file__).with_name('worker.py')).resolve()
            kwargs = dict(cwd=root, env=safe_environment(directory),
                          stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if os.name == 'nt':
                import subprocess
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            else:
                kwargs['start_new_session'] = True
            process = await asyncio.create_subprocess_exec(sys.executable, '-I', str(worker), str(root), **kwargs)
            started = time.monotonic()
            peak_rss, peak_disk, status = 0, 0, 'cancelled'
            try:
                while process.returncode is None:
                    if time.monotonic() - started > limits.wall_seconds:
                        raise MaterialError('parser_timeout')
                    size = sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
                    peak_disk = max(peak_disk, size)
                    import psutil
                    try:
                        tree = psutil.Process(process.pid)
                        peak_rss = max(peak_rss,sum(p.memory_info().rss for p in [tree]+tree.children(recursive=True)))
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                    if size > limits.disk_mb * 1024**2:
                        raise MaterialError('parser_disk_budget')
                    try:
                        await asyncio.wait_for(process.wait(), timeout=0.1)
                    except asyncio.TimeoutError:
                        pass
                output = root / 'result.json'
                if not output.is_file() or output.stat().st_size > limits.output_mb * 1024**2:
                    raise MaterialError('parser_resource_failure')
                result = json.loads(output.read_text(encoding='utf-8'))
                if 'error' in result:
                    raise MaterialError(result['error'])
                if process.returncode:
                    raise MaterialError('parser_resource_failure')
                status = 'ok'
                return result
            except MaterialError as exc:
                status = exc.code
                raise
            finally:
                if process.returncode is None:
                    if os.name == 'nt':
                        process.kill()  # Closing worker's Job handle kills Tesseract too.
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    await process.wait()
                metrics = dict(status=status,elapsed_seconds=round(time.monotonic()-started,3),
                    sampled_peak_rss_mb=round(peak_rss/1024**2,2),peak_temporary_disk_mb=round(peak_disk/1024**2,2))
                if trace is not None:
                    trace.update(metrics)
                logger.info('Document worker finished: %s', json.dumps(metrics))


def apply_resource_limits(limits):
    """Called by worker before importing any native parser or reading originals."""
    if os.name != 'nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (limits['memory_mb'] * 1024**2,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (int(limits['wall_seconds']) + 1,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits['disk_mb'] * 1024**2,) * 2)
        return None
    import ctypes
    from ctypes import wintypes as wt
    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                    ('flags', wt.DWORD), ('min_working', ctypes.c_size_t), ('max_working', ctypes.c_size_t),
                    ('active_processes', wt.DWORD), ('affinity', ctypes.c_size_t),
                    ('priority', wt.DWORD), ('scheduling', wt.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(k, ctypes.c_uint64) for k in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                    ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    kernel.CreateJobObjectW.restype = wt.HANDLE
    kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    kernel.GetCurrentProcess.restype = wt.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    extended = Extended()
    extended.basic.flags = 0x2000 | 0x100 | 0x200 | 0x8 | 0x2 | 0x400
    extended.basic.active_processes = 4
    extended.basic.process_time = int(limits['wall_seconds'] * 10_000_000)
    extended.process_memory = limits['memory_mb'] * 1024**2
    extended.job_memory = int(limits['memory_mb'] * 1.5) * 1024**2
    if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(extended), ctypes.sizeof(extended)) or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        raise MaterialError('parser_isolation_unavailable')
    # Keep handle alive for entire worker lifetime. OS closes it on exit/crash.
    return job
