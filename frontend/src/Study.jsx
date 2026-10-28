// Study page for an uploaded PDF: Listen mode (audiobook), Visual mode (animated lesson), Exam mode (concepts,
// revision notes, flashcards, practice questions). Everything shown comes from the document's study pack.
import { useEffect, useMemo, useRef, useState } from "react";
import Watch from "./Watch";
import { extendDocument, getConfig, getDocument, waitJob } from "./api";
import { sentences, speak, stopSpeaking, useBrowserVoices, voiceFor } from "./speech";

const MODES = [
  ["listen", "Listen", "Audiobook of the whole document"],
  ["visual", "Visual", "Animated lesson with a presenter"],
  ["exam", "Exam", "Concepts, notes, flashcards, practice"],
];
const STAGE_NAMES = {
  queued: "Waiting", reading: "Reading the PDF", understanding: "Understanding each section", translating: "Translating",
  narrating: "Recording the audiobook", planning: "Planning the animation", animating: "Animating the video", done: "Ready",
};
const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const pages = (p) => (p ? (p[0] === p[1] ? `p. ${p[0]}` : `pp. ${p[0]}–${p[1]}`) : "");
const store = {
  get: (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};
function download(name, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
const seeded = (seed) => () => { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; };
const shuffle = (items, rnd) => { const r = [...items]; for (let i = r.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [r[i], r[j]] = [r[j], r[i]]; } return r; };

// ---------- Listen mode ----------
function Audiobook({ pack, onCreate, busy }) {
  const book = pack.audiobook;
  const browserVoices = useBrowserVoices();
  const browserVoice = voiceFor(pack.language, browserVoices);
  const [chapter, setChapter] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [chars, setChars] = useState(0);
  const [rate, setRate] = useState(1);
  const audio = useRef(null);
  const browser = book?.engine === "browser";
  const ch = book?.chapters[chapter];
  const total = book ? book.chapters.reduce((a, c) => a + c.seconds, 0) : 0;
  const before = book ? book.chapters.slice(0, chapter).reduce((a, c) => a + c.seconds, 0) : 0;

  const go = (k, autoplay = playing) => { setChapter(k); setTime(0); setChars(0); setPlaying(autoplay); };
  const waiting = !browser && ch && !ch.audio;          // this chapter is still being recorded
  const next = () => (chapter < book.chapters.length - 1 ? go(chapter + 1, true) : setPlaying(false));

  useEffect(() => {                                    // recorded audiobook
    const a = audio.current;
    if (!a || browser || !ch) return;
    if (!ch.audio) { a.pause(); return; }               // resumes automatically once the chapter is recorded
    if (a.dataset.src !== ch.audio) { a.dataset.src = ch.audio; a.src = ch.audio; }
    a.playbackRate = rate;
    if (playing) a.play().catch(() => setPlaying(false)); else a.pause();
  }, [chapter, playing, rate, browser, ch?.audio]);
  useEffect(() => {                                    // browser voice
    if (!browser || !ch) return;
    if (!playing || !browserVoice) { stopSpeaking(); return; }
    const offset = chars > 0 ? ch.text.lastIndexOf(" ", chars) + 1 : 0;
    return speak(ch.text, { lang: pack.language, voice: browserVoice, rate, offset, onProgress: setChars, onEnd: next });
  }, [chapter, playing, rate, browser, browserVoice]);
  useEffect(() => () => stopSpeaking(), []);

  if (!book) return (
    <div className="empty-mode">
      <h3>No audiobook yet</h3>
      <p>Create a narrated audiobook of this document in {pack.languageName}.</p>
      <button className="cta small" onClick={() => onCreate("audio")} disabled={busy}>Create the audiobook</button>
    </div>
  );
  const position = browser ? chars / Math.max(1, ch.text.length) : time / Math.max(0.1, ch.seconds);
  const parts = sentences(ch.text);
  const spoken = position * ch.text.length;
  const current = parts.findIndex((s) => spoken < s.at + s.text.length);
  const elapsed = before + position * ch.seconds;
  return (
    <div className="listen">
      <div className="player-card">
        <div className={`wave${playing ? " on" : ""}`} aria-hidden="true">{Array.from({ length: 28 }, (_, k) => <i key={k} style={{ animationDelay: `${(k * 137) % 900}ms` }} />)}</div>
        <p className="eyebrow">CHAPTER {chapter + 1} OF {book.chapters.length}{ch.pages ? ` · ${pages(ch.pages)}` : ""}</p>
        <h2 lang={pack.language}>{ch.heading}</h2>
        <div className="book-progress" onClick={(e) => {
          if (browser) return;
          const f = (e.clientX - e.currentTarget.getBoundingClientRect().left) / e.currentTarget.clientWidth;
          if (audio.current) audio.current.currentTime = f * ch.seconds;
        }}><i style={{ width: `${Math.min(1, position) * 100}%` }} /></div>
        <div className="book-time"><span>{fmt(elapsed)}</span><span>{waiting ? "Recording this chapter…" : ""}</span><span>{fmt(total)}</span></div>
        <div className="book-controls">
          <button onClick={() => go(Math.max(0, chapter - 1))} aria-label="Previous chapter" disabled={chapter === 0}>⏮</button>
          <button className="play" onClick={() => setPlaying((p) => !p)} aria-label={playing ? "Pause" : "Play"} disabled={browser && !browserVoice}>{playing ? "❚❚" : "▶"}</button>
          <button onClick={() => go(Math.min(book.chapters.length - 1, chapter + 1))} aria-label="Next chapter" disabled={chapter === book.chapters.length - 1}>⏭</button>
          <select value={rate} onChange={(e) => setRate(+e.target.value)} aria-label="Speed">
            {[0.75, 1, 1.25, 1.5, 1.75].map((r) => <option key={r} value={r}>{r}×</option>)}
          </select>
        </div>
        {browser && !browserVoice && <p className="err">This browser has no {pack.languageName} voice. <button className="link" onClick={() => onCreate("audio", true)}>Record the audiobook on this machine</button></p>}
        <div className="book-actions">
          {book.file && <a href={book.file} download={`${pack.title}.m4a`}>Download audiobook (.m4a)</a>}
          <button className="link" onClick={() => download(`${pack.title}.txt`, book.chapters.map((c) => `${c.heading}\n\n${c.text}`).join("\n\n\n"))}>Download transcript</button>
          <span className="tiny">Voice: {browser ? `browser · ${browserVoice?.name || "none"}` : book.voice_name}</span>
        </div>
      </div>
      <div className="transcript" lang={pack.language}>
        {parts.map((s, k) => <span key={k} className={k === current ? "now" : k < current ? "said" : ""}>{s.text} </span>)}
      </div>
      <ol className="chapters">
        {book.chapters.map((c, k) => (
          <li key={k}><button className={k === chapter ? "on" : ""} onClick={() => go(k, true)}>
            <span className="ch-n">{k + 1}</span><span lang={pack.language}>{c.heading}</span>
            <span className="ch-t">{browser || c.audio ? fmt(c.seconds) : "recording…"}</span>
          </button></li>
        ))}
      </ol>
      <audio ref={audio} onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)} onEnded={next} preload="auto" />
    </div>
  );
}

