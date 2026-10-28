"""PDF -> study pack: an audiobook (listen mode), an animated lesson (visual mode) and exam material
(key concepts, revision notes, flashcards, practice questions).

Accuracy rules: the local LLM only rewrites text taken from the PDF, every generated note, definition, question and
narration is checked against the source words, and anything that is not supported by the PDF is dropped or replaced
by an extract of the PDF itself. Each practice question carries the source sentence and page that support its answer.
Without a local LLM the whole pack is built extractively from the PDF's own sentences."""
import copy, hashlib, json, logging, random, re, shutil, subprocess, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import requests
from .config import CFG, DATA
from .models import Plan
from . import pipeline, planner, translate, tts

log = logging.getLogger("document")
ACTIVE = set()            # (document, level, language) packs being built by this server
MAX_SECTIONS = 6          # local-LLM passes per document; longer PDFs get larger sections
MIN_SECTION, MAX_SECTION = 600, 5000
MAX_BYTES = 40 * 1024 * 1024
STOP = set("""a an the and or but if then than that this these those there their they them it its is are was were be been
being of to in on at by for with from as into about over under between through during before after above below up down out
off can could may might must shall should will would do does did done have has had having not no nor so such which who whom
whose what when where why how all any both each few more most other some own same only very also just your you we our us
he she his her him i me my one two three first second third use used using""".split())


def doc_id(data):
    return "pdf-" + hashlib.sha1(data).hexdigest()[:10]


def _dir(i):
    return DATA / "docs" / i


# ---------- reading the PDF ----------
def _pdf_pages(path):
    if not shutil.which("pdftotext"):
        raise RuntimeError("Reading PDFs needs Poppler's pdftotext (sudo apt install poppler-utils).")
    out = subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), "-"], capture_output=True, check=True).stdout
    pages = out.decode("utf-8", "replace").split("\f")
    while pages and not pages[-1].strip():
        pages.pop()
    return pages


def _pdf_title(path):
    if not shutil.which("pdfinfo"):
        return ""
    info = subprocess.run(["pdfinfo", str(path)], capture_output=True).stdout.decode("utf-8", "replace")
    m = re.search(r"^Title:\s*(.+)$", info, re.M)
    title = (m.group(1).strip() if m else "")
    return "" if re.search(r"untitled|microsoft|\.docx?$|\.pdf$|^\W*$", title, re.I) else title


def _paragraphs(pages):
    """[(page_number, paragraph)] with hyphenation, page numbers and repeated headers/footers removed."""
    lines_per_page = [[l.strip() for l in p.splitlines()] for p in pages]
    counts = Counter(l for lines in lines_per_page for l in set(lines) if l)
    repeated = {l for l, c in counts.items() if len(pages) >= 3 and c >= max(3, len(pages) * 0.5) and len(l) < 90}
    out = []
    for number, lines in enumerate(lines_per_page, 1):
        lines = [l for l in lines if l not in repeated and not re.fullmatch(r"(page\s*)?\d{1,4}(\s*(of|/)\s*\d+)?", l, re.I)]
        widths = sorted(len(l) for l in lines if l)
        wide = widths[int(len(widths) * 0.9)] if widths else 80
        blocks, block, kind = [], [], None

        def flush():
            nonlocal block, kind
            if block:
                text = re.sub(r"(\w)-\s+(\w)", r"\1\2", " ".join(block))
                blocks.append(re.sub(r"\s+", " ", text).strip())
            block, kind = [], None

        for k, line in enumerate(lines):
            if not line:
                flush()
                continue
            prev_done = not block or re.search(r"[.!?:\"”)]$", block[-1])
            heading_like = (len(line) < 0.75 * wide and not re.search(r"[.!,;:]$", line)
                            and line[0].isupper() | line[0].isdigit() and len(line.split()) <= 12)
            if heading_like and (prev_done or kind == "heading"):
                if kind != "heading" or line.endswith("?"):    # a question line is a heading of its own
                    flush()
                kind = "heading"
                block.append(line)
                continue
            if kind == "heading":
                flush()
            kind = "text"
            block.append(line)
            if len(line) < 0.75 * wide and re.search(r"[.!?\"”]$", line):
                flush()
        flush()
        out += [(number, b) for b in blocks]
    return [(n, p) for n, p in out if len(p) > 1]


