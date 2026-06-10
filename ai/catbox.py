from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from config import CATBOX_USERHASH

logger = logging.getLogger(__name__)

CATBOX_API_URL = "https://catbox.moe/user/api.php"
CATBOX_TIMEOUT = 120.0


@dataclass(frozen=True)
class CatboxUploadResult:
    url: str
    file_id: str
    managed: bool


async def upload_file(path: Path | str) -> CatboxUploadResult:
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError(f"Файл не найден: {file_path}")

    file_bytes = await asyncio.to_thread(file_path.read_bytes)
    userhash = CATBOX_USERHASH.strip(' "')
    if userhash.lower() in ("none", "null", "false", ""):
        userhash = ""

    data = {"reqtype": "fileupload"}
    if userhash:
        data["userhash"] = userhash

    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=CATBOX_TIMEOUT, trust_env=False) as client:
        for attempt in range(1, 4):
            try:
                files = {"fileToUpload": (file_path.name, file_bytes, "audio/wav")}
                response = await client.post(CATBOX_API_URL, data=data, files=files)
                text = response.text.strip()
                if response.status_code >= 500 or response.status_code == 429:
                    last_error = RuntimeError(f"Catbox HTTP {response.status_code}: {text[:300]}")
                elif response.status_code >= 400:
                    raise RuntimeError(f"Catbox HTTP {response.status_code}: {text[:300]}")
                elif not text.startswith("https://files.catbox.moe/"):
                    raise RuntimeError(f"Catbox вернул неожиданный ответ: {text[:300]}")
                else:
                    return CatboxUploadResult(
                        url=text,
                        file_id=text.rsplit("/", 1)[-1],
                        managed=bool(userhash),
                    )
            except (httpx.HTTPError, RuntimeError) as exc:
                last_error = exc

            if attempt < 3:
                await asyncio.sleep(min(2 ** attempt, 8))

    if last_error is not None:
        if not userhash:
            logger.warning(
                f"Catbox anonymous upload failed: {last_error}. "
                "Attempting fallback to Litterbox (72h temporary storage)..."
            )
            try:
                litterbox_url = "https://litterbox.catbox.moe/resources/internals/api.php"
                litterbox_data = {"reqtype": "fileupload", "time": "72h"}
                async with httpx.AsyncClient(timeout=CATBOX_TIMEOUT, trust_env=False) as client:
                    files = {"fileToUpload": (file_path.name, file_bytes, "audio/wav")}
                    response = await client.post(litterbox_url, data=litterbox_data, files=files)
                    text = response.text.strip()
                    if response.status_code == 200 and text.startswith("https://litter.catbox.moe/"):
                        logger.info(f"Litterbox fallback upload succeeded: {text}")
                        return CatboxUploadResult(
                            url=text,
                            file_id=text.rsplit("/", 1)[-1],
                            managed=False,
                        )
                    else:
                        logger.error(f"Litterbox fallback failed: {response.status_code} {text[:300]}")
            except Exception as e:
                logger.error(f"Litterbox fallback failed with exception: {e}")

        raise RuntimeError(f"Catbox upload failed: {last_error}") from last_error
    raise RuntimeError("Catbox upload failed")


async def delete_file(file_id: str | None) -> bool:
    userhash = CATBOX_USERHASH.strip(' "')
    if userhash.lower() in ("none", "null", "false", ""):
        userhash = ""

    if not userhash or not file_id:
        return False

    data = {
        "reqtype": "deletefiles",
        "userhash": userhash,
        "files": file_id,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            response = await client.post(CATBOX_API_URL, data=data)
            text = response.text.strip().lower()
            if response.status_code >= 400:
                logger.warning("Catbox delete HTTP %s: %s", response.status_code, response.text[:300])
                return False
            return "success" in text or "deleted" in text
    except httpx.HTTPError as exc:
        logger.warning("Catbox delete failed: %s", exc)
        return False


async def download_file(url: str, target_path: Path | str) -> Path:
    path = Path(target_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None

    # Попытка 1: httpx со стандартными настройками (trust_env по умолчанию True,
    # чтобы использовать SSL-сертификаты Conda на Windows).
    try:
        async with httpx.AsyncClient(timeout=CATBOX_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            await asyncio.to_thread(path.write_bytes, response.content)
        return path
    except Exception as exc:
        last_error = exc
        logger.debug("httpx download failed for %s: %s", url, exc)

    # Попытка 2: aiohttp fallback (лучше работает с Cloudflare на Windows).
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=CATBOX_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                content = await response.read()
                await asyncio.to_thread(path.write_bytes, content)
        return path
    except Exception as exc:
        last_error = exc
        logger.debug("aiohttp download failed for %s: %s", url, exc)

    # Попытка 3: curl.exe fallback (отлично обходит локальные проблемы со SSL/прокси в Python на Windows)
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl.exe", "-L", "-sS", "-o", str(path), url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0 and path.exists() and path.stat().st_size > 0:
            logger.debug("curl.exe fallback download succeeded for %s", url)
            return path
        err_msg = stderr.decode(errors="replace").strip()
        logger.warning("curl.exe download failed for %s: exit=%s, stderr=%s", url, proc.returncode, err_msg)
    except Exception as exc:
        logger.warning("curl.exe download failed with exception for %s: %s", url, exc)

    if last_error is not None:
        raise last_error
    raise RuntimeError("Все попытки скачать файл провалились.")
