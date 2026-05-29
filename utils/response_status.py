"""
Управление статусом ответов бота (с использованием PostgreSQL)
"""
from database.models import ResponseStatus

# In-memory кеш для быстрого доступа
_response_cache = {}


async def is_responses_enabled(chat_id: int) -> bool:
    """Проверить, включены ли ответы бота в чате"""
    if chat_id in _response_cache:
        return _response_cache[chat_id]
    
    enabled = await ResponseStatus.get(chat_id)
    _response_cache[chat_id] = enabled
    return enabled


async def set_responses_enabled(chat_id: int, enabled: bool):
    """Установить статус ответов бота в чате"""
    await ResponseStatus.set(chat_id, enabled)
    _response_cache[chat_id] = enabled

