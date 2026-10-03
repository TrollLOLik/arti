"""Adversarial acoustic-bank cases; synthetic audio and deterministic embeddings."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as Turn
import unittest

import numpy as np
import soundfile as sf

from videotrans.reference_bank import BankConfig, SAMPLE_RATE, build_bank, safe_intervals


def tone(frequency, seconds):
    return (0.12 * np.sin(2 * np.pi * frequency * np.arange(round(seconds * SAMPLE_RATE)) / SAMPLE_RATE)).astype("float32")


def encode(audio):
    # Distinguishable synthetic identities; this tests bank decisions, not a
    # claim that speaker recognition works on sine waves or on real actors.
    spectrum = np.abs(np.fft.rfft(audio))
    frequency = np.argmax(spectrum) * SAMPLE_RATE / len(audio)
    return np.array([1, 0]) if frequency < 400 else np.array([0, 1])


class ReferenceBankTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source.wav"
        self.destination = self.root / "bank"
        self.config = BankConfig()

    def bank(self, audio, turns, sources=None):
        sf.write(self.source, audio, SAMPLE_RATE)
        return build_bank(turns, sources or {"vocals": self.source}, self.destination, self.config, encode=encode)

    def report(self):
        return json.loads((self.destination / "bank.json").read_text(encoding="utf-8"))

    def test_overlap_and_boundary_drift_are_excluded(self):
        result = safe_intervals([Turn(start=0, end=10, speaker="A"), Turn(start=4, end=6, speaker="B")])
        self.assertEqual([(0.25, 3.75), (6.25, 9.75)], result["A"])
        self.assertEqual([], result["B"])

    def test_two_clean_speakers_get_stable_transcript_free_references(self):
        refs = self.bank(np.concatenate([tone(220, 6), tone(660, 6)]),
                         [Turn(start=0, end=6, speaker="A"), Turn(start=6, end=12, speaker="B")])
        self.assertEqual({"A", "B"}, set(refs))
        for path, transcript in refs.values():
            self.assertEqual("", transcript)
            info = sf.info(path)
            self.assertEqual(SAMPLE_RATE, info.samplerate)
            self.assertGreaterEqual(info.duration, 5)
            self.assertLessEqual(np.max(np.abs(sf.read(path)[0])), 0.89)
        self.assertTrue(all((self.destination / sample["file"]).exists()
                            for data in self.report()["speakers"].values() for sample in data["samples"]))

    def test_unreported_voice_change_inside_turn_is_rejected(self):
        refs = self.bank(np.concatenate([tone(220, 6), tone(660, 6)]),
                         [Turn(start=0, end=12, speaker="A")])
        self.assertEqual({}, refs)
        self.assertTrue(any(c["rejection"] == "voice_changes_inside_sample" for c in self.report()["candidates"]))

    def test_conflicting_pure_voices_under_one_label_are_rejected(self):
        audio = np.concatenate([tone(220, 6), np.zeros(SAMPLE_RATE), tone(660, 6)])
        refs = self.bank(audio, [Turn(start=0, end=6, speaker="A"), Turn(start=7, end=13, speaker="A")])
        self.assertEqual({}, refs)
        self.assertTrue(any(c["rejection"] == "ambiguous_speaker_cluster" for c in self.report()["candidates"]))

    def test_majority_consensus_rejects_a_mislabeled_outlier(self):
        audio = np.concatenate([tone(220, 6), np.zeros(SAMPLE_RATE), tone(220, 6), np.zeros(SAMPLE_RATE), tone(660, 6)])
        refs = self.bank(audio, [Turn(start=s, end=s + 6, speaker="A") for s in (0, 7, 14)])
        self.assertEqual({"A"}, set(refs))
        self.assertTrue(any(c["rejection"] == "speaker_cluster_outlier" for c in self.report()["candidates"]))

    def test_duplicate_stems_cannot_outvote_an_ambiguous_cluster(self):
        audio = np.concatenate([tone(220, 6), np.zeros(SAMPLE_RATE), tone(660, 6)])
        refs = self.bank(audio, [Turn(start=0, end=6, speaker="A"), Turn(start=7, end=13, speaker="A")],
                         sources={"vocals": self.source, "original": self.source})
        self.assertEqual({}, refs)

    def test_same_voice_under_two_speaker_labels_is_ambiguous(self):
        refs = self.bank(tone(220, 12), [Turn(start=0, end=6, speaker="A"), Turn(start=6, end=12, speaker="B")])
        self.assertEqual({}, refs)
        self.assertTrue(any(c["rejection"] == "ambiguous_between_speakers" for c in self.report()["candidates"]))

    def test_short_clean_segments_are_combined_without_repeating_time(self):
        audio = np.concatenate([tone(220, 3), np.zeros(SAMPLE_RATE), tone(220, 3)])
        refs = self.bank(audio, [Turn(start=0, end=3, speaker="A"), Turn(start=4, end=7, speaker="A")])
        self.assertEqual({"A"}, set(refs))
        self.assertEqual(2, len(self.report()["speakers"]["A"]["samples"]))
        self.assertGreaterEqual(sf.info(refs["A"][0]).duration, 5)

    def test_insufficient_audio_never_uses_an_unverified_fallback(self):
        self.assertEqual({}, self.bank(tone(220, 3), [Turn(start=0, end=3, speaker="A")]))
        self.assertEqual("unavailable", self.report()["speakers"]["A"]["status"])

    def test_silence_and_clipping_are_reported(self):
        for audio in (np.zeros(6 * SAMPLE_RATE), tone(220, 6) * 12):
            with self.subTest(peak=float(np.max(np.abs(audio)))):
                self.assertEqual({}, self.bank(audio, [Turn(start=0, end=6, speaker="A")]))
                self.assertTrue(any(c["rejection"] in {"too_quiet", "clipped_audio"} for c in self.report()["candidates"]))

    def test_embedding_failure_writes_diagnostics_and_fails_closed(self):
        sf.write(self.source, tone(220, 6), SAMPLE_RATE)
        def broken(audio):
            raise OSError("synthetic model failure")
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            build_bank([Turn(start=0, end=6, speaker="A")], {"vocals": self.source}, self.destination, self.config, encode=broken)
        self.assertEqual("OSError", self.report()["error"])

    def test_unreadable_source_writes_diagnostics_and_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            build_bank([Turn(start=0, end=6, speaker="A")],
                       {"vocals": self.source}, self.destination, self.config)
        self.assertIn("error", self.report())

    def test_config_rejects_unbounded_or_invalid_durations(self):
        for changes in ({"max_seconds": 31}, {"min_seconds": float("nan")}, {"similarity": 1}, {"guard_seconds": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                BankConfig(**changes)
