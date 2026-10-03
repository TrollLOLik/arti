"""
Text-to-Speech для Арти.

Иерархия бэкендов (по убыванию качества):
  1. **VoxCPM2 Demo Space** (HuggingFace ``openbmb/VoxCPM-Demo``) —
     основной путь. Через gradio_client, отличное качество.
  2. **Локальный VoxCPM2** (``localhost:8000/generate``) — второй
     фоллбэк, если Demo Space недоступен/упал/HF-rate-limit.

Различия по передаче direction в этих бэкендах:
  * Demo Space — ``control_instruction`` это **отдельный параметр**,
    содержит сырое описание без скобок (``warm aristocratic tone``).
  * Локальный VoxCPM — ``(...)`` встраивается в начало ``text``.

VoxCPM использует ДВЕ системы скобок:
  * ``(...)`` — Control Instruction (общая интонация).
  * ``[...]`` — нативные невербальные теги: ``[laughing]``, ``[sigh]``,
    ``[Uhm]``, ``[Shh]``, ``[Question-ah/ei/en/oh]``,
    ``[Surprise-wa/yo]``, ``[Dissatisfaction-hnn]``.
    https://voxcpm.readthedocs.io/en/latest/cookbook.html

Карточка Арти выдаёт описательные маркеры в ``[...]``
(``[warm aristocratic tone]``, ``[whispering, barely audible]``).
Они конвертируются в Control Instruction; нативные теги остаются на
своих местах.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import logging
from typing import Optional
from pathlib import Path

from bs4 import BeautifulSoup

from config import TTS_ENABLED

logger = logging.getLogger(__name__)

# --- VoxCPM2 Demo Space (основной путь) ---
VOXCPM_DEMO_SPACE = os.getenv("VOXCPM_DEMO_SPACE", "openbmb/VoxCPM-Demo")
VOXCPM_DEMO_CFG = float(os.getenv("VOXCPM_DEMO_CFG", "2.0"))
VOXCPM_DEMO_DENOISE = os.getenv("VOXCPM_DEMO_DENOISE", "0").strip() not in ("0", "false", "False", "")
VOXCPM_DEMO_NORMALIZE = os.getenv("VOXCPM_DEMO_NORMALIZE", "0").strip() not in ("0", "false", "False", "")
VOXCPM_DEMO_TIMEOUT = float(os.getenv("VOXCPM_DEMO_TIMEOUT", "180"))
VOXCPM_CLONE_MODE = os.getenv("VOXCPM_CLONE_MODE", "reference")

# --- VoxCPM2 локальный (второй фоллбэк) ---
VOXCPM_LOCAL_URL = os.getenv("VOXCPM_URL", "http://localhost:8000/generate")
VOXCPM_LOCAL_CFG = os.getenv("VOXCPM_CFG_VALUE", "2.0")
VOXCPM_LOCAL_STEPS = os.getenv("VOXCPM_INFERENCE_TIMESTEPS", "10")
VOXCPM_LOCAL_MAX_LENGTH = os.getenv("VOXCPM_MAX_LENGTH", "2048")
VOXCPM_LOCAL_TIMEOUT = float(os.getenv("VOXCPM_TIMEOUT", "120"))

# --- Референс для zero-shot voice cloning (общий для всех бекендов) ---
REF_PATH = Path("sample.wav")
PROMPT_TEXT = (
    "[laughing] Не бойтесь. Послушайте. Разве вы не сами меня пригласили? "
    "Эта паутина так хрупка, порвётся от прикосновения. "
    "Но, может, вас сломать ещё легче? [laughing] Проверим? [sigh] "
    "А-а... Сейчас узнаем, где вы прячетесь. "
    "Как думаешь, что нас ждёт в финале?"
)

# --- VoxCPM native non-verbal tags ---
# Согласно cookbook: https://voxcpm.readthedocs.io/en/latest/cookbook.html
# Эти теги остаются в квадратных скобках ВНУТРИ текста как невербальные
# звуки. Всё остальное (описания тона, голоса, манеры) уходит в
# Control Instruction в начале строки.
_NATIVE_VOXCPM_TAGS = {
    # Laughs and sighs
    "laughing", "laugh", "sigh", "sighing",
    # Pauses and thinking
    "uhm", "umm", "uh", "hmm", "hm", "shh",
    # Questions
    "question-ah", "question-ei", "question-en", "question-oh",
    # Emotions
    "surprise-wa", "surprise-yo", "dissatisfaction-hnn",
}


def _is_native_voxcpm_tag(content: str) -> bool:
    """Проверяет, является ли содержимое ``[...]`` нативным VoxCPM-тегом."""
    return content.strip().lower() in _NATIVE_VOXCPM_TAGS


# --- Лимит длины сегмента (символы) ---
# VoxCPM теряет качество и ускоряется на длинных фрагментах (>~10-15 с).
# Дробим на предложения, чтобы каждый кусок был коротким.
_MAX_SEGMENT_CHARS = int(os.getenv("VOXCPM_MAX_SEGMENT_CHARS", "200"))


def _split_body_sentences(body: str) -> list[str]:
    """Разбивает текст по границам предложений.

    Возвращает список фрагментов, каждый не длиннее ``_MAX_SEGMENT_CHARS``
    (если одно предложение длиннее — оставляет как есть).
    Нативные VoxCPM-теги ``[...]`` сохраняются на своих местах.
    """
    if not body or len(body) <= _MAX_SEGMENT_CHARS:
        return [body] if body else []

    # Разделяем по .!?…;—  с сохранением пунктуации
    raw_parts = re.split(r'(?<=[.!?…;—])\s+', body)

    chunks: list[str] = []
    current = ""
    for part in raw_parts:
        candidate = f"{current} {part}".strip() if current else part
        if len(candidate) <= _MAX_SEGMENT_CHARS or not current:
            current = candidate
        else:
            chunks.append(current)
            current = part
    if current:
        chunks.append(current)

    return [c for c in chunks if c.strip()]


# ============================================================================
# Подготовка текста
# ============================================================================

def _split_directions_and_native(text: str) -> tuple[list[str], str]:
    """Разбирает текст: возвращает (descriptive_dirs, body).

    * descriptive_dirs — список описательных маркеров (``warm aristocratic
      tone``, ``whispering, barely audible``), которые нужно собрать в один
      Control Instruction в начале.
    * body — текст с сохранёнными нативными VoxCPM-тегами в ``[...]``
      (``[laughing]``, ``[sigh]``, ``[Question-ah]`` и т.п.).

    Описательные маркеры вырезаются из тела, чтобы не дублироваться.
    """
    if not text:
        return [], ""

    descriptive: list[str] = []

    def _replace(match: re.Match) -> str:
        content = match.group(1).strip()
        if not content:
            return ""
        if _is_native_voxcpm_tag(content):
            # Нативный тег — остаётся в [...] на исходном месте
            return f"[{content}]"
        # Описательное — вырезаем из тела, копим в шапку
        descriptive.append(content)
        return ""

    body = re.sub(r"\[([^\]]+)\]", _replace, text)
    return descriptive, body


def _split_first_direction(text: str) -> tuple[str, str]:
    """Возвращает ``(direction_with_parens, body)`` если в начале есть
    Control Instruction в ``(...)``. Иначе ``('', text)``."""
    if not text:
        return "", ""
    m = re.match(r"\s*(\([^)]+\))\s*(.*)", text, flags=re.DOTALL)
    if m:
        return m.group(1), m.group(2).strip()
    return "", text.strip()


def _strip_inline_directions(text: str) -> str:
    """Удаляет ВСЕ ``(...)`` из строки. Используется как страховка после
    того, как мы уже вытащили ведущий direction отдельно — любые
    оставшиеся ``(...)`` посреди текста модель прочитает вслух."""
    return re.sub(r"\([^)]+\)", "", text or "").strip()


def _clean_quotes_and_spaces(text: str) -> str:
    """Снимает кавычки-ёлочки, markdown-звёздочки, схлопывает пробелы."""
    if not text:
        return ""
    text = text.replace("*", "").replace("«", "").replace("»", "").strip()
    return re.sub(r"\s+", " ", text).strip()


def _extract_blockquote_segments(raw: str) -> list[str]:
    """Возвращает список фраз внутри ``<blockquote>`` в исходном порядке.

    Пустые отбрасываются. Если ``<blockquote>`` нет — возвращает один
    элемент: весь сырой текст без HTML.
    """
    if not raw or not raw.strip():
        return []

    try:
        soup = BeautifulSoup(raw, "html.parser")
        quotes = [q.get_text(separator=" ", strip=True) for q in soup.find_all("blockquote")]
        if quotes:
            return [q for q in quotes if q]
    except Exception as exc:
        logger.warning(f"Ошибка парсинга HTML для TTS: {exc}")

    # Фоллбек: ручная регексп-вырезка
    quotes = re.findall(r"<blockquote>(.*?)</blockquote>", raw, re.DOTALL | re.IGNORECASE)
    if quotes:
        return [re.sub(r"<[^>]+>", "", q).strip() for q in quotes if q.strip()]

    plain = re.sub(r"<[^>]+>", "", raw).strip()
    return [plain] if plain else []


def _prepare_segments(raw: str) -> list[str]:
    """Готовит список финальных строк для подачи в локальный VoxCPM2.

    Каждая строка имеет вид:
    ``(описательный_direction) тело [native_tag] продолжение``
    """
    segments: list[str] = []
    for direction, body in _prepare_segment_pairs(raw):
        if direction:
            segments.append(f"({direction}) {body}")
        else:
            segments.append(body)
    return segments


def _prepare_segment_pairs(raw: str) -> list[tuple[str, str]]:
    """Готовит список пар ``(direction, body)`` — для Demo Space, где
    Control Instruction передаётся отдельным параметром.

    * ``direction`` — описательный маркер БЕЗ скобок (``"warm
      aristocratic tone"``) или пустая строка.
    * ``body`` — текст с сохранёнными нативными VoxCPM-тегами в ``[...]``.
    """
    pairs: list[tuple[str, str]] = []
    for q in _extract_blockquote_segments(raw):
        descriptive, body = _split_directions_and_native(q)
        body = _clean_quotes_and_spaces(body)
        body = _strip_inline_directions(body)
        body = _clean_quotes_and_spaces(body)
        if not body:
            continue
        direction = ", ".join(descriptive) if descriptive else ""
        # Дробим длинный текст на предложения — каждый чанк получает
        # тот же direction (Control Instruction).
        for chunk in _split_body_sentences(body):
            pairs.append((direction, chunk))
    return pairs


def _extract_speech_text(raw: str) -> str:
    """Совместимый со старой логикой однострочный текст. Используется
    только для логов и фоллбека на одиночный запрос."""
    return " ".join(_prepare_segments(raw)).strip()


# ============================================================================
# VoxCPM2 Demo Space backend (основной путь)
# ============================================================================

# Persistent gradio_client + lock — Demo Space держим один на процесс.
_demo_client = None
_demo_client_lock = threading.Lock()


def _get_demo_client(force_reconnect: bool = False):
    """Ленивое создание/пересоздание gradio_client. Возвращает None при
    финальной невозможности подключиться."""
    global _demo_client
    with _demo_client_lock:
        if _demo_client is not None and not force_reconnect:
            return _demo_client
        try:
            from gradio_client import Client
            if force_reconnect:
                logger.warning(f"VoxCPM Demo: переподключаюсь к {VOXCPM_DEMO_SPACE}...")
            else:
                logger.info(f"VoxCPM Demo: подключаюсь к {VOXCPM_DEMO_SPACE}...")
            _demo_client = Client(VOXCPM_DEMO_SPACE, httpx_kwargs={"timeout": min(30.0, VOXCPM_DEMO_TIMEOUT)})
            logger.info("VoxCPM Demo: клиент готов")
            return _demo_client
        except Exception as exc:
            logger.warning(f"VoxCPM Demo: не удалось создать клиент: {exc}")
            _demo_client = None
            return None


def _reset_demo_client():
    """Сбрасывает клиент после ошибки, чтобы при следующей попытке создался заново."""
    global _demo_client
    with _demo_client_lock:
        _demo_client = None


def _generate_voxcpm_demo(
    direction: str,
    body: str,
    wav_path: Path,
    reference_path: Path | None = None,
    prompt_text: str | None = None,
) -> bool:
    """Запрос к Demo Space. ``direction`` без скобок, ``body`` с нативными ``[...]``.

    Демо-Space периодически даёт SSL handshake timeout / network errors —
    делаем 3 попытки с экспоненциальным бэкоффом и переподключением
    клиента. Возвращает True при успехе, False — пора уходить на следующий
    бэкенд.

    :param reference_path: пользовательский референс. None → fallback на
        модульный ``REF_PATH`` (старое поведение, обратно-совместимо).
    :param prompt_text: точный текст референса. None использует
        ``PROMPT_TEXT`` только при ``VOXCPM_CLONE_MODE=prompt``.
        По умолчанию и при пустой строке используем аудио без транскрипта.
    """
    ref = reference_path if reference_path is not None else REF_PATH
    ptext = prompt_text if prompt_text is not None else (PROMPT_TEXT if VOXCPM_CLONE_MODE == "prompt" else "")

    if not ref.exists():
        logger.error(f"Файл референса {ref} не найден")
        return False

    try:
        from gradio_client import handle_file
    except ImportError:
        logger.error("gradio_client не установлен; пропускаю Demo Space")
        return False

    import time as _time

    use_pt = ptext != ""
    ref_label = f" [ref={ref.name}]" if ref != REF_PATH else ""

    max_attempts = 3
    last_error: Optional[Exception] = None
    result_path = None

    for attempt in range(1, max_attempts + 1):
        client = _get_demo_client(force_reconnect=(attempt > 1))
        if client is None:
            last_error = RuntimeError("client unavailable")
            if attempt < max_attempts:
                _time.sleep(2.0 * attempt)
            continue

        try:
            logger.info(
                f"VoxCPM Demo TTS (attempt {attempt}/{max_attempts}): "
                f"'{body[:60]}{'...' if len(body) > 60 else ''}'"
                + (f" [direction='{direction}']" if direction else "")
                + ref_label
            )
            job = client.submit(
                text_input=body,
                control_instruction=(direction or "") if not use_pt else "",
                reference_wav_path_input=handle_file(str(ref.resolve())),
                use_prompt_text=use_pt,
                prompt_text_input=ptext,
                cfg_value_input=VOXCPM_DEMO_CFG,
                do_normalize=VOXCPM_DEMO_NORMALIZE,
                denoise=VOXCPM_DEMO_DENOISE,
                api_name="/generate",
            )
            try:
                result_path = job.result(timeout=VOXCPM_DEMO_TIMEOUT)
            except Exception:
                job.cancel()
                raise
            if result_path:
                break
            last_error = RuntimeError("empty result")
        except Exception as exc:
            last_error = exc
            logger.warning(
                f"VoxCPM Demo attempt {attempt}/{max_attempts} failed: {str(exc)[:200]}"
            )

        # Сбрасываем клиент перед следующей попыткой — старое httpx-соединение
        # могло протухнуть.
        _reset_demo_client()
        if attempt < max_attempts:
            _time.sleep(2.0 * attempt)

    if not result_path:
        logger.error(f"VoxCPM Demo finally failed after {max_attempts} attempts: {last_error}")
        return False

    src = Path(result_path)
    if not src.exists():
        logger.error(f"VoxCPM Demo: файл не найден: {result_path}")
        return False

    # Demo обычно возвращает mp3 — конвертируем в wav через pydub.
    suffix = src.suffix.lower()
    try:
        if suffix == ".wav":
            shutil.copyfile(str(src), str(wav_path))
        else:
            from pydub import AudioSegment
            audio = AudioSegment.from_file(str(src))
            audio.export(str(wav_path), format="wav")
    except Exception as exc:
        logger.error(f"VoxCPM Demo: не удалось конвертировать {suffix} -> wav: {exc}")
        return False

    return True


# ============================================================================
# VoxCPM2 локальный backend (второй фоллбэк)
# ============================================================================

def _generate_voxcpm_local(
    text_with_direction: str,
    wav_path: Path,
    reference_path: Path | None = None,
    prompt_text: str | None = None,
) -> bool:
    """Запрашивает локальный VoxCPM (FastAPI). Direction в круглых скобках
    встроен в начало ``text_with_direction``.

    :param reference_path: пользовательский референс. None → fallback на
        модульный ``REF_PATH``.
    :param prompt_text: точный текст референса. None использует
        ``PROMPT_TEXT`` только при ``VOXCPM_CLONE_MODE=prompt``.
        По умолчанию используем аудио без транскрипта.
    """
    ref = reference_path if reference_path is not None else REF_PATH
    ptext = prompt_text if prompt_text is not None else (PROMPT_TEXT if VOXCPM_CLONE_MODE == "prompt" else "")
    if ptext:
        _, text_with_direction = _split_first_direction(text_with_direction)

    if not ref.exists():
        logger.error(f"Файл референса {ref} не найден")
        return False

    ref_label = f" [ref={ref.name}]" if ref != REF_PATH else ""

    try:
        import requests
        with open(ref, "rb") as ref_file:
            data = {
                "text": text_with_direction,
                "max_length": VOXCPM_LOCAL_MAX_LENGTH,
                "prompt_text": ptext,
                "use_prompt_text": str(bool(ptext)).lower(),
                "clone_mode": "prompt" if ptext else "reference",
                "reference_wav_path": str(ref.resolve()),
                "cfg_value": VOXCPM_LOCAL_CFG,
                "inference_timesteps": VOXCPM_LOCAL_STEPS,
                "normalize": str(VOXCPM_DEMO_NORMALIZE).lower(),
                "denoise": str(VOXCPM_DEMO_DENOISE).lower(),
            }
            files = {"ref_audio": (ref.name, ref_file, "audio/wav")}

            logger.info(
                f"VoxCPM Local TTS: "
                f"'{text_with_direction[:60]}{'...' if len(text_with_direction) > 60 else ''}'"
                + ref_label
            )
            response = requests.post(
                VOXCPM_LOCAL_URL,
                data=data,
                files=files,
                headers={"Accept": "audio/wav"},
                timeout=VOXCPM_LOCAL_TIMEOUT,
            )
    except requests.Timeout:
        logger.error(f"VoxCPM Local таймаут после {VOXCPM_LOCAL_TIMEOUT}s")
        return False
    except Exception as exc:
        logger.error(f"VoxCPM Local connection error: {exc}")
        return False

    if response.status_code != 200:
        logger.error(f"VoxCPM Local HTTP {response.status_code}: {response.text[:300]}")
        return False

    content_type = (response.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        logger.error(f"VoxCPM Local вернул JSON вместо аудио: {response.text[:300]}")
        return False

    try:
        with open(wav_path, "wb") as f:
            f.write(response.content)
    except Exception as exc:
        logger.error(f"Не удалось записать VoxCPM Local ответ в {wav_path}: {exc}")
        return False

    return True

# ============================================================================
# Публичная функция
# ============================================================================

def _wav_to_telegram_ogg(wav_path: Path) -> Optional[Path]:
    """Перекодирует WAV в OGG/Opus (telegram voice)."""
    ogg_path = wav_path.with_suffix(".ogg")
    cmd = [
        "ffmpeg", "-y", "-i", str(wav_path),
        "-c:a", "libopus", "-b:a", "16k", "-vbr", "on",
        "-application", "voip", str(ogg_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        logger.error(f"ffmpeg WAV->OGG fail: {stderr[-500:]}")
        return None
    return ogg_path


def text_to_speech_telegram(text: str) -> Optional[str]:
    """Главная точка входа TTS для Telegram-бота.

    Принимает HTML-ответ Арти. Каждый ``<blockquote>`` синтезируется
    отдельным запросом, результаты склеиваются с короткой паузой и
    упаковываются в OGG/Opus.

    Иерархия бэкендов:
      1. VoxCPM Demo Space (HF) — основной
      2. VoxCPM локальный (если развёрнут)

    Возвращает путь к OGG-файлу или None при ошибке.
    """
    if not TTS_ENABLED:
        logger.info("TTS отключён флагом TTS_ENABLED=False")
        return None

    pairs = _prepare_segment_pairs(text or "")
    if not pairs:
        logger.error(f"Текст для TTS пуст после очистки: {text!r}")
        return None

    temp_dir = Path("temp")
    temp_dir.mkdir(exist_ok=True)
    run_id = os.urandom(4).hex()

    segment_wavs: list[Path] = []
    used_backends: set[str] = set()

    try:
        for idx, (direction, body) in enumerate(pairs):
            wav_path = temp_dir / f"tts_seg_{run_id}_{idx}.wav"

            # 1. Demo Space (основной)
            ok = _generate_voxcpm_demo(direction, body, wav_path)
            if ok:
                used_backends.add("voxcpm-demo")

            # 2. Локальный VoxCPM
            if not ok:
                logger.warning(
                    f"VoxCPM Demo segment {idx} не отработал — пробую локальный VoxCPM"
                )
                local_text = f"({direction}) {body}" if direction else body
                ok = _generate_voxcpm_local(local_text, wav_path)
                if ok:
                    used_backends.add("voxcpm-local")

            if not ok or not wav_path.exists():
                logger.error(f"TTS segment {idx} провалился на всех бэкендах, прерываю")
                for p in segment_wavs:
                    try: p.unlink()
                    except Exception: pass
                if wav_path.exists():
                    try: wav_path.unlink()
                    except Exception: pass
                return None

            segment_wavs.append(wav_path)

        # Склеиваем сегменты
        if len(segment_wavs) == 1:
            merged_wav = segment_wavs[0]
            cleanup_after = []
        else:
            merged_wav = temp_dir / f"tts_local_{run_id}.wav"
            cleanup_after = [merged_wav, *segment_wavs]
            try:
                from pydub import AudioSegment
                merged = None
                gap = AudioSegment.silent(duration=100)  # 100 ms между фразами
                for p in segment_wavs:
                    seg_audio = AudioSegment.from_file(p)
                    merged = seg_audio if merged is None else (merged + gap + seg_audio)
                merged.export(merged_wav, format="wav")
            except Exception as exc:
                logger.exception(f"Не удалось склеить TTS-сегменты: {exc}")
                for p in segment_wavs:
                    try: p.unlink()
                    except Exception: pass
                return None

        # WAV -> OGG/Opus
        ogg_path = _wav_to_telegram_ogg(merged_wav)

        # Очистка временных WAV
        for p in (cleanup_after if len(segment_wavs) > 1 else segment_wavs):
            if p.exists():
                try: p.unlink()
                except Exception: pass

        if not ogg_path or not ogg_path.exists():
            return None

        if used_backends:
            logger.info(f"TTS: использованы бэкенды: {sorted(used_backends)}")

        return str(ogg_path)
    except Exception as exc:
        logger.exception(f"Критическая ошибка TTS: {exc}")
        for p in segment_wavs:
            try: p.unlink()
            except Exception: pass
        return None
