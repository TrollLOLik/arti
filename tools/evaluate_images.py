"""OCR references and optional live structured vision; never Telegram/user files."""
import argparse
import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
import time
from materials.extractors.images import ImageExtractor
from tests.materials.test_images import image_bytes
from tests.materials.image_fixtures import diagram,logarithmic_chart


async def evaluate(live=False,model='stealth/space-bunny-alpha',live_case=None):
    cases=[]
    for name,options,expected in (('printed',{},('1200','1500')),('skewed',{'angle':5},('1200','1500')),
        ('rotated',{'angle':90},('1200','1500')),('heldout',{'heldout':True},('2750','3590'))):
        started=time.perf_counter(); bundle=await ImageExtractor().extract_async(name,1,image_bytes(**options),'image/png')
        text=' '.join(b.text for b in bundle.blocks)
        cases.append(dict(case=name,passed=all(x in text for x in expected),expected=list(expected),seconds=round(time.perf_counter()-started,3),
            region_boxes_valid=all(b.locator.bbox is not None for b in bundle.blocks),quality='uncertain'))
    probes=[]
    if live:
        from dotenv import load_dotenv
        from cognition.interpreter import environment_key
        from openai import AsyncOpenAI
        import httpx
        from ai.capabilities import CapabilityRegistry,ModelEndpoint
        from ai.providers.visual import VisualAnalyzer
        load_dotenv(); key=environment_key(); endpoint='https://openrouter.ai/api/v1'
        if not key: raise RuntimeError('OpenRouter key unavailable')
        async with httpx.AsyncClient(timeout=45,trust_env=False) as http:
            response=await http.get(endpoint+'/models'); response.raise_for_status()
            metadata=next((m for m in response.json()['data'] if m['id']==model),{})
            inputs=metadata.get('architecture',{}).get('input_modalities',[])
            if 'image' not in inputs: raise RuntimeError('Model image capability unavailable')
        # Features are a set; structured JSON is validated locally and never
        # depends on an untested provider response_format or tool mode.
        registry=CapabilityRegistry([ModelEndpoint(model,'openai',endpoint,frozenset({'text','image'}),frozenset(),evidence='metadata',observed_at=datetime.now(timezone.utc))])
        async with AsyncOpenAI(base_url=endpoint,api_key=key,http_client=httpx.AsyncClient(timeout=45,trust_env=False),max_retries=0) as client:
            analyzer=VisualAnalyzer(model,registry=registry,endpoint=endpoint,client=client)
            fixtures=[('crossing',*diagram()),('crossing_heldout',*diagram(heldout=True)),('log_axis',logarithmic_chart(),())]
            for name,data,labels in fixtures:
                if live_case and name!=live_case: continue
                started=time.perf_counter()
                try:
                    value=await analyzer.observe(data,'image/png'); objects=value['objects']; relations=value['relations']
                    recognized={o.label.strip() for o in objects}
                    labels_ok=all(any(label==s or s.startswith(label+' ') for s in recognized) for label in labels)
                    mapping={o.id:o.label.strip()[:1] for o in objects}
                    observed_edges={(mapping[r.source],mapping[r.target]) for r in relations if r.kind=='points_to'}
                    expected_edges={(labels[0],labels[3]),(labels[2],labels[1])} if labels else set()
                    correct_edges=expected_edges<=observed_edges if labels else any(o.kind=='axis' and o.axis_scale=='log' for o in objects)
                    probes.append(dict(case=name,schema_passed=True,labels_passed=labels_ok,relations_or_axis_passed=correct_edges,
                        extra_arrow_edges=len(observed_edges-expected_edges),objects=len(objects),relations=len(relations),seconds=round(time.perf_counter()-started,3)))
                except Exception as exc: probes.append(dict(case=name,schema_passed=False,error_code=getattr(exc,'code',type(exc).__name__),validation_reason=str(exc.__cause__)[:160] if getattr(exc,'code',None)=='invalid_visual_schema' else None,seconds=round(time.perf_counter()-started,3)))
    return dict(contract='image-evaluation-1',scope='original_synthetic_development_and_holdout; not real-world accuracy',ocr_cases=cases,
        ocr_passed=sum(c['passed'] for c in cases),ocr_total=len(cases),live_probes=probes,provider_calls=len(probes),model=model if live else None,
        telegram_calls=0,user_material_sent=False)


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--live',action='store_true'); parser.add_argument('--model',default='stealth/space-bunny-alpha')
    parser.add_argument('--case',choices=('crossing','crossing_heldout','log_axis')); args=parser.parse_args()
    report=asyncio.run(evaluate(args.live,args.model,args.case))
    path=Path('docs/evaluation/materials_images'+('_live' if args.live else '')+('_'+args.case if args.case else '')+'.json')
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))
    if report['ocr_passed']!=report['ocr_total']: raise SystemExit(1)


if __name__=='__main__': main()
