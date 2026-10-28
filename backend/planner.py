"""Stage 2: local LLM (Ollama) writes the lesson plan as JSON. Output is validated; on any failure we fall back."""
import copy, json, logging, re
import requests
from .config import CFG
from .models import Plan
from .fallback import FALLBACK

log = logging.getLogger("planner")

SYSTEM = """You are a lesson planner for a teaching video engine. Reply with JSON ONLY, no prose, shaped exactly like:
{"title": str, "level": "beginner"|"intermediate", "scenes": [Scene, ...]}
Scene = {"type": "title_card"|"diagram", "heading": str, "narration": str (required on every scene),
         "diagram_kind": "flow"|"cycle"|"network"|"layers" (diagram only),
         "items": [2-6 strings] (diagram only)}
Every scene MUST include a narration field with spoken text. For example:
{"type":"title_card","heading":"Photosynthesis","narration":"Plants capture light energy."}
{"type":"diagram","diagram_kind":"flow","heading":"How it works","narration":"Follow the energy through the process.","items":["Light","Water","Sugar"]}
Rules: the first scene is a title_card; use only title_card and diagram scene types; never show source code or code
snippets. Explain programming concepts with topic-specific visual flow diagrams and concise labels instead. Every
narration is 2-4 concise plain spoken English sentences with no markdown; include at least one diagram in every lesson;
choose flow for a process, cycle for a repeating loop, network for connected concepts, or layers for hierarchy;
with 4 or more scenes use at least 2 diagram scenes that each explain a different part of the topic, building from
the basic idea to how it works; end with a short recap title_card;
diagram items are concise labels (at most 5 words); be factually correct and build understanding step by step for the
stated level."""


def _prompt(topic, level, length):
    n = max(3, min(6, round(length / 15)))
    target_words = max(n * 18, int(length * 2.6))
    min_words = max(n * 14, int(length * 2.2))
    return (f"Topic: {topic}\nLevel: {level}\nScenes: exactly {n}\n"
            f"Every scene must include a non-empty narration field. Write about {target_words} total spoken words "
            f"across all scenes, and never fewer than {min_words} total words. Distribute the explanation across "
            f"scenes; avoid one-line narration. Include a topic-specific diagram scene. Target runtime: {length} "
            "seconds.\nReturn the complete JSON now.")


def _output_token_budget(length):
    return min(1200, max(384, int(length * 6)))


def _request_timeout():
    c = CFG["ollama"]
    return min(c.get("planning_timeout", 60), c["timeout"])


def _extract(txt):
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    return json.loads(txt[txt.find("{"): txt.rfind("}") + 1])


def _expand_narrations(plan, topic, length, minimum_words):
    scenes = [{"type": scene.type, "heading": scene.heading, "items": scene.items,
               "current_narration": scene.narration}
              for scene in plan.scenes]
    target_words = max(minimum_words, int(length * 2.6))
    r = requests.post(CFG["ollama"]["url"] + "/api/chat", timeout=_request_timeout(), json={
        "model": CFG["ollama"]["model"], "stream": False, "format": "json", "keep_alive": "30m",
        "options": {"temperature": 0.2, "num_ctx": 4096,
                    "num_predict": _output_token_budget(length)},
        "messages": [
            {"role": "system", "content": "Write clear, accurate spoken educational narration. Return JSON only."},
            {"role": "user", "content":
             f"Expand the narration for these {len(scenes)} lesson scenes about {topic!r}. Return exactly "
             f"{{\"narrations\":[{{\"narration\":\"...\"}}]}} with one narration per scene, in the same order. "
             f"Each narration should explain its scene in 2-4 spoken sentences. Aim for {target_words} words total "
             f"and write at least {minimum_words} words overall. Do not change the topic or invent facts. "
             f"Scene details: {json.dumps(scenes, ensure_ascii=False)}"},
        ]})
    r.raise_for_status()
    result = _extract(r.json()["message"]["content"])
    entries = result.get("narrations")
    if not isinstance(entries, list) or len(entries) != len(plan.scenes):
        raise ValueError("narration expansion returned the wrong number of scenes")

    narrations = []
    for entry in entries:
        if isinstance(entry, str):
            try:
                entry = json.loads(entry)
            except json.JSONDecodeError:
                pass
        narration = entry.get("narration") if isinstance(entry, dict) else entry
        if not isinstance(narration, str) or not narration.strip():
            raise ValueError("narration expansion returned an empty scene")
        narrations.append(narration.strip())
    if sum(len(narration.split()) for narration in narrations) < minimum_words:
        raise ValueError("narration expansion did not meet the minimum word count")
    return narrations


def plan_lesson(topic, level, length):
    """Returns (plan_dict, source, warnings). Never raises."""
    c = CFG["ollama"]
    last = None
    expected_scenes = max(3, min(6, round(length / 15)))
    # below this the narration is expanded with a second (slow) LLM call, so keep it a real minimum
    minimum_words = max(expected_scenes * 12, int(length * 1.5))
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": _prompt(topic, level, length)}]
    try:
        r = requests.post(c["url"] + "/api/chat", timeout=_request_timeout(), json={
            "model": c["model"], "stream": False, "format": "json", "keep_alive": "30m",
            "options": {"temperature": 0.3, "num_ctx": 4096,
                        "num_predict": _output_token_budget(length)},
            "messages": messages})
        r.raise_for_status()
        content = r.json()["message"]["content"]
        data = _extract(content)
        data["level"] = level
        plan = Plan.model_validate(data)
        spoken_words = sum(len(scene.narration.split()) for scene in plan.scenes)
        if spoken_words < minimum_words:
            narrations = _expand_narrations(plan, topic, length, minimum_words)
            plan = Plan.model_validate({
                **plan.model_dump(),
                "scenes": [{**scene.model_dump(), "narration": narration}
                           for scene, narration in zip(plan.scenes, narrations)],
            })
        return plan.model_dump(), "llm", []
    except Exception as e:  # network, bad JSON, schema error
        last = e
        log.warning("planner failed: %s", e)
    fb = copy.deepcopy(FALLBACK)
    fb["level"] = level
    return fb, "fallback", [f"Local LLM unavailable or invalid output ({type(last).__name__}); playing the built-in sample lesson."]


def plan_from_notes(title, level, length, notes):
    """Plan an animated lesson that only uses the given study notes (from a PDF). Raises on failure."""
    n = max(3, min(6, round(length / 15)))
    c = CFG["ollama"]
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content":
                 f"{_prompt(title, level, length)}\nBase every scene ONLY on these study notes from the learner's "
                 f"document. Do not add facts, numbers or examples that are not in the notes. Use exactly {n} scenes.\n"
                 f"Notes:\n{notes}"}]
    r = requests.post(c["url"] + "/api/chat", timeout=c["timeout"], json={
        "model": c["model"], "stream": False, "format": "json", "keep_alive": "30m",
        "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": min(2048, max(512, length * 14))},
        "messages": messages})
    r.raise_for_status()
    data = _extract(r.json()["message"]["content"])
    data["level"] = level
    return Plan.model_validate(data).model_dump()
