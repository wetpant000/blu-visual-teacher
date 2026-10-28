import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import asyncio
from fastapi import HTTPException

from backend import document, main

TEXT = """Photosynthesis: How Plants Make Food
What is photosynthesis?
Photosynthesis is the process by which green plants use sunlight, water and carbon dioxide to make glucose and oxygen.
It takes place mainly in the leaves of the plant.
Chlorophyll is a green pigment that absorbs light energy. Chloroplasts are the organelles in plant cells where
photosynthesis happens. Without chlorophyll, a plant cannot capture the energy of sunlight.
\fRaw materials and products
Plants take in carbon dioxide from the air through tiny pores called stomata. Water is absorbed by the roots and
carried to the leaves through tubes called xylem. Glucose is a simple sugar that the plant uses for energy and growth.
Oxygen is released into the air through the stomata.
\fThe two stages
The light-dependent reactions take place in the thylakoid membranes of the chloroplast. The Calvin cycle is the second
stage of photosynthesis. The Calvin cycle takes place in the stroma and uses ATP and NADPH to turn carbon dioxide into
glucose. The energy is stored in two carrier molecules called ATP and NADPH.
"""
PAGES = TEXT.split("\f")


def offline(*_args, **_kwargs):
    raise document.requests.ConnectionError("offline")


class ReadingTests(unittest.TestCase):
    def test_paragraphs_find_headings_and_sections_follow_them(self):
        paras = document._paragraphs(PAGES)
        sections, truncated = document._sections(paras)
        self.assertFalse(truncated)
        self.assertEqual([s["title"] for s in sections], ["What is photosynthesis?", "Raw materials and products", "The two stages"])
        self.assertEqual([s["pages"] for s in sections], [[1, 1], [2, 2], [3, 3]])

    def test_terms_come_from_definitions_in_the_text(self):
        terms = {t["term"]: t["definition"] for t in document._extract_terms(" ".join(p for _, p in document._paragraphs(PAGES)))}
        self.assertTrue(terms["Chloroplasts"].startswith("Chloroplasts are the organelles"))
        self.assertIn("Stomata", terms)
        self.assertNotIn("It", terms)


class StudyPackTests(unittest.TestCase):
    def study(self, ollama=offline):
        with patch("backend.document._pdf_pages", return_value=PAGES), patch("backend.document._pdf_title", return_value=""), \
                patch("backend.document._ollama_json", side_effect=ollama):
            return document._study(Path("x.pdf"), "beginner", lambda *a: None, "plants.pdf")

    def test_offline_pack_is_built_only_from_pdf_sentences(self):
        study = self.study()
        source = " ".join(TEXT.split())
        for section in study["sections"]:
            for point in section["points"]:
                self.assertIn(point.rstrip("…"), source)
        for q in study["questions"]:
            self.assertEqual(len(q["options"]), 4)
            self.assertIn(q["evidence"], source)
            self.assertIn(q["options"][q["answer"]].casefold(), q["evidence"].casefold())
        self.assertTrue(study["flashcards"])
        self.assertTrue(any("LLM unavailable" in w for w in study["warnings"]))

    def test_unsupported_llm_content_is_removed_by_verification(self):
        wrong = {"question": "What is the role of chlorophyll?", "options": ["Absorbing light energy", "Producing glucose and oxygen",
                 "Absorbing water", "Releasing oxygen"], "answer": "Producing glucose and oxygen"}
        right = {"question": "Which green pigment absorbs light energy?", "options": ["Chlorophyll", "Glucose", "Xylem", "Stomata"],
                 "answer": "Chlorophyll"}

        def ollama(messages, *_):
            prompt = messages[-1]["content"]
            if "Statements:" in prompt:      # verifier: rejects the false point, answers questions independently
                statements = [line for line in prompt.splitlines() if re.match(r"S\d+\. ", line)]
                return {"statements": [{"n": k + 1, "supported": "glucose from moonlight" not in s} for k, s in enumerate(statements)],
                        "questions": [{"n": 1, "option": "A", "quote": "Chlorophyll is a green pigment that absorbs light energy."},
                                      {"n": 2, "option": "A", "quote": "Chlorophyll is a green pigment that absorbs light energy."}]}
            if "summary" in prompt:
                return {"summary": "Photosynthesis is the process by which green plants use sunlight, water and carbon dioxide to make glucose and oxygen."}
            return {"heading": "Basics", "narration": "Photosynthesis is the process by which green plants use sunlight, water and carbon dioxide to make glucose and oxygen. Chlorophyll is a green pigment that absorbs light energy. Plants make glucose from moonlight.",
                    "points": ["Chlorophyll is a green pigment that absorbs light energy.", "Plants make glucose from moonlight at night."],
                    "terms": [{"term": "Chlorophyll", "definition": "Chlorophyll is a green pigment that absorbs light energy."}],
                    "questions": [wrong, right]}

        study = self.study(ollama)
        prompts = [q["question"] for q in study["questions"]]
        self.assertNotIn(wrong["question"], prompts)
        self.assertIn(right["question"], prompts)
        self.assertFalse(any("moonlight" in p for s in study["sections"] for p in s["points"]))
        kept = next(q for q in study["questions"] if q["question"] == right["question"])
        self.assertEqual(kept["options"][kept["answer"]], "Chlorophyll")
        self.assertEqual(kept["evidence"], "Chlorophyll is a green pigment that absorbs light energy.")

    def test_translation_keeps_answers_as_option_indexes(self):
        study = self.study()
        with patch("backend.document.translate.translate", side_effect=lambda texts, lang, progress=None, warnings=None: [f"[{lang}] {t}" for t in texts]):
            pack = document._translated(study, "hi", lambda *a: None)
        for original, translated in zip(study["questions"], pack["questions"]):
            self.assertEqual(translated["answer"], original["answer"])
            self.assertEqual(translated["options"][translated["answer"]], "[hi] " + original["options"][original["answer"]])
        self.assertEqual(pack["concepts"][0]["term_en"], study["concepts"][0]["term"])


