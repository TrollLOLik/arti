"""Closed DAG protocol shared by native calls, explicit recipes and model planners."""
from copy import deepcopy
from materials.types import MaterialError,canonical
from agents.tools.registry import validate_schema

class Plan:
    def __init__(self,value,registry):
        self.value=deepcopy(value)
        if set(value)-{'goal','steps','checks','inputs','max_replans'} or not isinstance(value.get('goal'),str) or not 1<=len(value['goal'])<=4000 or not 1<=len(value.get('steps',[]))<=40 or len(canonical(value).encode())>500000 or not 0<=value.get('max_replans',2)<=3: raise MaterialError('plan_invalid')
        seen=set()
        for s in value['steps']:
            if set(s)-{'id','tool','version','args','depends','grant_id'} or not isinstance(s.get('id'),str) or not s['id'].isalnum() or s['id'] in seen or not set(s.get('depends',[]))<=seen or len(s.get('depends',[]))>40 or not isinstance(s.get('args'),dict): raise MaterialError('plan_dependency_invalid')
            t=registry.get(s['tool'],s['version']); seen.add(s['id'])
            def check(v):
                if isinstance(v,dict):
                    if '$step' in v:
                        if set(v)-{'$step','path'} or v['$step'] not in s.get('depends',[]) or not isinstance(v.get('path',[]),list) or len(v.get('path',[]))>12: raise MaterialError('plan_reference_invalid')
                    else:
                        for x in v.values(): check(x)
                elif isinstance(v,list):
                    for x in v: check(x)
            check(s['args'])
            if '$step' not in canonical(s['args']): validate_schema(t.input_schema,s['args'])
        if not isinstance(value.get('checks',[]),list) or not 1<=len(value.get('checks',[]))<=50: raise MaterialError('plan_checks_invalid')
        if not isinstance(value.get('inputs',[]),list) or len(value.get('inputs',[]))>16 or any(not isinstance(i,str) for i in value.get('inputs',[])): raise MaterialError('plan_inputs_invalid')
        for c in value.get('checks',[]):
            if set(c)-{'step','path','op','value'} or c.get('step') not in seen or c.get('op') not in ('exists','equals','nonempty') or not isinstance(c.get('path'),list): raise MaterialError('plan_checks_invalid')
    def to_dict(self): return deepcopy(self.value)

def resolve_args(value,outputs):
    if isinstance(value,dict):
        if '$step' in value:
            result=outputs[value['$step']]
            try:
                for part in value.get('path',[]): result=result[part]
            except (KeyError,IndexError,TypeError): raise MaterialError('plan_output_missing') from None
            return deepcopy(result)
        return {k:resolve_args(v,outputs) for k,v in value.items()}
    if isinstance(value,list): return [resolve_args(v,outputs) for v in value]
    return value
