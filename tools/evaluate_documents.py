"""Known-truth document corpus; exact facts, CER, coverage and worker metrics.

These are synthetic engineering checks, not accuracy on arbitrary real scans.
No model judging and no external provider calls.
"""
import asyncio
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import re
from materials.extractors.basic import BasicExtractor
from materials.extractors.documents import DocumentExtractor,DOCX
from materials.extractors.isolation import run_worker
from materials.types import ExtractionBundle
from tests.materials.document_fixtures import structured_pdf,scanned_pdf,rich_docx,damaged_page_pdf
from tools.check_document_stack import health


def distance(left,right):
    previous=list(range(len(right)+1))
    for i,a in enumerate(left,1):
        row=[i]
        for j,b in enumerate(right,1): row.append(min(row[-1]+1,previous[j]+1,previous[j-1]+(a!=b)))
        previous=row
    return previous[-1]


def normalized(text): return re.sub(r'\s+',' ',text).strip()


async def main():
    truth=' '.join(['Отчёт проекта Арти','Дата: 30 сентября 2026','Аренда: 1200 рублей','Доставка: 300 рублей','Итого: 1500 рублей',
        'Решение ещё не принято.','Проверка источников обязательна.','Документ содержит наблюдения,','а не разрешение на действия.'])
    cases=[('native-layout',structured_pdf(),'application/pdf',('1200','300','840','2750','3590'),None,'development'),
        ('docx-merged-nested',rich_docx(),DOCX,('840','2750','42','Подвал документа'),None,'development'),
        ('scan-upright',scanned_pdf(),'application/pdf',('1200','300','1500','30 сентября'),truth,'development'),
        ('scan-grid',scanned_pdf(table=True),'application/pdf',('1200','300','Статья'),truth+' Статья Рубли Аренда 1200 Доставка 300','development'),
        ('scan-90',scanned_pdf(angle=90),'application/pdf',('1200','300','1500'),truth,'development'),
        ('scan-180',scanned_pdf(angle=180),'application/pdf',('1200','300','1500'),truth,'development'),
        ('scan-skew',scanned_pdf(angle=3),'application/pdf',('1200','300','1500'),truth,'development'),
        ('heldout-scan',scanned_pdf(heldout=True),'application/pdf',('2750','840','3590','отменено','18 ноября'),None,'synthetic_heldout'),
        ('damaged-page',damaged_page_pdf(),'application/pdf',('840','2750'),None,'negative_coverage')]
    rows=[]; extractor=DocumentExtractor()
    for name,data,mime,facts,reference,split in cases:
        metrics={}
        payload=await run_worker(dict(operation='extract',options=extractor.options,asset_id=name,version=1,mime=mime),data,extractor.limits,trace=metrics)
        bundle=ExtractionBundle.from_dict(payload)
        text=normalized(' '.join(b.text for b in bundle.blocks if b.metadata.get('role')!='table_cell'))
        native=BasicExtractor().extract(name,1,data,mime)
        native_text=' '.join(b.text for b in native.blocks)
        row=dict(case=name,split=split,source_sha256=sha256(data).hexdigest(),expected_facts=len(facts),
            exact_facts=sum(f in text for f in facts),native_baseline_facts=sum(f in native_text for f in facts),
            coverage=asdict(bundle.manifest),blocks=len(bundle.blocks),tables=sum(b.kind=='table' for b in bundle.blocks),
            uncertain_blocks=sum(b.quality=='uncertain' for b in bundle.blocks),metrics=metrics)
        if reference:
            row['character_error_rate']=round(distance(normalized(reference),text)/len(normalized(reference)),5)
        rows.append(row)
    # Actual numeric evidence crop from table cell, not merely a plausible bbox.
    data=structured_pdf(); bundle=await extractor.extract_async('region',1,data,'application/pdf')
    table=next(b for b in bundle.blocks if b.kind=='table'); cell=next(c for c in table.metadata['cells'] if c['text']=='1200')
    block=next(b for b in bundle.blocks if b.block_id==cell['block_id'])
    crop=await extractor.region_async(data,'application/pdf',asdict(block.locator),reread=True)
    region_pass='1200' in ' '.join(w['text'] for w in crop['observation']['words'])
    report=dict(batch='A05',synthetic=True,fixture_split_is_independent_real_corpus=False,stack=health(),rows=rows,
        exact_facts=sum(r['exact_facts'] for r in rows),expected_facts=sum(r['expected_facts'] for r in rows),
        native_baseline_facts=sum(r['native_baseline_facts'] for r in rows),numeric_region_reread_passed=region_pass,
        provider_calls=0,provider_cost_usd=0,working_database_mutated=False,telegram_calls=0,
        limitations=['Printed text, not handwriting','Layout/caption/continuation heuristics are not semantic proof',
            'Synthetic checks do not estimate real corpus accuracy','RSS is sampled, not an OS high-water measurement'])
    Path('docs/evaluation/materials_documents.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('rows','stack','limitations')}))
    return 0 if report['exact_facts']==report['expected_facts'] and region_pass else 1


if __name__=='__main__': raise SystemExit(asyncio.run(main()))
