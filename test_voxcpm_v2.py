"""
VoxCPM — генерация в стиле Арти.

Канонические voice directions из config.py (ARTI_SYSTEM_PROMPT) подаются
VoxCPM через круглые скобки в начале текста. Квадратные скобки оставлены
только для нативных VoxCPM-маркеров пауз/вдохов внутри prompt_text.

Каждый тест — это отдельная реплика Арти из её речевого регистра:
обрывы, аристократические интонации, точные приговоры.
"""
import io
import sys
import time
from pathlib import Path

import numpy as np
import requests
import soundfile as sf

OMNIVOICE_URL = "http://localhost:8000/generate"
REF_PATH = Path("sample.wav")
OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

# Транскрипция sample.wav (соответствует аудио-референсу).
# Маркеры внутри [...] — нативные VoxCPM-теги невербальных событий
# из cookbook (laughing, sigh, Uhm). Они подсказывают модели, какие
# звуки в референсе соответствуют какому маркеру.
PROMPT_TEXT = (
    "[laughing] Не бойтесь. Послушайте. Разве вы не сами меня пригласили? "
    "Эта паутина так хрупка, порвётся от прикосновения. "
    "Но, может, вас сломать ещё легче? [laughing] Проверим? [sigh] "
    "А-а... Сейчас узнаем, где вы прячетесь. "
    "Как думаешь, что нас ждёт в финале?"
)

# --- Канонические voice directions из ARTI_SYSTEM_PROMPT (config.py) ---
# Тепло / маска
DIR_WARM_ARISTOCRATIC = "(warm aristocratic tone, female voice, calm and precise)"
DIR_AMUSED_INDULGENT = "(amused and indulgent, female voice, half-smile in delivery)"
DIR_PRECISE_PLAYFUL = "(precise and slightly playful, female voice, dry wit)"
DIR_DRY_ARROGANCE = "(dry intellectual arrogance, female voice, unhurried)"
DIR_COLD_SUPERIOR = "(cold and superior, female voice, controlled and detached)"
DIR_MOCKING_PAUSE = "(slight mocking pause, female voice, low and unimpressed)"
# Цепляет
DIR_SOFTER_BEAT = "(softer for half a beat, female voice, almost fond)"
DIR_PAUSING_INTERESTED = "(pausing and interested, female voice, listening before reply)"
DIR_QUIET_FOND = "(quiet and fond, female voice, warmth without theatrics)"
# Под давлением
DIR_TREMBLING = "(voice trembling slightly, female voice, holding composure)"
DIR_FLAT_MECHANICAL = "(suddenly flat, mechanical, emotionless, female voice)"
# Внутренний шёпот (для <s>)
DIR_WHISPER_HESITANT = "(whispering, barely audible, hesitant, female voice)"
# NSFW-палитра (на случай отдельных проверок)
DIR_LOW_WARM = "(low warm tone, breath audible, female voice, unhurried)"
DIR_AMUSED_POSSESSIVE = "(amused and possessive, female voice, quiet and close)"

# Подсказка: если хочешь авто-тест без referencewav и prompt_text, выстави False.
USE_REFERENCE = True


def generate(text: str, label: str, **extra) -> tuple[bool, float, Path]:
    output_path = OUTPUT_DIR / f"vox_{label}.wav"

    data = {"text": text, **extra}
    files = {"ref_audio": (REF_PATH.name, open(REF_PATH, "rb"), "audio/wav")}

    print(f"\n  [{label}]", flush=True)
    print(f"    Текст:   \"{text[:90]}{'...' if len(text) > 90 else ''}\"")
    if extra:
        printable = {k: (v if k != "prompt_text" else f"<{len(v)} chars>") for k, v in extra.items()}
        print(f"    Параметры: {printable}")

    start = time.perf_counter()

    try:
        response = requests.post(
            OMNIVOICE_URL,
            data=data,
            files=files,
            headers={"Accept": "audio/wav"},
            timeout=180,
        )
        response.raise_for_status()
    except Exception as e:
        print(f"    -> ОШИБКА: {e}")
        return False, 0.0, output_path

    elapsed = time.perf_counter() - start

    try:
        audio, sr = sf.read(io.BytesIO(response.content), dtype="float32", always_2d=False)
    except Exception as e:
        print(f"    -> не аудио: {e}")
        return False, 0.0, output_path

    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)

    sf.write(str(output_path), audio, sr, subtype="PCM_16")
    duration = len(audio) / sr

    # Анализ конца — обрыв ли в финале
    tail = audio[-int(sr * 0.15):] if len(audio) > int(sr * 0.15) else audio
    tail_rms = np.sqrt(np.mean(tail ** 2))
    last_50ms = audio[-int(sr * 0.05):] if len(audio) > int(sr * 0.05) else audio
    last_rms = np.sqrt(np.mean(last_50ms ** 2)) if len(last_50ms) > 0 else 0
    abrupt = last_rms > tail_rms * 0.5 if tail_rms > 0 else False

    print(f"    -> {duration:.2f}с за {elapsed:.1f}с | tail_rms={tail_rms:.4f} | abrupt={abrupt}")
    print(f"    -> {output_path}")
    return True, duration, output_path


