"""Stage 4a: local IndicTrans2 translation with a local Ollama fallback."""
import json
import logging
import re
import threading
import time
from functools import lru_cache
import requests
from .config import CFG

CODES = {"hi": "hin_Deva", "kn": "kan_Knda", "ta": "tam_Taml", "te": "tel_Telu", "bn": "ben_Beng", "mr": "mar_Deva"}
LANGUAGE_NAMES = {"hi": "Hindi", "kn": "Kannada", "ta": "Tamil", "te": "Telugu", "bn": "Bengali", "mr": "Marathi"}
SCRIPTS = {"hi": (0x0900, 0x097F), "kn": (0x0C80, 0x0CFF), "ta": (0x0B80, 0x0BFF),
           "te": (0x0C00, 0x0C7F), "bn": (0x0980, 0x09FF), "mr": (0x0900, 0x097F)}
GLOSSARIES = {
    "hi": "gravity=गुरुत्वाकर्षण, planet=ग्रह, orbit=कक्षा, star=तारा, Sun=सूर्य, atom=परमाणु, molecule=अणु, chemical reaction=रासायनिक अभिक्रिया, substance=पदार्थ",
    "kn": "gravity=ಗುರುತ್ವಾಕರ್ಷಣೆ, planet=ಗ್ರಹ, orbit=ಕಕ್ಷೆ, star=ನಕ್ಷತ್ರ, Sun=ಸೂರ್ಯ, atom=ಪರಮಾಣು, molecule=ಅಣು, chemical reaction=ರಾಸಾಯನಿಕ ಕ್ರಿಯೆ, substance=ವಸ್ತು",
    "ta": "gravity=ஈர்ப்பு விசை, planet=கோள், orbit=சுற்றுப்பாதை, star=நட்சத்திரம், Sun=சூரியன், atom=அணு, molecule=மூலக்கூறு, chemical reaction=வேதிவினை, substance=பொருள்",
    "te": "gravity=గురుత్వాకర్షణ, planet=గ్రహం, orbit=కక్ష్య, star=నక్షత్రం, Sun=సూర్యుడు, atom=పరమాణువు, molecule=అణువు, chemical reaction=రసాయన చర్య, substance=పదార్థం",
    "bn": "gravity=মহাকর্ষ, planet=গ্রহ, orbit=কক্ষপথ, star=নক্ষত্র, Sun=সূর্য, atom=পরমাণু, molecule=অণু, chemical reaction=রাসায়নিক বিক্রিয়া, substance=পদার্থ",
    "mr": "gravity=गुरुत्वाकर्षण, planet=ग्रह, orbit=कक्षा, star=तारा, Sun=सूर्य, atom=अणू, molecule=रेणू, chemical reaction=रासायनिक अभिक्रिया, substance=पदार्थ",
}
log = logging.getLogger("translate")


@lru_cache(maxsize=1)
def _load():
    import torch  # noqa
    from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
    from IndicTransToolkit.processor import IndicProcessor
    name = CFG["translation"]["model"]
    tok = AutoTokenizer.from_pretrained(name, trust_remote_code=True, local_files_only=True)
    model = AutoModelForSeq2SeqLM.from_pretrained(name, trust_remote_code=True, local_files_only=True).to(CFG["translation"]["device"]).eval()
    return tok, model, IndicProcessor(inference=True)


def _needs_translation(source):
    """Codes, acronyms and numbers ("RM&IPR", "Q1", "(a)", "1970") may stay as they are."""
    return any(not w.isupper() for w in re.findall(r"[A-Za-z]{3,}", source))


def _in_script(source, output, lang):
    start, end = SCRIPTS[lang]
    if not isinstance(output, str) or not output.strip():
        return False
    return any(start <= ord(char) <= end for char in output) or not _needs_translation(source)


def _validate(texts, outputs, lang):
    if not isinstance(outputs, list) or len(outputs) != len(texts):
        raise RuntimeError("translation engine returned the wrong number of strings")
    for source, output in zip(texts, outputs):
        if not isinstance(output, str) or not output.strip():
            raise RuntimeError(f"translation engine returned an empty string for {LANGUAGE_NAMES[lang]}")
        if not _in_script(source, output, lang):
            raise RuntimeError(f"translation output is not in {LANGUAGE_NAMES[lang]} script")
    return [text.strip() for text in outputs]


_INDIC = {"error": None}          # IndicTrans2 availability is checked once per server start


def _indictrans(texts, lang):
    import torch
    if _INDIC["error"]:
        raise RuntimeError(_INDIC["error"])
    try:
        tok, model, ip = _load()
    except Exception as exc:
        _INDIC["error"] = f"model not available offline ({type(exc).__name__}); download it once, see README"
        raise
    out = []
    for i in range(0, len(texts), 6):
        batch = ip.preprocess_batch(texts[i:i + 6], src_lang="eng_Latn", tgt_lang=CODES[lang])
        enc = tok(batch, truncation=True, padding="longest", return_tensors="pt").to(model.device)
        with torch.no_grad():
            gen = model.generate(**enc, num_beams=4, max_length=256, use_cache=True)
        out += ip.postprocess_batch(tok.batch_decode(gen, skip_special_tokens=True), lang=CODES[lang])
    return _validate(texts, out, lang)


_OLLAMA_LOCK = threading.Lock()      # one translation request at a time from this server


