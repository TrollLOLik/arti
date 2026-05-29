"""
Тест Fish Speech — 1 фраза с клонированием голоса из sample.wav
"""
import base64
import time
from pathlib import Path

import requests
import soundfile as sf

FISH_URL = "http://127.0.0.1:8080/v1/tts"
REF_PATH = Path("sample.wav")
PROMPT_TEXT = (
    "[laughing] Не бойтесь. Послушайте. Разве вы не сами меня пригласили? "
    "Эта паутина так хрупка, порвётся от прикосновения. "
    "Но, может, вас сломать ещё легче? [laughing] Проверим? [sigh] "
    "А-а... Сейчас узнаем, где вы прячетесь. "
    "Как думаешь, что нас ждёт в финале?"
)
OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)


def main():
    print("=" * 60)
    print("  Fish Speech — тест клонирования голоса")
    print(f"  Референс: {REF_PATH} ({sf.info(str(REF_PATH)).duration:.1f}с)")
    print("=" * 60)

    # Проверка референса
    if not REF_PATH.exists():
        print(f"[ОШИБКА] {REF_PATH} не найден")
        return

    # Проверка сервера
    try:
        r = requests.get("http://127.0.0.1:8080/v1/health", timeout=3)
        print(f"[OK] Сервер жив (HTTP {r.status_code})")
    except Exception:
        print("[WARN] /health недоступен, пробую TTS напрямую...")

    # Кодируем референс в base64
    with open(REF_PATH, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")

    text = "Привет! Как твои дела? Я рад тебя слышать."

    payload = {
        "text": text,
        "format": "wav",
        "references": [
            {"audio": audio_b64, "text": PROMPT_TEXT},
        ],
        "latency": "normal",
        "chunk_length": 200,
        "repetition_penalty": 2.0,
    }

    print(f"\nОтправка: \"{text}\"")
    start = time.perf_counter()

    try:
        response = requests.post(FISH_URL, json=payload, timeout=60)
        response.raise_for_status()
    except requests.Timeout:
        print("[ОШИБКА] Таймаут 60с")
        return
    except requests.ConnectionError:
        print(f"[ОШИБКА] Не удалось подключиться к {FISH_URL}")
        return
    except requests.HTTPError as e:
        print(f"[ОШИБКА] HTTP {e.response.status_code}: {e.response.text[:300]}")
        return

    elapsed = time.perf_counter() - start

    output_path = OUTPUT_DIR / "fish_test.wav"
    output_path.write_bytes(response.content)

    info = sf.info(str(output_path))
    print(f"\n[ГОТОВО] {elapsed:.1f}с")
    print(f"  Длительность: {info.duration:.2f}с")
    print(f"  Частота:      {info.samplerate}Гц")
    print(f"  Сохранено:    {output_path}")


if __name__ == "__main__":
    main()
