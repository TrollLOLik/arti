"""
Тест VoxCPM (OmniVoice) — клонирование голоса из sample.wav
Отправляет референс-аудио и текст на http://localhost:8000/generate
Сохраняет сгенерированный WAV в outputs/
"""
import sys
import time
from pathlib import Path

import requests
import soundfile as sf
import numpy as np


OMNIVOICE_URL = "http://localhost:8000/generate"
REF_AUDIO_PATH = Path("sample.wav")
OUTPUT_DIR = Path("outputs")
TIMEOUT = 120


def check_server() -> bool:
    """Проверяем, жив ли сервер OmniVoice."""
    try:
        r = requests.get("http://localhost:8000/docs", timeout=3)
        return r.status_code == 200
    except requests.ConnectionError:
        return False
    except Exception:
        return False


def check_ref_audio() -> bool:
    """Проверяем наличие sample.wav."""
    if not REF_AUDIO_PATH.exists():
        print(f"[ОШИБКА] Референс-файл не найден: {REF_AUDIO_PATH.resolve()}")
        return False
    try:
        info = sf.info(str(REF_AUDIO_PATH))
        print(f"[OK] Референс: {REF_AUDIO_PATH} ({info.duration:.1f}с, {info.samplerate}Гц, {info.channels}кан.)")
        return True
    except Exception as e:
        print(f"[ОШИБКА] Не удалось прочитать {REF_AUDIO_PATH}: {e}")
        return False


def generate(text: str, output_name: str = "voxcpm_output") -> Path | None:
    """
    Отправляет текст и референс-аудио в OmniVoice, сохраняет результат.
    Возвращает путь к WAV-файлу или None при ошибке.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    output_path = OUTPUT_DIR / f"{output_name}.wav"

    print(f"\n{'='*60}")
    print(f"Отправка в OmniVoice...")
    print(f"  URL:      {OMNIVOICE_URL}")
    print(f"  Референс: {REF_AUDIO_PATH}")
    print(f"  Текст:    \"{text}\"")
    print(f"  Вывод:    {output_path}")
    print(f"{'='*60}")

    start = time.perf_counter()

    try:
        with open(REF_AUDIO_PATH, "rb") as ref_file:
            response = requests.post(
                OMNIVOICE_URL,
                data={"text": text},
                files={"ref_audio": (REF_AUDIO_PATH.name, ref_file, "audio/wav")},
                headers={"Accept": "audio/wav"},
                timeout=TIMEOUT,
            )
        response.raise_for_status()
    except requests.Timeout:
        print(f"[ОШИБКА] Таймаут запроса ({TIMEOUT}с)")
        return None
    except requests.ConnectionError:
        print(f"[ОШИБКА] Не удалось подключиться к {OMNIVOICE_URL}. Сервер запущен?")
        return None
    except requests.HTTPError as e:
        body = e.response.text[:500] if e.response is not None else ""
        print(f"[ОШИБКА] HTTP {e.response.status_code if e.response else '?'}: {body}")
        return None

    elapsed = time.perf_counter() - start

    content_type = response.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            err = response.json()
        except Exception:
            err = response.text[:500]
        print(f"[ОШИБКА] Сервер вернул JSON вместо аудио: {err}")
        return None

    if not response.content:
        print("[ОШИБКА] Сервер вернул пустой ответ")
        return None

    # Сохраняем сырой ответ
    try:
        audio, sr = sf.read(
            __import__("io").BytesIO(response.content),
            dtype="float32",
            always_2d=False,
        )
    except Exception as e:
        preview = response.content[:200]
        print(f"[ОШИБКА] Ответ не является аудио: {e}\n  Первые байты: {preview!r}")
        return None

    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)

    sf.write(str(output_path), audio, sr, subtype="PCM_16")
    duration = len(audio) / sr

    print(f"\n[ГОТОВО] Сгенерировано за {elapsed:.1f}с")
    print(f"  Длительность: {duration:.2f}с")
    print(f"  Частота:      {sr}Гц")
    print(f"  Сохранено:    {output_path}")
    return output_path


def main():
    print("=" * 60)
    print("  VoxCPM / OmniVoice — тест клонирования голоса")
    print("=" * 60)

    # Проверки
    if not check_server():
        print("\n[ОШИБКА] Сервер OmniVoice недоступен на http://localhost:8000")
        print("Убедитесь, что VoxCPM запущен и слушает порт 8000.")
        sys.exit(1)

    if not check_ref_audio():
        sys.exit(1)

    print("[OK] Сервер OmniVoice доступен")

    # Тестовые фразы
    test_phrases = [
        ("Привет! Как твои дела? Я рад тебя слышать.", "test_greeting"),
        ("Сегодня отличная погода, самое время прогуляться в парке.", "test_weather"),
        ("Ты знаешь, а ведь этот голос звучит довольно натурально.", "test_meta"),
    ]

    for text, name in test_phrases:
        result = generate(text, name)
        if result is None:
            print(f"\n[ПРОПУСК] Не удалось сгенерировать: \"{text[:40]}...\"")
            continue

    print(f"\n{'='*60}")
    print("  Все тесты завершены. Результаты в папке outputs/")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
