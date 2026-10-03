"""Exercise actual provider/pipeline functions without model or provider calls."""
import argparse
import ast
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace as NS
from typing import Optional
import unittest
from unittest.mock import Mock, patch

import numpy as np
import requests
import soundfile as sf


ROOT = Path(__file__).resolve().parents[2]


def functions(path, names):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Provider logic is tested directly. Avoid importing GPU dependencies just
    # to test HTTP forms, timeout/cancel behavior, and pipeline decisions.
    future = ast.parse("from __future__ import annotations").body
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    namespace = globals().copy()
    namespace["__package__"] = "videotrans" if path.parent.name == "videotrans" else "ai"
    namespace["__spec__"] = None
    exec(compile(ast.Module(body=future + nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class VoxCPMPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.reference = self.root / "reference.wav"
        sf.write(self.reference, np.zeros(16000), 16000)
        self.output = self.root / "output.wav"
        names = {"SpeakerTurn", "WordTiming", "Phrase", "TranslatedPhrase", "normalize_text",
                 "parse_args", "validate_reference_audio_path", "resolve_tts_reference",
                 "speaker_turns_from_words", "generate_tts_audio", "generate_voxcpm_local_audio",
                 "generate_voxcpm_demo_audio", "_strip_voxcpm_direction", "synthesize_and_sync",
                 "tts_runaway_threshold", "build_speaker_references"}
        self.main = functions(ROOT / "videotrans/main.py", names)
        self.main["AUDIO_REFERENCE_EXTENSIONS"] = {".wav"}
        self.main["normalize_audio"] = Mock(return_value=self.reference)
        self.main["_save_tts_response"] = Mock()
        self.args = NS(voxcpm_clone_mode="reference", voxcpm_url="http://synthetic.invalid/generate",
                       voxcpm_timeout=17, voxcpm_steps=10, voxcpm_demo_cfg=2,
                       voxcpm_demo_normalize=False, voxcpm_demo_denoise=False,
                       dub_sample_rate=48000, tts_backend="voxcpm-demo",
                       tts_shorten_retries=0, tts_runaway_retries=2, max_speedup=1.8,
                       max_overflow=1, tts_shorten_trigger=1.25)
        self.item = self.main["TranslatedPhrase"](self.main["Phrase"](0, 0, 1, "source", "A"), "target")

    def test_local_reference_mode_never_sends_unreliable_asr_text(self):
        response = Mock(headers={"content-type": "audio/wav"}, content=b"audio")
        with patch.object(requests, "post", return_value=response) as post:
            self.main["generate_voxcpm_local_audio"]("(angry) target", self.reference, "wrong ASR", self.output, self.args)
        data = post.call_args.kwargs["data"]
        self.assertEqual("", data["prompt_text"])
        self.assertEqual("false", data["use_prompt_text"])
        self.assertEqual("(angry) target", data["text"])
        self.assertEqual(17, post.call_args.kwargs["timeout"])

    def test_local_prompt_mode_requires_exact_text_and_removes_direction(self):
        self.args.voxcpm_clone_mode = "prompt"
        response = Mock(headers={"content-type": "audio/wav"}, content=b"audio")
        with patch.object(requests, "post", return_value=response) as post:
            self.main["generate_voxcpm_local_audio"]("(angry) target", self.reference, "exact transcript", self.output, self.args)
            self.assertEqual("target", post.call_args.kwargs["data"]["text"])
            self.assertEqual("exact transcript", post.call_args.kwargs["data"]["prompt_text"])
            with self.assertRaisesRegex(ValueError, "exact reference"):
                self.main["generate_voxcpm_local_audio"]("target", self.reference, "", self.output, self.args)
            self.assertEqual(1, post.call_count)

    def test_manual_reference_does_not_invent_a_transcript(self):
        item = self.main["TranslatedPhrase"](self.item.phrase, "target", reference_audio_path=self.reference)
        _, text = self.main["resolve_tts_reference"](item, self.reference, [], [], self.root, self.args)
        self.assertEqual("", text)
        self.args.voxcpm_clone_mode = "prompt"
        with self.assertRaisesRegex(ValueError, "exact reference"):
            self.main["resolve_tts_reference"](item, self.reference, [], [], self.root, self.args)

    def test_missing_verified_speaker_fails_before_any_tts(self):
        generate = Mock()
        self.main.update(build_speaker_references=Mock(return_value={}), generate_tts_audio=generate)
        with self.assertRaisesRegex(RuntimeError, "No verified voice"):
            self.main["synthesize_and_sync"]([self.item], self.args, self.root, self.root,
                                             self.root, self.root, self.reference, [], [])
        generate.assert_not_called()

    def test_prepared_bank_is_reused_in_the_same_run(self):
        build = Mock()
        self.main.update(build_speaker_references=build, generate_tts_audio=Mock(),
                         audio_duration=lambda p: 1, process_phrase_audio=Mock())
        self.main["synthesize_and_sync"]([self.item], self.args, self.root, self.root,
                                         self.root, self.root, self.reference, [], [],
                                         speaker_references={"A": (self.reference, "")})
        build.assert_not_called()

    def test_runaway_audio_is_rejected_after_last_attempt(self):
        process = Mock()
        generate = Mock()
        self.main.update(generate_tts_audio=generate, audio_duration=lambda p: 20,
                         process_phrase_audio=process)
        with self.assertRaisesRegex(RuntimeError, "invalid audio after retries"):
            self.main["synthesize_and_sync"]([self.item], self.args, self.root, self.root,
                                             self.root, self.root, self.reference, [], [],
                                             speaker_references={"A": (self.reference, "")})
        self.assertEqual(3, generate.call_count)
        process.assert_not_called()

    def test_bank_builder_does_not_read_asr_words(self):
        from videotrans import reference_bank
        self.args.reference_min_seconds = 5
        self.args.reference_max_seconds = 12
        self.args.reference_boundary_guard = 0.25
        self.args.reference_similarity = 0.78
        self.args.reference_speaker_margin = 0.08
        self.args.reference_embedding_model = "synthetic"
        class UnreliableWords:
            def __iter__(self):
                raise AssertionError("ASR words must not crop the reference")
        with patch.object(reference_bank, "build_bank", return_value={}) as build:
            self.main["build_speaker_references"]([], UnreliableWords(), self.reference, self.root, self.args)
        self.assertEqual({"vocals": self.reference}, build.call_args.args[1])

    def test_demo_reference_mode_bounds_job_and_preserves_direction(self):
        job = Mock()
        job.result.return_value = str(self.reference)
        client = Mock()
        client.submit.return_value = job
        self.main["_vtrans_get_demo_client"] = Mock(return_value=client)
        gradio = NS(handle_file=lambda path: path)
        with patch.dict(sys.modules, {"gradio_client": gradio}):
            self.main["generate_voxcpm_demo_audio"]("(angry) target", self.reference, "wrong ASR", self.output, self.args)
        self.assertFalse(client.submit.call_args.kwargs["use_prompt_text"])
        self.assertEqual("angry", client.submit.call_args.kwargs["control_instruction"])
        job.result.assert_called_once_with(timeout=17)

    def test_demo_timeout_cancels_and_has_only_three_attempts(self):
        job = Mock()
        job.result.side_effect = TimeoutError("synthetic")
        client = Mock(submit=Mock(return_value=job))
        self.main["_vtrans_get_demo_client"] = Mock(return_value=client)
        with patch.dict(sys.modules, {"gradio_client": NS(handle_file=lambda path: path)}), patch.object(time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                self.main["generate_voxcpm_demo_audio"]("target", self.reference, "", self.output, self.args)
        self.assertEqual(3, job.cancel.call_count)
        self.assertEqual(3, client.submit.call_count)

    def test_general_tts_defaults_to_reference_mode_too(self):
        tts = functions(ROOT / "ai/tts.py", {"_generate_voxcpm_demo"})
        job = Mock(result=Mock(return_value=str(self.reference)))
        client = Mock(submit=Mock(return_value=job))
        tts.update(REF_PATH=self.reference, PROMPT_TEXT="legacy transcript", VOXCPM_CLONE_MODE="reference",
                   VOXCPM_DEMO_CFG=2, VOXCPM_DEMO_NORMALIZE=False, VOXCPM_DEMO_DENOISE=False,
                   VOXCPM_DEMO_TIMEOUT=19, _get_demo_client=Mock(return_value=client),
                   logger=logging.getLogger("test.voxcpm"))
        with patch.dict(sys.modules, {"gradio_client": NS(handle_file=lambda path: path)}):
            self.assertTrue(tts["_generate_voxcpm_demo"]("angry", "target", self.output))
        self.assertFalse(client.submit.call_args.kwargs["use_prompt_text"])
        self.assertEqual("angry", client.submit.call_args.kwargs["control_instruction"])
        job.result.assert_called_once_with(timeout=19)

    def test_general_local_prompt_mode_does_not_pronounce_directions(self):
        tts = functions(ROOT / "ai/tts.py", {"_generate_voxcpm_local", "_split_first_direction"})
        tts.update(REF_PATH=self.reference, PROMPT_TEXT="legacy transcript", VOXCPM_CLONE_MODE="reference",
                   VOXCPM_LOCAL_CFG="2", VOXCPM_LOCAL_STEPS="10", VOXCPM_LOCAL_MAX_LENGTH="2048",
                   VOXCPM_LOCAL_TIMEOUT=19, VOXCPM_LOCAL_URL="http://synthetic.invalid/generate",
                   VOXCPM_DEMO_NORMALIZE=False, VOXCPM_DEMO_DENOISE=False,
                   logger=logging.getLogger("test.voxcpm"))
        response = Mock(status_code=200, headers={"content-type": "audio/wav"}, content=b"audio")
        with patch.object(requests, "post", return_value=response) as post:
            self.assertTrue(tts["_generate_voxcpm_local"]("(angry) target", self.output,
                                                        prompt_text="exact transcript"))
        self.assertEqual("target", post.call_args.kwargs["data"]["text"])
        self.assertEqual("prompt", post.call_args.kwargs["data"]["clone_mode"])
