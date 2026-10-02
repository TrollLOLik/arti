"""Bounded cleanup for child processes created by this application."""
import asyncio
import os
import signal
import sys
from pathlib import Path

_launch_cleanups=set()


def _is_windows(): return os.name=='nt'


class _OwnedLeaseProcess:
    """Keep the helper's parent-death lease open while collecting payload IO."""
    _owned_control_lease=True
    stdin=None  # Payload stdin is deliberately DEVNULL, never the lease pipe.

    def __init__(self,process,*,windows=True):
        self._process=process
        self._owned_windows_job=windows
        self._owned_posix_watchdog=not windows
    def __getattr__(self,name): return getattr(self._process,name)
    def close_control(self):
        if self._process.stdin is not None: self._process.stdin.close()
    async def wait(self):
        code=await self._process.wait()
        self.close_control()
        return code
    async def communicate(self,input=None):
        if input is not None: raise ValueError('owned_payload_stdin_unsupported')
        async def read(stream): return await stream.read() if stream is not None else None
        stdout,stderr=await asyncio.gather(read(self.stdout),read(self.stderr))
        await self.wait()
        return stdout,stderr


_WindowsOwnedProcess=_OwnedLeaseProcess  # Internal compatibility for contract tests.


async def create_owned_subprocess_exec(*cmd,**kwargs):
    """Create only an owned process tree; Windows requires Job List support.

    Payload stdin is DEVNULL. A fixed Windows helper owns an atomic-at-creation
    kill-on-close Job Object, and its stdin is reserved for the parent's lease.
    POSIX uses a lease watchdog and an owned session. No shell or PID discovery.
    """
    windows=_is_windows()
    if not cmd or any(not isinstance(v,(str,os.PathLike)) for v in cmd):
        raise ValueError('owned_process_command_invalid')
    if any(k in kwargs for k in ('preexec_fn','executable','startupinfo','pass_fds')):
        raise ValueError('owned_process_unsafe_launch_option')
    command=[os.fspath(v) for v in cmd]
    if kwargs.pop('stdin',None) not in (None,asyncio.subprocess.DEVNULL):
        raise ValueError('owned_payload_stdin_unsupported')
    if windows:
        if kwargs.pop('creationflags',0) or kwargs.pop('start_new_session',False):
            raise ValueError('owned_process_unsafe_launch_option')
        helper=Path(__file__).with_name('windows_owned_process.py')
        command=[sys.executable,'-I',str(helper),'--',*command]
        kwargs.update(stdin=asyncio.subprocess.PIPE,creationflags=0x08000000,close_fds=True)
        # A GUI parent may have no usable inherited standard handles.
        for name in ('stdout','stderr'):
            if kwargs.get(name) is None: kwargs[name]=asyncio.subprocess.DEVNULL
    else:
        helper=Path(__file__).with_name('posix_owned_process.py')
        command=[sys.executable,'-I',str(helper),'--',*command]
        kwargs.update(stdin=asyncio.subprocess.PIPE,start_new_session=True,close_fds=True)
    launch=asyncio.create_task(asyncio.create_subprocess_exec(*command,**kwargs))
    process=None
    try:
        raw=await asyncio.shield(launch)
        process=_OwnedLeaseProcess(raw,windows=windows)
        # The helper cannot execute any payload before this explicit grant.
        raw.stdin.write(b'G')
        async with asyncio.timeout(10): await raw.stdin.drain()
        return process
    except BaseException:
        async def cleanup():
            try:
                child=process
                if child is None:
                    raw=await launch
                    child=_OwnedLeaseProcess(raw,windows=windows)
                await stop_process(child)
            except Exception: pass
        task=asyncio.create_task(cleanup()); _launch_cleanups.add(task)
        task.add_done_callback(_launch_cleanups.discard)
        try:
            async with asyncio.timeout(5): await asyncio.shield(task)
        except (TimeoutError,asyncio.CancelledError): pass
        raise


async def stop_process(process, *, process_group=False, grace=2.):
    """Stop only an owned child/session; never leave cancellation waiting forever."""
    process_group=process_group or getattr(process,'_owned_process_group',False)
    if getattr(process,'_owned_control_lease',False): process.close_control()
    def signal_child(force=False):
        try:
            if process_group and os.name=='posix':
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif process.returncode is None:
                process.kill() if force else process.terminate()
        except ProcessLookupError:
            pass
    signal_child()
    try:
        async with asyncio.timeout(grace): await process.communicate()
    except TimeoutError:
        signal_child(True)
        try:
            async with asyncio.timeout(grace): await process.communicate()
        except TimeoutError:
            # The kill was issued. Do not strand the worker on broken pipe IO.
            pass


async def communicate_bounded(process, timeout, *, process_group=False):
    try:
        async with asyncio.timeout(timeout): return await process.communicate()
    except BaseException:
        await asyncio.shield(stop_process(process,process_group=process_group))
        raise
