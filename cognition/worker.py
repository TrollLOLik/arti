"""Owned worker lifecycle with renewal, bounded drain and recoverable cancellation."""
import asyncio
import logging
from cognition.interpreter import InterpreterFailure
from cognition.repositories import StaleRevision,SuppressedEvidence

logger = logging.getLogger(__name__)


class CognitiveWorker:
    def __init__(self,queue,handler,poll_seconds=.25):
        self.queue,self.handler = queue,handler
        self.poll_seconds = poll_seconds
        self.stopping = asyncio.Event()
        self.task = None

    def start(self):
        if self.task is None or self.task.done():
            self.stopping.clear()
            self.task = asyncio.create_task(self.run(),name='cognitive-worker')
        return self.task

    async def stop(self,timeout=20):
        self.stopping.set()
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task),timeout)
            except asyncio.TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task,return_exceptions=True)

    async def _renew(self,job):
        while True:
            await asyncio.sleep(40)
            if not await self.queue.renew(job['id'],job['lease_token']):
                raise StaleRevision()

    async def run(self):
        while not self.stopping.is_set():
            try:
                await self._run()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error('Cognitive worker connection failed; restarting without payload logging')
                await asyncio.sleep(1)

    async def _run(self):
        sweep_at = 0.
        while not self.stopping.is_set():
            now = asyncio.get_running_loop().time()
            if now-sweep_at>=60:
                async with self.queue.pool.acquire() as conn:
                    await conn.execute("UPDATE cognitive_outbox SET status='delivery_unknown',updated_at=NOW() WHERE status='sending' AND updated_at<NOW()-INTERVAL '10 minutes'")
                sweep_at = now
            job = await self.queue.claim()
            if not job:
                try:
                    await asyncio.wait_for(self.stopping.wait(),self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
                continue
            renew = asyncio.create_task(self._renew(job))
            work = asyncio.create_task(self.handler(job))
            try:
                done,_ = await asyncio.wait((work,renew),return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    t.result()
                if work not in done:
                    raise StaleRevision()
                await self.queue.finish(job['id'],job['lease_token'])
            except asyncio.CancelledError:
                await self.queue.fail(job['id'],job['lease_token'],'internal_error')
                raise
            except SuppressedEvidence:
                # Forget has already cancelled the affected job.
                await self.queue.finish(job['id'],job['lease_token'])
            except (InterpreterFailure,StaleRevision) as exc:
                code = exc.code if isinstance(exc,InterpreterFailure) else 'stale_revision'
                if code not in ('timeout','provider_unavailable','invalid_perception','output_truncated','stale_revision'):
                    code = 'provider_unavailable'
                await self.queue.fail(job['id'],job['lease_token'],code)
            except Exception:
                logger.error('Cognitive job failed: id=%s kind=%s',job['id'],job['kind'])
                await self.queue.fail(job['id'],job['lease_token'],'internal_error')
            finally:
                renew.cancel()
                work.cancel()
                await asyncio.gather(renew,work,return_exceptions=True)
