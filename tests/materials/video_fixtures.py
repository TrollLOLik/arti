"""Own three-scene video with native timestamps, transient and audio fixtures."""
import os,subprocess,tempfile
from pathlib import Path
from PIL import Image,ImageDraw
from tests.materials.audio_fixtures import wav
from materials.extractors.media import installation


def video(*,with_audio=True,offset=False,transient_frame=14,transient_box=(340,25,440,110)):
    ffmpeg=installation()[0]
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        for i in range(30):
            image=Image.new('RGB',(480,270),('navy','darkred','darkgreen')[i//10]); draw=ImageDraw.Draw(image)
            draw.text((30,80),'SLIDE '+str(i//10+1),fill='white',font_size=44)
            if i==transient_frame: draw.rectangle(transient_box,fill='yellow')
            image.save(root/f'frame_{i:03d}.png')
        (root/'audio.wav').write_bytes(wav())
        command=[ffmpeg,'-nostdin','-v','error','-threads','1','-framerate','10','-i',str(root/'frame_%03d.png')]
        if with_audio:
            if offset: command+=['-itsoffset','0.5']
            command+=['-i',str(root/'audio.wav'),'-map','0:v:0','-map','1:a:0','-c:a','aac']
        command+=['-c:v','libx264','-pix_fmt','yuv420p','-t','3','-y',str(root/'video.mp4')]
        subprocess.run(command,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        return (root/'video.mp4').read_bytes()
