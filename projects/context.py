"""Small authorized project state; positions never become an inferred consensus."""
from projects.workflows import WorkflowRepository
from projects.types import WorkflowUse
from materials.types import MaterialError

async def workflow_context(materials,project_id,actor):
    repo=WorkflowRepository(materials); state=[]; uses=[]; causal=set()
    async with materials.pool.acquire() as conn:
        rows=await conn.fetch("SELECT id FROM arti_workflow_objects WHERE project_id=$1 AND realm=$2 AND kind IN ('decision','assignment','procedure','subscription','learning') AND status<>'deleted' ORDER BY created_at DESC LIMIT 10",project_id,actor.realm)
    for r in rows:
        try:
            row=await repo.get(r['id'],actor); b=row['body']; item=dict(id=row['id'],kind=row['kind'],revision=row['revision'],state=row['status'])
            if row['kind']=='decision': item.update(text=b['text'],status=b['status'],options=b['options'],positions=b['positions'],confirmation=b['confirmation'],consensus_inferred=False)
            elif row['kind']=='assignment': item.update(text=b['text'],recipient=b['recipient'],status=b['status'],due=b['due'],response=b['response'],offered_by=b['offered_by'])
            elif row['kind']=='procedure': item.update(title=b['title'],confirmed=row['head']==row['accepted'])
            elif row['kind']=='subscription': item.update(procedure_id=b['procedure_id'],schedule=b['schedule'],audience=b['audience'])
            else: item.update(title=b['title'],scenario=b['scenario'],completed=sum(p['completed'] for p in b['progress']),stages=len(b['stages']),mastery='not_measured')
            from materials.types import canonical
            if len(canonical(state+[item]))>14000: break
            uses.append(WorkflowUse(row['id'],actor,repo,row['revision'],row['head'],row['status'])); state.append(item)
            async with materials.pool.acquire() as conn:
                refs=await repo.derivatives._chain(conn,row['head'],actor)
                events=await conn.fetch('''SELECT DISTINCT e.id FROM material_assets a JOIN cognitive_events e ON e.source_id=a.source_id AND e.owner_id IS NOT DISTINCT FROM a.owner_id
                 JOIN cognitive_contexts c ON c.id=e.context_id WHERE a.id=ANY($1::text[]) AND c.persona_id=$2 AND c.chat_id=$3 AND c.topic_id=$4 AND c.mode=$5 AND c.scene_id=$6 AND e.suppressed_at IS NULL''',list({ref['asset_id'] for ref in refs}),actor.scope.persona_id,actor.scope.chat_id,actor.scope.topic_id,actor.scope.mode,actor.scope.scene_id)
                causal.update(e['id'] for e in events)
        except MaterialError: continue
    return state,uses,sorted(causal)
