from artifacts.spec import ArtifactSpec
from materials.types import MaterialError

def patch(spec,operations):
    if not isinstance(operations,list) or not 1<=len(operations)<=30: raise MaterialError('artifact_patch_budget')
    value=spec.to_dict()
    for op in operations:
        if set(op)-{'op','id','value'}: raise MaterialError('artifact_patch_invalid')
        if op['op'] in ('style','format','title','illustrations'):
            value[op['op']]=op['value']
        elif op['op'] in ('replace','remove'):
            targets=[e for e in value['elements'] if e['id']==op.get('id')]
            if len(targets)!=1: raise MaterialError('artifact_patch_target')
            i=value['elements'].index(targets[0])
            if op['op']=='replace':
                if op['value'].get('id')!=op['id']: raise MaterialError('artifact_stable_id_required')
                value['elements'][i]=op['value']
            else:
                value['elements'].pop(i); value['relations']=[r for r in value.get('relations',[]) if op['id'] not in (r['from'],r['to'])]
        else: raise MaterialError('artifact_patch_invalid')
    revised=ArtifactSpec(value)
    return revised,dict(content_changed=spec.factual_hash!=revised.factual_hash,style_changed=spec.value.get('style')!=revised.value.get('style'),illustrations_changed=spec.value.get('illustrations')!=revised.value.get('illustrations'))
