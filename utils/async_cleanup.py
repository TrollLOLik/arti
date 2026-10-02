"""Bounded cleanup remains owned even when its caller is cancelled again."""
import asyncio


async def await_owned(awaitable, *, timeout=10):
    async def bounded():
        async with asyncio.timeout(timeout):
            return await awaitable

    task = asyncio.create_task(bounded())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    # Always retrieve the result/exception before the owner can leave. The
    # bounded task also waits for cancellation of its SQL operation to settle.
    try:
        result = task.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError() from None
        raise
    if cancelled:
        raise asyncio.CancelledError()
    return result
