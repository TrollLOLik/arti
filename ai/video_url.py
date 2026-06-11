"""
Утилиты для работы с видео по URL (без videotrans-пайплайна):
скачивание audio-only через yt-dlp, транскрипция через AssemblyAI
(с Groq fallback) и краткий конспект через генеративную модель.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from ai.generation import generate_response_stream

logger = logging.getLogger(__name__)


# Видео-хостинги, явно поддерживаемые yt-dlp. Для них принимаем "обычные" ссылки
# даже если URL вынесен на отдельную строку.
_KNOWN_VIDEO_HOSTS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "vimeo.com", "tiktok.com", "twitch.tv",
    "instagram.com", "facebook.com", "fb.watch",
    "twitter.com", "x.com",
    "rutube.ru", "vk.com", "vk.ru",
    "dailymotion.com", "soundcloud.com",
)

_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def find_first_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = _URL_RE.search(text)
    return m.group(0).rstrip(".,;)") if m else None


def is_message_only_url(text: str) -> bool:
    """Сообщение состоит только из URL (плюс возможные пробелы/перевод строки)."""
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    m = _URL_RE.fullmatch(stripped)
    return m is not None


def is_known_video_url(url: str) -> bool:
    """URL принадлежит известному видеохостингу или указывает на видеофайл."""
    if not url:
        return False

    # VAL-03: сверяем именно HOSTNAME, а не подстроку во всём URL — иначе
    # https://evil.com/?x=youtube.com и https://youtube.com.evil.io/... считались бы
    # «известным видеохостингом».
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if host:
        host = host.lstrip(".")
        for known in _KNOWN_VIDEO_HOSTS:
            k = known.lower()
            if host == k or host.endswith("." + k):
                return True

    # Прямые ссылки на видеофайлы
    if re.search(r"\.(mp4|webm|mkv|mov|m4v|avi)(?:\?|$)", url.lower()):
        return True
    return False


async def download_audio_for_url(url: str, work_dir: Path) -> Path:
    """
    Скачивает только аудиодорожку для URL через yt-dlp.
    Возвращает путь к получившемуся аудиофайлу.
    """
    from utils.url_safety import is_safe_public_url_async

    if not await is_safe_public_url_async(url):
        raise ValueError(f"Недопустимый или небезопасный URL для загрузки: {url!r}")

    work_dir.mkdir(parents=True, exist_ok=True)

    def _do_download() -> Path:
        import yt_dlp

        outtmpl = str(work_dir / "audio.%(ext)s")
        options = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",
                }
            ],
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(url, download=True)

        candidates = sorted(
            [p for p in work_dir.glob("audio.*") if p.is_file() and p.suffix.lower() != ".part"],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise FileNotFoundError(f"yt-dlp не создал аудиофайл в {work_dir}")
        # Предпочитаем mp3, если он есть
        mp3 = next((p for p in candidates if p.suffix.lower() == ".mp3"), None)
        return mp3 or candidates[0]

    return await asyncio.to_thread(_do_download)


async def transcribe_url_audio(audio_path: Path, language: Optional[str] = None) -> str:
    """Транскрибирует аудиофайл. AssemblyAI primary, Groq fallback.

    ``language=None`` — автоопределение языка.
    """
    from ai.stt import transcribe_audio
    return await transcribe_audio(audio_path, language=language, lowercase=False)


async def summarize_transcript(
    chat_id: int,
    user_name: str,
    transcript: str,
    url: str,
) -> str:
    """
    Просит ИИ сделать краткий конспект транскрипта в характере Арти.
    """
    # Обрезаем чрезмерно длинный транскрипт — для лёгкой модели контекста хватит
    snippet = transcript.strip()
    if len(snippet) > 30000:
        snippet = snippet[:30000] + "\n\n[... транскрипт обрезан ...]"

    system_prompt = (
        "Ты — Арти. Сделай ёмкий, структурный конспект видео по транскрипту. "
        "Формат:\n"
        "1) Одна строка <b>о чём это</b> (2-3 предложения).\n"
        "2) Список из 5-9 пунктов <b>ключевые идеи</b>.\n"
        "3) Если есть — короткий блок <b>выводы/итог</b> (2-3 пункта).\n"
        "Никаких приветствий и комментариев в роли. HTML-разметка: <b>, <i>, <blockquote>. "
        "Не используй markdown. Уважай факты в транскрипте, не выдумывай."
    )

    prompt = (
        f"Источник: {url}\n\n"
        f"Транскрипт видео:\n---\n{snippet}\n---\n\n"
        f"Сделай конспект."
    )

    response_text, _used_search, _links, _imgs = await generate_response_stream(
        chat_id=chat_id,
        prompt=prompt,
        user_name=user_name,
        chat_context="",
        model="gemini-3.1-flash-lite-preview",
        temperature=0.4,
        custom_system_prompt=system_prompt,
    )
    return (response_text or "").strip()
