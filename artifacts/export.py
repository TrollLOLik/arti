from io import BytesIO
from functools import lru_cache
import struct
from html import escape
from PIL import Image,ImageDraw,ImageFont
from artifacts.rendering import scene,FONT,WIDTH,HEIGHT
from materials.types import canonical


@lru_cache(maxsize=64)
def _svg_font_subset(characters):
    """Small, deterministic Unicode TTF subset using the existing PDF stack.

    ReportLab's PDF subset uses consecutive byte codes. Rebuild its cmap as a
    Unicode format-12 table before embedding it in SVG so Cyrillic and composed
    glyphs retain their actual code points. All glyph outlines/metrics and the
    font's name/license tables are kept verbatim from the valid subset.
    """
    from reportlab.pdfbase.ttfonts import TTFontFace, TTFontParser, TTFontMaker
    codes=sorted(set(characters) | {32})
    subset=TTFontFace(str(FONT)).makeSubset(codes)
    parsed=TTFontParser(BytesIO(subset))
    legacy_cmap=parsed.get_table('cmap')
    glyphs=struct.unpack_from('>'+str(len(codes))+'H',legacy_cmap,22)
    body=struct.pack('>HHIII',12,0,16+12*len(codes),0,len(codes))
    body+=b''.join(struct.pack('>III',code,code,glyph) for code,glyph in zip(codes,glyphs))
    # Both Unicode and Windows full-repertoire records share the same subtable.
    cmap=struct.pack('>HHHHIHHI',0,2,0,4,20,3,10,20)+body
    rebuilt=TTFontMaker()
    for tag in parsed.table:
        rebuilt.add(tag,cmap if tag=='cmap' else parsed.get_table(tag))
    return rebuilt.makeStream()

