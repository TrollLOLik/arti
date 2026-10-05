"""Transactional conversational turns and content-free replay tombstones.

No transport occurs inside this transaction. A durable request may checkpoint
later; this ledger already owns the native side effect if that checkpoint fails.
"""
import json
from organizer.time import OrganizerError


def _object(value):
    return json.loads(value) if isinstance(value,str) else value


async def cancel_pending(conn,owner):
    if type(owner) is not int or owner<=0: return
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",f'organizer:{owner}')
    await conn.execute('INSERT INTO arti_organizer_dialogue_state(owner_id,generation) VALUES($1,1) ON CONFLICT(owner_id) DO UPDATE SET generation=arti_organizer_dialogue_state.generation+1',owner)
    await conn.execute('DELETE FROM arti_organizer_reply_refs WHERE owner_id=$1',owner)
    await conn.execute("""UPDATE arti_organizer_turns SET state='cancelled',outcome=NULL
        WHERE owner_id=$1 AND state='pending' AND root_source_key IN
        (SELECT source_key FROM arti_organizer_pending WHERE owner_id=$1)""",owner)
    await conn.execute('DELETE FROM arti_organizer_pending WHERE owner_id=$1 AND chat_id=$1',owner)


async def perform(repo,owner,chat,text,source_key,*,reply_to_id=None):
    from organizer.natural import converse,parse,_pending
    from bot.organizer_commands import execute,summary
    async with repo.transaction(owner,chat) as conn:
        pending=await _pending(repo,owner,chat)
        await repo._live_source(conn,owner,source_key)
        generation=await conn.fetchval('SELECT generation FROM arti_organizer_dialogue_state WHERE owner_id=$1',owner) or 0
        saved=await conn.fetchrow('SELECT * FROM arti_organizer_turns WHERE owner_id=$1 AND source_key=$2',owner,source_key)
        if saved:
            if saved['state'] in ('erased','cancelled'): raise OrganizerError('request_cancelled')
            outcome=_object(saved['outcome'])
            if saved['item_id']:
                item=await repo.get(owner,chat,saved['item_id'])
                if not item: raise OrganizerError('source_erased')
                outcome=dict(outcome,response='Сохранено:\n'+summary(item),items=[dict(id=item['id'],version=item['version'])])
            await _link_request(conn,owner,source_key)
            await _attach_provenance(conn,owner,outcome.get('items',[]))
            return outcome
        root=pending['source_key'] if pending and parse(text) is None else source_key
        operation=await converse(repo,owner,chat,text,source_key,reply_to_id=reply_to_id)
        if operation is None: return None
        items=[]; item_id=None; clear_source=None
        if isinstance(operation,str):
            response=operation
            pending_now=await _pending(repo,owner,chat)
            if pending_now:
                data=pending_now['payload']
                if data.get('item_id'):
                    items=[dict(id=data['item_id'],version=data['expected_version'])]
                else:
                    items=data.get('candidates',[])

        elif isinstance(operation,dict):
            root=operation['source_key']; clear_source=root
            if 'mutation' in operation:
                data=operation['mutation']; action=data['action']
                if action in ('cancel','done'):
                    item=await repo.change(owner,chat,data['item_id'],'cancelled' if action=='cancel' else 'done',
                        source_key=root,expected_version=data['expected_version'],
                        expected_kind={'todo':'todo','event':'event','remind':'reminder'}[data['command']])
                else:
                    from datetime import datetime
                    item=await repo.edit(owner,chat,data['item_id'],source_key=root,expected_version=data['expected_version'],
                        title=data.get('title') if action=='rename' else None,
                        due_at=datetime.fromisoformat(data['when']) if action=='reschedule' else None,
                        timezone_name=data.get('timezone'))
                if not item: raise OrganizerError('item_not_found')
                response='Текущее состояние:\n'+summary(item)
            else:
                item=operation['existing_item']; response='Сохранено:\n'+summary(item)
            item_id=item['id']; items=[dict(id=item['id'],version=item['version'])]
        else:
            command,args,root,display_zone=operation
            response=await execute(owner,chat,command,args,root,repo=repo,display_timezone=display_zone)
            clear_source=root
            if args[0]=='add':
                item=await repo.by_source(owner,chat,root)
                item_id=item['id']; items=[dict(id=item['id'],version=item['version'])]
            elif args[0]=='list':
                rows=await repo.list(owner,chat,{'todo':'todo','event':'event','remind':'reminder'}[command],limit=20)
                items=[dict(id=r['id'],version=r['version']) for r in rows if r['id'] in response]
        await _link_request(conn,owner,source_key)
        await _attach_provenance(conn,owner,items)
        outcome=dict(response=response,clear_source=clear_source,source_key=source_key,root_source_key=root,items=items,generation=generation)
        # A successful mutation settles all clarification turns of this root.
        state='complete' if clear_source else 'pending' if await conn.fetchval('SELECT 1 FROM arti_organizer_pending WHERE owner_id=$1 AND source_key=$2',owner,root) else 'complete'
        if state=='complete' and clear_source:
            await conn.execute("UPDATE arti_organizer_turns SET state='complete',outcome=$3::jsonb,item_id=$4 WHERE owner_id=$1 AND root_source_key=$2 AND state='pending'",owner,root,json.dumps(outcome,ensure_ascii=False),item_id)
        await conn.execute('INSERT INTO arti_organizer_turns(owner_id,source_key,root_source_key,state,outcome,item_id) VALUES($1,$2,$3,$4,$5::jsonb,$6)',owner,source_key,root,state,json.dumps(outcome,ensure_ascii=False),item_id)
        return outcome


