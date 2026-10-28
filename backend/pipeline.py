"""Orchestrates planner -> translator -> TTS. The voice-over sets every scene's length."""
import copy, hashlib, json, time
from .config import CFG, DATA
from . import planner, translate, tts, theme
from .fallback import FALLBACK

PAUSE = CFG["tts"]["pause_seconds"]
def lesson_id(topic, level, length):
    return hashlib.sha1(f"{topic.strip().lower()}|{level}|{length}".encode()).hexdigest()[:12]


def _dir(i): return DATA / "lessons" / i


def _speech_speed(length, pause, audio_seconds, engine):
    if length is None or engine not in {"kokoro", "espeak"} or not audio_seconds:
        return 1.0
    target_audio = max(1.0, length - pause * len(audio_seconds))
    actual_audio = sum(audio_seconds)
    return min(1.25, max(0.75, actual_audio / target_audio))


def available_voices(language=None):
    if language:
        return [{"id": voice["id"], "name": voice["name"]} for voice in [*tts.voice_options(language), tts.BROWSER]]
    return {lang: available_voices(lang) for lang in CFG["languages"]}


def _scene_out(s, audio, secs, mouth):
    return {"type": s["type"], "diagram_kind": s.get("diagram_kind", "flow"),
            "heading": s["heading"], "narration": s["narration"], "items": s.get("items", []),
            "audio": audio, "audio_seconds": round(secs, 3), "duration": round(secs + PAUSE, 3), "mouth": mouth, "ready": True}


def create(topic, level, language, length, voice_id, report):
    i = lesson_id(topic, level, length)
    pp = _dir(i) / "plan.json"
    rec = json.loads(pp.read_text(encoding="utf-8")) if pp.exists() else None
    if not rec or rec.get("length") != length:
        report("planning")
        t = time.time()
        plan, src, warns = planner.plan_lesson(topic, level, length)
        rec = {"id": i, "topic": topic, "length": length, "source": src, "warnings": warns, "plan": plan,
               "plan_seconds": round(time.time() - t, 2)}
        pp.parent.mkdir(parents=True, exist_ok=True)
        pp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return localize(i, language, voice_id, report)


def _translate(scenes, title, lang, warnings=None):
    if all(lang in (s.get("i18n") or {}) for s in scenes):       # built-in sample lesson
        for s in scenes:
            s.update(s["i18n"][lang])
        return scenes, scenes[0]["heading"]
    box = {"title": title}
    refs = [(box, "title")]
    for s in scenes:
        refs += [(s, "heading"), (s, "narration")]
        refs += [(s.setdefault("items", []), j) for j, it in enumerate(s["items"])]
    for (c, k), o in zip(refs, translate.translate([c[k] for c, k in refs], lang, warnings=warnings)):
        c[k] = o
    return scenes, box["title"]


