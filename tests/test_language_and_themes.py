import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import wave
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from pydantic import ValidationError

from backend import export, main, pipeline, planner, theme, tts, translate
from backend.fallback import FALLBACK
from backend.models import Plan


KOKORO_MODEL = tts.ROOT / tts.CFG["tts"]["kokoro"]["model_path"]
KOKORO_VOICES = tts.ROOT / tts.CFG["tts"]["kokoro"]["voices_path"]
HAS_KOKORO = importlib.util.find_spec("kokoro_onnx") and KOKORO_MODEL.is_file() and KOKORO_VOICES.is_file()


class ThemeTests(unittest.TestCase):
    def test_topic_themes_are_deterministic_and_distinct(self):
        astronomy = theme.theme_for("How do planets orbit stars?")
        chemistry = theme.theme_for("Balancing a chemical reaction")
        self.assertEqual(astronomy, theme.theme_for("How do planets orbit stars?"))
        self.assertEqual(astronomy["archetype"], "space")
        self.assertEqual(chemistry["archetype"], "laboratory")
        self.assertNotEqual(astronomy["id"], chemistry["id"])
        self.assertNotEqual(astronomy["palette"], chemistry["palette"])
        self.assertNotEqual(astronomy["character"], chemistry["character"])
        self.assertNotEqual(
            theme.theme_for("Photosynthesis", "lesson-a")["id"],
            theme.theme_for("Photosynthesis", "lesson-b")["id"],
        )


class LessonHistoryTests(unittest.TestCase):
    def test_history_lists_saved_language_lessons_newest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for lesson_id, topic, language, title, timestamp in (
                ("older", "How do loops work?", "hi", "लूप", 100),
                ("newer", "What is gravity?", "kn", "ಗುರುತ್ವ", 200),
            ):
                lesson_dir = root / "lessons" / lesson_id
                (lesson_dir / language).mkdir(parents=True)
                (lesson_dir / "plan.json").write_text(
                    json.dumps({"topic": topic}), encoding="utf-8"
                )
                lesson_path = lesson_dir / language / "lesson.json"
                lesson_path.write_text(json.dumps({"title": title}), encoding="utf-8")
                os.utime(lesson_path, (timestamp, timestamp))

            with patch("backend.main.DATA", root):
                history = main.lesson_history()

        self.assertEqual(
            [(entry["id"], entry["topic"], entry["language"]) for entry in history],
            [
                ("newer", "What is gravity?", "kn"),
                ("older", "How do loops work?", "hi"),
            ],
        )
        self.assertEqual(history[0]["title"], "ಗುರುತ್ವ")
        self.assertTrue(history[0]["updated_at"].endswith("+00:00"))

    def test_plans_require_topic_diagram_and_allow_multiple_layouts(self):
        lesson = copy.deepcopy(FALLBACK)
        lesson["scenes"] = [scene for scene in lesson["scenes"] if scene["type"] != "diagram"]
        lesson["scenes"].append(copy.deepcopy(lesson["scenes"][0]))
        with self.assertRaisesRegex(ValidationError, "topic-specific diagram"):
            Plan.model_validate(lesson)

        lesson["scenes"][1]["type"] = "diagram"
        lesson["scenes"][1]["items"] = ["Sunlight", "Chlorophyll", "Glucose"]
        lesson["scenes"][1]["diagram_kind"] = "cycle"
        self.assertEqual(Plan.model_validate(lesson).scenes[1].diagram_kind, "cycle")

    def test_plans_reject_code_walkthrough_scenes(self):
        lesson = copy.deepcopy(FALLBACK)
        lesson["scenes"][1]["type"] = "code_walkthrough"
        with self.assertRaises(ValidationError):
            Plan.model_validate(lesson)

    def test_ffmpeg_video_filter_leaves_motion_to_the_stage(self):
        video_filter = export._video_filter()
        self.assertNotIn("zoompan", video_filter)
        self.assertIn("fade=t=in", video_filter)
        self.assertTrue(video_filter.endswith("format=yuv420p"))

    def test_mp4_export_requires_both_video_and_audio_streams(self):
        streams = [
            {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
            {"codec_type": "audio", "codec_name": "aac"},
        ]
        with patch.object(export.subprocess, "run", return_value=Mock(stdout=json.dumps({"streams": streams}))):
            export._validate_export(Path("lesson.mp4"))

        with patch.object(export.subprocess, "run", return_value=Mock(stdout=json.dumps({"streams": streams[1:]}))):
            with self.assertRaisesRegex(RuntimeError, "H.264 video stream"):
                export._validate_export(Path("lesson.mp4"))

    def test_speech_pacing_targets_requested_runtime_with_safe_limits(self):
        slow = pipeline._speech_speed(60, 0.6, [10.0, 10.0, 10.0], "kokoro")
        fast = pipeline._speech_speed(20, 0.6, [12.0, 12.0, 12.0], "espeak")
        self.assertEqual(slow, 0.75)
        self.assertEqual(fast, 1.25)
        self.assertEqual(pipeline._speech_speed(60, 0.6, [30.0], "parler"), 1.0)


class PlannerPerformanceTests(unittest.TestCase):
    def test_planner_token_budget_scales_with_requested_lesson_length(self):
        self.assertEqual(planner._output_token_budget(20), 384)
        self.assertEqual(planner._output_token_budget(60), 384)
        self.assertEqual(planner._output_token_budget(180), 1080)
        self.assertLess(planner._output_token_budget(60), 60 * 14)

    def test_planner_uses_bounded_timeout_and_falls_back_after_one_failed_request(self):
        with patch("backend.planner.requests.post", side_effect=planner.requests.Timeout("slow")) as request:
            plan, source, warnings = planner.plan_lesson("A repeated topic", "beginner", 60)

        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["timeout"], 60)
        self.assertEqual(source, "fallback")
        self.assertEqual(plan["title"], FALLBACK["title"])
        self.assertTrue(warnings)

    def test_cached_fallback_plan_does_not_repeat_slow_llm_planning(self):
        topic, level, length = "A repeated topic", "beginner", 60
        lesson_id = pipeline.lesson_id(topic, level, length)
        with tempfile.TemporaryDirectory() as directory, patch("backend.pipeline.DATA", Path(directory)):
            plan_path = Path(directory) / "lessons" / lesson_id / "plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps({
                "id": lesson_id, "topic": topic, "length": length, "source": "fallback",
                "warnings": [], "plan": copy.deepcopy(FALLBACK), "plan_seconds": 1.0,
            }), encoding="utf-8")
            with patch("backend.pipeline.planner.plan_lesson") as plan_lesson, \
                    patch("backend.pipeline.localize", return_value={"cached": True}) as localize:
                result = pipeline.create(topic, level, "en", length, "browser", lambda *_: None)

        plan_lesson.assert_not_called()
        localize.assert_called_once_with(lesson_id, "en", "browser", unittest.mock.ANY)
        self.assertEqual(result, {"cached": True})


