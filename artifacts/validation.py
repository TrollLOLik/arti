from dataclasses import asdict
from decimal import Decimal
from materials.types import EvidenceRef,Locator,MaterialError
from materials.dataset_repository import DatasetRepository
from materials.retrieval import verify_quote

def ref_from_dict(v): return EvidenceRef(**{**v,'locator':Locator.from_dict(v['locator'])})

async def validate_evidence(spec,actor,materials):
    refs=[]; inputs=set(); diagnostics=[]; repo=DatasetRepository(materials)
    for e in spec.value['elements']+spec.value.get('relations',[]):
        p=e.get('proof')
        if not p:
            diagnostics.append(dict(id=e['id'],support='unverified')); continue
        if set(p)-{'kind','dataset_id','address','computation_id','source','quote','observation_id','segment_id'}: raise MaterialError('artifact_proof_invalid')
        if p['kind']=='quote':
            ref=ref_from_dict(p['source']); quote=p['quote']
            # A supported claim must be the literal source wording, not an unchecked paraphrase.
            if e.get('text',e.get('label',''))!=quote: raise MaterialError('artifact_claim_quote_mismatch')
            diagnostic=await verify_quote(materials,actor,ref,quote,observation_id=p.get('observation_id'),segment_id=p.get('segment_id'))
            refs.append(asdict(ref)); diagnostics.append(dict(id=e['id'],**diagnostic))
            if p.get('observation_id'): inputs.add(p['observation_id'])
        elif p['kind'] in ('dataset','computation'):
            if 'quantity' not in e: raise MaterialError('artifact_numeric_quantity_required')
            if p['kind']=='dataset':
                data=await repo.load_dataset(p['dataset_id'],actor); cell=data.cell(p['address']); value=cell.normalized
                if cell.formula or value.kind!='number' or cell.quality=='unreadable': raise MaterialError('artifact_cell_requires_computation')
                expected=dict(value=value.value,lower=value.lower or value.value,upper=value.upper or value.value,unit=value.unit)
                refs.append(asdict(cell.source)); inputs.add(data.id)
            else:
                data=await repo.load_computation(p['computation_id'],actor); expected=data['result']; refs.extend(i['source'] for i in data['inputs']); inputs.add(p['computation_id'])
            q=e['quantity']
            if q['unit']!=expected.get('unit') or any(Decimal(q.get(k,q['value']))!=Decimal(str(expected.get(k,expected['value']))) for k in ('value','lower','upper')): raise MaterialError('artifact_quantity_mismatch')
            diagnostics.append(dict(id=e['id'],support='verified_numeric',uncertainty=q.get('lower',q['value'])!=q.get('upper',q['value'])))
        else: raise MaterialError('artifact_proof_invalid')
    from materials.derivatives import DerivativeRepository
    for id in spec.value.get('illustrations',[]):
        body=await DerivativeRepository(materials).load(id,actor,'illustration')
        if body.get('role')!='decoration_not_evidence': raise MaterialError('artifact_illustration_role')
        inputs.add(id)
    return refs,sorted(inputs),diagnostics
