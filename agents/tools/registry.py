from dataclasses import dataclass,field
from decimal import Decimal
from jsonschema import Draft202012Validator
import asyncio,re
from materials.types import MaterialError,canonical

@dataclass(frozen=True)
class ToolResult:
    outcome:str
    outputs:dict=field(default_factory=dict)
    evidence:tuple=()
    diagnostics:tuple=()
    cost:str='0'
    receipt:dict|None=None
    dependencies:tuple=()
    def validate(self,tool):
        if self.outcome not in ('success','partial','waiting','unavailable') or not isinstance(self.outputs,dict) or len(canonical(self.outputs).encode())>tool.max_bytes or len(self.diagnostics)>20 or len(self.dependencies)>32 or any(not isinstance(id,str) for id in self.dependencies): raise MaterialError('tool_result_invalid')
        if not Decimal('0')<=Decimal(self.cost)<=Decimal(tool.max_cost): raise MaterialError('tool_cost_invalid')
        if self.outcome=='success': validate_schema(tool.output_schema,self.outputs,max_bytes=tool.max_bytes)
        if self.outcome=='success' and 'files' in self.outputs:
            import base64,binascii
            from hashlib import sha256
            if not isinstance(self.outputs['files'],list) or not self.outputs['files']: raise MaterialError('tool_file_missing')
            for file in self.outputs['files']:
                if set(file)!={'name','sha256','base64'} or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,150}',file['name']): raise MaterialError('tool_file_invalid')
                try: data=base64.b64decode(file['base64'],validate=True)
                except (ValueError,binascii.Error): raise MaterialError('tool_file_invalid') from None
                if not data or sha256(data).hexdigest()!=file['sha256']: raise MaterialError('tool_file_integrity')
        if tool.effect=='external' and self.outcome=='success' and (not self.receipt or len(canonical(self.receipt))>4000): raise MaterialError('tool_receipt_required')
        return self

def validate_schema(schema,value,*,max_bytes=500000):
    if '$ref' in canonical(schema) or len(canonical(value).encode())>max_bytes: raise MaterialError('tool_schema_budget')
    if list(Draft202012Validator(schema).iter_errors(value)): raise MaterialError('tool_schema_invalid')

@dataclass(frozen=True)
class Tool:
    name:str
    version:str
    input_schema:dict
    output_schema:dict
    handler:object
    effect:str='read'
    timeout:float=60
    max_cost:str='0'
    max_bytes:int=2000000
    resources:object=lambda args: ()
    recipient:object=lambda args: ''
    idempotent:bool=False
    reconcile:object=None
    def __post_init__(self):
        if not re.fullmatch(r'[a-z][a-z0-9_.]{1,79}',self.name) or not self.version or self.effect not in ('read','write','external') or not 0<self.timeout<=300 or not 0<=Decimal(self.max_cost)<=100 or not 0<self.max_bytes<=4*1024**2: raise MaterialError('tool_contract_invalid')
        for schema in (self.input_schema,self.output_schema): Draft202012Validator.check_schema(schema)

@dataclass
class ToolContext:
    actor:object
    service:object
    project_id:str
    task_id:str
    idempotency_key:str
    guard:object
    grant_id:str|None=None
    grants:object=None
    request_scope:object=None
    async def validate(self): await self.guard()

class Registry:
    def __init__(self): self.tools={}
    def register(self,tool):
        if tool.name in self.tools: raise MaterialError('duplicate_tool')
        self.tools[tool.name]=tool
    def get(self,name,version=None):
        tool=self.tools.get(name)
        if not tool or (version and version!=tool.version): raise MaterialError('tool_unavailable')
        return tool
    def schemas(self):
        return [dict(type='function',function=dict(name=t.name,description=f'{t.effect}; version {t.version}',parameters=t.input_schema)) for t in self.tools.values()]
    async def call(self,name,args,ctx,*,version=None):
        tool=self.get(name,version); validate_schema(tool.input_schema,args); await ctx.validate()
        if ctx.request_scope:
            await ctx.request_scope.validate_args(name,args)
        if tool.effect=='external':
            if not ctx.grants or not ctx.grant_id: raise MaterialError('capability_required')
            await ctx.grants.validate(ctx.grant_id,ctx.actor,tool,args)
        result=await asyncio.wait_for(tool.handler(args,ctx),tool.timeout)
        if not isinstance(result,ToolResult): raise MaterialError('tool_result_invalid')
        result.validate(tool); await ctx.validate()
        return result

def object_schema(properties,required=None): return dict(type='object',properties=properties,required=required or list(properties),additionalProperties=False)
STRING=dict(type='string',minLength=1,maxLength=4000)
