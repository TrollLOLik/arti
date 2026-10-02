"""Explicit permissions; statistical receptivity can never grant permission."""
from dataclasses import dataclass,asdict
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json


@dataclass(frozen=True)
class GroupPolicy:
    mode: str = 'mentions'
    execution: str = 'shadow'
    full_visibility: bool = False
    timezone: str | None = None
    quiet_start: int = 23
    quiet_end: int = 9
    daily_limit: int = 3
    spacing_seconds: int = 900
    assessment_hourly_limit: int = 12
    max_share: float = .15
    retention_days: int = 30
    reactions: bool = False
    topic_seeds: bool = False
    disabled: bool = False
    paused_until: str | None = None

    def __post_init__(self):
        if self.mode not in ('mentions','useful','social') or self.execution not in ('shadow','live'):
            raise ValueError('Invalid group mode')
        for k in ('full_visibility','reactions','topic_seeds','disabled'):
            if not isinstance(getattr(self,k),bool): raise ValueError('Invalid policy boolean')
        for k,low,high in (('daily_limit',0,20),('spacing_seconds',60,86400),('assessment_hourly_limit',0,120),('retention_days',1,90),('quiet_start',0,23),('quiet_end',0,23)):
            v=getattr(self,k)
            if isinstance(v,bool) or not isinstance(v,int) or not low<=v<=high: raise ValueError('Invalid policy bound')
        if isinstance(self.max_share,bool) or not isinstance(self.max_share,(int,float)) or not .01<=self.max_share<=.5:
            raise ValueError('Invalid participation share')
        if self.timezone: ZoneInfo(self.timezone)
        if self.paused_until:
            if datetime.fromisoformat(self.paused_until).tzinfo is None: raise ValueError('Pause requires UTC offset')

    def reason(self,now,kind='initiative'):
        if self.disabled: return 'disabled'
        if self.paused_until and datetime.fromisoformat(self.paused_until)>now: return 'paused'
        if kind=='reminder': return None
        if self.mode=='mentions': return 'mentions_only'
        if not self.full_visibility: return 'partial_visibility'
        if self.timezone:
            hour=now.astimezone(ZoneInfo(self.timezone)).hour
            quiet=(self.quiet_start<=hour<self.quiet_end if self.quiet_start<self.quiet_end
                   else hour>=self.quiet_start or hour<self.quiet_end) if self.quiet_start!=self.quiet_end else False
            if quiet: return 'quiet_hours'
        return None


class PolicyRepository:
    def __init__(self,pool): self.pool=pool

    async def get(self,chat_id,topic_id,connection=None):
        async def read(conn):
            a=await conn.fetchrow('SELECT payload,revision FROM group_chat_settings WHERE chat_id=$1',chat_id)
            b=await conn.fetchrow('SELECT payload,revision FROM group_topic_settings WHERE chat_id=$1 AND topic_id=$2',chat_id,topic_id)
            return a,b
        if connection is not None: a,b=await read(connection)
        else:
            async with self.pool.acquire() as conn: a,b=await read(conn)
        from cognition.serialization import object_value
        data=object_value(a['payload']) if a else {}
        overrides=object_value(b['payload']) if b else {}
        group_disabled=data.get('disabled',False)
        pauses=[v for v in (data.get('paused_until'),overrides.get('paused_until')) if v]
        data={**data,**{k:v for k,v in overrides.items() if k in GroupPolicy.__dataclass_fields__}}
        data['disabled']=group_disabled or data.get('disabled',False)
        if pauses: data['paused_until']=max(pauses,key=datetime.fromisoformat)
        policy=GroupPolicy(**data)
        revision=hashlib.sha256(json.dumps([asdict(policy),a['revision'] if a else 0,b['revision'] if b else 0],sort_keys=True).encode()).hexdigest()[:24]
        return policy,revision

    async def set(self,chat_id,changes,topic_id=None):
        if not set(changes)<=set(GroupPolicy.__dataclass_fields__): raise ValueError('Unknown policy setting')
        from cognition.serialization import object_value,dump
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock($1::bigint)',chat_id)
            table='group_chat_settings' if topic_id is None else 'group_topic_settings'
            row=await conn.fetchval(f'SELECT payload FROM {table} WHERE chat_id=$1'+(' AND topic_id=$2' if topic_id is not None else ''),*([chat_id] if topic_id is None else [chat_id,topic_id]))
            data={**(object_value(row) if row else {}),**changes}
            GroupPolicy(**{k:v for k,v in data.items() if k in GroupPolicy.__dataclass_fields__})
            if topic_id is None:
                await conn.execute('''INSERT INTO group_chat_settings(chat_id,payload) VALUES($1,$2::jsonb)
                    ON CONFLICT(chat_id) DO UPDATE SET payload=EXCLUDED.payload,revision=group_chat_settings.revision+1,updated_at=NOW()''',chat_id,dump(data))
            else:
                await conn.execute('''INSERT INTO group_topic_settings(chat_id,topic_id,payload) VALUES($1,$2,$3::jsonb)
                    ON CONFLICT(chat_id,topic_id) DO UPDATE SET payload=EXCLUDED.payload,revision=group_topic_settings.revision+1''',chat_id,topic_id,dump(data))
            await conn.execute("UPDATE group_candidates SET status='cancelled',payload=NULL WHERE context_id IN (SELECT id FROM cognitive_contexts WHERE chat_id=$1) AND status IN ('pending','deferred','claimed')",chat_id)

    async def opt_out(self,chat_id,user_id,value):
        async with self.pool.acquire() as conn:
            await conn.execute('''INSERT INTO group_participant_settings VALUES($1,$2,$3)
                ON CONFLICT(chat_id,user_id) DO UPDATE SET opt_out=EXCLUDED.opt_out''',chat_id,user_id,value)
            if value:
                await conn.execute("UPDATE group_candidates SET status='cancelled',payload=NULL WHERE context_id IN (SELECT id FROM cognitive_contexts WHERE chat_id=$1) AND (payload->>'owner_id')::bigint=$2 AND status IN ('pending','deferred','claimed')",chat_id,user_id)

    async def opted_out(self,chat_id,user_id):
        if user_id is None: return False
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval('SELECT opt_out FROM group_participant_settings WHERE chat_id=$1 AND user_id=$2',chat_id,user_id))
