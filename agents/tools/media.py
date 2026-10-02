"""One adapter for existing generators; unknown provider outcomes are not retried."""
import asyncio,base64,os
from hashlib import sha256
from pathlib import Path
from agents.tools.registry import Tool,ToolResult,object_schema,STRING
from materials.types import MaterialError

def register_media(registry):
    async def generate(args,c,kind):
        refs=[]; sources=[]
        if kind=='image':
            for id in args['reference_assets']:
                row,rev,pixels=await c.service.read_bytes(id,c.actor)
                if not rev['mime'].startswith('image/'): raise MaterialError('media_reference_mime')
                from materials.types import EvidenceRef
                from dataclasses import asdict
                extraction,bundle=await c.service.extract(id,c.actor)
                b=bundle.blocks[0]
                sources.append(pixels); refs.append(asdict(EvidenceRef(id,row['current_version'],extraction,b.block_id,b.locator)))
            await c.validate()
            for ref in refs:
                from artifacts.validation import ref_from_dict
                await c.service.repository.resolve(ref_from_dict(ref),c.actor)
            from ai.image import generate_image
            result=await asyncio.to_thread(generate_image,args['prompt']+'\nIllustrative decoration only. Do not draw text, labels, numbers or factual charts.',sources or None,args['aspect_ratio'],'1K',1)
            pixels=result[0] if isinstance(result,list) and result else result
            if not isinstance(pixels,bytes): raise MaterialError('media_provider_unavailable')
            name='illustration.png'
        elif kind=='video':
            from ai.image import generate_video,VIDEO_MODELS
            result=await asyncio.to_thread(generate_video,args['prompt'],None,VIDEO_MODELS[args['model']],str(args['duration']),args['aspect_ratio'])
            if not result: raise MaterialError('media_provider_unavailable')
            if isinstance(result,bytes): pixels=result
            elif isinstance(result,str) and result.startswith(('https://','http://')):
                from utils.public_fetch import fetch_public
                pixels=(await fetch_public(result,max_bytes=2*1024**2,allowed_mimes={'video/mp4','video/webm'},validate=c.validate)).data
            else: raise MaterialError('media_provider_result_invalid')
            name='illustration.mp4'
        else:
            from ai.music import generate_music
            result=await asyncio.to_thread(generate_music,args['prompt'],args['instrumental'],args['style'])
            if not result: raise MaterialError('media_provider_unavailable')
            from utils.public_fetch import fetch_public
            if isinstance(result,str) and result.startswith(('https://','http://')): pixels=(await fetch_public(result,max_bytes=2*1024**2,allowed_mimes={'audio/mpeg','audio/mp3','audio/mp4','video/mp4'},validate=c.validate)).data
            else:
                # Only generator's task temp directory may produce a local file.
                path=Path(result).resolve(); temp=Path('temp').resolve()
                if not path.is_relative_to(temp) or path.stat().st_size>2*1024**2: raise MaterialError('media_provider_result_invalid')
                pixels=path.read_bytes()
            name='illustration.mp3'
        if len(pixels)>2*1024**2: raise MaterialError('media_output_budget')
        from materials.validation import inspect_bytes
        mime=inspect_bytes(pixels,name)
        if not mime.startswith({'image':'image/','video':'video/','music':'audio/'}[kind]) and not (kind=='music' and mime=='video/mp4'): raise MaterialError('media_provider_mime_invalid')
        suffix={'image/jpeg':'jpg','image/png':'png','image/webp':'webp','image/gif':'gif','video/mp4':'mp4','video/webm':'webm','audio/mpeg':'mp3','audio/mp4':'m4a','audio/ogg':'ogg','audio/wav':'wav'}.get(mime)
        if not suffix: raise MaterialError('media_provider_mime_invalid')
        name='illustration.'+suffix
        for ref in refs:
            from artifacts.validation import ref_from_dict
            await c.service.repository.resolve(ref_from_dict(ref),c.actor)
        outputs=dict(files=[dict(name=name,base64=base64.b64encode(pixels).decode(),sha256=sha256(pixels).hexdigest())],role='fictional_decoration_not_evidence',mime=mime)
        dependencies=()
        if kind=='image':
            from artifacts.illustrations import IllustrationRepository
            from materials.derivatives import DerivativeRepository
            async with c.service.repository.pool.acquire() as conn:
                plan_id=await conn.fetchval('SELECT plan_id FROM arti_tasks WHERE id=$1',c.task_id)
                sources=await DerivativeRepository(c.service.repository)._chain(conn,plan_id,c.actor)
            variant=await IllustrationRepository(c.service.repository).generated(c.actor,pixels,prompt=args['prompt'],provider='existing_image_generator',parameters=dict(aspect_ratio=args['aspect_ratio'],resolution='1K'),sources=[*sources,*refs],inputs=[plan_id])
            outputs['illustration_id']=variant; dependencies=(variant,)
        return ToolResult('success',outputs,tuple(refs),('provider_cost_not_reported_reserved_ceiling','generated_content_quality_unverified'),os.getenv('ARTI_MEDIA_COST_CEILING','1'),dict(status='provider_output_received',digest=sha256(pixels).hexdigest()),dependencies)
    for kind,properties in [('image',dict(prompt=STRING,reference_assets=dict(type='array',maxItems=4,items=STRING),aspect_ratio=dict(enum=['1:1','16:9','9:16']))),('video',dict(prompt=STRING,model=dict(enum=['seedance','veo','sora']),duration=dict(enum=[4,8]),aspect_ratio=dict(enum=['16:9','9:16']))),('music',dict(prompt=STRING,instrumental=dict(type='boolean'),style=STRING))]:
        async def handler(args,c,k=kind): return await generate(args,c,k)
        output=dict(files=dict(type='array'),role=STRING,mime=STRING)
        if kind=='image': output['illustration_id']=STRING
        registry.register(Tool('media.'+kind,'1',object_schema(properties),object_schema(output),handler,'external',timeout=240,max_cost=os.getenv('ARTI_MEDIA_COST_CEILING','1'),max_bytes=4000000,resources=lambda a:tuple(a.get('reference_assets',()))))
