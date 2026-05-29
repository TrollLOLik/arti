"""
Генерация изображений и видео через OmniRoute/FreeTheAI API
"""
import logging
import base64
import time
import os
from pathlib import Path
from typing import Optional, Union

import requests
from PIL import Image
from dotenv import dotenv_values

from config import YEPAPI_API_KEY

logger = logging.getLogger(__name__)

YEPAPI_BASE_URL = "https://api.yepapi.com/v1"
IMAGE_MODEL = "google/nano-banana-3-flash"
VIDEO_MODEL = "openai/sora-2"
IMAGE_TO_VIDEO_MODEL = "openai/sora-2"
VIDEO_MODELS = {
    "seedance": "bytedance/seedance-2-0-fast",
    "veo": "google/veo-3-lite",
    "sora": "openai/sora-2",
}

def _yepapi_keys() -> list[str]:
    env_value = os.getenv("YEPAPI_API_KEY", "")
    dotenv_value = dotenv_values(Path(__file__).resolve().parents[1] / ".env").get("YEPAPI_API_KEY", "")
    raw_keys = dotenv_value or env_value or YEPAPI_API_KEY or ""
    keys = [key.strip() for key in raw_keys.split(",") if key.strip()]
    return keys or [YEPAPI_API_KEY]


def _yepapi_headers(api_key: str) -> dict:
    return {
        "x-api-key": api_key,
        "Content-Type": "application/json"
    }


def _yepapi_status_headers(api_key: str) -> dict:
    return {
        "x-api-key": api_key,
        "Accept": "application/json",
        "Connection": "close"
    }


def _is_key_retryable_status(status_code: int) -> bool:
    return status_code in {401, 402, 403, 429, 500, 502, 503, 504}


def _safe_json(response: requests.Response, label: str) -> Optional[dict]:
    try:
        return response.json()
    except ValueError:
        logger.warning(f"{label}: ответ не JSON. HTTP {response.status_code}. Тело: {response.text[:500]}")
        return None


def _short_response_text(response: requests.Response, limit: int = 1000) -> str:
    text = response.text or ""
    if len(text) > limit:
        return text[:limit] + "...[truncated]"
    return text