def _is_heading(p):
    return 3 <= len(p) <= 80 and not re.search(r"[.!:;,]$", p) and len(p.split()) <= 12 and p[0].isalnum()


def _sections(paras):
    total = sum(len(p) for _, p in paras)
    size = min(MAX_SECTION, max(MIN_SECTION, total // MAX_SECTIONS + 1))
    sections, cur, stopped = [], None, False
    for page, p in paras:
        if cur is not None and _is_heading(p) and cur["chars"] == 0:
            cur["title"] = p          # a sub-heading right after a heading names the section more precisely
            continue
        start_new = cur is None or (cur["chars"] >= size) or (_is_heading(p) and cur["chars"] >= size * 0.4)
        if start_new:
            if len(sections) == MAX_SECTIONS:
                stopped = True
                break
            cur = {"title": p if _is_heading(p) else "", "parts": [], "chars": 0, "pages": [page, page]}
            sections.append(cur)
            if _is_heading(p):
                continue
        cur["parts"].append(p)
        cur["chars"] += len(p)
        cur["pages"][1] = page
    sections = [{"title": s["title"], "text": "\n".join(s["parts"]), "pages": s["pages"]} for s in sections if s["chars"] > 80]
    return sections, stopped


# ---------- grounding ----------
def _words(text):
    return [w for w in re.findall(r"\w+", text.casefold()) if len(w) > 3 and w not in STOP]


def _grounded(text, source_words, need=0.5):
    words = _words(text)
    if not words:
        return False
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    if any(n not in source_words["_numbers"] for n in numbers):
        return False
    return sum(w in source_words for w in words) / len(words) >= need


def _vocab(text):
    v = set(_words(text))
    v_dict = dict.fromkeys(v, True)
    v_dict["_numbers"] = set(re.findall(r"\d+(?:\.\d+)?", text))
    return v_dict


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?।])\s+", re.sub(r"\s+", " ", text)) if len(s.strip()) > 20]


def _best_sentence(query, sentences):
    q = set(_words(query))
    return max(sentences, key=lambda s: len(q & set(_words(s))) / (1 + len(_words(s)) ** 0.3), default="")


def _top_sentences(text, k):
    sents = _sentences(text)
    freq = Counter(_words(text))
    scored = sorted(range(len(sents)), key=lambda j: -sum(freq[w] for w in set(_words(sents[j]))) / (1 + len(_words(sents[j]))) ** 0.5)
    return [sents[j] for j in sorted(scored[:k])]


def _short(text, limit):
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _extract_terms(text):
    """Definitions written in the text itself: 'X is a ...', 'X refers to ...', 'X: ...', '... called X'."""
    terms = []
    for sentence in _sentences(text):
        found = []
        m = (re.match(r"^((?:[A-Z][\w-]*|[a-z][\w-]*)(?: [\w-]+){0,3}) (?:is|are) (?:a|an|the|called|defined as) .{15,}", sentence)
             or re.match(r"^((?:[\w-]+)(?: [\w-]+){0,3}) (?:refers to|means|is defined as) .{10,}", sentence)
             or re.match(r"^([A-Z][\w-]*(?: [\w-]+){0,3}):\s+.{15,}", sentence))
        if m:
            found.append((0, m.group(1)))
        found += [(1, t) for t in re.findall(r"\b(?:called|known as|named) ((?:the )?[a-zA-Z][\w-]+(?: [A-Z][\w-]+)?)", sentence)]
        for rank, term in found:
            term = re.sub(r"^(?:the|a|an) ", "", term.strip(), flags=re.I)
            low = term.casefold()
            if (2 < len(term) <= 40 and low not in STOP and not re.search(r"\b(is|are|was|were)$", low)
                    and not low.startswith(("this ", "it ", "there ", "these ", "they "))):
                terms.append((rank, len(terms), {"term": term[0].upper() + term[1:], "definition": _short(sentence, 240)}))
    best = {}
    for rank, order, t in terms:         # a real definition beats a "called X" mention
        key = t["term"].casefold()
        if key not in best or rank < best[key][0]:
            best[key] = (rank, best.get(key, (0, order))[1], t)
    return [t for _, _, t in sorted(best.values(), key=lambda v: v[1])]


