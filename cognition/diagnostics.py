"""Owner-scoped views with provenance and payload-free operational metrics."""
from html import escape
from cognition.affect import affect,expression
from cognition.relationships import relationship_view
from cognition.serialization import dump


async def active_context(runtime,chat_id,mode):
    if not runtime or runtime.mode=='legacy':
        return None
    context = await runtime.context(chat_id,mode)
    if getattr(runtime,'strict',False):
        return await runtime.ensure_context(context)
    async with runtime.pool.acquire() as conn:
        row = await conn.fetchrow('SELECT id,authority FROM cognitive_contexts WHERE persona_id=$1 AND chat_id=$2 AND mode=$3 AND scene_id=$4 AND topic_id=$5',*context.identity())
    return row['id'] if row and row['authority']=='active' else None


async def profile_text(runtime,cid,owner):
    beliefs = await runtime.memory.artifacts(cid,owner,'belief',limit=24)
    intentions = await runtime.memory.artifacts(cid,owner,'intention',limit=8)
    lines = ['Сведения из разрешённых источников:']
    for row in beliefs:
        p = row['payload']
        line = f"{p['predicate']}: {p['value']}"
        if p['condition']:
            line += ' ('+p['condition']+')'
        if p['assertion']=='inferred':
            line += ' — предположение'
        lines.append(escape(line))
    if not beliefs:
        lines.append('Пока нет сформированных сведений.')
    open_items = [r['payload'] for r in intentions if r['payload']['status'] in ('open','reminder')]
    if open_items:
        lines += ['','Незавершённые дела:']+[escape(p['description']) for p in open_items]
    return '\n'.join(lines)[:3800]


async def state_text(runtime,cid,owner):
    state = await runtime.personal_state(cid,owner)
    view = relationship_view(await runtime.memory.relationship(cid,owner),runtime.clock())
    data = dict(model=state.model_version,revision=state.revision,affect=affect(state),
                episodes=[dict(emotion=e.emotion,intensity=round(e.intensity,4),confidence=e.confidence,cause=e.cause_id) for e in state.episodes if e.intensity>=.025],
                relationships=view,expression=expression(state).instruction())
    text = dump(data)
    while len(escape(text))>3700:
        text = text[:-100]
    return '<pre>'+escape(text)+'</pre>'


async def operational_metrics(pool):
    async with pool.acquire() as conn:
        jobs = await conn.fetch('SELECT status,COUNT(*) AS count FROM cognitive_jobs GROUP BY status')
        deliveries = await conn.fetch('SELECT status,COUNT(*) AS count FROM cognitive_outbox GROUP BY status')
        contexts = await conn.fetch('SELECT authority,COUNT(*) AS count FROM cognitive_contexts GROUP BY authority')
        group_states=await conn.fetch('SELECT status,COUNT(*) AS count FROM group_candidates GROUP BY status')
        group_reasons=await conn.fetch('SELECT reason,COUNT(*) AS count FROM group_decisions GROUP BY reason')
        return dict(jobs={r['status']:r['count'] for r in jobs},deliveries={r['status']:r['count'] for r in deliveries},contexts={r['authority']:r['count'] for r in contexts},
                    group_candidates={r['status']:r['count'] for r in group_states},group_decisions={r['reason']:r['count'] for r in group_reasons},
                    rebuilding_contexts=await conn.fetchval('SELECT COUNT(*) FROM cognitive_contexts WHERE rebuilding'),
                    oldest_ready_job_seconds=await conn.fetchval("SELECT coalesce(EXTRACT(EPOCH FROM NOW()-MIN(available_at)),0)::double precision FROM cognitive_jobs WHERE status='pending' AND kind!='replay' AND available_at<=NOW()"),
                    private_owner_violations=await conn.fetchval('''SELECT COUNT(*) FROM cognitive_provenance p JOIN cognitive_artifacts a ON a.id=p.artifact_id
                        JOIN cognitive_events e ON e.id=p.source_event_id WHERE a.suppressed_at IS NULL AND a.owner_id IS DISTINCT FROM e.owner_id'''),
                    leases_expired=await conn.fetchval("SELECT COUNT(*) FROM cognitive_jobs WHERE status='running' AND lease_until<=NOW()"),
                    provenance_gaps=await conn.fetchval('''SELECT COUNT(*) FROM cognitive_artifacts a WHERE suppressed_at IS NULL
                        AND NOT EXISTS(SELECT 1 FROM cognitive_provenance p WHERE p.artifact_id=a.id)'''))