def export(spec,illustrations=None):
    spec.validate()
    pages,style=scene(spec); outputs={'spec.json':canonical(spec.to_dict()).encode()}
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    pdfmetrics.registerFont(TTFont('ArtiDejaVu',str(FONT)))
    stream=BytesIO(); pdf=Canvas(stream,pagesize=(WIDTH,HEIGHT),invariant=1)
    pdf.setTitle(spec.value['title'])
    import base64
    for n,page in enumerate(pages,1):
        characters=tuple(sorted({ord(c) for p in page.items if p['kind']=='text' for c in p['text']}))
        font_data=base64.b64encode(_svg_font_subset(characters)).decode()
        picture=Image.new('RGB',(WIDTH,HEIGHT),style.background); draw=ImageDraw.Draw(picture)
        svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title description">',
             f'<title id="title">{escape(spec.value["title"])} · {n}/{len(pages)}</title>',
             '<desc id="description">Статусы, значения, интервалы и источники обозначены текстом. Полная детализация продолжается на следующих страницах.</desc>',
             f'<style>@font-face {{font-family: "ArtiEmbeddedSubset"; src: url(data:font/ttf;base64,{font_data}) format("truetype");}} text {{font-kerning: none; font-variant-ligatures: none;}}</style>',
             f'<rect width="100%" height="100%" fill="{style.background}"/>']
        pdf.setFillColor(style.background); pdf.rect(0,0,WIDTH,HEIGHT,fill=1,stroke=0)
        for p in page.items:
            x,y=p['x'],p['y']; color=p['color']; pdf.setFillColor(color); pdf.setStrokeColor(color)
            if p['kind']=='text':
                font=ImageFont.truetype(str(FONT),p['size'])
                # y is the measured ink top. The same TTF baseline is used by
                # every backend, including descenders and Cyrillic capitals.
                baseline=y-font.getbbox(p['text'],anchor='ls',features=['-kern','-liga'])[1]
                draw.text((x,baseline),p['text'],font=font,fill=color,anchor='ls',features=['-kern','-liga'])
                owner=f' data-owner="{escape(str(p["owner"]),quote=True)}"' if p.get('owner') else ''
                svg.append(f'<text x="{x}" y="{baseline}" font-family="ArtiEmbeddedSubset, sans-serif" font-size="{p["size"]}" fill="{color}"{owner}>{escape(p["text"])}</text>')
                pdf.setFont('ArtiDejaVu',p['size']); pdf.drawString(x,HEIGHT-baseline,p['text'])
            elif p['kind']=='rect':
                radius=p.get('radius',0)
                stroke=p.get('stroke',color)
                if radius:
                    draw.rounded_rectangle((x,y,x+p['w'],y+p['h']),radius=radius,fill=color,outline=stroke,width=1)
                else:
                    draw.rectangle((x,y,x+p['w'],y+p['h']),fill=color,outline=stroke,width=1)
                svg.append(f'<rect x="{x}" y="{y}" width="{p["w"]}" height="{p["h"]}" rx="{radius}" fill="{color}" stroke="{stroke}"/>')
                pdf.setStrokeColor(stroke); pdf.setLineWidth(1)
                if radius: pdf.roundRect(x,HEIGHT-y-p['h'],p['w'],p['h'],radius,fill=1,stroke=1)
                else: pdf.rect(x,HEIGHT-y-p['h'],p['w'],p['h'],fill=1,stroke=1)
            elif p['kind']=='circle':
                draw.ellipse((x,y,x+p['w'],y+p['h']),fill=color)
                svg.append(f'<ellipse cx="{x+p["w"]/2}" cy="{y+p["h"]/2}" rx="{p["w"]/2}" ry="{p["h"]/2}" fill="{color}"/>')
                pdf.ellipse(x,HEIGHT-y-p['h'],x+p['w'],HEIGHT-y,fill=1,stroke=0)
            elif p['kind']=='line':
                draw.line((x,y,p['x2'],p['y2']),fill=color,width=p['width'])
                svg.append(f'<line x1="{x}" y1="{y}" x2="{p["x2"]}" y2="{p["y2"]}" stroke="{color}" stroke-width="{p["width"]}"/>')
                pdf.setLineWidth(p['width']); pdf.line(x,HEIGHT-y,p['x2'],HEIGHT-p['y2'])
            else:
                raise ValueError('unknown_scene_primitive')
        svg.append('</svg>'); out=BytesIO(); picture.save(out,format='PNG')
        outputs[f'page-{n}.png']=out.getvalue(); outputs[f'page-{n}.svg']='\n'.join(svg).encode(); pdf.showPage()
    # Decoration has a dedicated labelled page, never covers verified data.
    for n,pixels in enumerate(illustrations or [],1):
        from PIL import ImageOps
        image=Image.open(BytesIO(pixels)); image.load()
        if image.width*image.height>16000000: raise ValueError('illustration_pixel_budget')
        image=ImageOps.contain(image.convert('RGB'),(960,1100))
        picture=Image.new('RGB',(WIDTH,HEIGHT),style.background); picture.paste(image,((WIDTH-image.width)//2,150))
        draw=ImageDraw.Draw(picture); draw.text((60,70),'Иллюстрация / художественная часть',font=ImageFont.truetype(str(FONT),30),fill=style.foreground)
        draw.text((60,1320),'Изображение не является свидетельством фактов.',font=ImageFont.truetype(str(FONT),24),fill=style.muted)
        out=BytesIO(); picture.save(out,format='PNG'); outputs[f'illustration-{n}.png']=out.getvalue()
        import base64
        encoded=base64.b64encode(out.getvalue()).decode()
        outputs[f'illustration-{n}.svg']=(f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}"><title>Иллюстрация: художественная часть, не свидетельство фактов</title><image width="{WIDTH}" height="{HEIGHT}" href="data:image/png;base64,{encoded}"/></svg>').encode()
        from reportlab.lib.utils import ImageReader
        pdf.drawImage(ImageReader(picture),0,0,WIDTH,HEIGHT); pdf.showPage()
    pdf.save(); outputs['report.pdf']=stream.getvalue()
    return outputs

async def export_current(repository,id,actor,*,revision=None):
    from artifacts.spec import ArtifactSpec
    from materials.derivatives import DerivativeRepository
    import asyncio
    row=await repository.get(id,actor)
    if revision is not None and row['revision']!=revision:
        from materials.types import MaterialError
        raise MaterialError('stale_artifact_revision')
    images=[]
    for illustration in row['spec'].get('illustrations',[]):
        body=await DerivativeRepository(repository.materials).load(illustration,actor,'illustration')
        if 'pixels' in body:
            import base64
            from hashlib import sha256
            from materials.types import MaterialError
            pixels=base64.b64decode(body['pixels'],validate=True)
            if sha256(pixels).hexdigest()!=body['sha256']: raise MaterialError('illustration_integrity')
            images.append(pixels); continue
        # Repository stores the original under its normal blob locator; caller supplies the service.
        service=getattr(repository,'service',None)
        if service is None:
            from materials.runtime import service_for_bot
            service=await service_for_bot()
        _,_,pixels=await service.read_bytes(body['asset_id'],actor); images.append(pixels)
    files=await asyncio.to_thread(export,ArtifactSpec(row['spec']),images)
    current=await repository.get(id,actor)
    if current['head']!=row['head']:
        from materials.types import MaterialError
        raise MaterialError('stale_artifact_revision')
    return files,row
