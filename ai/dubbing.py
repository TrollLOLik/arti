"""
Тонкая async-обёртка над videotrans/main.py.

videotrans живёт со своим venv (CUDA torch, pyannote, audio-separator),
поэтому запускаем его как отдельный subprocess и не тянем зависимости
в основной процесс бота.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# --- Пути ---
ROOT_DIR = Path(__file__).resolve().parent.parent
VIDEOTRANS_DIR = ROOT_DIR / "videotrans"
VIDEOTRANS_RUNS = VIDEOTRANS_DIR / "runs"

if os.name == "nt":
    VIDEOTRANS_PYTHON = VIDEOTRANS_DIR / ".venv" / "Scripts" / "python.exe"
else:
    VIDEOTRANS_PYTHON = VIDEOTRANS_DIR / ".venv" / "bin" / "python"

LogCallback = Callable[[str], Awaitable[None]]


def is_supported_url(text: str) -> bool:
    text = (text or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


def _resolve_python() -> Optional[Path]:
    """Возвращает путь к Python из venv videotrans, либо None."""
    if VIDEOTRANS_PYTHON.exists():
        return VIDEOTRANS_PYTHON
    # Fallback: системный python (предполагается, что зависимости стоят глобально)
    return None


async def run_dubbing(
    url: str,
    run_id: str,
    log_callback: Optional[LogCallback] = None,
    extra_args: Optional[list[str]] = None,
    with_subs: bool = False,
    input_file: Optional[Path] = None,
    audio_only: bool = False,
    timeout_seconds: float | None = None,
    output_root: Path | None = None,
) -> tuple[bool, Optional[Path], str]:
    """
    Запускает videotrans/main.py.

    Источник: либо `url` (YouTube/прямой), либо локальный `input_file`.
    Если `audio_only=True` — на выходе аудиофайл (mp3), а не mp4.

    Returns:
        (success, output_path, error_text)
    """
    budget = timeout_seconds if timeout_seconds is not None else float(os.getenv('ARTI_DUBBING_TIMEOUT_SECONDS','3600'))
    if not 0 < budget <= 14400: raise ValueError('invalid_dubbing_timeout')
    if not isinstance(run_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',run_id):
        raise ValueError('invalid_dubbing_run_id')
    if output_root is not None:
        if not isinstance(output_root,Path): raise ValueError('invalid_dubbing_output_root')
        try: runs_root=output_root.resolve(strict=True)
        except OSError: raise ValueError('invalid_dubbing_output_root') from None
        if not runs_root.is_dir(): raise ValueError('invalid_dubbing_output_root')
    else: runs_root=VIDEOTRANS_RUNS

    if not input_file and not is_supported_url(url):
        return False, None, "URL должен начинаться с http:// или https:// либо нужен файл"

    if not input_file:
        from utils.url_safety import is_safe_public_url_async
        if not await is_safe_public_url_async(url):
            return False, None, "Недопустимый или небезопасный URL (заблокировано SSRF-защитой)"

    python_path = _resolve_python()
    if python_path is None:
        return False, None, (
            f"Не найден Python в videotrans/.venv. "
            f"Ожидался: {VIDEOTRANS_PYTHON}"
        )

    if not (VIDEOTRANS_DIR / "main.py").exists():
        return False, None, f"Не найден videotrans/main.py: {VIDEOTRANS_DIR}"

    runs_root.mkdir(parents=True, exist_ok=True)
    run_dir = runs_root / run_id
    if run_dir.is_symlink(): raise ValueError('invalid_dubbing_run_directory')
    run_dir.mkdir(parents=True, exist_ok=True)
    if run_dir.resolve(strict=True).parent!=runs_root.resolve(strict=True):
        raise ValueError('invalid_dubbing_run_directory')

    work_dir = run_dir / "work"
    output_suffix = ".mp3" if audio_only else ".mp4"
    output_name = "dubbed_audio" if audio_only else "dubbed"
    output_path = run_dir / f"{output_name}{output_suffix}"
    summary_path = run_dir / "summary.txt"
    dub_plan_path = run_dir / "dub_plan.json"

    cmd = [
        str(python_path),
        "main.py",
    ]
    # Источник
    if input_file:
        cmd.extend(["--input-file", str(Path(input_file).resolve())])
    else:
        cmd.append(url)

    cmd.extend([
        "--work-dir", str(work_dir),
        "--output", str(output_path),
        "--summary-output", str(summary_path),
        "--dub-plan", str(dub_plan_path),
        "--no-review-pause",
        "--stage", "full",
        "--tts-backend", os.getenv("VOXCPM_TTS_BACKEND", "voxcpm-demo"),
    ])
    if audio_only:
        cmd.append("--audio-only")
        # для аудио сабы не имеют смысла
        cmd.append("--no-hardsubs")
    elif not with_subs:
        cmd.append("--no-hardsubs")
    if extra_args:
        cmd.extend(extra_args)

    logger.info("Запускаю videotrans: %s", " ".join(cmd))

    try:
        from utils.process_limits import create_owned_subprocess_exec
        process = await create_owned_subprocess_exec(
            *cmd,
            cwd=str(VIDEOTRANS_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as exc:
        logger.exception("Не удалось стартовать videotrans subprocess")
        return False, None, f"Не удалось запустить процесс: {exc}"

    from utils.process_limits import stop_process
    return_code = None
    log_tail: list[str] = []
    try:
        async with asyncio.timeout(budget):
            assert process.stdout is not None
            async for raw in process.stdout:
                line = raw.decode('utf-8', errors='replace').rstrip()
                if not line: continue
                log_tail.append(line)
                if len(log_tail)>200: del log_tail[:-200]
                logger.info('[videotrans] %s',line)
                if log_callback:
                    try: await log_callback(line)
                    except Exception: logger.debug('Dubbing progress callback failed')
            return_code = await process.wait()
    except TimeoutError:
        return False,None,'Истёк срок выполнения дубляжа; процесс остановлен.'
    except asyncio.CancelledError:
        raise
    except Exception:
        return False,None,'Не удалось прочитать результат процесса дубляжа.'
    finally:
        if return_code != 0:
            await asyncio.shield(stop_process(process,process_group=os.name=='posix'))

    if return_code != 0:
        tail = "\n".join(log_tail[-15:])
        return False, None, (
            f"videotrans завершился с кодом {return_code}.\n"
            f"Последние строки лога:\n{tail}"
        )

    if not output_path.exists():
        tail = "\n".join(log_tail[-15:])
        return False, None, (
            f"Выходной файл не создан: {output_path}\n"
            f"Последние строки лога:\n{tail}"
        )

    return True, output_path, ""


def cleanup_run(run_id: str, keep_paths: Optional[list[Path]] = None) -> None:
    """Удаляет рабочую папку запуска.

    Если задан ``keep_paths`` — эти файлы (и их родительские каталоги до
    ``run_dir``) сохраняются. Это позволяет, например, оставить
    ``dubbed.mp4`` для ручного забора, но снести промежуточный ``work/``
    каталог с гигабайтами WAV-ов.
    """
    run_dir = VIDEOTRANS_RUNS / run_id
    if not run_dir.exists():
        return

    keep_paths = keep_paths or []
    keep_resolved = set()
    for kp in keep_paths:
        try:
            keep_resolved.add(Path(kp).resolve())
        except Exception:
            continue

    if not keep_resolved:
        try:
            shutil.rmtree(run_dir, ignore_errors=True)
            logger.info("Очищена директория запуска: %s", run_dir)
        except Exception as exc:
            logger.warning("Не удалось очистить %s: %s", run_dir, exc)
        return

    # Селективная чистка: проходим по содержимому run_dir и сносим всё, что
    # не помечено как keep.
    try:
        run_dir_resolved = run_dir.resolve()
        for entry in run_dir.iterdir():
            entry_resolved = entry.resolve()
            # Сохраняем сам файл, помеченный как keep
            if entry_resolved in keep_resolved:
                continue
            # Сохраняем директорию, если внутри есть keep-файл
            if entry.is_dir() and any(
                str(kp).startswith(str(entry_resolved)) for kp in keep_resolved
            ):
                # Заходим внутрь и удаляем всё кроме keep
                for inner in entry.rglob("*"):
                    inner_resolved = inner.resolve()
                    if inner_resolved in keep_resolved:
                        continue
                    if inner.is_file():
                        try: inner.unlink()
                        except Exception: pass
                # Удаляем пустые подкаталоги
                for inner in sorted(entry.rglob("*"), key=lambda p: -len(str(p))):
                    if inner.is_dir():
                        try: inner.rmdir()
                        except OSError: pass
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                try: entry.unlink()
                except Exception: pass
        logger.info(
            "Частично очищена директория запуска (сохранено: %s): %s",
            [p.name for p in keep_resolved], run_dir,
        )
    except Exception as exc:
        logger.warning("Не удалось селективно очистить %s: %s", run_dir, exc)
