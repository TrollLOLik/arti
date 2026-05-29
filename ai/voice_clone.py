"""
Модуль клонирования голоса для команды ``/vclone`` (alias ``/steal``).

Архитектурно повторяет паттерн ``/dub``: чистые функции пайплайна без классов,
управление FSM/очередью — снаружи в ``bot/commands.py``, ``bot/handlers.py`` и
``bot/queue.py``.

Стадии пайплайна (см. design.md::Architecture):

    extract_reference  → validate_reference  → run_separator (опционально)
    → normalize_text_via_llm → synthesize_with_clone → cleanup_vclone_files

TTS-бэкенды (Demo Space → локальный VoxCPM → Fish Speech) переиспользуются
из ``ai.tts``: сегментация через ``_split_body_sentences``, очистка тэгов
направления через локальный ``sanitize_direction`` поверх
``_NATIVE_VOXCPM_TAGS``.

Сепаратор запускается subprocess'ом через ``videotrans/.venv`` —
см. ``VIDEOTRANS_PYTHON``/``VIDEOTRANS_DIR`` из ``ai.dubbing``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Union

from ai.dubbing import VIDEOTRANS_DIR, VIDEOTRANS_PYTHON
from ai.generation import generate_response_stream
from ai.tts import (
    _NATIVE_VOXCPM_TAGS,
    _generate_fish,
    _generate_voxcpm_demo,
    _generate_voxcpm_local,
    _split_body_sentences,
)

if TYPE_CHECKING:
    from telegram import File as TelegramFile

logger = logging.getLogger(__name__)

# Системный промпт LLM-нормализатора (см. design.md::LLM Normalizer JSON Contract).
# Модель должна вернуть СТРОГО JSON-объект `{"text": ...}`.
_NORMALIZER_SYSTEM_PROMPT = """Ты — нормализатор текста для синтеза речи. Получаешь сырой текст и возвращаешь СТРОГО JSON-объект:
{"text": "<нормализованный текст>"}

