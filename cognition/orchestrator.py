"""Observed input and delivered output are separate events; no Telegram side effects."""
import asyncio
from weakref import WeakValueDictionary

from cognition.affect import appraise, expression
from cognition.repositories import StaleRevision


class CognitiveOrchestrator:
    def __init__(self, repository, interpreter):
        self.repository = repository
        self.interpreter = interpreter
        self._locks = WeakValueDictionary()

    async def observe(self, event):
        lock = self._locks.setdefault(event.context.identity(), asyncio.Lock())
        async with lock:
            cid, eid = await self.repository.observe(event)
            if await self.repository.applied(cid, event.evidence.independent_group):
                return expression(await self.repository.state(cid))
            interpreted = await self.interpreter.interpret(event)
            # Reuse validated meaning on a CAS retry; never repeat an LLM call or
            # apply an increment to a stale snapshot.
            for _ in range(3):
                old = await self.repository.state(cid)
                new = appraise(old, event, interpreted.perception)
                try:
                    await self.repository.commit(cid, eid, interpreted.perception, old.revision, new)
                    return expression(await self.repository.state(cid))
                except StaleRevision:
                    continue
            raise StaleRevision()
