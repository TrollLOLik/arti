"""Synthetic, seeded reference arithmetic and evidence checks; no provider judge."""
import asyncio
from decimal import Decimal,localcontext
import json
from pathlib import Path
import random
from artifacts.computation import ComputationSpec,compute
from materials.datasets import datasets_from_bundle,normalize,ColumnPolicy
from materials.extractors.tables import TableExtractor,XLSX
from materials.types import MaterialError
from tests.materials.table_fixtures import xlsx,styled_xlsx


async def evaluate():
    records=[]
    def record(case,passed,**details): records.append(dict(case=case,passed=bool(passed),**details))
    randomizer=random.Random(20260930)
    for split,count in (('development',8),('held_out_synthetic',8)):
        for index in range(count):
            cents=[randomizer.randrange(-1000000,1000000) for _ in range(8)]
            # Build sign-aware decimal strings; integer cents are the independent oracle.
            source=('Amount (RUB)\n'+''.join(f'{"-" if c<0 else ""}{abs(c)//100}.{abs(c)%100:02d}\n' for c in cents)).encode()
            bundle=await TableExtractor().extract_async(f'{split}-{index}',1,source,'text/csv')
            dataset=datasets_from_bundle(f'e-{split}-{index}',bundle)[0]
            result=compute(dataset,ComputationSpec('sum','A2:A9'))
            expected=Decimal(sum(cents))/100
            record(f'{split}:sum:{index}',Decimal(result.result['value'])==expected,expected=str(expected),actual=result.result['value'])
            repeated=compute(dataset,ComputationSpec('sum','A2:A9'))
            record(f'{split}:repeat_evidence:{index}',result.id==repeated.id and all(i['source']['block_id'] in {b.block_id for b in bundle.blocks} for i in result.inputs))
    bundle=await TableExtractor().extract_async('xlsx',1,xlsx(),XLSX)
    budget,rates=datasets_from_bundle('excel-e',bundle)
    result=compute(budget,ComputationSpec('sum','B4'))
    record('stale_cache_recomputed',result.result['value']=='1500.3' and result.formula_steps[0]['cache_status']=='stale')
    record('leaf_sources', {i['address'] for i in result.inputs}=={'B2','B3','B4'})
    result=compute(budget,ComputationSpec('sum','C2'),formula_cells={(d.name,c.address):c for d in (budget,rates) for c in d.cells})
    record('cross_sheet_exact','14.4012'==result.result['value'])
    styled=await TableExtractor().extract_async('styled',1,styled_xlsx(),XLSX)
    dataset=datasets_from_bundle('styled-e',styled)[0]
    result=compute(dataset,ComputationSpec('sum','E4'))
    record('percent_chain','600.05'==result.result['value'])
    refusals=[('cycle',ComputationSpec('sum','C6')),('zero_denominator',ComputationSpec('ratio','B2',reference='B6')),
        ('header',ComputationSpec('sum','B1')),('blank',ComputationSpec('sum','D7')),('unextracted',ComputationSpec('sum','X1')),
        ('foreign_sheet_missing',ComputationSpec('sum','C2')),('merged',ComputationSpec('sum','B5'))]
    for name,spec in refusals:
        try: compute(budget,spec); passed=False
        except MaterialError: passed=True
        record('refusal:'+name,passed)
    for raw in ('1,234','1.234','03/04/2026','$12.50'):
        record('ambiguity:'+raw,normalize(raw).kind=='ambiguous')
    record('locale_ru_exact',normalize('1\u202f234,50',ColumnPolicy(0,locale='ru')).value=='1234.5')
    return dict(contract='dataset-evaluation-1',seed=20260930,scope='synthetic_development_and_seeded_holdout; not real-world accuracy',
        cases=records,total=len(records),passed=sum(r['passed'] for r in records),provider_calls=0)


def main():
    report=asyncio.run(evaluate())
    path=Path(__file__).resolve().parents[1]/'docs/evaluation/materials_datasets.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f"Dataset evaluation: {report['passed']}/{report['total']}; {path}")
    if report['passed']!=report['total']: raise SystemExit(1)


if __name__=='__main__': main()
