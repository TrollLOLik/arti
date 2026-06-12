from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import math
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import ffmpeg
import librosa
import numpy as np
import requests
import soundfile as sf
import torch
import yt_dlp
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pyannote.audio import Pipeline
from pydub import AudioSegment


AUDIO_REFERENCE_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".webm"}


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    speaker: str

    def to_json(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "speaker": self.speaker,
        }


@dataclass(frozen=True)
class WordTiming:
    text: str
    start: float
    end: float
    speaker: str = "UNKNOWN"
    confidence: float | None = None

    def to_json(self) -> dict[str, Any]:
        payload = {
            "text": self.text,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "speaker": self.speaker,
        }
        if self.confidence is not None:
            payload["confidence"] = round(self.confidence, 3)
        return payload



@dataclass(frozen=True)
class Phrase:
    id: int
    start: float
    end: float
    text: str
    speaker: str = "UNKNOWN"
    word_confidence_min: float | None = None
    word_confidence_avg: float | None = None
    needs_review: bool = False
    review_reason: str = ""
    words: list[WordTiming] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.01, self.end - self.start)

    def to_llm_payload(self, chars_per_sec: float = 15.0) -> dict[str, Any]:
        return {
            "id": self.id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "char_budget": max(8, int(round(self.duration * chars_per_sec))),
            "text": self.text,
            "speaker": self.speaker,
            "word_confidence_min": round(self.word_confidence_min, 3) if self.word_confidence_min is not None else None,
            "word_confidence_avg": round(self.word_confidence_avg, 3) if self.word_confidence_avg is not None else None,
            "needs_review": self.needs_review,
            "review_reason": self.review_reason,
        }

    def to_json(self) -> dict[str, Any]:
        payload = self.to_llm_payload()
        payload.pop("char_budget", None)
        payload["duration"] = round(self.duration, 3)
        payload["words"] = [word.to_json() for word in self.words]
        return payload


@dataclass(frozen=True)
class TranslatedPhrase:
    phrase: Phrase
    translated_text: str
    skip_tts: bool = False
    tts_voice: str = "default_voice.pt"
    reference_audio_path: Path | None = None
    reference_text: str = ""
    raw_audio_path: Path | None = None
    processed_audio_path: Path | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            **self.phrase.to_json(),
            "translated_text": self.translated_text,
            "skip_tts": self.skip_tts,
            "tts_voice": self.tts_voice,
            "reference_audio_path": str(self.reference_audio_path) if self.reference_audio_path else "",
            "reference_text": self.reference_text,
            "raw_audio_path": str(self.raw_audio_path) if self.raw_audio_path else None,
            "processed_audio_path": str(self.processed_audio_path) if self.processed_audio_path else None,
        }


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", nargs="?", help="YouTube video URL")
    parser.add_argument("--input-file", default=None, help="Path to local audio/video file (skips yt-dlp)")
    parser.add_argument("--audio-only", action="store_true", help="Treat input as audio-only; produce dubbed audio file instead of mp4")
    parser.add_argument("--output", default="outputs/output_dubbed.mp4")
    parser.add_argument("--summary-output", default="outputs/summary.txt")
    parser.add_argument("--work-dir", default="work")
    parser.add_argument("--assemblyai-api-key", default=os.getenv("ASSEMBLYAI_API_KEY", ""), help="AssemblyAI API key (or set ASSEMBLYAI_API_KEY env)")
    parser.add_argument("--gemini-model", default="gemini-3.1-flash-lite-preview")
    parser.add_argument("--gemini-fallback-model", default="gemini-3-flash-preview")
    parser.add_argument("--translation-batch-size", type=int, default=20)
    parser.add_argument("--dub-sample-rate", type=int, default=48000)
    parser.add_argument("--tts-backend", choices=["fish", "omnivoice", "voxcpm-demo"], default="voxcpm-demo", help="TTS backend: VoxCPM Demo Space (default), local OmniVoice/VoxCPM, or Fish Speech")
    parser.add_argument("--fish-url", default="http://127.0.0.1:8080/v1/tts", help="Fish Speech /v1/tts endpoint")
    parser.add_argument("--omnivoice-url", default="http://localhost:8000/generate")
    parser.add_argument("--omnivoice-timeout", type=float, default=120.0)
    parser.add_argument("--voxcpm-demo-space", default="openbmb/VoxCPM-Demo", help="HF Space for VoxCPM Demo (gradio_client)")
    parser.add_argument("--voxcpm-demo-cfg", type=float, default=2.0, help="cfg_value_input for Demo Space")
    parser.add_argument("--voxcpm-demo-denoise", action="store_true", help="Enable denoise=True for Demo Space")
    parser.add_argument("--voxcpm-demo-normalize", action="store_true", help="Enable do_normalize=True for Demo Space")
    parser.add_argument("--reference-min-seconds", type=float, default=3.0)
    parser.add_argument("--separator-model", default="mel_band_roformer_karaoke_becruily.ckpt")
    parser.add_argument("--separator-segment-size", type=int, default=256)
    parser.add_argument("--separator-overlap", type=int, default=8)
    parser.add_argument("--separator-no-autocast", action="store_true")
    parser.add_argument("--mix-mode", choices=["dub", "voiceover"], default="voiceover", help="voiceover uses original audio background; dub uses separated no_vocals background")
    parser.add_argument("--original-volume-db", type=float, default=-14.0, help="Reduce original audio by this many dB before overlaying Russian dub (voiceover duck)")
    parser.add_argument("--asr-low-confidence-threshold", type=float, default=0.5)
    parser.add_argument("--asr-critical-confidence-threshold", type=float, default=0.3)
    parser.add_argument("--asr-request-timeout", type=float, default=120.0)
    parser.add_argument("--asr-poll-timeout", type=float, default=3600.0)
    parser.add_argument("--min-tts-duration", type=float, default=0.6)
    parser.add_argument("--diarization-model", default="models/pyannote-speaker-diarization-3.1")
    parser.add_argument("--disable-diarization", action="store_true")
    parser.add_argument("--max-pause", type=float, default=1.5, help="Max pause between words to merge into one phrase (seconds)")
    parser.add_argument("--max-phrase-length", type=float, default=15.0, help="Max merged phrase duration before forcing a split (seconds)")
    parser.add_argument("--min-phrase-duration", type=float, default=0.4, help="Drop single-word phrases shorter than this (seconds) as likely hallucinations")
    parser.add_argument("--word-min-overlap", type=float, default=0.5, help="Minimum fraction of word duration that must overlap with a pyannote turn to assign a speaker")
    parser.add_argument("--max-speedup", type=float, default=1.8)
    parser.add_argument("--max-overflow", type=float, default=1.0, help="Max seconds a sped-up phrase may overlap the next slot before being hard-clipped")
    parser.add_argument("--tts-runaway-retries", type=int, default=2, help="Regeneration attempts when TTS output is anomalously long for its slot")
    parser.add_argument("--translation-chars-per-sec", type=float, default=15.0, help="Russian speech rate used to compute per-phrase character budget for translation")
    parser.add_argument("--tts-shorten-retries", type=int, default=2, help="LLM shorten-and-regenerate attempts when TTS output overshoots its slot")
    parser.add_argument("--tts-shorten-trigger", type=float, default=1.25, help="Shorten the line when raw TTS duration exceeds the slot by this factor")
    parser.add_argument("--snap-window", type=float, default=0.4, help="Max seconds each phrase boundary may shift towards real silence on the vocals track")
    parser.add_argument("--no-snap-boundaries", action="store_true", help="Disable snapping phrase boundaries to silence on the vocals track")
    parser.add_argument("--per-phrase-reference", action="store_true", help="Crop a fresh TTS reference around every phrase instead of one cached reference per speaker")
    parser.add_argument("--cookies", default=None, help="Path to cookies file for yt-dlp (e.g. exported from browser)")
    parser.add_argument("--cookies-from-browser", default=None, help="Browser to extract cookies from (e.g. chrome, firefox, edge)")
    parser.add_argument("--dub-plan", default="outputs/dub_plan.json")
    parser.add_argument("--no-hardsubs", action="store_true", help="Disable burning animated ASS subtitles into the final video")
    parser.add_argument("--subtitles-output", default=None, help="Path for generated animated .ass subtitles")
    parser.add_argument("--subtitle-font", default="Montserrat SemiBold")
    parser.add_argument("--subtitle-font-size", type=int, default=44)
    parser.add_argument("--no-review-pause", action="store_true")
    parser.add_argument("--stage", choices=["full", "prepare", "polish", "tts"], default="full")
    parser.add_argument("--keep-temp", action="store_true")
    return parser.parse_args()


def ensure_cuda() -> None:
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required, but PyTorch does not see an NVIDIA GPU")
    logging.info("CUDA device: %s", torch.cuda.get_device_name(0))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "1", "yes"}


def is_audio_reference_path(value: str) -> bool:
    if not value:
        return False
    return Path(value).suffix.lower() in AUDIO_REFERENCE_EXTENSIONS


