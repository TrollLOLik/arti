"""One measured scene for SVG, PNG and PDF; no generative typography."""
from dataclasses import dataclass,field
from pathlib import Path
from decimal import Decimal
from PIL import ImageFont
from artifacts.styles import StyleProfile
from artifacts.spec import number
from materials.types import MaterialError

FONT=Path(__file__).parent/'fonts'/'DejaVuSans.ttf'
WIDTH,HEIGHT=1080,1440
STATUS={'observed':'Наблюдение','confirmed':'Подтверждено источником','proposed':'Предложение','unknown':'Данных нет','fiction':'Художественный вымысел'}
RELATION={'sequence':'следует','dependency':'зависит','contrasts':'сравнение','supports':'поддерживает','objects':'возражает','part_of':'часть','correlates':'корреляция','claimed_cause':'причина по утверждению источника','illustrates':'иллюстрирует'}

@dataclass
class Page:
    items:list=field(default_factory=list)
    boxes:list=field(default_factory=list)
    elements:list=field(default_factory=list)

def wrap(text,width,size):
    font=ImageFont.truetype(str(FONT),size); lines=[]
    for para in str(text).split('\n'):
        line=''
        for word in para.split():
            # Even long URLs/identifiers get measured character wrapping.
            for chunk in [word]:
                if line and font.getlength(line+' '+chunk)>width: lines.append(line); line=''
                while font.getlength(chunk)>width:
                    end=1
                    while end<len(chunk) and font.getlength(chunk[:end+1])<=width: end+=1
                    if line: lines.append(line); line=''
                    lines.append(chunk[:end]); chunk=chunk[end:]
                line=(line+' '+chunk).strip()
        lines.append(line)
    return lines

