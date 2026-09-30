"""Strict visual observations, separate from OCR and confirmed numeric facts."""
from dataclasses import asdict,dataclass
import json
import re
from materials.types import ContentBlock,Locator,MaterialError,block_id

VISUAL_VERSION='visual-observation-1'
OBJECT_KINDS={'object','node','label','legend','axis','annotation','decoration','data_mark'}
RELATIONS={'points_to','connects','contains','left_of','above','overlaps','label_for','illustrates'}


@dataclass(frozen=True)
class VisualObject:
    id: str
    kind: str
    label: str
    bbox: tuple[float,float,float,float]
    axis_scale: str='unknown'
    unit: str | None=None
    def __post_init__(self):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}',self.id) or self.kind not in OBJECT_KINDS or len(self.label)>2000 or self.axis_scale not in ('unknown','linear','log','broken','categorical'):
            raise MaterialError('invalid_visual_object')
        Locator('region',bbox=self.bbox)
        if self.unit is not None and (not isinstance(self.unit,str) or len(self.unit)>80): raise MaterialError('invalid_visual_unit')


@dataclass(frozen=True)
class VisualRelation:
    id: str
    source: str
    target: str
    kind: str
    label: str=''
    def __post_init__(self):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}',self.id) or self.kind not in RELATIONS or self.source==self.target or len(self.label)>1000:
            raise MaterialError('invalid_visual_relation')


def parse_observations(text):
    if not isinstance(text,str) or len(text)>100000: raise MaterialError('visual_output_budget')
    text=text.strip()
    if text.startswith('```'):
        text=re.sub(r'^```(?:json)?\s*|\s*```$','',text)
    try:
        value=json.loads(text)
        if not isinstance(value,dict) or set(value)-{'summary','objects','relations','limitations'}: raise ValueError()
        if not isinstance(value.get('summary',''),str) or len(value.get('summary',''))>6000: raise ValueError()
        objects=tuple(VisualObject(**{**o,'bbox':tuple(o['bbox'])}) for o in value.get('objects',()))
        relations=tuple(VisualRelation(**r) for r in value.get('relations',()))
        ids={o.id for o in objects}
        if len(objects)>128 or len(relations)>256 or len(ids)!=len(objects) or len({r.id for r in relations})!=len(relations) or any(r.source not in ids or r.target not in ids for r in relations): raise ValueError()
        limitations=value.get('limitations',[])
        if not isinstance(limitations,list) or len(limitations)>20 or any(not isinstance(s,str) or len(s)>300 for s in limitations): raise ValueError()
        return dict(summary=value.get('summary',''),objects=objects,relations=relations,limitations=tuple(limitations))
    except (ValueError,TypeError,KeyError) as exc: raise MaterialError('invalid_visual_schema') from exc


def observation_blocks(aid,version,observations,parent,ordinal,*,method,outer=(0,0,1,1)):
    from materials.regions import map_box
    blocks=[]; ids={}
    for obj in observations['objects']:
        locator=Locator('region',bbox=map_box(obj.bbox,outer)); bid=block_id(aid,version,'image',locator,ordinal+len(blocks)); ids[obj.id]=bid
        blocks.append(ContentBlock(bid,'image',locator,obj.label,parent,ordinal+len(blocks),'observed','uncertain',
            ('model_visual_observation_not_verified','numeric_labels_require_source_review'),
            dict(role='visual_object',object={**asdict(obj),'bbox':list(obj.bbox)},method=method,contract=VISUAL_VERSION)))
    for relation in observations['relations']:
        objects={o.id:o for o in observations['objects']}; a,b=objects[relation.source].bbox,objects[relation.target].bbox
        box=(min(a[0],b[0]),min(a[1],b[1]),max(a[2],b[2]),max(a[3],b[3]))
        locator=Locator('region',bbox=map_box(box,outer)); index=ordinal+len(blocks)
        blocks.append(ContentBlock(block_id(aid,version,'image',locator,index),'image',locator,relation.label,parent,index,'interpreted','uncertain',
            ('spatial_relation_not_causality',),dict(role='visual_relation',relation=asdict(relation),source_block=ids[relation.source],target_block=ids[relation.target],method=method)))
    return tuple(blocks)


def compare_labels(observations,document_facts):
    """Keep disagreements as alternatives; do not overwrite either source."""
    conflicts=[]
    for observed in observations:
        for documented in document_facts:
            if observed['key']==documented['key'] and observed['value']!=documented['value']:
                conflicts.append(dict(key=observed['key'],status='unresolved',alternatives=(observed,documented),resolution=None))
    return tuple(conflicts)
