"""Versioned source-bound objects with project role checks and erasure semantics."""
import uuid
from materials.derivatives import DerivativeRepository
from materials.types import MaterialError
from projects.repository import ProjectRepository

class WorkflowRepository:
    def __init__(self,materials): self.materials=materials; self.pool=materials.pool; self.projects=ProjectRepository(materials); self.derivatives=DerivativeRepository(materials)
    async def create(self,project_id,actor,kind,body,sources,*,inputs=(),id=None,cursor_next=None):
        if kind not in ('decision','assignment','procedure','subscription','learning','style'): raise MaterialError('workflow_kind_invalid')
        if kind=='subscription' and cursor_next is None: raise MaterialError('subscription_cursor_required')
        (await self.projects.get(project_id,actor)).require('edit')
        head=await self.derivatives.save(actor,'workflow_'+kind,body,sources,inputs=inputs); id=id or uuid.uuid4().hex
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor); (await self.projects._get(conn,project_id,actor,lock=True)).require('edit')
            await self.derivatives._sources(conn,actor,await self.derivatives._chain(conn,head,actor))
            old=await conn.fetchrow('SELECT * FROM arti_workflow_objects WHERE id=$1',id)
            if old:
                if old['realm']!=actor.realm or old['owner_id']!=actor.user_id or old['head']!=head or old['project_id']!=project_id or old['kind']!=kind: raise MaterialError('workflow_identity_conflict')
            else:
                if await conn.fetchval("SELECT COUNT(*) FROM arti_workflow_objects WHERE realm=$1 AND status<>'deleted'",actor.realm)>=500: raise MaterialError('workflow_scope_quota')
                await conn.execute('INSERT INTO arti_workflow_objects(id,realm,owner_id,project_id,kind,head) VALUES($1,$2,$3,$4,$5,$6)',id,actor.realm,actor.user_id,project_id,kind,head)
                await conn.execute('INSERT INTO arti_workflow_versions VALUES($1,1,$2,$3)',id,head,actor.user_id)
                if kind=='subscription': await conn.execute('INSERT INTO arti_subscription_cursor(subscription_id,revision,next_at) VALUES($1,1,$2)',id,cursor_next)
        return await self.get(id,actor)
    async def get(self,id,actor,kind=None):
        async with self.pool.acquire() as conn:
            row=await conn.fetchrow('SELECT * FROM arti_workflow_objects WHERE id=$1',id)
            if not row or row['realm']!=actor.realm or row['status']=='deleted' or (kind and row['kind']!=kind): raise MaterialError('workflow_unavailable')
            (await self.projects._get(conn,row['project_id'],actor)).require('view',active=False)
        body=await self.derivatives.load(row['head'],actor,'workflow_'+row['kind'])
        return dict(row,body=body)
    async def update(self,id,actor,expected,body,*,right='edit',status=None,accept=False,clear_accept=False,inputs=(),sources=()):
        old=await self.get(id,actor)
        async with self.pool.acquire() as conn:
            refs=await self.derivatives._chain(conn,old['head'],actor)
            envelope=await self.derivatives._payload(conn,old['head'],actor,'workflow_'+old['kind'])
            inputs=list(dict.fromkeys([*envelope.get('inputs',[]),*inputs]))
        refs.extend(sources)
        head=await self.derivatives.save(actor,'workflow_'+old['kind'],body,refs,inputs=inputs)
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor); (await self.projects._get(conn,old['project_id'],actor,lock=True)).require(right)
            row=await conn.fetchrow('SELECT * FROM arti_workflow_objects WHERE id=$1 FOR UPDATE',id)
            if row['revision']!=expected or row['status']=='deleted': raise MaterialError('stale_workflow_revision')
            await self.derivatives._sources(conn,actor,await self.derivatives._chain(conn,head,actor))
            if head!=row['head'] and old['kind'] in ('procedure','subscription'):
                await self.derivatives._invalidate(conn,row['head'])
            await conn.execute('UPDATE arti_workflow_objects SET revision=revision+1,head=$2,status=$3,accepted=CASE WHEN $5 THEN NULL WHEN $4 THEN $2 ELSE accepted END WHERE id=$1',id,head,status or row['status'],accept,clear_accept)
            await conn.execute('INSERT INTO arti_workflow_versions VALUES($1,$2,$3,$4)',id,expected+1,head,actor.user_id)
            if old['kind']=='subscription':
                from agents.subscriptions import schedule_after
                from datetime import datetime,timezone
                changed=old['body'].get('bindings')!=body.get('bindings') or old['body'].get('procedure_revision')!=body.get('procedure_revision')
                await conn.execute('UPDATE arti_subscription_cursor SET revision=$2,next_at=$3,paused_reason=NULL,fingerprint=CASE WHEN $4 THEN NULL ELSE fingerprint END WHERE subscription_id=$1',id,expected+1,schedule_after(body['schedule'],datetime.now(timezone.utc)),changed)
        return await self.get(id,actor)
    async def control(self,id,actor,expected,action):
        old=await self.get(id,actor)
        if action not in ('pause','resume','delete') or old['owner_id']!=actor.user_id: raise MaterialError('workflow_control_denied')
        if action!='delete':
            body=old['body']
            if old['kind']=='subscription': body['control_generation']=body.get('control_generation',0)+1
            return await self.update(id,actor,expected,body,status={'pause':'paused','resume':'active'}[action])
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor); (await self.projects._get(conn,old['project_id'],actor,lock=True)).require('edit')
            row=await conn.fetchrow('SELECT * FROM arti_workflow_objects WHERE id=$1 FOR UPDATE',id)
            if row['revision']!=expected: raise MaterialError('stale_workflow_revision')
            await conn.execute("UPDATE arti_workflow_objects SET status='deleted',revision=revision+1 WHERE id=$1",id)
            heads=await conn.fetch('SELECT head FROM arti_workflow_versions WHERE object_id=$1',id)
            for h in heads:
                await conn.execute('''WITH RECURSIVE dependent(id) AS(SELECT $1::text UNION SELECT l.derivative_id FROM material_derivative_links l JOIN dependent d ON l.input_id=d.id)
                 UPDATE material_derivatives SET payload=NULL,invalidated_at=NOW() WHERE id IN(SELECT id FROM dependent)''',h['head'])
