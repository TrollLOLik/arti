from datetime import datetime,timezone,timedelta
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from hashlib import sha256
from projects.workflows import WorkflowRepository
from agents.procedures import ProcedureRepository
from materials.types import MaterialError,canonical

def schedule_after(schedule,after):
    if after.tzinfo is None: raise MaterialError('schedule_timezone_required')
    if set(schedule)-{'kind','timezone','hour','minute','weekdays','seconds','anchor'}: raise MaterialError('schedule_invalid')
    try: zone=ZoneInfo(schedule['timezone'])
    except (KeyError,ZoneInfoNotFoundError): raise MaterialError('schedule_timezone_invalid') from None
    if schedule.get('kind')=='interval':
        seconds=schedule.get('seconds')
        if type(seconds) is not int or not 300<=seconds<=31*86400: raise MaterialError('schedule_interval_invalid')
        anchor=datetime.fromisoformat(schedule['anchor'])
        if anchor.tzinfo is None: raise MaterialError('schedule_timezone_required')
        anchor=anchor.astimezone(timezone.utc); count=max(0,int((after-anchor).total_seconds()//seconds)+1)
        return anchor+timedelta(seconds=count*seconds)
    if schedule.get('kind')!='daily' or type(schedule.get('hour')) is not int or type(schedule.get('minute')) is not int or not 0<=schedule['hour']<=23 or not 0<=schedule['minute']<=59: raise MaterialError('schedule_daily_invalid')
    weekdays=schedule.get('weekdays',list(range(7)))
    if not weekdays or any(type(d) is not int or not 0<=d<=6 for d in weekdays): raise MaterialError('schedule_weekday_invalid')
    local=after.astimezone(zone)
    for day in range(9):
        date=local.date()+timedelta(days=day)
        if date.weekday() not in weekdays: continue
        naive=datetime(date.year,date.month,date.day,schedule['hour'],schedule['minute'])
        # Fold: first real occurrence only. Gap: first valid minute after requested wall time.
        for shift in range(181):
            candidate=(naive+timedelta(minutes=shift)).replace(tzinfo=zone,fold=0); utc=candidate.astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None)==candidate.replace(tzinfo=None):
                if utc>after.astimezone(timezone.utc): return utc
                break
    raise MaterialError('schedule_occurrence_missing')

def semantic_fingerprint(value):
    def clean(v):
        if isinstance(v,dict): return {k:clean(x) for k,x in v.items() if k not in ('base64','read_at','created_at','cost','receipt','id','revision','asset_id','derivative_id','dataset_id','computation_id','extraction_id','sha256','files','source','sources','inputs','diagnostics','style','illustrations')}
        if isinstance(v,list): return [clean(x) for x in v]
        return v
    return sha256(canonical(clean(value)).encode()).hexdigest()

class SubscriptionRepository(WorkflowRepository):
    def __init__(self,materials,registry): super().__init__(materials); self.registry=registry
    async def subscribe(self,actor,procedure_id,bindings,schedule,sources,*,confirmed,origin,max_cost='1',max_calls=30,max_bytes=8*1024**2,until=None,max_runs=None):
        if not confirmed or origin!='user': raise MaterialError('subscription_confirmation_required')
        plan,procedure=await ProcedureRepository(self.materials,self.registry).instantiate(procedure_id,actor,bindings)
        next_at=schedule_after(schedule,datetime.now(timezone.utc))
        body=dict(procedure_id=procedure_id,procedure_revision=procedure['revision'],bindings=bindings,schedule=schedule,audience=actor.scope.key,owner=actor.user_id,access_generation=(await self.projects.get(procedure['project_id'],actor)).access_generation,max_cost=max_cost,max_calls=max_calls,max_bytes=max_bytes,missed_policy='coalesce_latest',fold_policy='first',gap_policy='next_valid_minute')
        from decimal import Decimal
        if not 0<=Decimal(max_cost)<=100 or not 1<=max_calls<=100 or not 1024<=max_bytes<=32*1024**2: raise MaterialError('subscription_budget_invalid')
        self.validate_completion(until,max_runs); body.update(until=until,max_runs=max_runs)
        row=await self.create(procedure['project_id'],actor,'subscription',body,sources,inputs=[procedure['head'],*plan.get('inputs',[])][:32],cursor_next=next_at)
        return row
    @staticmethod
    def validate_completion(until,max_runs):
        if until is not None:
            if not isinstance(until,str) or datetime.fromisoformat(until).tzinfo is None: raise MaterialError('subscription_completion_invalid')
        if max_runs is not None and (type(max_runs) is not int or not 1<=max_runs<=10000): raise MaterialError('subscription_completion_invalid')
    async def revise(self,id,actor,expected,changes,sources):
        old=await self.get(id,actor,'subscription')
        if old['owner_id']!=actor.user_id or not sources: raise MaterialError('subscription_owner_required')
        if not changes or set(changes)-{'bindings','schedule','max_cost','max_calls','max_bytes','until','max_runs'}: raise MaterialError('subscription_input_invalid')
        body=dict(old['body'],**changes)
        plan,procedure=await ProcedureRepository(self.materials,self.registry).instantiate(body['procedure_id'],actor,body['bindings'])
        body['procedure_revision']=procedure['revision']; body['access_generation']=(await self.projects.get(old['project_id'],actor)).access_generation
        from decimal import Decimal
        if not 0<=Decimal(body['max_cost'])<=100 or type(body['max_calls']) is not int or not 1<=body['max_calls']<=100 or type(body['max_bytes']) is not int or not 1024<=body['max_bytes']<=32*1024**2: raise MaterialError('subscription_budget_invalid')
        self.validate_completion(body.get('until'),body.get('max_runs')); schedule_after(body['schedule'],datetime.now(timezone.utc))
        # A new recipe requires a newly created subscription; no old example or grant
        # can silently become permission for a different confirmed procedure.
        if procedure['revision']!=old['body']['procedure_revision']: raise MaterialError('subscription_procedure_changed')
        return await self.update(id,actor,expected,body,sources=sources)
    async def describe(self,id,actor):
        row=await self.get(id,actor,'subscription')
        async with self.pool.acquire() as conn:
            cursor=await conn.fetchrow('SELECT * FROM arti_subscription_cursor WHERE subscription_id=$1',id)
            last=await conn.fetchrow('SELECT revision,occurrence,status,task_id FROM arti_subscription_runs WHERE subscription_id=$1 ORDER BY occurrence DESC LIMIT 1',id)
        return row,dict(next_at=cursor['next_at'].isoformat() if row['status']=='active' else None,last_result=dict(last) if last else None,paused_reason=cursor['paused_reason'])
    async def manage(self,id,actor,expected,action):
        old=await self.get(id,actor,'subscription')
        if old['owner_id']!=actor.user_id: raise MaterialError('subscription_owner_required')
        if action=='resume':
            _,p=await ProcedureRepository(self.materials,self.registry).instantiate(old['body']['procedure_id'],actor,old['body']['bindings'])
            if p['revision']!=old['body']['procedure_revision']: raise MaterialError('subscription_procedure_changed')
            body=old['body']; body['access_generation']=(await self.projects.get(old['project_id'],actor)).access_generation; body['control_generation']=body.get('control_generation',0)+1
            row=await self.update(id,actor,expected,body,status='active')
            return row
        return await self.control(id,actor,expected,action)
    async def record_result(self,id,actor,revision,occurrence,outputs,*,verified):
        row=await self.get(id,actor,'subscription')
        if not verified or row['status']!='active' or row['revision']!=revision: raise MaterialError('subscription_result_unverified')
        async with self.pool.acquire() as conn:
            task=await conn.fetchrow('SELECT t.* FROM arti_subscription_runs r JOIN arti_tasks t ON t.id=r.task_id WHERE r.subscription_id=$1 AND r.revision=$2 AND r.occurrence=$3',id,revision,occurrence)
        if not task or task['status']!='succeeded': raise MaterialError('subscription_result_unverified')
        from agents.tasks import TaskRepository
        from agents.verifier import verify
        tasks=TaskRepository(self.materials,self.registry); persisted=await tasks.outputs(task)
        plan=await tasks.derivatives.load(task['plan_id'],actor,'task_plan')
        if canonical(persisted)!=canonical(outputs) or not verify(plan,persisted)['complete']: raise MaterialError('subscription_result_unverified')
        fingerprint=semantic_fingerprint(outputs)
        async with self.pool.acquire() as conn,conn.transaction():
            cursor=await conn.fetchrow('SELECT * FROM arti_subscription_cursor WHERE subscription_id=$1 FOR UPDATE',id)
            if cursor['revision']!=revision: raise MaterialError('stale_subscription_revision')
            run=await conn.fetchrow('SELECT * FROM arti_subscription_runs WHERE subscription_id=$1 AND revision=$2 AND occurrence=$3 FOR UPDATE',id,revision,occurrence)
            if not run: raise MaterialError('subscription_run_missing')
            if run['status'] in ('unchanged','delivered','unknown'): return False
            if await conn.fetchval("SELECT 1 FROM arti_subscription_runs WHERE subscription_id=$1 AND status='unknown' AND fingerprint=$2",id,fingerprint):
                await conn.execute("UPDATE arti_subscription_runs SET status='unchanged',fingerprint=$4 WHERE subscription_id=$1 AND revision=$2 AND occurrence=$3",id,revision,occurrence,fingerprint); return False
            if cursor['fingerprint']==fingerprint:
                await conn.execute("UPDATE arti_subscription_runs SET status='unchanged',fingerprint=$4 WHERE subscription_id=$1 AND revision=$2 AND occurrence=$3",id,revision,occurrence,fingerprint); return False
            await conn.execute("UPDATE arti_subscription_runs SET status='ready',fingerprint=$4 WHERE subscription_id=$1 AND revision=$2 AND occurrence=$3",id,revision,occurrence,fingerprint)
        return True
    async def delivery_result(self,id,actor,revision,occurrence,status,key):
        if status not in ('delivered','unknown'): raise MaterialError('subscription_delivery_invalid')
        row=await self.get(id,actor,'subscription')
        if row['status']!='active' or row['revision']!=revision: raise MaterialError('stale_subscription_revision')
        async with self.pool.acquire() as conn,conn.transaction():
            run=await conn.fetchrow('SELECT * FROM arti_subscription_runs WHERE subscription_id=$1 AND revision=$2 AND occurrence=$3 FOR UPDATE',id,revision,occurrence)
            if not run or run['status']!='ready': raise MaterialError('subscription_delivery_repeated')
            await conn.execute('UPDATE arti_subscription_runs SET status=$4,delivery_key=$5 WHERE subscription_id=$1 AND revision=$2 AND occurrence=$3',id,revision,occurrence,status,key)
            # Unknown is terminal for this occurrence and never advances a known delivered baseline.
            if status=='delivered': await conn.execute('UPDATE arti_subscription_cursor SET fingerprint=$2,last_run=$3 WHERE subscription_id=$1 AND revision=$4',id,run['fingerprint'],occurrence,revision)
