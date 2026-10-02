"""Declarative work runtime: data operations only, no Python/eval, files or imports."""
from decimal import Decimal,localcontext
from materials.types import MaterialError,canonical

def run(program,inputs):
    if not isinstance(program,list) or not 1<=len(program)<=100 or len(canonical(inputs).encode())>1000000: raise MaterialError('runtime_budget')
    values=dict(inputs)
    for op in program:
        if set(op)-{'op','input','output','value'} or op.get('op') not in ('sum','count','filter_equal','sort','take') or op.get('input') not in values or not isinstance(op.get('output'),str) or len(op['output'])>64: raise MaterialError('runtime_operation_denied')
        source=values[op['input']]
        if not isinstance(source,list) or len(source)>4000: raise MaterialError('runtime_input_budget')
        if op['op']=='sum':
            from materials.datasets import decimal,decstr
            with localcontext() as ctx:
                ctx.prec=50; result=decstr(sum((decimal(x) for x in source),Decimal(0)))
        elif op['op']=='count': result=len(source)
        elif op['op']=='filter_equal': result=[x for x in source if x==op.get('value')]
        elif op['op']=='sort': result=sorted(source,key=str)
        else:
            if type(op.get('value')) is not int or not 0<=op['value']<=4000: raise MaterialError('runtime_budget')
            result=source[:op['value']]
        values[op['output']]=result
        if len(canonical(values).encode())>2000000: raise MaterialError('runtime_output_budget')
    return values

async def run_isolated(program,inputs,*,guard=None):
    from materials.extractors.isolation import run_worker,WorkerLimits
    if guard: await guard()
    result=await run_worker(dict(operation='declarative'),canonical(dict(program=program,inputs=inputs)).encode(),WorkerLimits(wall_seconds=15,memory_mb=512,disk_mb=8,output_mb=4))
    if guard: await guard()
    return result['values']
