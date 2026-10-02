"""Reviewable audience transfer, exact source grants and resumable scoped copies."""
import json,uuid
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from hashlib import sha256
from materials.types import AccessContext,MaterialScope,MaterialError,canonical
from materials.sharing import ShareGrant,share_copy
from projects.repository import ProjectRepository

class ProjectPublication:
    def __init__(self,service): self.service=service; self.projects=ProjectRepository(service.repository); self.pool=service.repository.pool
    async def preview(self,project_id,actor,expected,destination,request_id):
        p=await self.projects.get(project_id,actor); p.require('publish'); self.projects._cas(p,expected)
        if destination.user_id!=actor.user_id or destination.scope.key==actor.scope.key: raise MaterialError('publication_audience_invalid')
        materials=await self.projects.materials_for(project_id,actor); inputs=[]
        for item in materials:
            if item['status']!='current': raise MaterialError('publication_source_unavailable')
            row,rev=await self.service.repository.read(item['asset_id'],actor)
            self.service.repository.check(row,actor,edit=True)
            inputs.append(dict(id=row['id'],version=rev['version'],generation=row['generation'],sha256=rev['sha256'],filename=row['filename']))
        payload=dict(project_revision=p.revision,access_generation=p.access_generation,title=p.title,goal=p.goal,questions=p.questions,
            destination=asdict(destination.scope),request_id=request_id,inputs=inputs)
        id=uuid.uuid4().hex; expiry=datetime.now(timezone.utc)+timedelta(minutes=30)
        async with self.pool.acquire() as conn,conn.transaction():
            await self.service.repository._locks(conn,actor)
            current=await self.projects._get(conn,project_id,actor,lock=True); current.require('publish'); self.projects._cas(current,expected)
            for source in sorted(inputs,key=lambda s:s['id']):
                original=await conn.fetchrow('SELECT * FROM material_assets WHERE id=$1 FOR UPDATE',source['id'])
                self.service.repository.check(original,actor,edit=True)
                await self.service.repository._source_allowed(conn,actor,original['source_id'],owner_id=original['owner_id'])
                if original['generation']!=source['generation'] or original['current_version']!=source['version']: raise MaterialError('publication_source_changed')
            await conn.execute('INSERT INTO arti_project_publications(id,project_id,realm,author_id,payload,expires_at) VALUES($1,$2,$3,$4,$5::jsonb,$6)',id,project_id,actor.realm,actor.user_id,canonical(payload),expiry)
        return dict(id=id,expires_at=expiry.isoformat(),**payload)
    async def _load(self,id,actor):
        async with self.pool.acquire() as conn: row=await conn.fetchrow('SELECT * FROM arti_project_publications WHERE id=$1',id)
        if not row or row['realm']!=actor.realm or row['author_id']!=actor.user_id or row['payload'] is None or row['status']=='revoked': raise MaterialError('publication_unavailable')
        payload=json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
        p=await self.projects.get(row['project_id'],actor); p.require('publish')
        if p.access_generation!=payload['access_generation'] or p.revision!=payload['project_revision']: raise MaterialError('publication_project_changed')
        if row['expires_at']<=datetime.now(timezone.utc): raise MaterialError('publication_expired')
        for source in payload['inputs']:
            original,revision=await self.service.repository.read(source['id'],actor,source['version'])
            self.service.repository.check(original,actor,edit=True)
            if original['generation']!=source['generation'] or original['current_version']!=source['version'] or revision['sha256']!=source['sha256']: raise MaterialError('publication_source_changed')
        return dict(row),payload
    async def publish(self,id,actor):
        row,body=await self._load(id,actor)
        destination=AccessContext(MaterialScope(**body['destination']),actor.user_id,actor.sender_ref)
        if row['status']=='completed': return await self.projects.get(row['target_project_id'],destination)
        # Target ID is deterministic; a retry resumes this project and each
        # immutable share_copy, rather than creating another audience transfer.
        target_id=sha256(('project-publication:'+id).encode()).hexdigest()[:32]
        target=await self.projects.create(destination,body['title'],body['goal'],id=target_id,publication_guard=(id,actor))
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE arti_project_publications SET target_project_id=$2,status='running' WHERE id=$1 AND payload IS NOT NULL AND status IN ('prepared','running')",id,target.id)
        for source in body['inputs']:
            await self._load(id,actor)
            grant=ShareGrant(actor.user_id,source['id'],source['version'],source['generation'],source['sha256'],destination.scope.key,body['request_id']+':'+id,row['expires_at'])
            copy=await share_copy(self.service,actor,destination,grant)
            current=await self.projects.get(target.id,destination)
            existing=await self.projects.materials_for(target.id,destination)
            if not any(m['asset_id']==copy['id'] and m['status']=='current' for m in existing):
                await self.projects.attach(target.id,destination,current.revision,copy['id'])
        await self._load(id,actor)
        if body['questions']:
            current=await self.projects.get(target.id,destination)
            if tuple(body['questions'])!=current.questions: await self.projects.edit(target.id,destination,current.revision,questions=body['questions'])
        async with self.pool.acquire() as conn,conn.transaction():
            source_project=await self.projects._get(conn,row['project_id'],actor,lock=True); source_project.require('publish')
            self.projects._cas(source_project,body['project_revision'])
            for source in body['inputs']:
                original=await conn.fetchrow('SELECT * FROM material_assets WHERE id=$1 FOR UPDATE',source['id'])
                self.service.repository.check(original,actor,edit=True)
                await self.service.repository._source_allowed(conn,actor,original['source_id'],owner_id=original['owner_id'])
                if original['generation']!=source['generation'] or original['current_version']!=source['version']: raise MaterialError('publication_source_changed')
            await conn.execute("UPDATE arti_project_publications SET status='completed' WHERE id=$1 AND payload IS NOT NULL AND status<>'revoked'",id)
        return await self.projects.get(target.id,destination)