def scene(spec):
    style=StyleProfile.from_dict(spec.value.get('style',{})); pages=[]; page=None; y=0
    def text(x,y,t,size=24,color=None,*,owner=None,panel=None):
        page.items.append(dict(kind='text',x=x,y=y,text=t,size=size,color=color or style.foreground,owner=owner,panel=panel))
    def new_page():
        nonlocal page,y
        page=Page(); pages.append(page)
        for i,line in enumerate(wrap(spec.value['title'],960,36)): text(60,64+i*44,line,36,owner='__title__')
        y=80+len(wrap(spec.value['title'],960,36))*44
        text(60,HEIGHT-48,f"АРТИ / {spec.value['format']} / {len(pages)}",18,style.muted)
    def block(id,lines,*,chart=None,element=True):
        nonlocal y
        padding=32 if style.density=='compact' else 40; gap=10 if style.density=='compact' else 18
        # Long elements continue on another page rather than shrinking text.
        offset=0; first=True
        while offset<len(lines):
            available=int((HEIGHT-110-y-padding)/32)
            if available<3: new_page(); available=int((HEIGHT-110-y-padding)/32)
            chunk=lines[offset:offset+available]; h=32*len(chunk)+padding
            page.boxes.append((60,y,960,h))
            if element: page.elements.append(id)
            page.items.append(dict(kind='rect',x=60,y=y,w=960,h=h,color=style.background,stroke=style.accent))
            for i,(line,size,color) in enumerate(chunk): text(80,y+padding/2+i*32,line,size,color,owner=id if element else None,panel=(60,y,960,h))
            y+=h+gap; offset+=len(chunk); first=False
        if chart:
            if y+100>HEIGHT-110: new_page()
            lo,hi,val,low,high,ticklo,tickhi=chart; span=hi-lo
            x=lambda a: 80+float((a-lo)/span)*920
            zero=Decimal(0) if lo<=0<=hi else lo
            page.items.append(dict(kind='line',x=80,y=y+30,x2=1000,y2=y+30,color=style.muted,width=2))
            scale=spec.value.get('axis',{}).get('scale','linear')
            if scale=='linear':
                page.items.append(dict(kind='line',x=x(zero),y=y+14,x2=x(zero),y2=y+46,color=style.foreground,width=1))
                if abs(x(val)-x(zero))>=1: page.items.append(dict(kind='rect',x=min(x(zero),x(val)),y=y+18,w=abs(x(val)-x(zero)),h=24,color=style.accent,stroke=style.accent))
            else: page.items.append(dict(kind='rect',x=x(val)-4,y=y+17,w=8,h=26,color=style.accent,stroke=style.accent))
            if low!=high: page.items.append(dict(kind='line',x=x(low),y=y+12,x2=x(high),y2=y+12,color=style.foreground,width=4))
            text(80,y+53,str(ticklo),18,style.muted); text(800,y+53,str(tickhi),18,style.muted); y+=95
    new_page(); elements=spec.value['elements']
    # Overview preserves IDs while the following cards carry full text/evidence.
    if spec.value['format'] in ('process','arguments','teaching','roadmap'):
        relations=spec.value.get('relations',[])
        begin=0
        while begin<len(elements):
            if begin: new_page()
            # Actual title height determines how many overview rows fit. Never
            # shrink labels merely to retain a fixed six-card template.
            rows=max(1,min(3,int((HEIGHT-110-y-48)/245)))
            group=elements[begin:begin+rows*2]; positions={}
            text(60,y,'Карта структуры / типы связей и подробности далее',24,style.muted); y+=48
            top=y
            for i,e in enumerate(group):
                x=60+(i%2)*500; yy=top+(i//2)*245; positions[e['id']]=(x,yy)
                page.items.append(dict(kind='rect',x=x,y=yy,w=460,h=205,color=style.background,stroke=style.accent)); page.boxes.append((x,yy,460,205))
                for j,l in enumerate(wrap(e['id']+' / '+e['label'],420,24)[:4]): text(x+20,yy+20+j*30,l,24,style.accent)
                text(x+20,yy+160,STATUS[e['status']],18,style.muted)
            y=top+((len(group)+1)//2)*245
            for r in relations:
                if r['from'] in positions and r['to'] in positions:
                    a,b=positions[r['from']],positions[r['to']]
                    # Route around card interiors, including links between columns.
                    cross=a[0]!=b[0]; x=540 if cross else (45 if a[0]==60 else 1035)
                    ax=a[0]+460 if (cross and a[0]==60) or (not cross and a[0]!=60) else a[0]
                    bx=b[0]+460 if (cross and b[0]==60) or (not cross and b[0]!=60) else b[0]
                    y1=a[1]+102; y2=b[1]+102
                    page.items.extend([dict(kind='line',x=ax,y=y1,x2=x,y2=y1,color=style.muted,width=1),dict(kind='line',x=x,y=y1,x2=x,y2=y2,color=style.muted,width=1),dict(kind='line',x=x,y=y2,x2=bx,y2=y2,color=style.muted,width=1)])
                    if r['kind'] not in ('contrasts','correlates'):
                        back=bx-7 if bx>x else bx+7
                        page.items.extend([dict(kind='line',x=back,y=y2-4,x2=bx,y2=y2,color=style.foreground,width=2),dict(kind='line',x=back,y=y2+4,x2=bx,y2=y2,color=style.foreground,width=2)])
            begin+=len(group)
        new_page()
    if spec.value['format'] in ('table','comparison'):
        text(60,y,'Сравнение / статус / значение',24,style.muted); y+=48
        for e in elements:
            left=wrap(e['label'],390,22); q=e.get('quantity'); right=wrap((q['value']+' '+q['unit'] if q else 'Данных нет')+' / '+STATUS[e['status']],490,22); h=max(len(left),len(right))*30+30
            if y+h>HEIGHT-110: new_page()
            page.items.append(dict(kind='rect',x=60,y=y,w=960,h=h,color=style.background,stroke=style.accent)); page.boxes.append((60,y,960,h))
            for j,l in enumerate(left): text(80,y+15+j*30,l,22)
            for j,l in enumerate(right): text(500,y+15+j*30,l,22,style.muted)
            y+=h+8
        new_page()
    if spec.value['format'] in ('timeline','roadmap'): elements=sorted(elements,key=lambda e:e['order'])
    numeric=[e['quantity'] for e in elements if e.get('quantity')]; chart_bounds=None
    if spec.value['format']=='statistical':
        axis=spec.value.get('axis',{}); lo=min(Decimal(0),*(number(q.get('lower',q['value'])) for q in numeric)); hi=max(Decimal(0),*(number(q.get('upper',q['value'])) for q in numeric))
        if axis.get('scale') in ('log','broken'):
            # Explicit nonproportional axes use labelled points, never bar lengths.
            lo=min(number(q.get('lower',q['value'])) for q in numeric)
        lo=number(axis['minimum']) if 'minimum' in axis else lo; hi=number(axis['maximum']) if 'maximum' in axis else hi
        if hi<=lo: hi=lo+1
        if any(number(q.get('lower',q['value']))<lo or number(q.get('upper',q['value']))>hi for q in numeric): raise MaterialError('artifact_axis_clips_data')
        chart_ticks=(lo,hi); chart_bounds=(lo,hi)
        if axis.get('scale')=='log': chart_bounds=(lo.ln(),hi.ln())
        disclosure=axis.get('disclosure') or ('Логарифмическая шкала; метки значений точные' if axis.get('scale')=='log' else 'Общая линейная шкала; интервалы из источника')
        if axis.get('scale','linear')=='linear' and any(0<abs(number(q['value']))/(hi-lo)*920<1 for q in numeric): disclosure+='; величины меньше пикселя показаны точной подписью'
        block('axis',[(line,22,style.muted) for line in wrap(disclosure,920,22)],element=False)
    for n,e in enumerate(elements):
        lines=[(l,28,style.accent) for l in wrap(f"{n+1:02d}  {e['label']}",920,28)]
        lines.append((STATUS[e['status']],20,style.muted))
        if 'when' in e: lines.append((e['when'],22,style.foreground))
        lines.extend((l,24,style.foreground) for l in wrap(e.get('text',''),920,24))
        chart=None; q=e.get('quantity')
        if q:
            display=f"{q['value']} {q['unit']}"
            if q.get('lower',q['value'])!=q.get('upper',q['value']): display+=f"   [{q.get('lower',q['value'])}; {q.get('upper',q['value'])}]"
            lines.extend((l,28,style.foreground) for l in wrap(display,920,28))
            if chart_bounds:
                values=[number(q[k] if k in q else q['value']) for k in ('value','lower','upper')]
                if spec.value.get('axis',{}).get('scale')=='log': values=[v.ln() for v in values]
                chart=(*chart_bounds,*values,*chart_ticks)
        p=e.get('proof')
        if p:
            source=p.get('source',{}); label=(source.get('asset_id','')[:12]+' / '+source.get('block_id','')) if p['kind']=='quote' else (p.get('dataset_id',p.get('computation_id',''))[:12]+' / '+p.get('address','расчёт'))
            lines.extend((l,18,style.muted) for l in wrap('Источник: '+label,920,18))
        block(e['id'],lines,chart=chart)
    for r in spec.value.get('relations',[]):
        block(r['id'],[(l,22,style.muted) for l in wrap(f"{r['from']} → {r['to']}: {RELATION[r['kind']]} {r.get('label','')}",920,22)],element=False)
    for question in spec.value.get('questions',[]): block('question',[(l,22,style.muted) for l in wrap('Открытый вопрос: '+question,920,22)],element=False)
    check_layout(pages,spec)
    return pages,style

def check_layout(pages,spec):
    if len(pages)>40: raise MaterialError('artifact_page_budget')
    seen=set(); content={}; fonts={}
    compact=lambda value: ''.join(str(value).split())
    for p in pages:
        seen.update(p.elements)
        for i,(x,y,w,h) in enumerate(p.boxes):
            if x<0 or y<0 or x+w>WIDTH or y+h>HEIGHT-95: raise MaterialError('artifact_overflow')
            if any(x<a+c and x+w>a and y<b+d and y+h>b for a,b,c,d in p.boxes[:i]): raise MaterialError('artifact_overlap')
        text_bounds=[]
        for item in p.items:
            if item['kind']!='text': continue
            size=item['size']
            if size<18: raise MaterialError('artifact_text_too_small')
            if size not in fonts: fonts[size]=ImageFont.truetype(str(FONT),size)
            font=fonts[size]
            x,y=item['x'],item['y']; left,top,right,bottom=font.getbbox(item['text'],anchor='lt')
            bounds=(x+left,y+top,x+right,y+bottom)
            if bounds[0]<0 or bounds[1]<0 or bounds[2]>WIDTH-30 or bounds[3]>HEIGHT-20:
                raise MaterialError('artifact_text_overflow')
            panel=item.get('panel')
            if panel:
                a,b,w,h=panel
                if bounds[0]<a or bounds[1]<b or bounds[2]>a+w or bounds[3]>b+h:
                    raise MaterialError('artifact_text_outside_panel')
            if compact(item['text']):
                if any(bounds[0]<r and bounds[2]>l and bounds[1]<b and bounds[3]>t for l,t,r,b in text_bounds):
                    raise MaterialError('artifact_text_overlap')
                text_bounds.append(bounds)
            owner=item.get('owner')
            if owner: content.setdefault(owner,[]).append(compact(item['text']))
    if not {e['id'] for e in spec.value['elements']}<=seen: raise MaterialError('artifact_element_omitted')
    if compact(spec.value['title']) not in ''.join(content.get('__title__',[])):
        raise MaterialError('artifact_content_omitted')
    for e in spec.value['elements']:
        rendered=''.join(content.get(e['id'],[]))
        required=[e['label'],e.get('text',''),STATUS[e['status']],e.get('when','')]
        quantity=e.get('quantity')
        if quantity: required.extend(quantity.get(k,'') for k in ('value','unit','lower','upper'))
        if any(compact(value) not in rendered for value in required):
            raise MaterialError('artifact_content_omitted')
