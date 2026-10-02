"""Editable content contract. Source assertions never acquire decision authority."""
from copy import deepcopy
from hashlib import sha256
from decimal import Decimal,InvalidOperation
import re
from datetime import datetime,date,timezone
from materials.types import MaterialError,canonical

FORMATS={'comparison','timeline','process','roadmap','arguments','statistical','cards','table','teaching'}
STATUSES={'observed','confirmed','proposed','unknown','fiction'}
RELATIONS={'sequence','dependency','contrasts','supports','objects','part_of','correlates','claimed_cause','illustrates'}
ID=re.compile(r'^[a-zA-Z][a-zA-Z0-9_-]{0,63}$')

def number(value):
    if not isinstance(value,str) or len(value)>100: raise MaterialError('artifact_decimal_required')
    try: result=Decimal(value)
    except InvalidOperation: raise MaterialError('artifact_decimal_invalid') from None
    if not result.is_finite() or abs(result.adjusted())>100: raise MaterialError('artifact_decimal_budget')
    return result

class ArtifactSpec:
    def __init__(self,value):
        self.value=deepcopy(value); self.validate()
    def validate(self):
        v=self.value
        if set(v)-{'contract','title','format','elements','relations','axis','style','illustrations','questions'} or v.get('contract')!='artifact-1' or v.get('format') not in FORMATS: raise MaterialError('artifact_contract_invalid')
        if not isinstance(v.get('title'),str) or not 0<len(v['title'])<=300: raise MaterialError('artifact_title_invalid')
        elements=v.get('elements',[]); relations=v.get('relations',[])
        if not isinstance(elements,list) or not 1<=len(elements)<=120 or not isinstance(relations,list) or len(relations)>240: raise MaterialError('artifact_size_budget')
        ids=set()
        for e in elements:
            if set(e)-{'id','label','text','status','quantity','proof','order','series','when'} or not ID.fullmatch(e.get('id','')) or e['id'] in ids: raise MaterialError('artifact_element_invalid')
            ids.add(e['id'])
            if e.get('status') not in STATUSES or not isinstance(e.get('label'),str) or len(e['label'])>300 or not isinstance(e.get('text',''),str) or len(e.get('text',''))>2000: raise MaterialError('artifact_text_invalid')
            if e.get('status') in ('observed','confirmed') and not e.get('proof'): raise MaterialError('artifact_evidence_required')
            if v['format'] in ('timeline','roadmap') and (type(e.get('order')) is not int): raise MaterialError('artifact_order_required')
            if 'when' in e:
                value=e['when']
                try:
                    if not isinstance(value,str): raise ValueError()
                    if len(value)==10: date.fromisoformat(value)
                    elif datetime.fromisoformat(value).tzinfo is None: raise ValueError()
                except ValueError: raise MaterialError('artifact_time_invalid') from None
                if e['status'] in ('observed','confirmed') and (e.get('proof',{}).get('kind')!='quote' or value not in e['proof'].get('quote','')): raise MaterialError('artifact_time_evidence_required')
            if v['format']=='timeline' and e['status'] in ('observed','confirmed') and 'when' not in e: raise MaterialError('artifact_time_required')
            q=e.get('quantity')
            if q is not None:
                if set(q)-{'value','lower','upper','unit'} or not isinstance(q.get('unit'),str) or not q['unit'] or len(q['unit'])>40: raise MaterialError('artifact_unit_required')
                val=number(q['value']); lo=number(q.get('lower',q['value'])); hi=number(q.get('upper',q['value']))
                if not lo<=val<=hi or e['status'] in ('unknown','fiction') or e.get('proof',{}).get('kind') not in ('dataset','computation'): raise MaterialError('artifact_numeric_proof_required')
        for r in relations:
            if set(r)-{'id','from','to','kind','proof','label'} or not ID.fullmatch(r.get('id','')) or r['id'] in ids or r.get('from') not in ids or r.get('to') not in ids or r.get('kind') not in RELATIONS: raise MaterialError('artifact_relation_invalid')
            if not isinstance(r.get('label',''),str) or len(r.get('label',''))>2000: raise MaterialError('artifact_text_invalid')
            if r.get('kind')=='claimed_cause' and r.get('proof',{}).get('kind')!='quote': raise MaterialError('artifact_cause_evidence_required')
            ids.add(r['id'])
        # Relations may only target elements, never other relations.
        element_ids={e['id'] for e in elements}
        if any(r['from'] not in element_ids or r['to'] not in element_ids for r in relations): raise MaterialError('artifact_relation_endpoint')
        for r in relations:
            if r['kind']=='claimed_cause':
                quote=r['proof'].get('quote','').casefold()
                labels=[next(e['label'] for e in elements if e['id']==r[k]).casefold() for k in ('from','to')]
                if any(not label or label not in quote for label in labels) or not re.search(r'cause|because|lead.?to|потому|привод|обуслов|из-за|вызыва|влияет',quote): raise MaterialError('artifact_cause_claim_not_explicit')
        if v['format'] in ('timeline','roadmap') and len({e['order'] for e in elements})!=len(elements): raise MaterialError('artifact_order_ambiguous')
        if v['format'] in ('timeline','roadmap'):
            timed=[e for e in sorted(elements,key=lambda e:e['order']) if 'when' in e]
            if len({len(e['when'])==10 for e in timed})>1: raise MaterialError('artifact_time_precision_ambiguous')
            instants=[date.fromisoformat(e['when']) if len(e['when'])==10 else datetime.fromisoformat(e['when']).astimezone(timezone.utc) for e in timed]
            if instants!=sorted(instants): raise MaterialError('artifact_chronology_invalid')
            order={e['id']:e['order'] for e in elements}
            if any(r['kind']=='sequence' and order[r['from']]>=order[r['to']] for r in relations): raise MaterialError('artifact_sequence_invalid')
        axis=v.get('axis',{})
        if v['format']=='statistical':
            values=[e for e in elements if e.get('quantity')]
            if not values or len({e['quantity']['unit'] for e in values})!=1 or axis.get('unit')!=values[0]['quantity']['unit']: raise MaterialError('artifact_axis_unit')
            if set(axis)-{'scale','unit','minimum','maximum','disclosure'} or axis.get('scale','linear') not in ('linear','log'): raise MaterialError('artifact_axis_invalid')
            if axis.get('scale')=='log' and any(number(e['quantity'].get('lower',e['quantity']['value']))<=0 for e in values): raise MaterialError('artifact_log_domain')
            if axis.get('scale')=='broken' and not axis.get('disclosure'): raise MaterialError('artifact_axis_disclosure')
            if 'minimum' in axis and number(axis['minimum'])!=0 and not axis.get('disclosure'): raise MaterialError('artifact_axis_disclosure')
            lower=min(number(e['quantity'].get('lower',e['quantity']['value'])) for e in values)
            upper=max(number(e['quantity'].get('upper',e['quantity']['value'])) for e in values)
            if ('minimum' in axis and number(axis['minimum'])>lower) or ('maximum' in axis and number(axis['maximum'])<upper): raise MaterialError('artifact_axis_clips_data')
            if 'minimum' in axis and 'maximum' in axis and number(axis['minimum'])>=number(axis['maximum']): raise MaterialError('artifact_axis_invalid')
        from artifacts.styles import StyleProfile
        StyleProfile.from_dict(v.get('style',{}))
        illustrations=v.get('illustrations',[])
        if not isinstance(illustrations,list) or len(illustrations)>4 or any(not isinstance(x,str) or len(x)!=64 for x in illustrations): raise MaterialError('artifact_illustration_invalid')
        if not isinstance(v.get('questions',[]),list) or len(v.get('questions',[]))>12 or any(not isinstance(x,str) or len(x)>1000 for x in v.get('questions',[])): raise MaterialError('artifact_questions_invalid')
        if len(canonical(v).encode())>500000: raise MaterialError('artifact_payload_budget')
    @property
    def factual_hash(self): return sha256(canonical({k:v for k,v in self.value.items() if k not in ('style','illustrations')}).encode()).hexdigest()
    def to_dict(self): return deepcopy(self.value)

def choose_format(intent):
    return {'compare':'comparison','dates':'timeline','steps':'process','plan':'roadmap','debate':'arguments','numbers':'statistical','learn':'teaching'}.get(intent,'cards')
