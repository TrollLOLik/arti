"""Original diagram/axis fixtures with known geometry and no user material."""
from io import BytesIO
from PIL import Image,ImageDraw,ImageFont
from tests.materials.document_fixtures import font_path


def diagram(*,heldout=False):
    image=Image.new('RGB',(1100,650),'white'); draw=ImageDraw.Draw(image); font=ImageFont.truetype(font_path(),42)
    labels=('W','X','Y','Z') if heldout else ('A','B','C','D')
    boxes=((80,70,300,220),(780,70,1000,220),(80,410,300,560),(780,410,1000,560))
    for label,box in zip(labels,boxes):
        draw.rectangle(box,outline='black',width=4); draw.text((box[0]+80,box[1]+45),label,font=font,fill='black')
    for start,end in (((300,145),(780,485)),((300,485),(780,145))):
        draw.line((start,end),fill='black',width=4)
        x,y=end; draw.polygon((end,(x-27,y-5),(x-17,y-26)) if y>300 else (end,(x-28,y+4),(x-17,y+25)),fill='black')
    draw.text((410,40),'No join',font=font,fill='black')
    out=BytesIO(); image.save(out,format='PNG'); image.close(); return out.getvalue(),labels


def logarithmic_chart():
    image=Image.new('RGB',(1100,700),'white'); draw=ImageDraw.Draw(image); font=ImageFont.truetype(font_path(),38)
    draw.text((100,25),'Log scale; Amount (RUB)',font=font,fill='black')
    draw.line((180,110,180,610,1010,610),fill='black',width=4)
    for y,label in ((600,'1'),(450,'10'),(300,'100'),(150,'1000')):
        draw.text((40,y-20),label,font=font,fill='black'); draw.line((175,y,1010,y),fill='#888888',width=1)
    draw.rectangle((330,300,470,610),fill='#2168b0'); draw.rectangle((650,450,790,610),fill='#29a088')
    draw.text((330,625),'Alpha',font=font,fill='black'); draw.text((650,625),'Beta',font=font,fill='black')
    out=BytesIO(); image.save(out,format='PNG'); image.close(); return out.getvalue()