# ---------- per-section study notes ----------
SECTION_SYSTEM = """You write accurate study material from a source text. Use ONLY facts stated in the source text.
Never add outside knowledge, examples or numbers that are not in the text. Reply with JSON only."""


def _ollama_json(messages, num_predict=1000):
    c = CFG["ollama"]
    r = requests.post(c["url"] + "/api/chat", timeout=c["timeout"], json={
        "model": c["model"], "stream": False, "format": "json", "keep_alive": "30m",
        "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": num_predict}, "messages": messages})
    r.raise_for_status()
    return planner._extract(r.json()["message"]["content"])


def _section_prompt(text, level):
    return (f"Learner level: {level}\nSource text:\n<<<\n{text}\n>>>\n"
            "Return JSON exactly shaped like:\n"
            '{"heading": "section title, at most 8 words",\n'
            ' "narration": "an audiobook explanation of this text in 4-6 clear spoken sentences",\n'
            ' "points": ["3-4 short revision points"],\n'
            ' "terms": [{"term": "key term that appears in the text", "definition": "one sentence based on the text"}],\n'
            ' "questions": [{"question": "...", "options": ["four", "different", "short", "options"], '
            '"answer": "the exact text of the correct option"}]}\n'
            "Give 0-4 terms and exactly 2 multiple-choice questions whose answers are stated in the text.")


def _match_sentence(quote, sentences, need=0.75):
    """The PDF sentence a quote was copied from, or None when the quote is not really in the text."""
    q = set(_words(quote))
    if not q:
        return None
    best = max(sentences, key=lambda s: len(q & set(_words(s))), default="")
    return best if best and len(q & set(_words(best))) / len(q) >= need else None


def _verify(text, sentences, statements, questions):
    """Second, independent pass: the model judges each statement against the source and answers each question
    without seeing the proposed answer, quoting the sentence that proves it. Returns (statement_ok, question_quotes)."""
    if not statements and not questions:
        return [], []
    listed = "\n".join(f"S{k + 1}. {st}" for k, st in enumerate(statements))
    asked = "\n".join(f"Q{k + 1}. {q['question']} " + " ".join(f"{'ABCD'[j]}) {o}" for j, o in enumerate(q["options"]))
                      for k, q in enumerate(questions))
    raw = _ollama_json([{"role": "system", "content": "You check study material strictly against a source text. "
                         "Judge only by what the text states. Reply with JSON only."},
                        {"role": "user", "content":
                         f"Source text:\n<<<\n{text}\n>>>\n"
                         f"Statements:\n{listed or '(none)'}\n\nQuestions:\n{asked or '(none)'}\n\n"
                         'Return {"statements": [{"n": 1, "supported": true|false}], '
                         '"questions": [{"n": 1, "option": "A"|"B"|"C"|"D"|null, "quote": "exact sentence copied from the text"}]}. '
                         "A statement is supported only if the text states it. For a question choose the option the text "
                         "states is correct, or null if no option is clearly correct."}], 700)
    verdicts = {int(v.get("n", 0)): v.get("supported") is True for v in raw.get("statements") or [] if isinstance(v, dict)}
    answers = {int(v.get("n", 0)): v for v in raw.get("questions") or [] if isinstance(v, dict)}
    statement_ok = [verdicts.get(k + 1, False) for k in range(len(statements))]
    quotes = []
    for k, q in enumerate(questions):
        v = answers.get(k + 1) or {}
        option = str(v.get("option") or "").strip().upper()[:1]
        source = _match_sentence(str(v.get("quote") or ""), sentences)
        quotes.append(source if option and "ABCD".find(option) == q["answer"] and source else None)
    return statement_ok, quotes