def localize(i, language, voice_id, report):
    """Translate (if needed) + narrate an existing English plan into `language`."""
    rec = json.loads((_dir(i) / "plan.json").read_text(encoding="utf-8"))
    if rec["source"] == "fallback":      # older saved sample lessons may predate newly added translations
        rec["plan"] = {**copy.deepcopy(FALLBACK), "level": rec["plan"]["level"]}
    voice = tts.resolve_voice(language, voice_id)
    lp = _dir(i) / language / "lesson.json"
    if lp.exists() and rec["source"] in ("llm", "document"):
        cached = json.loads(lp.read_text(encoding="utf-8"))
        audio_files_exist = voice["engine"] == "browser" or all(
            (lp.parent / f"s{k}.wav").is_file() for k in range(len(cached.get("scenes", []))))
        if (cached.get("complete", True) and cached.get("voice_id") == voice["id"]
                and cached.get("voice_name") == voice["name"]
                and cached.get("engines") == [voice["engine"]]
                and cached.get("source") == rec["source"]
                and cached.get("requested_length") == rec.get("length")
                and audio_files_exist):
            report("done")
            return {**cached, "cached": True}
    scenes, title = copy.deepcopy(rec["plan"]["scenes"]), rec["plan"]["title"]
    warnings, timings, voice_lang = list(rec["warnings"]), {"planning": rec["plan_seconds"]}, language
    localized = (rec.get("localized") or {}).get(language)
    plan_hash = hashlib.sha1(json.dumps([rec["plan"], localized], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    cached = json.loads(lp.read_text(encoding="utf-8")) if lp.exists() else {}
    if localized:            # documents supply a plan already written in this language: nothing to translate
        scenes, title, timings["translating"] = copy.deepcopy(localized["scenes"]), localized["title"], 0.0
    elif language != "en" and cached.get("plan_hash") == plan_hash and len(cached.get("scenes", [])) == len(scenes):
        # only the voice changed: reuse the translated text instead of translating again
        for s, c in zip(scenes, cached["scenes"]):
            s.update(heading=c["heading"], narration=c["narration"], items=c["items"])
        title, timings["translating"] = cached["title"], 0.0
    elif language != "en":
        report("translating")
        t = time.time()
        scenes, title = _translate(scenes, title, language, warnings)
        timings["translating"] = round(time.time() - t, 2)
    report("narrating", 0)
    t = time.time()
    out_dir = _dir(i) / language
    out_dir.mkdir(parents=True, exist_ok=True)
    out, speed, requested_length = [], 1.0, rec.get("length")
    lesson = {"id": i, "topic": rec.get("topic", ""), "title": title, "level": rec["plan"]["level"],
              "requested_length": requested_length, "plan_hash": plan_hash, "speech_speed": 1.0,
              "language": language, "voice_language": voice_lang, "voice_id": voice["id"], "voice_name": voice["name"],
              "theme": theme.theme_for(rec.get("topic", ""), i), "source": rec["source"], "engines": [voice["engine"]],
              "warnings": warnings, "timings": timings, "scenes": out, "complete": False}

    def save():               # atomic, because the player reads this file while scenes are still being recorded
        tmp = lp.with_suffix(".tmp")
        tmp.write_text(json.dumps(lesson, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(lp)

    if voice["engine"] == "browser":
        out += [_scene_out(s, None, tts.estimate_seconds(s["narration"]), []) for s in scenes]
    else:
        files = [out_dir / f"s{k}.wav" for k in range(len(scenes))]
        texts = [s["narration"] for s in scenes]
        # Topic lessons fit the requested length: time one sentence, predict the rest, pick one speed.
        # Document lessons keep normal speed so they reuse the audiobook's cached sentence recordings.
        if voice["engine"] in {"kokoro", "espeak"} and not rec.get("document") and len(scenes) > 1:
            sample = tts._sentences(texts[0])[0]
            probe = out_dir / "probe.wav"
            tts.speak_many([sample], voice_lang, [probe], voice["id"])
            per_char = tts.wav_seconds(probe) / max(1, len(sample))
            probe.unlink(missing_ok=True)
            speed = _speech_speed(requested_length, PAUSE, [per_char * len(x) for x in texts], voice["engine"])
            speed = 1.0 if abs(speed - 1.0) < 0.05 else speed
        lesson["speech_speed"] = round(speed, 2)
        # Every scene starts as an estimate; the player opens once scene 1 is recorded and the rest stream in.
        out += [{**_scene_out(s, None, tts.estimate_seconds(s["narration"]) / speed, []), "ready": False} for s in scenes]
        stamp = int(time.time())

        def ready(k):
            out[k] = _scene_out(scenes[k], f"/data/lessons/{i}/{language}/s{k}.wav?v={stamp}",
                                tts.wav_seconds(files[k]), tts.wav_envelope(files[k]))
            report("narrating", (k + 1) / len(scenes))
            save()

        save()
        lesson["engines"] = [tts.speak_many(texts, voice_lang, files, voice["id"], speed=speed, on_done=ready)]
    actual_length = sum(scene["duration"] for scene in out)
    if requested_length and voice["engine"] != "browser" and abs(actual_length - requested_length) > max(3.0, requested_length * 0.15):
        warnings.append(f"Speech duration is about {actual_length:.0f}s; requested lesson length is {requested_length}s.")
    timings["narrating"] = round(time.time() - t, 2)
    lesson["complete"] = True
    save()
    report("done")
    return lesson
