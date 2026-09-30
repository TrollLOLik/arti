from materials.types import MaterialError

def verify(plan,outputs):
    missing=[]
    for check in plan.get('checks',[]):
        try:
            value=outputs[check['step']]
            for part in check['path']: value=value[part]
            good=check['op']=='exists' or (check['op']=='equals' and value==check.get('value')) or (check['op']=='nonempty' and bool(value))
        except (KeyError,IndexError,TypeError): good=False
        if not good: missing.append(check)
    return dict(complete=not missing,missing=missing,basis='deterministic_tool_outputs')
