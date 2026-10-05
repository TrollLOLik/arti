import base64,csv
from io import StringIO
from hashlib import sha256
from agents.tools.registry import Tool,ToolResult,object_schema,STRING
from materials.types import MaterialError
from agents.subscriptions import semantic_fingerprint

def register_documents(registry):
    async def data_export(args,c):
        from materials.dataset_repository import DatasetRepository
        data=await DatasetRepository(c.service.repository).load_dataset(args['dataset_id'],c.actor)
        cells=data.cells; maxr=max(x.row for x in cells)+1; maxc=max(x.column for x in cells)+1
        matrix=[['' for _ in range(maxc)] for _ in range(maxr)]
        for x in cells: matrix[x.row][x.column]=x.normalized.value if x.normalized.kind=='number' else x.raw
        if args['format']=='csv':
            # Spreadsheet formula injection is escaped only in exported presentation.
            out=StringIO(newline=''); writer=csv.writer(out)
            writer.writerows(["'"+str(v) if isinstance(v,str) and v[:1] in ('=','+','-','@') and not any(x.row==r and x.column==j and x.normalized.kind=='number' for x in cells) else v for j,v in enumerate(row)] for r,row in enumerate(matrix))
            content=out.getvalue().encode('utf-8-sig')
        else:
            from artifacts.document_styles import dataset_xlsx
            content=dataset_xlsx(data,matrix)
        return ToolResult('success',dict(files=[dict(name='data.'+args['format'],base64=base64.b64encode(content).decode(),sha256=sha256(content).hexdigest())],content_hash=semantic_fingerprint(matrix)),dependencies=(data.id,))
    registry.register(Tool('documents.data_export','1',object_schema(dict(dataset_id=STRING,format=dict(enum=['csv','xlsx']))),object_schema(dict(files=dict(type='array'),content_hash=STRING)),data_export,max_bytes=4000000))
    async def report(args,c):
        from artifacts.revisions import ArtifactRepository
        row=await ArtifactRepository(c.service.repository).get(args['artifact_id'],c.actor)
        if row['revision']!=args['revision']: raise MaterialError('stale_artifact_revision')
        if args['format']=='pdf':
            from artifacts.export import export_current
            repo=ArtifactRepository(c.service.repository); repo.service=c.service
            content=(await export_current(repo,args['artifact_id'],c.actor,revision=args['revision']))[0]['report.pdf']
        else:
            from artifacts.document_styles import report_docx
            from artifacts.export import export_current
            repo=ArtifactRepository(c.service.repository); repo.service=c.service
            files,_=await export_current(repo,args['artifact_id'],c.actor,revision=args['revision'])
            content=report_docx(row['spec'],args['revision'],files)
        return ToolResult('success',dict(files=[dict(name='report.'+args['format'],base64=base64.b64encode(content).decode(),sha256=sha256(content).hexdigest())],content_hash=semantic_fingerprint(row['spec'])),dependencies=(row['head'],))
    registry.register(Tool('documents.report','1',object_schema(dict(artifact_id=STRING,revision=dict(type='integer',minimum=1),format=dict(enum=['pdf','docx']))),object_schema(dict(files=dict(type='array'),content_hash=STRING)),report,timeout=120,max_bytes=4000000))
