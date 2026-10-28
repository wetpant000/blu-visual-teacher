import { useEffect, useMemo, useRef, useState } from "react";
import { OUTDATED, getConfig, getDocuments, getHistory, startDocument, startGenerate, waitJob } from "./api";
import { Presenter, Stage } from "./Scenes";
import { demoLesson } from "./demo";
import { useBrowserVoices, voiceFor } from "./speech";

const TOPICS = ["How does a Python loop work?", "What is photosynthesis?", "Newton's second law of motion", "How does binary search work?", "Balancing a chemical equation", "What is a variable in code?"];
const FALLBACK_LANGS = { en: "English", hi: "हिन्दी", kn: "ಕನ್ನಡ", ta: "தமிழ்", te: "తెలుగు", bn: "বাংলা", mr: "मराठी" };
const STAGES = [
  ["planning", "Planning the lesson", "A small model on this laptop drafts the scenes"],
  ["translating", "Translating", "Captions and narration in your script"],
  ["narrating", "Recording the voice-over", "Each scene lasts as long as its audio"],
  ["done", "Ready", "Opening your lesson"],
];
const DOC_STAGES = [
  ["reading", "Reading the PDF", "Extracting the text page by page"],
  ["understanding", "Understanding each section", "Notes, terms and questions, checked against the text"],
  ["translating", "Translating", "Into your chosen language and script"],
  ["narrating", "Recording the audiobook", "One chapter per section"],
  ["planning", "Planning the animation", "Scenes built only from the verified notes"],
  ["animating", "Animating the video", "Presenter, diagrams and voice-over"],
  ["done", "Ready", "Opening your study pack"],
];
const OUTPUTS = [["video,audio", "Video + audiobook"], ["video", "Animated video"], ["audio", "Audiobook"]];
const STEPS = [
  ["1", "Ask anything", "Type a concept, pick a level and how long the lesson should be."],
  ["2", "Blu plans & narrates", "A local model writes the scenes, translates them and records the voice-over."],
  ["3", "Watch, quiz, export", "Follow the animated presenter, take a quick quiz and export a 720p MP4."],
];
const reducedMotion = () => typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches;

