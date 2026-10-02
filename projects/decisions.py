from datetime import datetime,timezone
from projects.workflows import WorkflowRepository
from materials.types import MaterialError

class DecisionRepository(WorkflowRepository):
    async def propose(self,project_id,actor,text,sources,*,options=()):
        if not text or len(text)>2000 or len(options)>12 or any(len(x)>500 for x in options): raise MaterialError('decision_invalid')
        return await self.create(project_id,actor,'decision',dict(text=text,options=list(options),status='proposed',proposer=actor.user_id,positions=[],confirmation=None,coverage='explicit_project_input_only'),sources)
    async def act(self,id,actor,expected,action,*,reason='',option=None,origin='user',sources=()):
        old=await self.get(id,actor,'decision'); b=old['body']
        if origin!='user' or not sources or action not in ('support','object','confirm','revoke') or len(reason)>1000 or (option is not None and option not in b['options']): raise MaterialError('decision_action_invalid')
        right='view'
        if action in ('confirm','revoke'):
            right='approve'; b['status']='confirmed' if action=='confirm' else 'revoked'; b['confirmation']=dict(actor_id=actor.user_id,at=datetime.now(timezone.utc).isoformat(),reason=reason,option=option)
        else:
            b['positions']=[p for p in b['positions'] if p['actor_id']!=actor.user_id]
            b['positions'].append(dict(actor_id=actor.user_id,position=action,reason=reason,option=option,at=datetime.now(timezone.utc).isoformat()))
        # The confirming organizer records their authority; silence/other positions are never votes.
        b['consensus_inferred']=False
        return await self.update(id,actor,expected,b,right=right,accept=action=='confirm',clear_accept=action=='revoke',sources=sources)