def _summarize_for_log(value, depth: int = 0):
    """Рекурсивно усекает длинные строки (base64), чтобы лог не разрывало."""
    if depth > 6:
        return "..."
    if isinstance(value, dict):
        return {k: _summarize_for_log(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > 10:
            head = [_summarize_for_log(v, depth + 1) for v in value[:5]]
            return head + [f"...+{len(value) - 5} more"]
        return [_summarize_for_log(v, depth + 1) for v in value]
    if isinstance(value, str) and len(value) > 200:
        return f"<str len={len(value)}: {value[:80]}...{value[-30:]}>"
    return value


def _extract_failure_reason(job: dict) -> str:
    """Достаёт человекочитаемую причину failed/error из job."""
    if not isinstance(job, dict):
        return "unknown"
    candidates = []
    for key in ("error", "errorMessage", "failureReason", "reason", "message"):
        v = job.get(key)
        if isinstance(v, str) and v.strip():
            candidates.append(f"{key}={v.strip()[:300]}")
        elif isinstance(v, dict):
            inner = v.get("message") or v.get("code")
            if inner:
                candidates.append(f"{key}={str(inner)[:300]}")
    result = job.get("result")
    if isinstance(result, dict):
        for key in ("error", "errorMessage", "reason", "message"):
            v = result.get(key)
            if isinstance(v, str) and v.strip():
                candidates.append(f"result.{key}={v.strip()[:300]}")
    return "; ".join(candidates) or "no error details in job"


def _extract_media_result(job: dict, media_key: str) -> tuple[Optional[str], Optional[str]]:
    result = job.get("result", {}) if isinstance(job, dict) else {}
    media = result.get(media_key, {}) if isinstance(result, dict) else {}
    urls = [
        result.get("url") if isinstance(result, dict) else None,
        media.get("url") if isinstance(media, dict) else None,
        job.get("url") if isinstance(job, dict) else None,
        job.get("outputUrl") if isinstance(job, dict) else None,
    ]
    base64_values = [
        media.get("base64") if isinstance(media, dict) else None,
        result.get("base64") if isinstance(result, dict) else None,
        job.get("base64") if isinstance(job, dict) else None,
    ]
    return next((url for url in urls if url), None), next((value for value in base64_values if value), None)


def _detect_image_mime(image_bytes: bytes, fallback: str = "image/jpeg") -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    return fallback


def _to_data_url(image: Union[str, bytes]) -> str:
    if isinstance(image, str) and image.startswith("data:image/"):
        return image

    if isinstance(image, bytes):
        content_type = _detect_image_mime(image, "image/png")
        return f"data:{content_type};base64,{base64.b64encode(image).decode('utf-8')}"

    response = requests.get(image, timeout=60)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0].lower()
    if not content_type.startswith("image/"):
        content_type = _detect_image_mime(response.content)
    return f"{content_type};base64,{base64.b64encode(response.content).decode('utf-8')}".replace("image/", "data:image/", 1)


def _to_raw_base64(image: Union[str, bytes]) -> str:
    """Возвращает чистый base64 без префикса ``data:...;base64,``.

    YepAPI для multi-image передаёт массив строк напрямую в Gemini API,
    и Gemini ждёт там чистый base64. Префикс ломает декодирование.
    """
    data_url = _to_data_url(image)
    return data_url.split(",", 1)[1]


def _to_image_data(image: Union[str, bytes]) -> dict:
    data_url = _to_data_url(image)
    header, image_base64 = data_url.split(",", 1)
    mime_type = header.removeprefix("data:").split(";")[0]
    return {"mimeType": mime_type, "base64": image_base64}


def _load_image_bytes(image: Union[str, bytes]) -> bytes:
    """Достаёт сырые bytes изображения из любого источника."""
    if isinstance(image, bytes):
        return image
    if isinstance(image, str) and image.startswith("data:image/"):
        return base64.b64decode(image.split(",", 1)[1])
    response = requests.get(image, timeout=60)
    response.raise_for_status()
    return response.content


def _compose_grid(images: list[Union[str, bytes]], max_size: int = 2048, gap: int = 8) -> dict:
    """Склеивает несколько картинок в одну сетку, возвращает {mimeType, base64}.

    YepAPI / nano-banana-3-flash принимает только ОДНУ ``imageData``.
    Чтобы поддержать multi-image edit ("соедини две картинки"), склеиваем
    их в общий канвас, который модель видит как одно изображение и сама
    разбирает по композиции, как это рекомендует Google для Gemini 3.
    """
    if not images:
        raise ValueError("Need at least one image for grid composition")

    from io import BytesIO

    pil_images: list[Image.Image] = []
    for src in images[:14]:
        img_bytes = _load_image_bytes(src)
        img = Image.open(BytesIO(img_bytes)).convert("RGB")
        pil_images.append(img)

    if len(pil_images) == 1:
        # Не должны бы здесь оказаться, но fallback.
        out = BytesIO()
        pil_images[0].save(out, format="JPEG", quality=92)
        return {"mimeType": "image/jpeg", "base64": base64.b64encode(out.getvalue()).decode("utf-8")}

    # Высчитываем сетку: cols = ceil(sqrt(n)), rows = ceil(n/cols)
    import math
    n = len(pil_images)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)

    # Цельная стандартизация: каждая ячейка max_size/cols × max_size/rows
    cell_w = (max_size - gap * (cols + 1)) // cols
    cell_h = (max_size - gap * (rows + 1)) // rows
    if cell_w <= 0 or cell_h <= 0:
        cell_w = cell_h = 512  # fallback

    canvas_w = cell_w * cols + gap * (cols + 1)
    canvas_h = cell_h * rows + gap * (rows + 1)
    canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))

    for idx, img in enumerate(pil_images):
        col = idx % cols
        row = idx // cols
        # Подгоняем под cell, сохраняя пропорции
        img_ratio = img.width / img.height
        cell_ratio = cell_w / cell_h
        if img_ratio > cell_ratio:
            new_w = cell_w
            new_h = int(cell_w / img_ratio)
        else:
            new_h = cell_h
            new_w = int(cell_h * img_ratio)
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        # Центруем в ячейке
        x = gap + col * (cell_w + gap) + (cell_w - new_w) // 2
        y = gap + row * (cell_h + gap) + (cell_h - new_h) // 2
        canvas.paste(resized, (x, y))

    out = BytesIO()
    canvas.save(out, format="JPEG", quality=92)
    return {
        "mimeType": "image/jpeg",
        "base64": base64.b64encode(out.getvalue()).decode("utf-8"),
    }


