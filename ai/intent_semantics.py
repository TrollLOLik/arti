"""Conservative local matching of material work versus discussing that work."""
import re
EXAMPLES = (
    'Представь содержимое документов в виде понятной схемы.',
    'Покажи различия между этими файлами наглядно.',
    'Оформи сведения из материалов в таблицу для сравнения.',
    'Я хочу получить визуальное представление этой информации.',
    'Сделай инфографику по прикрепленным материалам.',
    'Хочу просто поговорить о документах.',
    'Объясни, как самостоятельно делать схемы.',
    'Мне не нужна инфографика, не создавай ее.',
    'Ты любишь красивые картинки?',
    'Расскажи подробнее об этом вопросе.',
)


async def local_artifact_intent(text,encoder):
    references=getattr(encoder,'intent_references',None)
    text=re.sub(r'```.*?```|«[^»]*»|"[^"\n]*"',' ',str(text)[:3500],flags=re.S)
    queries=[text]
    sentences=re.split(r'[.!]\s+',text)
    if len(sentences)>1 and re.match(r'(?i)(?:можешь|можно|покажи|хочу)\b',sentences[-1]):
        queries.append(sentences[-1])
    batch=queries if references is not None else [*EXAMPLES,*queries]
    vectors=await encoder.encode(batch,timeout=.6)
    if vectors is None:
        return False
    if references is None:
        references=vectors[:len(EXAMPLES)]
        encoder.intent_references=references
    for query in vectors[-len(queries):]:
        scores=[sum(x*y for x,y in zip(query,v)) for v in references]
        positive,negative=max(scores[:5]),max(scores[5:])
        if positive>=.60 and positive-negative>=.08:
            return True
    return False