def validate_reference_audio_path(path: Path, segment_id: int) -> Path:
    if path.suffix.lower() not in AUDIO_REFERENCE_EXTENSIONS:
        raise ValueError(f"reference_audio_path for segment {segment_id} must be an audio file: {path}")
    if not path.exists():
        raise FileNotFoundError(f"reference_audio_path for segment {segment_id} does not exist: {path}")
    return path


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def chunked(items: list[Phrase], size: int) -> Iterable[list[Phrase]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def progress_hook(status: dict[str, Any]) -> None:
    if status.get("status") == "downloading":
        percent = status.get("_percent_str", "").strip()
        speed = status.get("_speed_str", "").strip()
        eta = status.get("_eta_str", "").strip()
        if percent:
            logging.info("yt-dlp: %s, %s, ETA %s", percent, speed, eta)
    if status.get("status") == "finished":
        logging.info("yt-dlp: download finished, merging streams")


def download_video(url: str, download_dir: Path, cookies: str | None = None, cookies_from_browser: str | None = None) -> Path:
    download_dir.mkdir(parents=True, exist_ok=True)
    options = {
        "format": "best",
        "remote_components": ["ejs:npm"],
        "outtmpl": str(download_dir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "progress_hooks": [progress_hook],
    }
    if cookies_from_browser:
        options["cookiesfrombrowser"] = (cookies_from_browser, None, None, None)
        logging.info("Using cookies from browser: %s", cookies_from_browser)
    elif cookies:
        options["cookiefile"] = cookies
        logging.info("Using cookies file: %s", cookies)
    logging.info("Downloading video")
    with yt_dlp.YoutubeDL(options) as downloader:
        downloader.extract_info(url, download=True)
    candidates = [path for path in download_dir.glob("source.*") if path.is_file()]
    if not candidates:
        raise FileNotFoundError("yt-dlp did not produce a video file")
    video_path = max(candidates, key=lambda path: path.stat().st_mtime)
    logging.info("Downloaded video: %s", video_path)
    return video_path


def run_ffmpeg(stream: ffmpeg.nodes.OutputStream) -> None:
    try:
        stream.overwrite_output().run(capture_stdout=True, capture_stderr=True)
    except ffmpeg.Error as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise RuntimeError(stderr) from exc


def extract_audio(video_path: Path, audio_path: Path, sample_rate: int = 16000, channels: int = 1) -> None:
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    logging.info("Extracting original audio: %s", audio_path)
    stream = ffmpeg.input(str(video_path)).output(
        str(audio_path),
        acodec="pcm_s16le",
        ac=channels,
        ar=sample_rate,
        vn=None,
    )
    run_ffmpeg(stream)


def extract_audio_native(video_path: Path, audio_path: Path) -> None:
    """Extract audio stream without re-encoding (preserves original codec/format)."""
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    logging.info("Extracting native audio for transcription: %s", audio_path)
    stream = ffmpeg.input(str(video_path)).output(
        str(audio_path),
        acodec="copy",
        vn=None,
    )
    run_ffmpeg(stream)


def normalize_wav(input_path: Path, output_path: Path, sample_rate: int, channels: int = 1) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stream = ffmpeg.input(str(input_path)).output(
        str(output_path),
        acodec="pcm_s16le",
        ac=channels,
        ar=sample_rate,
    )
    run_ffmpeg(stream)


def _pick_separator_output(output_files: list[str], output_dir: Path, keywords: tuple[str, ...], label: str) -> Path:
    for output_file in output_files:
        output_path = Path(output_file)
        name = output_path.name.lower()
        if any(keyword in name for keyword in keywords):
            return output_path if output_path.is_absolute() else output_dir / output_path
    raise FileNotFoundError(f"audio-separator did not produce a {label} track. Outputs: {output_files}")


def separate_audio_roformer(input_path: Path, output_dir: Path, args: argparse.Namespace) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.info("Running audio-separator source separation: %s", args.separator_model)
    try:
        from audio_separator.separator import Separator

        separator = Separator(
            output_dir=str(output_dir),
            output_format="WAV",
            use_autocast=not args.separator_no_autocast,
            mdxc_params={
                "segment_size": args.separator_segment_size,
                "override_model_segment_size": True,
                "batch_size": 1,
                "overlap": args.separator_overlap,
                "pitch_shift": 0,
            },
        )
        separator.load_model(model_filename=args.separator_model)
        output_files = separator.separate(str(input_path))
    except ImportError as exc:
        raise RuntimeError('audio-separator is not installed. Install it with: pip install "audio-separator[gpu]"') from exc
    except Exception as exc:
        raise RuntimeError(f"audio-separator failed: {exc}") from exc
    vocals_raw = _pick_separator_output(output_files, output_dir, ("vocals", "vocal"), "vocal")
    no_vocals_raw = _pick_separator_output(output_files, output_dir, ("instrumental", "no_vocals", "no-vocals", "karaoke"), "instrumental")
    no_vocals_path = output_dir / "no_vocals.wav"
    normalize_wav(no_vocals_raw, no_vocals_path, sample_rate=args.dub_sample_rate, channels=1)
    logging.info("audio-separator vocals saved: %s", vocals_raw)
    logging.info("audio-separator background saved: %s", no_vocals_path)
    return vocals_raw, no_vocals_path


def split_audio_into_chunks(audio_path: Path, chunk_dir: Path, chunk_duration_sec: int = 1200) -> list[Path]:
    chunk_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(chunk_dir / "chunk_%03d.wav")
    stream = ffmpeg.input(str(audio_path)).output(
        pattern,
        f="segment",
        segment_time=chunk_duration_sec,
        acodec="pcm_s16le",
        ac=1,
        ar=16000,
        reset_timestamps=1,
    )
    run_ffmpeg(stream)
    return sorted(chunk_dir.glob("chunk_*.wav"))


def _assemblyai_headers(api_key: str) -> dict[str, str]:
    if not api_key:
        raise RuntimeError("ASSEMBLYAI_API_KEY is not set (use --assemblyai-api-key or env)")
    return {"authorization": api_key}


def _request_with_retries(method: str, url: str, *, timeout: float, attempts: int = 3, **kwargs: Any) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            data = kwargs.get("data")
            if hasattr(data, "seek"):
                data.seek(0)
            response = requests.request(method, url, timeout=timeout, **kwargs)
            if response.status_code not in {429, 500, 502, 503, 504}:
                return response
            last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
        except requests.RequestException as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(min(2 ** attempt, 10))
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Request failed after {attempts} attempts: {method} {url}")


def _upload_audio_assemblyai(audio_path: Path, headers: dict[str, str], timeout: float) -> str:
    url = "https://api.assemblyai.com/v2/upload"
    with open(audio_path, "rb") as f:
        response = _request_with_retries("POST", url, headers=headers, data=f, timeout=timeout)
    response.raise_for_status()
    return response.json()["upload_url"]


def _submit_transcript_assemblyai(upload_url: str, headers: dict[str, str], timeout: float) -> str:
    url = "https://api.assemblyai.com/v2/transcript"
    payload = {
        "audio_url": upload_url,
        "language_detection": True,
        "speech_models": ["universal-3-pro", "universal-2"],
        "speaker_labels": True,
        "temperature": 0
    }
    response = _request_with_retries("POST", url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()["id"]


def _poll_transcript_assemblyai(transcript_id: str, headers: dict[str, str], request_timeout: float, poll_timeout: float) -> dict[str, Any]:
    url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
    started_at = time.monotonic()
    while True:
        if time.monotonic() - started_at > poll_timeout:
            raise TimeoutError(f"AssemblyAI polling timed out after {poll_timeout:.0f}s")
        response = _request_with_retries("GET", url, headers=headers, timeout=request_timeout)
        response.raise_for_status()
        result = response.json()
        status = result.get("status")
        if status == "completed":
            return result
        elif status == "error":
            raise RuntimeError(f"AssemblyAI transcription failed: {result.get('error', 'unknown error')}")
        logging.info("AssemblyAI polling: status=%s", status)
        time.sleep(3)


def _parse_assemblyai_words(result: dict[str, Any]) -> list[WordTiming]:
    words: list[WordTiming] = []
    for w in result.get("words", []):
        text = normalize_text(str(w.get("text", "")))
        start_ms = w.get("start")
        end_ms = w.get("end")
        speaker = w.get("speaker", "UNKNOWN")
        raw_confidence = w.get("confidence")
        confidence = float(raw_confidence) if raw_confidence is not None else None
        if text and start_ms is not None and end_ms is not None:
            start_sec = float(start_ms) / 1000.0
            end_sec = float(end_ms) / 1000.0
            if end_sec > start_sec:
                words.append(WordTiming(text=text, start=start_sec, end=end_sec, speaker=speaker, confidence=confidence))
    return words


def _parse_assemblyai_utterances(result: dict[str, Any]) -> list[Phrase]:
    phrases: list[Phrase] = []
    for u in result.get("utterances", []):
        text = normalize_text(str(u.get("text", "")))
        start_ms = u.get("start")
        end_ms = u.get("end")
        speaker = str(u.get("speaker", "UNKNOWN"))
        if text and start_ms is not None and end_ms is not None:
            start_sec = float(start_ms) / 1000.0
            end_sec = float(end_ms) / 1000.0
            if end_sec > start_sec:
                phrases.append(
                    Phrase(
                        id=len(phrases),
                        start=start_sec,
                        end=end_sec,
                        text=text,
                        speaker=speaker,
                    )
                )
    return phrases


def transcribe_audio(audio_path: Path, raw_result_path: Path, args: argparse.Namespace) -> tuple[list[WordTiming], list[Phrase]]:
    headers = _assemblyai_headers(args.assemblyai_api_key)
    logging.info("Uploading audio to AssemblyAI: %s", audio_path.name)
    upload_url = _upload_audio_assemblyai(audio_path, headers, args.asr_request_timeout)
    transcript_id = _submit_transcript_assemblyai(upload_url, headers, args.asr_request_timeout)
    logging.info("AssemblyAI transcript submitted: %s", transcript_id)
    result = _poll_transcript_assemblyai(transcript_id, headers, args.asr_request_timeout, args.asr_poll_timeout)
    save_json(raw_result_path, result)
    logging.info("AssemblyAI raw result saved: %s", raw_result_path)
    words = _parse_assemblyai_words(result)
    utterances = _parse_assemblyai_utterances(result)
    logging.info("AssemblyAI transcription words: %d, utterances: %d", len(words), len(utterances))
    if not words:
        raise RuntimeError("AssemblyAI transcription produced no words")
    return words, utterances


def diarize_speakers(audio_path: Path, args: argparse.Namespace) -> list[SpeakerTurn]:
    model_path = Path(args.diarization_model)
    if not model_path.exists():
        raise FileNotFoundError(
            "Local pyannote diarization model was not found: "
            f"{model_path.resolve()}. Put the model files there or pass --diarization-model with a local path."
        )
    logging.info("Loading local pyannote diarization model on CUDA: %s", model_path)
    pipeline = Pipeline.from_pretrained(str(model_path))
    pipeline.to(torch.device("cuda"))
    logging.info("Running speaker diarization")
    diarization = pipeline(str(audio_path))
    turns = extract_speaker_turns(diarization)
    logging.info("Diarization turns: %d, speakers: %d", len(turns), len({turn.speaker for turn in turns}))
    return turns


def extract_speaker_turns(diarization: Any) -> list[SpeakerTurn]:
    annotation = diarization
    if not hasattr(annotation, "itertracks"):
        annotation = getattr(diarization, "speaker_diarization", None)
    if annotation is None and hasattr(diarization, "serialize"):
        serialized = diarization.serialize()
        turns = []
        for item in serialized.get("diarization", serialized if isinstance(serialized, list) else []):
            start = float(item.get("start", 0))
            end = float(item.get("end", 0))
            speaker = str(item.get("speaker", "UNKNOWN"))
            if end > start:
                turns.append(SpeakerTurn(start=start, end=end, speaker=speaker))
        return turns
    if annotation is None or not hasattr(annotation, "itertracks"):
        raise TypeError(f"Unsupported diarization output type: {type(diarization).__name__}")
    turns: list[SpeakerTurn] = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        start = float(turn.start)
        end = float(turn.end)
        if end > start:
            turns.append(SpeakerTurn(start=start, end=end, speaker=str(speaker)))
    return turns


def span_turn_overlap(start: float, end: float, turn: SpeakerTurn) -> float:
    return max(0.0, min(end, turn.end) - max(start, turn.start))


def speaker_for_word(word: WordTiming, turns: list[SpeakerTurn], min_overlap_ratio: float = 0.5) -> str:
    # Assigns a word to a pyannote turn only if their temporal overlap covers a sufficient
    # fraction of the word. This filters out Whisper hallucinations on music/silence.
    if not turns:
        return "UNKNOWN"
    word_duration = max(0.001, word.end - word.start)
    best_speaker = "UNKNOWN"
    best_overlap = 0.0
    for turn in turns:
        overlap = span_turn_overlap(word.start, word.end, turn)
        if overlap > best_overlap:
            best_speaker = turn.speaker
            best_overlap = overlap
    if best_overlap / word_duration < min_overlap_ratio:
        return "UNKNOWN"
    return best_speaker


def nearest_turn_speaker(word: WordTiming, turns: list[SpeakerTurn], max_gap: float = 1.5) -> str:
    # Fallback when no pyannote turn meaningfully overlaps the word: pick the closest turn
    # within max_gap seconds. This keeps speaker IDs consistent (avoids mixing pyannote's
    # SPEAKER_00 with AssemblyAI's "A" for the same person).
    if not turns:
        return "UNKNOWN"
    word_mid = (word.start + word.end) / 2.0
    best_speaker = "UNKNOWN"
    best_distance = float("inf")
    for turn in turns:
        if turn.start <= word_mid <= turn.end:
            distance = 0.0
        else:
            distance = min(abs(word_mid - turn.start), abs(word_mid - turn.end))
        if distance < best_distance:
            best_distance = distance
            best_speaker = turn.speaker
    if best_distance > max_gap:
        return "UNKNOWN"
    return best_speaker


def speaker_for_phrase(phrase: Phrase, turns: list[SpeakerTurn], max_gap: float = 1.5) -> str:
    if not turns:
        return phrase.speaker
    best_speaker = "UNKNOWN"
    best_overlap = 0.0
    for turn in turns:
        overlap = span_turn_overlap(phrase.start, phrase.end, turn)
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = turn.speaker
    if best_speaker != "UNKNOWN":
        return best_speaker
    phrase_mid = (phrase.start + phrase.end) / 2.0
    best_distance = float("inf")
    for turn in turns:
        if turn.start <= phrase_mid <= turn.end:
            distance = 0.0
        else:
            distance = min(abs(phrase_mid - turn.start), abs(phrase_mid - turn.end))
        if distance < best_distance:
            best_distance = distance
            best_speaker = turn.speaker
    if best_distance > max_gap:
        return phrase.speaker
    return best_speaker


def map_phrase_speakers_to_turns(phrases: list[Phrase], turns: list[SpeakerTurn]) -> list[Phrase]:
    if not turns:
        return phrases
    unique_turn_speakers = {turn.speaker for turn in turns}
    if len(unique_turn_speakers) == 1:
        single_speaker = next(iter(unique_turn_speakers))
        return [
            Phrase(
                id=phrase.id,
                start=phrase.start,
                end=phrase.end,
                text=phrase.text,
                speaker=single_speaker,
                word_confidence_min=phrase.word_confidence_min,
                word_confidence_avg=phrase.word_confidence_avg,
                needs_review=phrase.needs_review,
                review_reason=phrase.review_reason,
                words=phrase.words,
            )
            for phrase in phrases
        ]
    mapped: list[Phrase] = []
    changed = 0
    for phrase in phrases:
        speaker = speaker_for_phrase(phrase, turns)
        if speaker != phrase.speaker:
            changed += 1
        mapped.append(
            Phrase(
                id=phrase.id,
                start=phrase.start,
                end=phrase.end,
                text=phrase.text,
                speaker=speaker,
                word_confidence_min=phrase.word_confidence_min,
                word_confidence_avg=phrase.word_confidence_avg,
                needs_review=phrase.needs_review,
                review_reason=phrase.review_reason,
                words=phrase.words,
            )
        )
    logging.info("Mapped AssemblyAI utterance speakers to pyannote turns: %d/%d changed", changed, len(phrases))
    return mapped


def merge_words_into_phrases(
    words: list[WordTiming],
    turns: list[SpeakerTurn],
    max_pause: float,
    max_phrase_length: float,
    min_phrase_duration: float = 0.4,
    word_min_overlap: float = 0.5,
    low_confidence_threshold: float = 0.5,
    critical_confidence_threshold: float = 0.3,
    min_tts_duration: float = 0.0,
) -> list[Phrase]:
    # Merges flat word list into phrases by time, speaker, pause, punctuation, and hard duration cap.
    # Drops single-word phrases shorter than min_phrase_duration (likely Whisper hallucinations).
    if not words:
        raise RuntimeError("Word-level alignment produced no phrases (empty word list)")
    words.sort(key=lambda w: w.start)
    phrases: list[Phrase] = []
    current_words: list[WordTiming] = []
    current_speaker: str = "UNKNOWN"
    dropped_phantom = 0

    def flush() -> None:
        nonlocal current_words, dropped_phantom
        if not current_words:
            return
        text = normalize_text(" ".join(w.text for w in current_words))
        start = current_words[0].start
        end = current_words[-1].end
        duration = end - start
        confidences = [w.confidence for w in current_words if w.confidence is not None]
        confidence_min = min(confidences) if confidences else None
        confidence_avg = sum(confidences) / len(confidences) if confidences else None
        review_reasons: list[str] = []
        if duration < min_tts_duration:
            review_reasons.append(f"short_duration<{min_tts_duration:.2f}s")
        if confidence_min is not None and confidence_min < critical_confidence_threshold:
            review_reasons.append(f"critical_asr_confidence<{critical_confidence_threshold:.2f}")
        elif confidence_min is not None and confidence_min < low_confidence_threshold:
            review_reasons.append(f"low_asr_confidence<{low_confidence_threshold:.2f}")
        is_phantom = len(current_words) == 1 and (end - start) < min_phrase_duration
        if text and not is_phantom:
            phrases.append(
                Phrase(
                    id=len(phrases),
                    start=start,
                    end=end,
                    text=text,
                    speaker=current_speaker,
                    word_confidence_min=confidence_min,
                    word_confidence_avg=confidence_avg,
                    needs_review=bool(review_reasons),
                    review_reason=", ".join(review_reasons),
                    words=list(current_words),
                )
            )
        elif is_phantom:
            dropped_phantom += 1
        current_words = []

    dropped_unknown = 0
    for word in words:
        if turns:
            speaker = speaker_for_word(word, turns, min_overlap_ratio=word_min_overlap)
            if speaker == "UNKNOWN":
                # Fallback: pick nearest pyannote turn so we don't drop edge words and
                # keep speaker IDs consistent (avoid mixing pyannote SPEAKER_xx with
                # AssemblyAI's "A"/"B" for the same person).
                speaker = nearest_turn_speaker(word, turns)
        else:
            speaker = word.speaker
        if speaker == "UNKNOWN":
            dropped_unknown += 1
            continue
        if current_words:
            pause = word.start - current_words[-1].end
            would_exceed_length = (word.end - current_words[0].start) > max_phrase_length
            if speaker != current_speaker or pause >= max_pause or would_exceed_length:
                flush()
        current_speaker = speaker
        current_words.append(word)
        if word.text.strip().endswith((".", "!", "?")):
            flush()
    flush()

    logging.info(
        "Smart-merged phrases: %d (max_pause=%.2fs, max_phrase_length=%.2fs, needs_review=%d, dropped_phantom=%d, dropped_unknown=%d)",
        len(phrases),
        max_pause,
        max_phrase_length,
        sum(1 for phrase in phrases if phrase.needs_review),
        dropped_phantom,
        dropped_unknown,
    )
    if not phrases:
        raise RuntimeError("Word-level merge produced no phrases")
    return phrases


def shorten_line_with_llm(text: str, target_chars: int, args: argparse.Namespace) -> str:
    # Asks the LLM to compress one dubbing line to fit its time slot; returns the
    # original text unchanged if the model fails or does not actually shorten it.
    prompt = (
        "Сократи реплику русского дубляжа так, чтобы она укладывалась в "
        f"{target_chars} символов (включая пробелы), сохранив смысл и разговорное звучание. "
        "Убирай вводные слова, используй короткие синонимы. "
        "Верни ТОЛЬКО JSON вида {\"text\": \"...\"} без пояснений.\n"
        f"Реплика: {json.dumps(text, ensure_ascii=False)}"
    )
    try:
        parsed = extract_json_object(call_llm(prompt, args))
        shortened = normalize_text(str(parsed.get("text", "")))
    except Exception as exc:
        logging.warning("Shorten LLM call failed: %s", str(exc)[:200])
        return text
    if not shortened or len(shortened) >= len(text):
        return text
    return shortened


def _clone_phrase_with_bounds(phrase: Phrase, start: float, end: float) -> Phrase:
    return Phrase(
        id=phrase.id,
        start=start,
        end=end,
        text=phrase.text,
        speaker=phrase.speaker,
        word_confidence_min=phrase.word_confidence_min,
        word_confidence_avg=phrase.word_confidence_avg,
        needs_review=phrase.needs_review,
        review_reason=phrase.review_reason,
        words=phrase.words,
    )


def snap_phrases_to_silence(
    phrases: list[Phrase],
    vocals_path: Path,
    window: float = 0.4,
    pad: float = 0.05,
    sample_rate: int = 16000,
) -> list[Phrase]:
    # ASR word timings drift by 100-300ms; this snaps each phrase boundary to the
    # actual speech onset/offset found on the separated vocals track (RMS energy),
    # so dub slots line up with real speech instead of the ASR estimate.
    if not phrases or window <= 0:
        return phrases
    audio = read_audio_mono(vocals_path, sample_rate)
    if audio.size == 0:
        return phrases
    hop = max(1, int(sample_rate * 0.010))
    frame = max(hop, int(sample_rate * 0.025))
    rms = librosa.feature.rms(y=audio, frame_length=frame, hop_length=hop)[0]
    if rms.size == 0:
        return phrases
    noise_floor = float(np.percentile(rms, 10))
    threshold = max(noise_floor * 4.0, float(rms.max()) * 0.04)
    times = np.arange(rms.size) * (hop / sample_rate)
    is_speech = rms >= threshold

    def speech_onset(lo: float, hi: float) -> float | None:
        mask = (times >= lo) & (times <= hi)
        idx = np.flatnonzero(mask & is_speech)
        if idx.size == 0:
            return None
        return float(times[idx[0]])

    def speech_offset(lo: float, hi: float) -> float | None:
        mask = (times >= lo) & (times <= hi)
        idx = np.flatnonzero(mask & is_speech)
        if idx.size == 0:
            return None
        return float(times[idx[-1]]) + hop / sample_rate

    snapped: list[Phrase] = []
    moved = 0
    total_shift = 0.0
    for i, phrase in enumerate(phrases):
        prev_end = snapped[-1].end if snapped else 0.0
        next_start = phrases[i + 1].start if i + 1 < len(phrases) else float("inf")
        onset = speech_onset(phrase.start - window, phrase.start + window)
        offset = speech_offset(phrase.end - window, phrase.end + window)
        new_start = phrase.start if onset is None else max(0.0, onset - pad)
        new_end = phrase.end if offset is None else offset + pad
        new_start = max(new_start, prev_end + 0.01)
        new_end = min(new_end, next_start - 0.01) if next_start != float("inf") else new_end
        if new_end - new_start < 0.1:
            snapped.append(phrase)
            continue
        shift = abs(new_start - phrase.start) + abs(new_end - phrase.end)
        if shift > 0.005:
            moved += 1
            total_shift += shift
        snapped.append(_clone_phrase_with_bounds(phrase, new_start, new_end))
    logging.info(
        "Snapped phrase boundaries to silence: %d/%d adjusted (avg shift %.0fms, window %.2fs)",
        moved, len(phrases), (total_shift / moved * 1000.0) if moved else 0.0, window,
    )
    return snapped


def _build_gemini_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=api_key)


def _gemini_generate(client: genai.Client, model: str, prompt: str) -> str:
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text="Ты профессиональный переводчик и редактор дубляжа.\n\n" + prompt
                ),
            ],
        ),
    ]
    config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
    )
    chunks: list[str] = []
    for chunk in client.models.generate_content_stream(
        model=model,
        contents=contents,
        config=config,
    ):
        if text := chunk.text:
            chunks.append(text)
    result = "".join(chunks).strip()
    if not result:
        raise RuntimeError(f"Gemini ({model}) returned an empty response")
    return result