def _to_video_image_data(image: Union[str, bytes]) -> dict:
    image_data = _to_image_data(image)
    if image_data["mimeType"] not in {"image/png", "image/jpeg", "image/webp"}:
        logger.warning(f"Неподдерживаемый MIME для image-to-video: {image_data['mimeType']}. Отправляем как image/jpeg.")
        image_data["mimeType"] = "image/jpeg"
    return image_data


def _to_sora_image_data(image: Union[str, bytes], aspect_ratio: str = "16:9") -> dict:
    width, height = (720, 1280) if aspect_ratio == "9:16" else (1280, 720)
    if isinstance(image, bytes):
        image_bytes = image
    elif isinstance(image, str) and image.startswith("data:image/"):
        image_bytes = base64.b64decode(image.split(",", 1)[1])
    else:
        response = requests.get(image, timeout=60)
        response.raise_for_status()
        image_bytes = response.content

    from io import BytesIO

    with Image.open(BytesIO(image_bytes)) as img:
        img = img.convert("RGB")
        source_width, source_height = img.size
        target_ratio = width / height
        source_ratio = source_width / source_height

        if source_ratio > target_ratio:
            new_width = int(source_height * target_ratio)
            left = (source_width - new_width) // 2
            img = img.crop((left, 0, left + new_width, source_height))
        elif source_ratio < target_ratio:
            new_height = int(source_width / target_ratio)
            top = (source_height - new_height) // 2
            img = img.crop((0, top, source_width, top + new_height))

        img = img.resize((width, height), Image.LANCZOS)
        output = BytesIO()
        img.save(output, format="JPEG", quality=95)

    return {"mimeType": "image/jpeg", "base64": base64.b64encode(output.getvalue()).decode("utf-8")}