class MediaTests(unittest.TestCase):
    def test_video_narration_reuses_the_audiobook_sentences(self):
        study = StudyPackTests().study()
        plan = document._lesson_plan(study, 60)
        chapters = [study["summary"]] + [x["narration"] for x in study["sections"]]
        for scene in plan["scenes"]:
            spoken = document.tts._sentences(scene["narration"])
            self.assertTrue(any(document.tts._sentences(c)[:len(spoken)] == spoken for c in chapters), scene["heading"])

    def test_build_in_hindi_saves_pack_audiobook_and_video_without_retranslating(self):
        fake = lambda texts, lang, progress=None, warnings=None: [f"हिन्दी {t}" for t in texts]
        with tempfile.TemporaryDirectory() as directory, \
                patch("backend.document.DATA", Path(directory)), patch("backend.pipeline.DATA", Path(directory)), \
                patch("backend.document._pdf_pages", return_value=PAGES), patch("backend.document._pdf_title", return_value=""), \
                patch("backend.document._ollama_json", side_effect=offline), \
                patch("backend.document.translate.translate", side_effect=fake) as translate_call, \
                patch("backend.pipeline._translate") as lesson_translate:
            document.build("pdf-test", "beginner", "hi", {"audio", "video"}, "browser", 60, lambda *a: None,
                           pdf_bytes=b"%PDF-1.4", filename="plants.pdf")
            pack = document.load("pdf-test", "beginner", "hi")
            lesson = json.loads((Path(directory) / "lessons" / "pdf-test-b" / "hi" / "lesson.json").read_text(encoding="utf-8"))
        lesson_translate.assert_not_called()
        self.assertEqual(translate_call.call_count, 1)
        self.assertEqual(pack["progress"], {})
        self.assertTrue(all(c["ready"] for c in pack["audiobook"]["chapters"]))
        self.assertTrue(pack["video"]["ready"])
        self.assertTrue(all(scene["narration"].startswith("हिन्दी") for scene in lesson["scenes"]))


class UploadTests(unittest.TestCase):
    def test_upload_rejects_non_pdf_unknown_language_and_bad_output(self):
        class Body:
            def __init__(self, data):
                self.data = data

            async def body(self):
                return self.data

        for data, params in ((b"hello", {"language": "en"}), (b"%PDF-1.4", {"language": "xx"}),
                             (b"%PDF-1.4", {"language": "en", "formats": "nope"})):
            with self.subTest(params=params), self.assertRaises(HTTPException) as caught:
                asyncio.run(main.upload_document(Body(data), **params))
            self.assertEqual(caught.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
