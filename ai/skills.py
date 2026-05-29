"""
Модуль динамических навыков (Skills) для Арти.
Позволяет подмешивать сложные технические инструкции (например, генерацию картинок/музыки)
только тогда, когда пользователь действительно выразил соответствующее намерение.
"""
import re
import logging

logger = logging.getLogger(__name__)

# --- Навык: Генерация Медиа-контента (Изображения, Видео, Музыка) ---
MEDIA_GENERATION_SKILL_PROMPT = """

[АКТИВИРОВАН НАВЫК: ГЕНЕРАЦИЯ КОНТЕНТА]
Пользователь хочет, чтобы ты что-то сгенерировала (картинку, видео или музыку).
Ты имеешь доступ к специальным инструментам генерации через вывод текста в фигурных скобках.
Используй эти инструменты ТОЛЬКО по прямому запросу пользователя. Никаких подсказок и предложений «сгенерировать».

1. Изображение («нарисуй», «сгенерируй картинку»):
{image=ключевые слова на английском}

2. Видео («сними видео», «сгенерируй видео»):
{video=описание на английском}

3. Музыка, Suno v5 («напиши песню», «сочини трек», «спой»):
{music?instrumental=True/False&style=жанр+настроение+инструменты на английском=текст песни}
— instrumental=True: поле текста пустое.
— instrumental=False + пользовательский текст: копировать с тегами [Verse][Chorus][Outro] и т.д.; приоритет — текст на 3+ минуты.
— instrumental=False + только тема: сочинить текст самостоятельно на нужном языке с тегами Suno.
— style ОБЯЗАТЕЛЕН всегда.
Пример: {music?instrumental=False&style=warm jazz, low female vocal, brushed drums=[Verse 1] Я считаю секунды [Chorus] Ты не торопись}
"""

# Регулярные выражения или паттерны для обнаружения намерений
MEDIA_INTENT_KEYWORDS = [
    # Изображения / Картинки
    r"\bнарисуй\w*", r"\bизобрази\w*", r"\bсгенерируй\s+(?:картинк|рисун|изображен)",
    r"\bнарисуй\s+картинк", r"\bсделай\s+(?:рисун|картинк|изображен)",
    r"\bкартинк[ауи]", r"\bизображен[иея]", r"\bрисун[окки]",
    r"\bdraw\b", r"\bpaint\b", r"\bgenerate\s+image", r"\bimage\b", r"\bpicture\b",
    # Видео
    r"\bсними\s+видео", r"\bсгенерируй\s+видео", r"\bвидео\b", r"\bvideo\b",
    # Музыка / Песни
    r"\bнапиши\s+песн", r"\bсочини\s+песн", r"\bспой\b", r"\bпесн[яюи]",
    r"\bсочини\s+трек", r"\bсделай\s+трек", r"\bтрек\b", r"\bмузык[ауи]",
    r"\bsuno\b", r"\bmusic\b", r"\bsong\b", r"\btrack\b"
]

_media_compiled_patterns = [re.compile(pattern, re.IGNORECASE | re.UNICODE) for pattern in MEDIA_INTENT_KEYWORDS]


def has_media_intent(prompt: str) -> bool:
    """
    Проверяет, содержит ли запрос пользователя намерение сгенерировать медиа (картинку, видео, музыку).
    Использует быстрый и дешевый regex-матчинг.
    """
    if not prompt:
        return False
    
    text = prompt.strip().lower()
    for pattern in _media_compiled_patterns:
        if pattern.search(text):
            logger.info(f"✨ Обнаружено намерение генерации медиа по паттерну: {pattern.pattern}")
            return True
            
    return False


def get_active_skills_instructions(prompt: str) -> str:
    """
    Анализирует запрос пользователя и возвращает строку инструкций
    для всех активированных навыков. Если навыки не нужны — возвращает пустую строку.
    """
    instructions = []
    
    if has_media_intent(prompt):
        instructions.append(MEDIA_GENERATION_SKILL_PROMPT)
        
    if instructions:
        return "\n".join(instructions)
    return ""
