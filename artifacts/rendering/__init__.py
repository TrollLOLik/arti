"""Measured editorial layouts shared by SVG, PNG and PDF.

All wrapping and pagination happens here, not in individual exporters. Labels,
source identifiers, uncertainty and status are text, never color-only signals.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
import math
from PIL import ImageFont
from artifacts.styles import StyleProfile, semantic_theme, contrast
from artifacts.spec import number
from materials.types import MaterialError, canonical

FONT = Path(__file__).parent / 'fonts' / 'DejaVuSans.ttf'
WIDTH, HEIGHT = 1080, 1440
BOTTOM = HEIGHT - 118
STATUS = {'observed': 'Наблюдение', 'confirmed': 'Подтверждено источником',
          'proposed': 'Предложение', 'unknown': 'Данных нет', 'fiction': 'Художественный вымысел'}
RELATION = {'sequence': 'следует', 'dependency': 'зависит', 'contrasts': 'сравнение',
            'supports': 'поддерживает', 'objects': 'возражает', 'part_of': 'часть',
            'correlates': 'корреляция', 'claimed_cause': 'причина по утверждению источника',
            'illustrates': 'иллюстрирует'}
FORMATS = {'cards': ('КАРТОЧКИ', 'Главное и подтверждения'),
           'comparison': ('СРАВНЕНИЕ', 'Варианты рядом · единые правила чтения'),
           'timeline': ('ХРОНОЛОГИЯ', 'Порядок событий · расстояния не обозначают длительность'),
           'process': ('ПРОЦЕСС', 'Шаги и явно заданные связи'),
           'roadmap': ('ДОРОЖНАЯ КАРТА', 'План по этапам · статус каждого шага указан отдельно'),
           'arguments': ('АРГУМЕНТЫ', 'Позиции, основания и типы связей'),
           'statistical': ('ДАННЫЕ', 'Значения, интервалы и источники'),
           'table': ('ТАБЛИЦА', 'Структурированный обзор с полной детализацией'),
           'teaching': ('УЧЕБНАЯ КАРТА', 'Материал по разделам · вопросы отдельно')}


@lru_cache(maxsize=32)
def font_for(size):
    return ImageFont.truetype(str(FONT), size)


@dataclass
class Page:
    items: list = field(default_factory=list)
    boxes: list = field(default_factory=list)
    elements: list = field(default_factory=list)


def wrap(text, width, size):
    font = font_for(size)
    lines = []
    for para in str(text).split('\n'):
        line = ''
        for word in para.split():
            if line and font.getlength(line + ' ' + word, features=['-kern','-liga']) > width:
                lines.append(line)
                line = ''
            while font.getlength(word, features=['-kern','-liga']) > width:
                # Binary search bounds long identifiers without quadratic work.
                lo, hi = 1, len(word)
                while lo < hi:
                    middle = (lo + hi + 1) // 2
                    if font.getlength(word[:middle], features=['-kern','-liga']) <= width:
                        lo = middle
                    else:
                        hi = middle - 1
                if line:
                    lines.append(line)
                    line = ''
                lines.append(word[:lo])
                word = word[lo:]
            line = (line + ' ' + word).strip()
        lines.append(line)
    return lines


def source_lines(proof):
    """Full source addresses, including revision and locator, without elision."""
    if not proof:
        return []
    if proof.get('kind') == 'quote':
        source = proof.get('source', {})
        parts = [str(source[k]) for k in ('asset_id', 'block_id') if source.get(k) is not None]
        for key, label in (('asset_version', 'версия'), ('asset_revision', 'версия'), ('revision', 'версия'),
                           ('extraction_id', 'извлечение'), ('extraction_hash', 'извлечение')):
            if source.get(key) is not None:
                parts.append(f'{label}: {source[key]}')
        locator = source.get('locator', {})
        for key, label in (('kind', 'тип'), ('page', 'страница'), ('paragraph', 'абзац'),
                           ('sheet', 'лист'), ('cell', 'ячейка'), ('start_ms', 'начало, мс'),
                           ('end_ms', 'конец, мс'), ('bbox', 'область')):
            if locator.get(key) is not None:
                parts.append(f'{label}: {locator[key]}')
    else:
        parts = [str(proof[k]) for k in ('dataset_id', 'computation_id', 'address') if proof.get(k) is not None]
    for key, label in (('observation_id', 'наблюдение'), ('segment_id', 'сегмент')):
        if proof.get(key):
            parts.append(f'{label}: {proof[key]}')
    return ['Источник: ' + ' / '.join(parts)]


def quantity_text(quantity):
    text = f"{quantity['value']} {quantity['unit']}"
    low, high = quantity.get('lower', quantity['value']), quantity.get('upper', quantity['value'])
    if low != high:
        text += f'   [{low}; {high}]'
    return text


class _Layout:
    def __init__(self, spec):
        self.spec = spec
        self.style = StyleProfile.from_dict(spec.value.get('style', {}))
        self.theme = semantic_theme(self.style)
        self.pages = []
        self.sources = {}
        self.page = None
        self.y = 0
        self.format = spec.value['format']
        self.gap = 18 if self.style.density == 'compact' else 24
        self.new_page()

    def text(self, x, y, value, size=24, role='text', *, owner=None, panel=None, fill=None):
        self.page.items.append(dict(kind='text', x=x, y=y, text=str(value), size=size,
                                    color=self.theme[role], owner=owner, panel=panel,
                                    background=fill or self.theme['background']))

    def rect(self, x, y, w, h, role='surface', radius=16, stroke=None, **extra):
        self.page.items.append(dict(kind='rect', x=x, y=y, w=w, h=h, radius=radius,
                                    color=self.theme[role], stroke=stroke or self.theme[role], **extra))

    def line(self, x, y, x2, y2, role='border', width=2, **extra):
        self.page.items.append(dict(kind='line', x=x, y=y, x2=x2, y2=y2,
                                    color=self.theme[role], width=width, **extra))

    def circle(self, x, y, radius, role='heading'):
        self.page.items.append(dict(kind='circle', x=x-radius, y=y-radius,
                                    w=radius*2, h=radius*2, color=self.theme[role], stroke=self.theme[role]))

    def new_page(self):
        self.page = Page()
        self.pages.append(self.page)
        self.rect(60, 44, 8, 25, 'marker', radius=4)
        self.text(84, 46, 'АРТИ / ' + FORMATS[self.format][0], 18, 'heading')
        title_lines = wrap(self.spec.value['title'], 960, 42)
        for i, value in enumerate(title_lines):
            self.text(60, 98+i*52, value, 42, 'heading', owner='__title__')
        self.y = 108 + len(title_lines)*52
        for value in wrap(FORMATS[self.format][1], 960, 20):
            self.text(60, self.y, value, 20, 'secondary')
            self.y += 29
        self.line(60, self.y+9, 1020, self.y+9)
        self.y += 38

    def ensure(self, height):
        if self.y + height > BOTTOM:
            self.new_page()

    def lines(self, value, width, size=24, role='text', after=8):
        result = [(line, size, role, size+9) for line in wrap(value, width, size)]
        if result:
            line, size, role, leading = result[-1]
            result[-1] = (line, size, role, leading+after)
        return result

    def reference(self, proof):
        key=canonical(proof)
        if key not in self.sources:
            self.sources[key]=(f'S{len(self.sources)+1:02d}', proof)
        identifier=self.sources[key][0]
        source=proof.get('source',{})
        locator=source.get('locator',{})
        parts=['Источник '+identifier]
        for key,label in (('page','стр.'),('paragraph','абзац'),('sheet','лист'),('cell','ячейка')):
            if locator.get(key) is not None:
                parts.append(f'{label} {locator[key]}')
        if locator.get('start_ms') is not None:
            parts.append(f"{locator['start_ms']}–{locator.get('end_ms','?')} мс")
        if proof.get('address'):
            parts.append('ячейка '+str(proof['address']))
        if source.get('asset_version') is not None:
            parts.append('версия '+str(source['asset_version']))
        return ' · '.join(parts)

    def content(self, element, width, *, hero=False, label=True, include_code=True):
        rows = []
        if label:
            rows += self.lines(element['label'], width, 30, 'heading', 9)
        rows += self.lines(STATUS[element['status']], width, 20, 'secondary', 12)
        if element.get('when'):
            rows += self.lines(element['when'], width, 24, 'heading', 8)
        if element.get('series'):
            rows += self.lines('Серия: ' + str(element['series']), width, 20, 'secondary', 8)
        if element.get('quantity') and hero:
            rows += self.lines(quantity_text(element['quantity']), width, 38, 'heading', 12)
        if element.get('text'):
            rows += self.lines(element['text'], width, 24, 'text', 12)
        if element.get('quantity') and not hero:
            rows += self.lines(quantity_text(element['quantity']), width, 32, 'heading', 12)
        proof = element.get('proof')
        if proof and proof.get('quote') and proof['quote'] != element.get('text', ''):
            rows += self.lines('Цитата: ' + proof['quote'], width, 24, 'text', 12)
        if proof:
            rows += self.lines(self.reference(proof), width, 18, 'secondary', 4)
        if include_code and self.spec.value.get('relations'):
            rows += self.lines('Код: ' + element['id'], width, 18, 'secondary', 4)
        return rows

    def paint(self, x, y, w, h, rows, *, owner=None, tag=None, variant='card', continued=False):
        panel = (x, y, w, h)
        self.page.boxes.append(panel)
        if owner:
            self.page.elements.append(owner)
        fill = 'surface'
        self.rect(x, y, w, h, fill, radius=16 if variant != 'table' else 3)
        if variant == 'comparison':
            self.rect(x, y, w, 7, 'series', radius=3)
        elif variant == 'roadmap':
            self.rect(x, y, 8, h, 'series_alt', radius=4)
        elif variant == 'teaching':
            self.rect(x, y, w, 5, 'heading', radius=2)
        elif variant == 'arguments':
            self.rect(x+20, y+24, 6, h-48, 'series', radius=3)
        elif variant not in ('table', 'statistics'):
            self.rect(x, y+18, 6, h-36, 'series', radius=3)
        xx, yy = x+28, y+24
        if tag or continued:
            text = (tag or '') + (' / продолжение' if continued else '')
            self.text(xx, yy, text, 18, 'secondary', panel=panel, fill=self.theme[fill])
            yy += 32
        for value, size, role, leading in rows:
            self.text(xx, yy, value, size, role, owner=owner, panel=panel, fill=self.theme[fill])
            yy += leading

    @staticmethod
    def take(rows, available):
        height, count = 0, 0
        for row in rows:
            if height + row[3] > available:
                break
            height += row[3]
            count += 1
        return count, height

    def card(self, element, index, *, x=60, w=960, variant='card', hero=False, sidebar=None, show_tag=True):
        rows = self.content(element, w-56, hero=hero,include_code=show_tag)
        padding=80 if show_tag else 48
        offset = 0
        part = 0
        while offset < len(rows):
            full = sum(r[3] for r in rows[offset:])+padding
            self.ensure(min(full, 210))
            # Avoid splitting a card which would fit in full on the next page.
            if full > BOTTOM-self.y and full <= BOTTOM-self.header_height:
                self.new_page()
            count, height = self.take(rows[offset:], BOTTOM-self.y-padding)
            if not count:
                raise MaterialError('artifact_page_budget')
            h = height+padding
            top = self.y
            self.paint(x, top, w, h, rows[offset:offset+count], owner=element['id'],
                       tag=f'{index:02d}' if show_tag else None, variant=variant, continued=bool(part and show_tag))
            if sidebar:
                sidebar(top, h, part)
            self.y += h+self.gap
            offset += count
            part += 1
            if offset < len(rows):
                self.new_page()

    @property
    def header_height(self):
        return 146 + len(wrap(self.spec.value['title'], 960, 42))*52 + len(wrap(FORMATS[self.format][1],960,20))*29

    def columns(self, elements, *, variant='comparison', hero=True, start_index=1):
        for base in range(0, len(elements), 2):
            group = elements[base:base+2]
            rows = [self.content(e, 412, hero=hero) for e in group]
            offsets = [0]*len(group)
            part = 0
            while any(offsets[j] < len(rows[j]) for j in range(len(group))):
                remaining = max(sum(r[3] for r in rows[j][offsets[j]:]) for j in range(len(group)))+80
                self.ensure(min(remaining, 240))
                if remaining > BOTTOM-self.y and remaining <= BOTTOM-self.header_height:
                    self.new_page()
                chunks = [self.take(rows[j][offsets[j]:], BOTTOM-self.y-80) for j in range(len(group))]
                height = max(h for _, h in chunks)+80
                if not any(count for count, _ in chunks):
                    raise MaterialError('artifact_page_budget')
                for j, element in enumerate(group):
                    count, _ = chunks[j]
                    if not count:
                        continue
                    self.paint(60+j*492, self.y, 468, height,
                               rows[j][offsets[j]:offsets[j]+count], owner=element['id'],
                               tag=f'{base+j+start_index:02d}', variant=variant, continued=bool(part))
                    offsets[j] += count
                self.y += height+self.gap
                part += 1
                if any(offsets[j] < len(rows[j]) for j in range(len(group))):
                    self.new_page()

    def note(self, title, body, *, owner=None):
        rows = self.lines(title, 904, 24, 'heading', 8) + self.lines(body, 904, 20, 'secondary', 4)
        offset = 0
        while offset < len(rows):
            self.ensure(min(180,sum(r[3] for r in rows[offset:])+48))
            count, height = self.take(rows[offset:], BOTTOM-self.y-48)
            if not count:
                raise MaterialError('artifact_page_budget')
            self.paint(60, self.y, 960, height+48, rows[offset:offset+count], owner=owner, variant='teaching')
            self.y += height+48+self.gap
            offset += count
            if offset < len(rows):
                self.new_page()

    def process(self, elements):
        # Compact map plus full evidence cards. Width is stable for existing
        # integrations which recognize the six-node overview geometry.
        start = 0
        relations = self.spec.value.get('relations', [])
        while start < len(elements):
            if start:
                self.new_page()
            self.text(60, self.y, 'КАРТА ШАГОВ / полные тексты и источники далее', 20, 'secondary')
            self.y += 43
            row_count = max(1, min(3, int((BOTTOM-self.y)/245)))
            group = elements[start:start+row_count*2]
            positions = {}
            for j, element in enumerate(group):
                x, y = 60+(j % 2)*500, self.y+(j//2)*245
                self.rect(x, y, 460, 205, 'surface', radius=20)
                self.page.boxes.append((x,y,460,205))
                self.rect(x+22,y+22,44,38,'heading',radius=10)
                self.text(x+29,y+28,f'{start+j+1:02d}',20,'inverse',fill=self.theme['heading'])
                identifier = element['id']
                while font_for(18).getlength(identifier) > 330:
                    identifier = identifier[:-2]+'…'
                self.text(x+82,y+29,identifier,18,'secondary',fill=self.theme['surface'])
                labels = wrap(element['label'], 416, 25)
                labels = labels[:3]
                if len(wrap(element['label'],416,25)) > 3:
                    labels[-1] = labels[-1][:-2]+'…'
                for k, label in enumerate(labels):
                    self.text(x+22,y+76+k*31,label,25,'heading',fill=self.theme['surface'])
                self.text(x+22,y+174,STATUS[element['status']],18,'secondary',fill=self.theme['surface'])
                positions[element['id']] = (x,y)
            for relation in relations:
                if relation['from'] not in positions or relation['to'] not in positions:
                    continue
                a,b = positions[relation['from']],positions[relation['to']]
                cross = a[0] != b[0]
                lane = 540 if cross else (43 if a[0] == 60 else 1037)
                ax = a[0]+460 if (cross and a[0] == 60) or (not cross and a[0] != 60) else a[0]
                bx = b[0]+460 if (cross and b[0] == 60) or (not cross and b[0] != 60) else b[0]
                yy, end = a[1]+102,b[1]+102
                self.line(ax,yy,lane,yy,'heading',2)
                self.line(lane,yy,lane,end,'heading',2)
                self.line(lane,end,bx,end,'heading',2)
                if relation['kind'] not in ('contrasts','correlates'):
                    back = bx-7 if bx > lane else bx+7
                    self.line(back,end-5,bx,end,'heading',2)
                    self.line(back,end+5,bx,end,'heading',2)
            self.y += ((len(group)+1)//2)*245
            start += len(group)
        self.new_page()
        for i, element in enumerate(elements,1):
            self.card(element,i,variant='card')

    def chronology(self, elements, *, roadmap=False):
        for i, element in enumerate(elements, 1):
            def rail(top, height, part):
                if roadmap:
                    self.text(72,top+27,f'{i:02d}',48,'heading')
                    self.text(65,top+89,'ЭТАП',18,'secondary')
                    self.line(141,top+30,162,top+30,'series_alt',3)
                else:
                    self.line(110,top,110,top+height,'series_alt',3)
                    self.circle(110,top+39,24,'heading')
                    self.text(96,top+28,f'{i:02d}',20,'inverse',fill=self.theme['heading'])
                    self.line(134,top+39,164,top+39,'series_alt',3)
                    if part:
                        self.text(75,top+80,'далее',18,'secondary')
            self.card(element, i, x=180, w=840, variant='roadmap' if roadmap else 'timeline',sidebar=rail,show_tag=False)

    def table(self, elements):
        for i, element in enumerate(elements,1):
            left = self.lines(element['label'], 278, 26, 'heading', 12)
            left += self.lines(STATUS[element['status']],278,20,'secondary',8)
            right = self.content(element, 562, hero=True, label=False)
            # Status belongs to its explicit left-hand column, not duplicated.
            status_rows = len(self.lines(STATUS[element['status']],562,20,'secondary',12))
            right = right[status_rows:]
            if not right:
                right = self.lines('—',562,24,'secondary',0)
            chunks = [left,right]
            offsets = [0,0]
            part = 0
            while any(offsets[j]<len(chunks[j]) for j in (0,1)):
                self.ensure(230)
                if part == 0 or self.y == self.header_height:
                    self.text(80,self.y,'ЭЛЕМЕНТ / СТАТУС',18,'secondary')
                    self.text(442,self.y,'СОДЕРЖАНИЕ / ЗНАЧЕНИЕ / ИСТОЧНИК',18,'secondary')
                    self.y += 40
                count_heights = [self.take(chunks[j][offsets[j]:],BOTTOM-self.y-48) for j in (0,1)]
                height = max(h for _,h in count_heights)+48
                if not any(n for n,_ in count_heights):
                    raise MaterialError('artifact_page_budget')
                panel = (60,self.y,960,height)
                self.page.boxes.append(panel)
                self.page.elements.append(element['id'])
                self.rect(60,self.y,960,height,'surface',radius=3)
                self.line(418,self.y+18,418,self.y+height-18,'border',1)
                self.rect(60,self.y,5,height,'series' if i%2 else 'series_alt',radius=2)
                for j,x in enumerate((80,442)):
                    yy=self.y+22
                    count,_=count_heights[j]
                    for value,size,role,leading in chunks[j][offsets[j]:offsets[j]+count]:
                        self.text(x,yy,value,size,role,owner=element['id'],panel=panel,fill=self.theme['surface'])
                        self.page.items[-1]['flow']=str(j)
                        yy+=leading
                    offsets[j]+=count
                self.y += height+10
                part += 1
                if any(offsets[j]<len(chunks[j]) for j in (0,1)):
                    self.new_page()

    def statistics(self, elements):
        quantities=[e['quantity'] for e in elements if e.get('quantity')]
        axis=self.spec.value.get('axis',{})
        logarithmic=axis.get('scale')=='log'
        lower=min(number(q.get('lower',q['value'])) for q in quantities)
        upper=max(number(q.get('upper',q['value'])) for q in quantities)
        lo=number(axis['minimum']) if 'minimum' in axis else (lower if logarithmic else min(Decimal(0),lower))
        hi=number(axis['maximum']) if 'maximum' in axis else max(Decimal(0),upper)
        if hi<=lo:
            hi=lo*10 if logarithmic else lo+1
        if lower<lo or upper>hi:
            raise MaterialError('artifact_axis_clips_data')
        start,end=(lo.ln(),hi.ln()) if logarithmic else (lo,hi)
        span=end-start
        includes_zero=lo<=0<=hi
        disclosure=axis.get('disclosure') or ('Логарифмическая шкала; точки обозначают точные значения.' if logarithmic else 'Общая линейная шкала. Вертикальная отметка: ноль.')
        if not logarithmic and not includes_zero:
            disclosure+=' Ноль вне шкалы; значения показаны точками.'
        if any(q.get('lower',q['value'])!=q.get('upper',q['value']) for q in quantities):
            disclosure+=' Усики: интервал из источника.'
        if not logarithmic and any(0<abs(number(q['value']))/span*848<1 for q in quantities):
            disclosure+=' Значения меньше пикселя: точка и точная подпись.'
        for label in wrap(disclosure,960,20):
            self.ensure(40)
            self.text(60,self.y,label,20,'secondary')
            self.y+=29
        self.y+=16
        for i,element in enumerate(elements,1):
            if not element.get('quantity'):
                self.card(element,i)
                continue
            q=element['quantity']
            rows=self.lines(element['label'],904,28,'heading',4)
            metadata=STATUS[element['status']]
            if element.get('proof'):
                metadata+=' · '+self.reference(element['proof'])
            rows+=self.lines(metadata,904,20,'secondary',3)
            rows+=self.lines(quantity_text(q),904,36,'heading',4)
            if element.get('when'):
                rows+=self.lines(element['when'],904,24,'heading',4)
            if element.get('series'):
                rows+=self.lines('Серия: '+str(element['series']),904,20,'secondary',4)
            if element.get('text'):
                rows+=self.lines(element['text'],904,24,'text',4)
            proof=element.get('proof',{})
            if proof.get('quote') and proof['quote']!=element.get('text',''):
                rows+=self.lines('Цитата: '+proof['quote'],904,24,'text',4)
            left,right=str(lo),str(hi)
            tick_lines=max(len(wrap(left,390,18)),len(wrap(right,390,18)))
            chart_height=72+tick_lines*22
            full=sum(r[3] for r in rows)+48+chart_height
            self.ensure(min(full,260))
            if full>BOTTOM-self.y and full<=BOTTOM-self.header_height:
                self.new_page()
            offset=0
            # Exceptionally long commentary continues at the same readable size.
            while offset<len(rows):
                count,height=self.take(rows[offset:],BOTTOM-self.y-48-chart_height)
                if count==0:
                    self.new_page()
                    count,height=self.take(rows[offset:],BOTTOM-self.y-48-chart_height)
                if count==0:
                    raise MaterialError('artifact_page_budget')
                final=offset+count==len(rows)
                panel_height=height+48+(chart_height if final else 0)
                top=self.y
                self.paint(60,top,960,panel_height,rows[offset:offset+count],owner=element['id'],variant='statistics')
                self.y+=panel_height+self.gap
                offset+=count
                if not final:
                    self.new_page()
            panel=(60,top,960,panel_height)
            def point(value):
                value=number(value)
                transformed=value.ln() if logarithmic else value
                return 116+float((transformed-start)/span)*848
            value=point(q['value'])
            low=point(q.get('lower',q['value']))
            high=point(q.get('upper',q['value']))
            yy=top+height+55
            self.line(116,yy,964,yy,'border',2)
            if logarithmic or not includes_zero:
                self.circle(value,yy,7,'heading')
            else:
                baseline=point('0')
                self.line(baseline,yy-20,baseline,yy+19,'secondary',2)
                distance=abs(value-baseline)
                if distance>=1:
                    self.rect(min(value,baseline),yy-10,distance,20,'series' if i%2 else 'series_alt',radius=0,
                              semantic='value_bar',value=q['value'],baseline=baseline)
                else:
                    self.circle(value,yy,4,'heading')
            if low!=high:
                self.line(low,yy-22,high,yy-22,'heading',3,semantic='interval')
                self.line(low,yy-28,low,yy-16,'heading',2)
                self.line(high,yy-28,high,yy-16,'heading',2)
            self.circle(value,yy,4,'heading')
            for k,label in enumerate(wrap(left,390,18)):
                self.text(116,yy+30+k*22,label,18,'secondary',panel=panel,fill=self.theme['surface'])
            for k,label in enumerate(wrap(right,390,18)):
                self.text(964-font_for(18).getlength(label),yy+30+k*22,label,18,'secondary',panel=panel,fill=self.theme['surface'])
            if not logarithmic and lo<0<hi:
                zero=point('0')
                left_end=116+max(font_for(18).getlength(t) for t in wrap(left,390,18))
                right_start=964-max(font_for(18).getlength(t) for t in wrap(right,390,18))
                zero_width=font_for(18).getlength('0')
                if zero-zero_width/2>left_end+18 and zero+zero_width/2<right_start-18:
                    self.text(zero-zero_width/2,yy+30,'0',18,'secondary',panel=panel,fill=self.theme['surface'])

    def source_appendix(self):
        if not self.sources:
            return
        if self.y>self.header_height+200:
            self.new_page()
        for identifier,proof in self.sources.values():
            self.note('ИСТОЧНИК / '+identifier,'\n'.join(source_lines(proof)),owner='source:'+canonical(proof))

    def finish(self):
        for i,page in enumerate(self.pages,1):
            self.page=page
            self.line(60,1363,1020,1363,'border',1)
            self.text(60,1381,'АРТИ · '+FORMATS[self.format][0],18,'secondary')
            label=f'{i:02d} / {len(self.pages):02d}'
            self.text(1020-font_for(18).getlength(label),1381,label,18,'secondary')
        check_layout(self.pages,self.spec)
        return self.pages,self.style


def scene(spec):
    layout=_Layout(spec)
    elements=spec.value['elements']
    fmt=spec.value['format']
    if fmt in ('timeline','roadmap'):
        elements=sorted(elements,key=lambda e:e['order'])
    if fmt=='process':
        layout.process(elements)
    elif fmt=='comparison':
        layout.columns(elements,variant='comparison')
    elif fmt=='arguments':
        layout.columns(elements,variant='arguments',hero=False)
    elif fmt in ('timeline','roadmap'):
        layout.chronology(elements,roadmap=fmt=='roadmap')
    elif fmt=='statistical':
        layout.statistics(elements)
    elif fmt=='table':
        layout.table(elements)
    elif fmt=='teaching':
        for i,element in enumerate(elements,1):
            def label(top,height,part):
                layout.text(65,top+25,f'{i:02d}',48,'heading')
                layout.text(65,top+91,'РАЗДЕЛ',18,'secondary')
                layout.line(65,top+126,143,top+126,'series',5)
            layout.card(element,i,x=180,w=840,variant='teaching',sidebar=label)
    else:
        # A wide lead card anchors a document summary, followed by paired cards.
        layout.card(elements[0],1,hero=True)
        if len(elements)>1:
            layout.columns(elements[1:],variant='card',hero=True,start_index=2)
    for relation in spec.value.get('relations',[]):
        labels={e['id']:e['label'] for e in elements}
        text=f"{labels[relation['from']]} [{relation['from']}] → {labels[relation['to']]} [{relation['to']}]: {RELATION[relation['kind']]}"
        if relation.get('label'):
            text+='\n'+relation['label']
        proof=relation.get('proof')
        if proof and proof.get('quote'):
            text+='\nЦитата: '+proof['quote']
        if proof:
            text+='\n'+layout.reference(proof)
        layout.note('СВЯЗЬ / '+relation['id'],text,owner='relation:'+relation['id'])
    for i,question in enumerate(spec.value.get('questions',[]),1):
        layout.note(f'ОТКРЫТЫЙ ВОПРОС / {i:02d}',question,owner='question:'+str(i))
    layout.source_appendix()
    return layout.finish()


def check_layout(pages,spec):
    if len(pages)>40:
        raise MaterialError('artifact_page_budget')
    seen=set()
    content={}
    compact=lambda value: ''.join(str(value).split())
    for page in pages:
        seen.update(page.elements)
        for i,(x,y,w,h) in enumerate(page.boxes):
            if not all(math.isfinite(v) for v in (x,y,w,h)) or w<0 or h<0 or x<0 or y<0 or x+w>WIDTH or y+h>HEIGHT-95:
                raise MaterialError('artifact_overflow')
            if any(x<a+c and x+w>a and y<b+d and y+h>b for a,b,c,d in page.boxes[:i]):
                raise MaterialError('artifact_overlap')
        text_bounds=[]
        for item in page.items:
            if item['kind']!='text':
                continue
            size=item['size']
            if size<18:
                raise MaterialError('artifact_text_too_small')
            font=font_for(size)
            x,y=item['x'],item['y']
            left,top,right,bottom=font.getbbox(item['text'],anchor='lt',features=['-kern','-liga'])
            bounds=(x+left,y+top,x+right,y+bottom)
            if not all(math.isfinite(v) for v in bounds) or bounds[0]<0 or bounds[1]<0 or bounds[2]>WIDTH-30 or bounds[3]>HEIGHT-20:
                raise MaterialError('artifact_text_overflow')
            panel=item.get('panel')
            if panel:
                a,b,w,h=panel
                if bounds[0]<a or bounds[1]<b or bounds[2]>a+w or bounds[3]>b+h:
                    raise MaterialError('artifact_text_outside_panel')
            if item.get('background') and contrast(item['color'],item['background'])<4.5:
                raise MaterialError('artifact_contrast')
            if compact(item['text']):
                if any(bounds[0]<r and bounds[2]>l and bounds[1]<b and bounds[3]>t for l,t,r,b in text_bounds):
                    raise MaterialError('artifact_text_overlap')
                text_bounds.append(bounds)
            owner=item.get('owner')
            if owner:
                content.setdefault(owner,{}).setdefault(item.get('flow','body'),[]).append(compact(item['text']))
    content={owner:[text for lines in flows.values() for text in lines] for owner,flows in content.items()}
    if not {e['id'] for e in spec.value['elements']}<=seen:
        raise MaterialError('artifact_element_omitted')
    if compact(spec.value['title']) not in ''.join(content.get('__title__',[])):
        raise MaterialError('artifact_content_omitted')
    for element in spec.value['elements']:
        rendered=''.join(content.get(element['id'],[]))
        required=[element['label'],element.get('text',''),STATUS[element['status']],element.get('when',''),element.get('series','')]
        quantity=element.get('quantity')
        if quantity:
            required.extend(quantity.get(k,'') for k in ('value','unit','lower','upper'))
        proof=element.get('proof')
        if proof:
            appendix=''.join(content.get('source:'+canonical(proof),[]))
            import re
            source_id=re.search(r'ИСТОЧНИК/(S[0-9]+)',appendix)
            if not source_id or 'Источник'+source_id.group(1) not in rendered:
                raise MaterialError('artifact_content_omitted')
            rendered+=appendix
            required.extend(source_lines(proof))
        if element.get('proof',{}).get('quote'):
            required.append(element['proof']['quote'])
        if any(compact(value) not in rendered for value in required):
            raise MaterialError('artifact_content_omitted')
    for relation in spec.value.get('relations',[]):
        rendered=''.join(content.get('relation:'+relation['id'],[]))
        required=[relation['from'],relation['to'],RELATION[relation['kind']],relation.get('label','')]
        proof=relation.get('proof')
        if proof:
            appendix=''.join(content.get('source:'+canonical(proof),[]))
            import re
            source_id=re.search(r'ИСТОЧНИК/(S[0-9]+)',appendix)
            if not source_id or 'Источник'+source_id.group(1) not in rendered:
                raise MaterialError('artifact_content_omitted')
            rendered+=appendix
            required.extend(source_lines(proof))
        if relation.get('proof',{}).get('quote'):
            required.append(relation['proof']['quote'])
        if any(compact(value) not in rendered for value in required):
            raise MaterialError('artifact_content_omitted')
    for i,question in enumerate(spec.value.get('questions',[]),1):
        if compact(question) not in ''.join(content.get('question:'+str(i),[])):
            raise MaterialError('artifact_content_omitted')
