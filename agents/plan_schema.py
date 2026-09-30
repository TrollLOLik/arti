"""Model proposal shape deliberately excludes grant IDs and executable expressions."""
from agents.tools.registry import object_schema
IDENT=dict(type='string',pattern='^[a-zA-Z][a-zA-Z0-9]{0,63}$')
PATH=dict(type='array',maxItems=12,items=dict(type=['string','integer']))
STEP=object_schema(dict(id=IDENT,tool=dict(type='string'),version=dict(type='string'),args=dict(type='object'),depends=dict(type='array',maxItems=40,items=IDENT)),['id','tool','version','args','depends'])
CHECK=object_schema(dict(step=IDENT,path=PATH,op=dict(enum=['exists','equals','nonempty']),value={}),['step','path','op'])
PLAN_SCHEMA=object_schema(dict(goal=dict(type='string',minLength=1,maxLength=4000),steps=dict(type='array',minItems=1,maxItems=40,items=STEP),checks=dict(type='array',minItems=1,maxItems=50,items=CHECK),inputs=dict(type='array',maxItems=16,items=dict(type='string')),max_replans=dict(type='integer',minimum=0,maximum=3)),['goal','steps','checks'])