def _chat(messages, options, json_format=False):
    """Ask the local model, waiting patiently: a busy Ollama queues requests, which is not an error."""
    payload = {"model": CFG["translation"].get("ollama_model", CFG["ollama"]["model"]), "stream": False,
               "keep_alive": "30m", "options": options, "messages": messages}
    if json_format:
        payload["format"] = "json"
    for attempt in range(3):
        try:
            with _OLLAMA_LOCK:
                response = requests.post(CFG["ollama"]["url"] + "/api/chat", json=payload,
                                         timeout=(10, max(300, CFG["ollama"]["timeout"])))
            response.raise_for_status()
            return response.json()["message"]["content"]
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == 2:
                raise
            log.warning("Ollama busy or unreachable (%s); retrying", type(exc).__name__)
            time.sleep(3 * (attempt + 1))


def _instructions(lang):
    return (f"Translate into simple, natural {LANGUAGE_NAMES[lang]}, written in its native script. Preserve the full "
            "meaning and scientific facts. Keep proper names, abbreviations and code identifiers unchanged. "
            f"Use these scientific terms when applicable: {GLOSSARIES.get(lang, '')}.")


def _ollama_one(text, lang):
    content = _chat([{"role": "system", "content": _instructions(lang) + " Return only the complete translation, with no "
                      "explanation or alternatives."}, {"role": "user", "content": text}],
                    {"temperature": 0, "num_ctx": 2048, "num_predict": 512})
    return content.strip().strip('"')


def _ollama_single(source, lang):
    """One string, two attempts; None when the model keeps answering outside the target script."""
    for _ in range(2):
        translated = _ollama_one(source, lang)
        if _in_script(source, translated, lang):
            return translated.strip()
        log.warning("retrying untranslated string in %s", lang)
    return None


def _ollama_batch(texts, lang):
    """Several strings in one request. Returns a list aligned with texts; wrong-script items are None."""
    numbered = "\n".join(f"{k + 1}. {t}" for k, t in enumerate(texts))
    content = _chat([{"role": "system", "content": _instructions(lang) + ' Translate each numbered item. Return JSON '
                      '{"items": ["translation of item 1", "translation of item 2", ...]} with exactly one translation '
                      "per item, in the same order."}, {"role": "user", "content": numbered}],
                    {"temperature": 0, "num_ctx": 4096, "num_predict": 3072}, json_format=True)
    items = json.loads(content[content.find("{"): content.rfind("}") + 1]).get("items")
    if not isinstance(items, list) or len(items) != len(texts):
        raise RuntimeError("batch translation returned the wrong number of items")
    items = [re.sub(r"^\s*\d+[.)]\s*", "", str(x)).strip().strip('"') for x in items]
    return [x if _in_script(t, x, lang) else None for t, x in zip(texts, items)]


def _chunks(texts, max_items=12, max_chars=1400):
    chunk, size = [], 0
    for k, t in enumerate(texts):
        if chunk and (len(chunk) >= max_items or size + len(t) > max_chars):
            yield chunk
            chunk, size = [], 0
        chunk.append(k)
        size += len(t)
    if chunk:
        yield chunk


def _ollama(texts, lang, progress=None, warnings=None):
    """Batched requests; only the items a batch got wrong are retried one by one. A few stubborn strings may stay in
    English (with a warning); if too many fail, the translation fails instead of mixing languages."""
    outputs = [None] * len(texts)
    done = 0
    for chunk in _chunks(texts):
        batch = [texts[k] for k in chunk]
        try:
            results = _ollama_batch(batch, lang) if len(batch) > 1 else [None]
        except (RuntimeError, ValueError) as exc:          # malformed reply: translate this batch one by one
            log.warning("batch translation to %s failed (%s); translating one by one", lang, exc)
            results = [None] * len(batch)
        for k, source, translated in zip(chunk, batch, results):
            translated = translated or _ollama_single(source, lang)
            if translated and source.rstrip().endswith((".", "!", "?", "।")) and not translated.rstrip().endswith((".", "!", "?", "।")):
                translated += source.rstrip()[-1]
            outputs[k] = translated
        done += len(chunk)
        if progress:
            progress(done / len(texts))
    failed = [t for t, o in zip(texts, outputs) if o is None]
    if len(failed) > max(1, len(texts) * 0.15):
        raise RuntimeError(f"{len(failed)} of {len(texts)} strings could not be translated into {LANGUAGE_NAMES[lang]}")
    if failed:
        log.warning("%d strings kept in English for %s: %s", len(failed), lang, failed[:3])
        if warnings is not None:
            warnings.append(f"{len(failed)} short passage(s) could not be translated into {LANGUAGE_NAMES[lang]} and are shown in English.")
    return [o if o is not None else t for t, o in zip(texts, outputs)]


def translate(texts, lang, progress=None, warnings=None):
    if lang not in CODES:
        raise RuntimeError(f"no translation code for '{lang}'")
    errors = []
    try:
        result = _indictrans(texts, lang)
        if progress:
            progress(1)
        return result
    except Exception as exc:
        errors.append(f"IndicTrans2: {exc}")
        if "download it once" not in str(exc):
            log.warning("IndicTrans2 unavailable for %s: %s", lang, exc)
    try:
        return _ollama(texts, lang, progress, warnings)
    except Exception as exc:
        errors.append(f"local Ollama: {type(exc).__name__}: {exc}")
        log.exception("local translation failed for %s", lang)
    raise RuntimeError(f"Could not translate lesson to {LANGUAGE_NAMES[lang]}. " + " | ".join(errors))