class NarrationTests(unittest.TestCase):
    def _plan(self, root, length=30):
        lesson_dir = root / "lessons" / "demo"
        lesson_dir.mkdir(parents=True)
        (lesson_dir / "plan.json").write_text(json.dumps({
            "id": "demo", "topic": "loops", "length": length, "source": "llm", "warnings": [],
            "plan": copy.deepcopy(FALLBACK), "plan_seconds": 1.0}), encoding="utf-8")

    def test_browser_voice_skips_synthesis_and_estimates_timing(self):
        with tempfile.TemporaryDirectory() as directory, patch("backend.pipeline.DATA", Path(directory)), \
                patch("backend.tts.speak") as speak:
            self._plan(Path(directory))
            lesson = pipeline.localize("demo", "te", "browser", lambda *a: None)
        speak.assert_not_called()
        self.assertEqual(lesson["engines"], ["browser"])
        self.assertTrue(all(scene["audio"] is None and scene["audio_seconds"] >= 1.5 for scene in lesson["scenes"]))
        self.assertTrue(any(0x0C00 <= ord(ch) <= 0x0C7F for ch in lesson["scenes"][0]["narration"]))

    def test_server_voice_records_each_scene_once_and_reuses_translation(self):
        with tempfile.TemporaryDirectory() as directory, patch("backend.pipeline.DATA", Path(directory)), \
                patch("backend.tts.DATA", Path(directory)):
            self._plan(Path(directory))
            pipeline.localize("demo", "kn", "browser", lambda *a: None)
            with patch("backend.pipeline._translate") as translate_call:
                pipeline.localize("demo", "kn", "kn", lambda *a: None)
            with patch("backend.tts._speak_one", wraps=tts._speak_one) as synthesize:   # a rebuild reuses cached sentences
                lesson = pipeline.localize("demo", "kn", "kn+f2", lambda *a: None)
                self.assertTrue(synthesize.called)
                synthesize.reset_mock()
                lesson = pipeline.localize("demo", "kn", "kn", lambda *a: None)
                synthesize.assert_not_called()
        translate_call.assert_not_called()
        self.assertTrue(all(scene["audio"] and scene["mouth"] for scene in lesson["scenes"]))
        self.assertEqual(lesson["timings"]["translating"], 0.0)


    def test_lesson_streams_scene_by_scene_while_the_voice_over_records(self):
        snapshots = []
        real = tts.speak_many

        def spy(texts, lang, outs, voice_id=None, speed=1.0, progress=None, on_done=None):
            def watch(k):
                on_done(k)
                lesson_path = outs[0].parent / "lesson.json"
                snapshots.append(json.loads(lesson_path.read_text(encoding="utf-8")))
            return real(texts, lang, outs, voice_id, speed, progress, watch if on_done else None)

        with tempfile.TemporaryDirectory() as directory, patch("backend.pipeline.DATA", Path(directory)), \
                patch("backend.tts.DATA", Path(directory)), patch("backend.pipeline.tts.speak_many", side_effect=spy):
            self._plan(Path(directory))
            lesson = pipeline.localize("demo", "kn", "kn", lambda *a: None)
        first = snapshots[0]
        self.assertFalse(first["complete"])
        self.assertTrue(first["scenes"][0]["ready"] and first["scenes"][0]["audio"])
        self.assertFalse(first["scenes"][-1]["ready"])
        self.assertTrue(lesson["complete"])
        self.assertTrue(all(scene["ready"] for scene in lesson["scenes"]))


