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
from urllib.parse import urlparse
from PIL import Image
from dotenv import dotenv_values

from config import YEPAPI_API_KEY
from utils.url_safety import is_safe_public_url

logger = logging.getLogger(__name__)

# Разрешённые хосты для загрузки изображений (S-12: SSRF protection)
_ALLOWED_IMAGE_HOSTS = {"api.telegram.org", "files.catbox.moe", "litter.catbox.moe"}


def _is_safe_image_url(url: str) -> bool:
    """Проверяет, что URL ведёт на доверенный хост."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("https", "http") and parsed.hostname in _ALLOWED_IMAGE_HOSTS
    except Exception:
        return False

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

    if not _is_safe_image_url(image):
        raise ValueError(f"Blocked image URL from untrusted host: {urlparse(image).hostname}")
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
    if not _is_safe_image_url(image):
        raise ValueError(f"Blocked image URL from untrusted host: {urlparse(image).hostname}")
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
        if not _is_safe_image_url(image):
            raise ValueError(f"Blocked image URL from untrusted host: {urlparse(image).hostname}")
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


def _inferall_keys() -> list[str]:
    env_value = os.getenv("INFERALL_API_KEY", "")
    dotenv_value = dotenv_values(Path(__file__).resolve().parents[1] / ".env").get("INFERALL_API_KEY", "")
    raw_keys = dotenv_value or env_value or ""
    keys = [key.strip() for key in raw_keys.split(",") if key.strip()]
    if not keys:
        raise RuntimeError(
            "INFERALL_API_KEY не задан. Укажите ключ(и) через переменную окружения "
            "INFERALL_API_KEY (несколько — через запятую)."
        )
    return keys


def _extract_inferall_images(raw_response: dict) -> tuple[list[bytes], str | None]:
    """Извлекает изображения из ответа InferAll.
    
    Returns:
        (images, text_reply) — список bytes изображений и текстовый ответ модели если был.
    """
    extracted = []
    text_parts = []
    
    # 1. Parse Gemini format (since the gateway returns this model in Gemini format)
    candidates = raw_response.get("candidates", [])
    if candidates and len(candidates) > 0:
        parts = candidates[0].get("content", {}).get("parts", [])
        for part in parts:
            if "inlineData" in part:
                inline_data = part["inlineData"]
                base64_data = inline_data.get("data", "")
                if base64_data:
                    try:
                        extracted.append(base64.b64decode(base64_data))
                    except Exception as e:
                        logger.error(f"Failed to decode base64 inlineData: {e}")
            elif "text" in part:
                text_parts.append(part["text"])

    if not extracted and text_parts:
        text_reply = " ".join(text_parts)
        logger.warning(f"InferAll вернул текстовый ответ вместо картинки: {text_reply[:300]}")
        return [], text_reply
                        
    # 2. Parse OpenAI standard format if returned
    if isinstance(raw_response.get("data"), list):
        for item in raw_response["data"]:
            if isinstance(item, dict):
                image_url_or_b64 = item.get("b64_json") or item.get("url")
                if image_url_or_b64:
                    if image_url_or_b64.startswith("http"):
                        if not is_safe_public_url(image_url_or_b64):
                            logger.warning(f"Пропущен небезопасный URL изображения (SSRF guard): {image_url_or_b64!r}")
                            continue
                        logger.info(f"Downloading generated image from URL: {image_url_or_b64}")
                        try:
                            img_resp = requests.get(image_url_or_b64, timeout=30)
                            if img_resp.status_code == 200:
                                extracted.append(img_resp.content)
                        except Exception as e:
                            logger.error(f"Failed to download image from URL {image_url_or_b64}: {e}")
                    else:
                        try:
                            extracted.append(base64.b64decode(image_url_or_b64))
                        except Exception as e:
                            logger.error(f"Failed to decode base64 OpenAI data: {e}")
                            
    # 3. Parse single output URL/base64 if returned
    elif isinstance(raw_response.get("output"), str):
        output = raw_response.get("output")
        if output.startswith("http"):
            if not is_safe_public_url(output):
                logger.warning(f"Пропущен небезопасный URL изображения (SSRF guard): {output!r}")
                return extracted, None
            try:
                img_resp = requests.get(output, timeout=30)
                if img_resp.status_code == 200:
                    extracted.append(img_resp.content)
            except Exception as e:
                logger.error(f"Failed to download image from output URL {output}: {e}")
        else:
            try:
                extracted.append(base64.b64decode(output))
            except Exception as e:
                logger.error(f"Failed to decode base64 output data: {e}")
                
    return extracted, None


def _resize_image_to_resolution(image_bytes: bytes, resolution: str) -> bytes:
    if not image_bytes:
        return image_bytes
    if resolution == "4K":
        return image_bytes
        
    try:
        from io import BytesIO
        target_max = 1024
        if resolution == "512":
            target_max = 512
        elif resolution == "1K":
            target_max = 1024
        elif resolution == "2K":
            target_max = 2048
        else:
            return image_bytes
            
        img = Image.open(BytesIO(image_bytes))
        width, height = img.size
        
        if max(width, height) <= target_max:
            return image_bytes
            
        if width > height:
            new_w = target_max
            new_h = int(height * (target_max / width))
        else:
            new_h = target_max
            new_w = int(width * (target_max / height))
            
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        
        out = BytesIO()
        resized.save(out, format=img.format or "JPEG", quality=95)
        return out.getvalue()
    except Exception as e:
        logger.error(f"Error resizing image to resolution {resolution}: {e}")
        return image_bytes


def generate_image(
    prompt: str,
    image_urls: list = None,
    aspect_ratio: str = "1:1",
    resolution: str = "1K",
    num_images: int = 1
) -> Optional[Union[str, bytes, list[bytes]]]:
    """Генерирует изображение через InferAll API. Возвращает bytes или list[bytes] изображения."""
    
    def _generate_single() -> Optional[bytes]:
        try:
            has_source_image = bool(image_urls)
            url = "https://api.inferall.ai/ai/v1/generate"

            # Каждая индивидуальная генерация запрашивает ровно 1 картинку
            payload = {
                "provider": "gemini",
                "model": "gemini-3.1-flash-image-preview",
                "operation": "image-edit" if has_source_image else "image-generate",
                "prompt": prompt,
                "config": {
                    "aspectRatio": aspect_ratio,
                    "aspect_ratio": aspect_ratio,
                    "resolution": resolution,
                    "imageSize": resolution,
                    "numberOfImages": 1,
                    "number_of_images": 1
                }
            }

            if has_source_image:
                if len(image_urls) == 1:
                    payload["images"] = [_to_raw_base64(image_urls[0])]
                else:
                    grid_data = _compose_grid(image_urls)
                    payload["images"] = [grid_data["base64"]]
                    grid_hint = (
                        f"[Reference: {len(image_urls)} input images are stitched into a single grid; "
                        f"treat each cell as a separate reference.] "
                    )
                    payload["prompt"] = grid_hint + payload["prompt"]

            last_error = None
            inferall_keys = _inferall_keys()
     
            for key_index, api_key in enumerate(inferall_keys, start=1):
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
            
                try:
                    response = requests.post(url, headers=headers, json=payload, timeout=120)
                    if response.status_code >= 400:
                        logger.error(f"Сырой ответ InferAll (image): {_short_response_text(response)}")
                        last_error = _short_response_text(response)
                        if _is_key_retryable_status(response.status_code):
                            continue
                        response.raise_for_status()
                
                    response.raise_for_status()
                    raw_response = response.json()
                    img_list, text_reply = _extract_inferall_images(raw_response)
                    
                    if img_list:
                        return img_list[0]
                    elif text_reply is not None:
                        # Модель вернула текст вместо картинки — это не временная ошибка, ретраить бессмысленно
                        raise ValueError(f"Модель отказалась генерировать изображение: {text_reply[:200]}")
                    else:
                        logger.error(f"Не удалось декодировать изображения из ответа InferAll. Сырой ответ: {_summarize_for_log(raw_response)}")
                        continue
                        
                except Exception as e:
                    logger.error(f"Ошибка при работе с ключом {key_index} в InferAll: {e}")
                    if key_index == len(inferall_keys):
                        raise e
                    continue
                    
            return None
        except Exception as e:
            logger.error(f"Ошибка при генерации одиночного изображения: {e}")
            raise e

    try:
        if aspect_ratio not in {"1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9"}:
            aspect_ratio = "1:1"
        if resolution not in {"512", "1K", "2K", "4K"}:
            resolution = "1K"

        # Если запрашивается более одной картинки, запускаем параллельные потоки
        if num_images > 1:
            import concurrent.futures
            logger.info(f"Запускаем {num_images} параллельных запросов для генерации изображений через InferAll.")
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_images) as executor:
                futures = [executor.submit(_generate_single) for _ in range(num_images)]
                results = []
                for future in concurrent.futures.as_completed(futures):
                    try:
                        res = future.result()
                        if res:
                            res = _resize_image_to_resolution(res, resolution)
                            results.append(res)
                    except Exception as e:
                        logger.error(f"Ошибка в одном из параллельных потоков генерации: {e}")
            
            if results:
                logger.info(f"Параллельная генерация завершена. Успешно получено {len(results)} из {num_images} изображений.")
                return results
            return None
        else:
            # Одиночная генерация
            logger.info(f"Отправляем задачу в InferAll API (одиночная генерация): {prompt}")
            res = _generate_single()
            if res:
                res = _resize_image_to_resolution(res, resolution)
            return res

    except Exception as e:
        logger.error(f"Ошибка при генерации изображения через InferAll: {e}")
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
