"""Bounded cleanup for child processes created by this application."""
import asyncio
import os
import signal


async def stop_process(process, *, process_group=False, grace=2.):
    """Stop only an owned child/session; never leave cancellation waiting forever."""
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
