"""Automatic, conservative VoxCPM2 references, independent of ASR word timing.

Speaker labels propose intervals; acoustic checks and speaker embeddings decide
whether they are usable. Ambiguous speakers are reported instead of silently
cropping the current phrase. This is a per-input bank, not character naming.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import soundfile as sf


SAMPLE_RATE = 16000


@dataclass(frozen=True)
class BankConfig:
    min_seconds: float = 5.0
    max_seconds: float = 12.0
    guard_seconds: float = 0.25
    similarity: float = 0.78
    speaker_margin: float = 0.08
    max_candidates: int = 12
    embedding_model: str = "pyannote/wespeaker-voxceleb-resnet34-LM"

    def __post_init__(self):
        values = (self.min_seconds, self.max_seconds, self.guard_seconds,
                  self.similarity, self.speaker_margin)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("non_finite_reference_bank_config")
        if not 2 <= self.min_seconds <= self.max_seconds <= 30:
            raise ValueError("invalid_reference_duration")
        if not 0 <= self.guard_seconds <= 1 or not 0 < self.similarity < 1:
            raise ValueError("invalid_reference_threshold")
        if not 0 <= self.speaker_margin < 1 or not 1 <= self.max_candidates <= 64:
            raise ValueError("invalid_reference_bank_budget")


@dataclass
class Candidate:
    speaker: str
    source: str
    start: float
    end: float
    audio: np.ndarray = field(repr=False)
    score: float
    embedding: np.ndarray | None = field(default=None, repr=False)
    checks: list[np.ndarray] = field(default_factory=list, repr=False)
    reason: str = ""


def _unit(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if not vector.size or not np.isfinite(vector).all() or norm < 1e-8:
        raise ValueError("invalid_speaker_embedding")
    return vector / norm


def safe_intervals(turns: Iterable, guard: float = 0.25) -> dict[str, list[tuple[float, float]]]:
    """Union own turns, subtract other speakers plus drift margins, erode edges."""
    valid = []
    for turn in turns:
        speaker = str(turn.speaker)
        if (speaker not in {"UNKNOWN", "None", ""}
                and math.isfinite(turn.start) and math.isfinite(turn.end)
                and 0 <= turn.start < turn.end):
            valid.append(turn)
    result = {}
    for speaker in sorted({t.speaker for t in valid}):
        own = sorted((t.start, t.end) for t in valid if t.speaker == speaker)
        merged: list[list[float]] = []
        for start, end in own:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        spans = [(start + guard, end - guard) for start, end in merged if end - start > 2 * guard]
        for other in valid:
            if other.speaker == speaker:
                continue
            lo, hi = max(0, other.start - guard), other.end + guard
            remaining = []
            for start, end in spans:
                if hi <= start or lo >= end:
                    remaining.append((start, end))
                else:
                    if start < lo:
                        remaining.append((start, min(lo, end)))
                    if hi < end:
                        remaining.append((max(hi, start), end))
            spans = remaining
        result[str(speaker)] = sorted(spans)
    return result


def _read_window(path: Path, start: float, end: float) -> np.ndarray:
    with sf.SoundFile(path) as stream:
        first = min(stream.frames, max(0, round(start * stream.samplerate)))
        last = min(stream.frames, round(end * stream.samplerate))
        stream.seek(first)
        audio = stream.read(max(0, last - first), dtype="float32", always_2d=True).mean(axis=1)
        rate = stream.samplerate
    if rate != SAMPLE_RATE and audio.size:
        import librosa
        audio = librosa.resample(audio, orig_sr=rate, target_sr=SAMPLE_RATE)
    return np.asarray(audio, dtype=np.float32)


def acoustic_score(audio: np.ndarray) -> tuple[float, str]:
    if audio.size < 2 * SAMPLE_RATE or not np.isfinite(audio).all():
        return 0, "too_short_or_invalid_audio"
    if np.mean(np.abs(audio) >= 0.999) > 0.001:
        return 0, "clipped_audio"
    frame = SAMPLE_RATE // 50
    frames = audio[:audio.size // frame * frame].reshape(-1, frame)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    high = float(np.percentile(rms, 90))
    if high < 0.003:
        return 0, "too_quiet"
    active = float(np.mean(rms > max(0.002, high * 0.12)))
    if active < 0.55:
        return 0, "not_enough_active_audio"
    # Energy contrast is only a ranking cue, not proof of speech or purity.
    contrast = min(3, high / max(float(np.percentile(rms, 20)), 0.001))
    return active + 0.15 * contrast, ""


def _collect(turns: list, sources: dict[str, Path], config: BankConfig) -> list[Candidate]:
    candidates = []
    window = min(8.0, config.max_seconds)
    for speaker, intervals in safe_intervals(turns, config.guard_seconds).items():
        proposed = []
        for start, end in intervals:
            if end - start < 2:
                continue
            starts = [start]
            if end - start > window:
                starts = list(np.arange(start, end - window, window / 2)) + [end - window]
            for left in starts:
                right = min(end, left + window)
                for label, path in sources.items():
                    audio = _read_window(path, float(left), float(right))
                    score, reason = acoustic_score(audio)
                    proposed.append(Candidate(speaker, label, float(left), float(right), audio, score, reason=reason))
                    # Keep memory bounded while scanning long recordings. Limit
                    # duplicate stems so they cannot crowd out other times.
                    if len(proposed) > config.max_candidates * 4:
                        ranked = sorted(proposed, key=lambda c: (-c.score, c.start, c.source))
                        diverse, duplicates, seen = [], [], set()
                        for item in ranked:
                            key = (round(item.start, 3), round(item.end, 3))
                            if key in seen:
                                duplicates.append(item)
                            else:
                                diverse.append(item)
                                seen.add(key)
                        proposed = (diverse + duplicates)[:config.max_candidates * 2]
        # Spread the budget across different times before considering both stems.
        ranked = sorted(proposed, key=lambda c: (-c.score, c.start, c.source))
        chosen = []
        seen = set()
        for candidate in ranked:
            key = (round(candidate.start, 3), round(candidate.end, 3))
            if key not in seen:
                chosen.append(candidate)
                seen.add(key)
        for candidate in ranked:
            if not any(candidate is item for item in chosen):
                chosen.append(candidate)
        candidates.extend(chosen[:config.max_candidates])
    return candidates


def _encoder(model: str) -> Callable[[np.ndarray], np.ndarray]:
    import torch
    from pyannote.audio.pipelines.speaker_verification import PretrainedSpeakerEmbedding

    inference = PretrainedSpeakerEmbedding(model, device=torch.device("cpu"))

    def encode(audio: np.ndarray) -> np.ndarray:
        if inference.sample_rate != SAMPLE_RATE:
            raise RuntimeError("unsupported_reference_embedding_sample_rate")
        waveform = torch.from_numpy(np.ascontiguousarray(audio)).reshape(1, 1, -1)
        return np.asarray(inference(waveform)[0])

    return encode


def _verify(candidate: Candidate, encode: Callable, config: BankConfig) -> None:
    if candidate.reason:
        return
    try:
        width = 2 * SAMPLE_RATE
        starts = list(range(0, max(1, candidate.audio.size - width + 1), SAMPLE_RATE))
        starts.append(max(0, candidate.audio.size - width))
        candidate.checks = [_unit(encode(candidate.audio[s:s + width])) for s in sorted(set(starts))]
        similarities = np.stack(candidate.checks) @ np.stack(candidate.checks).T
        if float(similarities.min()) < config.similarity:
            candidate.reason = "voice_changes_inside_sample"
            return
        candidate.embedding = _unit(np.mean(candidate.checks, axis=0))
    except (ValueError, RuntimeError) as exc:
        candidate.reason = f"embedding_failed:{type(exc).__name__}"


def _consensus(candidates: list[Candidate], config: BankConfig) -> tuple[list[Candidate], np.ndarray | None]:
    usable = [c for c in candidates if c.embedding is not None and not c.reason]
    if not usable:
        return [], None
    matrix = np.stack([c.embedding for c in usable])
    similarities = matrix @ matrix.T
    groups = [np.flatnonzero(row >= config.similarity).tolist() for row in similarities]
    def evidence(indices):
        # Original and separated versions of one span are one piece of evidence.
        return len({(round(usable[i].start, 3), round(usable[i].end, 3)) for i in indices})
    best = max(groups, key=lambda group: (evidence(group), sum(usable[i].score for i in group)))
    if evidence(best) < 0.65 * evidence(list(range(len(usable)))):
        for candidate in usable:
            candidate.reason = "ambiguous_speaker_cluster"
        return [], None
    center = _unit(np.mean(matrix[best], axis=0))
    accepted = []
    for candidate in usable:
        if min(float(v @ center) for v in candidate.checks) >= config.similarity:
            accepted.append(candidate)
        else:
            candidate.reason = "speaker_cluster_outlier"
    return accepted, center if accepted else None


def _assemble(candidates: list[Candidate], config: BankConfig) -> tuple[np.ndarray | None, list[Candidate]]:
    selected = []
    pieces = []
    seconds = 0.0
    for candidate in sorted(candidates, key=lambda c: (-c.score, c.start, c.source)):
        if any(candidate.start < c.end and candidate.end > c.start for c in selected):
            continue
        gaps = 0.05 * len(selected)
        remaining = int((config.max_seconds - seconds - gaps) * SAMPLE_RATE)
        if remaining < 2 * SAMPLE_RATE:
            break
        audio = candidate.audio[:remaining].copy()
        fade = min(80, len(audio) // 2)
        audio[:fade] *= np.linspace(0, 1, fade)
        audio[-fade:] *= np.linspace(1, 0, fade)
        if pieces:
            pieces.append(np.zeros(800, dtype=np.float32))
        pieces.append(audio)
        seconds += len(audio) / SAMPLE_RATE
        selected.append(candidate)
        if seconds >= config.min_seconds:
            break
    if seconds < config.min_seconds:
        return None, selected
    audio = np.concatenate(pieces)
    rms = float(np.sqrt(np.mean(audio ** 2)))
    peak = float(np.max(np.abs(audio)))
    gain = min(0.1 / max(rms, 1e-8), 0.89 / max(peak, 1e-8))
    return audio * gain, selected


def build_bank(turns: list, sources: dict[str, Path], destination: Path,
               config: BankConfig, encode: Callable | None = None) -> dict[str, tuple[Path, str]]:
    """Save verified audio and a rejection report; return transcript-free references."""
    destination.mkdir(parents=True, exist_ok=True)
    report = {"version": 1, "mode": "reference", "sample_rate": SAMPLE_RATE,
              "embedding_model": config.embedding_model, "speakers": {}, "candidates": []}
    candidates = []
    speakers = sorted({str(t.speaker) for t in turns if str(t.speaker) not in {"UNKNOWN", "None", ""}})
    references = {}
    try:
        candidates = _collect(turns, sources, config)
        if any(not candidate.reason for candidate in candidates):
            encoder = encode if encode is not None else _encoder(config.embedding_model)
            for candidate in candidates:
                _verify(candidate, encoder, config)
        groups = {}
        centers = {}
        for speaker in speakers:
            groups[speaker], center = _consensus([c for c in candidates if c.speaker == speaker], config)
            if center is not None:
                centers[speaker] = center
        for speaker in speakers:
            accepted = []
            for candidate in groups[speaker]:
                own = min(float(v @ centers[speaker]) for v in candidate.checks)
                other = max((float(v @ center) for label, center in centers.items()
                             if label != speaker for v in candidate.checks), default=-1.0)
                if own - other < config.speaker_margin:
                    candidate.reason = "ambiguous_between_speakers"
                else:
                    accepted.append(candidate)
            audio, selected = _assemble(accepted, config)
            if audio is None:
                report["speakers"][speaker] = {"status": "unavailable", "reason": "insufficient_verified_audio"}
                continue
            leaf = "speaker_" + hashlib.sha256(speaker.encode()).hexdigest()[:16] + ".wav"
            path = destination / leaf
            sf.write(path, audio, SAMPLE_RATE, subtype="PCM_16")
            references[speaker] = (path, "")
            samples = []
            for index, candidate in enumerate(selected):
                sample_leaf = path.stem + f"_sample_{index + 1}.wav"
                sf.write(destination / sample_leaf, candidate.audio, SAMPLE_RATE, subtype="PCM_16")
                samples.append({"file": sample_leaf, "source": candidate.source,
                                "start": round(candidate.start, 3), "end": round(candidate.end, 3)})
            report["speakers"][speaker] = {
                "status": "ready", "reference": leaf, "seconds": round(len(audio) / SAMPLE_RATE, 3),
                "samples": samples,
            }
        return references
    except Exception as exc:
        report["error"] = type(exc).__name__
        raise RuntimeError("Reference bank verification failed; no unverified voice will be used") from exc
    finally:
        report["candidates"] = [{"speaker": c.speaker, "source": c.source,
                                 "start": round(c.start, 3), "end": round(c.end, 3),
                                 "score": round(c.score, 4), "rejection": c.reason}
                                for c in candidates]
        temporary = destination / "bank.json.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination / "bank.json")
