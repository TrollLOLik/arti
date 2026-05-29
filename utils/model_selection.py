"""
Управление выбором модели ИИ для чата (с использованием PostgreSQL)
"""
from database.models import ChatModel
from config import DEFAULT_MODEL

# In-memory кеш для быстрого доступа
_model_cache = {}

async def get_chat_model(chat_id: int) -> str:
    """Получить выбранную модель для чата"""
    if chat_id in _model_cache:
        return _model_cache[chat_id]
    
    model_id = await ChatModel.get(chat_id, DEFAULT_MODEL)
    _model_cache[chat_id] = model_id
    return model_id

async def set_chat_model(chat_id: int, model_id: str):
    """Установить модель для чата"""
    await ChatModel.set(chat_id, model_id)
    _model_cache[chat_id] = model_id