def _clean_section(raw, sec, vocab, sentences):
    """Keep only what the source supports; fill gaps from the source itself."""
    text = sec["text"]
    out = {"heading": "", "narration": "", "points": [], "terms": [], "questions": []}
    heading = str(raw.get("heading") or "").strip()
    out["heading"] = _short(sec["title"] or (heading if _grounded(heading, vocab, 0.34) else "") or _top_sentences(text, 1)[0], 80)
    narration = str(raw.get("narration") or "").strip()
    narr_ok = len(narration) > 60 and all(_grounded(s, vocab, 0.45) for s in _sentences(narration) or [narration])
    out["narration"] = narration if narr_ok else " ".join(_top_sentences(text, 5))
    out["points"] = [_short(p, 200) for p in raw.get("points") or [] if isinstance(p, str) and _grounded(p, vocab, 0.55)][:5]
    if len(out["points"]) < 2:
        out["points"] = [_short(s, 200) for s in _top_sentences(text, 4)]
    for t in raw.get("terms") or []:
        if not isinstance(t, dict):
            continue
        term, definition = str(t.get("term", "")).strip(), str(t.get("definition", "")).strip()
        if 1 < len(term) <= 60 and term.casefold() in text.casefold() and _grounded(definition, vocab, 0.55):
            out["terms"].append({"term": term, "definition": _short(definition, 260)})
    for q in raw.get("questions") or []:
        fixed = _clean_question(q, vocab, sentences)
        if fixed:
            out["questions"].append(fixed)
    return out


def _verify_section(cleaned, sec, sentences):
    """Drop generated statements and questions the source does not support (keeps extractive text untouched)."""
    narration = _sentences(cleaned["narration"]) or [cleaned["narration"]]
    statements = cleaned["points"] + [t["definition"] for t in cleaned["terms"]] + narration
    try:
        ok, quotes = _verify(sec["text"], sentences, statements, cleaned["questions"])
    except Exception as exc:
        log.warning("verification failed (%s); keeping only text copied from the PDF", exc)
        ok, quotes = [False] * len(statements), [None] * len(cleaned["questions"])
    verbatim = lambda st: _match_sentence(st, sentences, 0.95) is not None
    n_points, n_terms = len(cleaned["points"]), len(cleaned["terms"])
    keep = [good or verbatim(st) for good, st in zip(ok, statements)]
    cleaned["points"] = [p for p, k in zip(cleaned["points"], keep[:n_points]) if k]
    for extra in _top_sentences(sec["text"], 4):      # top up with the section's own key sentences
        if len(cleaned["points"]) >= 2:
            break
        if extra not in cleaned["points"]:
            cleaned["points"].append(_short(extra, 200))
    cleaned["terms"] = [t for t, k in zip(cleaned["terms"], keep[n_points:n_points + n_terms]) if k]
    kept_narration = [st for st, k in zip(narration, keep[n_points + n_terms:]) if k]
    cleaned["narration"] = " ".join(kept_narration) if len(kept_narration) >= max(3, len(narration) * 0.6) \
        else " ".join(_top_sentences(sec["text"], 5))
    cleaned["questions"] = [dict(q, evidence=_short(src, 300)) for q, src in zip(cleaned["questions"], quotes) if src]


