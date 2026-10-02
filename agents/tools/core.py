"""First party material/computation/artifact tools, also used by Telegram commands."""
import asyncio,base64
from dataclasses import asdict
from agents.tools.registry import Registry,Tool,ToolResult,object_schema,STRING
from agents.subscriptions import semantic_fingerprint
from materials.types import MaterialError

def build_registry():
    registry=Registry()
    def add(name,properties,handler,output,effect='read',**kw):
        registry.register(Tool(name,'1',object_schema(properties),object_schema(output),handler,effect,**kw))
    obj=dict(type='object'); arr=dict(type='array',maxItems=200); integer=dict(type='integer',minimum=1)
    from artifacts.schema import ARTIFACT_SCHEMA,REF
    conversion=object_schema(dict(from_unit=STRING,to_unit=STRING,factor=STRING,source=REF,dataset_id=STRING,lower=STRING,upper=STRING),['from_unit','to_unit','factor','source'])
    calculation=object_schema(dict(operation=dict(enum=['sum','mean','min','max','count','percent','change','ratio','difference','convert','compare','reconcile']),selection=STRING,reference=STRING,missing=dict(enum=['error','exclude']),allow_uncertain=dict(type='boolean'),target_unit=STRING,conversions=dict(type='array',maxItems=16,items=conversion),tolerance=STRING),['operation','selection'])
    async def read(args,c):
        from materials.types import EvidenceRef
        id,bundle=await c.service.extract(args['asset_id'],c.actor)
        blocks=bundle.blocks[:120]; refs=tuple(asdict(EvidenceRef(bundle.asset_id,bundle.asset_version,id,b.block_id,b.locator)) for b in blocks)
        return ToolResult('success',dict(asset_id=bundle.asset_id,blocks=[dict(text=b.text[:12000],kind=b.kind,source=r,quality=b.quality) for b,r in zip(blocks,refs)],coverage=bundle.manifest.coverage,limitations=list(bundle.manifest.limitations)),refs)
    add('materials.read',dict(asset_id=STRING),read,dict(asset_id=STRING,blocks=arr,coverage=STRING,limitations=arr),max_bytes=2000000)
    async def search(args,c):
        from materials.index import MaterialIndex
        from projects.repository import ProjectRepository
        sources=await ProjectRepository(c.service.repository).materials_for(c.project_id,c.actor)
        ids=[m['asset_id'] for m in sources if m['status']=='current']
        hits=await MaterialIndex(c.service.repository).search(c.actor,args['query'],asset_ids=ids,limit=8)
        refs=tuple(asdict(h.source) for h in hits)
        return ToolResult('success',dict(hits=[dict(text=h.text[:6000],source=asdict(h.source),quality=h.quality,filename=h.filename) for h in hits],coverage='indexed_project_window'),refs,dependencies=tuple(dict.fromkeys(h.observation_id for h in hits if h.observation_id)))
    add('materials.search',dict(query=STRING),search,dict(hits=arr,coverage=STRING),max_bytes=150000)
    async def dataset(args,c):
        from materials.datasets import DatasetPolicy
        datasets=await c.service.datasets(args['asset_id'],c.actor,policy=DatasetPolicy.from_dict(args['policy']))
        return ToolResult('success',dict(datasets=[dict(id=d.id,name=d.name,columns=d.columns,coverage=d.coverage) for d in datasets]),dependencies=tuple(d.id for d in datasets))
    add('dataset.extract',dict(asset_id=STRING,policy=obj),dataset,dict(datasets=arr),max_bytes=150000)
    async def compute(args,c):
        from artifacts.computation import ComputationSpec,Conversion
        value=args['spec']; spec=ComputationSpec(**{**value,'conversions':tuple(Conversion(**v) for v in value.get('conversions',[]))})
        result=await c.service.compute(args['dataset_id'],c.actor,spec)
        return ToolResult('success',dict(id=result.id,result=result.result,warnings=list(result.warnings),checks=list(result.checks)),dependencies=(result.id,))
    add('dataset.compute',dict(dataset_id=STRING,spec=calculation),compute,dict(id=STRING,result=obj,warnings=arr,checks=arr),max_bytes=200000)
    async def transform(args,c):
        from materials.dataset_repository import DatasetRepository
        from materials.derivatives import DerivativeRepository
        d=await DatasetRepository(c.service.repository).load_dataset(args['dataset_id'],c.actor)
        selected=d.select(args['range']); rows=[dict(address=x.address,raw=x.raw,value=x.normalized.value,kind=x.normalized.kind,unit=x.normalized.unit,source=asdict(x.source)) for x in selected]
        if args['order']=='numeric':
            from decimal import Decimal
            if any(r['kind']!='number' for r in rows): raise MaterialError('transform_numeric_required')
            rows.sort(key=lambda r:Decimal(r['value']))
        id=await DerivativeRepository(c.service.repository).save(c.actor,'dataset_transform',dict(rows=rows,source_dataset=d.id),[],inputs=[d.id])
        return ToolResult('success',dict(id=id,rows=rows),dependencies=(id,))
    add('dataset.transform',dict(dataset_id=STRING,range=STRING,order=dict(enum=['source','numeric'])),transform,dict(id=STRING,rows=arr),max_bytes=2000000)
    async def create(args,c):
        from artifacts.revisions import ArtifactRepository
        from materials.derivatives import DerivativeRepository
        async with c.service.repository.pool.acquire() as conn: refs=await DerivativeRepository(c.service.repository)._chain(conn,(await conn.fetchrow('SELECT plan_id FROM arti_tasks WHERE id=$1',c.task_id))['plan_id'],c.actor)
        from hashlib import sha256
        id=sha256(c.idempotency_key.encode()).hexdigest()[:32]
        row=await ArtifactRepository(c.service.repository).create(c.project_id,c.actor,args['spec'],id=id,sources=refs)
        return ToolResult('success',dict(id=row['id'],revision=row['revision'],derivative_id=row['head'],content_hash=semantic_fingerprint(row['spec'])),dependencies=(row['head'],))
    add('artifact.create',dict(spec=ARTIFACT_SCHEMA),create,dict(id=STRING,revision=integer,derivative_id=STRING,content_hash=STRING),'write',max_bytes=10000,idempotent=True)
    async def revise(args,c):
        from artifacts.revisions import ArtifactRepository
        row,diff=await ArtifactRepository(c.service.repository).revise(args['id'],c.actor,args['revision'],args['patches'])
        return ToolResult('success',dict(id=row['id'],revision=row['revision'],derivative_id=row['head'],diff=diff,content_hash=semantic_fingerprint(row['spec'])),dependencies=(row['head'],))
    add('artifact.patch',dict(id=STRING,revision=integer,patches=arr),revise,dict(id=STRING,revision=integer,derivative_id=STRING,diff=obj,content_hash=STRING),'write',max_bytes=10000,resources=lambda a:(a['id'],))
    async def render(args,c):
        from artifacts.revisions import ArtifactRepository
        from artifacts.export import export_current
        from hashlib import sha256
        repo=ArtifactRepository(c.service.repository); repo.service=c.service
        files,row=await export_current(repo,args['id'],c.actor,revision=args['revision'])
        allowed={name:data for name,data in files.items() if name==args['format'] or (args['format']=='png' and name.endswith('.png')) or (args['format']=='svg' and name.endswith('.svg'))}
        if not allowed: raise MaterialError('export_format_invalid')
        return ToolResult('success',dict(files=[dict(name=n,sha256=sha256(b).hexdigest(),base64=base64.b64encode(b).decode()) for n,b in allowed.items()],content_hash=semantic_fingerprint(row['spec'])),dependencies=(row['head'],))
    add('artifact.export',dict(id=STRING,revision=integer,format=dict(enum=['png','svg','report.pdf','spec.json'])),render,dict(files=arr,content_hash=STRING),timeout=120,max_bytes=4000000)
    from agents.tools.research import register_research
    from agents.tools.documents import register_documents
    register_research(registry); register_documents(registry)
    from agents.tools.media import register_media
    register_media(registry)
    from agents.connectors import register_connectors
    register_connectors(registry)
    async def runtime(args,c):
        from agents.sandbox import run_isolated
        result=await run_isolated(args['program'],args['inputs'],guard=c.validate)
        return ToolResult('success',dict(values=result))
    add('runtime.transform',dict(program=dict(type='array',minItems=1,maxItems=100),inputs=obj),runtime,dict(values=obj),timeout=25,max_bytes=2000000)
    from agents.tools.planning import register_planning
    register_planning(registry)
    return registry
