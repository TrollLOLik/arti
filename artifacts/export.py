from io import BytesIO
from html import escape
from PIL import Image,ImageDraw,ImageFont
from artifacts.rendering import scene,FONT,WIDTH,HEIGHT
from materials.types import canonical

def export(spec,illustrations=None):
    spec.validate()
    pages,style=scene(spec); outputs={'spec.json':canonical(spec.to_dict()).encode()}
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    pdfmetrics.registerFont(TTFont('ArtiDejaVu',str(FONT)))
    stream=BytesIO(); pdf=Canvas(stream,pagesize=(WIDTH,HEIGHT),invariant=1)
    pdf.setTitle(spec.value['title'])
    for n,page in enumerate(pages,1):
        picture=Image.new('RGB',(WIDTH,HEIGHT),style.background); draw=ImageDraw.Draw(picture)
        import base64
        font_data=base64.b64encode(FONT.read_bytes()).decode()
        svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',f'<style>@font-face {{font-family: "DejaVu Sans"; src: url(data:font/ttf;base64,{font_data}) format("truetype");}}</style>',f'<rect width="100%" height="100%" fill="{style.background}"/>']
        pdf.setFillColor(style.background); pdf.rect(0,0,WIDTH,HEIGHT,fill=1,stroke=0)
        for p in page.items:
            x,y=p['x'],p['y']; color=p['color']; pdf.setFillColor(color); pdf.setStrokeColor(color)
            if p['kind']=='text':
                font=ImageFont.truetype(str(FONT),p['size']); draw.text((x,y),p['text'],font=font,fill=color,anchor='lt')
                svg.append(f'<text x="{x}" y="{y+p["size"]}" font-family="DejaVu Sans, sans-serif" font-size="{p["size"]}" fill="{color}">{escape(p["text"])}</text>')
                pdf.setFont('ArtiDejaVu',p['size']); pdf.drawString(x,HEIGHT-y-p['size'],p['text'])
            elif p['kind']=='rect':
                draw.rectangle((x,y,x+p['w'],y+p['h']),fill=color,outline=p['stroke'],width=1)
                svg.append(f'<rect x="{x}" y="{y}" width="{p["w"]}" height="{p["h"]}" fill="{color}" stroke="{p["stroke"]}"/>')
                pdf.setStrokeColor(p['stroke']); pdf.rect(x,HEIGHT-y-p['h'],p['w'],p['h'],fill=1,stroke=1)
            else:
                draw.line((x,y,p['x2'],p['y2']),fill=color,width=p['width'])
                svg.append(f'<line x1="{x}" y1="{y}" x2="{p["x2"]}" y2="{p["y2"]}" stroke="{color}" stroke-width="{p["width"]}"/>')
                pdf.setLineWidth(p['width']); pdf.line(x,HEIGHT-y,p['x2'],HEIGHT-p['y2'])
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
