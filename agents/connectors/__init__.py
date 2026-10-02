"""Disconnected adapters return unavailable; they never simulate a remote write."""
from dataclasses import dataclass
from agents.tools.registry import ToolResult
from materials.types import MaterialError

@dataclass
class Connector:
    name:str
    collections:tuple=()
    connected:bool=False
    supports_idempotency:bool=False
    supports_reconciliation:bool=False
    allowed_realms:tuple=()
    async def preview(self,collection,operation,payload,*,actor=None):
        if not self.connected: return dict(status='unavailable',connector=self.name)
        if actor is None or actor.realm not in self.allowed_realms or collection not in self.collections or operation not in ('create','update','delete'): raise MaterialError('connector_collection_denied')
        return dict(status='prepared',connector=self.name,collection=collection,operation=operation,payload=payload)
    async def execute(self,preview,*,idempotency_key,actor=None): return ToolResult('unavailable',diagnostics=('connector_unavailable',))
    async def reconcile(self,key,*,actor=None): return ToolResult('unavailable',diagnostics=('connector_reconciliation_unavailable',))
    async def revoke(self): self.connected=False; self.collections=()

def register_connectors(registry,connectors=None):
    from agents.tools.registry import Tool,object_schema,STRING
    connectors=connectors or [Connector(name) for name in ('calendar','tasks','storage')]
    for connector in connectors:
        properties=dict(collection=STRING,operation=dict(enum=['create','update','delete']),payload=dict(type='object'))
        async def preview(args,c,adapter=connector):
            value=await adapter.preview(args['collection'],args['operation'],args['payload'],actor=c.actor)
            return ToolResult('success' if value['status']=='prepared' else 'unavailable',dict(preview=value))
        async def write(args,c,adapter=connector):
            value=await adapter.preview(args['collection'],args['operation'],args['payload'],actor=c.actor)
            if value['status']!='prepared': return ToolResult('unavailable',diagnostics=('connector_unavailable',))
            await c.validate()
            return await adapter.execute(value,idempotency_key=c.idempotency_key,actor=c.actor)
        async def reconcile(args,c,key,adapter=connector):
            value=await adapter.preview(args['collection'],args['operation'],args['payload'],actor=c.actor)
            if value['status']!='prepared': return ToolResult('unavailable',diagnostics=('connector_unavailable',))
            return await adapter.reconcile(key,actor=c.actor)
        registry.register(Tool('connector.'+connector.name+'.preview','1',object_schema(properties),object_schema(dict(preview=dict(type='object'))),preview,max_bytes=100000))
        registry.register(Tool('connector.'+connector.name+'.write','1',object_schema(properties),object_schema(dict(result=dict(type='object'))),write,'external',max_bytes=100000,resources=lambda a:(a['collection'],),recipient=lambda a:a['collection'],idempotent=connector.supports_idempotency,reconcile=reconcile if connector.supports_reconciliation else None))
