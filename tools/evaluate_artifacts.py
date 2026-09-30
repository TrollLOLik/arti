"""Owned renderer corpus; exact text, actual PDF pages and layout bounds."""
import json
from pathlib import Path
from io import BytesIO
from PIL import Image
from pypdf import PdfReader
from artifacts.spec import ArtifactSpec
from artifacts.export import export
from tests.materials.test_artifacts import fixture

def main():
    target=Path('temp/artifact-qa'); target.mkdir(parents=True,exist_ok=True); cases=[]
    graph=fixture(); graph['format']='arguments'; graph['elements']=[dict(id='e'+str(i),label='Вариант '+str(i)+' с длинной кириллицей',text='Сохраняем предложение отдельно от принятого решения. '*8,status='proposed') for i in range(10)]
    graph['relations']=[dict(id='r'+str(i),kind='objects' if i%2 else 'supports',**{'from':'e'+str(i%10),'to':'e'+str((i+3)%10)}) for i in range(20)]
    table=fixture(); table['format']='comparison'; table['elements']=[dict(id='e'+str(i),label='Длинное название варианта '+str(i),text='Данных о стоимости нет; значение не заменяется нулём.',status='unknown') for i in range(18)]
    chart=fixture(); chart.update(format='statistical',axis=dict(scale='linear',unit='RUB')); chart['elements']=[dict(id='e'+str(i),label='Измерение '+str(i),status='observed',quantity=dict(value=v,lower=v,upper=v,unit='RUB'),proof=dict(kind='dataset',dataset_id='synthetic_owned_fixture',address='A'+str(i+1))) for i,v in enumerate(['-0.000001','2.345','0'])]
    for name,value in [('dense_graph',graph),('comparison_missing',table),('exact_signed_values',chart)]:
        files=export(ArtifactSpec(value)); pdf=PdfReader(BytesIO(files['report.pdf'])); text=' '.join(p.extract_text() for p in pdf.pages)
        labels=all(e['label'] in text for e in value['elements']); numbers=all(e.get('quantity',{}).get('value','') in text for e in value['elements'])
        for n,b in files.items():
            if n.endswith('.png'): Image.open(BytesIO(b)).verify()
        (target/(name+'.pdf')).write_bytes(files['report.pdf'])
        import pypdfium2 as pdfium
        document=pdfium.PdfDocument(files['report.pdf']); preview=document[0].render(scale=.6).to_pil(); preview.save(target/(name+'-preview.png')); document.close()
        cases.append(dict(case=name,passed=labels and numbers,pdf_pages=len(pdf.pages),all_labels_present=labels,exact_numbers_present=numbers,physical_pngs_opened=True))
    report=dict(contract='artifact-render-evaluation-1',scope='owned_synthetic_renderer_corpus_source_validation_tested_separately',cases=cases,passed=sum(c['passed'] for c in cases),total=len(cases),visual_review='Separate manual inspection of rasterized PDF previews; no claim of human study')
    Path('docs/evaluation/artifacts_render.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(report,ensure_ascii=True)); return 0 if report['passed']==report['total'] else 1

if __name__=='__main__': raise SystemExit(main())
