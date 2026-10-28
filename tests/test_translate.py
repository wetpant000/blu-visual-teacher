import json
import unittest
from unittest.mock import Mock, patch

import requests

from backend import translate


def reply(content):
    return Mock(json=lambda: {"message": {"content": content}}, raise_for_status=lambda: None)


class OllamaTranslationTests(unittest.TestCase):
    def setUp(self):
        translate._INDIC["error"] = "model not available offline; download it once, see README"

    def test_busy_ollama_is_retried_instead_of_failing_the_lesson(self):
        answers = [requests.ReadTimeout("busy"), reply(json.dumps({"items": ["प्रकाश संश्लेषण", "हरा रंग"]}))]
        with patch("backend.translate.requests.post", side_effect=answers), patch("backend.translate.time.sleep"):
            self.assertEqual(translate.translate(["Photosynthesis", "Green colour"], "hi"), ["प्रकाश संश्लेषण", "हरा रंग"])

    def test_codes_and_acronyms_may_stay_unchanged(self):
        with patch("backend.translate.requests.post", return_value=reply(json.dumps({"items": ["RM&IPR", "Q1", "पेटेंट कानून"]}))):
            self.assertEqual(translate.translate(["RM&IPR", "Q1", "Patent law"], "hi"), ["RM&IPR", "Q1", "पेटेंट कानून"])

    def test_one_stubborn_sentence_is_kept_in_english_with_a_warning(self):
        texts = [f"Sentence number {k} about plants." for k in range(10)]
        batch = [f"पौधों के बारे में वाक्य {k}।" for k in range(10)]
        batch[3] = "Sentence number 3 about plants."           # model answered in English
        answers = [reply(json.dumps({"items": batch}))] + [reply("Sentence number 3 about plants.")] * 2
        warnings = []
        with patch("backend.translate.requests.post", side_effect=answers):
            out = translate.translate(texts, "hi", warnings=warnings)
        self.assertEqual(out[3], texts[3])
        self.assertEqual(out[0], batch[0])
        self.assertEqual(len(warnings), 1)

    def test_mostly_untranslated_output_still_fails_instead_of_mixing_languages(self):
        texts = ["The plant grows.", "Leaves are green.", "Roots take water."]
        with patch("backend.translate.requests.post", return_value=reply(json.dumps({"items": texts}))):
            with self.assertRaisesRegex(RuntimeError, "Could not translate"):
                translate.translate(texts, "ta")


if __name__ == "__main__":
    unittest.main()
