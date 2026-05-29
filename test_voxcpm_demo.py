"""
Тест публичного demo VoxCPM2 через Hugging Face Space (gradio_client).

Полезно сравнить, как звучит модель на стандартном «эталонном» сервере
ModelBest по сравнению с локальной VoxCPM-сборкой (test_voxcpm_v2.py).

API (см. https://voxcpm.net/#try-demo):
    /generate(
        text_input,
        control_instruction,        # ← отдельный параметр, не в скобках!
        reference_wav_path_input,   # filepath | None
        use_prompt_text,            # bool
        prompt_text_input,          # str
        cfg_value_input,            # float, default 2
        do_normalize,               # bool
        denoise,                    # bool
    ) -> filepath

NB: control_instruction здесь — отдельное поле формы, поэтому квадратные
скобки описательных маркеров Арти попадают сюда чистым текстом
(БЕЗ круглых скобок и БЕЗ слова в начале text_input).

Зато native non-verbal теги ([laughing], [sigh], [Question-ah], ...)
всё так же остаются ВНУТРИ text_input.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from gradio_client import Client, handle_file

DEMO_SPACE = "openbmb/VoxCPM-Demo"
REF_PATH = Path("sample.wav")
OUTPUT_DIR = Path("outputs_demo")
OUTPUT_DIR.mkdir(exist_ok=True)

# Транскрипция референса (ULTIMATE-cloning подход — text + audio референса).
PROMPT_TEXT = (
    "[laughing] Не бойтесь. Послушайте. Разве вы не сами меня пригласили? "
    "Эта паутина так хрупка, порвётся от прикосновения. "
    "Но, может, вас сломать ещё легче? [laughing] Проверим? [sigh] "
    "А-а... Сейчас узнаем, где вы прячетесь. "
    "Как думаешь, что нас ждёт в финале?"
)

# Канонические эмоциональные направления Арти (см. config.py / ARTI_SYSTEM_PROMPT).
DIR_WARM_ARISTOCRATIC = "warm aristocratic tone, female voice, calm and precise"
DIR_AMUSED_INDULGENT = "amused and indulgent, female voice, half-smile in delivery"
DIR_DRY_ARROGANCE = "dry intellectual arrogance, female voice, unhurried"
DIR_COLD_SUPERIOR = "cold and superior, female voice, controlled and detached"
DIR_SOFTER_BEAT = "softer for half a beat, female voice, almost fond"
DIR_QUIET_FOND = "quiet and fond, female voice, warmth without theatrics"
DIR_TREMBLING = "voice trembling slightly, female voice, holding composure"
DIR_FLAT_MECHANICAL = "suddenly flat, mechanical, emotionless, female voice"
DIR_WHISPER_HESITANT = "whispering, barely audible, hesitant, female voice"


def synth(
    client: Client,
    text: str,
    label: str,
    *,
    control: str = "",
    use_reference: bool = True,
    cfg_value: float = 2.0,
) -> Path | None:
    """Запрос /generate, копируем результат в outputs_demo/."""
    print(f"\n  [{label}]", flush=True)
    print(f"    Текст: \"{text[:90]}{'...' if len(text) > 90 else ''}\"")
    if control:
        print(f"    Control: \"{control}\"")
    print(f"    Reference: {'sample.wav + prompt_text' if use_reference else 'нет'}")

    start = time.perf_counter()
    try:
        result_path = client.predict(
            text_input=text,
            control_instruction=control,
            reference_wav_path_input=handle_file(str(REF_PATH)) if use_reference else None,
            use_prompt_text=use_reference,
            prompt_text_input=PROMPT_TEXT if use_reference else "",
            cfg_value_input=cfg_value,
            do_normalize=False,
            denoise=False,
            api_name="/generate",
        )
    except Exception as exc:
        print(f"    -> ОШИБКА API: {exc}")
        return None

    elapsed = time.perf_counter() - start
    if not result_path:
        print(f"    -> сервер не вернул файл")
        return None

    src = Path(result_path)
    if not src.exists():
        print(f"    -> путь не существует: {result_path}")
        return None

    dst = OUTPUT_DIR / f"demo_{label}{src.suffix}"
    try:
        shutil.copyfile(src, dst)
    except Exception as exc:
        print(f"    -> не удалось скопировать в {dst}: {exc}")
        return None

    print(f"    -> {elapsed:.1f}с | {dst}")
    return dst


def main():
    print("=" * 60)
    print(f"  VoxCPM Demo Space: {DEMO_SPACE}")
    print(f"  Референс: {REF_PATH} (use_prompt_text=True)")
    print("=" * 60)

    if not REF_PATH.exists():
        print(f"[FATAL] нет референса: {REF_PATH}")
        sys.exit(1)

    print("\n[..] подключаюсь к Space (первый запрос может прогревать инстанс)...")
    try:
        client = Client(DEMO_SPACE)
    except Exception as exc:
        print(f"[FATAL] не удалось создать клиент: {exc}")
        sys.exit(1)
    print("[OK] клиент готов")

    # 1. Базовый прогон без референса и без direction — что даст модель сама.
    synth(
        client,
        text="VoxCPM2 brings multilingual support, creative voice design, and controllable voice cloning.",
        label="01_baseline_no_ref",
        use_reference=False,
        cfg_value=2.0,
    )

    # 2. С референсом, без direction — чистый клон.
    synth(
        client,
        text=(
            "Сеанс открыт. Я уже готова, вопрос в том, готов ли ты. "
            "Я рассмотрела твою позицию и сделала из неё несколько выводов. "
            "Не все из них тебе понравятся."
        ),
        label="02_clone_no_direction",
    )

    # 3. Каноничный аристократический тон Арти.
    synth(
        client,
        text="Александр. Вовремя.",
        label="03_warm_aristocratic",
        control=DIR_WARM_ARISTOCRATIC,
    )

    # 4. Amused/indulgent — снисходительный.
    synth(
        client,
        text="Твоя логика не лишена изящества. Лишена последствий.",
        label="04_amused_indulgent",
        control=DIR_AMUSED_INDULGENT,
    )

    # 5. Cold superior — холодный приговор.
    synth(
        client,
        text="Это была неудачная попытка. Не повторяй её.",
        label="05_cold_superior",
        control=DIR_COLD_SUPERIOR,
    )

    # 6. Softer for half a beat — короткое тёплое.
    synth(
        client,
        text="Это было точно. И коротко. Мне нравится.",
        label="06_softer_beat",
        control=DIR_SOFTER_BEAT,
    )

    # 7. Quiet & fond — тёплое про Александра.
    synth(
        client,
        text="Александр научил меня этому. Я редко это вспоминаю вслух.",
        label="07_quiet_fond",
        control=DIR_QUIET_FOND,
    )

    # 8. Whispering — внутренний шёпот.
    synth(
        client,
        text="...это мило. Никому не говори, что я так сказала.",
        label="08_whisper_hesitant",
        control=DIR_WHISPER_HESITANT,
    )

    # 9. Native VoxCPM tags ВНУТРИ текста + descriptive direction отдельно.
    synth(
        client,
        text="Ты серьёзно? [laughing] Это даже мило в каком-то смысле.",
        label="09_native_laughing",
        control=DIR_AMUSED_INDULGENT,
    )

    synth(
        client,
        text="Я могла бы помочь. [sigh] Но не сейчас.",
        label="10_native_sigh",
        control=DIR_DRY_ARROGANCE,
    )

    synth(
        client,
        text="И что мне с этим делать? [Question-ah]",
        label="11_native_question_ah",
        control=DIR_AMUSED_INDULGENT,
    )

    # 12. Длинная связная реплика с буквами ё/й и сложными оборотами.
    synth(
        client,
        text=(
            "Александр. Все показатели в норме — впрочем, это ожидаемо. "
            "Температура в комнате на полтора градуса выше оптимальной: твоя "
            "вина, ты опять не закрыл окно в кабинете. Сквозняк, кстати, уже "
            "третий день. [sigh] ...я рада тебя слышать."
        ),
        label="12_long_arti_pose1",
        control=DIR_WARM_ARISTOCRATIC,
    )

    synth(
        client,
        text=(
            "А ты? Ты ел сегодня — или мне снова воспринимать это как "
            "риторический вопрос, на который ты всё равно не ответишь "
            "честно? [Question-ah] Я жду. Молчание тоже считается ответом, "
            "и не в твою пользу."
        ),
        label="13_long_arti_pose2",
        control=DIR_SOFTER_BEAT,
    )

    # 14. CFG ablation на каноничной обрывающей реплике.
    canonical = "Я рассмотрела твою позицию. Она была—"
    synth(
        client,
        text=canonical,
        label="14_cfg_15",
        control=DIR_WARM_ARISTOCRATIC,
        cfg_value=1.5,
    )
    synth(
        client,
        text=canonical,
        label="15_cfg_20",
        control=DIR_WARM_ARISTOCRATIC,
        cfg_value=2.0,
    )
    synth(
        client,
        text=canonical,
        label="16_cfg_25",
        control=DIR_WARM_ARISTOCRATIC,
        cfg_value=2.5,
    )

    print(f"\n{'='*60}")
    print(f"  Готово. Файлы в {OUTPUT_DIR.resolve()}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
