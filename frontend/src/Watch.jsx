import { useEffect, useMemo, useRef, useState } from "react";
import { Stage, locate } from "./Scenes";
import { getConfig, getLesson, startExport, startRevoice, waitJob } from "./api";
import { makeQuiz } from "./quiz";
import { speak, stopSpeaking, useBrowserVoices, voiceFor } from "./speech";

const isReady = (scene) => scene.ready !== false;      // older lessons have no flag: always ready
const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

export default function Watch({ id, lang, render, embedded }) {
  const [lesson, setLesson] = useState(null);
  const [langs, setLangs] = useState({});
  const [serverVoices, setServerVoices] = useState({});
  const [err, setErr] = useState("");
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [audioError, setAudioError] = useState("");
  const [rate, setRate] = useState(1);
  const [volume, setVolume] = useState(0.8);
  const [muted, setMuted] = useState(false);
  const [busy, setBusy] = useState("");
  const [links, setLinks] = useState(null);
  const [seekTick, setSeekTick] = useState(0);
  const [scale, setScale] = useState(1);
  const [answers, setAnswers] = useState({});
  const [quizResult, setQuizResult] = useState(null);
  // the quiz rotates: once a set has been checked, the next visit or "Next quiz" shows different questions
  const quizKey = `blu.quiz-round.${id}`;
  const [quizRound, setQuizRound] = useState(() => { try { return +localStorage.getItem(quizKey) || 0; } catch { return 0; } });
  const saveRound = (round) => { try { localStorage.setItem(quizKey, String(round)); } catch { /* storage unavailable */ } };
  const nextQuiz = () => { setAnswers({}); setQuizResult(null); setQuizRound((round) => { saveRound(round + 1); return round + 1; }); };
  const tRef = useRef(0), audio = useRef(null), box = useRef(null), lastSeek = useRef(-1);
  const speech = useRef({ scene: -1, ended: false, reached: 0, words: false });
  const browserVoices = useBrowserVoices();
  const browser = !!lesson?.engines?.includes("browser");
  const browserVoice = lesson ? voiceFor(lesson.language, browserVoices) : null;

  useEffect(() => {
    getLesson(id, lang).then(setLesson).catch((e) => setErr(String(e.message || e)));
    if (!render) getConfig().then((c) => { setLangs(c.languages); setServerVoices(c.voices || {}); }).catch(() => {});
  }, [id, lang, render]);

  // streaming: the voice-over is still being recorded, so pick up each scene as soon as it is ready
  const streaming = !!lesson && lesson.complete === false;
  useEffect(() => {
    if (!streaming || render) return;
    const readyCount = (l) => l.scenes.filter(isReady).length;
    const timer = setInterval(() => getLesson(id, lang).then((next) => setLesson((prev) =>
      prev && next.complete === prev.complete && readyCount(next) === readyCount(prev) ? prev : next)).catch(() => {}), 1500);
    return () => clearInterval(timer);
  }, [streaming, id, lang, render]);

  // export mode: the backend drives time through window.__seek
  useEffect(() => {
    if (!render || !lesson) return;
    document.documentElement.classList.add("export");
    window.__seek = (x) => new Promise((r) => { setT(x); requestAnimationFrame(() => requestAnimationFrame(() => r(true))); });
    document.fonts.ready.then(() => { window.__ready = true; });
  }, [render, lesson]);

  useEffect(() => {
    if (render || !box.current) return;
    const ro = new ResizeObserver(([e]) => setScale(e.contentRect.width / 1280));
    ro.observe(box.current);
    return () => ro.disconnect();
  }, [render, lesson]);

  const total = useMemo(() => (lesson ? lesson.scenes.reduce((a, s) => a + s.duration, 0) : 0), [lesson]);
  const cur = lesson ? locate(lesson.scenes, t) : null;
  const idx = cur?.i;
  const quiz = useMemo(() => makeQuiz(lesson, quizRound), [lesson, quizRound]);
  const answeredCount = quiz.filter((_, index) => answers[index] !== undefined).length;

  const seek = (x) => { tRef.current = x; setT(x); setSeekTick((k) => k + 1); };

  // the clock: audio is the master while a voice-over plays; otherwise wall-clock time (pauses between scenes)
  useEffect(() => {
    if (!playing || !lesson) return;
    let last = performance.now(), raf;
    const tick = (now) => {
      const dt = (now - last) / 1000; last = now;
      let nt = tRef.current + dt * rate;
      const { i, start } = locate(lesson.scenes, tRef.current);
      const a = audio.current, sp = speech.current, au = lesson.scenes[i].audio_seconds, loc = nt - start;
      if (browser) {
        // the browser's speech is the master: wait for it at the end of a scene, catch up when it runs ahead
        if (sp.scene === i && sp.ended) { if (loc < au) nt = start + au; }
        else if (sp.scene === i) {
          if (sp.reached > loc) nt = start + sp.reached;
          else if (sp.words) nt = Math.min(nt, start + sp.reached + 0.8);
          if (nt - start > au - 0.05) {
            nt = start + au - 0.05;
            // speech engines occasionally stall without firing events; give up waiting after a while
            sp.held = sp.lastReached === sp.reached ? (sp.held || 0) + dt : 0;
            sp.lastReached = sp.reached;
            if (sp.held > 3 + au) sp.ended = true;
          }
          nt = Math.max(nt, tRef.current);
        }
      } else if (a && nt - start < au && !a.ended) {
        if (!a.paused && a.readyState > 2) nt = Math.max(tRef.current, start + a.currentTime);
        else nt = tRef.current;
      }
      const end = start + lesson.scenes[i].duration;
      if (!isReady(lesson.scenes[i])) nt = tRef.current;            // this scene is still being recorded
      else if (i + 1 < lesson.scenes.length && !isReady(lesson.scenes[i + 1])) nt = Math.min(nt, end - 0.02);
      if (nt >= total) { tRef.current = total; setT(total); setPlaying(false); return; }
      tRef.current = nt; setT(nt);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, lesson, rate, total, browser]);

  // browser narration: (re)start speaking whenever the scene, play state, position or speed changes
  useEffect(() => {
    if (!browser || render || !lesson) return;
    const sc = lesson.scenes[idx];
    const { local } = locate(lesson.scenes, tRef.current);
    if (!playing || !browserVoice || local >= sc.audio_seconds - 0.05) { stopSpeaking(); return; }
    const text = sc.narration, len = text.length || 1;
    const guess = Math.floor((local / sc.audio_seconds) * len);
    const offset = guess > 0 ? text.lastIndexOf(" ", guess) + 1 : 0;
    speech.current = { scene: idx, ended: false, reached: local, words: false };
    const sp = speech.current;
    return speak(text, {
      lang: lesson.language, voice: browserVoice, rate, volume: muted ? 0 : volume, offset,
      onProgress: (c, word) => { sp.reached = Math.max(sp.reached, (c / len) * sc.audio_seconds); sp.words ||= !!word; },
      onEnd: () => { sp.ended = true; },
    });
  }, [idx, playing, lesson, seekTick, rate, browser, browserVoice, render]);
  useEffect(() => () => stopSpeaking(), []);

  useEffect(() => {
    const a = audio.current;
    if (!a || !lesson || render || browser) return;
    const sc = lesson.scenes[idx];
    if (!isReady(sc) || !sc.audio) { a.pause(); return; }
    // a streamed update to other scenes must not restart the audio that is already playing
    if (playing && !a.paused && a.dataset.src === sc.audio && lastSeek.current === seekTick) { a.playbackRate = rate; return; }
    lastSeek.current = seekTick;
    if (a.dataset.src !== sc.audio) {
      setAudioError("");
      a.dataset.src = sc.audio;
      a.src = sc.audio;
      a.load();
    }
    a.playbackRate = rate;
    const { local } = locate(lesson.scenes, tRef.current);
    if (playing && local < sc.audio_seconds) {
      const startPlayback = () => {
        try {
          if (!Number.isFinite(a.duration)) throw new Error("audio duration is unavailable");
          a.currentTime = Math.min(Math.max(0, local), a.duration);
          a.play().catch((error) => {
            if (error?.name === "AbortError") return;
            setAudioError(`Audio playback failed: ${error?.message || "the browser could not play this WAV file"}`);
            setPlaying(false);
          });
        } catch (error) {
          setAudioError(`Audio playback failed: ${error?.message || "the browser could not seek this WAV file"}`);
          setPlaying(false);
        }
      };
      if (a.readyState >= 1) startPlayback();
      else a.addEventListener("loadedmetadata", startPlayback, { once: true });
      return () => a.removeEventListener("loadedmetadata", startPlayback);
    } else a.pause();
  }, [idx, playing, lesson, seekTick, render, browser]);
  useEffect(() => { if (audio.current) audio.current.playbackRate = rate; }, [rate]);
  useEffect(() => {
    if (audio.current) {
      audio.current.volume = volume;
      audio.current.muted = muted;
    }
  }, [volume, muted]);

  const toggle = () => {
    setAudioError("");
    if (tRef.current >= total - 0.05) seek(0);
    setPlaying((p) => !p);
  };
  const starts = useMemo(() => { let s = 0; return lesson ? lesson.scenes.map((x) => { const r = s; s += x.duration; return r; }) : []; }, [lesson]);
  const next = () => idx < lesson.scenes.length - 1 && seek(starts[idx + 1]);
  const prev = () => seek(cur.local > 1.5 || idx === 0 ? starts[idx] : starts[idx - 1]);

  async function switchLang(l) {
    if (l === lang) return;
    setPlaying(false); setBusy("Preparing…");
    try {
      try { await getLesson(id, l); } catch {
        const { job } = await startRevoice(id, l);
        await waitJob(job, (s) => setBusy(s.stage === "narrating" && s.progress ? `narrating ${Math.round(s.progress * 100)}%` : s.stage));
      }
      location.hash = `#/watch/${id}/${l}`;
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(""); }
  }
  async function recordLocally() {
    setPlaying(false); setBusy("Recording voice…");
    try {
      const { job } = await startRevoice(id, lang);
      await waitJob(job, (s) => setBusy(s.stage === "narrating" ? `Recording voice ${Math.round((s.progress || 0) * 100)}%` : s.stage));
      setLesson(await getLesson(id, lang));
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(""); }
  }
  async function doExport() {
    setLinks(null); setBusy("rendering");
    try {
      const { job } = await startExport(id, lang);
      setLinks(await waitJob(job, (s) => setBusy(s.stage === "rendering" ? `rendering ${Math.round((s.progress || 0) * 100)}%` : s.stage)));
    } catch (e) { setErr(String(e.message || e)); } finally { setBusy(""); }
  }

  if (err && !lesson) return <div className="center-msg"><p>{err}</p><a href="#/">Back to start</a></div>;
  if (!lesson) return <div className="center-msg"><div className="spinner" /></div>;
  if (render) return <div id="render-root"><Stage lesson={lesson} t={t} /></div>;

  return (
    <div className={`watch${embedded ? " embedded" : ""}`}>
      {!embedded && <header className="wbar">
        <a href="#/" className="back">← New lesson</a>
        <h2>{lesson.title}</h2>
        <nav className="langs">
          {Object.entries(langs).map(([c, n]) => (
            <button key={c} className={c === lang ? "on" : ""} onClick={() => switchLang(c)} disabled={!!busy}>{n}</button>
          ))}
        </nav>
      </header>}

      <div className="player" ref={box} style={{ height: 720 * scale }}>
        <div style={{ transform: `scale(${scale})`, transformOrigin: "0 0", width: 1280, height: 720 }}>
          <Stage lesson={lesson} t={t} />
        </div>
        {!playing && t === 0 && <button className="bigplay" onClick={toggle} aria-label="Play">▶</button>}
        {playing && cur && (!isReady(lesson.scenes[idx]) || (idx + 1 < lesson.scenes.length && !isReady(lesson.scenes[idx + 1])
          && cur.local >= lesson.scenes[idx].duration - 0.1)) && <div className="waiting-badge"><span className="spinner" />Preparing the next scene…</div>}
        {busy && <div className="busy"><div className="spinner" />{busy}</div>}
      </div>
      <audio ref={audio} preload="auto" onError={(event) => {
        const code = event.currentTarget.error?.code;
        const reason = ({ 1: "loading was aborted", 2: "a network error occurred", 3: "the WAV could not be decoded", 4: "the audio format is unsupported" })[code] || "the audio file could not be loaded";
        setAudioError(`Audio error: ${reason}. Re-generate this lesson or try another voice.`);
        setPlaying(false);
      }} />

      <div className="controls">
        <button onClick={prev} aria-label="Previous scene">⏮</button>
        <button className="play" onClick={toggle}>{playing ? "Pause" : t >= total - 0.05 ? "Replay" : "Play"}</button>
        <button onClick={next} aria-label="Next scene">⏭</button>
        <input type="range" min="0" max={total} step="0.05" value={t} onChange={(e) => seek(+e.target.value)} aria-label="Seek" />
        <span className="time">{fmt(t)} / {fmt(total)}</span>
        <select value={rate} onChange={(e) => setRate(+e.target.value)} aria-label="Speed">
          {[0.75, 1, 1.25, 1.5].map((r) => <option key={r} value={r}>{r}×</option>)}
        </select>
        <button onClick={() => setMuted((value) => !value)} aria-label={muted ? "Unmute audio" : "Mute audio"} aria-pressed={muted}>
          {muted ? "Unmute" : "Mute"}
        </button>
        <input type="range" min="0" max="1" step="0.05" value={volume} onChange={(e) => setVolume(+e.target.value)} aria-label="Volume" />
      </div>
      <div className="dots">
        {lesson.scenes.map((s, k) => <button key={k} className={k === idx ? "on" : ""} onClick={() => seek(starts[k])} title={s.heading}>{k + 1}</button>)}
      </div>

      <section className="panel">
        <div className="export">
          <button className="cta small" onClick={doExport} disabled={!!busy || streaming}
            title={streaming ? "Available once every scene has its voice-over" : undefined}>Export MP4 (720p)</button>
          {streaming && <span className="tiny">Recording voice-over · {lesson.scenes.filter(isReady).length} of {lesson.scenes.length} scenes ready</span>}
          {links && <><a href={links.mp4} download>Download MP4</a><a href={links.srt} download>Download captions (.srt)</a></>}
        </div>
        {links && <video className="export-preview" src={links.mp4} controls playsInline preload="metadata">
          Your browser does not support MP4 playback.
        </video>}
        {err && <p className="err">{err}</p>}
        <ul className="meta">
          {audioError && <li className="warn" role="alert">{audioError}</li>}
          {browser && !browserVoice && (
            <li className="warn" role="alert">
              This browser has no {langs[lesson.language] || lesson.language} voice, so the lesson plays without sound.{" "}
              <button className="link" onClick={recordLocally} disabled={!!busy || !serverVoices[lesson.language]?.some((v) => v.id !== "browser")}>Record the voice on this machine</button>
            </li>
          )}
          {lesson.warnings.map((w, k) => <li key={k} className="warn">{w}</li>)}
          <li>Planner: {lesson.source === "llm" ? "local LLM" : "built-in sample lesson"} · Voice: {browser ? `browser · ${browserVoice?.name || "none available"}` : lesson.voice_name || lesson.engines.join(", ")} ({lesson.voice_language || lesson.language})</li>
          <li>Stage times: {Object.entries(lesson.timings).map(([k, v]) => `${k} ${v}s`).join(" · ")}</li>
        </ul>
      </section>
      {!embedded && <section className="panel quiz" aria-labelledby="quiz-title">
        <h3 id="quiz-title">Quick quiz: {lesson.title}</h3>
        <p>Fresh questions based on the lesson diagram. Answer every question to see your score and explanations.</p>
        {quiz.length > 0 && (
          <div className="quiz-progress" aria-live="polite">
            <span>{answeredCount} of {quiz.length} answered</span>
            <button type="button" onClick={nextQuiz}>
              New quiz
            </button>
          </div>
        )}
        {quiz.map((question, questionIndex) => (
          <fieldset key={`${quizRound}-${question.prompt}`} className="quiz-question" disabled={quizResult !== null}>
            <legend>{questionIndex + 1}. {question.prompt}</legend>
            {question.options.map((option) => (
              <label key={option} className={quizResult !== null
                ? option === question.answer ? "correct" : answers[questionIndex] === option ? "incorrect" : ""
                : ""}>
                <input
                  type="radio"
                  name={`quiz-${quizRound}-${questionIndex}`}
                  value={option}
                  checked={answers[questionIndex] === option}
                  onChange={() => setAnswers((current) => ({ ...current, [questionIndex]: option }))}
                />
                {option}
              </label>
            ))}
            {quizResult !== null && (
              <p className="quiz-explanation">
                {answers[questionIndex] === question.answer ? "Correct. " : `Correct answer: ${question.answer}. `}
                {question.explanation}
              </p>
            )}
          </fieldset>
        ))}
        {quiz.length === 0 ? <p>This lesson has no diagram questions available.</p> : quizResult === null ? (
          <button className="cta small" onClick={() => { setQuizResult(quiz.filter((q, k) => answers[k] === q.answer).length); saveRound(quizRound + 1); }}
            disabled={answeredCount !== quiz.length}>
            Check answers
          </button>
        ) : (
          <div className="quiz-result" role="status">
            <span>{quizResult} / {quiz.length} correct · {Math.round((quizResult / quiz.length) * 100)}%</span>
            <button className="cta small" onClick={nextQuiz}>Next quiz →</button>
          </div>
        )}
      </section>}
    </div>
  );
}
