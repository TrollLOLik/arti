"""Adversarial STT cases: namespace conflicts, audio crops and lossy repairs."""
import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

from videotrans.stt_pipeline import (WordTiming, STTConfig, parse_assembly_words, assign_speakers,
                                     assess_words, retry_windows, accept_recheck, apply_alignment,
                                     process_stt, coverage, atomic_json, file_hash, local_batches, untranscribed_speech)
from videotrans.stt_worker import language_name
from tests.videotrans.test_voxcpm_pipeline import functions, ROOT


class STTTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.audio = self.root / "audio.wav"
        sf.write(self.audio, np.zeros(10 * 16000), 16000)
        self.report = self.root / "stt_result.json"
        self.turns = [NS(start=1., end=5., speaker="P0")]

    def word(self, text="hello", start=1., end=2., speaker="A", confidence=.95, **kwargs):
        return WordTiming(text, start, end, speaker, confidence, provider_speaker=speaker, **kwargs)

    def raw(self, confidence=.9):
        return {"speech_model_used": "universal-3-5-pro", "language_code": "en",
                "words": [{"text": "Hello!", "start": 1000, "end": 2000,
                           "speaker": "A", "confidence": confidence, "speaker_confidence": .8}]}

    def test_parser_keeps_separate_confidences_and_reports_invalid_records(self):
        raw = self.raw()
        raw["words"] += [{"text": "bad", "start": -1, "end": 100},
                         {"text": "nan", "start": float("nan"), "end": 100},
                         {"text": "late", "start": 9000, "end": 12000}]
        words, rejected = parse_assembly_words(raw, duration=10)
        self.assertEqual(3, len(rejected))
        self.assertEqual(1, len(words))
        self.assertEqual(.8, words[0].speaker_confidence)
        self.assertEqual(.9, words[0].confidence)
        self.assertEqual("A", words[0].provider_speaker)

    def test_nonfinite_confidence_is_unavailable_not_fabricated(self):
        raw = self.raw(float("nan"))
        self.assertIsNone(parse_assembly_words(raw)[0][0].confidence)

    def test_speaker_is_not_assigned_by_nearness(self):
        word = self.word(start=5.2, end=5.5)
        result = assign_speakers([word], self.turns)[0]
        self.assertEqual("UNKNOWN", result.speaker)
        self.assertIn("unassigned_speaker", result.issues)
        self.assertEqual("hello", result.text)

    def test_overlapping_speakers_are_ambiguous(self):
        turns = self.turns + [NS(start=1, end=5, speaker="P1")]
        word = assign_speakers([self.word()], turns)[0]
        self.assertEqual("UNKNOWN", word.speaker)
        self.assertIn("ambiguous_speaker", word.issues)

    def test_same_speaker_turns_are_unioned_not_double_counted(self):
        self.assertEqual(.5, coverage(0, 2, [(0, 1), (.5, 1)]))
        word = self.word(start=0, end=2)
        result = assign_speakers([word], [NS(start=0,end=1,speaker="P0"), NS(start=.5,end=1,speaker="P0")], min_overlap=.7)[0]
        self.assertEqual("UNKNOWN", result.speaker)

    def test_provider_namespace_conflict_remains_unknown(self):
        words = [self.word(start=s, end=s + .7) for s in (1, 2, 3, 4)]
        words.append(self.word(start=7, end=7.5))
        turns = self.turns + [NS(start=7, end=8, speaker="P1")]
        assigned = assign_speakers(words, turns)
        self.assertEqual("P0", assigned[0].speaker)
        self.assertEqual("UNKNOWN", assigned[-1].speaker)
        self.assertIn("speaker_conflict", assigned[-1].issues)

    def test_retry_labels_have_independent_namespaces(self):
        words = [self.word(), self.word(start=7,end=8,source_id="retry_1")]
        turns = self.turns + [NS(start=7,end=8,speaker="P1")]
        self.assertEqual(["P0", "P1"], [w.speaker for w in assign_speakers(words, turns)])

    def test_low_speaker_confidence_without_turns_is_not_trusted(self):
        word = assign_speakers([self.word(speaker_confidence=.2)], [])[0]
        self.assertEqual("UNKNOWN", word.speaker)
        self.assertIn("low_speaker_confidence", word.issues)

    def test_low_provider_speaker_confidence_remains_visible_with_clear_turn(self):
        word = assign_speakers([self.word(speaker_confidence=.2)], self.turns)[0]
        self.assertEqual("P0", word.speaker)
        self.assertIn("low_speaker_confidence", word.issues)

    def test_unmapped_crop_label_is_not_a_global_speaker_without_diarization(self):
        word = assign_speakers([self.word(source_id="retry_0")], [])[0]
        self.assertEqual("UNKNOWN", word.speaker)

    def test_accepted_recheck_maps_crop_labels_to_primary_without_diarization(self):
        local = lambda tasks: {"0":{"words":[{"text":"World","start":1.,"end":2.}]}}
        recheck = lambda *args: {"words":[{"text":"World!","start":1000,"end":2000,
                                         "speaker":"B","confidence":.99,"speaker_confidence":.9}]}
        words = process_stt(self.raw(.2),self.audio,self.audio,[],self.report,STTConfig(),
                            vad=lambda _: [(0,10)],recheck=recheck,local=local,align=False)
        self.assertEqual("World!", words[0].text)
        self.assertEqual("A", words[0].speaker)
        self.assertEqual("B", words[0].provider_speaker)
        self.assertEqual("primary_overlap", words[0].speaker_method)
        self.assertEqual("A", assign_speakers(words,[])[0].speaker)

    def test_alignment_worker_normalizes_provider_locale_codes(self):
        self.assertEqual("English", language_name("en_us"))
        self.assertEqual("Russian", language_name("ru-RU"))
        self.assertIsNone(language_name(None))

    def test_batches_preserve_completed_work_and_stop_after_a_failed_batch(self):
        tasks = [{"id":str(index)} for index in range(150)]
        local = Mock(side_effect=[{"0":{"words":[]}},TimeoutError("synthetic")])
        result = local_batches(tasks,local)
        self.assertEqual(2,local.call_count)
        self.assertIn("0",result)
        self.assertIn("TimeoutError",result["149"]["error"])

    def test_speech_check_preserves_silent_region_words(self):
        result = assess_words([self.word(confidence=.2)], [(4, 5)])[0]
        self.assertEqual("hello", result.text)
        self.assertEqual({"low_text_confidence", "outside_speech"}, set(result.issues))

    def test_retry_budget_preserves_context_and_whole_boundary_words(self):
        words = [replace(self.word(start=s,end=s+.3),issues=("low_text_confidence",)) for s in (1, 4, 7)]
        windows = retry_windows(words, 10, STTConfig(max_rechecks=2))
        self.assertEqual(2, len(windows))
        self.assertTrue(all(0 <= start < end <= 10 for start,end in windows))
        self.assertEqual([],retry_windows(words,10,STTConfig(max_rechecks=0)))

    def test_vad_gaps_trigger_recheck_without_low_confidence_words(self):
        words = [self.word(start=1,end=2)]
        speech = [(1,5)]
        self.assertEqual([(2.15,5)],untranscribed_speech(words,speech))
        self.assertEqual([(1,6)],retry_windows(words,10,STTConfig(),speech))
        local = lambda tasks: {"0":{"words":[{"text":"Hello","start":0,"end":1},
                                               {"text":"World","start":2,"end":4}]}}
        recheck = lambda *args: {"words":[{"text":"Hello!","start":0,"end":1000,"speaker":"X","confidence":.95},
                                          {"text":"World!","start":2000,"end":4000,"speaker":"X","confidence":.95}]}
        accepted = process_stt(self.raw(),self.audio,self.audio,self.turns,self.report,STTConfig(),
                               vad=lambda _: speech,recheck=recheck,local=local,align=False)
        self.assertEqual(["Hello!","World!"],[w.text for w in accepted])
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertTrue(report["repairs"][0]["accepted"])
        self.assertEqual([],report["unresolved_speech_gaps"])

    def test_short_pauses_are_not_missing_speech_and_long_gaps_obey_budget(self):
        words = [self.word(start=1,end=2),self.word(start=2.7,end=4)]
        self.assertEqual([],untranscribed_speech(words,[(1,4)]))
        windows = retry_windows([],100,STTConfig(max_rechecks=2),[(0,100)])
        self.assertEqual(2,len(windows))
        self.assertTrue(all(end-start<=25 for start,end in windows))

    def test_disagreeing_models_do_not_overwrite_primary(self):
        first,second = [self.word("yes")],[self.word("no")]
        accepted,reason = accept_recheck(first,first,second,0,3,[(0,3)],STTConfig())
        self.assertFalse(accepted)
        self.assertEqual("transcript_disagreement",reason)

    def test_agreement_cannot_remove_a_boundary_word(self):
        existing = [self.word(start=0,end=.5),self.word(start=2,end=3)]
        retry = [self.word(start=2,end=3)]
        self.assertEqual((False,"retry_loses_boundary_words"), accept_recheck(existing,retry,retry,0,4,[(0,4)],STTConfig()))

    def test_alignment_reconciles_tokenization_and_keeps_asr_punctuation(self):
        words=[self.word("can't!",start=4,end=5),self.word("go.",start=5,end=6)]
        items=[{"text":"can","start":.1,"end":.4},{"text":"t","start":.4,"end":.5},{"text":"go","start":.6,"end":.9}]
        aligned=apply_alignment(words,items,4,6,[(4,6)])
        self.assertEqual(["can't!","go."],[w.text for w in aligned])
        self.assertEqual((4.1,4.5),(aligned[0].start,aligned[0].end))
        self.assertEqual(4.,aligned[0].original_start or words[0].start)
        self.assertEqual("qwen3-forced-aligner",aligned[0].timing_source)

    def test_bad_alignment_fails_without_destroying_words(self):
        for item in ({"text":"wrong","start":0,"end":1},
                     {"text":"hello","start":0,"end":float("nan")},
                     {"text":"hello","start":0,"end":20}):
            with self.subTest(item=item),self.assertRaises(ValueError):
                apply_alignment([self.word()],[item],0,3,[(0,3)])

    def test_full_report_keeps_original_rejected_and_accepted_variants(self):
        raw=self.raw(.2)
        def local(tasks):
            if tasks[0]["mode"]=="transcribe":
                return {"0":{"text":"World!","words":[{"text":"World","start":1,"end":2}]}}
            # The alignment crop starts at 0.4 seconds.
            return {"0":{"words":[{"text":"World","start":.7,"end":1.6}]}}
        recheck=Mock(return_value={"words":[{"text":"World!","start":1000,"end":2000,"speaker":"X","confidence":.99}]})
        words=process_stt(raw,self.audio,self.audio,self.turns,self.report,STTConfig(context_seconds=1),
                          vad=lambda _: [(0,10)],recheck=recheck,local=local)
        self.assertEqual("World!",words[0].text)
        report=json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual("Hello!",report["original_words"][0]["text"])
        self.assertTrue(report["repairs"][0]["accepted"])
        self.assertEqual("P0",words[0].speaker)
        self.assertAlmostEqual(1.1,words[0].start)
        self.assertEqual(1.,words[0].original_start)

    def test_missing_aligner_preserves_provider_timings_and_reports_failure(self):
        def unavailable(tasks): raise TimeoutError("synthetic")
        words=process_stt(self.raw(),self.audio,self.audio,self.turns,self.report,STTConfig(),
                          vad=lambda _: [(0,10)],local=unavailable)
        self.assertEqual((1.,2.),(words[0].start,words[0].end))
        self.assertIn("alignment_unverified",words[0].issues)
        self.assertIn("TimeoutError",json.loads(self.report.read_text())["alignment"][0]["reason"])

    def test_empty_stt_result_has_diagnostics_and_stops(self):
        with self.assertRaisesRegex(RuntimeError,"no usable words"):
            process_stt({"words":[]},self.audio,self.audio,[],self.report,STTConfig(),vad=None)
        self.assertEqual("RuntimeError",json.loads(self.report.read_text())["error"])

    def test_saved_word_round_trip_preserves_provenance(self):
        word=self.word(speaker_confidence=.6,source_id="retry_2",issues=("outside_speech",))
        self.assertEqual(word,WordTiming.from_json(word.to_json()))


class MainSTTIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.audio=self.root/"source.wav"
        sf.write(self.audio,np.zeros(16000),16000)
        self.raw=self.root/"raw.json"
        self.ns=functions(ROOT/"videotrans/main.py",{"SpeakerTurn","Phrase","TranslatedPhrase","normalize_text",
                         "parse_bool","is_audio_reference_path","save_json","save_dub_plan","load_dub_plan","merge_words_into_phrases",
                         "assemblyai_payload","transcribe_audio","_parse_assemblyai_words","_parse_assemblyai_utterances",
                         "_clone_phrase_with_bounds","snap_phrases_to_silence"})
        self.ns.update(assign_speakers=assign_speakers,atomic_json=atomic_json,file_hash=file_hash,
                       parse_assembly_words=parse_assembly_words,AUDIO_REFERENCE_EXTENSIONS={".wav", ".mp3"})
        self.args=NS(assemblyai_api_key="synthetic",asr_models="universal-3-5-pro,universal-2",asr_language=None,
                     asr_context="",asr_keyterms_file=None,asr_request_timeout=10,asr_poll_timeout=20)

    def test_unknown_and_short_words_survive_phrase_merge(self):
        words=[WordTiming("No!",.01,.1),WordTiming("yes",.5,.6)]
        phrases=self.ns["merge_words_into_phrases"](words,[NS(start=1,end=2,speaker="P0")],1,15,min_phrase_duration=.4)
        self.assertEqual("No! yes"," ".join(p.text for p in phrases))
        self.assertTrue(all(p.speaker=="UNKNOWN" and p.needs_review for p in phrases))

    def test_dub_plan_round_trip_keeps_speaker_confidence_and_source(self):
        word=WordTiming("hello",0,1,"A",.7,.8,"A",source_id="retry_0",issues=("alignment_unverified",))
        phrase=self.ns["Phrase"](0,0,1,"hello",speaker="A",words=[word])
        item=self.ns["TranslatedPhrase"](phrase,"привет")
        path=self.root/"plan.json"
        self.ns["save_dub_plan"](path,[item])
        loaded=self.ns["load_dub_plan"](path)
        self.assertEqual(word,loaded[0].phrase.words[0])

    def test_submission_requests_speaker_confidence_and_modern_models(self):
        payload=self.ns["assemblyai_payload"]("https://synthetic.invalid/audio",self.args)
        self.assertEqual(["universal-3-5-pro","universal-2"],payload["speech_models"])
        self.assertTrue(payload["speaker_options"]["include_speaker_confidence"])
        self.assertTrue(payload["language_detection"])

    def test_known_russian_does_not_send_unsupported_pro_prompt(self):
        self.args.asr_language="ru"
        self.args.asr_context="Dialogue"
        payload=self.ns["assemblyai_payload"]("",self.args)
        self.assertEqual("ru",payload["language_code"])
        self.assertNotIn("prompt",payload)

    def test_provider_locale_is_normalized_for_recheck_request(self):
        self.args.asr_language="en_us"
        self.args.asr_context="Dialogue"
        payload=self.ns["assemblyai_payload"]("",self.args)
        self.assertEqual("en",payload["language_code"])
        self.assertEqual("Dialogue",payload["prompt"])

    def test_universal_2_omits_pro_prompt_and_uses_smaller_keyterm_budget(self):
        self.args.asr_models="universal-2"
        self.args.asr_language="en"
        self.args.asr_context="Dialogue"
        self.assertNotIn("prompt",self.ns["assemblyai_payload"]("",self.args))
        terms=self.root/"terms.json"
        terms.write_text(json.dumps(["term"]*201),encoding="utf-8")
        self.args.asr_keyterms_file=str(terms)
        with self.assertRaisesRegex(ValueError,"200"):
            self.ns["assemblyai_payload"]("",self.args)

    def test_keyterm_budget_counts_words_in_multiword_names(self):
        terms=self.root/"terms.json"
        terms.write_text(json.dumps(["name surname"]*101),encoding="utf-8")
        self.args.asr_keyterms_file=str(terms)
        with self.assertRaisesRegex(ValueError,"200-word"):
            self.ns["assemblyai_payload"]("",self.args)

    def test_timeout_resume_reuses_remote_job_and_completed_result(self):
        result={"words":[{"text":"hello","start":0,"end":500,"speaker":"A"}]}
        upload,submit=Mock(return_value="upload"),Mock(return_value="job")
        poll=Mock(side_effect=[TimeoutError("synthetic"),result])
        self.ns.update(_assemblyai_headers=Mock(return_value={}),_upload_audio_assemblyai=upload,
                       _submit_transcript_assemblyai=submit,_poll_transcript_assemblyai=poll)
        with self.assertRaises(TimeoutError): self.ns["transcribe_audio"](self.audio,self.raw,self.args)
        self.ns["transcribe_audio"](self.audio,self.raw,self.args)
        self.ns["transcribe_audio"](self.audio,self.raw,self.args)
        self.assertEqual(1,submit.call_count)
        self.assertEqual(1,upload.call_count)
        self.assertEqual(2,poll.call_count)

    def test_vad_padding_does_not_cut_words_or_serialize_real_overlap(self):
        Phrase=self.ns["Phrase"]
        phrases=[Phrase(0,1,2,"first"),Phrase(1,1.8,3,"second")]
        snapped=self.ns["snap_phrases_to_silence"](phrases,self.audio,speech_intervals=[(.9,3.1)])
        self.assertLessEqual(snapped[0].start,1)
        self.assertGreaterEqual(snapped[0].end,2)
        self.assertLessEqual(snapped[1].start,1.8)
        self.assertGreaterEqual(snapped[1].end,3)
