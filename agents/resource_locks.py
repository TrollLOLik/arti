"""Session locks serialize shared effects across worker processes."""
from contextlib import asynccontextmanager
from hashlib import sha256
from materials.types import MaterialError

@asynccontextmanager
async def effect_lock(pool,tool,args):
    resources=sorted(set([*tool.resources(args),*([tool.recipient(args)] if tool.recipient(args) else [])])) if tool.effect!='read' else []
    if not resources:
        yield; return
    if len(resources)>16 or any(not isinstance(r,str) or len(r)>4000 for r in resources): raise MaterialError('tool_resource_invalid')
    keys=[int.from_bytes(sha256((tool.name+':'+r).encode()).digest()[:8],'big',signed=True) for r in resources]
    async with pool.acquire() as conn:
        held=[]
        try:
            for key in keys:
                if not await conn.fetchval('SELECT pg_try_advisory_lock($1::bigint)',key): raise MaterialError('step_resource_busy')
                held.append(key)
            yield
        finally:
            for key in reversed(held): await conn.execute('SELECT pg_advisory_unlock($1::bigint)',key)