def call_llm(prompt: str, args: argparse.Namespace) -> str:
    client = _build_gemini_client()
    primary = args.gemini_model
    fallback = args.gemini_fallback_model
    try:
        return _gemini_generate(client, primary, prompt)
    except Exception as exc:
        logging.warning("Primary model %s failed: %s — switching to fallback %s", primary, exc, fallback)
        return _gemini_generate(client, fallback, prompt)


def extract_json_value(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    object_start = stripped.find("{")
    object_end = stripped.rfind("}")
    array_start = stripped.find("[")
    array_end = stripped.rfind("]")
    if array_start != -1 and array_end != -1 and (object_start == -1 or array_start < object_start):
        return json.loads(stripped[array_start : array_end + 1])
    start = object_start
    end = object_end
    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM response does not contain JSON")
    return json.loads(stripped[start : end + 1])


def extract_json_object(text: str) -> dict[str, Any]:
    parsed = extract_json_value(text)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response does not contain a JSON object")
    return parsed


def build_translation_prompt(phrases: list[Phrase], chars_per_sec: float = 15.0) -> str:
    payload = [phrase.to_llm_payload(chars_per_sec) for phrase in phrases]
    return (
        "You are an expert anime dubbing director, Russian script adapter, and ASR cleanup specialist.\n"
        "I will provide a JSON list of transcribed phrase objects. Return ONLY a valid JSON array with one object for every input id.\n\n"
        "For each phrase, decide whether it is SPEECH or NON_LEXICAL_REACTION.\n\n"
        "NON_LEXICAL_REACTION rule:\n"
        "Set skip_tts=true when the phrase contains ONLY reaction/filler/noise tokens and no real spoken content. "
        "Before deciding, ignore case, punctuation, quotes, dashes, repeated letters, repeated syllables, spaces, and elongation. "
        "Treat standalone or repeated forms of these as NON_LEXICAL_REACTION: huh, uh, um, er, eh, ah, aah, ahh, aaah, agh, ugh, ooh, oh, wow, whoa, oof, ow, ouch, hm, hmm, mhm, mm, shh, phew, ha, haha, hahaha, hahahaha, hehe, heh, lol, gasp, sigh, sob, sniff, pant, groan, grunt, bwah, wah, waah, aww, eek, yikes, yo, hey, huhh, а, ах, ох, ух, эх, эм, мм, хм, ха, хаха, хи-хи, тсс. "
        "Examples that MUST be skip_tts=true: \"Huh?\", \"Ah!\", \"Ahh...\", \"Aah!\", \"Ugh!\", \"Bwah!\", \"Hahaha!\", \"Haha\", \"Oh!\", \"Wow!\", \"Oof\", \"Hm?\", \"Hmm\", \"Shh!\", \"Uh...\", \"Eh?\".\n"
        "When skip_tts=true, set translated_text to an empty string \"\".\n\n"
        "SPEECH rule:\n"
        "Set skip_tts=false if the phrase contains ANY actual spoken word, command, name, profanity, sentence, or meaningful utterance. "
        "Do NOT skip short real lines such as \"No!\", \"Fuck!\", \"Release!\", \"Found it!\", \"Oh no.\", \"Wow, everyone is here.\", \"Huh, I see.\", \"Hey, stop!\". "
        "For speech, translated_text must be a natural Russian dubbing line.\n\n"
        "translated_text rules for speech:\n"
        "Translate/adapt into natural Russian, not literal Russian. "
        "HARD LENGTH BUDGET: each phrase object has a char_budget field — the maximum number of characters (including spaces) "
        "that fits the original duration at natural Russian speech rate. translated_text MUST NOT exceed char_budget; "
        "if a literal translation is longer, compress it: drop filler words, use shorter synonyms, restructure the sentence. "
        "A line slightly under budget is always better than one over budget. "
        "Fix obvious ASR hallucinations, wrong words, broken names, malformed catchphrases, and language switches using nearby context in the batch. "
        "Remove profanity censorship based on context, e.g. F*** -> Блядь/Чёрт/нахрен. "
        "Make the line sound like real spoken anime/dialogue performance.\n\n"
        "Do NOT change id, start, end, duration, or speaker values. "
        "Each output item must include id, translated_text, and skip_tts.\n"
        f"Входные фразы:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def parse_translation_response(content: str) -> dict[int, tuple[str, bool]]:
    parsed = extract_json_value(content)
    translations = parsed.get("translations") if isinstance(parsed, dict) else parsed
    if not isinstance(translations, list):
        raise ValueError("LLM JSON must contain a translations array")
    by_id: dict[int, tuple[str, bool]] = {}
    for item in translations:
        if isinstance(item, dict) and "id" in item:
            skip_tts = parse_bool(item.get("skip_tts", False))
            raw_text = item.get("translated_text", item.get("text", ""))
            text = normalize_text(str(raw_text))
            if skip_tts:
                by_id[int(item["id"])] = ("", True)
            elif text:
                by_id[int(item["id"])] = (text, False)
    return by_id


def translate_batch_with_retries(batch: list[Phrase], args: argparse.Namespace, batch_index: int) -> dict[int, tuple[str, bool]]:
    prompt = build_translation_prompt(batch, args.translation_chars_per_sec)
    content = call_llm(prompt, args)
    by_id = parse_translation_response(content)
    missing = [phrase for phrase in batch if phrase.id not in by_id]
    if missing:
        logging.warning(
            "Translation batch %d missed segment ids: %s; retrying one by one",
            batch_index,
            ", ".join(str(phrase.id) for phrase in missing),
        )
    for phrase in missing:
        try:
            retry_content = call_llm(build_translation_prompt([phrase], args.translation_chars_per_sec), args)
            retry_by_id = parse_translation_response(retry_content)
            retry_item = retry_by_id.get(phrase.id)
            if retry_item:
                by_id[phrase.id] = retry_item
                continue
            logging.warning("LLM retry did not return translation for segment %d; using source text fallback", phrase.id)
        except Exception as exc:
            logging.warning("LLM retry failed for segment %d: %s; using source text fallback", phrase.id, exc)
        by_id[phrase.id] = (phrase.text, False)
    return by_id


def translate_phrases(phrases: list[Phrase], args: argparse.Namespace) -> list[TranslatedPhrase]:
    translated: list[TranslatedPhrase] = []
    for batch_index, batch in enumerate(chunked(phrases, args.translation_batch_size), start=1):
        logging.info("Translating batch %d", batch_index)
        by_id = translate_batch_with_retries(batch, args, batch_index)
        for phrase in batch:
            item = by_id.get(phrase.id)
            if not item:
                logging.warning("Missing translation for segment %d after retries; using source text fallback", phrase.id)
                item = (phrase.text, False)
            text, skip_tts = item
            translated.append(TranslatedPhrase(phrase=phrase, translated_text=normalize_text(text), skip_tts=skip_tts))
    return translated


def polish_dub_plan(phrases: list[TranslatedPhrase], args: argparse.Namespace) -> list[TranslatedPhrase]:
    polished: list[TranslatedPhrase] = []
    for batch_index, batch in enumerate(chunked(phrases, args.translation_batch_size), start=1):
        logging.info("Polishing dub plan batch %d", batch_index)
        prompt = build_translation_prompt([item.phrase for item in batch], args.translation_chars_per_sec)
        draft_payload = []
        for item in batch:
            payload = item.to_json()
            payload.pop("words", None)
            draft_payload.append(payload)
        prompt += (
            "\n\nImportant adaptation pass:\n"
            "The existing draft Russian lines may be literal or weak. Rewrite them as punchy, natural spoken Russian for dubbing. "
            "Do not copy the previous translated_text unless it is already excellent. Prefer short colloquial lines that fit timing. "
            "Fix wrong names/ASR hallucinations when context makes it obvious. Keep catchphrases recognizable but natural in Russian. "
            "Return only JSON for every input id.\n"
            f"Existing draft dub plan:\n{json.dumps(draft_payload, ensure_ascii=False, indent=2)}"
        )
        by_id = parse_translation_response(call_llm(prompt, args))
        for item in batch:
            text, skip_tts = by_id.get(item.phrase.id, (item.translated_text, item.skip_tts))
            polished.append(
                TranslatedPhrase(
                    phrase=item.phrase,
                    translated_text=normalize_text(text),
                    skip_tts=skip_tts,
                    tts_voice=item.tts_voice,
                    reference_audio_path=item.reference_audio_path,
                    reference_text=item.reference_text,
                )
            )
    return polished


def save_dub_plan(path: Path, phrases: list[TranslatedPhrase]) -> None:
    payload = []
    for item in phrases:
        phrase = item.phrase
        payload.append(
            {
                "id": phrase.id,
                "start": round(phrase.start, 3),
                "end": round(phrase.end, 3),
                "text": phrase.text,
                "translated_text": item.translated_text,
                "skip_tts": item.skip_tts,
                "api_speaker": phrase.speaker,
                "word_confidence_min": round(phrase.word_confidence_min, 3) if phrase.word_confidence_min is not None else None,
                "word_confidence_avg": round(phrase.word_confidence_avg, 3) if phrase.word_confidence_avg is not None else None,
                "needs_review": phrase.needs_review,
                "review_reason": phrase.review_reason,
                "tts_voice": item.tts_voice,
                "reference_audio_path": str(item.reference_audio_path) if item.reference_audio_path else "",
                "reference_text": item.reference_text,
                "words": [word.to_json() for word in phrase.words],
            }
        )
    save_json(path, payload)


def load_dub_plan(path: Path) -> list[TranslatedPhrase]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    phrases: list[TranslatedPhrase] = []
    for item in payload:
        skip_tts = parse_bool(item.get("skip_tts", False))
        translated_text = "" if skip_tts else normalize_text(str(item.get("translated_text") or item.get("text", "")))
        tts_voice = str(item.get("tts_voice") or "default_voice.pt")
        reference_audio_raw = str(item.get("reference_audio_path") or "").strip()
        if not reference_audio_raw and is_audio_reference_path(tts_voice):
            reference_audio_raw = tts_voice
            tts_voice = "default_voice.pt"
        reference_audio_path = Path(reference_audio_raw) if reference_audio_raw else None
        phrase_words = [
            WordTiming(
                text=normalize_text(str(word.get("text", ""))),
                start=float(word["start"]),
                end=float(word["end"]),
                speaker=str(word.get("speaker", item.get("api_speaker", "UNKNOWN"))),
                confidence=float(word["confidence"]) if word.get("confidence") is not None else None,
            )
            for word in item.get("words", [])
            if isinstance(word, dict) and word.get("text") and word.get("start") is not None and word.get("end") is not None
        ]
        phrase = Phrase(
            id=int(item["id"]),
            start=float(item["start"]),
            end=float(item["end"]),
            text=normalize_text(str(item.get("text", ""))),
            speaker=str(item.get("api_speaker", "UNKNOWN")),
            word_confidence_min=float(item["word_confidence_min"]) if item.get("word_confidence_min") is not None else None,
            word_confidence_avg=float(item["word_confidence_avg"]) if item.get("word_confidence_avg") is not None else None,
            needs_review=parse_bool(item.get("needs_review", False)),
            review_reason=str(item.get("review_reason", "")),
            words=phrase_words,
        )
        phrases.append(
            TranslatedPhrase(
                phrase=phrase,
                translated_text=translated_text,
                skip_tts=skip_tts,
                tts_voice=tts_voice,
                reference_audio_path=reference_audio_path,
                reference_text=normalize_text(str(item.get("reference_text", ""))),
            )
        )
    return phrases


def attach_word_timings(phrases: list[TranslatedPhrase], words: list[WordTiming]) -> list[TranslatedPhrase]:
    if not words:
        return phrases
    attached: list[TranslatedPhrase] = []
    for item in phrases:
        if item.phrase.words:
            attached.append(item)
            continue
        phrase_words = [
            word
            for word in words
            if word.start >= item.phrase.start - 0.05 and word.end <= item.phrase.end + 0.05
        ]
        phrase = Phrase(
            id=item.phrase.id,
            start=item.phrase.start,
            end=item.phrase.end,
            text=item.phrase.text,
            speaker=item.phrase.speaker,
            word_confidence_min=item.phrase.word_confidence_min,
            word_confidence_avg=item.phrase.word_confidence_avg,
            needs_review=item.phrase.needs_review,
            review_reason=item.phrase.review_reason,
            words=phrase_words,
        )
        attached.append(
            TranslatedPhrase(
                phrase=phrase,
                translated_text=item.translated_text,
                skip_tts=item.skip_tts,
                tts_voice=item.tts_voice,
                reference_audio_path=item.reference_audio_path,
                reference_text=item.reference_text,
                raw_audio_path=item.raw_audio_path,
                processed_audio_path=item.processed_audio_path,
            )
        )
    return attached


def load_words_from_assemblyai(path: Path) -> list[WordTiming]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return _parse_assemblyai_words(json.load(f))


def load_speaker_turns(path: Path) -> list[SpeakerTurn]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    return [
        SpeakerTurn(start=float(item["start"]), end=float(item["end"]), speaker=str(item["speaker"]))
        for item in payload
    ]


def find_downloaded_video(downloads_dir: Path) -> Path:
    candidates = [path for path in downloads_dir.glob("source.*") if path.is_file()]
    if not candidates:
        raise FileNotFoundError(f"No downloaded source media found in {downloads_dir}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def import_local_input(src: Path, downloads_dir: Path) -> Path:
    # Stage local audio/video file into downloads/ as source.<ext> so the
    # rest of the pipeline (which assumes one source media file) just works.
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"Input file not found: {src}")
    downloads_dir.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower() or ".mp4"
    dest = downloads_dir / f"source{suffix}"
    if dest.resolve() != src.resolve():
        shutil.copyfile(str(src), str(dest))
    logging.info("Imported local input: %s -> %s", src, dest)
    return dest


def render_audio_only(final_audio_path: Path, output_path: Path) -> None:
    # Audio-only mode: encode mixed final audio as mp3 (default) or m4a.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix.lower()
    if suffix == ".m4a":
        codec, kwargs = "aac", {"audio_bitrate": "192k"}
    else:
        # default to mp3
        codec, kwargs = "libmp3lame", {"audio_bitrate": "192k"}
    stream = ffmpeg.input(str(final_audio_path)).output(
        str(output_path),
        vn=None,
        acodec=codec,
        **kwargs,
    )
    run_ffmpeg(stream)
    logging.info("Final audio saved: %s", output_path)


def find_separator_vocals(separator_dir: Path) -> Path:
    candidates = [
        path for path in separator_dir.glob("*")
        if path.is_file() and "vocal" in path.name.lower()
    ]
    if not candidates:
        raise FileNotFoundError(f"No separator vocals file found in {separator_dir}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def find_separator_background(separator_dir: Path) -> Path:
    candidates = [
        path for path in separator_dir.glob("*")
        if path.is_file() and any(keyword in path.name.lower() for keyword in ("no_vocals", "instrumental", "karaoke"))
    ]
    if not candidates:
        raise FileNotFoundError(f"No separator background file found in {separator_dir}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def build_summary_prompt(phrases: list[Phrase]) -> str:
    transcript = "\n".join(f"[{phrase.start:.2f}-{phrase.end:.2f}] {phrase.text}" for phrase in phrases)
    return (
        "Составь краткий конспект видео на русском языке по транскрипту.\n"
        "Верни отдельный блок summary без JSON и без Markdown-таблиц.\n"
        f"Транскрипт:\n{transcript}"
    )


def create_summary(phrases: list[Phrase], args: argparse.Namespace, summary_path: Path) -> None:
    logging.info("Creating summary")
    prompt = build_summary_prompt(phrases)
    content = call_llm(prompt, args)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(content.strip() + "\n", encoding="utf-8")
    logging.info("Summary saved: %s", summary_path)


def audio_duration(path: Path) -> float:
    info = sf.info(str(path))
    if info.samplerate <= 0:
        raise RuntimeError(f"Invalid sample rate for {path}")
    return info.frames / info.samplerate


def read_audio_mono(path: Path, sample_rate: int) -> np.ndarray:
    audio, _ = librosa.load(str(path), sr=sample_rate, mono=True)
    return np.asarray(audio, dtype=np.float32)


def write_audio(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.asarray(audio, dtype=np.float32).reshape(-1)
    if data.size == 0:
        data = np.zeros(1, dtype=np.float32)
    sf.write(path, np.clip(data, -1.0, 1.0), sample_rate, subtype="PCM_16")


def normalize_audio(input_path: Path, output_path: Path, target_dbfs: float = -20.0) -> Path:
    audio = AudioSegment.from_file(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if audio.dBFS == float("-inf"):
        audio.export(output_path, format="wav")
        return output_path
    normalized = audio.apply_gain(target_dbfs - audio.dBFS)
    normalized.export(output_path, format="wav")
    return output_path


def trim_silence(input_path: Path, output_path: Path, sample_rate: int, top_db: float = 30.0) -> tuple[float, float]:
    # Trims leading/trailing silence and reports how many seconds were cut at each end.
    audio = read_audio_mono(input_path, sample_rate)
    if audio.size == 0:
        write_audio(output_path, audio, sample_rate)
        return 0.0, 0.0
    trimmed, idx = librosa.effects.trim(audio, top_db=top_db)
    if trimmed.size == 0:
        # Fallback: keep at least a tiny slice so downstream tools don't fail.
        trimmed = audio
        idx = (0, audio.size)
    head_cut = idx[0] / sample_rate
    tail_cut = (audio.size - idx[1]) / sample_rate
    write_audio(output_path, trimmed, sample_rate)
    return head_cut, tail_cut


def apply_fade_out(input_path: Path, output_path: Path, sample_rate: int, fade_ms: float = 5.0) -> None:
    # Linear fade-out over the last fade_ms milliseconds to suppress click artifacts at sample edges.
    audio = read_audio_mono(input_path, sample_rate)
    if audio.size == 0:
        write_audio(output_path, audio, sample_rate)
        return
    fade_samples = min(audio.size, max(1, int(round(sample_rate * fade_ms / 1000.0))))
    ramp = np.linspace(1.0, 0.0, fade_samples, dtype=audio.dtype)
    audio = audio.copy()
    audio[-fade_samples:] *= ramp
    write_audio(output_path, audio, sample_rate)


def apply_atempo(input_path: Path, output_path: Path, tempo: float, sample_rate: int) -> None:
    stream = ffmpeg.input(str(input_path)).filter("atempo", tempo).output(
        str(output_path),
        acodec="pcm_s16le",
        ac=1,
        ar=sample_rate,
    )
    run_ffmpeg(stream)


def pad_or_clip(input_path: Path, output_path: Path, duration: float, sample_rate: int, fade_ms: float = 5.0) -> None:
    audio = read_audio_mono(input_path, sample_rate)
    target_samples = max(1, int(round(duration * sample_rate)))
    if audio.size < target_samples:
        audio = np.pad(audio, (0, target_samples - audio.size), mode="constant")
    elif audio.size > target_samples:
        audio = audio[:target_samples]
    fade_samples = min(audio.size, max(1, int(round(sample_rate * fade_ms / 1000.0))))
    if fade_samples > 1:
        ramp = np.linspace(1.0, 0.0, fade_samples, dtype=audio.dtype)
        audio = audio.copy()
        audio[-fade_samples:] *= ramp
    write_audio(output_path, audio, sample_rate)


def tts_runaway_threshold(available_duration: float, max_speedup: float) -> float:
    # Anything longer cannot fit even at max speedup plus a small overlap allowance,
    # which indicates a degenerate TTS generation (e.g. an endless vowel artifact).
    return max(available_duration * max_speedup + 2.0, available_duration * 2.0, 4.0)


def process_phrase_audio(
    raw_path: Path,
    processed_path: Path,
    temp_dir: Path,
    duration: float,
    sample_rate: int,
    max_speedup: float,
    max_overflow: float = 1.0,
) -> None:
    # Always trim leading/trailing silence first to prevent the next phrase from overlaying a dead tail.
    raw_duration = audio_duration(raw_path)
    trimmed_path = temp_dir / f"{raw_path.stem}_trim.wav"
    head_cut, tail_cut = trim_silence(raw_path, trimmed_path, sample_rate)
    current_path = trimmed_path
    current_duration = audio_duration(current_path)
    logging.info(
        "Trimmed %s: %.2fs -> %.2fs (head=%.2fs, tail=%.2fs)",
        raw_path.name,
        raw_duration,
        current_duration,
        head_cut,
        tail_cut,
    )
    if current_duration > duration:
        required_tempo = current_duration / duration
        tempo = min(max_speedup, max(1.0, required_tempo))
        sped_path = temp_dir / f"{raw_path.stem}_atempo.wav"
        apply_atempo(current_path, sped_path, tempo, sample_rate)
        current_path = sped_path
        current_duration = audio_duration(current_path)
        logging.info("Atempo %s: %.3fx, result %.2fs", raw_path.name, tempo, current_duration)
        if current_duration > duration:
            overflow = current_duration - duration
            allowed_overflow = min(overflow, max(0.0, max_overflow))
            logging.info(
                "Overflow %s: required %.3fx but capped at %.3fx; %.2fs over slot, keeping %.2fs overlap",
                raw_path.name,
                required_tempo,
                tempo,
                overflow,
                allowed_overflow,
            )
            if overflow > allowed_overflow:
                logging.warning(
                    "Clipping %s: %.2fs exceeds slot by %.2fs (max overlap %.2fs); hard-clipping tail",
                    raw_path.name,
                    current_duration,
                    overflow,
                    allowed_overflow,
                )
            # pad_or_clip applies a tail fade-out, suppressing clicks at the clip point.
            pad_or_clip(current_path, processed_path, duration + allowed_overflow, sample_rate)
            return
    pad_or_clip(current_path, processed_path, duration, sample_rate)


def speaker_intervals(turns: list[SpeakerTurn], speaker: str) -> list[tuple[float, float]]:
    # Returns sorted diarization intervals for one speaker.
    return sorted((turn.start, turn.end) for turn in turns if turn.speaker == speaker and turn.end > turn.start)


def find_reference_bounds(phrase: Phrase, turns: list[SpeakerTurn], min_duration: float) -> tuple[float, float]:
    # Expands phrase bounds inside the same speaker region without crossing into another speaker.
    if phrase.speaker == "UNKNOWN" or not turns:
        return phrase.start, phrase.end
    intervals = speaker_intervals(turns, phrase.speaker)
    best_interval = None
    best_overlap = 0.0
    for start, end in intervals:
        overlap = max(0.0, min(phrase.end, end) - max(phrase.start, start))
        if overlap > best_overlap:
            best_interval = (start, end)
            best_overlap = overlap
    if best_interval is not None:
        start, end = best_interval
        target_start = max(start, phrase.start)
        target_end = min(end, phrase.end)
        deficit = max(0.0, min_duration - (target_end - target_start))
        before = min(deficit / 2, target_start - start)
        target_start -= before
        deficit -= before
        after = min(deficit, end - target_end)
        target_end += after
        deficit -= after
        if deficit > 0:
            target_start -= min(deficit, target_start - start)
        return max(start, target_start), min(end, target_end)
    return phrase.start, phrase.end


def text_for_window(words: list[WordTiming], start: float, end: float, fallback: str) -> str:
    # Joins words whose timing overlaps [start, end]. Falls back to phrase text if empty.
    selected = [w.text for w in words if w.end > start and w.start < end]
    text = normalize_text(" ".join(selected))
    return text or fallback


def words_fully_inside(words: list[WordTiming], start: float, end: float, slack: float = 0.05) -> list[WordTiming]:
    # Only words whose audio is fully contained in [start, end]. Prompt text for
    # voice-cloning TTS must exactly match the reference audio: a word that is cut
    # off in the clip but present in the text gets spoken aloud before the target
    # line (reference text leaking into every generated phrase).
    return [w for w in words if w.start >= start - slack and w.end <= end + slack]


def crop_reference_audio(
    audio_path: Path,
    phrase: Phrase,
    turns: list[SpeakerTurn],
    words: list[WordTiming],
    reference_dir: Path,
    min_duration: float,
    sample_rate: int,
) -> tuple[Path, str]:
    # Creates a speaker-safe reference WAV for OmniVoice and matching prompt text.
    reference_dir.mkdir(parents=True, exist_ok=True)
    ref_start, ref_end = find_reference_bounds(phrase, turns, min_duration)
    contained = words_fully_inside(words, ref_start, ref_end)
    if contained:
        # Snap clip bounds to word edges so the audio matches ref_text exactly.
        ref_start = max(ref_start, contained[0].start - 0.1)
        ref_end = min(ref_end, contained[-1].end + 0.1)
    ref_text = normalize_text(" ".join(w.text for w in contained))
    output_path = reference_dir / f"ref_{phrase.id:05d}_{phrase.speaker}.wav"
    stream = ffmpeg.input(str(audio_path), ss=max(0.0, ref_start), t=max(0.01, ref_end - ref_start)).output(
        str(output_path),
        acodec="pcm_s16le",
        ac=1,
        ar=sample_rate,
    )
    run_ffmpeg(stream)
    logging.info(
        "Reference %s: speaker=%s %.2fs-%.2fs (%.2fs), text=%r",
        output_path.name,
        phrase.speaker,
        ref_start,
        ref_end,
        ref_end - ref_start,
        ref_text[:80],
    )
    return output_path, ref_text


def _save_tts_response(content: bytes, output_path: Path, sample_rate: int) -> None:
    # Decodes a returned WAV/MP3/binary stream and writes a clean mono PCM_16 WAV.
    if not content:
        raise RuntimeError("TTS backend returned an empty response")
    try:
        audio, sr = sf.read(io.BytesIO(content), dtype="float32", always_2d=False)
    except Exception:
        # Fallback: pydub (handles mp3, m4a, etc.)
        try:
            from pydub import AudioSegment
            seg = AudioSegment.from_file(io.BytesIO(content))
            seg.export(output_path, format="wav")
            return
        except Exception as exc2:
            preview = content[:200]
            raise RuntimeError(f"TTS backend returned non-audio response: {preview!r}") from exc2
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)
    sf.write(output_path, audio, sr or sample_rate, subtype="PCM_16")


def generate_fish_audio(
    text: str,
    ref_audio_path: Path,
    ref_text: str,
    output_path: Path,
    args: argparse.Namespace,
) -> None:
    # Calls local Fish Speech /v1/tts with base64-encoded reference audio + reference text.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not text:
        sf.write(output_path, np.zeros(1, dtype=np.float32), args.dub_sample_rate, subtype="PCM_16")
        return
    audio_b64 = base64.b64encode(ref_audio_path.read_bytes()).decode("utf-8")
    payload = {
        "text": text,
        "format": "wav",
        "references": [{"audio": audio_b64, "text": ref_text or ""}],
        "latency": "normal",
    }
    try:
        response = requests.post(args.fish_url, json=payload, timeout=args.omnivoice_timeout)
        response.raise_for_status()
    except requests.Timeout as exc:
        raise RuntimeError(f"Fish Speech request timed out after {args.omnivoice_timeout}s") from exc
    except requests.RequestException as exc:
        status = exc.response.status_code if exc.response is not None else "no response"
        body = exc.response.text[:500] if exc.response is not None else str(exc)
        raise RuntimeError(f"Fish Speech request failed ({status}): {body}") from exc
    content_type = response.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = response.text[:500]
        raise RuntimeError(f"Fish Speech returned JSON instead of audio: {error_payload}")
    _save_tts_response(response.content, output_path, args.dub_sample_rate)


def generate_tts_audio(
    text: str,
    ref_audio_path: Path,
    ref_text: str,
    output_path: Path,
    args: argparse.Namespace,
) -> None:
    """Диспетчер TTS-бекендов с автоматическим per-segment фоллбэком.

    Иерархия фоллбэков (в порядке убывания приоритета по `--tts-backend`):
      voxcpm-demo -> omnivoice (local) -> fish
      omnivoice   -> voxcpm-demo -> fish
      fish        -> voxcpm-demo -> omnivoice

    Если выбранный бэкенд упал на этом сегменте — пробуем следующий,
    чтобы один сетевой сбой/таймаут не валил весь дубляж.
    """
    primary = args.tts_backend
    chain_map = {
        "voxcpm-demo": ["voxcpm-demo", "omnivoice", "fish"],
        "omnivoice":   ["omnivoice", "voxcpm-demo", "fish"],
        "fish":        ["fish", "voxcpm-demo", "omnivoice"],
    }
    chain = chain_map.get(primary, [primary])

    backend_funcs = {
        "voxcpm-demo": generate_voxcpm_demo_audio,
        "omnivoice":   generate_omnivoice_audio,
        "fish":        generate_fish_audio,
    }

    last_error: Exception | None = None
    for backend in chain:
        fn = backend_funcs.get(backend)
        if fn is None:
            continue
        try:
            fn(text, ref_audio_path, ref_text, output_path, args)
            if backend != primary:
                logging.warning(
                    "TTS segment fell back to '%s' (primary '%s' failed)",
                    backend, primary,
                )
            return
        except Exception as exc:
            last_error = exc
            logging.warning(
                "TTS backend '%s' failed for segment: %s", backend, str(exc)[:200],
            )
            continue

    raise RuntimeError(
        f"All TTS backends failed for segment ({chain}): {last_error}"
    ) from last_error


# --- Persistent gradio_client for VoxCPM Demo Space (videotrans-side) ---
_VTRANS_DEMO_CLIENT = None


def _vtrans_get_demo_client(args: argparse.Namespace, force_reconnect: bool = False):
    global _VTRANS_DEMO_CLIENT
    if _VTRANS_DEMO_CLIENT is not None and not force_reconnect:
        return _VTRANS_DEMO_CLIENT
    try:
        from gradio_client import Client
    except ImportError as exc:
        raise RuntimeError(
            "gradio_client is required for --tts-backend voxcpm-demo. "
            "Install with: pip install gradio_client"
        ) from exc
    if force_reconnect:
        logging.warning("VoxCPM Demo: re-connecting to %s ...", args.voxcpm_demo_space)
    else:
        logging.info("VoxCPM Demo: connecting to %s ...", args.voxcpm_demo_space)
    _VTRANS_DEMO_CLIENT = Client(args.voxcpm_demo_space)
    logging.info("VoxCPM Demo: client ready")
    return _VTRANS_DEMO_CLIENT


def _strip_voxcpm_direction(text: str) -> tuple[str, str]:
    # text может быть подан как "(direction) body" — для Demo Space надо
    # отделить direction в свой параметр.
    m = re.match(r"\s*\(([^)]+)\)\s*(.*)", text, flags=re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", text.strip()


def generate_voxcpm_demo_audio(
    text: str,
    ref_audio_path: Path,
    ref_text: str,
    output_path: Path,
    args: argparse.Namespace,
) -> None:
    """VoxCPM Demo Space (HF) через gradio_client.

    Демо-Space периодически даёт SSL handshake timeout / network errors —
    делаем 3 попытки с экспоненциальным бэкоффом и переподключением
    клиента. При финальном провале перебрасываем исключение, чтобы
    верхний уровень мог переключиться на следующий бэкенд.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not text:
        sf.write(output_path, np.zeros(1, dtype=np.float32), args.dub_sample_rate, subtype="PCM_16")
        return

    direction, body = _strip_voxcpm_direction(text)

    try:
        from gradio_client import handle_file
    except ImportError as exc:
        raise RuntimeError("gradio_client.handle_file unavailable") from exc

    last_error: Exception | None = None
    result_path = None
    max_attempts = 15
    for attempt in range(1, max_attempts + 1):
        try:
            client = _vtrans_get_demo_client(args, force_reconnect=(attempt > 1))
            result_path = client.predict(
                text_input=body or text,
                control_instruction=direction,
                reference_wav_path_input=handle_file(str(ref_audio_path.resolve())),
                use_prompt_text=bool(ref_text),
                prompt_text_input=ref_text or "",
                cfg_value_input=float(args.voxcpm_demo_cfg),
                do_normalize=bool(args.voxcpm_demo_normalize),
                denoise=bool(args.voxcpm_demo_denoise),
                api_name="/generate",
            )
            if result_path:
                break
            last_error = RuntimeError("empty result")
        except Exception as exc:
            last_error = exc
            logging.warning(
                "VoxCPM Demo attempt %d/%d failed: %s",
                attempt, max_attempts, str(exc)[:200],
            )
        # Сбрасываем клиент, чтобы при следующей попытке создался новый
        global _VTRANS_DEMO_CLIENT
        _VTRANS_DEMO_CLIENT = None
        if attempt < max_attempts:
            backoff = 2.0 * attempt
            time.sleep(backoff)

    if not result_path:
        raise RuntimeError(
            f"VoxCPM Demo Space failed after {max_attempts} attempts: {last_error}"
        ) from last_error

    src = Path(result_path)
    if not src.exists():
        raise RuntimeError(f"VoxCPM Demo Space file not found: {result_path}")

    with open(src, "rb") as f:
        content = f.read()
    _save_tts_response(content, output_path, args.dub_sample_rate)


def generate_omnivoice_audio(text: str, ref_audio_path: Path, ref_text: str, output_path: Path, args: argparse.Namespace) -> None:
    # Calls local VoxCPM2 server (FastAPI) with multipart/form-data: text=Form, ref_audio=File.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not text:
        sf.write(output_path, np.zeros(1, dtype=np.float32), args.dub_sample_rate, subtype="PCM_16")
        return
    try:
        with open(ref_audio_path, "rb") as ref_file:
            response = requests.post(
                args.omnivoice_url,
                data={"text": text},
                files={"ref_audio": (ref_audio_path.name, ref_file, "audio/wav")},
                headers={"Accept": "audio/wav"},
                timeout=args.omnivoice_timeout,
            )
        response.raise_for_status()
    except requests.Timeout as exc:
        raise RuntimeError(f"OmniVoice request timed out after {args.omnivoice_timeout}s") from exc
    except requests.RequestException as exc:
        status = exc.response.status_code if exc.response is not None else "no response"
        body = exc.response.text[:500] if exc.response is not None else str(exc)
        raise RuntimeError(f"OmniVoice request failed ({status}): {body}") from exc
    content_type = response.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = response.text[:500]
        raise RuntimeError(f"OmniVoice returned JSON instead of audio: {error_payload}")
    _save_tts_response(response.content, output_path, args.dub_sample_rate)


def build_speaker_references(
    speaker_turns: list[SpeakerTurn],
    words: list[WordTiming],
    source_audio_path: Path,
    reference_dir: Path,
    args: argparse.Namespace,
) -> dict[str, tuple[Path, str]]:
    # One cached reference per speaker keeps the cloned voice timbre stable across
    # phrases (per-phrase crops give the TTS a different prompt every time).
    references: dict[str, tuple[Path, str]] = {}
    if not speaker_turns:
        return references
    reference_dir.mkdir(parents=True, exist_ok=True)
    min_dur = max(2.0, args.reference_min_seconds)
    max_dur = 8.0
    for speaker in sorted({turn.speaker for turn in speaker_turns}):
        best_turn: SpeakerTurn | None = None
        best_score = -1.0
        for turn in speaker_turns:
            if turn.speaker != speaker:
                continue
            duration = turn.end - turn.start
            if duration < min_dur:
                continue
            turn_words = [w for w in words if w.start >= turn.start - 0.05 and w.end <= turn.end + 0.05]
            if not turn_words:
                continue
            confidences = [w.confidence for w in turn_words if w.confidence is not None]
            avg_conf = sum(confidences) / len(confidences) if confidences else 0.5
            score = min(duration, max_dur) + 3.0 * avg_conf
            if score > best_score:
                best_score = score
                best_turn = turn
        if best_turn is None:
            continue
        ref_start = best_turn.start
        ref_end = min(best_turn.end, best_turn.start + max_dur)
        contained = words_fully_inside(words, ref_start, ref_end)
        if not contained:
            continue
        ref_start = max(ref_start, contained[0].start - 0.1)
        ref_end = min(ref_end, contained[-1].end + 0.1)
        if ref_end - ref_start < min_dur * 0.5:
            continue
        ref_text = normalize_text(" ".join(w.text for w in contained))
        output_path = reference_dir / f"speaker_ref_{speaker}.wav"
        stream = ffmpeg.input(str(source_audio_path), ss=max(0.0, ref_start), t=max(0.01, ref_end - ref_start)).output(
            str(output_path),
            acodec="pcm_s16le",
            ac=1,
            ar=args.dub_sample_rate,
        )
        run_ffmpeg(stream)
        normalized = normalize_audio(output_path, reference_dir / f"speaker_ref_{speaker}_norm.wav")
        references[speaker] = (normalized, ref_text)
        logging.info(
            "Cached speaker reference %s: %.2fs-%.2fs (%.2fs), text=%r",
            speaker, ref_start, ref_end, ref_end - ref_start, ref_text[:80],
        )
    return references


def resolve_tts_reference(
    translated: TranslatedPhrase,
    source_audio_path: Path,
    speaker_turns: list[SpeakerTurn],
    words: list[WordTiming],
    reference_dir: Path,
    args: argparse.Namespace,
    speaker_references: dict[str, tuple[Path, str]] | None = None,
) -> tuple[Path, str]:
    if translated.reference_audio_path is not None:
        reference_path = validate_reference_audio_path(translated.reference_audio_path, translated.phrase.id)
        reference_text = translated.reference_text or translated.phrase.text
        normalized_path = reference_dir / f"manual_ref_{translated.phrase.id:05d}_norm.wav"
        return normalize_audio(reference_path, normalized_path), reference_text
    voice = translated.tts_voice.strip()
    if voice and voice != "default_voice.pt":
        logging.info("TTS voice label for segment %d: %s", translated.phrase.id, voice)
    if speaker_references and translated.phrase.speaker in speaker_references:
        return speaker_references[translated.phrase.speaker]
    ref_path, ref_text = crop_reference_audio(
        audio_path=source_audio_path,
        phrase=translated.phrase,
        turns=speaker_turns,
        words=words,
        reference_dir=reference_dir,
        min_duration=args.reference_min_seconds,
        sample_rate=args.dub_sample_rate,
    )
    return normalize_audio(ref_path, reference_dir / f"{ref_path.stem}_norm.wav"), ref_text


def synthesize_and_sync(
    phrases: list[TranslatedPhrase],
    args: argparse.Namespace,
    raw_dir: Path,
    processed_dir: Path,
    audio_temp_dir: Path,
    reference_dir: Path,
    source_audio_path: Path,
    speaker_turns: list[SpeakerTurn],
    words: list[WordTiming],
) -> list[TranslatedPhrase]:
    safety_margin = 0.05
    trailing_gap = 2.0
    synced: list[TranslatedPhrase] = []
    speaker_references: dict[str, tuple[Path, str]] = {}
    if not args.per_phrase_reference:
        speaker_references = build_speaker_references(
            speaker_turns, words, source_audio_path, reference_dir, args,
        )
    for index, translated in enumerate(phrases, start=1):
        phrase = translated.phrase
        if translated.skip_tts:
            logging.info(
                "TTS[%s] %d/%d: skipping segment %d marked skip_tts",
                args.tts_backend, index, len(phrases), phrase.id,
            )
            synced.append(
                TranslatedPhrase(
                    phrase=phrase,
                    translated_text=translated.translated_text,
                    skip_tts=True,
                    tts_voice=translated.tts_voice,
                    reference_audio_path=translated.reference_audio_path,
                    reference_text=translated.reference_text,
                )
            )
            continue
        raw_path = raw_dir / f"phrase_{phrase.id:05d}.wav"
        processed_path = processed_dir / f"phrase_{phrase.id:05d}.wav"
        if index < len(phrases):
            next_start = phrases[index].phrase.start
            gap = max(0.0, next_start - phrase.end)
            available_duration = max(phrase.duration, phrase.duration + gap - safety_margin)
        else:
            available_duration = phrase.duration + trailing_gap
        logging.info(
            "TTS[%s] %d/%d: segment %d, speaker=%s, slot %.2fs, available %.2fs",
            args.tts_backend, index, len(phrases), phrase.id, phrase.speaker, phrase.duration, available_duration,
        )
        ref_path, ref_text = resolve_tts_reference(
            translated,
            source_audio_path,
            speaker_turns,
            words,
            reference_dir,
            args,
            speaker_references=speaker_references,
        )
        runaway_limit = tts_runaway_threshold(available_duration, args.max_speedup)
        current_text = translated.translated_text
        shorten_attempts_left = max(0, args.tts_shorten_retries)
        max_generation_attempts = max(1, args.tts_runaway_retries + 1) + shorten_attempts_left
        for generation_attempt in range(1, max_generation_attempts + 1):
            generate_tts_audio(
                text=current_text,
                ref_audio_path=ref_path,
                ref_text=ref_text,
                output_path=raw_path,
                args=args,
            )
            raw_duration = audio_duration(raw_path)
            if raw_duration > runaway_limit:
                logging.warning(
                    "Runaway TTS for segment %d: %.2fs for %.2fs slot (limit %.2fs), attempt %d/%d",
                    phrase.id, raw_duration, available_duration, runaway_limit,
                    generation_attempt, max_generation_attempts,
                )
                continue
            if (
                raw_duration > available_duration * args.tts_shorten_trigger
                and shorten_attempts_left > 0
                and generation_attempt < max_generation_attempts
            ):
                shorten_attempts_left -= 1
                target_chars = max(8, int(len(current_text) * available_duration / raw_duration * 0.95))
                shortened = shorten_line_with_llm(current_text, target_chars, args)
                if shortened != current_text:
                    logging.info(
                        "Shortening segment %d: %.2fs > %.2fs slot; %d -> %d chars, regenerating",
                        phrase.id, raw_duration, available_duration, len(current_text), len(shortened),
                    )
                    current_text = shortened
                    continue
            break
        process_phrase_audio(
            raw_path=raw_path,
            processed_path=processed_path,
            temp_dir=audio_temp_dir,
            duration=available_duration,
            sample_rate=args.dub_sample_rate,
            max_speedup=args.max_speedup,
            max_overflow=args.max_overflow,
        )
        synced.append(
            TranslatedPhrase(
                phrase=phrase,
                translated_text=current_text,
                skip_tts=translated.skip_tts,
                tts_voice=translated.tts_voice,
                reference_audio_path=translated.reference_audio_path,
                reference_text=translated.reference_text,
                raw_audio_path=raw_path,
                processed_audio_path=processed_path,
            )
        )
    return synced


def probe_video_duration(video_path: Path) -> float:
    probe = ffmpeg.probe(str(video_path))
    duration = probe.get("format", {}).get("duration")
    if duration is None:
        raise RuntimeError("Could not read video duration with ffprobe")
    return float(duration)


def mix_final_audio(phrases: list[TranslatedPhrase], output_path: Path, total_video_duration: float) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # The canvas is clamped to the video duration; overlay drops any tail past the end,
    # so phrase overflow can never grow the final track (and thus the video).
    loaded: list[tuple[int, AudioSegment]] = []
    for translated in phrases:
        if translated.skip_tts:
            continue
        if translated.processed_audio_path is None:
            raise RuntimeError(f"Missing processed audio for segment {translated.phrase.id}")
        phrase_audio = AudioSegment.from_file(translated.processed_audio_path)
        start_ms = max(0, int(round(translated.phrase.start * 1000)))
        loaded.append((start_ms, phrase_audio))
    total_duration_ms = max(1, int(math.ceil(total_video_duration * 1000)))
    canvas = AudioSegment.silent(duration=total_duration_ms)
    for start_ms, phrase_audio in loaded:
        canvas = canvas.overlay(phrase_audio, position=start_ms)
    canvas.export(output_path, format="wav")
    logging.info("Russian dub only track saved: %s (%.2fs)", output_path, total_duration_ms / 1000.0)


def mix_dub_with_background(
    dub_track_path: Path,
    original_audio_path: Path,
    output_path: Path,
    original_volume_db: float,
    mix_mode: str = "voiceover",
    separated_background_path: Path | None = None,
) -> None:
    # Voiceover strategy: duck the full original audio, then overlay the Russian dub on top.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dub = AudioSegment.from_file(dub_track_path)
    if mix_mode == "dub":
        if separated_background_path is None:
            raise RuntimeError("mix_mode=dub requires separated no_vocals background")
        logging.info("Dub mix: separated background + Russian dub overlay: %s", separated_background_path)
        background = AudioSegment.from_file(separated_background_path)
    else:
        logging.info(
            "Voiceover mix: original ducked by %.1f dB + Russian dub overlay",
            original_volume_db,
        )
        original = AudioSegment.from_file(original_audio_path)
        background = original.apply_gain(original_volume_db)
    if len(background) < len(dub):
        background = background + AudioSegment.silent(duration=len(dub) - len(background))
    mixed = background.overlay(dub)
    mixed.export(output_path, format="wav")
    logging.info("Final video audio saved: %s", output_path)


def ass_time(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))


def escape_ass_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def split_subtitle_words(text: str) -> list[str]:
    return re.findall(r"\S+", normalize_text(text))


def karaoke_text_for_phrase(phrase: Phrase, text: str) -> str:
    words = split_subtitle_words(text)
    if not words:
        return ""
    source_words = [word for word in phrase.words if word.end > word.start]
    if source_words and len(source_words) == len(words):
        durations = [max(1, int(round((word.end - word.start) * 100))) for word in source_words]
    else:
        total_cs = max(len(words), int(round(phrase.duration * 100)))
        base = max(1, total_cs // len(words))
        durations = [base for _ in words]
        durations[-1] += max(0, total_cs - sum(durations))
    return " ".join(f"{{\\K{duration}}}{escape_ass_text(word)}" for duration, word in zip(durations, words))


def generate_animated_ass(dub_plan_json: Path | str | list[TranslatedPhrase], output_ass_path: Path, args: argparse.Namespace | None = None) -> None:
    try:
        import pysubs2
    except ImportError as exc:
        raise RuntimeError("pysubs2 is required for animated hardsubs. Install it with: pip install pysubs2") from exc

    phrases = load_dub_plan(Path(dub_plan_json)) if isinstance(dub_plan_json, (str, Path)) else dub_plan_json
    font_name = getattr(args, "subtitle_font", "Montserrat SemiBold") if args is not None else "Montserrat SemiBold"
    font_size = getattr(args, "subtitle_font_size", 44) if args is not None else 44
    output_ass_path.parent.mkdir(parents=True, exist_ok=True)
    subs = pysubs2.SSAFile()
    subs.info["ScriptType"] = "v4.00+"
    subs.info["ScaledBorderAndShadow"] = "yes"
    subs.info["PlayResX"] = "1080"
    subs.info["PlayResY"] = "1920"
    subs.styles["TikTokKaraoke"] = pysubs2.SSAStyle(
        fontname=font_name,
        fontsize=font_size,
        primarycolor=pysubs2.Color(255, 214, 10, 0),
        secondarycolor=pysubs2.Color(255, 255, 255, 0),
        outlinecolor=pysubs2.Color(0, 0, 0, 0),
        backcolor=pysubs2.Color(0, 0, 0, 120),
        bold=True,
        alignment=2,
        marginl=70,
        marginr=70,
        marginv=115,
        outline=3,
        shadow=2,
    )
    for item in phrases:
        if item.skip_tts:
            continue
        subtitle_text = item.translated_text or item.phrase.text
        karaoke_text = karaoke_text_for_phrase(item.phrase, subtitle_text)
        if not karaoke_text:
            continue
        subs.events.append(
            pysubs2.SSAEvent(
                start=ass_time(item.phrase.start),
                end=ass_time(item.phrase.end),
                style="TikTokKaraoke",
                text=karaoke_text,
            )
        )
    subs.save(str(output_ass_path))
    logging.info("Animated ASS subtitles saved: %s", output_ass_path)


def render_final(video_path: Path, final_audio_path: Path, output_path: Path, subtitles_path: Path | None = None) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logging.info("Rendering final video")
    if subtitles_path is None:
        source = ffmpeg.input(str(video_path))
        final_audio = ffmpeg.input(str(final_audio_path))
        stream = ffmpeg.output(
            source.video,
            final_audio.audio,
            str(output_path),
            vcodec="copy",
            acodec="aac",
            audio_bitrate="192k",
        )
        run_ffmpeg(stream)
    else:
        # ffmpeg subtitles/ass filter on Windows breaks on absolute paths
        # ("C:" is treated as filtergraph separator, libass can't open file).
        # Reliable workaround: copy .ass next to output and run ffmpeg from
        # that directory with a relative filename in the filter.
        import subprocess
        work_dir = output_path.parent.resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        local_subs = work_dir / "_hardsubs.ass"
        try:
            if Path(subtitles_path).resolve() != local_subs.resolve():
                shutil.copyfile(str(subtitles_path), str(local_subs))
        except Exception as exc:
            raise RuntimeError(f"Failed to stage subtitles for ffmpeg: {exc}") from exc
        cmd = [
            "ffmpeg", "-y",
            "-i", str(Path(video_path).resolve()),
            "-i", str(Path(final_audio_path).resolve()),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-vf", f"ass={local_subs.name}",
            "-c:v", "libx264", "-crf", "18", "-preset", "medium",
            "-c:a", "aac", "-b:a", "192k",
            str(Path(output_path).resolve()),
        ]
        try:
            subprocess.run(
                cmd,
                cwd=str(work_dir),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            raise RuntimeError(stderr) from exc
        finally:
            try:
                local_subs.unlink()
            except Exception:
                pass
    logging.info("Final video saved: %s", output_path)


def run_pipeline(args: argparse.Namespace) -> None:
    if args.stage == "polish":
        dub_plan_path = Path(args.dub_plan)
        translated = load_dub_plan(dub_plan_path)
        polished = polish_dub_plan(translated, args)
        save_dub_plan(dub_plan_path, polished)
        logging.info("Dub plan polished and saved: %s", dub_plan_path)
        return

    if args.stage in {"full", "prepare"} and not args.url and not args.input_file:
        raise RuntimeError("Either a URL or --input-file is required for --stage full and --stage prepare")
    if args.stage in {"full", "prepare"}:
        ensure_cuda()
    logging.info("LLM model: %s (fallback: %s)", args.gemini_model, args.gemini_fallback_model)

    work_dir = Path(args.work_dir)
    downloads_dir = work_dir / "downloads"
    audio_dir = work_dir / "audio"
    raw_tts_dir = work_dir / "tts_raw"
    processed_tts_dir = work_dir / "tts_processed"
    audio_temp_dir = work_dir / "audio_temp"
    reference_dir = work_dir / "references"
    output_path = Path(args.output)
    subtitles_path = Path(args.subtitles_output) if args.subtitles_output else output_path.with_suffix(".ass")
    summary_path = Path(args.summary_output)
    transcript_path = output_path.parent / "transcript_original.json"
    translation_path = output_path.parent / "transcript_ru.json"
    assemblyai_raw_path = output_path.parent / "assemblyai_raw.json"
    diarization_path = output_path.parent / "diarization_turns.json"
    dub_plan_path = Path(args.dub_plan)
    dub_track_path = work_dir / "russian_dub_only.wav"
    final_audio_path = work_dir / "final_video_audio.wav"

    if args.stage != "tts":
        if args.input_file:
            video_path = import_local_input(Path(args.input_file), downloads_dir)
        else:
            video_path = download_video(args.url, downloads_dir, cookies=args.cookies, cookies_from_browser=args.cookies_from_browser)
        source_audio_path = audio_dir / "source_separator.wav"
        extract_audio(video_path, source_audio_path, sample_rate=args.dub_sample_rate, channels=2)
        vocals_path, no_vocals_path = separate_audio_roformer(source_audio_path, audio_dir / "clean_vocals", args)

        words, utterances = transcribe_audio(vocals_path, assemblyai_raw_path, args)
        speaker_turns = []
        if not args.disable_diarization:
            try:
                speaker_turns = diarize_speakers(vocals_path, args)
            except Exception as exc:
                logging.warning("Diarization failed; falling back to AssemblyAI speaker labels: %s", exc)
        phrases = merge_words_into_phrases(
            words,
            turns=speaker_turns,
            max_pause=args.max_pause,
            max_phrase_length=args.max_phrase_length,
            min_phrase_duration=0.0,
            word_min_overlap=args.word_min_overlap,
            low_confidence_threshold=args.asr_low_confidence_threshold,
            critical_confidence_threshold=args.asr_critical_confidence_threshold,
            min_tts_duration=args.min_tts_duration,
        )
        if not args.no_snap_boundaries:
            try:
                phrases = snap_phrases_to_silence(phrases, vocals_path, window=args.snap_window)
            except Exception as exc:
                logging.warning("Boundary snapping failed; keeping ASR timings: %s", exc)
        save_json(transcript_path, [phrase.to_json() for phrase in phrases])
        logging.info("Original transcript saved: %s", transcript_path)
        save_json(diarization_path, [turn.to_json() for turn in speaker_turns])
        logging.info("Diarization turns saved: %s", diarization_path)

        translated = translate_phrases(phrases, args)
        save_dub_plan(dub_plan_path, translated)
        save_json(translation_path, [phrase.to_json() for phrase in translated])
        logging.info("Dub plan saved: %s", dub_plan_path)
        logging.info("Russian transcript saved: %s", translation_path)
        if args.stage == "prepare":
            logging.info("Transcription complete. Please review %s; set 'tts_voice' labels and optional 'reference_audio_path', then run with --stage tts.", dub_plan_path)
            return
        if not args.no_review_pause:
            input(f"Transcription complete. Please review {dub_plan_path}; set 'tts_voice' labels and optional 'reference_audio_path', then press Enter to continue to TTS generation.")
    else:
        video_path = find_downloaded_video(downloads_dir)
        source_audio_path = audio_dir / "source_separator.wav"
        vocals_path = find_separator_vocals(audio_dir / "clean_vocals")
        no_vocals_path = find_separator_background(audio_dir / "clean_vocals") if args.mix_mode == "dub" else None
        words = load_words_from_assemblyai(assemblyai_raw_path)
        speaker_turns = load_speaker_turns(diarization_path)

    translated = attach_word_timings(load_dub_plan(dub_plan_path), words)
    synced = synthesize_and_sync(
        translated,
        args,
        raw_tts_dir,
        processed_tts_dir,
        audio_temp_dir,
        reference_dir,
        vocals_path,
        speaker_turns,
        words,
    )
    save_json(translation_path, [phrase.to_json() for phrase in synced])

    video_duration = probe_video_duration(video_path)
    mix_final_audio(synced, dub_track_path, video_duration)
    original_audio_path = audio_dir / "source_separator.wav"
    mix_dub_with_background(
        dub_track_path,
        original_audio_path,
        final_audio_path,
        args.original_volume_db,
        mix_mode=args.mix_mode,
        separated_background_path=no_vocals_path,
    )
    if args.audio_only:
        # Subtitles never make sense for audio-only output
        render_audio_only(final_audio_path, output_path)
    elif args.no_hardsubs:
        render_final(video_path, final_audio_path, output_path)
    else:
        generate_animated_ass(synced, subtitles_path, args)
        render_final(video_path, final_audio_path, output_path, subtitles_path=subtitles_path)

    if not args.keep_temp:
        shutil.rmtree(work_dir, ignore_errors=True)
        logging.info("Temporary files removed: %s", work_dir)


def main() -> int:
    setup_logging()
    load_dotenv()
    args = parse_args()
    try:
        run_pipeline(args)
        return 0
    except KeyboardInterrupt:
        logging.error("Interrupted")
        return 130
    except Exception as exc:
        logging.exception("Pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