// seconds since mount, updated every animation frame (frozen when the user prefers reduced motion)
function useClock(frozenAt = 6) {
  const [t, setT] = useState(reducedMotion() ? frozenAt : 0);
  useEffect(() => {
    if (reducedMotion()) return;
    let raf, start = performance.now();
    const tick = (now) => { setT(Math.max(0, now - start) / 1000); raf = requestAnimationFrame(tick); };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return t;
}

function LivePreview({ lang, name }) {
  const clock = useClock(7.4), box = useRef(null);
  const [scale, setScale] = useState(0.4);
  useEffect(() => {
    const ro = new ResizeObserver(([e]) => setScale(e.contentRect.width / 1280));
    ro.observe(box.current);
    return () => ro.disconnect();
  }, []);
  const total = demoLesson(lang).scenes.reduce((a, s) => a + s.duration, 0);
  const round = Math.floor(clock / total);
  const lesson = useMemo(() => demoLesson(lang, round), [lang, round]);
  return (
    <figure className="preview">
      <div className="preview-frame" ref={box} style={{ height: 720 * scale }}>
        <div style={{ transform: `scale(${scale})`, transformOrigin: "0 0", width: 1280, height: 720 }}>
          <Stage lesson={lesson} t={clock % total} />
        </div>
      </div>
      <figcaption><span className="live-dot" />Live preview · {name} · a new presenter every loop</figcaption>
    </figure>
  );
}

function BuildingOverlay({ stage, progress, browserVoice, stages = STAGES, title = "Building your lesson" }) {
  const t = useClock(1);
  const cur = stages.findIndex((s) => s[0] === stage);
  return (
    <div className="overlay" role="status" aria-live="polite">
      <div className="gen">
        <div className="gen-presenter">
          <Presenter t={t} speaking open={clamp01(0.4 + 0.4 * Math.sin(t * 12) * Math.sin(t * 3))} point={95 + Math.sin(t * 1.5) * 20} wave={1} />
        </div>
        <div>
          <h2>{title}</h2>
          <div className="gen-bar"><i style={{ width: `${((Math.max(cur, 0) + (progress > 0 ? progress : 0.5)) / stages.length) * 100}%` }} /></div>
          <ol>
            {stages.map(([k, name, hint], me) => (
              <li key={k} className={me < cur ? "past" : me === cur ? "now" : ""}>
                <b>{name}{me === cur && progress > 0 ? ` · ${Math.round(progress * 100)}%` : ""}</b>
                <span>{k === "narrating" && browserVoice ? "Your browser speaks the lesson, so nothing to record" : hint}</span>
              </li>
            ))}
          </ol>
          <p className="tiny">Everything runs on this machine. No internet needed.</p>
        </div>
      </div>
    </div>
  );
}
const clamp01 = (x) => Math.min(1, Math.max(0, x));

export default function Home() {
  const [cfg, setCfg] = useState(null);
  const [topic, setTopic] = useState("");
  const [level, setLevel] = useState("beginner");
  const [lang, setLang] = useState(() => localStorage.getItem("blu.language") || "hi");
  const [voice, setVoice] = useState("");
  const [length, setLength] = useState(60);
  const [stage, setStage] = useState(null);
  const [progress, setProgress] = useState(0);
  const [err, setErr] = useState("");
  const [history, setHistory] = useState([]);
  const [historyError, setHistoryError] = useState("");
  const [showAll, setShowAll] = useState(false);
  const [source, setSource] = useState("topic");
  const [file, setFile] = useState(null);
  const [outputs, setOutputs] = useState("video,audio");
  const [dragging, setDragging] = useState(false);
  const [docs, setDocs] = useState([]);
  const fileInput = useRef(null);
  useEffect(() => { getDocuments().then(setDocs).catch(() => {}); }, []);
  const pickFile = (f) => {
    if (!f) return;
    if (f.type !== "application/pdf" && !/\.pdf$/i.test(f.name)) { setErr("Please choose a PDF file."); return; }
    if (f.size > 40 * 1024 * 1024) { setErr("The PDF is larger than 40 MB."); return; }
    setErr(""); setFile(f); setSource("pdf");
  };
  useEffect(() => { getConfig().then(setCfg).catch(() => setCfg(false)); }, []);
  useEffect(() => {
    getHistory().then((items) => setHistory(items.filter((item) => !item.id.startsWith("pdf-")))).catch(() => setHistoryError("Could not load recent lessons."));
  }, []);
  const langs = cfg?.languages || FALLBACK_LANGS;
  const voices = cfg?.voices?.[lang] || [];
  const browserVoices = useBrowserVoices();
  const browserVoice = voiceFor(lang, browserVoices);
  const topicCount = new Set(history.map((item) => item.id)).size;
  const languageCount = new Set(history.map((item) => item.language)).size;
  useEffect(() => { localStorage.setItem("blu.language", lang); }, [lang]);
  useEffect(() => {
    if (!cfg || cfg === false || localStorage.getItem("blu.parler-defaults-migrated")) return;
    for (const [language, options] of Object.entries(cfg.voices || {})) {
      const parlerVoice = options.find((item) => item.id.startsWith("parler-"));
      const saved = localStorage.getItem(`blu.voice.${language}`);
      if (parlerVoice && saved && !saved.startsWith("parler-")) {
        localStorage.setItem(`blu.voice.${language}`, parlerVoice.id);
      }
    }
    localStorage.setItem("blu.parler-defaults-migrated", "true");
  }, [cfg]);
  useEffect(() => {
    if (!voices.length) return;
    // the first configured voice is the default (AI4Bharat for Indian languages, Kokoro for English and Hindi)
    const usable = (id) => voices.some((item) => item.id === id) && (id !== "browser" || !!browserVoice);
    const saved = localStorage.getItem(`blu.voice.${lang}`);
    const chosen = localStorage.getItem(`blu.voice-picked.${lang}`) && usable(saved) ? saved
      : voices.find((item) => usable(item.id))?.id;
    setVoice(chosen || "");
  }, [lang, cfg, !!browserVoice]);

  async function go(e) {
    e.preventDefault();
    if (source === "pdf") return makeStudyPack();
    if (topic.trim().length < 2) { setErr("Type a topic first, for example “How does a Python loop work?”"); return; }
    setErr(""); setStage("planning"); setProgress(0);
    try {
      const { job, lesson } = await startGenerate({ topic: topic.trim(), level, language: lang, length, voice });
      // the player opens as soon as the first scene has its voice-over; the rest streams in while it plays
      const lessonId = await new Promise((resolve, reject) => waitJob(job, (s) => {
        setStage(s.stage === "queued" ? "planning" : s.stage); setProgress(s.progress || 0);
        if (s.stage === "narrating" && s.progress > 0 && lesson) resolve(lesson);
      }).then((res) => resolve(res.id), reject));
      location.hash = `#/watch/${lessonId}/${lang}`;
    } catch (ex) {
      setStage(null);
      setErr(cfg === false ? "The backend is not running. Start it with ./scripts/run.sh" : String(ex.message || ex));
    }
  }

  async function makeStudyPack() {
    if (!file) { setErr("Drop a PDF first, or click the box to choose one."); return; }
    if (cfg && !cfg.features?.includes("documents")) { setErr(OUTDATED); return; }
    setErr(""); setStage("reading"); setProgress(0);
    try {
      const { job, id } = await startDocument(file, { language: lang, level, formats: outputs, voice, length });
      // open the study pack as soon as its notes exist; the audiobook and video keep recording in the background
      const open = () => { location.hash = `#/study/${id}/${level}/${lang}`; };
      await new Promise((resolve, reject) => waitJob(job, (s) => {
        setStage(s.stage === "queued" ? "reading" : s.stage); setProgress(s.progress || 0);
        if (["narrating", "animating", "planning"].includes(s.stage)) resolve();
      }).then(resolve, reject));
      open();
    } catch (ex) {
      setStage(null);
      setErr(cfg === false ? "The backend is not running. Start it with ./scripts/run.sh" : String(ex.message || ex));
    }
  }

  const recent = showAll ? history : history.slice(0, 6);

  return (
    <div className="home" onDragOver={(e) => { if ([...e.dataTransfer.types].includes("Files")) { e.preventDefault(); setDragging(true); } }}
      onDragLeave={(e) => { if (!e.relatedTarget) setDragging(false); }}
      onDrop={(e) => { e.preventDefault(); setDragging(false); pickFile(e.dataTransfer.files[0]); }}>
      {dragging && <div className="drop-veil" aria-hidden="true"><div><b>Drop your PDF</b><span>We’ll turn it into a video, an audiobook and exam notes</span></div></div>}
      <div className="home-glow" aria-hidden="true" />
      <header className="nav">
        <span className="logo"><svg viewBox="0 0 32 32" width="28" height="28"><circle cx="16" cy="16" r="13" /><path d="M12 10 L23 16 L12 22 Z" /></svg>Blu Visual Teacher</span>
        <nav className="nav-links" aria-label="Sections">
          <a href="#create" onClick={(e) => { e.preventDefault(); document.getElementById("create")?.scrollIntoView({ behavior: "smooth" }); }}>Create</a>
          <a href="#recent" onClick={(e) => { e.preventDefault(); document.getElementById("recent")?.scrollIntoView({ behavior: "smooth" }); }}>Recent</a>
          <a href="#how" onClick={(e) => { e.preventDefault(); document.getElementById("how")?.scrollIntoView({ behavior: "smooth" }); }}>How it works</a>
        </nav>
        <span className={`status ${cfg?.ollama ? "ok" : ""}`}>
          <i />{cfg === null ? "checking…" : cfg?.ollama ? `Local model ready · ${cfg.model}` : "Local model offline · sample lesson will play"}
        </span>
      </header>

      <main className="home-main">
        <section className="hero" id="create" aria-labelledby="hero-title">
          <div className="hero-copy">
            <p className="eyebrow">OFFLINE · NARRATED · ANIMATED</p>
            <h1 id="hero-title">Type a concept.<br /><span className="grad">Watch it come alive.</span></h1>
            <p className="hero-intro">An animated presenter explains any idea step by step, with voice-over and captions in {Object.keys(langs).length} languages. Or drop in a PDF to get an animated video, an audiobook and exam notes.</p>

            <form className="card create" onSubmit={go}>
              <div className="source-switch" role="tablist" aria-label="Learn from">
                <button type="button" role="tab" aria-selected={source === "topic"} className={source === "topic" ? "on" : ""} onClick={() => setSource("topic")}>Type a topic</button>
                <button type="button" role="tab" aria-selected={source === "pdf"} className={source === "pdf" ? "on" : ""} onClick={() => setSource("pdf")}>Upload a PDF</button>
              </div>
              {source === "topic" ? (<>
                <label className="lab" htmlFor="topic">What should we teach?</label>
                <div className="topic-row">
                  <input id="topic" value={topic} onChange={(e) => setTopic(e.target.value)} placeholder={TOPICS[0]} autoComplete="off" />
                </div>
                <div className="chips">
                  {TOPICS.map((t) => <button type="button" key={t} onClick={() => setTopic(t)} className={topic === t ? "on" : ""}>{t}</button>)}
                </div>
              </>) : (<>
                <span className="lab">Your document</span>
                <button type="button" className={`dropzone${file ? " has-file" : ""}`} onClick={() => fileInput.current?.click()}>
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 2h8l5 5v15H6z" /><path d="M14 2v5h5" /><path d="M12 11v6M9 14l3-3 3 3" /></svg>
                  {file ? <span><b>{file.name}</b><small>{(file.size / 1024 / 1024).toFixed(1)} MB · click to choose another</small></span>
                    : <span><b>Drag & drop a PDF here</b><small>or click to browse · text PDFs up to 40 MB</small></span>}
                </button>
                <input ref={fileInput} type="file" accept="application/pdf,.pdf" hidden onChange={(e) => pickFile(e.target.files[0])} />
                <span className="lab" id="out-label">Output</span>
                <div className="seg" role="radiogroup" aria-labelledby="out-label">
                  {OUTPUTS.map(([k, name]) => <button type="button" role="radio" aria-checked={outputs === k} key={k} className={outputs === k ? "on" : ""} onClick={() => setOutputs(k)}>{name}</button>)}
                </div>
                {cfg && !cfg.features?.includes("documents") && <p className="err" role="alert">{OUTDATED}</p>}
                <p className="voice-note">Every pack also includes Exam mode: key concepts, revision notes, flashcards and practice questions, all checked against your PDF.</p>
              </>)}

              <span className="lab" id="lang-label">Language</span>
              <div className="lang-pills" role="radiogroup" aria-labelledby="lang-label">
                {Object.entries(langs).map(([c, n]) => (
                  <button type="button" role="radio" aria-checked={lang === c} key={c} lang={c} className={lang === c ? "on" : ""} onClick={() => setLang(c)}>{n}</button>
                ))}
              </div>

              <div className="row">
                <div>
                  <span className="lab">Level</span>
                  <div className="seg">
                    {["beginner", "intermediate"].map((l) => <button type="button" key={l} className={level === l ? "on" : ""} onClick={() => setLevel(l)}>{l === "beginner" ? "Beginner" : "Intermediate"}</button>)}
                  </div>
                </div>
                <div hidden={source === "pdf" && outputs === "audio"}>
                  <span className="lab">{source === "pdf" ? "Video length" : "Length"} <output>{length}s</output></span>
                  <input type="range" min="20" max="180" step="5" value={length} onChange={(e) => setLength(+e.target.value)} aria-label="Target length in seconds" />
                </div>
              </div>

              <label className="lab" htmlFor="voice">Voice</label>
              <select id="voice" className="choice" value={voice} onChange={(e) => {
                setVoice(e.target.value);
                localStorage.setItem(`blu.voice.${lang}`, e.target.value);
                localStorage.setItem(`blu.voice-picked.${lang}`, "1");
              }} disabled={!voices.length}>
                {voices.map((item) => item.id === "browser"
                  ? <option key="browser" value="browser" disabled={!browserVoice}>
                    {browserVoice ? `${item.name} · ${browserVoice.name}` : "Browser voice · not available in this browser"}
                  </option>
                  : <option key={item.id} value={item.id}>{item.name} · {langs[lang]}</option>)}
              </select>

              <p className="voice-note">{voice === "browser"
                ? "Fastest: your browser speaks the lesson. MP4 export records a local voice when needed."
                : voice.startsWith("parler-") ? "AI4Bharat voice, made for this language. Recorded on this machine."
                : "Recorded on this machine before the lesson opens."}</p>
              {err && <p className="err" role="alert">{err}</p>}
              <button className="cta" type="submit">{source === "pdf" ? "Make my study pack" : "Make my lesson"} <span aria-hidden="true">→</span></button>
            </form>
          </div>

          <div className="hero-side">
            <LivePreview lang={lang} name={langs[lang] || lang} />
            <div className="stats" aria-label="Learning activity">
              <div><b>{topicCount}</b><span>Lessons explored</span></div>
              <div><b>{history.length}</b><span>Saved versions</span></div>
              <div><b>{languageCount}</b><span>Languages used</span></div>
            </div>
          </div>
        </section>

        <section className="recent" id="recent" aria-labelledby="recent-title">
          <div className="section-head">
            <div><p className="eyebrow">PICK UP WHERE YOU LEFT OFF</p><h2 id="recent-title">Recent lessons</h2></div>
            {history.length > 6 && <button type="button" className="ghost" onClick={() => setShowAll((v) => !v)}>{showAll ? "Show fewer" : `Show all ${history.length}`}</button>}
          </div>
          {docs.length > 0 && (
            <ul className="lesson-grid packs">
              {docs.slice(0, 6).map((d) => (
                <li key={`${d.id}-${d.level}-${d.language}`}>
                  <a href={`#/study/${d.id}/${d.level}/${d.language}`}>
                    <span className="lesson-thumb pdf-thumb" aria-hidden="true"><span>PDF</span></span>
                    <span className="lesson-text">
                      <span className="lesson-title">{d.title}</span>
                      <span className="lesson-topic">{d.filename}</span>
                      <span className="lesson-meta"><em lang={d.language}>{langs[d.language] || d.language}</em>Study pack · {d.level}</span>
                    </span>
                  </a>
                </li>
              ))}
            </ul>
          )}
          {historyError ? <p className="history-empty" role="status">{historyError}</p>
            : history.length ? (
              <ul className="lesson-grid">
                {recent.map((item, k) => (
                  <li key={`${item.id}-${item.language}`} style={{ "--hue": (parseInt(item.id.slice(0, 3), 16) || k * 40) % 360 }}>
                    <a href={`#/watch/${item.id}/${item.language}`}>
                      <span className="lesson-thumb" aria-hidden="true"><span>▶</span></span>
                      <span className="lesson-text">
                        <span className="lesson-title" lang={item.language}>{item.title || item.topic}</span>
                        {item.title && item.title !== item.topic && <span className="lesson-topic">{item.topic}</span>}
                        <span className="lesson-meta">
                          <em lang={item.language}>{langs[item.language] || item.language}</em>
                          {item.updated_at && new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(item.updated_at))}
                        </span>
                      </span>
                    </a>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="history-empty">Your lessons will appear here after you make one.</p>
            )}
        </section>

        <section className="how" id="how" aria-labelledby="how-title">
          <p className="eyebrow">HOW IT WORKS</p>
          <h2 id="how-title">From question to lesson in three steps</h2>
          <ol className="steps">
            {STEPS.map(([n, title, text]) => <li key={n}><span className="step-n">{n}</span><b>{title}</b><span>{text}</span></li>)}
          </ol>
        </section>
      </main>
      <footer className="foot">Runs fully offline · Planner, translation and voices on your machine</footer>

      {stage && <BuildingOverlay stage={stage} progress={progress} browserVoice={voice === "browser"}
        stages={source === "pdf" ? DOC_STAGES : STAGES} title={source === "pdf" ? "Building your study pack" : "Building your lesson"} />}
    </div>
  );
}