// ---------- Exam mode ----------
function Flashcards({ cards, lang, id }) {
  const [deck, setDeck] = useState(() => cards.map((_, k) => k));
  const [flipped, setFlipped] = useState(false);
  const [known, setKnown] = useState(0);
  const card = cards[deck[0]];
  const answer = (gotIt) => {
    setFlipped(false);
    setDeck((d) => (gotIt ? d.slice(1) : [...d.slice(1), d[0]]));
    if (gotIt) setKnown((n) => n + 1);
  };
  const restart = () => { setDeck(shuffle(cards.map((_, k) => k), seeded(Date.now() % 1e6))); setKnown(0); setFlipped(false); };
  useEffect(() => {
    const key = (e) => {
      if (e.target.closest?.("input,select,textarea")) return;
      if (e.key === " ") { e.preventDefault(); setFlipped((f) => !f); }
      if (flipped && e.key === "ArrowRight") answer(true);
      if (flipped && e.key === "ArrowLeft") answer(false);
    };
    addEventListener("keydown", key);
    return () => removeEventListener("keydown", key);
  }, [flipped, deck]);
  if (!cards.length) return <p className="tiny">No flashcards could be made from this document.</p>;
  return (
    <div className="flash">
      <div className="flash-meta"><span>{deck.length} left · {known} mastered</span><button className="ghost" onClick={restart}>Shuffle & restart</button></div>
      {card ? (
        <>
          <button className={`flashcard${flipped ? " flipped" : ""}`} onClick={() => setFlipped((f) => !f)} lang={lang} key={`${id}-${deck[0]}-${deck.length}`}>
            <span className="face front"><small>Question · tap to flip</small>{card.front}</span>
            <span className="face back"><small>Answer</small>{card.back}</span>
          </button>
          <div className="flash-actions">
            <button className="again" onClick={() => answer(false)} disabled={!flipped}>← Review again</button>
            <button className="got" onClick={() => answer(true)} disabled={!flipped}>Got it →</button>
          </div>
          <p className="tiny center">Space to flip · ← / → to answer</p>
        </>
      ) : (
        <div className="empty-mode"><h3>Deck complete</h3><p>You mastered all {cards.length} cards.</p><button className="cta small" onClick={restart}>Go again</button></div>
      )}
    </div>
  );
}

