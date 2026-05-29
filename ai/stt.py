"""
Speech-to-Text.

Основной бэкенд: AssemblyAI (universal-3-pro / universal-2 fallback,
language detection включён). При недоступности — фоллбэк на Groq
Whisper Large V3 Turbo.

AssemblyAI лучше распознаёт русский, диалекты и контекст; Groq быстрее
и проще, оставлен резервом.
"""
from __future__ import annotations

import os
import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from config import groq_client

logger = logging.getLogger(__name__)


ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY", "").strip()
ASSEMBLYAI_BASE = "https://api.assemblyai.com/v2"
ASSEMBLYAI_REQUEST_TIMEOUT = float(os.getenv("ASSEMBLYAI_REQUEST_TIMEOUT", "120"))
ASSEMBLYAI_POLL_TIMEOUT = float(os.getenv("ASSEMBLYAI_POLL_TIMEOUT", "600"))


# ============================================================================
# AssemblyAI
# ============================================================================

async def _aai_request_with_retries(
    method: str,
    url: str,
    *,
    client: httpx.AsyncClient,
    attempts: int = 3,
    **kwargs: Any,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(method, url, **kwargs)
            if response.status_code not in {429, 500, 502, 503, 504}:
                return response
            last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
        except httpx.HTTPError as exc:
            last_error = exc
        if attempt < attempts:
            await asyncio.sleep(min(2 ** attempt, 10))
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"AssemblyAI request failed after {attempts} attempts: {method} {url}")


async def _aai_upload(client: httpx.AsyncClient, audio_bytes: bytes) -> str:
    response = await _aai_request_with_retries(
        "POST", f"{ASSEMBLYAI_BASE}/upload",
        client=client,
        content=audio_bytes,
    )
    response.raise_for_status()
    return response.json()["upload_url"]


async def _aai_submit(client: httpx.AsyncClient, upload_url: str, language: Optional[str]) -> str:
    payload: dict[str, Any] = {
        "audio_url": upload_url,
        "speech_models": ["universal-3-pro", "universal-2"],
        "temperature": 0,
    }
    if language:
        payload["language_code"] = language
    else:
        payload["language_detection"] = True
    response = await _aai_request_with_retries(
        "POST", f"{ASSEMBLYAI_BASE}/transcript",
        client=client,
        json=payload,
    )
    response.raise_for_status()
    return response.json()["id"]


async def _aai_poll(client: httpx.AsyncClient, transcript_id: str) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + ASSEMBLYAI_POLL_TIMEOUT
    url = f"{ASSEMBLYAI_BASE}/transcript/{transcript_id}"
    while True:
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"AssemblyAI polling timed out after {ASSEMBLYAI_POLL_TIMEOUT}s")
        response = await _aai_request_with_retries("GET", url, client=client)
        response.raise_for_status()
        result = response.json()
        status = result.get("status")
        if status == "completed":
            return result
        if status == "error":
            raise RuntimeError(f"AssemblyAI transcription failed: {result.get('error', 'unknown')}")
        await asyncio.sleep(2)


async def _transcribe_assemblyai(
    audio_path: Path,
    language: Optional[str],
) -> str:
    """Транскрибирует аудио через AssemblyAI. Возвращает текст или поднимает исключение."""
    if not ASSEMBLYAI_API_KEY:
        raise RuntimeError("ASSEMBLYAI_API_KEY не задан в .env")

    abs_path = Path(audio_path).resolve()
    if not abs_path.exists():
        raise FileNotFoundError(f"Файл не найден: {abs_path}")

    # Читаем файл синхронно в thread, чтобы не блокировать loop на больших файлах.
    audio_bytes = await asyncio.to_thread(abs_path.read_bytes)

    headers = {"authorization": ASSEMBLYAI_API_KEY}
    async with httpx.AsyncClient(
        timeout=ASSEMBLYAI_REQUEST_TIMEOUT,
        headers=headers,
        trust_env=False,  # игнорируем системные SOCKS-прокси из реестра Windows
    ) as client:
        logger.info(f"AssemblyAI: загружаю {abs_path.name} ({len(audio_bytes)} байт)")
        upload_url = await _aai_upload(client, audio_bytes)

        logger.info(f"AssemblyAI: создаю транскрипт (lang={language or 'auto'})")
        transcript_id = await _aai_submit(client, upload_url, language)

        logger.info(f"AssemblyAI: ожидаю готовности (id={transcript_id})")
        result = await _aai_poll(client, transcript_id)

    text = (result.get("text") or "").strip()
    if not text:
        raise RuntimeError("AssemblyAI вернул пустую транскрипцию")
    return text


# ============================================================================
# Groq Whisper (fallback)
# ============================================================================

async def _transcribe_groq(audio_path: Path, language: Optional[str]) -> str:
    abs_path = Path(audio_path).resolve()
    if not abs_path.exists():
        raise FileNotFoundError(f"Файл не найден: {abs_path}")

    file_bytes = await asyncio.to_thread(abs_path.read_bytes)

    kwargs: dict[str, Any] = {
        "file": (abs_path.name, file_bytes),
        "model": "whisper-large-v3-turbo",
        "response_format": "json",
    }
    if language:
        kwargs["language"] = language

    logger.info(f"Groq STT (fallback): {abs_path.name}")
    transcription = await groq_client.audio.transcriptions.create(**kwargs)
    text = (transcription.text or "").strip()
    if not text:
        raise RuntimeError("Groq вернул пустую транскрипцию")
    return text


# ============================================================================
# Публичные функции
# ============================================================================

async def transcribe_audio(
    file_path: str | Path,
    language: Optional[str] = None,
    *,
    lowercase: bool = False,
) -> str:
    """Транскрибирует аудиофайл. AssemblyAI primary, Groq fallback.

    :param language: ISO 639-1 код (``'ru'``, ``'en'``…) или ``None`` для авто.
    :param lowercase: вернуть в нижнем регистре (для совместимости со старым кодом).
    """
    last_exc: Exception | None = None

    if ASSEMBLYAI_API_KEY:
        try:
            text = await _transcribe_assemblyai(Path(file_path), language=language)
            return text.lower() if lowercase else text
        except Exception as exc:
            last_exc = exc
            logger.warning(f"AssemblyAI failed, fallback to Groq: {exc}")
    else:
        logger.warning("ASSEMBLYAI_API_KEY не задан — использую Groq как primary")

    try:
        text = await _transcribe_groq(Path(file_path), language=language)
        return text.lower() if lowercase else text
    except Exception as exc:
        if last_exc is None:
            last_exc = exc
        logger.error(f"Все STT-бэкенды упали: AAI={last_exc}, Groq={exc}")
        raise


async def transcribe_audio_groq(file_path: str | Path) -> str:
    """Совместимый алиас. Использует transcribe_audio (AAI primary, Groq fallback).

    Возвращает текст в нижнем регистре, как делал старый код.
    """
    # Старый код передавал language='ru' жёстко; теперь auto-detect.
    return await transcribe_audio(file_path, language=None, lowercase=True)
