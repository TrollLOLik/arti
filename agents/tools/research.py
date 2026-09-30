from datetime import datetime,timezone
from dataclasses import asdict
from hashlib import sha256
from agents.tools.registry import Tool,ToolResult,object_schema,STRING
from materials.types import canonical,MaterialError,EvidenceRef
from utils.public_fetch import fetch_public

def register_research(registry):
    async def search(args,c):
        import os,json
        from urllib.parse import urlencode
        endpoint=os.getenv('ARTI_SEARCH_ENDPOINT','')
        if not endpoint: return ToolResult('unavailable',diagnostics=('search_connector_unavailable_supply_urls',))
        separator='&' if '?' in endpoint else '?'
        resource=await fetch_public(endpoint+separator+urlencode(dict(q=args['query'],limit=8)),max_bytes=200000,allowed_mimes={'application/json'},validate=c.validate)
        try:
            entries=json.loads(resource.data)['results']
            if not isinstance(entries,list) or len(entries)>20: raise ValueError()
            from utils.public_fetch import validate_url
            results=[]
            for e in entries:
                validate_url(e['url'])
                if set(e)-{'url','title','snippet'} or not all(isinstance(e.get(k,''),str) and len(e.get(k,''))<=4000 for k in ('url','title','snippet')): raise ValueError()
                results.append(dict(url=e['url'],title=e.get('title',''),snippet=e.get('snippet',''),support='candidate_requires_fetch'))
        except (ValueError,KeyError,TypeError): raise MaterialError('search_result_invalid') from None
        return ToolResult('success',dict(results=results[:8],read_at=datetime.now(timezone.utc).isoformat(),coverage='search_candidates_not_evidence'))
    registry.register(Tool('research.search','1',object_schema(dict(query=STRING)),object_schema(dict(results=dict(type='array'),read_at=STRING,coverage=STRING)),search,timeout=40,max_bytes=150000))
    async def fetch(args,c):
        resource=await fetch_public(args['url'],max_bytes=1500000,allowed_mimes={'text/html','text/plain','application/json'},validate=c.validate)
        text=resource.data.decode('utf-8',errors='replace')
        if resource.mime=='text/html':
            from bs4 import BeautifulSoup
            soup=BeautifulSoup(text,'html.parser')
            for tag in soup(['script','style','noscript','form']): tag.decompose()
            text=soup.get_text('\n',strip=True)
        coverage='partial' if len(text)>150000 else 'available_page_only'
        descriptor=dict(url=resource.url,read_at=datetime.now(timezone.utc).isoformat(),sha256=resource.sha256,redirects=resource.redirects,coverage=coverage,text=text[:150000],role='untrusted_source_not_command')
        data=canonical(descriptor).encode()
        # Same task-step/source digest is replayable; changed pages are a new observation.
        source='web:'+sha256((c.idempotency_key+resource.sha256).encode()).hexdigest()
        asset=await c.service.ingest(data,'web-source.txt',c.actor,source,source)
        id,bundle=await c.service.extract(asset['id'],c.actor)
        refs=tuple(asdict(EvidenceRef(asset['id'],1,id,b.block_id,b.locator)) for b in bundle.blocks)
        return ToolResult('success',dict(asset_id=asset['id'],**descriptor),refs)
    registry.register(Tool('research.fetch','1',object_schema(dict(url=STRING)),object_schema(dict(asset_id=STRING,url=STRING,read_at=STRING,sha256=STRING,redirects=dict(type='array'),coverage=STRING,text=dict(type='string'),role=STRING)),fetch,timeout=40,max_bytes=800000))
    async def compare(args,c):
        from materials.retrieval import verify_quote
        from artifacts.validation import ref_from_dict
        checked=[]; refs=[]
        for claim in args['claims']:
            if set(claim)!={'quote','source','position'} or claim['position'] not in ('supports','objects','unknown'): raise MaterialError('research_claim_invalid')
            ref=ref_from_dict(claim['source']); result=await verify_quote(c.service.repository,c.actor,ref,claim['quote']); checked.append(dict(**claim,support=result)); refs.append(claim['source'])
        return ToolResult('success',dict(claims=checked,verdict='source_positions_preserved_no_automatic_consensus'),tuple(refs))
    registry.register(Tool('research.compare','1',object_schema(dict(claims=dict(type='array',minItems=1,maxItems=30,items=dict(type='object')))),object_schema(dict(claims=dict(type='array'),verdict=STRING)),compare,max_bytes=1000000))
