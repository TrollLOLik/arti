"""Live capability probes on synthetic data. No Telegram, secrets or user files."""
import argparse
import asyncio
import base64
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import time
import httpx
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
from cognition.interpreter import environment_key


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',default='stealth/space-bunny-alpha')
    args=parser.parse_args()
    load_dotenv()
    key=environment_key()
    if not key:
        raise SystemExit('OpenRouter key unavailable')
    base='https://openrouter.ai/api/v1'
    probes=[]
    async with httpx.AsyncClient(timeout=45,trust_env=False) as client:
        metadata_response=await client.get(base+'/models')
        metadata_response.raise_for_status()
        metadata=next((m for m in metadata_response.json().get('data',[]) if m['id']==args.model),None)
        async def probe(name,messages,check,**extra):
            started=time.perf_counter()
            try:
                response=await client.post(base+'/chat/completions',headers={'Authorization':'Bearer '+key},json=dict(model=args.model,messages=messages,max_tokens=128,temperature=0,**extra))
                if response.status_code!=200:
                    probes.append(dict(name=name,passed=False,error_code='http_'+str(response.status_code),seconds=round(time.perf_counter()-started,2)))
                    return
                value=response.json()
                message=value['choices'][0]['message']
                usage=value.get('usage',{})
                probes.append(dict(name=name,passed=bool(check(message)),seconds=round(time.perf_counter()-started,2),
                    prompt_tokens=usage.get('prompt_tokens'),completion_tokens=usage.get('completion_tokens'),reported_cost=usage.get('cost')))
            except Exception as exc:
                probes.append(dict(name=name,passed=False,error_code=type(exc).__name__,seconds=round(time.perf_counter()-started,2)))
        await probe('text',[dict(role='user',content='Return exactly MATERIAL_OK.')],lambda m:'MATERIAL_OK' in (m.get('content') or ''))
        parameters=set((metadata or {}).get('supported_parameters',[]))
        if 'tools' in parameters:
            tool=dict(type='function',function=dict(name='read_fixture',description='Read fixture evidence.',parameters=dict(type='object',properties=dict(source_id=dict(type='string')),required=['source_id'],additionalProperties=False)))
            await probe('native_tools_forced',[dict(role='user',content='Call read_fixture with source_id fixture-42.')],
                lambda m:any(c.get('function',{}).get('name')=='read_fixture' and json.loads(c['function']['arguments']).get('source_id')=='fixture-42' for c in m.get('tool_calls',[])),tools=[tool],tool_choice=dict(type='function',function=dict(name='read_fixture')),provider=dict(require_parameters=True))
            await probe('native_tools_auto',[dict(role='user',content='To answer, call read_fixture with source_id fixture-42. Do not answer without calling the tool.')],
                lambda m:any(c.get('function',{}).get('name')=='read_fixture' and json.loads(c['function']['arguments']).get('source_id')=='fixture-42' for c in m.get('tool_calls',[])),tools=[tool],tool_choice='auto')
        modalities=(metadata or {}).get('architecture',{}).get('input_modalities',[])
        if 'image' in modalities:
            token=str(secrets.randbelow(900000)+100000)
            image=Image.new('RGB',(480,180),'white')
            draw=ImageDraw.Draw(image)
            try: font=ImageFont.truetype('arial.ttf',72)
            except OSError: font=ImageFont.load_default(size=72)
            draw.text((60,40),token,font=font,fill='black')
            buffer=BytesIO(); image.save(buffer,format='PNG')
            content=[dict(type='text',text='Read the six-digit code in this image. Return only the code.'),dict(type='image_url',image_url=dict(url='data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode()))]
            await probe('vision_png',[dict(role='user',content=content)],lambda m:token in (m.get('content') or ''))
    report=dict(model=args.model,endpoint=base,observed_at=datetime.now(timezone.utc).isoformat(),
        metadata_found=metadata is not None,input_modalities=modalities,supported_parameters=sorted(parameters),
        probes=probes,provider_calls=len(probes),synthetic_data_only=True,Telegram_calls=0,
        automatic_proxy_manifest_update=False)
    Path('docs/evaluation/materials_provider_probe.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':
    asyncio.run(main())
