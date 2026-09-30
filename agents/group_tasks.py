"""Public subscriptions enter G01-G10; private sources never become group input."""
from datetime import datetime,timedelta
from materials.types import MaterialError,canonical

async def guard_subscription(pool,p,conn=None):
    async def check(c):
        row=await c.fetchrow('''SELECT o.status,o.revision,o.owner_id,d.payload,d.invalidated_at,t.status AS task_status,p.status AS project_status,p.access_generation,m.role
         FROM arti_workflow_objects o JOIN arti_projects p ON p.id=o.project_id JOIN arti_project_members m ON m.project_id=p.id AND m.user_id=o.owner_id
         JOIN material_derivatives d ON d.id=o.head JOIN arti_tasks t ON t.id=$2 WHERE o.id=$1''',p['subscription_id'],p['task_id'])
        if not row or row['status']!='active' or row['revision']!=p['subscription_revision'] or row['project_status']!='active' or row['task_status']!='succeeded' or row['invalidated_at'] or row['payload'] is None or row['access_generation']!=p['access_generation'] or row['owner_id']!=p['owner_id'] or row['role'] not in ('owner','manager','editor'): return False
        from agents.tasks import decoded
        if decoded(row['payload'])['body']['audience']!=p['audience']: return False
        run=await c.fetchrow('SELECT * FROM arti_subscription_runs WHERE subscription_id=$1 AND revision=$2 AND occurrence=$3',p['subscription_id'],p['subscription_revision'],datetime.fromisoformat(p['occurrence']))
        return bool(run and run['status']=='ready' and run['fingerprint']==p['fingerprint'])
    if conn: return await check(conn)
    async with pool.acquire() as c: return await check(c)

async def propose_subscription(service,row,outputs,actor,key,bot):
    from cognition.runtime import get_runtime
    from cognition.scope import TransportScope
    from agents.subscriptions import SubscriptionRepository
    from agents.tools.core import build_registry
    runtime=get_runtime()
    if not runtime or runtime.mode=='legacy': raise MaterialError('group_runtime_unavailable')
    member=await bot.get_chat_member(actor.scope.chat_id,actor.user_id)
    if member.status not in ('member','administrator','creator'): raise MaterialError('group_member_left')
    cid=await runtime.groups.context_id(TransportScope(actor.scope.chat_id,actor.scope.topic_id,actor.scope.chat_type,actor.user_id),actor.scope.mode)
    policy,revision=await runtime.groups.policies.get(actor.scope.chat_id,actor.scope.topic_id)
    if policy.reason(runtime.clock(),'initiative') or await runtime.groups.policies.opted_out(actor.scope.chat_id,actor.user_id): return
    repo=SubscriptionRepository(service.repository,build_registry()); sub=await repo.get(row['subscription_id'],actor,'subscription')
    async with repo.pool.acquire() as conn:
        refs=await repo.derivatives._chain(conn,sub['head'],actor)
        source=await conn.fetchrow('''SELECT e.* FROM cognitive_events e JOIN material_assets a ON a.source_id=e.source_id AND a.owner_id=e.owner_id
         WHERE a.id=ANY($1::text[]) AND e.context_id=$2 AND e.owner_id=$3 AND e.origin='user' AND e.suppressed_at IS NULL ORDER BY e.id DESC LIMIT 1''',[r['asset_id'] for r in refs],cid,actor.user_id)
        run=await conn.fetchrow('SELECT * FROM arti_subscription_runs WHERE subscription_id=$1 AND revision=$2 AND occurrence=$3',row['subscription_id'],row['revision'],row['occurrence'])
    if not source: raise MaterialError('group_workflow_source_missing')
    from cognition.serialization import load_event
    event=load_event(source['payload'])
    p=dict(owner_id=actor.user_id,message_id=int(event.event_id.split(':')[2]),source_id=event.evidence.source_id,kind='followup',mode=policy.mode,reactions=False,
      subscription_id=row['subscription_id'],subscription_revision=row['revision'],task_id=row['task_id'],occurrence=row['occurrence'].isoformat(),fingerprint=run['fingerprint'],access_generation=row['access_generation'],audience=actor.scope.key,
      workflow_text=f"Новый проверенный выпуск проекта. Результат {row['task_id']}; /task show {row['task_id']}. Доступ ограничен участниками проекта и этим топиком.")
    if not await guard_subscription(repo.pool,p): raise MaterialError('group_workflow_stale')
    async with repo.pool.acquire() as conn:
        await conn.execute('''INSERT INTO group_candidates(context_id,candidate_key,kind,source_ids,payload,created_at,due_at,expires_at,policy_revision)
         VALUES($1,$2,'followup',$3,$4::jsonb,$5,$5,$6,$7) ON CONFLICT(context_id,candidate_key) DO NOTHING''',cid,key,[source['id']],canonical(p),runtime.clock(),runtime.clock()+timedelta(hours=24),revision)

async def record_group_delivery(pool,p,status,key):
    async with pool.acquire() as conn,conn.transaction():
        run=await conn.fetchrow('''UPDATE arti_subscription_runs SET status=$4,delivery_key=$5 WHERE subscription_id=$1 AND revision=$2 AND occurrence=$3 AND status='ready' RETURNING fingerprint''',p['subscription_id'],p['subscription_revision'],datetime.fromisoformat(p['occurrence']),status,key)
        if run and status=='delivered': await conn.execute('UPDATE arti_subscription_cursor SET fingerprint=$2,last_run=$3 WHERE subscription_id=$1 AND revision=$4',p['subscription_id'],run['fingerprint'],datetime.fromisoformat(p['occurrence']),p['subscription_revision'])