def _clean_question(q, vocab, sentences):
    if not isinstance(q, dict):
        return None
    question = str(q.get("question", "")).strip()
    options = [str(o).strip() for o in q.get("options") or [] if str(o).strip()]
    options = list(dict.fromkeys(options))
    answer = str(q.get("answer", "")).strip()
    if re.fullmatch(r"[A-Da-d][).]?", answer) and len(options) >= "abcd".index(answer[0].lower()) + 1:
        answer = options["abcd".index(answer[0].lower())]
    answer = re.sub(r"^[A-Da-d][).]\s+", "", answer)
    options = [re.sub(r"^[A-Da-d][).]\s+", "", o) for o in options]
    match = next((o for o in options if o.casefold() == answer.casefold()), None)
    if not question or len(options) != 4 or match is None:
        return None
    if not (_grounded(f"{question} {match}", vocab, 0.55) and (_grounded(match, vocab, 0.5) or match.casefold() in " ".join(sentences).casefold())):
        return None
    evidence = _best_sentence(f"{question} {match}", sentences)
    return {"question": question, "options": options, "answer": options.index(match), "evidence": _short(evidence, 300)}


def _cloze_questions(terms, sentences, rng):
    """Extractive questions: identify the term from its definition in the text (offline fallback)."""
    names = list(dict.fromkeys(t["term"] for t in terms))
    out = []
    for t in terms:
        others = [n for n in names if n.casefold() != t["term"].casefold()]
        if len(others) < 3:
            continue
        pattern = re.compile(rf"\b{re.escape(t['term'])}\b", re.I)
        source = next((x for x in sentences if pattern.search(x) and x.casefold().startswith(t["term"].casefold())), None) \
            or next((x for x in sentences if pattern.search(x)), None)
        if not source or len(source) > 260:
            continue
        blanked = pattern.sub("_____", source)
        options = rng.sample(others, 3) + [t["term"]]
        rng.shuffle(options)
        out.append({"question": f"Which term completes this statement from the text? “{blanked}”",
                    "options": options, "answer": options.index(t["term"]),
                    "evidence": source})
    return out


# ---------- the study pack (English) ----------
def _study(pdf, level, report, filename):
    pages = _pdf_pages(pdf)
    paras = _paragraphs(pages)
    full = "\n".join(p for _, p in paras)
    if len(full) < 200:
        raise RuntimeError("This PDF has no readable text layer (it may be a scanned image). Scanned PDFs are not supported.")
    sections, truncated = _sections(paras)
    warnings = []
    if truncated:
        warnings.append(f"Only the first {sections[-1]['pages'][1]} of {len(pages)} pages were used, to keep generation fast on this machine.")
    vocab = _vocab(full)
    use_llm = True
    rng = random.Random(hashlib.sha1(full.encode()).hexdigest())
    done = 0

    def one(sec):
        nonlocal done, use_llm
        sentences = _sentences(sec["text"])
        raw = {}
        if use_llm:
            try:
                raw = _ollama_json([{"role": "system", "content": SECTION_SYSTEM},
                                    {"role": "user", "content": _section_prompt(sec["text"], level)}])
            except Exception as exc:          # offline or invalid JSON: build this section extractively
                log.warning("section: local LLM unavailable (%s); using extractive notes", exc)
                if isinstance(exc, requests.ConnectionError):
                    use_llm = False
                raw = {}
        cleaned = _clean_section(raw if isinstance(raw, dict) else {}, sec, vocab, sentences)
        if raw:
            _verify_section(cleaned, sec, sentences)
        if not cleaned["terms"]:
            cleaned["terms"] = _extract_terms(sec["text"])[:4]
        cleaned["pages"] = sec["pages"]
        cleaned["llm"] = bool(raw)
        done += 1
        report("understanding", done / len(sections))
        return cleaned

    report("understanding", 0)
    # Ollama queues or batches concurrent requests; on a CPU batching several sections raises throughput
    with ThreadPoolExecutor(max_workers=3) as pool:
        out_sections = list(pool.map(one, sections))
    if not any(s["llm"] for s in out_sections):
        warnings.append("Local LLM unavailable: notes and narration are taken directly from the PDF text.")
    elif not all(s["llm"] for s in out_sections):
        warnings.append("Some notes were taken directly from the PDF text because the local LLM was unavailable or unsure.")
    use_llm = any(s["llm"] for s in out_sections)
    report("understanding", 1)

    all_terms = list({t["term"].casefold(): t for s in out_sections for t in s["terms"]}.values())
    questions = [dict(q, pages=s["pages"]) for s in out_sections for q in s["questions"]]
    all_sentences = [x for sec in sections for x in _sentences(sec["text"]) if re.search(r"[.!?।]$", x)]
    if len(questions) < 6:
        questions += [dict(q, pages=None) for q in _cloze_questions(all_terms, all_sentences, rng)][:8 - len(questions)]
    title = _pdf_title(pdf) or next((p for _, p in paras[:8] if _is_heading(p) and len(p) > 6), "") or Path(filename).stem
    summary = _summary(title, out_sections, vocab, use_llm)
    flashcards = [{"front": t["term"], "back": t["definition"]} for t in all_terms]
    used = set()
    for s in out_sections:            # cloze cards: a revision point with one of its key terms blanked out
        for p in s["points"]:
            term = next((t["term"] for t in all_terms if t["term"] not in used
                         and re.search(rf"\b{re.escape(t['term'])}\b", p, re.I)), None)
            if term and len(flashcards) < 24:
                used.add(term)
                flashcards.append({"front": re.sub(rf"\b{re.escape(term)}\b", "_____", p, flags=re.I), "back": term})
    return {"title": _short(title, 120), "filename": filename, "pages": len(pages), "level": level,
            "summary": summary, "sections": out_sections, "concepts": all_terms, "flashcards": flashcards,
            "questions": questions, "warnings": warnings, "llm": use_llm}