async def current_outcome(repo,owner,chat,outcome):
    """Never expose a cached checkpoint after reset, source erasure or mutation."""
    from bot.organizer_commands import summary
    source_key=outcome.get('source_key')
    if not source_key: return outcome
    async with repo.connection() as conn:
        row=await conn.fetchrow('SELECT * FROM arti_organizer_turns WHERE owner_id=$1 AND source_key=$2',owner,source_key)
        generation=await conn.fetchval('SELECT generation FROM arti_organizer_dialogue_state WHERE owner_id=$1',owner) or 0
        if not row or row['state'] in ('cancelled','erased') or generation!=outcome.get('generation',0):
            return dict(response='Это уточнение уже отменено или его источник удалён.',clear_source=None,items=[])
        # Keep the exact checkpoint body/version. Durable transport can return
        # an older receipt after a crash; refreshing the item here would bind an
        # unseen newer version to that old message and grant unsafe authority.
        return outcome


async def _attach_provenance(conn,owner,items):
    """Checkpoint and delivery fences include every displayed native source."""
    from cognition.runtime import CURRENT_TURN
    turn=CURRENT_TURN.get()
    if turn is None or not items: return
    ids=[item['id'] for item in items]
    sources=await conn.fetchval("""SELECT ARRAY_AGG(DISTINCT source_key || ':user') FROM (
        SELECT source_key FROM arti_organizer_items WHERE owner_id=$1 AND id=ANY($2::text[])
        UNION SELECT source_key FROM arti_organizer_actions WHERE owner_id=$1 AND item_id=ANY($2::text[])
        UNION SELECT source_key FROM arti_organizer_turns WHERE owner_id=$1 AND item_id=ANY($2::text[])
    ) native_sources""",owner,ids) or []
    events=await conn.fetchval('SELECT ARRAY_AGG(id) FROM cognitive_events WHERE context_id=$1 AND owner_id=$2 AND source_id=ANY($3::text[]) AND suppressed_at IS NULL',turn.context_id,owner,sources) or []
    turn.supporting_event_ids=sorted(set(getattr(turn,'supporting_event_ids',())) | set(events))


async def _link_request(conn,owner,source_key):
    # Structured commands / old installations may have no cognitive event.
    # Preserve a content-free route to their exact prepared-send/checkpoint rows.
    from bot.request_runtime import CURRENT_REQUEST
    job=CURRENT_REQUEST.get()
    if job is not None:
        await conn.execute('INSERT INTO arti_organizer_request_links(owner_id,source_key,request_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING',owner,source_key,job['id'])
