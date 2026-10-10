"""Auditable STT words, speech detection, speaker reconciliation and local repair.

All times are seconds on the original recording. Provider text is retained even
when timing or speaker checks fail; quality scores are never invented.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import hashlib
import json
import logging
import math
from pathlib import Path
import sys
import unicodedata
import uuid

import numpy as np
import soundfile as sf


@dataclass(frozen=True)
class WordTiming:
    text: str
    start: float
    end: float
    speaker: str = "UNKNOWN"
    confidence: float | None = None
    speaker_confidence: float | None = None
    provider_speaker: str = "UNKNOWN"
    source: str = "original"
    source_id: str = "primary"
    timing_source: str = "assemblyai"
    speaker_method: str = "unchecked"
    original_start: float | None = None
    original_end: float | None = None
    issues: tuple[str, ...] = ()

    def to_json(self):
        payload = dict(self.__dict__)
        for key in ("start", "end", "original_start", "original_end", "confidence", "speaker_confidence"):
            if payload[key] is not None:
                payload[key] = round(payload[key], 4)
        payload["issues"] = list(self.issues)
        return payload

    @classmethod
    def from_json(cls, item):
        values = {key: item[key] for key in cls.__dataclass_fields__ if key in item}
        values["issues"] = tuple(values.get("issues", ()))
        word = cls(**values)
        if not math.isfinite(word.start) or not math.isfinite(word.end) or not 0 <= word.start < word.end:
            raise ValueError("invalid_saved_word_timing")
        return word


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def file_hash(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def score(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and 0 <= value <= 1 else None
    except (TypeError, ValueError):
        return None


def parse_assembly_words(result, *, duration=None, source="original", offset=0.0, source_id="primary"):
    words, rejected = [], []
    for index, item in enumerate(result.get("words") or []):
        try:
            if not isinstance(item, dict):
                raise TypeError("invalid_word_record")
            text = " ".join(str(item.get("text") or "").split())
            start, end = float(item["start"]) / 1000 + offset, float(item["end"]) / 1000 + offset
            if not text or not all(math.isfinite(t) for t in (start, end)) or not 0 <= start < end:
                raise ValueError("invalid_word")
            if duration is not None and end > duration + 0.05:
                raise ValueError("outside_audio")
            label = str(item.get("speaker") or "UNKNOWN")
            words.append(WordTiming(text, start, end, label, score(item.get("confidence")),
                                    score(item.get("speaker_confidence")), label, source, source_id,
                                    original_start=start, original_end=end))
        except (KeyError, TypeError, ValueError) as exc:
            # The native response remains on disk, including unusable records.
            rejected.append({"index": index, "reason": str(exc)})
    return sorted(words, key=lambda w: (w.start, w.end)), rejected


def lexical(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold()
                   if unicodedata.category(c)[0] in {"L", "N"})


def union_spans(spans):
    merged = []
    for start, end in sorted(spans):
        if not all(math.isfinite(v) for v in (start, end)) or start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def coverage(start, end, spans):
    return min(1.0, sum(max(0, min(end, hi) - max(start, lo))
                        for lo, hi in union_spans(spans)) / max(0.001, end - start))


SPEAKER_ISSUES = {"unassigned_speaker", "ambiguous_speaker", "speaker_conflict", "low_speaker_confidence"}


def assign_speakers(words, turns, *, min_overlap=0.5, margin=0.2, min_confidence=0.6):
    """Only direct overlap can assign a turn; reconcile namespaces by consensus."""
    spans = {}
    for turn in turns:
        if str(turn.speaker) not in {"UNKNOWN", "None", ""}:
            spans.setdefault(str(turn.speaker), []).append((turn.start, turn.end))
    spans = {label: union_spans(items) for label, items in spans.items()}
    votes, rankings = {}, []
    for word in words:
        ranked = sorted(((coverage(word.start, word.end, items), label) for label, items in spans.items()), reverse=True)
        rankings.append(ranked)
        if ranked and ranked[0][0] >= min_overlap and (len(ranked) < 2 or ranked[0][0] - ranked[1][0] >= margin):
            key = (word.source_id, word.provider_speaker)
            if word.provider_speaker != "UNKNOWN" and (word.speaker_confidence is None or word.speaker_confidence >= min_confidence):
                ballot = votes.setdefault(key, {})
                label = ranked[0][1]
                ballot[label] = ballot.get(label, 0) + (word.end - word.start) * ranked[0][0]
    mapping = {}
    for key, ballot in votes.items():
        ranked = sorted(((weight, label) for label, weight in ballot.items()), reverse=True)
        if ranked[0][0] / sum(ballot.values()) >= 0.75:
            mapping[key] = ranked[0][1]
    assigned = []
    for word, ranked in zip(words, rankings):
        issues = set(word.issues) - SPEAKER_ISSUES
        if word.speaker_confidence is not None and word.speaker_confidence < min_confidence:
            issues.add("low_speaker_confidence")
        label, method = "UNKNOWN", "unassigned"
        if not spans:
            if word.speaker_method == "primary_overlap":
                label, method = word.speaker, "primary_overlap"
            elif word.source_id == "primary" and (word.speaker_confidence is None or word.speaker_confidence >= min_confidence):
                label = word.provider_speaker if word.provider_speaker != "UNKNOWN" else (word.speaker if word.speaker_method == "unchecked" else "UNKNOWN")
                method = "provider"
        elif ranked and ranked[0][0] >= min_overlap:
            if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < margin:
                issues.add("ambiguous_speaker")
            else:
                label, method = ranked[0][1], "diarization_overlap"
                mapped = mapping.get((word.source_id, word.provider_speaker))
                if mapped and mapped != label:
                    label, method = "UNKNOWN", "conflict"
                    issues.add("speaker_conflict")
        if label == "UNKNOWN":
            issues.add("unassigned_speaker")
        assigned.append(replace(word, speaker=label, speaker_method=method, issues=tuple(sorted(issues))))
    return assigned


def assess_words(words, speech, *, text_threshold=0.5, speaker_threshold=0.6):
    assessed = []
    for word in words:
        issues = set(word.issues) - {"low_text_confidence", "outside_speech", "low_speaker_confidence"}
        if word.confidence is not None and word.confidence < text_threshold:
            issues.add("low_text_confidence")
        if word.speaker_confidence is not None and word.speaker_confidence < speaker_threshold:
            issues.add("low_speaker_confidence")
        if speech is not None and coverage(word.start, word.end, speech) < 0.5:
            issues.add("outside_speech")
        assessed.append(replace(word, issues=tuple(sorted(issues))))
    return assessed


def read_window(path, start, end, rate=16000):
    with sf.SoundFile(path) as stream:
        stream.seek(min(stream.frames, max(0, round(start * stream.samplerate))))
        audio = stream.read(max(0, round((end - start) * stream.samplerate)), dtype="float32", always_2d=True).mean(axis=1)
        native_rate = stream.samplerate
    if native_rate != rate:
        import librosa
        audio = librosa.resample(audio, orig_sr=native_rate, target_sr=rate)
    return np.asarray(audio, dtype=np.float32)


def detect_speech(path):
    import torch
    from silero_vad import load_silero_vad, get_speech_timestamps

    model = load_silero_vad()
    duration = sf.info(path).duration
    spans = []
    # Bounded memory, overlapping windows preserve detection at chunk boundaries.
    for offset in np.arange(0, duration, 29.0):
        audio = read_window(path, float(offset), min(duration, float(offset) + 30))
        for item in get_speech_timestamps(torch.from_numpy(audio), model, sampling_rate=16000,
                                         min_speech_duration_ms=100, min_silence_duration_ms=150,
                                         speech_pad_ms=60, return_seconds=False):
            spans.append((float(offset) + item["start"] / 16000,
                          min(duration, float(offset) + item["end"] / 16000)))
    return union_spans(spans)


@dataclass(frozen=True)
class STTConfig:
    text_threshold: float = 0.5
    speaker_threshold: float = 0.6
    min_overlap: float = 0.5
    speaker_margin: float = 0.2
    max_rechecks: int = 8
    context_seconds: float = 1.0
    max_chunk_seconds: float = 25.0
    agreement: float = 0.92
    language: str | None = None

    def __post_init__(self):
        for value in (self.text_threshold, self.speaker_threshold, self.min_overlap, self.speaker_margin, self.agreement):
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("invalid_stt_threshold")
        if not 0 <= self.max_rechecks <= 32 or not 0 <= self.context_seconds <= 3 or not 5 <= self.max_chunk_seconds <= 60:
            raise ValueError("invalid_stt_budget")


def untranscribed_speech(words, speech, *, min_seconds=0.8, tolerance=0.15):
    """VAD-only gaps are recheck candidates, never proof of missing words."""
    if speech is None:
        return []
    occupied = union_spans((max(0.0, w.start - tolerance), w.end + tolerance) for w in words)
    gaps = []
    for lo, hi in union_spans(speech):
        cursor = lo
        for start, end in occupied:
            if end <= cursor:
                continue
            if start >= hi:
                break
            if start - cursor >= min_seconds:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
            if cursor >= hi:
                break
        if hi - cursor >= min_seconds:
            gaps.append((cursor, hi))
    return gaps


def retry_windows(words, duration, config, speech=None):
    candidates = []
    for word in words:
        if not word.issues:
            continue
        start, end = max(0, word.start - config.context_seconds), min(duration, word.end + config.context_seconds)
        candidates.append((start, end))
    gaps = untranscribed_speech(words, speech)
    context = min(config.context_seconds, config.max_chunk_seconds / 4)
    for start, end in gaps:
        for offset in np.arange(start, end, config.max_chunk_seconds - 2 * context):
            candidates.append((max(0, float(offset) - context),
                               min(duration, end + context, float(offset) + config.max_chunk_seconds - context)))
    proposed = []
    for start, end in sorted(candidates):
        if proposed and start <= proposed[-1][1] + 0.5 and end - proposed[-1][0] <= config.max_chunk_seconds:
            proposed[-1] = (proposed[-1][0], max(end, proposed[-1][1]))
        else:
            proposed.append((start, end))
    # Never replace half a word at a crop boundary. Prefer highest-risk windows.
    windows = []
    for start, end in proposed:
        while True:
            included = [w for w in words if w.start < end and w.end > start]
            expanded = (min([start] + [w.start for w in included]), max([end] + [w.end for w in included]))
            if expanded == (start, end):
                break
            start, end = expanded
            if end - start > config.max_chunk_seconds:
                break
        if end - start <= config.max_chunk_seconds:
            risk = sum(len(w.issues) for w in included) + sum(max(0, min(end, hi) - max(start, lo)) for lo, hi in gaps)
            windows.append((risk, start, end))
    selected = []
    for _, start, end in sorted(windows, reverse=True):
        if any(start < hi and end > lo for lo, hi in selected):
            continue
        selected.append((start, end))
        if len(selected) >= config.max_rechecks:
            break
    return sorted(selected) if config.max_rechecks else []


def accept_recheck(existing, first, second, start, end, speech, config):
    """Require independent ASR agreement and coverage; keep all rejected versions."""
    a, b = lexical(" ".join(w.text for w in first)), lexical(" ".join(w.text for w in second))
    if not a or not b or SequenceMatcher(None, a, b, autojunk=False).ratio() < config.agreement:
        return False, "transcript_disagreement"
    if any(w.confidence is not None and w.confidence < config.text_threshold for w in first):
        return False, "retry_low_text_confidence"
    if any(not 0 <= w.start < w.end <= end + 0.05 or w.start < start - 0.05 for w in first + second):
        return False, "invalid_retry_timing"
    if speech is not None and any(coverage(w.start, w.end, speech) < 0.5 for w in first + second):
        return False, "retry_outside_speech"
    original = [w for w in existing if w.start < end and w.end > start]
    if original and (min(w.start for w in first) > min(w.start for w in original) + 0.5
                     or max(w.end for w in first) < max(w.end for w in original) - 0.5):
        return False, "retry_loses_boundary_words"
    return True, "independent_asr_agreement"


def alignment_groups(words, duration, max_seconds=25.0):
    groups = []
    for word in words:
        if (not groups or word.start - groups[-1][-1].end > 0.7
                or word.end - groups[-1][0].start > max_seconds
                or word.speaker != groups[-1][-1].speaker):
            groups.append([])
        groups[-1].append(word)
    return [(max(0, group[0].start - 0.6), min(duration, max(w.end for w in group) + 0.6), group)
            for group in groups]


def apply_alignment(words, items, offset, limit, speech):
    """Map different tokenizer boundaries by character spans, retaining ASR text."""
    lengths = [len(lexical(w.text)) for w in words]
    expected = "".join(lexical(w.text) for w in words)
    actual = "".join(lexical(str(item["text"])) for item in items)
    if not expected or expected != actual:
        raise ValueError("alignment_text_mismatch")
    spans, position, previous = [], 0, -1.0
    for item in items:
        width = len(lexical(str(item["text"])))
        start, end = offset + float(item["start"]), offset + float(item["end"])
        if (not all(math.isfinite(t) for t in (start, end)) or not offset <= start < end <= limit + 0.02
                or start < previous):
            raise ValueError("invalid_alignment_timing")
        previous = start
        spans.append((position, position + width, start, end))
        position += width
    aligned, position = [], 0
    for word, width in zip(words, lengths):
        matches = [item for item in spans if item[0] < position + width and item[1] > position]
        position += width
        if not matches:
            aligned.append(word)
            continue
        start, end = min(item[2] for item in matches), max(item[3] for item in matches)
        if speech is not None and coverage(start, end, speech) < 0.5:
            raise ValueError("alignment_outside_speech")
        aligned.append(replace(word, start=start, end=end, timing_source="qwen3-forced-aligner"))
    return aligned


def run_qwen_tasks(tasks, job_root, *, timeout=600.0, asr_model="Qwen/Qwen3-ASR-1.7B",
                   aligner_model="Qwen/Qwen3-ForcedAligner-0.6B", device="cuda:0"):
    """Run local models in an owned process, bounded by a deadline and file sizes."""
    if not tasks:
        return {}
    if not math.isfinite(timeout) or not 0 < timeout <= 3600 or len(tasks) > 256:
        raise ValueError("invalid_local_stt_budget")
    job_dir = Path(job_root) / uuid.uuid4().hex
    job_dir.mkdir(parents=True)
    manifest = {"asr_model": asr_model, "aligner_model": aligner_model, "device": device, "tasks": []}
    for index, task in enumerate(tasks):
        leaf = f"audio_{index:04d}.wav"
        sf.write(job_dir / leaf, read_window(task["path"], task["start"], task["end"]), 16000, subtype="PCM_16")
        manifest["tasks"].append({**{key: task[key] for key in ("id", "mode", "language", "text") if key in task}, "audio": leaf})
    atomic_json(job_dir / "manifest.json", manifest)

    async def launch():
        project = str(Path(__file__).resolve().parent.parent)
        if project not in sys.path:
            sys.path.insert(0, project)
        from utils.process_limits import create_owned_subprocess_exec, communicate_bounded
        with (job_dir / "worker.log").open("wb") as log:
            process = await create_owned_subprocess_exec(sys.executable, str(Path(__file__).with_name("stt_worker.py")),
                                                        str(job_dir / "manifest.json"), stdout=log, stderr=log)
            await communicate_bounded(process, timeout)
            if process.returncode:
                raise RuntimeError(f"Local STT worker failed; see {job_dir / 'worker.log'}")
    asyncio.run(launch())
    output = job_dir / "results.json"
    if output.stat().st_size > 4 * 1024 * 1024:
        raise RuntimeError("local_stt_output_budget")
    result = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("invalid_local_stt_result")
    return result


def local_batches(tasks, local):
    results = {}
    for offset in range(0, len(tasks), 64):
        try:
            batch = local(tasks[offset:offset + 64])
            if not isinstance(batch, dict):
                raise ValueError("invalid_local_stt_result")
            results.update(batch)
        except Exception as exc:
            # Preserve completed batches and stop a failed model from consuming
            # another full deadline on each remaining batch.
            for task in tasks[offset:]:
                results[str(task["id"])] = {"error": f"local_worker_unavailable:{type(exc).__name__}"}
            logging.warning("Local STT batch unavailable: %s", exc)
            break
    return results


def process_stt(raw, original_path, vocals_path, turns, destination, config, *,
                vad=detect_speech, recheck=None, local=None, align=True):
    """Persist raw/accepted words and every repair decision before translation."""
    destination = Path(destination)
    duration = sf.info(original_path).duration
    words, rejected = parse_assembly_words(raw, duration=duration)
    before = [w.to_json() for w in words]
    report = {"version": 2, "provider": "assemblyai", "model": raw.get("speech_model_used"),
              "language": config.language or raw.get("language_code"), "duration": duration,
              "input": "original", "audio_sha256": file_hash(Path(original_path)),
              "config": dict(config.__dict__), "recheck_enabled": bool(recheck and local),
              "alignment_enabled": bool(align and local), "rejected_records": rejected,
              "repairs": [], "alignment": []}
    speech = None
    try:
        if vad is not None:
            speech = vad(original_path)
        report["speech_intervals"] = speech
        report["speech_gaps_before"] = untranscribed_speech(words, speech)
        words = assess_words(words, speech, text_threshold=config.text_threshold, speaker_threshold=config.speaker_threshold)
        words = assign_speakers(words, turns, min_overlap=config.min_overlap,
                                margin=config.speaker_margin, min_confidence=config.speaker_threshold)
        windows = retry_windows(words, duration, config, speech) if recheck and local else []
        tasks = [{"id": str(index), "mode": "transcribe", "path": original_path, "start": start, "end": end,
                  "language": report["language"]} for index, (start, end) in enumerate(windows)]
        independent = {}
        if tasks:
            try:
                independent = local_batches(tasks, local)
            except Exception as exc:
                report["recheck_error"] = type(exc).__name__
                logging.warning("Independent STT recheck unavailable: %s", exc)
        for index, (start, end) in enumerate(windows):
            entry = {"start": start, "end": end, "accepted": False}
            report["repairs"].append(entry)
            if str(index) not in independent or "error" in independent[str(index)]:
                entry["reason"] = "independent_stt_unavailable"
                entry["independent"] = independent.get(str(index))
                continue
            try:
                alternate = recheck(vocals_path, start, end, index)
                first, invalid = parse_assembly_words(alternate, duration=duration, source="vocals",
                                                      offset=start, source_id=f"retry_{index}")
                second = [WordTiming(str(w["text"]), start + float(w["start"]), start + float(w["end"]),
                                     source="original", source_id=f"qwen_{index}", timing_source="qwen3-forced-aligner")
                          for w in independent[str(index)].get("words", [])]
                entry.update(words=[w.to_json() for w in first], independent=independent[str(index)], invalid=invalid)
                accept, reason = accept_recheck(words, first, second, start, end, speech, config)
                if invalid:
                    accept, reason = False, "invalid_retry_records"
                entry["accepted"], entry["reason"] = accept, reason
                if accept:
                    # Keep native punctuation and actual confidence from the cloud
                    # recheck; the independent model is evidence, not a fake score.
                    if not turns:
                        # Crop labels have their own namespace. Resolve them only
                        # from direct overlap with trusted primary speaker words.
                        primary = [w for w in words if w.source_id == "primary" and w.speaker != "UNKNOWN"
                                   and (w.speaker_confidence is None or w.speaker_confidence >= config.speaker_threshold)]
                        first = [replace(w, speaker_method="primary_overlap") for w in assign_speakers(
                            first, primary, min_overlap=config.min_overlap, margin=config.speaker_margin,
                            min_confidence=config.speaker_threshold)] if primary else [
                                replace(w, speaker="UNKNOWN", speaker_method="primary_overlap") for w in first]
                    words = sorted([w for w in words if w.end <= start or w.start >= end] + first,
                                   key=lambda w: (w.start, w.end))
            except Exception as exc:
                entry["reason"] = f"recheck_failed:{type(exc).__name__}"
        words = assess_words(words, speech, text_threshold=config.text_threshold, speaker_threshold=config.speaker_threshold)
        words = assign_speakers(words, turns, min_overlap=config.min_overlap,
                                margin=config.speaker_margin, min_confidence=config.speaker_threshold)
        if local and align:
            groups = alignment_groups(words, duration, config.max_chunk_seconds)
            tasks = [{"id": str(index), "mode": "align", "path": original_path, "start": start, "end": end,
                      "text": " ".join(w.text for w in group), "language": report["language"]}
                     for index, (start, end, group) in enumerate(groups)]
            try:
                results = local_batches(tasks, local)
            except Exception as exc:
                results = {}
                report["alignment_error"] = type(exc).__name__
                logging.warning("STT alignment unavailable: %s", exc)
            aligned = []
            for index, (start, end, group) in enumerate(groups):
                entry = {"start": start, "end": end, "accepted": False}
                report["alignment"].append(entry)
                try:
                    result = results[str(index)]
                    if "error" in result:
                        raise ValueError(result["error"])
                    updated = apply_alignment(group, result["words"], start, end, speech)
                    aligned.extend(updated)
                    entry["accepted"] = True
                except (KeyError, TypeError, ValueError) as exc:
                    entry["reason"] = str(exc)
                    aligned.extend(replace(w, issues=tuple(sorted(set(w.issues) | {"alignment_unverified"}))) for w in group)
            words = assess_words(sorted(aligned, key=lambda w: (w.start, w.end)), speech,
                                 text_threshold=config.text_threshold, speaker_threshold=config.speaker_threshold)
            words = assign_speakers(words, turns, min_overlap=config.min_overlap,
                                    margin=config.speaker_margin, min_confidence=config.speaker_threshold)
        if not words:
            raise RuntimeError("STT preparation produced no usable words; native records are retained")
        return words
    except Exception as exc:
        report["error"] = type(exc).__name__
        raise
    finally:
        report["original_words"] = before
        report["words"] = [w.to_json() for w in words]
        report["unresolved_speech_gaps"] = untranscribed_speech(words, speech)
        report["issue_counts"] = {issue: sum(issue in w.issues for w in words)
                                  for issue in sorted({issue for w in words for issue in w.issues})}
        atomic_json(destination, report)
