"""
Диагностика VoxCPM — почему обрезается конец фразы.
Проверяет: длину референса, длину текста, параметры сервера.
"""
import io
import sys
import time
from pathlib import Path

import numpy as np
import requests
import soundfile as sf
import librosa

OMNIVOICE_URL = "http://localhost:8000/generate"
REF_PATH = Path("sample.wav")
OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)


def prepare_reference(input_path: Path, max_sec: float = 10.0) -> Path:
    """Обрезаем референс до первых max_sec секунд, конвертируем в mono 48kHz."""
    output_path = OUTPUT_DIR / "ref_prepared.wav"
    audio, sr = librosa.load(str(input_path), sr=48000, mono=True, duration=max_sec)
    # Нормализуем громкость
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak * 0.95
    sf.write(str(output_path), audio, 48000, subtype="PCM_16")
    print(f"[REF] Подготовлен: {output_path} ({len(audio)/48000:.1f}с, mono, 48kHz, norm)")
    return output_path


def generate(text: str, ref_path: Path, label: str, extra_data: dict | None = None) -> tuple[bool, float]:
    """Возвращает (успех, длительность_аудио)."""
    output_path = OUTPUT_DIR / f"diag_{label}.wav"

    data = {"text": text}
    if extra_data:
        data.update(extra_data)

    print(f"\n  [{label}] \"{text}\"", end="", flush=True)

    try:
        with open(ref_path, "rb") as f:
            response = requests.post(
                OMNIVOICE_URL,
                data=data,
                files={"ref_audio": (ref_path.name, f, "audio/wav")},
                headers={"Accept": "audio/wav"},
                timeout=120,
            )
        response.raise_for_status()
    except Exception as e:
        print(f" -> ОШИБКА: {e}")
        return False, 0.0

    try:
        audio, sr = sf.read(io.BytesIO(response.content), dtype="float32", always_2d=False)
    except Exception as e:
        print(f" -> не аудио: {e}")
        return False, 0.0

    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)

    sf.write(str(output_path), audio, sr, subtype="PCM_16")
    duration = len(audio) / sr

    # Проверяем: есть ли тишина в конце (признак обрыва)
    tail = audio[-int(sr * 0.1):] if len(audio) > int(sr * 0.1) else audio
    tail_rms = np.sqrt(np.mean(tail ** 2))

    # Проверяем: резкий ли обрыв на последних 50 мс
    last_50ms = audio[-int(sr * 0.05):] if len(audio) > int(sr * 0.05) else audio
    last_rms = np.sqrt(np.mean(last_50ms ** 2)) if len(last_50ms) > 0 else 0

    abrupt_end = last_rms > tail_rms * 0.5 if tail_rms > 0 else False

    print(f" -> {duration:.2f}с, tail_rms={tail_rms:.4f}, abrupt={abrupt_end}")
    return True, duration


def main():
    print("=" * 60)
    print("  ДИАГНОСТИКА: обрезание конца фразы в VoxCPM")
    print("=" * 60)

    # 1. Проверяем сервер
    try:
        r = requests.get("http://localhost:8000/docs", timeout=3)
        print(f"[OK] Сервер жив (HTTP {r.status_code})")
    except Exception:
        print("[FATAL] Сервер недоступен")
        sys.exit(1)

    # 2. Готовим референс (короткий, mono, 48kHz)
    ref = prepare_reference(REF_PATH, max_sec=10.0)

    # 3. Тест: короткий текст
    print("\n--- Тест 1: короткий текст ---")
    generate("Привет.", ref, "short_hi")

    # 4. Тест: средний текст
    print("\n--- Тест 2: средний текст ---")
    generate("Привет! Как твои дела? Я рад тебя слышать.", ref, "mid_greet")

    # 5. Тест: длинный текст
    print("\n--- Тест 3: длинный текст ---")
    generate(
        "Сегодня отличная погода, самое время прогуляться в парке и насладиться свежим воздухом.",
        ref, "long_weather",
    )

    # 6. Тест: с max_length параметром (некоторые VoxCPM сборки это поддерживают)
    print("\n--- Тест 4: с max_length=2048 ---")
    generate(
        "Привет! Как твои дела? Я рад тебя слышать.",
        ref, "maxlen_2048",
        extra_data={"max_length": 2048},
    )

    # 7. Тест: с length_scale
    print("\n--- Тест 5: с length_scale=1.5 ---")
    generate(
        "Привет! Как твои дела? Я рад тебя слышать.",
        ref, "lscale_15",
        extra_data={"length_scale": 1.5},
    )

    # 8. Тест: сырой референс (без обработки) — сравнить
    print("\n--- Тест 6: сырой референс (27с, 44.1kHz, stereo) ---")
    generate(
        "Привет! Как твои дела? Я рад тебя слышать.",
        REF_PATH, "raw_ref",
    )

    # 9. Тест: с target_duration
    print("\n--- Тест 7: с target_duration=5.0 ---")
    generate(
        "Привет! Как твои дела? Я рад тебя слышать.",
        ref, "target_5s",
        extra_data={"target_duration": 5.0},
    )

    print(f"\n{'='*60}")
    print("  Диагностика завершена. Слушай файлы outputs/diag_*.wav")
    print("  Сравни diag_raw_ref.wav vs diag_mid_greet.wav")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
