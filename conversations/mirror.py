"""Durable copies of native text to the owner's bot chat. Never generates answers."""
import asyncio
import uuid


def chunks(text,limit=4000):
    part=[]
    units=0
    for char in text:
        size=2 if ord(char)>0xffff else 1
        if units+size>limit:
            yield ''.join(part)
            part=[]
            units=0
        part.append(char)
        units+=size
    if part:
        yield ''.join(part)


async def prepare_mirror(conn,mid,text):
    for ordinal,part in enumerate(chunks(text)):
        await conn.execute('''INSERT INTO app_deliveries(id,message_id,target,ordinal,state,payload)
            VALUES($1,$2,'telegram',$3,'prepared',$4) ON CONFLICT(message_id,target,ordinal) DO NOTHING''',uuid.uuid4(),mid,ordinal,part)


class MirrorWorker:
    def __init__(self,pool,send):
        self.pool,self.send=pool,send

    async def once(self):
        from .service import VISIBLE,JOINS
        async with self.pool.acquire() as conn,conn.transaction():
            # A lost network result cannot be recovered through Bot API idempotency.
            await conn.execute("UPDATE app_deliveries SET state='delivery_unknown',payload=NULL,token=NULL WHERE target='telegram' AND state='sending' AND updated_at<now()-interval '90 seconds'")
            row=await conn.fetchrow(f'''SELECT d.id,d.payload,msg.conversation_id,msg.cognitive_context_id,c.owner_id
                FROM app_deliveries d JOIN app_messages msg ON msg.id=d.message_id
                JOIN app_conversations c ON c.id=msg.conversation_id {JOINS}
                JOIN app_memberships member ON member.conversation_id=c.id AND member.user_id=c.owner_id AND member.state='active'
                WHERE d.target='telegram' AND d.state='prepared' AND d.available_at<=now() AND {VISIBLE}
                AND NOT EXISTS(SELECT 1 FROM app_deliveries earlier JOIN app_messages em ON em.id=earlier.message_id
                    WHERE em.conversation_id=c.id AND earlier.target='telegram' AND earlier.state IN ('prepared','sending')
                    AND (em.seq<msg.seq OR (em.seq=msg.seq AND earlier.ordinal<d.ordinal)))
                ORDER BY msg.seq,d.ordinal FOR UPDATE OF d SKIP LOCKED LIMIT 1''')
            if not row:
                return False
            token=uuid.uuid4().hex
            await conn.execute("UPDATE app_deliveries SET state='sending',token=$2,updated_at=now() WHERE id=$1",row['id'],token)
        state,receipt='delivery_unknown',None
        delay=None
        try:
            # Recheck the projection immediately before the external boundary.
            async with self.pool.acquire() as conn:
                permitted=await conn.fetchval(f'''SELECT 1 FROM app_deliveries d JOIN app_messages msg ON msg.id=d.message_id {JOINS}
                    WHERE d.id=$1 AND d.token=$2 AND d.state='sending' AND {VISIBLE}''',row['id'],token)
            if not permitted:
                state='cancelled'
            else:
                async with asyncio.timeout(40):
                    result=await self.send(chat_id=row['owner_id'],text=row['payload'],parse_mode=None)
                receipt=str(result.message_id)
                state='delivered'
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from telegram.error import RetryAfter
            from cognition.delivery import definite_rejection
            if isinstance(exc,RetryAfter):
                delay=exc.retry_after.total_seconds() if hasattr(exc.retry_after,'total_seconds') else float(exc.retry_after)
                state='prepared'
            elif definite_rejection(exc):
                state='cancelled'
        finally:
            async with self.pool.acquire() as conn,conn.transaction():
                await conn.fetchval('SELECT id FROM app_conversations WHERE id=$1 FOR UPDATE',row['conversation_id'])
                changed=await conn.fetchval('''UPDATE app_deliveries SET state=$3,receipt_id=$4,
                    payload=CASE WHEN $3='prepared' THEN payload ELSE NULL END,token=NULL,updated_at=now(),
                    available_at=now()+make_interval(secs=>$5) WHERE id=$1 AND token=$2 AND state='sending' RETURNING message_id''',
                    row['id'],token,state,receipt,max(1.,delay or 1.))
                if changed:
                    from .repository import Repository
                    await Repository(self.pool)._event(conn,row['conversation_id'],'message.created',changed)
        return True

    async def run(self):
        while True:
            try:
                ready=await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:
                ready=False
            await asyncio.sleep(.1 if ready else 1.)