function Practice({ questions, lang, id, level }) {
  const key = `blu.practice.${id}.${level}.${lang}`;
  const [round, setRound] = useState(() => +store.get(key, 0) || 0);
  const [answers, setAnswers] = useState({});
  const [checked, setChecked] = useState(false);
  const size = Math.min(5, questions.length);
  // each round takes the next questions from a fixed shuffled order, with options reshuffled per round
  const set = useMemo(() => {
    const order = shuffle(questions.map((_, k) => k), seeded(7));
    const rnd = seeded(round + 11);
    return Array.from({ length: size }, (_, k) => {
      const q = questions[order[(round * size + k) % questions.length]];
      const opts = shuffle(q.options.map((text, j) => ({ text, correct: j === q.answer })), rnd);
      return { ...q, opts };
    });
  }, [questions, round, size]);
  const score = set.filter((q, k) => q.opts[answers[k]]?.correct).length;
  const nextRound = () => { const r = round + 1; setRound(r); store.set(key, r); setAnswers({}); setChecked(false); };
  if (!questions.length) return <p className="tiny">No practice questions could be verified for this document.</p>;
  return (
    <div className="practice">
      {set.map((q, k) => (
        <fieldset key={`${round}-${k}`} className="quiz-question" disabled={checked}>
          <legend lang={lang}>{k + 1}. {q.question}</legend>
          {q.opts.map((o, j) => (
            <label key={j} lang={lang} className={checked ? (o.correct ? "correct" : answers[k] === j ? "incorrect" : "") : ""}>
              <input type="radio" name={`p-${round}-${k}`} checked={answers[k] === j} onChange={() => setAnswers((a) => ({ ...a, [k]: j }))} />
              {o.text}
            </label>
          ))}
          {checked && (
            <p className="evidence" lang={lang}>
              <b>{q.opts[answers[k]]?.correct ? "Correct." : `Answer: ${q.opts.find((o) => o.correct).text}.`}</b>{" "}
              From the document{q.pages ? ` (${pages(q.pages)})` : ""}: “{q.evidence}”
            </p>
          )}
        </fieldset>
      ))}
      {!checked ? (
        <button className="cta small" disabled={Object.keys(answers).length < set.length} onClick={() => { setChecked(true); store.set(key, round + 1); }}>Check answers</button>
      ) : (
        <div className="quiz-result" role="status">
          <span>{score} / {set.length} correct · {Math.round((score / set.length) * 100)}%</span>
          <button className="cta small" onClick={nextRound}>Next set →</button>
        </div>
      )}
    </div>
  );
}

