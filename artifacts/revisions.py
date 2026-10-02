import uuid
from artifacts.spec import ArtifactSpec
from artifacts.validation import validate_evidence
from artifacts.patches import patch
from projects.repository import ProjectRepository
from materials.derivatives import DerivativeRepository
from materials.types import MaterialError

class ArtifactRepository:
    def __init__(self,materials): self.materials=materials; self.pool=materials.pool; self.projects=ProjectRepository(materials); self.derivatives=DerivativeRepository(materials)
    async def create(self,project_id,actor,spec,*,id=None,sources=(),inputs=()):
        spec=ArtifactSpec(spec if isinstance(spec,dict) else spec.to_dict())
        p=await self.projects.get(project_id,actor); p.require('edit')
        refs,proof_inputs,checks=await validate_evidence(spec,actor,self.materials)
        inputs=list(dict.fromkeys([*proof_inputs,*inputs]))
        derivative=await self.derivatives.save(actor,'artifact',dict(spec=spec.to_dict(),checks=checks),[*refs,*sources],inputs=inputs)
        id=id or uuid.uuid4().hex
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor); p=await self.projects._get(conn,project_id,actor,lock=True); p.require('edit')
            await self.derivatives._sources(conn,actor,await self.derivatives._chain(conn,derivative,actor))
            old=await conn.fetchrow('SELECT * FROM arti_artifacts WHERE id=$1',id)
            if old:
                if old['project_id']!=project_id or old['head']!=derivative: raise MaterialError('artifact_identity_conflict')
            else:
                await conn.execute('INSERT INTO arti_artifacts(id,project_id,head) VALUES($1,$2,$3)',id,project_id,derivative)
                await conn.execute("INSERT INTO arti_artifact_revisions VALUES($1,1,$2,$3,'proposed',NULL)",id,derivative,actor.user_id)
                await conn.execute("INSERT INTO arti_project_result_candidates(project_id,result_key,derivative_id,actor_id,status) VALUES($1,$2,$3,$4,'proposed') ON CONFLICT DO NOTHING",project_id,'artifact:'+id,derivative,actor.user_id)
        return await self.get(id,actor)
    async def get(self,id,actor,*,revision=None):
        async with self.pool.acquire() as conn:
            row=await conn.fetchrow('SELECT * FROM arti_artifacts WHERE id=$1',id)
            if not row: raise MaterialError('artifact_unavailable')
            (await self.projects._get(conn,row['project_id'],actor)).require('view',active=False)
            derivative=row['head'] if revision is None else await conn.fetchval('SELECT derivative_id FROM arti_artifact_revisions WHERE artifact_id=$1 AND revision=$2',id,revision)
        body=await self.derivatives.load(derivative,actor,'artifact')
        return dict(row,derivative_id=derivative,spec=body['spec'],checks=body['checks'])
    async def revise(self,id,actor,expected,operations=None,*,rollback=None,sources=()):
        old=await self.get(id,actor); spec=ArtifactSpec(old['spec'])
        if rollback is not None: revised=ArtifactSpec((await self.get(id,actor,revision=rollback))['spec']); diff=dict(rollback=rollback)
        else: revised,diff=patch(spec,operations)
        refs,inputs,checks=await validate_evidence(revised,actor,self.materials)
        async with self.pool.acquire() as conn:
            refs.extend(await self.derivatives._chain(conn,old['head'],actor))
        refs.extend(sources)
        derivative=await self.derivatives.save(actor,'artifact',dict(spec=revised.to_dict(),checks=checks),refs,inputs=inputs)
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor); (await self.projects._get(conn,old['project_id'],actor,lock=True)).require('edit')
            row=await conn.fetchrow('SELECT * FROM arti_artifacts WHERE id=$1 FOR UPDATE',id)
            if row['revision']!=expected: raise MaterialError('stale_artifact_revision')
            await self.derivatives._sources(conn,actor,await self.derivatives._chain(conn,derivative,actor))
            await conn.execute('UPDATE arti_artifacts SET revision=revision+1,head=$2 WHERE id=$1',id,derivative)
            await conn.execute("INSERT INTO arti_artifact_revisions VALUES($1,$2,$3,$4,'proposed',NULL)",id,expected+1,derivative,actor.user_id)
            await conn.execute("INSERT INTO arti_project_result_candidates(project_id,result_key,derivative_id,actor_id,status) VALUES($1,$2,$3,$4,'proposed') ON CONFLICT DO NOTHING",old['project_id'],'artifact:'+id,derivative,actor.user_id)
        return await self.get(id,actor),diff
    async def decide(self,id,actor,expected,status,reason=''):
        if status not in ('accepted','rejected') or len(reason)>1000: raise MaterialError('artifact_decision_invalid')
        old=await self.get(id,actor)
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor); (await self.projects._get(conn,old['project_id'],actor,lock=True)).require('approve')
            row=await conn.fetchrow('SELECT * FROM arti_artifacts WHERE id=$1 FOR UPDATE',id)
            if row['revision']!=expected: raise MaterialError('stale_artifact_revision')
            await self.derivatives._sources(conn,actor,await self.derivatives._chain(conn,row['head'],actor))
            await conn.execute('UPDATE arti_artifact_revisions SET status=$3,reason=$4 WHERE artifact_id=$1 AND revision=$2',id,expected,status,reason)
            await conn.execute('UPDATE arti_project_result_candidates SET status=$4,reason=$5,actor_id=$6 WHERE project_id=$1 AND result_key=$2 AND derivative_id=$3',old['project_id'],'artifact:'+id,row['head'],status,reason,actor.user_id)
            if status=='accepted':
                await conn.execute('UPDATE arti_artifacts SET accepted=head WHERE id=$1',id)
                await conn.execute('INSERT INTO arti_project_results(project_id,result_key,derivative_id,accepted_by) VALUES($1,$2,$3,$4) ON CONFLICT(project_id,result_key) DO UPDATE SET derivative_id=$3,accepted_by=$4',old['project_id'],'artifact:'+id,row['head'],actor.user_id)
            else:
                await conn.execute('UPDATE arti_artifacts SET accepted=NULL WHERE id=$1 AND accepted=head',id)
                await conn.execute('DELETE FROM arti_project_results WHERE project_id=$1 AND result_key=$2 AND derivative_id=$3',old['project_id'],'artifact:'+id,row['head'])
        return await self.get(id,actor)
