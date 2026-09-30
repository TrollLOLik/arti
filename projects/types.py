from dataclasses import dataclass
from materials.types import MaterialScope,MaterialError

ROLE_RIGHTS={
    'owner':frozenset({'view','attach','edit','manage','approve','publish'}),
    'manager':frozenset({'view','attach','edit','manage'}),
    'editor':frozenset({'view','attach','edit'}),
    'contributor':frozenset({'view','attach'}),
    'viewer':frozenset({'view'}),
    'approver':frozenset({'view','approve'}),
}

@dataclass(frozen=True)
class Project:
    id: str
    title: str
    goal: str
    scope: MaterialScope
    owner_id: int
    status: str
    revision: int
    access_generation: int
    role: str
    questions: tuple[str,...]=()
    def __post_init__(self):
        if not self.title or len(self.title)>150 or len(self.goal)>4000 or self.status not in ('active','archived','deleted') or self.revision<1 or self.role not in ROLE_RIGHTS or len(self.questions)>12 or any(len(q)>500 for q in self.questions): raise MaterialError('invalid_project')
    def require(self,right,*,active=True):
        if right not in ROLE_RIGHTS[self.role] or self.status=='deleted' or (active and self.status!='active'): raise MaterialError('project_access_denied')

@dataclass(frozen=True)
class ProjectUse:
    project_id: str
    actor: object
    repository: object
    access_generation: int
    revision: int | None=None
    allow_archived: bool=False
    async def validate(self):
        project=await self.repository.get(self.project_id,self.actor)
        project.require('view',active=not self.allow_archived)
        if project.access_generation!=self.access_generation: raise MaterialError('project_access_changed')
        if self.revision is not None and project.revision!=self.revision: raise MaterialError('project_revision_changed')