function Exam({ pack, id, level }) {
  const [tab, setTab] = useState(() => store.get("blu.exam-tab", "concepts"));
  useEffect(() => store.set("blu.exam-tab", tab), [tab]);
  const lang = pack.language;
  const notesText = () => [pack.title, "", pack.summary, "",
    ...pack.sections.flatMap((s) => [`${s.heading} (${pages(s.pages)})`, ...s.points.map((p) => `• ${p}`), ""]),
    "Key concepts", ...pack.concepts.map((c) => `• ${c.term}: ${c.definition}`)].join("\n");
  const tabs = [["concepts", `Key concepts · ${pack.concepts.length}`], ["notes", "Revision notes"],
    ["cards", `Flashcards · ${pack.flashcards.length}`], ["practice", `Practice · ${pack.questions.length}`]];
  return (
    <div className="exam">
      <div className="subtabs" role="tablist">
        {tabs.map(([k, label]) => <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{label}</button>)}
      </div>
      {tab === "concepts" && (
        pack.concepts.length ? <ul className="concepts" lang={lang}>
          {pack.concepts.map((c, k) => (
            <li key={k}><b>{c.term}</b>{c.term_en && c.term_en !== c.term && <small>{c.term_en}</small>}<span>{c.definition}</span></li>
          ))}
        </ul> : <p className="tiny">No defined terms were found in this document.</p>
      )}
      {tab === "notes" && (
        <div className="notes" lang={lang}>
          <div className="summary"><p className="eyebrow">IN SHORT</p><p>{pack.summary}</p></div>
          {pack.sections.map((s, k) => (
            <section key={k}><h3>{s.heading} <small>{pages(s.pages)}</small></h3><ul>{s.points.map((p, j) => <li key={j}>{p}</li>)}</ul></section>
          ))}
          <div className="book-actions">
            <button className="link" onClick={() => download(`${pack.title} - notes.txt`, notesText())}>Download notes</button>
            <button className="link" onClick={() => window.print()}>Print</button>
          </div>
        </div>
      )}
      {tab === "cards" && <Flashcards cards={pack.flashcards} lang={lang} id={id} />}
      {tab === "practice" && <Practice questions={pack.questions} lang={lang} id={id} level={level} />}
    </div>
  );
}