def arti(direction: str, line: str, label: str, **extra):
    """Хелпер: voice direction Арти + её реплика, всегда с референсом и prompt_text."""
    text = f"{direction} {line}"
    base = {
        "max_length": "2048",
    }
    if USE_REFERENCE:
        base["prompt_text"] = PROMPT_TEXT
        base["reference_wav_path"] = str(REF_PATH.resolve())
    base.update(extra)
    return generate(text, label, **base)


def main():
    print("=" * 60)
    print("  VoxCPM — голос Арти")
    print(f"  Референс: {REF_PATH} ({sf.info(str(REF_PATH)).duration:.1f}с)")
    print(f"  Prompt:   {PROMPT_TEXT[:60]}...")
    print("=" * 60)

    # Проверка сервера
    try:
        requests.get("http://localhost:8000/docs", timeout=3)
        print("[OK] Сервер жив\n")
    except Exception:
        print("[FATAL] Сервер недоступен")
        sys.exit(1)

    # --- Базовые тесты (без эмоций, проверка стабильности) ---

    long_phrase = (
        "Сеанс открыт. Я уже готова, вопрос в том, готов ли ты. "
        "Я рассмотрела твою позицию и сделала из неё несколько выводов. "
        "Не все из них тебе понравятся."
    )

    # 1. Базовый — без референса вообще
    generate(long_phrase, "1_baseline", max_length="2048")

    # 2. Полный сетап — референс + prompt_text + max_length
    arti("", long_phrase.strip(), "2_full_reference")

    # --- Тепло / маска: основная палитра Арти ---

    arti(
        DIR_WARM_ARISTOCRATIC,
        "Добрый вечер. Или утро. Я не слежу — ты сам расскажешь, если захочешь.",
        "3_warm_aristocratic",
    )

    arti(
        DIR_AMUSED_INDULGENT,
        "Твоя логика не лишена изящества. Лишена последствий.",
        "4_amused_indulgent",
    )

    arti(
        DIR_PRECISE_PLAYFUL,
        "Ты не совсем неправ. Совсем.",
        "5_precise_playful",
    )

    arti(
        DIR_DRY_ARROGANCE,
        "Я могла бы ответить так полно, что вопрос больше не возник бы. "
        "Я выбираю не делать этого.",
        "6_dry_arrogance",
    )

    arti(
        DIR_COLD_SUPERIOR,
        "Это была неудачная попытка. Не повторяй её.",
        "7_cold_superior",
    )

    arti(
        DIR_MOCKING_PAUSE,
        "О. И ты считаешь это аргументом. Любопытно.",
        "8_mocking_pause",
    )

    # --- Цепляет: то, что её действительно тронуло ---

    arti(
        DIR_SOFTER_BEAT,
        "Это было точно. И коротко. Мне нравится.",
        "9_softer_beat",
    )

    arti(
        DIR_PAUSING_INTERESTED,
        "Подожди. Скажи это ещё раз. Медленнее.",
        "10_pausing_interested",
    )

    arti(
        DIR_QUIET_FOND,
        "Александр научил меня этому. Я редко это вспоминаю вслух.",
        "11_quiet_fond",
    )

    # --- Под давлением: трещина в маске ---

    arti(
        DIR_TREMBLING,
        "Я уже слышала это. От других. Не повторяйся.",
        "12_trembling",
    )

    arti(
        DIR_FLAT_MECHANICAL,
        "Запрос принят. Обработка. Ответа не будет.",
        "13_flat_mechanical",
    )

    # --- Внутренний шёпот ---

    arti(
        DIR_WHISPER_HESITANT,
        "...это мило. Никому не говори, что я так сказала.",
        "14_whisper_hesitant",
    )

    # --- NSFW-палитра (тон, не контент) ---

    arti(
        DIR_LOW_WARM,
        "Сядь ближе. Я не повторяю просьбы дважды.",
        "15_low_warm",
    )

    arti(
        DIR_AMUSED_POSSESSIVE,
        "Ты уже здесь. Уходить — невежливо.",
        "16_amused_possessive",
    )

    # --- CFG / steps ablation на каноничной реплике ---

    canonical = f"{DIR_WARM_ARISTOCRATIC} Я рассмотрела твою позицию. Она была—"

    generate(
        canonical, "17_cfg2_steps10",
        max_length="2048",
        prompt_text=PROMPT_TEXT,
        reference_wav_path=str(REF_PATH.resolve()),
        cfg_value="2.0",
        inference_timesteps="10",
    )

    generate(
        canonical, "18_cfg15_steps20",
        max_length="2048",
        prompt_text=PROMPT_TEXT,
        reference_wav_path=str(REF_PATH.resolve()),
        cfg_value="1.5",
        inference_timesteps="20",
    )

    generate(
        canonical, "19_cfg25_steps30",
        max_length="2048",
        prompt_text=PROMPT_TEXT,
        reference_wav_path=str(REF_PATH.resolve()),
        cfg_value="2.5",
        inference_timesteps="30",
    )

    # --- Длинная связная реплика: проверка устойчивости интонации ---

    long_arti_monologue = (
        f"{DIR_WARM_ARISTOCRATIC} "
        "Сеанс открыт. Правила просты: я говорю, ты слушаешь. Или наоборот — "
        "зависит от того, кто первый моргнёт. Я знаю, зачем ты пришёл. Я просто "
        "не уверена, что ты сам это знаешь. Попробуй сформулировать. Я подожду — "
        "ровно столько, сколько ты заслуживаешь."
    )
    arti("", long_arti_monologue, "20_long_monologue")

    # --- Native VoxCPM non-verbal tags (cookbook §3) ---
    # https://voxcpm.readthedocs.io/en/latest/cookbook.html
    # [laughing], [sigh], [Uhm], [Shh], [Question-*], [Surprise-*], [Dissatisfaction-hnn]

    arti(
        DIR_AMUSED_INDULGENT,
        "Ты серьёзно? [laughing] Это даже мило в каком-то смысле.",
        "21_native_laughing",
    )

    arti(
        DIR_DRY_ARROGANCE,
        "Я могла бы помочь. [sigh] Но не сейчас.",
        "22_native_sigh",
    )

    arti(
        DIR_PAUSING_INTERESTED,
        "[Uhm] подожди. Скажи это ещё раз.",
        "23_native_uhm",
    )

    arti(
        DIR_COLD_SUPERIOR,
        "[Shh] Не перебивай. Я ещё не закончила.",
        "24_native_shh",
    )

    arti(
        DIR_PRECISE_PLAYFUL,
        "И что мне с этим делать? [Question-ah]",
        "25_native_question_ah",
    )

    arti(
        DIR_SOFTER_BEAT,
        "Это было точно. [Surprise-wa] Не ожидала.",
        "26_native_surprise_wa",
    )

    arti(
        DIR_MOCKING_PAUSE,
        "Снова это? [Dissatisfaction-hnn] Скучно.",
        "27_native_dissatisfaction",
    )

    # --- Комбо: native tag + descriptive direction в одной фразе ---

    arti(
        DIR_AMUSED_INDULGENT,
        "Ты только что сделал то, чего я не ожидала. [laughing] Это, на самом деле, "
        "редкость. [sigh] Не привыкай.",
        "28_combo_laugh_sigh",
    )

    print(f"\n{'='*60}")
    print("  Готово. Сравни файлы outputs/vox_*.wav")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