class LanguageTests(unittest.TestCase):
    def test_builtin_lesson_uses_selected_language_for_educational_text(self):
        for language, bounds in translate.SCRIPTS.items():
            scenes, title = pipeline._translate(copy.deepcopy(FALLBACK["scenes"]), FALLBACK["title"], language)
            self.assertEqual(title, scenes[0]["heading"])
            self.assertTrue(all(
                any(bounds[0] <= ord(char) <= bounds[1] for char in scene["heading"] + scene["narration"])
                for scene in scenes
            ))

    def test_builtin_lesson_covers_every_configured_language(self):
        for language in pipeline.CFG["languages"]:
            if language == "en":
                continue
            self.assertTrue(all(language in scene["i18n"] for scene in FALLBACK["scenes"]), language)

    def test_wav_envelope_follows_speech_loudness(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tone.wav"
            sr = 24000
            tone = 0.5 * np.sin(np.linspace(0, 440 * 2 * np.pi, sr))
            tts._write_wav(path, np.concatenate([np.zeros(sr // 2), tone]), sr)
            envelope = tts.wav_envelope(path)
        self.assertEqual(len(envelope), 37)
        self.assertLess(max(envelope[:10]), 0.05)
        self.assertGreater(min(envelope[-10:]), 0.9)

    def test_language_script_validation_rejects_english_fallback(self):
        self.assertEqual(translate._validate(["Hello"], ["नमस्ते"], "hi"), ["नमस्ते"])
        self.assertEqual(translate._validate(["Hello"], ["ನಮಸ್ಕಾರ"], "kn"), ["ನಮಸ್ಕಾರ"])
        with self.assertRaisesRegex(RuntimeError, "script"):
            translate._validate(["gravity pulls objects"], ["gravity pulls objects"], "kn")

    def test_kannada_voice_generates_language_specific_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kn.wav"
            engine, warning = tts.speak("ನಮಸ್ಕಾರ", "kn", path, "kn+f2")
            self.assertEqual(engine, "espeak")
            self.assertIsNone(warning)
            with wave.open(str(path), "rb") as audio:
                self.assertGreater(audio.getnframes(), 0)

    def test_indic_parler_voices_are_available_for_requested_languages(self):
        expected_speakers = {
            "kn": {"Anu", "Suresh"},
            "te": {"Lalitha", "Prakash"},
            "ta": {"Jaya", "Kavitha"},
            "bn": {"Aditi", "Arjun"},
            "mr": {"Sunita", "Sanjay"},
        }
        for language, speakers in expected_speakers.items():
            voices = tts.voice_options(language)
            parler_voices = [voice for voice in voices if voice["engine"] == "parler"]
            self.assertEqual({voice["speaker"] for voice in parler_voices}, speakers, language)
            self.assertEqual(
                [voice["engine"] for voice in voices[:len(speakers)]],
                ["parler"] * len(speakers),
                language,
            )
            self.assertTrue(any(voice["engine"] == "espeak" for voice in voices), language)
        self.assertEqual(tts.CFG["tts"]["parler"]["model"], "models/indic-parler-tts")

    def test_parler_uses_description_and_text_tokenizers_for_their_inputs(self):
        class FakeTokenizer:
            def __init__(self):
                self.text = None

            def __call__(self, text, **_kwargs):
                self.text = text
                return SimpleNamespace(input_ids=text, attention_mask=f"{text} mask")

        class FakeModel:
            config = SimpleNamespace(sampling_rate=24000)

            def generate(self, **kwargs):
                self.inputs = kwargs
                return SimpleNamespace(cpu=lambda: SimpleNamespace(numpy=lambda: np.array([0.1])))

        description_tokenizer = FakeTokenizer()
        prompt_tokenizer = FakeTokenizer()
        model = FakeModel()
        with patch("backend.tts._parler", return_value=(model, description_tokenizer, prompt_tokenizer)), \
                patch.dict(sys.modules, {"torch": SimpleNamespace(no_grad=nullcontext)}), \
                patch("backend.tts._write_wav"):
            tts._parler_say("ನಮಸ್ಕಾರ", {"speaker": "Anu", "language": "Kannada"}, Path("unused.wav"))

        self.assertIn("Kannada", description_tokenizer.text)
        self.assertIn("Anu", description_tokenizer.text)
        self.assertEqual(prompt_tokenizer.text, "ನಮಸ್ಕಾರ")
        self.assertEqual(model.inputs["input_ids"], description_tokenizer.text)
        self.assertEqual(model.inputs["prompt_input_ids"], prompt_tokenizer.text)

    def test_parler_loads_prompt_tokenizer_from_model_and_description_tokenizer_from_text_encoder(self):
        model = SimpleNamespace(config=SimpleNamespace(
            text_encoder=SimpleNamespace(_name_or_path="google/flan-t5-large")
        ))
        tokenizer = object()
        tokenizer_sources = []
        model_sources = []

        class FakeModelLoader:
            @staticmethod
            def from_pretrained(source):
                model_sources.append(source)
                return model

        class FakeTokenizerLoader:
            @staticmethod
            def from_pretrained(source):
                tokenizer_sources.append(source)
                return tokenizer

        fake_parler = SimpleNamespace(ParlerTTSForConditionalGeneration=FakeModelLoader)
        fake_transformers = SimpleNamespace(AutoTokenizer=FakeTokenizerLoader)
        tts._parler.cache_clear()
        try:
            with patch.dict(sys.modules, {
                "parler_tts": fake_parler,
                "transformers": fake_transformers,
            }):
                loaded_model, description_tokenizer, prompt_tokenizer = tts._parler()
        finally:
            tts._parler.cache_clear()

        self.assertIs(loaded_model, model)
        self.assertIs(description_tokenizer, tokenizer)
        self.assertIs(prompt_tokenizer, tokenizer)
        self.assertEqual(model_sources, [str(tts.ROOT / "models/indic-parler-tts")])
        self.assertEqual(tokenizer_sources[0], str(tts.ROOT / "models/indic-parler-tts"))
        self.assertEqual(tokenizer_sources[1], "google/flan-t5-large")

    def test_parler_reports_missing_local_model_directory(self):
        fake_parler = SimpleNamespace(ParlerTTSForConditionalGeneration=object)
        fake_transformers = SimpleNamespace(AutoTokenizer=object)
        tts._parler.cache_clear()
        try:
            with patch("backend.tts.CFG", {"tts": {"parler": {
                    "model": "models/missing-parler-model",
                }}}), patch.dict(sys.modules, {
                    "parler_tts": fake_parler,
                    "transformers": fake_transformers,
                }):
                with self.assertRaisesRegex(RuntimeError, "AI4Bharat Parler model is incomplete"):
                    tts._parler()
        finally:
            tts._parler.cache_clear()

    @unittest.skipUnless(HAS_KOKORO, "local Kokoro runtime or model files are unavailable")
    def test_kokoro_english_and_hindi_voices_generate_non_silent_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            for language, text, voice in (
                ("en", "This is a short test.", "kokoro-af-heart"),
                ("hi", "यह एक छोटा परीक्षण है।", "kokoro-hf-alpha"),
            ):
                with self.subTest(language=language):
                    path = Path(directory) / f"{language}.wav"
                    engine, warning = tts.speak(text, language, path, voice)
                    self.assertEqual(engine, "kokoro")
                    self.assertIsNone(warning)
                    with wave.open(str(path), "rb") as audio:
                        self.assertEqual(audio.getframerate(), 24000)
                        self.assertGreater(audio.getnframes(), 0)
                        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
                    self.assertGreater(float(np.sqrt(np.mean(samples.astype("float32") ** 2))), 1.0)


if __name__ == "__main__":
    unittest.main()