def _summary(title, sections, vocab, use_llm):
    if use_llm:
        try:
            notes = "\n".join(f"- {p}" for s in sections for p in s["points"])
            raw = _ollama_json([{"role": "system", "content": SECTION_SYSTEM}, {"role": "user", "content":
                f"Title: {title}\nStudy notes:\n{notes}\nReturn JSON {{\"summary\": \"3-5 sentence overview using only these notes\"}}"}], 500)
            summary = str(raw.get("summary", "")).strip()
            if len(summary) > 60 and all(_grounded(s, vocab, 0.45) for s in _sentences(summary) or [summary]):
                return summary
        except Exception as exc:
            log.warning("summary: %s", exc)
    return " ".join(s["narration"].split(". ")[0].rstrip(".") + "." for s in sections[:4])


# ---------- the animated lesson ----------
def _lesson_plan(pack, length):
    """Scenes come straight from the verified notes (no extra LLM call). Narration is the first sentences of each
    audiobook chapter, split exactly as the speech engine splits them, so the video reuses the recorded sentences."""
    title, sections = pack["title"], pack["sections"][:5]
    budget = max(1, length * 14 // (len(sections) + 1))           # characters of narration per scene

    def opening(text):
        chosen = []
        for sentence in tts._sentences(text):
            if chosen and sum(map(len, chosen)) + len(sentence) > budget:
                break
            chosen.append(sentence)
        return _short(" ".join(chosen), 690)

    scenes = [{"type": "title_card", "heading": _short(title, 110), "narration": opening(pack["summary"])}]
    for s in sections:
        items = [_short(t["term"], 40) for t in s["terms"]][:5]
        if len(items) < 2:      # terms defined elsewhere in the document that this section talks about
            text = " ".join([s["narration"]] + s["points"]).casefold()
            items = list(dict.fromkeys(items + [_short(t["term"], 40) for t in pack["concepts"] if t["term"].casefold() in text]))[:5]
        scene = {"heading": _short(s["heading"], 110), "narration": opening(s["narration"])}
        scenes.append({**scene, "type": "diagram", "diagram_kind": "flow", "items": items} if len(items) >= 2
                      else {**scene, "type": "title_card"})
    if not any(sc["type"] == "diagram" for sc in scenes):
        heads = [_short(s["heading"], 40) for s in sections]
        scenes.insert(1, {"type": "diagram", "diagram_kind": "flow", "heading": _short(title, 110),
                          "narration": opening(pack["summary"]), "items": heads if len(heads) >= 2 else heads + [_short(title, 40)]})
    while len(scenes) < 3:
        scenes.append({"type": "title_card", "heading": _short(title, 110), "narration": opening(pack["summary"])})
    return Plan.model_validate({"title": title, "level": pack["level"], "scenes": scenes[:6]}).model_dump()


# ---------- localized pack ----------
def _translated(study, lang, report):
    if lang == "en":
        return copy.deepcopy(study)
    pack = copy.deepcopy(study)
    refs = [(pack, "title"), (pack, "summary")]
    for s in pack["sections"]:
        refs += [(s, "heading"), (s, "narration")] + [(s["points"], j) for j in range(len(s["points"]))]
        for t in s["terms"]:
            t["term_en"] = t["term"]
            refs += [(t, "term"), (t, "definition")]
    for t in pack["concepts"]:
        t["term_en"] = t["term"]
        refs += [(t, "term"), (t, "definition")]
    for c in pack["flashcards"]:
        refs += [(c, "front"), (c, "back")]
    for q in pack["questions"]:
        refs += [(q, "question"), (q, "evidence")] + [(q["options"], j) for j in range(len(q["options"]))]
    refs = list({(id(c), k): (c, k) for c, k in refs}.values())     # terms are shared by sections and concepts
    unique = list(dict.fromkeys(c[k] for c, k in refs if c[k]))
    done = translate.translate(unique, lang, progress=lambda f: report("translating", f), warnings=pack["warnings"])
    mapping = dict(zip(unique, done))
    for c, k in refs:
        if c[k]:
            c[k] = mapping[c[k]]
    return pack


def _audiobook(i, level, lang, pack, voice_id, report, save):
    """Chapters are saved as soon as each one is recorded, so Listen mode can start before the rest are done."""
    out_dir = _dir(i) / level / lang
    voice = tts.resolve_voice(lang, voice_id)
    chapters = [{"heading": pack["title"], "text": pack["summary"]}] + [
        {"heading": s["heading"], "text": s["narration"], "pages": s["pages"]} for s in pack["sections"]]
    book = {"voice_id": voice["id"], "voice_name": voice["name"], "engine": voice["engine"], "chapters": chapters, "file": None}
    for c in chapters:
        c.update(audio=None, seconds=tts.estimate_seconds(c["text"]), ready=voice["engine"] == "browser")
    pack["audiobook"] = book
    if voice["engine"] == "browser":
        return
    save()
    files = [out_dir / f"chapter{k}.wav" for k in range(len(chapters))]
    stamp = int(time.time())

    def ready(k):
        chapters[k].update(audio=f"/data/docs/{i}/{level}/{lang}/{files[k].name}?v={stamp}",
                           seconds=round(tts.wav_seconds(files[k]), 2), ready=True)
        pack["progress"]["audio"] = (k + 1) / len(chapters)
        report("narrating", (k + 1) / len(chapters))
        save()

    tts.speak_many([c["text"] for c in chapters], lang, files, voice["id"], on_done=ready)
    full = out_dir / "audiobook.m4a"
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for f in files:
        cmd += ["-i", str(f)]
    fc = ";".join(f"[{k}:a]aresample=24000,aformat=channel_layouts=mono,apad=pad_dur=0.8[a{k}]" for k in range(len(files)))
    fc += ";" + "".join(f"[a{k}]" for k in range(len(files))) + f"concat=n={len(files)}:v=0:a=1[a]"
    subprocess.run(cmd + ["-filter_complex", fc, "-map", "[a]", "-c:a", "aac", "-b:a", "96k", str(full)], check=True)
    book["file"] = f"/data/docs/{i}/{level}/{lang}/audiobook.m4a?v={stamp}"


def _video(i, level, lang, study, pack, length, voice_id, report, save):
    lesson_id = f"{i}-{level[0]}"
    lp = DATA / "lessons" / lesson_id / "plan.json"
    rec = json.loads(lp.read_text(encoding="utf-8")) if lp.exists() else {}
    if rec.get("length") != length or rec.get("source") != "document":
        rec = {"id": lesson_id, "topic": study["title"], "length": length, "source": "document", "warnings": [],
               "plan": _lesson_plan(study, length), "plan_seconds": 0, "document": i, "localized": {}}
    if lang != "en":
        rec["localized"][lang] = _lesson_plan(pack, length)    # written from the translated notes: no re-translation
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    pack["video"] = {"lesson_id": lesson_id, "ready": False}
    save()

    def progress(stage, value=None):
        if stage == "narrating" and value is not None:
            pack["progress"]["video"] = value
        report("animating", value if stage == "narrating" else None)

    pipeline.localize(lesson_id, lang, voice_id, progress)
    pack["video"]["ready"] = True


def build(i, level, lang, formats, voice_id, length, report, pdf_bytes=None, filename="document.pdf"):
    """Create or extend the study pack for (document, level, language). `formats` ⊆ {"video", "audio"};
    exam material is always included. Finished parts are reused."""
    d = _dir(i)
    pdf = d / "source.pdf"
    if pdf_bytes is not None and not pdf.exists():
        d.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(pdf_bytes)
    if not pdf.exists():
        raise RuntimeError("unknown document")
    meta_path = d / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {"filename": filename}
    study_path = d / f"study-{level}.json"
    if study_path.exists():
        study = json.loads(study_path.read_text(encoding="utf-8"))
    else:
        report("reading")
        study = _study(pdf, level, report, meta["filename"])
        study_path.write_text(json.dumps(study, ensure_ascii=False, indent=1), encoding="utf-8")
    meta.update(title=study["title"], pages=study["pages"])
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    pack_path = d / level / lang / "pack.json"
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    if pack_path.exists():
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
    else:
        report("translating", 0)
        pack = _translated(study, lang, report)
        pack.update(id=i, language=lang, audiobook=None, video=None)
    ACTIVE.add((i, level, lang))

    def save():
        pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")

    book = pack.get("audiobook")
    want_audio = "audio" in formats and (not book or (voice_id and book["voice_id"] != voice_id)
                                         or not all(c.get("ready", True) for c in book["chapters"]))
    want_video = "video" in formats and not (pack.get("video") or {}).get("ready", bool(pack.get("video")))
    pack["progress"] = {**({"audio": 0} if want_audio else {}), **({"video": 0} if want_video else {})}
    save()                       # exam material is ready now: the study page can open while media is recorded
    try:
        if want_audio:
            report("narrating", 0)
            _audiobook(i, level, lang, pack, voice_id, report, save)
            pack["progress"].pop("audio", None)
            save()
        if want_video:
            report("animating", 0)
            _video(i, level, lang, study, pack, length, voice_id, report, save)
    finally:
        pack["progress"] = {}
        save()
        ACTIVE.discard((i, level, lang))
    report("done")
    return {"id": i, "level": level, "language": lang}


def load(i, level, lang):
    p = _dir(i) / level / lang / "pack.json"
    if not p.exists():
        return None
    pack = json.loads(p.read_text(encoding="utf-8"))
    if pack.get("progress") and (i, level, lang) not in ACTIVE:     # the server stopped mid-build
        pack["progress"], pack["interrupted"] = {}, True
    return pack


def history():
    root = DATA / "docs"
    out = []
    for d in root.iterdir() if root.is_dir() else []:
        for pack in d.glob("*/*/pack.json"):
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").exists() else {}
            out.append({"id": d.name, "level": pack.parent.parent.name, "language": pack.parent.name,
                        "title": meta.get("title") or d.name, "filename": meta.get("filename", ""),
                        "updated_at": pack.stat().st_mtime})
    return sorted(out, key=lambda e: -e["updated_at"])[:30]
