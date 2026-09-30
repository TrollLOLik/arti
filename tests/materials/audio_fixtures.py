from io import BytesIO
import math
import struct
import wave


def wav(*,seconds=3,noise=False):
    buffer=BytesIO(); rate=16000
    with wave.open(buffer,'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(rate)
        samples=[]
        for i in range(round(seconds*rate)):
            t=i/rate
            value=.2*math.sin(2*math.pi*220*t) if .4<t<min(2.3,seconds-.3) else 0
            if noise: value+=.04*math.sin(2*math.pi*917*t)+.02*math.sin(2*math.pi*3177*t)
            samples.append(struct.pack('<h',round(value*32767)))
        stream.writeframes(b''.join(samples))
    return buffer.getvalue()


def assembly_result():
    return dict(text='Сумма 1200. I disagree.',language_code='ru',utterances=[
        dict(speaker='Alice',start=400,end=1600,text='Сумма 1200.',words=[dict(text='Сумма',start=400,end=800,confidence=.9),dict(text='1200.',start=900,end=1600,confidence=.52)]),
        dict(speaker='Bob',start=1100,end=2300,text='I disagree.',words=[dict(text='I',start=1100,end=1400,confidence=.91),dict(text='disagree.',start=1400,end=2300,confidence=.9)])])
