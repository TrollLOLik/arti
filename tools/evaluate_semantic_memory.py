"""Synthetic Russian paraphrases; no bot database, messages or provider calls."""
import asyncio
import json
import time
from pathlib import Path
from cognition.semantic import LocalEncoder, VERSION
from cognition.memory_dynamics import lexical_vector, cosine

CASES = [
    ('Я терпеть не могу, когда собеседник перебивает меня.', 'Что меня раздражает во время разговора?'),
    ('После молочных продуктов у меня болит живот.', 'Какую еду мой организм плохо переносит?'),
    ('Лучше напиши мне, голосовые неудобно слушать на работе.', 'Как мне удобнее общаться в офисе?'),
    ('В детстве я боялся темноты и спал с ночником.', 'Что пугало меня маленьким перед сном?'),
    ('Мы расстались с девушкой в конце лета.', 'Когда закончились мои романтические отношения?'),
    ('Хочу накопить на собственную квартиру.', 'Ради чего я откладываю деньги?'),
    ('По выходным люблю бродить по лесу без телефона.', 'Как я отдыхаю от цифрового шума?'),
    ('У моего кота аллергия на курицу.', 'Чем нельзя кормить моего питомца?'),
    ('Экзамен по математике перенесли на пятницу.', 'Когда теперь проверка знаний по алгебре?'),
    ('На прошлой работе начальник постоянно унижал сотрудников.', 'Почему у меня неприятные воспоминания о прежнем руководителе?'),
    ('Я учусь играть на гитаре по вечерам.', 'Какой музыкальный инструмент я осваиваю?'),
    ('Мне спокойнее, когда о смене планов сообщают заранее.', 'Как лучше предупредить меня о переносе встречи?'),
    ('Сестра переехала в другой город, я по ней скучаю.', 'Кого из родных мне сейчас не хватает рядом?'),
    ('Зимой я сломал ногу, катаясь на лыжах.', 'Какая травма случилась у меня на горнолыжном склоне?'),
    ('Собеседование назначено на вторник утром.', 'Когда я встречаюсь с потенциальным работодателем?'),
    ('Перед выступлениями на публике у меня дрожат руки.', 'Как мой организм реагирует на выход к аудитории?'),
    ('Я стараюсь ложиться до одиннадцати, иначе утром разбитый.', 'Какой режим сна помогает мне высыпаться?'),
    ('В августе мы ездили на море всей семьей.', 'Где прошел наш совместный летний отпуск?'),
    ('Я обещал другу помочь с переездом в субботу.', 'Какое обязательство у меня на ближайшие выходные?'),
    ('Мне понравился подарок, который ты помогла выбрать маме.', 'В чем твой совет оказался полезен для моей семьи?'),
]

async def main():
    encoder = LocalEncoder()
    started = time.perf_counter()
    try:
        vectors = await encoder.encode([p[0] for p in CASES]+[p[1] for p in CASES],timeout=60)
        if vectors is None:
            raise RuntimeError('Local semantic model unavailable; run setup_semantic_memory')
        cold = round((time.perf_counter()-started)*1000)
        timings=[]; semantic=[]; lexical=[]
        for i,(_,query) in enumerate(CASES):
            started=time.perf_counter()
            encoded=await encoder.encode([query],timeout=2)
            timings.append(round((time.perf_counter()-started)*1000,2))
            scores=[cosine(encoded[0],v) for v in vectors[:len(CASES)]]
            order=sorted(range(len(CASES)),key=lambda j:-scores[j])
            baseline=[cosine(lexical_vector(query),lexical_vector(t)) for t,_ in CASES]
            old_order=sorted(range(len(CASES)),key=lambda j:-baseline[j])
            semantic.append(dict(case=i+1,rank=order.index(i)+1,score=round(scores[i],4)))
            lexical.append(old_order.index(i)+1)
        report=dict(model=VERSION,synthetic_only=True,cases=len(CASES),cold_batch_ms=cold,
            warm_query_median_ms=sorted(timings)[len(timings)//2],warm_query_max_ms=max(timings),
            semantic_recall_at_1=sum(x['rank']==1 for x in semantic)/len(CASES),
            semantic_recall_at_3=sum(x['rank']<=3 for x in semantic)/len(CASES),
            lexical_recall_at_1=sum(x==1 for x in lexical)/len(CASES),
            hits_above_threshold=sum(x['score']>=.42 for x in semantic),details=semantic)
        Path('docs/evaluation/semantic_memory.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k!='details'}))
    finally:
        await encoder.close()

if __name__=='__main__':
    asyncio.run(main())
