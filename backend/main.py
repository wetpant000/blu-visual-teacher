import json, logging, threading, uuid
from datetime import datetime, timezone
import requests
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from .config import CFG, DATA, LANGS, ROOT
from .models import GenerateIn, RevoiceIn
from . import document, pipeline, tts, export as exporter

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("blu")
app = FastAPI(title="Blu Visual Teacher")
JOBS: dict = {}
LOCK = threading.Lock()          # one heavy job at a time keeps an 8 GB laptop alive


def run_job(fn, *args, **kwargs):
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "queued", "stage": "queued", "progress": None, "result": None, "error": None}

    def report(stage, progress=None):
        JOBS[jid].update(stage=stage, progress=progress)

    def work():
        with LOCK:
            JOBS[jid]["status"] = "running"
            try:
                JOBS[jid]["result"] = fn(*args, report, **kwargs)
                JOBS[jid]["status"] = "done"
            except Exception as e:
                log.exception("job failed")
                JOBS[jid].update(status="error", error=f"{type(e).__name__}: {e}")

    threading.Thread(target=work, daemon=True).start()
    return {"job": jid}


def _check_lang(lang):
    if lang not in LANGS:
        raise HTTPException(400, f"unsupported language '{lang}'")


@app.on_event("startup")
def warm_voices():
    """Load the AI4Bharat model in the background so the first lesson does not wait for it."""
    if not CFG["tts"]["parler"].get("preload", True):
        return
    if not any(v["engine"] == "parler" for voices in CFG["tts"]["voices"].values() for v in voices):
        return

    def load():
        with LOCK:                       # a job that needs the model simply waits for this load
            try:
                tts._parler()
                log.info("AI4Bharat Parler model loaded")
            except Exception:
                log.warning("could not preload the AI4Bharat Parler model", exc_info=True)

    threading.Thread(target=load, daemon=True).start()


@app.get("/api/config")
def config():
    try:
        ollama = requests.get(CFG["ollama"]["url"] + "/api/tags", timeout=1).ok
    except Exception:
        ollama = False
    return {"languages": LANGS, "voices": pipeline.available_voices(), "ollama": ollama, "model": CFG["ollama"]["model"],
            "features": ["documents"]}


@app.get("/api/history")
def lesson_history():
    lessons_root = DATA / "lessons"
    if not lessons_root.is_dir():
        return []

    entries = []
    for lesson_dir in lessons_root.iterdir():
        plan_path = lesson_dir / "plan.json"
        if not lesson_dir.is_dir() or not plan_path.is_file():
            continue
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("Skipping lesson history with unreadable plan: %s", plan_path, exc_info=True)
            continue

        for lesson_path in lesson_dir.glob("*/lesson.json"):
            try:
                lesson = json.loads(lesson_path.read_text(encoding="utf-8"))
                updated_at = lesson_path.stat().st_mtime
            except (OSError, json.JSONDecodeError):
                log.warning("Skipping unreadable lesson history entry: %s", lesson_path, exc_info=True)
                continue
            topic = plan.get("topic") or lesson.get("title") or "Untitled lesson"
            entries.append({
                "id": lesson_dir.name,
                "topic": topic,
                "title": lesson.get("title") or topic,
                "language": lesson_path.parent.name,
                "updated_at": datetime.fromtimestamp(updated_at, timezone.utc).isoformat(),
                "_timestamp": updated_at,
            })

    entries.sort(key=lambda entry: entry["_timestamp"], reverse=True)
    return [{key: value for key, value in entry.items() if key != "_timestamp"} for entry in entries[:30]]


@app.post("/api/generate")
def generate(body: GenerateIn):
    _check_lang(body.language)
    if body.voice and body.voice not in {v["id"] for v in pipeline.available_voices(body.language)}:
        raise HTTPException(400, f"unsupported voice '{body.voice}' for language '{body.language}'")
    return {**run_job(pipeline.create, body.topic, body.level, body.language, body.length, body.voice),
            "lesson": pipeline.lesson_id(body.topic, body.level, body.length)}


@app.get("/api/jobs/{jid}")
def job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404, "unknown job")
    return JOBS[jid]


@app.get("/api/lessons/{i}/{lang}")
def get_lesson(i: str, lang: str):
    p = DATA / "lessons" / i / lang / "lesson.json"
    if not p.exists():
        raise HTTPException(404, "lesson not generated for this language yet")
    return json.loads(p.read_text(encoding="utf-8"))


@app.post("/api/lessons/{i}/{lang}")            # re-voice an existing plan in another language (no re-planning)
def revoice(i: str, lang: str, body: RevoiceIn | None = None):
    _check_lang(lang)
    if not (DATA / "lessons" / i / "plan.json").exists():
        raise HTTPException(404, "unknown lesson")
    voice = body.voice if body else None
    if voice and voice not in {v["id"] for v in pipeline.available_voices(lang)}:
        raise HTTPException(400, f"unsupported voice '{voice}' for language '{lang}'")
    return run_job(pipeline.localize, i, lang, voice)


@app.post("/api/lessons/{i}/{lang}/export")
def export_mp4(i: str, lang: str):
    p = DATA / "lessons" / i / lang / "lesson.json"
    if not p.exists():
        raise HTTPException(404, "lesson not generated for this language yet")
    if not json.loads(p.read_text(encoding="utf-8")).get("complete", True):
        raise HTTPException(409, "the voice-over is still being recorded; export when the lesson is complete")
    return run_job(exporter.export, i, lang)


# ---------- PDF study packs ----------
def _formats(formats):
    chosen = {f for f in formats.split(",") if f in ("video", "audio")}
    if not chosen:
        raise HTTPException(400, "choose at least one output: video, audio")
    return chosen


@app.post("/api/documents")
async def upload_document(request: Request, filename: str = "document.pdf", language: str = "en",
                          level: str = "beginner", formats: str = "video,audio", voice: str | None = None,
                          length: int = 90):
    _check_lang(language)
    if level not in ("beginner", "intermediate"):
        raise HTTPException(400, "level must be beginner or intermediate")
    if voice and voice not in {v["id"] for v in pipeline.available_voices(language)}:
        raise HTTPException(400, f"unsupported voice '{voice}' for language '{language}'")
    data = await request.body()
    if len(data) > document.MAX_BYTES:
        raise HTTPException(413, "PDF is larger than 40 MB")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "the file is not a PDF")
    i = document.doc_id(data)
    return {**run_job(document.build, i, level, language, _formats(formats), voice, max(30, min(180, length)),
                      pdf_bytes=data, filename=Path(filename).name[:120]), "id": i}


@app.post("/api/documents/{i}/{level}/{lang}")    # add an output or a language to an uploaded document
def extend_document(i: str, level: str, lang: str, formats: str = "video,audio", voice: str | None = None, length: int = 90):
    _check_lang(lang)
    if not (DATA / "docs" / i / "source.pdf").exists() or level not in ("beginner", "intermediate"):
        raise HTTPException(404, "unknown document")
    return run_job(document.build, i, level, lang, _formats(formats), voice, max(30, min(180, length)))


@app.get("/api/documents")
def document_history():
    return document.history()


@app.get("/api/documents/{i}/{level}/{lang}")
def get_document(i: str, level: str, lang: str):
    pack = document.load(i, level, lang)
    if not pack:
        raise HTTPException(404, "study pack not generated for this language yet")
    return pack


app.mount("/data", StaticFiles(directory=DATA), name="data")
DIST = ROOT / "frontend" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="web")
