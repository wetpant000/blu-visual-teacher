"""Stage 4b: local text-to-speech with an explicitly selected language voice."""
import hashlib, logging, os, re, shutil, subprocess, threading, unicodedata, wave
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
import numpy as np
from .config import CFG, DATA, ROOT

log = logging.getLogger("tts")
_PHONEME_LOCK = threading.Lock()


def _write_wav(path, samples, sr):
    pcm = (np.clip(np.asarray(samples, dtype="float32"), -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(int(sr)); w.writeframes(pcm.tobytes())


def wav_seconds(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def wav_envelope(path, fps=25):
    """Loudness per video frame (0-1), used by the presenter's lip-sync."""
    with wave.open(str(path), "rb") as audio:
        rate, width, channels = audio.getframerate(), audio.getsampwidth(), audio.getnchannels()
        raw = audio.readframes(audio.getnframes())
    if width != 2 or not raw:
        return []
    samples = np.frombuffer(raw, dtype="<i2").astype("float32")[::channels] / 32768
    step = max(1, int(rate / fps))
    frames = samples[: len(samples) // step * step].reshape(-1, step)
    if not frames.size:
        return []
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    peak = float(np.percentile(rms, 95)) or 1.0
    return [round(float(v), 2) for v in np.sqrt(np.clip(rms / peak, 0, 1))]


def _validate_wav(path):
    with wave.open(str(path), "rb") as audio:
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
    if samples.size == 0 or np.max(np.abs(samples)) < 2:
        raise RuntimeError("speech engine produced an empty or silent WAV file")


@lru_cache(maxsize=1)
def _kokoro():
    from kokoro_onnx import Kokoro
    c = CFG["tts"]["kokoro"]
    return Kokoro(str(ROOT / c["model_path"]), str(ROOT / c["voices_path"]))


def _kokoro_say(text, spec, out, speed):
    model = _kokoro()
    if spec["voice"] not in model.get_voices():
        raise RuntimeError(f"Kokoro voice '{spec['voice']}' is not in the configured voices file")
    with _PHONEME_LOCK:          # eSpeak phonemization is not thread-safe; the ONNX inference below is
        phonemes = model.tokenizer.phonemize(text, spec["lang"])
    samples, sr = model.create(phonemes, voice=spec["voice"], speed=spec.get("speed", 1.0) * speed,
                               lang=spec["lang"], is_phonemes=True)
    _write_wav(out, samples, sr)


@lru_cache(maxsize=1)
def _parler():
    try:
        from parler_tts import ParlerTTSForConditionalGeneration
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError(
            "AI4Bharat Parler voices require torch, transformers, and parler-tts; see README setup instructions."
        ) from exc
    configured_model = Path(CFG["tts"]["parler"]["model"])
    model_name = configured_model
    if not configured_model.is_absolute() and configured_model.parts[0] == "models":
        model_name = (ROOT / configured_model).resolve()
        missing = []
        if not model_name.is_dir():
            missing.append("model directory")
        else:
            if not (model_name / "config.json").is_file():
                missing.append("config.json")
            has_weights = (
                (model_name / "model.safetensors").is_file()
                or (model_name / "model.safetensors.index.json").is_file()
            )
            if not has_weights:
                missing.append("model.safetensors weights")
            if not (model_name / "tokenizer.json").is_file():
                missing.append("tokenizer.json")
        if missing:
            raise RuntimeError(
                f"AI4Bharat Parler model is incomplete at '{model_name}'; missing "
                f"{', '.join(missing)}."
            )
    model_source = str(model_name)
    model = ParlerTTSForConditionalGeneration.from_pretrained(model_source)
    prompt_tokenizer = AutoTokenizer.from_pretrained(model_source)
    description_tokenizer = AutoTokenizer.from_pretrained(model.config.text_encoder._name_or_path)
    return model, description_tokenizer, prompt_tokenizer


def _parler_say(text, spec, out):
    import torch
    model, description_tokenizer, prompt_tokenizer = _parler()
    desc = (f"{spec['speaker']}'s voice is clear and easy to understand. "
            f"The spoken language is {spec['language']}. {CFG['tts']['parler']['style']}")
    description = description_tokenizer(desc, return_tensors="pt")
    prompt = prompt_tokenizer(text, return_tensors="pt")
    with torch.no_grad():
        generated = model.generate(
            input_ids=description.input_ids,
            attention_mask=description.attention_mask,
            prompt_input_ids=prompt.input_ids,
            prompt_attention_mask=prompt.attention_mask,
        )
    _write_wav(out, generated.cpu().numpy().squeeze(), model.config.sampling_rate)


def _sentences(text, limit=200):
    """Short sentences: Parler is more reliable on them, and they batch well."""
    parts = [m.strip() for m in re.findall(r"[^.!?।]+[.!?।]?", text) if m.strip()] or [text]
    out = []
    for part in parts:
        while len(part) > limit:
            cut = max(part.rfind(",", 0, limit), part.rfind(" ", 0, limit))
            cut = cut if cut > limit // 3 else limit
            out.append(part[:cut + 1].strip())
            part = part[cut + 1:].strip()
        if out and len(part) < 25 and len(out[-1]) + len(part) < limit:
            out[-1] += " " + part
        elif part:
            out.append(part)
    return out


def _parler_batch(sentences, spec, paths):
    """Generate several sentences in one batched pass on all CPU cores; one WAV per sentence."""
    import torch
    torch.set_num_threads(os.cpu_count() or 4)
    model, description_tokenizer, prompt_tokenizer = _parler()
    desc = (f"{spec['speaker']}'s voice is clear and easy to understand. "
            f"The spoken language is {spec['language']}. {CFG['tts']['parler']['style']}")
    order = sorted(range(len(sentences)), key=lambda j: len(sentences[j]))   # similar lengths waste less padding
    batch = max(1, int(CFG["tts"]["parler"].get("batch_size", 4)))
    for start in range(0, len(order), batch):
        rows = order[start:start + batch]
        description = description_tokenizer([desc] * len(rows), return_tensors="pt", padding=True)
        prompt = prompt_tokenizer([sentences[j] for j in rows], return_tensors="pt", padding=True)
        with torch.inference_mode():
            generated = model.generate(
                input_ids=description.input_ids, attention_mask=description.attention_mask,
                prompt_input_ids=prompt.input_ids, prompt_attention_mask=prompt.attention_mask,
                return_dict_in_generate=True)
        for row, j in enumerate(rows):
            _write_wav(paths[j], generated.sequences[row, :int(generated.audios_length[row])].float().cpu().numpy(),
                       model.config.sampling_rate)


def _cache_path(spec, speed, sentence):
    key = hashlib.sha1(f"{spec['id']}|{spec.get('voice', '')}|{spec.get('speaker', '')}|{speed:.3f}|{sentence}".encode()).hexdigest()
    return DATA / "tts-cache" / re.sub(r"[^\w.+-]", "_", spec["id"]) / f"{key}.wav"


def _read_wav(path):
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype("float32") / 32767, w.getframerate()


def speak_many(texts, lang, outs, voice_id=None, speed=1.0, progress=None, on_done=None):
    """One WAV per text, built from cached sentence recordings: a sentence is synthesized once per voice and speed
    and reused by every audiobook, lesson and language rebuild. AI4Bharat Parler sentences are batched.
    Texts are finished in order, and on_done(k) fires as soon as text k is ready (progressive playback)."""
    spec = resolve_voice(lang, voice_id)
    parts = [_sentences(text) for text in texts]
    todo_total = sum(1 for ss in parts for x in dict.fromkeys(ss) if not _cache_path(spec, speed, x).exists()) or 1
    made = 0
    try:
        for k, (sentences, out) in enumerate(zip(parts, outs)):
            todo = [x for x in dict.fromkeys(sentences) if not _cache_path(spec, speed, x).exists()]
            paths = [_cache_path(spec, speed, x) for x in todo]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
            if spec["engine"] == "parler" and todo:
                _parler_batch(todo, spec, paths)
            elif todo:               # ONNX Runtime and eSpeak run independent sentences in parallel threads
                with ThreadPoolExecutor(max_workers=int(CFG["tts"].get("parallel", 2))) as pool:
                    list(pool.map(lambda job: _speak_one(job[0], spec, job[1], speed), zip(todo, paths)))
            made += len(todo)
            if progress:
                progress(made / todo_total)
            clips = [_read_wav(_cache_path(spec, speed, x)) for x in sentences]
            sr = clips[0][1]
            gap = np.zeros(int(sr * 0.16), dtype="float32")
            _write_wav(out, np.concatenate([x for clip, _ in clips for x in (clip, gap)][:-1]), sr)
            _validate_wav(out)
            if on_done:
                on_done(k)
    except Exception as exc:
        log.exception("tts %s failed for %s voice %s", spec["engine"], lang, spec["id"])
        raise RuntimeError(f"Could not synthesize {lang} speech with voice '{spec['name']}': {exc}") from exc
    return spec["engine"]


def _espeak_say(text, voice, out, speed):
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        raise RuntimeError("espeak-ng is not installed")
    rate = str(round(150 * speed))
    subprocess.run([exe, "-v", voice, "-s", rate, "-w", str(out), text], check=True, capture_output=True)


# The viewer's browser speaks the narration (Web Speech API): no synthesis on this machine, so lessons open instantly.
BROWSER = {"id": "browser", "name": "Browser voice · instant", "engine": "browser"}


def voice_options(lang):
    return CFG["tts"]["voices"].get(lang, [])


def fast_voice(lang):
    """A local voice that renders quickly (Parler is slow on a CPU), used when audio files are required."""
    options = voice_options(lang)
    if not options:
        raise RuntimeError(f"No speech voices are configured for language '{lang}'.")
    return next((v for v in options if v["engine"] != "parler"), options[0])


def estimate_seconds(text):
    """Rough speaking time; combining marks (Indic vowel signs) do not count as separate letters."""
    letters = sum(1 for ch in text if not ch.isspace() and unicodedata.category(ch) not in ("Mn", "Mc"))
    return round(max(1.5, letters / 13.0), 3)


def resolve_voice(lang, voice_id=None):
    """The first configured voice is the default: AI4Bharat Parler for Indic languages, Kokoro for English/Hindi."""
    if voice_id == BROWSER["id"]:
        return BROWSER
    options = voice_options(lang)
    if not options:
        raise RuntimeError(f"No speech voices are configured for language '{lang}'.")
    if voice_id is None:
        return options[0]
    for spec in options:
        if spec["id"] == voice_id:
            return spec
    raise ValueError(f"Voice '{voice_id}' is not configured for language '{lang}'.")


def _speak_one(text, spec, out, speed):
    engine = spec["engine"]
    if engine == "kokoro":
        _kokoro_say(text, spec, out, speed)
    elif engine == "parler":
        _parler_say(text, spec, out)
    elif engine == "espeak":
        _espeak_say(text, spec["voice"], out, speed)
    else:
        raise RuntimeError(f"Unsupported speech engine '{engine}'.")
    _validate_wav(out)


def speak(text, lang, out, voice_id=None, speed=1.0):
    """Write speech in the selected voice; synthesis failures remain explicit."""
    if not 0.5 <= speed <= 2.0:
        raise ValueError(f"speech speed must be between 0.5 and 2.0, got {speed}")
    spec = resolve_voice(lang, voice_id)
    try:
        _speak_one(text, spec, out, speed)
    except Exception as exc:
        log.exception("tts %s failed for %s voice %s", spec["engine"], lang, spec["id"])
        raise RuntimeError(f"Could not synthesize {lang} speech with voice '{spec['name']}': {exc}") from exc
    return spec["engine"], None
