"""CLI helper: vocal separation via audio-separator.

Запускается из ai/voice_clone.py::run_separator через subprocess в videotrans/.venv,
где установлен audio-separator[gpu]. В основной venv бота этот пакет ставить не нужно.

Контракт:
- argparse-аргументы: --input <abs path>, --output-dir <abs path>,
  --model <model filename> (default: mel_band_roformer_karaoke_becruily.ckpt).
- На успех: ПОСЛЕДНЕЙ непустой строкой stdout печатается абсолютный путь
  к vocals-файлу. Любые промежуточные строки прогресса audio-separator
  идут до этой строки.
- На ошибку: префикс `ERROR:` в stderr и `sys.exit(2)`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="Run audio-separator and print vocals path.")
    ap.add_argument("--input", required=True, help="Path to input audio file.")
    ap.add_argument("--output-dir", required=True, help="Directory for separated stems.")
    ap.add_argument(
        "--model",
        default="mel_band_roformer_karaoke_becruily.ckpt",
        help="audio-separator model filename.",
    )
    args = ap.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: input not found: {input_path}", file=sys.stderr)
        sys.exit(2)

    out_dir = Path(args.output_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"ERROR: cannot create output dir {out_dir}: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        from audio_separator.separator import Separator
    except Exception as exc:
        print(f"ERROR: audio_separator import failed: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        sep = Separator(
            output_dir=str(out_dir),
            output_format="WAV",
            use_autocast=True,
            mdxc_params={
                "segment_size": 256,
                "override_model_segment_size": True,
                "batch_size": 1,
                "overlap": 8,
                "pitch_shift": 0,
            },
        )
        sep.load_model(model_filename=args.model)
        files = sep.separate(str(input_path.resolve()))
    except Exception as exc:
        print(f"ERROR: separator failed: {exc}", file=sys.stderr)
        sys.exit(2)

    # audio-separator возвращает список путей к стемам. Нужен vocals — отфильтровываем
    # `*_(No Vocals)*` / `*_Instrumental*` и берём первый, где в имени есть "vocal".
    vocals_path: Path | None = None
    for f in files or []:
        stem = Path(f).stem.lower()
        if "vocal" in stem and "no" not in stem and "instrument" not in stem:
            # audio-separator может вернуть относительный путь — приводим к out_dir
            candidate = Path(f)
            if not candidate.is_absolute():
                candidate = (out_dir / candidate.name).resolve()
            else:
                candidate = candidate.resolve()
            if candidate.exists():
                vocals_path = candidate
                break

    if vocals_path is None:
        print(f"ERROR: vocals not found in output: {files}", file=sys.stderr)
        sys.exit(2)

    # Последняя строка stdout — абсолютный путь к vocals.
    print(str(vocals_path))


if __name__ == "__main__":
    main()