ПРАВИЛА (СТРОГОЕ СОБЛЮДЕНИЕ):
- Числа прописью на языке текста ("в 2024 году" → "в две тысячи двадцать четвертом году").
- Аббревиатуры по произношению ("МГУ" → "эм гэ у", "ИИ" → "и и").
- НИКАКИХ квадратных или круглых скобок в "text".
- Сохраняй ВСЕ слова пользователя — НЕ меняй слова на синонимы, НЕ смягчай маты, НЕ заменяй сленг.
- Только исправляй орфографию и пунктуацию. Структура предложений и лексика должны остаться без изменений.
- Никаких пояснений, преамбул, markdown-блоков. Только JSON в одну строку."""

__all__ = [
    "VCloneJob",
    "extract_reference",
    "validate_reference",
    "run_separator",
    "normalize_text_via_llm",
    "sanitize_direction",
    "synthesize_with_clone",
    "cleanup_vclone_files",
]


@dataclass
class VCloneJob:
    """Полное описание задачи клонирования голоса в очереди.

    Создаётся в ``bot/commands.py`` после прохождения FSM (reference выбран,
    cleanup решён, текст синтеза получен) и кладётся в ``vclone_queue``.
    Воркер ``vclone_worker`` использует поля для нормализации текста, синтеза
    и отправки результата в чат.

    Attributes:
        chat_id: Telegram chat_id, куда отправлять результат и в reply на
            который должно прийти голосовое.
        user_id: Telegram user_id инициатора (для аудит-лога и FSM-очистки).
        user_name: Имя/username пользователя для caption и аудита.
        message_id: ``message_id`` исходного ``/vclone``-сообщения для
            ``reply_to_message_id`` при отправке результата.
        reference_path: Финальный путь к WAV-референсу (cleaned-версия,
            если пользователь выбрал чистку, иначе original).
        synthesis_text: Сырой текст для озвучки (до LLM-нормализации).
        source_kind: Категория источника референса для аудита, например
            ``"reply_voice"``, ``"url"``, ``"stepwise_video"``.
        cleaned: Был ли применён сепаратор. Только для аудит-лога.
    """

    chat_id: int
    user_id: int
    user_name: str
    message_id: int
    reference_path: Path
    synthesis_text: str
    source_kind: str
    cleaned: bool


async def extract_reference(
    src: Union["TelegramFile", str],
    work_dir: Path,
) -> Path:
    """Загружает источник и конвертирует его в моно WAV 24 kHz.

    Принимает либо Telegram ``File`` (тогда вызывается ``download_to_drive``),
    либо URL-строку (тогда — ``ai.video_url.download_audio_for_url``).
    Конвертация — subprocess ``ffmpeg`` с ``-ac 1 -ar 24000``.

    Args:
        src: Либо объект ``telegram.File`` (после ``bot.get_file``), либо URL.
        work_dir: Рабочая директория, куда складывается результирующий WAV
            (обычно ``temp/``).

    Returns:
        Path к сконвертированному моно-WAV 24 kHz внутри ``work_dir``.

    Raises:
        RuntimeError: Если ``ffmpeg`` завершился с ненулевым кодом — сообщение
            содержит хвост ``stderr``.
    """
    work_dir.mkdir(parents=True, exist_ok=True)

    run_id = uuid.uuid4().hex[:8]
    intermediate: Path | None = None
    cleanup_intermediate = True

    if isinstance(src, str):
        # URL-источник: качаем через yt-dlp в work_dir, потом перегоняем в WAV.
        from ai.video_url import download_audio_for_url

        intermediate = await download_audio_for_url(src, work_dir)
    else:
        # Telegram File: сохраняем рядом с work_dir с уникальным именем.
        # Имя расширения у telegram.File не всегда есть — берём ".bin"
        # как универсальный контейнер; ffmpeg сам разберётся с форматом.
        suffix = ""
        file_path = getattr(src, "file_path", None) or ""
        if file_path:
            ext = Path(file_path).suffix
            if ext and len(ext) <= 6:
                suffix = ext
        intermediate = work_dir / f"vclone_src_{run_id}{suffix or '.bin'}"
        await src.download_to_drive(custom_path=str(intermediate))

    output_wav = work_dir / f"vclone_ref_{run_id}.wav"

    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", str(intermediate),
        "-vn",
        "-ac", "1",
        "-ar", "24000",
        "-c:a", "pcm_s16le",
        str(output_wav),
    ]

    logger.info("vclone: ffmpeg → %s (src=%s)", output_wav.name, intermediate)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        rc = proc.returncode
    except Exception as exc:
        # Не удалось даже запустить ffmpeg — пробрасываем как RuntimeError,
        # вызывающая сторона сделает cleanup временных файлов.
        if cleanup_intermediate and intermediate and intermediate.exists():
            try:
                intermediate.unlink()
            except Exception:
                pass
        raise RuntimeError(f"ffmpeg launch failed: {exc}") from exc

    if rc != 0:
        stderr_text = (stderr or b"").decode("utf-8", errors="replace")
        tail = stderr_text.strip()[-500:]
        # Удаляем недосозданный output и intermediate
        try:
            if output_wav.exists():
                output_wav.unlink()
        except Exception:
            pass
        if cleanup_intermediate and intermediate and intermediate.exists():
            try:
                intermediate.unlink()
            except Exception:
                pass
        raise RuntimeError(f"ffmpeg failed (rc={rc}): {tail}")

    # Best-effort удаление промежуточного файла после успешной конвертации.
    if cleanup_intermediate and intermediate and intermediate.exists():
        try:
            intermediate.unlink()
        except Exception as exc:
            logger.debug("vclone: не удалось удалить intermediate %s: %s", intermediate, exc)

    return output_wav


async def validate_reference(wav: Path) -> tuple[bool, str, Path]:
    """Проверяет длительность и наличие звука в референсе.

    Через ``ffprobe`` получаем длительность, через ``ffmpeg -af volumedetect``
    — средний уровень громкости. Если 15 < dur ≤ 60 — обрезаем до 15 секунд
    в новом файле, и ``final_path`` указывает уже на обрезанную версию.

    Args:
        wav: Путь к моно-WAV из ``extract_reference``.

    Returns:
        Кортеж ``(ok, reason, final_path)``:

        - dur < 3 → ``(False, "refused_short_ref", wav)``
        - dur > 600 → ``(False, "refused_long_ref", wav)``
        - ``mean_volume`` < -50 dB → ``(False, "refused_silent", wav)``
        - dur > 30 → ``(True, "ok", trimmed_path)``
        - 3 ≤ dur ≤ 30 → ``(True, "ok", wav)``
    """
    # 1. Длительность через ffprobe.
    duration = await _probe_duration(wav)
    if duration is None:
        # ffprobe сломался — считаем ссылку невалидной, но без падения.
        logger.warning("vclone.validate: не удалось определить длительность %s", wav)
        return (False, "refused_short_ref", wav)

    if duration < 3.0:
        logger.info("vclone.validate: %s длительность %.2fs < 3s", wav.name, duration)
        return (False, "refused_short_ref", wav)
    if duration > 600.0:
        logger.info("vclone.validate: %s длительность %.2fs > 600s", wav.name, duration)
        return (False, "refused_long_ref", wav)

    # 2. Trim, если dur > 30.
    final_path = wav
    if duration > 30.0:
        trimmed = wav.with_name(f"{wav.stem}_trim.wav")
        ok = await _trim_to_30s(wav, trimmed)
        if ok and trimmed.exists():
            final_path = trimmed
            logger.info("vclone.validate: %s обрезан до 30s → %s", wav.name, trimmed.name)
        else:
            # Если trim упал — продолжаем с оригиналом, силенс-детект сам отсечёт мусор.
            logger.warning("vclone.validate: trim не удался, продолжаем с %s", wav.name)

    # 3. Силенс-детект через volumedetect.
    mean_volume = await _measure_mean_volume(final_path)
    if mean_volume is None:
        # Не смогли измерить — допускаем, не блокируем (ffmpeg уже сконвертировал WAV).
        logger.warning("vclone.validate: volumedetect не вернул mean_volume для %s", final_path)
    elif mean_volume < -50.0:
        logger.info(
            "vclone.validate: %s mean_volume=%.1f dB < -50dB (тишина)",
            final_path.name,
            mean_volume,
        )
        return (False, "refused_silent", final_path)

    return (True, "ok", final_path)


async def _probe_duration(wav: Path) -> float | None:
    """Возвращает длительность WAV в секундах через ``ffprobe`` или ``None``."""
    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(wav),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
        if proc.returncode != 0:
            return None
        text = (stdout or b"").decode("utf-8", errors="replace").strip()
        if not text:
            return None
        return float(text.splitlines()[-1].strip())
    except (OSError, ValueError) as exc:
        logger.debug("vclone.validate: ffprobe duration failed: %s", exc)
        return None


async def _trim_to_30s(src: Path, dst: Path) -> bool:
    """Обрезает ``src`` до первых 30 секунд в ``dst``. Возвращает ``True`` при успехе."""
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    # Re-encode (без -c copy): cut на ровной границе для WAV проще через перекодирование.
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", str(src),
        "-t", "30",
        "-ac", "1",
        "-ar", "24000",
        "-c:a", "pcm_s16le",
        str(dst),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            tail = (stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
            logger.warning("vclone.validate: ffmpeg trim rc=%s, %s", proc.returncode, tail)
            return False
        return True
    except OSError as exc:
        logger.warning("vclone.validate: ffmpeg trim launch failed: %s", exc)
        return False


_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", re.IGNORECASE)


async def _measure_mean_volume(wav: Path) -> float | None:
    """Возвращает ``mean_volume`` в dB через ``ffmpeg -af volumedetect`` или ``None``."""
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostats",
        "-i", str(wav),
        "-af", "volumedetect",
        "-vn", "-sn", "-dn",
        "-f", "null",
        "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        text = (stderr or b"").decode("utf-8", errors="replace")
        match = _MEAN_VOLUME_RE.search(text)
        if not match:
            return None
        return float(match.group(1))
    except (OSError, ValueError) as exc:
        logger.debug("vclone.validate: volumedetect failed: %s", exc)
        return None


async def run_separator(
    input_wav: Path,
    work_dir: Path,
    timeout: float = 120,
) -> Path | None:
    """Чистит вокал от музыки/шумов через ``audio-separator``.

    Запускает ``videotrans/_run_separator.py`` через
    ``asyncio.create_subprocess_exec`` с интерпретатором
    ``VIDEOTRANS_PYTHON`` (там стоит ``audio-separator[gpu]``). Скрипт пишет
    путь к vocals-файлу последней строкой stdout.

    Любые ошибки (non-zero exit, таймаут, отсутствие ``videotrans/.venv``,
    ``ERROR:``-префикс в stdout) поглощаются — функция логирует на ``ERROR``
    и возвращает ``None``, чтобы вызывающая сторона могла откатиться на
    оригинальный референс (см. design.md::Error Handling, Requirement 6.9).

    Args:
        input_wav: Путь к моно-WAV из ``validate_reference``.
        work_dir: Куда сепаратор сложит результирующие файлы.
        timeout: Жёсткий таймаут на subprocess в секундах. По умолчанию 120
            (запас x4 от типового времени работы).

    Returns:
        Path к vocals-WAV или ``None`` при любой ошибке.
    """
    script = VIDEOTRANS_DIR / "_run_separator.py"
    try:
        if not VIDEOTRANS_PYTHON.exists():
            logger.error(
                "vclone.separator: VIDEOTRANS_PYTHON не найден: %s", VIDEOTRANS_PYTHON
            )
            return None
        if not script.exists():
            logger.error("vclone.separator: helper-скрипт не найден: %s", script)
            return None

        work_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            str(VIDEOTRANS_PYTHON),
            str(script),
            "--input", str(input_wav.resolve()),
            "--output-dir", str(work_dir.resolve()),
        ]

        logger.info(
            "vclone.separator: запуск %s (input=%s, work_dir=%s, timeout=%.0fs)",
            script.name,
            input_wav.name,
            work_dir,
            timeout,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(VIDEOTRANS_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.error(
                "vclone.separator: таймаут %.0fs, kill subprocess", timeout
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
            return None

        rc = proc.returncode
        stdout_text = (stdout or b"").decode("utf-8", errors="replace")
        stderr_text = (stderr or b"").decode("utf-8", errors="replace")

        if rc != 0:
            tail = stderr_text.strip()[-500:]
            logger.error(
                "vclone.separator: subprocess rc=%s, stderr tail: %s", rc, tail
            )
            return None

        # Парсим stdout: последняя непустая строка не должна начинаться с ERROR:.
        last_line = ""
        for line in reversed(stdout_text.splitlines()):
            stripped = line.strip()
            if stripped:
                last_line = stripped
                break

        if not last_line:
            logger.error("vclone.separator: пустой stdout, stderr=%s", stderr_text.strip()[-300:])
            return None

        if last_line.upper().startswith("ERROR:"):
            logger.error("vclone.separator: helper вернул ошибку: %s", last_line)
            return None

        vocals = Path(last_line)
        if not vocals.is_absolute():
            vocals = (work_dir / vocals).resolve()

        if not vocals.exists() or not vocals.is_file():
            logger.error(
                "vclone.separator: путь vocals не существует: %s", vocals
            )
            return None

        logger.info("vclone.separator: vocals → %s", vocals)
        return vocals
    except Exception as exc:
        # Любая ошибка — лог и None, чтобы вызывающая сторона откатилась
        # на оригинальный референс (Requirement 6.9).
        logger.error("vclone.separator: непредвиденная ошибка: %s", exc, exc_info=True)
        return None


async def normalize_text_via_llm(raw_text: str) -> str:
    """Прогоняет текст через LLM-нормализатор.

    Промпт (см. design.md::LLM Normalizer JSON Contract) требует от модели
    строгий JSON ``{"text": ...}``: числа и аббревиатуры прописью,
    сохранение слов пользователя, только исправление орфографии и пунктуации.
    На любой сбой парсинга возвращаем сырой текст — это осознанный fallback
    (Requirement 8.6 / design.md::Error Handling).

    Args:
        raw_text: Сырой текст от пользователя.

    Returns:
        Нормализованный текст, или ``raw_text`` при ошибке.
    """
    if not raw_text or not raw_text.strip():
        return raw_text

    try:
        result = await generate_response_stream(
            chat_id=0,
            prompt=raw_text,
            user_name="vclone",
            chat_context="",
            model="gemini-3.1-flash-lite-preview",
            temperature=0.3,
            custom_system_prompt=_NORMALIZER_SYSTEM_PROMPT,
        )
        # Контракт: (response_text, used_search, grounding_links, found_image_urls).
        response_text = result[0] if isinstance(result, tuple) else result
        if not response_text or not isinstance(response_text, str):
            return raw_text

        obj = _parse_normalizer_json(response_text)
        if obj is None:
            return raw_text

        text = obj.get("text")
        if not isinstance(text, str):
            return raw_text

        normalized = text.strip() or raw_text
        return normalized
    except Exception as exc:
        logger.warning(
            "vclone.normalize: LLM-нормализатор упал, fallback на raw_text: %s", exc
        )
        return raw_text


def _parse_normalizer_json(raw: str) -> dict | None:
    """Парсит ответ LLM-нормализатора в dict.

    Сначала пробуем ``json.loads`` целиком. Если LLM обернула JSON в
    markdown code-fence (```` ```json ... ``` ```` или ``` ... ```), снимаем
    обёртку. В крайнем случае ищем первый ``{...}``-блок через regex.

    Args:
        raw: Сырой ответ модели.

    Returns:
        ``dict`` при успехе, иначе ``None``.
    """
    if not raw or not isinstance(raw, str):
        return None

    candidates: list[str] = []
    stripped = raw.strip()
    candidates.append(stripped)

    # Снимаем markdown code-fence, если есть.
    fence = re.match(
        r"^```(?:json)?\s*(.*?)\s*```$",
        stripped,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fence:
        candidates.append(fence.group(1).strip())

    # Fallback: вытаскиваем первый {...}-блок (жадно, чтобы захватить весь объект).
    obj_match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if obj_match:
        candidates.append(obj_match.group(0))

    for cand in candidates:
        if not cand:
            continue
        try:
            parsed = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed

    return None


def sanitize_direction(direction: str) -> str:
    """Очищает строку Control Instruction от запрещённого содержимого.

    Удаляет всё, что внутри квадратных и круглых скобок, а также любые
    токены из ``_NATIVE_VOXCPM_TAGS`` (case-insensitive). Если после
    очистки осталось меньше 3 значимых символов — возвращаем пустую строку,
    чтобы вызывающая сторона не передавала мусор в TTS-бэкенд.

    Args:
        direction: Строка ``control_instruction`` от LLM (или произвольная
            строка-описание манеры).

    Returns:
        Очищенная строка, или ``""`` если ничего полезного не осталось.
    """
    if not direction or not isinstance(direction, str):
        return ""

    current = direction
    # 1. Вырезаем всё в [...] и (...).
    current = re.sub(r"\[[^\]]*\]", " ", current)
    current = re.sub(r"\([^)]*\)", "", current)

    # 2. Удаляем нативные VoxCPM-теги по word-boundary, case-insensitive.
    #    Word-boundary важен, чтобы не зацепить, например, "laugh" внутри "laughter".
    for tag in _NATIVE_VOXCPM_TAGS:
        current = re.sub(
            rf"\b{re.escape(tag)}\b",
            " ",
            current,
            flags=re.IGNORECASE,
        )

    # 3. Чистим висящую пунктуацию вроде ", , ;  -" после удаления тегов
    #    и схлопываем пробелы.
    current = re.sub(r"\s+", " ", current).strip()
    # Срезаем ведущие/висящие разделители
    current = current.strip(" ,;:.-—–")
    current = re.sub(r"\s+", " ", current).strip()

    if len(current) < 3:
        return ""

    return current


async def synthesize_with_clone(
    reference: Path,
    text: str,
    work_dir: Path,
    direction: str = "",
) -> Path | None:
    """Синтезирует речь по тексту на голосе из ``reference``.

    Каскад бэкендов (Demo Space → локальный VoxCPM → Fish Speech)
    переиспользуется из ``ai.tts``. Текст разбивается через
    ``_split_body_sentences``; эмоции передаются как direction;
    итоговые WAV-куски склеиваются через ``pydub`` в один файл внутри
    ``work_dir``.

    Args:
        reference: Путь к финальному WAV-референсу (cleaned/original).
        text: Нормализованный текст после ``normalize_text_via_llm``.
        work_dir: Рабочая директория для промежуточных и финального WAV.
        direction: Описание эмоции на английском (извлечено из скобок).

    Returns:
        Path к итоговому WAV или ``None``, если все три бэкенда упали.
    """
    if not text or not text.strip():
        logger.error("vclone.synthesize: пустой текст, нечего озвучивать")
        return None

    if not reference.exists():
        logger.error("vclone.synthesize: референс не найден: %s", reference)
        return None

    work_dir.mkdir(parents=True, exist_ok=True)

    # Разбивка на предложения; для текста ≤ 200 символов вернётся [text].
    segments = _split_body_sentences(text.strip())
    if not segments:
        logger.error("vclone.synthesize: после сплита не осталось сегментов: %r", text)
        return None

    run_id = uuid.uuid4().hex[:8]

    segment_wavs: list[Path] = []
    used_backends: set[str] = set()

    def _cleanup_segments() -> None:
        for p in segment_wavs:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

    try:
        for idx, body in enumerate(segments):
            wav_path = work_dir / f"vclone_seg_{run_id}_{idx}.wav"

            # 1. Demo Space — direction передаётся отдельным параметром.
            ok = await asyncio.to_thread(
                _generate_voxcpm_demo,
                direction,
                body,
                wav_path,
                reference,
                "",  # пустой prompt_text → use_prompt_text=False
            )
            if ok:
                used_backends.add("voxcpm-demo")

            # 2. Локальный VoxCPM — direction НЕ встраивается, пропускаем если есть direction.
            if not ok:
                logger.warning(
                    "vclone.synthesize: Demo Space упал на сегменте %d, пробую локальный VoxCPM",
                    idx,
                )
                # Локальный VoxCPM не поддерживает отдельный параметр direction, поэтому не используем его
                # если direction задан, пропускаем этот бэкенд
                if not direction:
                    ok = await asyncio.to_thread(
                        _generate_voxcpm_local,
                        body,
                        wav_path,
                        reference,
                        "",
                    )
                    if ok:
                        used_backends.add("voxcpm-local")

            # 3. Fish Speech — без direction, без [native_tag] (Fish их не понимает).
            if not ok:
                logger.warning(
                    "vclone.synthesize: локальный VoxCPM упал на сегменте %d, пробую Fish Speech",
                    idx,
                )
                fish_body = re.sub(r"\[[^\]]+\]", "", body)
                fish_body = re.sub(r"\s+", " ", fish_body).strip()
                if fish_body:
                    ok = await asyncio.to_thread(
                        _generate_fish,
                        fish_body,
                        wav_path,
                        reference,
                        "",
                    )
                    if ok:
                        used_backends.add("fish")

            if not ok or not wav_path.exists():
                logger.error(
                    "vclone.synthesize: сегмент %d провалился на всех бэкендах, прерываю",
                    idx,
                )
                if wav_path.exists():
                    try:
                        wav_path.unlink()
                    except Exception:
                        pass
                _cleanup_segments()
                return None

            segment_wavs.append(wav_path)

        # Склейка. Если сегмент один — отдаём его сразу (без перекодирования).
        if len(segment_wavs) == 1:
            if used_backends:
                logger.info(
                    "vclone.synthesize: готово, бэкенды=%s, сегментов=1",
                    sorted(used_backends),
                )
            return segment_wavs[0]

        merged_wav = work_dir / f"vclone_out_{run_id}.wav"
        try:
            from pydub import AudioSegment

            merged = None
            gap = AudioSegment.silent(duration=100)  # 100 ms между фразами
            for p in segment_wavs:
                seg_audio = AudioSegment.from_file(p)
                merged = seg_audio if merged is None else (merged + gap + seg_audio)
            merged.export(merged_wav, format="wav")
        except Exception as exc:
            logger.exception("vclone.synthesize: не удалось склеить сегменты: %s", exc)
            _cleanup_segments()
            try:
                if merged_wav.exists():
                    merged_wav.unlink()
            except Exception:
                pass
            return None

        # Чистим промежуточные сегменты после успешной склейки.
        _cleanup_segments()

        if used_backends:
            logger.info(
                "vclone.synthesize: готово, бэкенды=%s, сегментов=%d",
                sorted(used_backends),
                len(segments),
            )
        return merged_wav
    except Exception as exc:
        logger.exception("vclone.synthesize: непредвиденная ошибка: %s", exc)
        _cleanup_segments()
        return None


def cleanup_vclone_files(*paths: Path | str | None) -> None:
    """Best-effort удаление временных файлов пайплайна.

    Каждое исключение поглощается и логируется на ``DEBUG``: cleanup не
    должен мешать основному потоку (отправке результата или сообщению об
    ошибке) и не должен спамить лог. Принимает любое количество путей;
    ``None`` и несуществующие файлы тихо игнорируются. Строки автоматически
    приводятся к ``Path``.

    Args:
        *paths: Пути к файлам, подлежащим удалению (``Path``, ``str`` или
            ``None``).
    """
    for raw in paths:
        if raw is None:
            continue
        try:
            path = raw if isinstance(raw, Path) else Path(raw)
        except (TypeError, ValueError) as exc:
            logger.debug("vclone.cleanup: некорректный путь %r: %s", raw, exc)
            continue
        try:
            if path.exists():
                path.unlink()
        except Exception as exc:
            logger.debug("vclone.cleanup: не удалось удалить %s: %s", path, exc)
