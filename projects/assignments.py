from datetime import datetime,timezone
from dataclasses import replace
from projects.workflows import WorkflowRepository
from materials.types import MaterialError

class AssignmentRepository(WorkflowRepository):
    async def offer(self,project_id,actor,recipient,text,sources,*,due=None):
        (await self.projects.get(project_id,actor)).require('manage')
        if type(recipient) is not int or recipient<=0 or not 0<len(text)<=2000: raise MaterialError('assignment_invalid')
        (await self.projects.get(project_id,replace(actor,user_id=recipient,sender_ref='user:'+str(recipient)))).require('view')
        if due and datetime.fromisoformat(due).tzinfo is None: raise MaterialError('assignment_timezone_required')
        return await self.create(project_id,actor,'assignment',dict(text=text,recipient=recipient,offered_by=actor.user_id,status='offered',due=due,response=None),sources)
    async def respond(self,id,actor,expected,action,*,origin='user',sources=(),reason=''):
        old=await self.get(id,actor,'assignment'); b=old['body']
        if origin!='user' or not sources or actor.user_id!=b['recipient'] or action not in ('accept','decline','complete') or not isinstance(reason,str) or len(reason)>1000: raise MaterialError('assignment_response_denied')
        if action=='complete' and b['status']!='accepted': raise MaterialError('assignment_not_accepted')
        b['status']={'accept':'accepted','decline':'declined','complete':'completed'}[action]; b['response']=dict(actor_id=actor.user_id,at=datetime.now(timezone.utc).isoformat(),reason=reason)
        return await self.update(id,actor,expected,b,right='view',sources=sources)
