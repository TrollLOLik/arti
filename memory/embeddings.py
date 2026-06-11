import asyncio
import logging
from typing import List

from google.genai import types

from config import genai_client
from memory.normalizer import compact_text

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "gemini-embedding-2"
EMBEDDING_DIMENSIONS = 1536


async def _embed(text: str) -> List[float]:
    if not text or not text.strip():
        return []

    response = await asyncio.to_thread(
        genai_client.models.embed_content,
        model=EMBEDDING_MODEL,
        contents=compact_text(text, 8000),
        config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIMENSIONS),
    )

    if not response or not getattr(response, "embeddings", None):
        return []

    values = response.embeddings[0].values
    vector = [float(value) for value in values]

    # Размерность зашита и в коде, и в схеме БД (vector(1536)). Если модель/версия
    # вернёт вектор другой длины — вставка в pgvector упадёт. Отбрасываем заранее,
    # чтобы поиск корректно деградировал на text retrieval, а не падал молча.
    if len(vector) != EMBEDDING_DIMENSIONS:
        logger.error(
            "Embedding размерности %s не совпадает с ожидаемой %s (модель %s) — вектор отброшен.",
            len(vector), EMBEDDING_DIMENSIONS, EMBEDDING_MODEL,
        )
        return []

    return vector


async def embed_query(text: str) -> List[float]:
    try:
        return await _embed(f"task: retrieval_query | {text}")
    except Exception as e:
        logger.warning(f"Ошибка embedding query: {e}")
        return []


async def embed_document(text: str) -> List[float]:
    try:
        return await _embed(f"task: retrieval_document | {text}")
    except Exception as e:
        logger.warning(f"Ошибка embedding document: {e}")
        return []