def generate_image(
    prompt: str,
    image_urls: list = None,
    aspect_ratio: str = "1:1",
    resolution: str = "1K"
) -> Optional[Union[str, bytes]]:
    """Генерирует изображение через YepAPI media queue. Возвращает URL или bytes изображения."""
    try:
        has_source_image = bool(image_urls)
        if aspect_ratio not in {"1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9"}:
            aspect_ratio = "1:1"
        if resolution not in {"512", "1K", "2K", "4K"}:
            resolution = "1K"
        queue_url = f"{YEPAPI_BASE_URL}/media/queue"

        payload = {
            "model": IMAGE_MODEL,
            "prompt": prompt,
            "options": {"aspectRatio": aspect_ratio, "resolution": resolution}
        }

        if has_source_image:
            if len(image_urls) == 1:
                # Single-image: API принимает объект {mimeType, base64}.
                payload["imageData"] = _to_image_data(image_urls[0])
            else:
                # Multi-image: YepAPI / nano-banana-3-flash в реальности
                # принимает только ОДНО ``imageData``. Склеиваем все
                # пользовательские картинки в одну сетку и подаём как
                # single image — модель сама разбирает композицию.
                payload["imageData"] = _compose_grid(image_urls)
                grid_hint = (
                    f"[Reference: {len(image_urls)} input images are stitched into a single grid; "
                    f"treat each cell as a separate reference.] "
                )
                payload["prompt"] = grid_hint + payload["prompt"]
                logger.info(
                    f"YepAPI image: склеил {len(image_urls)} картинок в одну сетку "
                    f"(base64_len={len(payload['imageData']['base64'])})"
                )

        logger.info(f"Отправляем задачу в YepAPI media queue ({'edit' if has_source_image else 'generation'}, {IMAGE_MODEL}, images={len(image_urls) if image_urls else 0}): {prompt}")
        if has_source_image:
            if isinstance(payload["imageData"], list):
                logger.info(f"YepAPI image: {len(payload['imageData'])} reference images (raw base64 strings)")
            elif isinstance(payload["imageData"], dict):
                logger.info(
                    f"YepAPI image imageData mimeType={payload['imageData']['mimeType']}, "
                    f"base64_len={len(payload['imageData']['base64'])}"
                )

        last_error = None
 
        for key_index, api_key in enumerate(_yepapi_keys(), start=1):
            headers = _yepapi_headers(api_key)
            logger.info(f"YepAPI image: пробуем ключ {key_index}/{len(_yepapi_keys())}")
        
            response = requests.post(queue_url, headers=headers, json=payload, timeout=60)
            logger.info(f"Ответ YepAPI queue: HTTP {response.status_code}")
        
            if response.status_code >= 400:
                logger.error(f"Сырой ответ YepAPI queue (image): {_short_response_text(response)}")
                last_error = _short_response_text(response)
        
                if _is_key_retryable_status(response.status_code):
                    continue
        
                response.raise_for_status()
        
            response.raise_for_status()
            data = _safe_json(response, "YepAPI image queue")
            if not data:
                continue
            job_id = data.get("data", {}).get("jobId")
        
            if not job_id:
                logger.error(f"YepAPI не вернул jobId. Структура JSON: {data}")
                continue
        
            logger.info(f"YepAPI image job создан: {job_id}")
            status_url = f"{YEPAPI_BASE_URL}/media/status/{job_id}"
            status_headers = _yepapi_status_headers(api_key)
            poll_delay = 5
            max_polls = 120

            for attempt in range(max_polls):
                status_response = requests.get(status_url, headers=status_headers, timeout=60)
                logger.info(
                    f"YepAPI image job {job_id}: HTTP {status_response.status_code} "
                    f"content_type={status_response.headers.get('Content-Type')} body_len={len(status_response.content)} "
                    f"(poll {attempt + 1}/{max_polls})"
                )
                if status_response.status_code >= 400:
                    logger.error(f"Сырой ответ YepAPI status (image): {_short_response_text(status_response)}")
                status_response.raise_for_status()

                status_data = _safe_json(status_response, "YepAPI image status")
                if not status_data:
                    time.sleep(poll_delay)
                    continue
                job = status_data.get("data", {})
                status = job.get("status")
                logger.info(f"YepAPI image job {job_id}: status={status}")

                if status == "completed":
                    result_url, result_b64 = _extract_media_result(job, "image")

                    if result_url:
                        logger.info(f"Изображение успешно сгенерировано: {result_url}")
                        return result_url
                    if result_b64:
                        logger.info("Изображение успешно сгенерировано в формате base64")
                        return base64.b64decode(result_b64)

                    logger.error(f"YepAPI completed без изображения. Job: {_summarize_for_log(job)}")
                    return None

                if status == "failed":
                    logger.error(
                        f"YepAPI image job failed. Причина: {_extract_failure_reason(job)}. "
                        f"Job: {_summarize_for_log(job)}"
                    )
                    return None

                time.sleep(poll_delay)

            logger.error(f"YepAPI image job {job_id} не завершился за {max_polls * poll_delay} секунд")
            return None

    except Exception as e:
        logger.error(f"Ошибка при генерации изображения: {e}")
        raise e


