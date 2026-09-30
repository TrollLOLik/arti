"""Reply-based precise moment inspection and bounded storyboard delivery."""
import base64,shlex
from io import BytesIO
from types import SimpleNamespace
from materials.runtime import enabled,capture_document,actor_for_current,service_for_bot,CURRENT_MATERIAL_USE,guard_current
from materials.types import EvidenceRef,MaterialError

HELP='Ответь на видео: /moment 1.4 — соседние кадры вокруг секунды; /storyboard — выборка кадров. Редкая выборка не доказывает отсутствие события.'

async def _run(update,context,dense):
    message=update.effective_message; source=getattr(message,'reply_to_message',None)
    video=getattr(source,'video',None) or getattr(source,'video_note',None) or getattr(source,'document',None)
    if not enabled() or not source or not video: await message.reply_text(HELP); return
    token=None
    try:
        descriptor=SimpleNamespace(file_id=video.file_id,file_size=video.file_size,file_name=getattr(video,'file_name',None) or 'video.mp4',mime_type=getattr(video,'mime_type',None) or 'video/mp4')
        material=await capture_document(context,descriptor,source,slot='video')
        token=CURRENT_MATERIAL_USE.set(material.material_uses); use=material.material_uses[0]
        actor=await actor_for_current(); service=await service_for_bot()
        if dense:
            parts=shlex.split(message.text)[1:]
            if len(parts)!=1: raise MaterialError('moment_required')
            from decimal import Decimal
            second=Decimal(parts[0])
            if not second.is_finite() or not 0<=second<=86400: raise MaterialError('invalid_moment')
            ms=int(second*1000)
            eid,bundle=await service.observe_video_interval(use.asset_id,actor,max(0,ms-600),ms+600)
        else: eid,bundle=await service.extract(use.asset_id,actor)
        frames=[b for b in bundle.blocks if b.metadata.get('role')=='video_frame']
        if not frames: raise MaterialError('video_frames_unavailable')
        from PIL import Image,ImageDraw
        selected=frames if len(frames)<=6 else [frames[round(i*(len(frames)-1)/5)] for i in range(6)]
        canvas=Image.new('RGB',(960,300*((len(selected)+1)//2)),'white'); draw=ImageDraw.Draw(canvas)
        for i,block in enumerate(selected):
            ref=EvidenceRef(use.asset_id,bundle.asset_version,eid,block.block_id,block.locator)
            value=await service.video_frame(ref,actor)
            with Image.open(BytesIO(base64.b64decode(value['image_base64']))) as image:
                image.thumbnail((470,260)); x=(i%2)*480; y=(i//2)*300
                canvas.paste(image,(x,y)); draw.text((x+8,y+268),str(value['timestamp_ms']/1000)+' s',fill='black')
        output=BytesIO(); canvas.save(output,format='PNG'); canvas.close(); output.seek(0); output.name='storyboard.png'
        await guard_current(message.chat_id)
        await message.reply_photo(output,caption='Кадры с исходными временными метками. Между кадрами содержимое не проверено; говорящий не идентифицирован.')
    except (MaterialError,ValueError,ArithmeticError):
        if token is not None: CURRENT_MATERIAL_USE.reset(token); token=None
        await message.reply_text('Момент недоступен или источник отозван. '+HELP)
    finally:
        if token is not None: CURRENT_MATERIAL_USE.reset(token)

async def moment_command(update,context): await _run(update,context,True)
async def storyboard_command(update,context): await _run(update,context,False)
