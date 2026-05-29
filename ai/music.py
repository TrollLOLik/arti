"""
Генерация музыки через Airforce API (Suno v5)
"""
import subprocess
import json
import logging
from typing import Optional

import requests

from config import AIRFORCE_TOKEN

logger = logging.getLogger(__name__)


def prepare_audio_with_cover(video_path, audio_output, cover_output):
    """Вырезает аудио и делает квадратную обложку из видео."""
    try:
        # Извлекаем первый кадр, делаем его квадратным 320x320
        subprocess.run([
            'ffmpeg', '-y', '-i', video_path,
            '-ss', '00:00:00', '-vframes', '1',
            '-vf', 'scale=320:320:force_original_aspect_ratio=increase,crop=320:320',
            cover_output
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Извлекаем аудио в MP3
        subprocess.run([
            'ffmpeg', '-y', '-i', video_path,
            '-vn', '-acodec', 'libmp3lame', '-q:a', '2', 
            audio_output
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        logger.error(f"Ошибка конвертации музыки: {e}")
        return False


def generate_music(prompt: str, instrumental: bool = False, style: str = "Pop") -> Optional[str]:
    """Генерирует музыку через Airforce API (Suno v5) с SSE-стримингом."""
    try:
        url = "https://api.airforce/v1/images/generations"
        headers = {
            "Authorization": f"Bearer {AIRFORCE_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        payload = {
            "model": "suno-v5.5",
            "n": 1,
            "size": "1024x1024",
            "response_format": "url",
            "sse": True,
            "custom": True,
            "instrumental": instrumental,
            "style": style,
        }

        if prompt:
            payload["prompt"] = prompt
        else:
            payload["prompt"] = "Instrumental music" if instrumental else "Vocalizing melody"

        logger.info(f"Генерация музыки (SSE): prompt='{prompt[:80]}...', style='{style}', instrumental={instrumental}")

        result_url = None
        last_data = None
        last_raw_line = None
        lines_received = 0

        with requests.post(url, headers=headers, json=payload, stream=True, timeout=600) as response:
            logger.debug(f"HTTP статус: {response.status_code}, Content-Type: {response.headers.get('Content-Type', '?')}")

            if response.status_code in (429, 403, 503):
                raw = response.text[:500]
                logger.error(f"Лимит или ошибка API ({response.status_code}): {raw}")
                raise Exception(f"API_LIMIT_{response.status_code}: {raw}")
            
            if response.status_code != 200:
                raw = response.text[:500]
                logger.error(f"Ошибка API ({response.status_code}): {raw}")
                return None

            for line in response.iter_lines():
                if not line:
                    continue

                lines_received += 1
                line_str = line.decode("utf-8")
                last_raw_line = line_str
                
                if lines_received == 1:
                    logger.info(f"Первая SSE строка: {line_str[:500]}")

                # Пропускаем keepalive и завершающие маркеры
                if line_str in ("data: [DONE]", "data: : keepalive", ": keepalive"):
                    continue

                if line_str.startswith("data: "):
                    try:
                        data = json.loads(line_str[6:])
                        last_data = data

                        # Ищем URL в разных форматах ответа
                        if "data" in data and isinstance(data["data"], list) and data["data"]:
                            candidate = data["data"][0].get("url") or data["data"][0].get("audio_url")
                            if candidate:
                                result_url = candidate
                        elif "url" in data:
                            result_url = data["url"]
                        elif "audio_url" in data:
                            result_url = data["audio_url"]

                    except json.JSONDecodeError:
                        logger.warning(f"Не удалось распарсить SSE строку: {line_str[:500]}")
                        continue

        logger.info(f"SSE завершён. Строк получено: {lines_received}. last_data: {last_data}. last_raw_line: {last_raw_line[:500] if last_raw_line else None}")

        if result_url:
            logger.info(f"Получен URL музыки: {result_url}")
            return result_url
        else:
            logger.error(f"URL музыки не найден. Строк SSE получено: {lines_received}. Последний пакет: {last_data}. Последняя строка: {last_raw_line[:500] if last_raw_line else None}")
            return None

    except Exception as e:
        logger.error(f"Ошибка при генерации музыки: {e}")
        raise e
