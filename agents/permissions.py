import uuid
from hashlib import sha256
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from materials.types import MaterialError,canonical

def content_digest(tool,args): return sha256(canonical([tool.name,tool.version,args]).encode()).hexdigest()

class CapabilityRepository:
    def __init__(self,pool): self.pool=pool
    async def issue(self,actor,tool,args,*,request_id,expires_at,max_cost,origin):
        # Only trusted transport handlers pass origin=user; the planner has no issue tool.
        if origin!='user' or not actor.user_id or not request_id or expires_at<=datetime.now(timezone.utc) or expires_at>datetime.now(timezone.utc)+timedelta(days=365) or Decimal(max_cost)<Decimal(tool.max_cost): raise MaterialError('capability_authorization_invalid')
        from agents.tools.registry import validate_schema
        validate_schema(tool.input_schema,args); id=uuid.uuid4().hex
        async with self.pool.acquire() as conn:
            row=await conn.fetchrow('''INSERT INTO arti_capability_grants(id,realm,owner_id,scope_key,tools,resources,audience,recipient,content_digest,max_cost,expires_at,request_id)
             VALUES($1,$2,$3,$4,$5,$6,$4,$7,$8,$9,$10,$11) ON CONFLICT(realm,owner_id,request_id,content_digest) DO NOTHING RETURNING id''',id,actor.realm,actor.user_id,actor.scope.key,[tool.name],list(tool.resources(args)),str(tool.recipient(args)),content_digest(tool,args),Decimal(max_cost),expires_at,request_id)
            if not row: id=await conn.fetchval('SELECT id FROM arti_capability_grants WHERE realm=$1 AND owner_id=$2 AND request_id=$3 AND content_digest=$4',actor.realm,actor.user_id,request_id,content_digest(tool,args))
        return id
    async def validate(self,id,actor,tool,args):
        async with self.pool.acquire() as conn: row=await conn.fetchrow('SELECT * FROM arti_capability_grants WHERE id=$1',id)
        if not row or row['realm']!=actor.realm or row['owner_id']!=actor.user_id or row['scope_key']!=actor.scope.key or row['audience']!=actor.scope.key or row['revoked_at'] or row['expires_at']<=datetime.now(timezone.utc) or tool.name not in row['tools'] or not set(tool.resources(args))<=set(row['resources']) or str(tool.recipient(args))!=row['recipient'] or content_digest(tool,args)!=row['content_digest'] or Decimal(tool.max_cost)>row['max_cost']: raise MaterialError('capability_denied')
    async def revoke(self,id,actor):
        async with self.pool.acquire() as conn:
            row=await conn.fetchrow('UPDATE arti_capability_grants SET revoked_at=NOW() WHERE id=$1 AND realm=$2 AND owner_id=$3 RETURNING id',id,actor.realm,actor.user_id)
            if not row: raise MaterialError('capability_denied')
