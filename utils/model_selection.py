"""
Управление выбором модели ИИ для чата (с использованием PostgreSQL)
"""
from database.models import AIModel, ChatModel
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
    row = await AIModel.get_by_model_id(model_id)
    if row is None:
        raise ValueError('unknown_model_selection')
    await ChatModel.set(chat_id, row['key'])
    _model_cache[chat_id] = row['key']
