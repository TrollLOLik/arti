"""Owned, disposable GPU worker for independent ASR and forced alignment."""
from __future__ import annotations

import gc
import json
import os
from pathlib import Path
import sys

import soundfile as sf

try:
    from .stt_pipeline import atomic_json
except ImportError:
    from stt_pipeline import atomic_json


LANGUAGES = dict(zip(
    "zh en yue ar de fr es pt id it ko ru th vi ja tr hi ms nl sv da fi pl cs fil fa el hu mk ro".split(),
    "Chinese English Cantonese Arabic German French Spanish Portuguese Indonesian Italian Korean Russian Thai Vietnamese Japanese Turkish Hindi Malay Dutch Swedish Danish Finnish Polish Czech Filipino Persian Greek Hungarian Macedonian Romanian".split(),
))
ALIGN_LANGUAGES = {"Chinese", "English", "Cantonese", "French", "German", "Italian", "Japanese",
                   "Korean", "Portuguese", "Russian", "Spanish"}


def language_name(code):
    if not code or code == "auto":
        return None
    return LANGUAGES.get(str(code).lower().replace("_", "-").split("-")[0], str(code))


def run(manifest_path):
    directory = manifest_path.resolve().parent
    if manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError("stt_manifest_budget")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tasks = manifest["tasks"]
    if not isinstance(tasks, list) or len(tasks) > 256:
        raise ValueError("stt_task_budget")
    for task in tasks:
        path = (directory / task["audio"]).resolve(strict=True)
        if path.parent != directory or path.suffix != ".wav" or not 0 < sf.info(path).duration <= 62:
            raise ValueError("invalid_stt_worker_audio")
        if task["mode"] not in {"align", "transcribe"} or len(task.get("text", "")) > 20000:
            raise ValueError("invalid_stt_worker_task")
    # No web UI, vLLM server, or child process is started by these model wrappers.
    import torch
    from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner
    device = manifest.get("device", "cuda:0")
    dtype = torch.bfloat16 if str(device).startswith("cuda") else torch.float32
    aligner = Qwen3ForcedAligner.from_pretrained(manifest["aligner_model"], dtype=dtype, device_map=device)
    asr = None
    if any(task["mode"] == "transcribe" for task in tasks):
        asr = Qwen3ASRModel.from_pretrained(manifest["asr_model"], dtype=dtype, device_map=device,
                                          max_inference_batch_size=1, max_new_tokens=512)
    results = {}
    for task in tasks:
        key = str(task["id"])
        try:
            language = language_name(task.get("language"))
            audio = str(directory / task["audio"])
            text = task.get("text", "")
            if task["mode"] == "transcribe":
                result = asr.transcribe(audio=audio, language=language)[0]
                text, language = result.text, result.language
            if language not in ALIGN_LANGUAGES:
                raise ValueError("unsupported_alignment_language")
            if not text.strip():
                raise ValueError("empty_local_transcript")
            aligned = aligner.align(audio=audio, text=text, language=language)[0]
            results[key] = {"text": text, "language": language,
                            "words": [{"text": item.text, "start": item.start_time, "end": item.end_time}
                                      for item in aligned]}
        except Exception as exc:
            results[key] = {"error": type(exc).__name__ + ":" + str(exc)[:200]}
        atomic_json(directory / "results.json", results)
    del asr, aligner
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


if __name__ == "__main__":
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    run(Path(sys.argv[1]))
