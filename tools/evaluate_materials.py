"""Synthetic evidence corpus: baseline/parser improvements, no model judge."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
from materials.extractors.basic import BasicExtractor, render_text
from tests.materials.fixtures import docx, pdf
from utils.document_parser import extract_text_from_file


async def main():
    rows=[]
    with tempfile.TemporaryDirectory() as root:
        for name,data,mime,expected in [('budget.docx',docx(),'application/vnd.openxmlformats-officedocument.wordprocessingml.document',('1200','Аренда')),
            ('budget.pdf',pdf(),'application/pdf',('1200','300')),
            ('budget.txt','Бюджет\nАренда: 1200\nПредложение даты: 12 октября'.encode(),'text/plain',('1200','Предложение'))]:
            path=Path(root)/name
            path.write_bytes(data)
            baseline=await extract_text_from_file(path,name)
            started=time.perf_counter()
            bundle=BasicExtractor().extract(name,1,data,mime)
            native=render_text(bundle)
            rows.append(dict(case=name,split='development',expected_facts=len(expected),
                legacy_facts=sum(f in baseline for f in expected),native_facts=sum(f in native for f in expected),
                locators=[b.locator.kind.value for b in bundle.blocks],blocks=len(bundle.blocks),
                coverage=bundle.manifest.coverage,limitations=bundle.manifest.limitations,
                seconds=round(time.perf_counter()-started,5)))
    # Held-out values and layout are different from the development fixture.
    data='Итог: 2750\nПредложение отменено\nНовая проверка: 18 ноября'.encode()
    bundle=BasicExtractor().extract('heldout',1,data,'text/plain')
    rows.append(dict(case='heldout-russian-lines',split='held_out',expected_facts=3,
        native_facts=sum(f in render_text(bundle) for f in ('2750','отменено','18 ноября')),
        locators=[b.locator.kind.value for b in bundle.blocks],blocks=len(bundle.blocks),coverage=bundle.manifest.coverage))
    report=dict(version='materials-1',synthetic=True,provider_calls=0,rows=rows,
        unevaluated=['OCR','audio_speech','video_semantics','infographic_visual_quality','agent_completion','human_ratings','Telegram_pilot'])
    Path('docs/evaluation/materials_native_baseline.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(cases=len(rows),native_facts=sum(r['native_facts'] for r in rows),expected_facts=sum(r['expected_facts'] for r in rows))))


if __name__=='__main__':
    asyncio.run(main())
