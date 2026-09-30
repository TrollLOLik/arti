import json,uuid
from dataclasses import asdict
from materials.types import MaterialScope,MaterialError,canonical
from projects.types import Project,ROLE_RIGHTS


class ProjectRepository:
    def __init__(self,materials): self.materials=materials; self.pool=materials.pool
    @staticmethod
    def _project(row,actor):
        if not row or row['realm']!=actor.realm or row['scope_key']!=actor.scope.key or not row['role'] or row['payload'] is None or row['status']=='deleted': raise MaterialError('project_unavailable')
        payload=json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
        scope=json.loads(row['scope']) if isinstance(row['scope'],str) else row['scope']
        return Project(row['id'],payload['title'],payload['goal'],MaterialScope(**scope),row['owner_id'],row['status'],row['revision'],row['access_generation'],row['role'],tuple(payload.get('questions',())))
    async def _get(self,conn,id,actor,*,lock=False):
        row=await conn.fetchrow('''SELECT p.*,m.role FROM arti_projects p LEFT JOIN arti_project_members m
            ON m.project_id=p.id AND m.user_id=$2 WHERE p.id=$1'''+(' FOR UPDATE OF p' if lock else ''),id,actor.user_id)
        return self._project(row,actor)
    async def get(self,id,actor):
        async with self.pool.acquire() as conn: return await self._get(conn,id,actor)
    async def _revision(self,conn,project,actor,kind,body,*,access=False,status=None):
        payload=dict(title=body.get('title',project.title),goal=body.get('goal',project.goal),questions=body.get('questions',project.questions))
        Project(project.id,payload['title'],payload['goal'],project.scope,project.owner_id,status or project.status,project.revision+1,project.access_generation+int(access),project.role,tuple(payload['questions']))
        await conn.execute('''UPDATE arti_projects SET revision=revision+1,access_generation=access_generation+$2,
            payload=$3::jsonb,status=$4 WHERE id=$1''',project.id,int(access),canonical(payload),status or project.status)
        await conn.execute('INSERT INTO arti_project_revisions(project_id,revision,actor_id,kind,payload) VALUES($1,$2,$3,$4,$5::jsonb)',project.id,project.revision+1,actor.user_id,kind,canonical(body))
    @staticmethod
    def _cas(project,expected):
        if project.revision!=expected: raise MaterialError('stale_project_revision')
    async def create(self,actor,title,goal='',*,id=None,publication_guard=None):
        if actor.user_id is None or actor.user_id<=0: raise MaterialError('project_author_required')
        id=id or uuid.uuid4().hex; p=Project(id,title,goal,actor.scope,actor.user_id,'active',1,0,'owner')
        payload=dict(title=title,goal=goal,questions=[])
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            if publication_guard:
                from datetime import datetime,timezone
                publication_id,source_actor=publication_guard
                plan=await conn.fetchrow('SELECT * FROM arti_project_publications WHERE id=$1',publication_id)
                if not plan or plan['payload'] is None or plan['realm']!=source_actor.realm or plan['author_id']!=actor.user_id or plan['author_id']!=source_actor.user_id or plan['status']=='revoked' or plan['expires_at']<=datetime.now(timezone.utc): raise MaterialError('publication_unavailable')
                source=await self._get(conn,plan['project_id'],source_actor,lock=True); source.require('publish')
                plan=await conn.fetchrow('SELECT * FROM arti_project_publications WHERE id=$1 FOR UPDATE',publication_id)
                if plan['payload'] is None or plan['status']=='revoked' or plan['expires_at']<=datetime.now(timezone.utc): raise MaterialError('publication_unavailable')
                body=json.loads(plan['payload']) if isinstance(plan['payload'],str) else plan['payload']
                if source.revision!=body['project_revision'] or source.access_generation!=body['access_generation'] or title!=body['title'] or goal!=body['goal'] or actor.scope.key!=MaterialScope(**body['destination']).key: raise MaterialError('publication_project_changed')
                for asset in sorted(body['inputs'],key=lambda r:r['id']):
                    original=await conn.fetchrow('SELECT * FROM material_assets WHERE id=$1 FOR UPDATE',asset['id'])
                    self.materials.check(original,source_actor,edit=True)
                    await self.materials._source_allowed(conn,source_actor,original['source_id'],owner_id=original['owner_id'])
                    if original['generation']!=asset['generation'] or original['current_version']!=asset['version']: raise MaterialError('publication_source_changed')
            existing=await conn.fetchval('SELECT 1 FROM arti_projects WHERE id=$1',id)
            if existing:
                previous=await self._get(conn,id,actor,lock=True)
                if previous.owner_id!=actor.user_id or previous.title!=title or previous.goal!=goal: raise MaterialError('project_identity_conflict')
                return previous
            if await conn.fetchval("SELECT COUNT(*) FROM arti_projects WHERE realm=$1 AND status<>'deleted'",actor.realm)>=100: raise MaterialError('project_quota')
            await conn.execute('INSERT INTO arti_projects(id,realm,scope_key,scope,owner_id,payload) VALUES($1,$2,$3,$4::jsonb,$5,$6::jsonb)',id,actor.realm,actor.scope.key,canonical(asdict(actor.scope)),actor.user_id,canonical(payload))
            await conn.execute("INSERT INTO arti_project_members VALUES($1,$2,'owner')",id,actor.user_id)
            await conn.execute("INSERT INTO arti_project_revisions(project_id,revision,actor_id,kind,payload) VALUES($1,1,$2,'create',$3::jsonb)",id,actor.user_id,canonical(payload))
            await conn.execute('INSERT INTO arti_project_selections VALUES($1,$2,$3) ON CONFLICT(realm,user_id) DO UPDATE SET project_id=$3',actor.realm,actor.user_id,id)
            if publication_guard: await conn.execute("UPDATE arti_project_publications SET target_project_id=$2,status='running' WHERE id=$1 AND status IN ('prepared','running')",publication_id,id)
        return p
    async def list(self,actor):
        async with self.pool.acquire() as conn:
            rows=await conn.fetch('''SELECT p.*,m.role FROM arti_projects p JOIN arti_project_members m ON m.project_id=p.id
                WHERE p.realm=$1 AND p.scope_key=$2 AND m.user_id=$3 AND p.status<>'deleted' ORDER BY p.created_at,p.id LIMIT 100''',actor.realm,actor.scope.key,actor.user_id)
        return [self._project(r,actor) for r in rows]
    async def select(self,id,actor):
        async with self.pool.acquire() as conn,conn.transaction():
            p=await self._get(conn,id,actor,lock=True); p.require('view')
            await conn.execute('INSERT INTO arti_project_selections VALUES($1,$2,$3) ON CONFLICT(realm,user_id) DO UPDATE SET project_id=$3',actor.realm,actor.user_id,id)
        return p
    async def current(self,actor):
        async with self.pool.acquire() as conn:
            id=await conn.fetchval('SELECT project_id FROM arti_project_selections WHERE realm=$1 AND user_id=$2',actor.realm,actor.user_id)
            return await self._get(conn,id,actor) if id else None
    async def edit(self,id,actor,expected,**changes):
        if not changes or set(changes)-{'title','goal','questions'}: raise MaterialError('project_patch_invalid')
        async with self.pool.acquire() as conn,conn.transaction():
            p=await self._get(conn,id,actor,lock=True); p.require('edit'); self._cas(p,expected)
            await self._revision(conn,p,actor,'edit',changes)
        return await self.get(id,actor)
    async def member(self,id,actor,expected,user_id,role):
        if type(user_id) is not int or user_id<=0 or role not in {*ROLE_RIGHTS,'remove'} or role=='owner': raise MaterialError('project_role_invalid')
        async with self.pool.acquire() as conn,conn.transaction():
            p=await self._get(conn,id,actor,lock=True); p.require('manage'); self._cas(p,expected)
            if user_id==p.owner_id or p.scope.chat_type=='private': raise MaterialError('project_membership_denied')
            # Managers cannot grant management or external-effect authority.
            if p.role!='owner' and role in ('manager','approver'): raise MaterialError('project_grant_denied')
            if role=='remove': await conn.execute('DELETE FROM arti_project_members WHERE project_id=$1 AND user_id=$2',id,user_id)
            else: await conn.execute('INSERT INTO arti_project_members VALUES($1,$2,$3) ON CONFLICT(project_id,user_id) DO UPDATE SET role=$3',id,user_id,role)
            await self._revision(conn,p,actor,'membership',dict(user_id=user_id,role=role),access=True)
        return await self.get(id,actor)
    async def status(self,id,actor,expected,status):
        if status not in ('active','archived','deleted'): raise MaterialError('project_status_invalid')
        async with self.pool.acquire() as conn,conn.transaction():
            p=await self._get(conn,id,actor,lock=True); p.require('manage',active=False); self._cas(p,expected)
            if status=='deleted' and p.role!='owner': raise MaterialError('project_delete_denied')
            await self._revision(conn,p,actor,'status',dict(status=status),access=True,status=status)
            if status=='archived': await conn.execute('DELETE FROM arti_project_selections WHERE project_id=$1',id)
            if status=='deleted':
                await conn.execute('UPDATE arti_projects SET payload=NULL WHERE id=$1',id)
                await conn.execute('UPDATE arti_project_revisions SET payload=NULL WHERE project_id=$1',id)
                await conn.execute('DELETE FROM arti_project_selections WHERE project_id=$1',id)
        return None if status=='deleted' else await self.get(id,actor)
    async def attach(self,id,actor,expected,asset_id):
        row,rev=await self.materials.read(asset_id,actor)
        async with self.pool.acquire() as conn,conn.transaction():
            p=await self._get(conn,id,actor,lock=True); p.require('attach'); self._cas(p,expected)
            current=await conn.fetchrow('SELECT * FROM material_assets WHERE id=$1 FOR UPDATE',asset_id)
            self.materials.check(current,actor); await self.materials._source_allowed(conn,actor,current['source_id'],owner_id=current['owner_id'])
            if current['current_version']!=rev['version']: raise MaterialError('stale_project_material')
            if await conn.fetchval('SELECT COUNT(*) FROM arti_project_materials WHERE project_id=$1',id)>=200: raise MaterialError('project_material_quota')
            await conn.execute('INSERT INTO arti_project_materials VALUES($1,$2,$3,$4) ON CONFLICT(project_id,asset_id) DO UPDATE SET version=$3,added_by=$4',id,asset_id,rev['version'],actor.user_id)
            await self._revision(conn,p,actor,'attach',dict(asset_id=asset_id,version=rev['version']))
        return await self.get(id,actor)
    async def materials_for(self,id,actor):
        p=await self.get(id,actor); p.require('view',active=False)
        async with self.pool.acquire() as conn: rows=await conn.fetch('SELECT asset_id,version FROM arti_project_materials WHERE project_id=$1 ORDER BY asset_id',id)
        output=[]
        for row in rows:
            try:
                asset,rev=await self.materials.read(row['asset_id'],actor)
                output.append(dict(asset_id=row['asset_id'],version=row['version'],current_version=rev['version'],generation=asset['generation'],status='current' if rev['version']==row['version'] else 'stale',filename=asset['filename']))
            except MaterialError: output.append(dict(asset_id=row['asset_id'],status='unavailable'))
        return output

    async def result(self,id,actor,expected,key,derivative_id,*,status='proposed',reason=''):
        from materials.derivatives import DerivativeRepository
        if not key or len(key)>80 or status not in ('proposed','accepted','rejected') or len(reason)>2000: raise MaterialError('project_result_invalid')
        derivatives=DerivativeRepository(self.materials)
        async with self.pool.acquire() as conn,conn.transaction():
            p=await self._get(conn,id,actor,lock=True); p.require('edit'); self._cas(p,expected)
            refs=await derivatives._chain(conn,derivative_id,actor)
            await derivatives._sources(conn,actor,refs)
            await conn.execute('''INSERT INTO arti_project_result_candidates(project_id,result_key,derivative_id,actor_id,status,reason)
                VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(project_id,result_key,derivative_id) DO UPDATE SET actor_id=$4,status=$5,reason=$6''',id,key,derivative_id,actor.user_id,status,reason)
            if status=='accepted':
                await conn.execute('''INSERT INTO arti_project_results(project_id,result_key,derivative_id,accepted_by) VALUES($1,$2,$3,$4)
                    ON CONFLICT(project_id,result_key) DO UPDATE SET derivative_id=$3,accepted_by=$4''',id,key,derivative_id,actor.user_id)
            elif status=='rejected': await conn.execute('DELETE FROM arti_project_results WHERE project_id=$1 AND result_key=$2 AND derivative_id=$3',id,key,derivative_id)
            await self._revision(conn,p,actor,'result',dict(key=key,derivative_id=derivative_id,status=status,reason=reason))
        return await self.get(id,actor)
    async def results_for(self,id,actor):
        from materials.derivatives import DerivativeRepository
        p=await self.get(id,actor); p.require('view',active=False)
        derivatives=DerivativeRepository(self.materials); output=[]
        async with self.pool.acquire() as conn,conn.transaction():
            await self.materials._locks(conn,actor)
            rows=await conn.fetch('''SELECT c.*,h.derivative_id=c.derivative_id AS selected FROM arti_project_result_candidates c
                LEFT JOIN arti_project_results h ON h.project_id=c.project_id AND h.result_key=c.result_key
                WHERE c.project_id=$1 ORDER BY c.created_at DESC,c.derivative_id LIMIT 50''',id)
            for row in rows:
                available=True
                try:
                    refs=await derivatives._chain(conn,row['derivative_id'],actor)
                    await derivatives._sources(conn,actor,refs)
                except MaterialError: available=False
                output.append(dict(key=row['result_key'],id=row['derivative_id'],status=row['status'],selected=bool(row['selected']),availability='current' if available else 'stale_or_revoked',actor_id=row['actor_id'],reason=row['reason'] if available else None))
        return output