def generate_video(
    prompt: str,
    image_urls: list = None,
    model: str = None,
    duration: str = "4",
    aspect_ratio: str = "16:9"
) -> Optional[Union[str, bytes]]:
    """Генерирует видео через YepAPI media queue. Возвращает URL или bytes видео."""
    try:
        has_source_image = bool(image_urls)
        video_model = model or (IMAGE_TO_VIDEO_MODEL if has_source_image else VIDEO_MODEL)
        duration = str(duration)
        if duration not in {"4", "8", "12", "16", "20"}:
            duration = "8"
        if has_source_image and duration != "8":
            duration = "8"
        if aspect_ratio not in {"16:9", "9:16"}:
            aspect_ratio = "16:9"
        queue_url = f"{YEPAPI_BASE_URL}/media/queue"

        payload = {
            "model": video_model,
            "prompt": prompt,
            "options": {
                "aspectRatio": aspect_ratio,
                "duration": duration
            }
        }

        if video_model in {"google/veo-3-lite", "openai/sora-2"}:
            payload["options"]["resolution"] = "720p"

        if has_source_image:
            if video_model == "openai/sora-2":
                payload["imageData"] = _to_sora_image_data(image_urls[0], aspect_ratio)
            else:
                payload["imageData"] = _to_video_image_data(image_urls[0])

        logger.info(f"Отправляем задачу в YepAPI media queue ({'image-to-video' if has_source_image else 'text-to-video'}, {video_model}): {prompt}")
        logger.info(f"YepAPI video options: {payload['options']}")
        if has_source_image:
            logger.info(f"YepAPI video imageData mimeType={payload['imageData']['mimeType']}, base64_len={len(payload['imageData']['base64'])}")

        yepapi_keys = _yepapi_keys()
        for key_index, api_key in enumerate(yepapi_keys, start=1):
            headers = _yepapi_headers(api_key)
            logger.info(f"YepAPI video: пробуем ключ {key_index}/{len(yepapi_keys)}")

            response = requests.post(queue_url, headers=headers, json=payload, timeout=60)
            logger.info(f"Ответ YepAPI video queue: HTTP {response.status_code}")
            if response.status_code >= 400:
                logger.error(f"Сырой ответ YepAPI video queue: {_short_response_text(response)}")
                if response.status_code == 400:
                    return None
                if _is_key_retryable_status(response.status_code):
                    continue
            response.raise_for_status()

            data = _safe_json(response, "YepAPI video queue")
            if not data:
                continue
            job_id = data.get("data", {}).get("jobId")
            if not job_id:
                logger.error(f"YepAPI video не вернул jobId. Структура JSON: {data}")
                continue

            logger.info(f"YepAPI video job создан: {job_id}")
            status_url = f"{YEPAPI_BASE_URL}/media/status/{job_id}"
            status_headers = _yepapi_status_headers(api_key)
            poll_delay = 5
            max_polls = 240
            attempt = 0
            empty_status_count = 0
            max_empty_status = 12

            while attempt < max_polls:
                status_response = requests.get(status_url, headers=status_headers, timeout=60)
                logger.info(
                    f"YepAPI video job {job_id}: HTTP {status_response.status_code} "
                    f"content_type={status_response.headers.get('Content-Type')} body_len={len(status_response.content)} "
                    f"(poll {attempt + 1}/{max_polls})"
                )
                if status_response.status_code >= 400:
                    logger.error(f"Сырой ответ YepAPI video status: {_short_response_text(status_response)}")
                status_response.raise_for_status()

                status_data = _safe_json(status_response, "YepAPI video status")
                if not status_data:
                    empty_status_count += 1
                    logger.warning(f"YepAPI video job {job_id}: пустой status ответ #{empty_status_count}, продолжаю ждать")
                    if empty_status_count >= max_empty_status:
                        logger.error(
                            f"YepAPI video job {job_id}: status endpoint вернул {empty_status_count} пустых ответов подряд. "
                            f"Останавливаю polling без создания нового job. Проверь вручную: {status_url}"
                        )
                        return None
                    time.sleep(min(poll_delay * 2, 15))
                    continue
                empty_status_count = 0
                attempt += 1
                job = status_data.get("data", {})
                status = job.get("status")
                logger.info(f"YepAPI video job {job_id}: status={status}")

                if status == "completed":
                    result_url, result_b64 = _extract_media_result(job, "video")

                    if result_url:
                        logger.info(f"Видео успешно сгенерировано: {result_url}")
                        return result_url
                    if result_b64:
                        logger.info("Видео успешно сгенерировано в формате base64")
                        return base64.b64decode(result_b64)

                    logger.error(f"YepAPI video completed без видео. Job: {_summarize_for_log(job)}")
                    return None

                if status == "failed":
                    logger.error(
                        f"YepAPI video job failed. Причина: {_extract_failure_reason(job)}. "
                        f"Job: {_summarize_for_log(job)}"
                    )
                    return None

                time.sleep(poll_delay)

            logger.error(f"YepAPI video job {job_id} не завершился за {max_polls * poll_delay} секунд")
            return None

        return None

    except Exception as e:
        logger.error(f"Ошибка при генерации видео: {type(e).__name__}: {str(e)[:1000]}")
        raise e