// ---------- page ----------
export default function Study({ id, level, lang }) {
  const [pack, setPack] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(null);
  const [mode, setMode] = useState(() => store.get("blu.study-mode", "listen"));
  useEffect(() => store.set("blu.study-mode", mode), [mode]);
  const [starting, setStarting] = useState(false);
  const load = () => getDocument(id, level, lang).then((p) => { setPack(p); if (p.progress && Object.keys(p.progress).length) setStarting(false); return p; })
    .catch((e) => setErr(String(e.message || e)));
  useEffect(() => { load(); getConfig().then(setCfg).catch(() => {}); }, [id, level, lang]);
  const building = starting || Object.keys(pack?.progress || {}).length > 0;
  useEffect(() => {                         // refresh while the audiobook or video is still being recorded
    if (!building) return;
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [building, id, level, lang]);
  const langs = cfg?.languages || {};


  async function create(format, recorded) {           // runs in the background; the page keeps polling
    setErr(""); setStarting(true);
    try {
      const { job } = await extendDocument(id, level, lang, { formats: format, voice: recorded ? undefined : store.get(`blu.voice.${lang}`, undefined) });
      waitJob(job).then(load, (e) => { setErr(String(e.message || e)); setStarting(false); load(); });
    } catch (e) { setErr(String(e.message || e)); setStarting(false); }
  }
  const resume = () => create([pack.audiobook && !pack.audiobook.chapters.every((c) => c.ready) && "audio",
    pack.video && !pack.video.ready && "video"].filter(Boolean).join(",") || "audio,video");
  async function switchLang(l) {
    if (l === lang) return;
    try { await getDocument(id, level, l); location.hash = `#/study/${id}/${level}/${l}`; return; } catch { /* build it */ }
    const formats = [pack.video && "video", pack.audiobook && "audio"].filter(Boolean).join(",") || "audio";
    setErr(""); setBusy({ stage: "queued" });
    try {
      const { job } = await extendDocument(id, level, l, { formats });
      await new Promise((resolve, reject) => waitJob(job, (s) => {
        setBusy({ stage: s.stage, progress: s.progress });
        if (["narrating", "animating", "planning"].includes(s.stage)) resolve();
      }).then(resolve, reject));
      location.hash = `#/study/${id}/${level}/${l}`;
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(null); }
  }

  if (err && !pack) return <div className="center-msg"><p>{err}</p><a href="#/">Back to start</a></div>;
  if (!pack) return <div className="center-msg"><div className="spinner" /></div>;
  const view = { ...pack, languageName: langs[lang] || lang };
  return (
    <div className="study">
      <header className="study-head">
        <a href="#/" className="back">← Home</a>
        <div className="study-title">
          <p className="eyebrow">PDF · {pack.pages} PAGES · {level.toUpperCase()}</p>
          <h1 lang={lang}>{pack.title}</h1>
        </div>
        <select className="choice lang-select" value={lang} onChange={(e) => switchLang(e.target.value)} aria-label="Language" disabled={!!busy}>
          {Object.entries(langs).map(([c, n]) => <option key={c} value={c}>{n}</option>)}
        </select>
      </header>
      <nav className="modes" role="tablist" aria-label="Study modes">
        {MODES.map(([k, name, hint]) => (
          <button key={k} role="tab" aria-selected={mode === k} className={`mode mode-${k}${mode === k ? " on" : ""}`} onClick={() => setMode(k)}>
            <span className="mode-icon" aria-hidden="true">{k === "listen" ? <svg viewBox="0 0 24 24"><path d="M4 14v-2a8 8 0 0 1 16 0v2" /><rect x="3" y="14" width="5" height="7" rx="2" /><rect x="16" y="14" width="5" height="7" rx="2" /></svg>
              : k === "visual" ? <svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="3" /><path d="M10 9l5 3-5 3z" /></svg>
              : <svg viewBox="0 0 24 24"><path d="M6 3h9l4 4v14H6z" /><path d="M9 12l2 2 4-4" /></svg>}</span>
            <span><b>{name} mode</b><small>{hint}</small></span>
          </button>
        ))}
      </nav>
      {building && (
        <div className="build-status" role="status">
          <span className="live-dot" />
          {starting && !Object.keys(pack.progress || {}).length ? "Starting…" : Object.entries(pack.progress).map(([k, v]) =>
            `${k === "audio" ? "Recording the audiobook" : "Animating the video"} ${Math.round(v * 100)}%`).join(" · ")}
          <small>Notes, flashcards and practice are ready now.</small>
        </div>
      )}
      {pack.interrupted && !building && (
        <p className="err">The server stopped before this pack was finished. <button className="link" onClick={resume}>Resume</button></p>
      )}
      {pack.warnings?.length > 0 && <ul className="meta">{pack.warnings.map((w, k) => <li key={k} className="warn">{w}</li>)}</ul>}
      {err && <p className="err">{err}</p>}
      <main className="study-body">
        {mode === "listen" && <Audiobook pack={view} onCreate={create} busy={!!busy || building} />}
        {mode === "visual" && pack.video && !pack.video.ready && (
          <div className="empty-mode">
            <div className="spinner" />
            <h3>{pack.interrupted ? "The video was not finished" : "Animating your video…"}</h3>
            <p>{pack.interrupted ? "Resume to finish it." : `Recording the narration · ${Math.round((pack.progress?.video || 0) * 100)}%. You can listen or study meanwhile.`}</p>
            {pack.interrupted && <button className="cta small" onClick={resume}>Resume</button>}
          </div>
        )}
        {mode === "visual" && (pack.video?.ready === false ? null : pack.video ? <Watch key={pack.video.lesson_id + lang} id={pack.video.lesson_id} lang={lang} embedded /> : (
          <div className="empty-mode">
            <h3>No animated video yet</h3>
            <p>Turn this document into an animated lesson with a presenter, in {view.languageName}.</p>
            <button className="cta small" onClick={() => create("video")} disabled={!!busy || building}>Create the animated video</button>
          </div>
        ))}
        {mode === "exam" && <Exam pack={view} id={id} level={level} />}
      </main>
      {busy && (
        <div className="overlay" role="status" aria-live="polite">
          <div className="busy-card"><div className="spinner" /><b>{STAGE_NAMES[busy.stage] || busy.stage}</b>
            {busy.progress > 0 && <span>{Math.round(busy.progress * 100)}%</span>}</div>
        </div>
      )}
    </div>
  );
}
