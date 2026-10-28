// Scene components. Everything is a pure function of (scene, p = progress through the narration, local = seconds, t).
// That makes playback and the MP4 exporter (which seeks to exact times) render identical frames, so no CSS
// animations or transitions are used inside the stage.
export const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
const lerp = (a, b, k) => a + (b - a) * k;
const outCubic = (x) => 1 - Math.pow(1 - clamp(x), 3);
const outBack = (x) => { const c = 1.70158; x = clamp(x) - 1; return 1 + (c + 1) * x * x * x + c * x * x; };
const inOut = (x) => { x = clamp(x); return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; };
// deterministic pseudo-random in [0, 1)
const rnd = (n) => { const x = Math.sin(n * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); };

export function locate(scenes, t) {
  let s = 0;
  for (let i = 0; i < scenes.length; i++) {
    if (t < s + scenes[i].duration || i === scenes.length - 1) return { i, local: t - s, start: s };
    s += scenes[i].duration;
  }
}

// Indic scripts combine several code points into one visible letter, so measure text in graphemes.
const segmenter = typeof Intl !== "undefined" && Intl.Segmenter ? new Intl.Segmenter(undefined, { granularity: "grapheme" }) : null;
const glen = (s) => (segmenter ? [...segmenter.segment(s)].length : [...s].length);

const wrap = (s, max) => {
  const L = [];
  let c = "";
  for (const w of s.split(/\s+/)) {
    if (c && glen(c + " " + w) > max) { L.push(c); c = w; } else c = (c ? c + " " : "") + w;
  }
  if (c) L.push(c);
  return L.slice(0, 3);
};
// largest font size (≤ base) at which every line fits inside `width`
const fit = (lines, width, base) => Math.max(15, Math.min(base, width / (Math.max(...lines.map(glen), 1) * 0.62)));

// Short phrases the presenter says in its speech bubble, in every UI language.
const PHRASES = {
  en: { intro: "Let’s learn!", look: "Look here!" },
  hi: { intro: "चलो सीखें!", look: "यहाँ देखें!" },
  kn: { intro: "ಬನ್ನಿ ಕಲಿಯೋಣ!", look: "ಇಲ್ಲಿ ನೋಡಿ!" },
  ta: { intro: "வாங்க கற்போம்!", look: "இங்கே பாருங்கள்!" },
  te: { intro: "రండి నేర్చుకుందాం!", look: "ఇక్కడ చూడండి!" },
  bn: { intro: "চলো শিখি!", look: "এখানে দেখো!" },
  mr: { intro: "चला शिकूया!", look: "इकडे पाहा!" },
};

// ---------- diagram layouts (shared by the renderer and the presenter's pointer) ----------
const W = 1000, H = 320;

function trim(a, b, hw, hh, pad = 10) {
  const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
  const k = Math.min(hw / Math.abs(dx || 1e-6), hh / Math.abs(dy || 1e-6));
  const off = Math.min(k + pad / len, 0.45);
  return { x1: a.x + dx * off, y1: a.y + dy * off, x2: b.x - dx * off, y2: b.y - dy * off };
}

function layout(sc) {
  const items = sc.items, n = items.length;
  if (sc.diagram_kind === "cycle") {
    const bw = n > 4 ? 170 : 200, bh = 78;
    const nodes = items.map((_, k) => {
      const a = -Math.PI / 2 + (2 * Math.PI * k) / n;
      return { x: W / 2 + Math.cos(a) * 345, y: 168 + Math.sin(a) * 112, w: bw, h: bh };
    });
    const edges = nodes.map((a, k) => trim(a, nodes[(k + 1) % n], bw / 2, bh / 2));
    return { nodes, edges, steps: n };
  }
  if (sc.diagram_kind === "network") {
    const outer = n - 1;
    const hub = { x: W / 2, y: 165, w: 230, h: 96 };
    const nodes = [hub, ...items.slice(1).map((_, k) => {
      const a = -Math.PI / 2 + (2 * Math.PI * k) / Math.max(outer, 1);
      return { x: W / 2 + Math.cos(a) * 360, y: 165 + Math.sin(a) * 118, w: 180, h: 70 };
    })];
    const edges = nodes.slice(1).map((b) => trim(hub, b, 115, 48));
    return { nodes, edges, steps: Math.max(outer, 1) };
  }
  if (sc.diagram_kind === "layers") {
    const row = Math.min(58, 300 / n), h = row - 12;
    const nodes = items.map((_, k) => {
      const w = n > 1 ? lerp(440, 820, k / (n - 1)) : 700;
      return { x: W / 2, y: 10 + k * row + h / 2, w, h };
    });
    const edges = nodes.slice(0, -1).map((a, k) => ({ x1: a.x, y1: a.y + a.h / 2, x2: a.x, y2: nodes[k + 1].y - nodes[k + 1].h / 2 }));
    return { nodes, edges, steps: n };
  }
  const bw = Math.min(210, (W - (n - 1) * 54) / n), gap = (W - n * bw) / Math.max(1, n - 1);
  const nodes = items.map((_, k) => ({ x: k * (bw + gap) + bw / 2, y: 150, w: bw, h: 132 }));
  const edges = nodes.slice(0, -1).map((a, k) => ({ x1: a.x + bw / 2 + 8, y1: 150, x2: nodes[k + 1].x - bw / 2 - 20, y2: 150 }));
  return { nodes, edges, steps: n };
}

// which node is being explained and how long ago it started (as a fraction of one step)
function focus(sc, p) {
  const { steps } = layout(sc);
  const phase = p * steps, active = Math.min(steps - 1, Math.floor(phase));
  const nodeIndex = sc.diagram_kind === "network" ? active + 1 : active;
  return { phase, active, nodeIndex, since: phase - active };
}

// ---------- scene views ----------
function Heading({ text, local }) {
  const k = outCubic(local / 0.6);
  return (
    <div className="heading" style={{ opacity: k, transform: `translateX(${(1 - k) * -40}px)` }}>
      <span className="pin" style={{ transform: `scale(${outBack(local / 0.5)})` }} />
      <span>{text}</span>
      <i className="heading-line" style={{ transform: `scaleX(${outCubic((local - 0.2) / 0.8)})` }} />
    </div>
  );
}

function TitleCard({ sc, local, t }) {
  const words = sc.heading.split(/\s+/).filter(Boolean);
  const n = glen(sc.heading), size = n > 48 ? 54 : n > 30 ? 64 : 80;
  return (
    <div className="title-card">
      <svg className="rays" viewBox="-400 -400 800 800" style={{ transform: `rotate(${t * 6}deg) scale(${0.8 + outCubic(local / 1.2) * 0.2})` }}>
        {Array.from({ length: 18 }, (_, k) => (
          <path key={k} d="M0 0 L-28 -400 L28 -400 Z" transform={`rotate(${k * 20})`} />
        ))}
      </svg>
      <svg className="orbits" viewBox="0 0 400 400">
        {[60, 100, 140, 180].map((r, k) => (
          <g key={r} transform={`rotate(${t * (10 + k * 5) * (k % 2 ? -1 : 1)} 200 200)`}>
            <circle cx="200" cy="200" r={r} className="ring" />
            <circle cx={200 + r} cy="200" r="8" className={`orb o${k}`} />
          </g>
        ))}
      </svg>
      <h1 style={{ fontSize: size }}>
        {words.map((w, k) => {
          const a = outBack((local - 0.15 - k * 0.11) / 0.45);
          return <span key={k} className="word" style={{ opacity: clamp(a * 1.4), transform: `translateY(${(1 - a) * 46}px) scale(${0.7 + 0.3 * a})` }}>{w}</span>;
        })}
      </h1>
      <svg className="underline" viewBox="0 0 600 12">
        <path d="M4 6 Q150 0 300 6 T596 6" pathLength="1" style={{ strokeDasharray: 1, strokeDashoffset: 1 - outCubic((local - 0.3 - words.length * 0.11) / 0.9) }} />
      </svg>
    </div>
  );
}

function NodeBox({ node, label, state, appear, t, k, maxChars, base, number }) {
  const lines = wrap(label, maxChars), size = fit(lines, node.w - 26, base);
  const s = state === "future" ? 0.94 : lerp(0.55, 1, outBack(appear));
  const glow = state === "active" ? 6 + 6 * (0.5 + 0.5 * Math.sin(t * 4.2)) : 0;
  const bob = state === "active" ? Math.sin(t * 3 + k) * 3 : 0;
  return (
    <g className={`node ${state}`} style={{
      opacity: state === "future" ? 0.22 : clamp(appear * 2),
      transform: `translate(${node.x}px, ${node.y + bob}px) scale(${s})`,
      filter: glow ? `drop-shadow(0 0 ${glow}px var(--theme-accent))` : "none",
    }}>
      <rect x={-node.w / 2} y={-node.h / 2} width={node.w} height={node.h} rx={Math.min(22, node.h / 2.6)} />
      {number && <g className="num"><circle cx={-node.w / 2 + 4} cy={-node.h / 2 + 4} r="17" /><text x={-node.w / 2 + 4} y={-node.h / 2 + 11} textAnchor="middle">{k + 1}</text></g>}
      {state === "done" && <g className="tick"><circle cx={node.w / 2 - 4} cy={-node.h / 2 + 4} r="14" /><path d={`M${node.w / 2 - 11} ${-node.h / 2 + 4} l5 5 l9 -10`} /></g>}
      {lines.map((l, j) => (
        <text key={j} x="0" y={(j - (lines.length - 1) / 2) * size * 1.18 + size * 0.36} textAnchor="middle" style={{ fontSize: size }}>{l}</text>
      ))}
    </g>
  );
}

function Diagram({ sc, p, local, t }) {
  const { nodes, edges } = layout(sc);
  const { phase, nodeIndex, active, since } = focus(sc, p);
  const kind = sc.diagram_kind;
  const network = kind === "network";
  const stateOf = (k) => (network && k === 0) || k === nodeIndex ? "active" : k < nodeIndex ? "done" : "future";
  const appearOf = (k) => (network && k === 0 ? local / 0.5 : (phase - (network ? k - 1 : k)) * 3);
  // edge k is drawn while the presenter moves from one node to the next
  const edgeDraw = (k) => network ? clamp((phase - k) * 2.5) : clamp((phase - k - 0.55) / 0.45) + (k < active ? 1 : 0);
  const tokenAt = (q) => {
    const ph = q * (network ? nodes.length - 1 : nodes.length), a = Math.min(nodes.length - 1, Math.floor(ph)), f = ph - a;
    if (network) {
      const hub = nodes[0], b = nodes[Math.min(a + 1, nodes.length - 1)], m = inOut(f * 1.6);
      return { x: lerp(hub.x, b.x, m), y: lerp(hub.y, b.y, m) };
    }
    if (kind === "layers") return { x: W / 2 + nodes[a].w / 2 + 34, y: lerp(nodes[a].y, (nodes[a + 1] || nodes[a]).y, inOut((f - 0.6) / 0.4)) };
    if (kind === "cycle") { const b = nodes[(a + 1) % nodes.length], m = inOut((f - 0.5) / 0.5); return { x: lerp(nodes[a].x, b.x, m), y: lerp(nodes[a].y, b.y, m) }; }
    const b = nodes[a + 1] || nodes[a];
    return { x: lerp(nodes[a].x, b.x, inOut((f - 0.6) / 0.4)), y: 282 };
  };
  const maxChars = kind === "layers" ? 46 : kind === "flow" ? Math.max(7, Math.floor((nodes[0].w - 20) / 17)) : 15;
  const base = kind === "layers" ? 25 : kind === "flow" ? 30 : 25;
  return (
    <div className="diagram">
      <Heading text={sc.heading} local={local} />
      <svg viewBox={`0 0 ${W} ${H}`} className="dgsvg">
        <defs><marker id="diagram-arrow" viewBox="0 0 10 10" markerWidth="4" markerHeight="4" refX="6" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="var(--theme-accent)" /></marker></defs>
        {kind === "network" && <ellipse className="halo" cx={W / 2} cy="165" rx="360" ry="118" />}
        {kind === "flow" && <line className="road" x1={nodes[0].x} y1="282" x2={nodes[nodes.length - 1].x} y2="282" />}
        {edges.map((e, k) => (
          <line key={`e${k}`} className="diagram-edge" {...e} pathLength="1" markerEnd={edgeDraw(k) >= 0.98 ? "url(#diagram-arrow)" : undefined}
            style={{ strokeDasharray: 1, strokeDashoffset: 1 - clamp(edgeDraw(k)) }} />
        ))}
        {sc.items.map((item, k) => (
          <NodeBox key={k} k={k} node={nodes[k]} label={item} state={stateOf(k)} appear={appearOf(k)} t={t}
            maxChars={network && k === 0 ? 17 : maxChars} base={network && k === 0 ? 28 : base} number={kind === "flow" || kind === "layers"} />
        ))}
        {[0.045, 0.03, 0.015, 0].map((lag, j) => {
          const pos = tokenAt(clamp(p - lag));
          return <circle key={j} className="token" cx={pos.x} cy={pos.y} r={6 + j * 2.5} style={{ opacity: 0.2 + j * 0.27 }} />;
        })}
        {since < 0.35 && nodes[nodeIndex] && (
          <circle className="ping" cx={nodes[nodeIndex].x} cy={nodes[nodeIndex].y} r={40 + since * 300} style={{ opacity: 0.7 * (1 - since / 0.35) }} />
        )}
      </svg>
    </div>
  );
}

function Caption({ sc, p }) {
  const parts = (sc.narration.match(/[^.!?।]+[.!?।]?/g) || [sc.narration]).map((s) => s.trim()).filter(Boolean);
  const total = parts.join("").length || 1;
  let from = 0, idx = 0;
  while (idx < parts.length - 1 && p > from + parts[idx].length / total) from += parts[idx++].length / total;
  const span = parts[idx].length / total || 1;
  const q = clamp((p - from) / span);
  const words = parts[idx].split(/\s+/);
  const len = words.join("").length || 1;
  let seen = 0;
  const enter = outCubic(q * 8);
  return (
    <div className="caption">
      <span style={{ opacity: enter, transform: `translateY(${(1 - enter) * 14}px)` }}>
        {words.map((w, k) => {
          const on = seen / len < q;
          seen += w.length;
          return <b key={k} className={on ? "said" : ""}>{w} </b>;
        })}
      </span>
    </div>
  );
}

// ---------- the presenter ----------
const SKINS = [["#f6cfae", "#e3a988"], ["#dca57d", "#c08660"], ["#a9714d", "#8a5a3c"]];
const HAIR = ["#2b2438", "#4a2f25", "#1d2433", "#5b3a2a"];

function Accessory({ role }) {
  if (role === "astronaut") return <g><circle cx="120" cy="104" r="68" className="acc-glass" /><circle cx="120" cy="36" r="6" className="acc-light" /></g>;
  if (role === "robot") return <g><line x1="120" y1="52" x2="120" y2="26" className="acc-line" /><circle cx="120" cy="22" r="8" className="acc-light" /><rect x="66" y="100" width="10" height="16" rx="3" className="acc-metal" /><rect x="164" y="100" width="10" height="16" rx="3" className="acc-metal" /></g>;
  if (role === "engineer") return <g><path d="M70 82 Q72 40 120 38 Q168 40 170 82 Z" className="acc-hat" /><rect x="60" y="78" width="120" height="10" rx="5" className="acc-hat" /></g>;
  if (role === "explorer" || role === "naturalist") return <g><ellipse cx="120" cy="66" rx="72" ry="13" className="acc-safari" /><path d="M82 66 Q86 30 120 30 Q154 30 158 66 Z" className="acc-safari" /><rect x="82" y="56" width="76" height="8" className="acc-band" />{role === "naturalist" && <path d="M150 56 q16 -18 26 -6 q-12 14 -26 6 Z" className="acc-leaf" />}</g>;
  if (role === "diver") return <g><rect x="80" y="88" width="80" height="34" rx="14" className="acc-mask" /><line x1="160" y1="105" x2="172" y2="60" className="acc-line" /></g>;
  if (role === "scientist" || role === "inventor") return <g><rect x="78" y="66" width="36" height="22" rx="10" className="acc-goggle" /><rect x="126" y="66" width="36" height="22" rx="10" className="acc-goggle" /><line x1="114" y1="77" x2="126" y2="77" className="acc-line" /></g>;
  return <g className="acc-glasses"><circle cx="102" cy="108" r="14" /><circle cx="138" cy="108" r="14" /><line x1="116" y1="108" x2="124" y2="108" /></g>;
}

export function Presenter({ t, open = 0, speaking, point = 100, jab = 0, wave = 0, role = "teacher", look = -1, variant = 0 }) {
  const v = Math.abs(Math.trunc(variant)) || 0;
  const [skin, skinShade] = SKINS[v % SKINS.length], hair = HAIR[(v >> 2) % HAIR.length];
  const blink = (t % 3.7) < 0.13 || (t % 7.9) < 0.11;
  const tilt = Math.sin(t * 1.3) * 3 + (speaking ? Math.sin(t * 2.7) * 2 : 0) + jab * -4;
  const breathe = 1 + Math.sin(t * 2) * 0.012;
  const pointer = point - jab * 12;
  const free = wave ? -150 + Math.sin(t * 11) * 22 : speaking ? -28 - (0.5 + 0.5 * Math.sin(t * 3.1)) * 30 : -14;
  const brow = jab * 5;
  return (
    <svg className="presenter" viewBox="0 0 240 320" aria-hidden="true">
      <ellipse cx="120" cy="312" rx="96" ry="9" className="p-shadow" />
      <g style={{ transform: `rotate(${jab * -2}deg) scale(${breathe})`, transformOrigin: "120px 300px" }}>
        {/* pointing arm (behind the body) */}
        <g style={{ transform: `rotate(${pointer}deg)`, transformOrigin: "80px 186px" }}>
          <rect x="68" y="182" width="24" height="70" rx="12" className="p-sleeve" />
          <line x1="80" y1="258" x2="80" y2="338" className="p-stick" />
          <circle cx="80" cy="340" r={5 + jab * 4} className="p-tip" />
          <circle cx="80" cy="256" r="13" style={{ fill: skin }} className="p-skin" />
        </g>
        <path d="M66 300 L70 206 Q72 172 120 168 Q168 172 170 206 L174 300 Z" className="p-coat" />
        <path d="M104 170 L120 214 L136 170 Z" className="p-shirt" />
        <path d="M120 214 L113 226 L120 262 L127 226 Z" className="p-tie" />
        <rect x="106" y="146" width="28" height="28" rx="10" style={{ fill: skinShade }} className="p-skin" />
        {/* waving / gesturing arm */}
        <g style={{ transform: `rotate(${free}deg)`, transformOrigin: "160px 188px" }}>
          <rect x="148" y="184" width="24" height="70" rx="12" className="p-sleeve" />
          <circle cx="160" cy="258" r="13" style={{ fill: skin }} className="p-skin" />
        </g>
        <g style={{ transform: `rotate(${tilt}deg)`, transformOrigin: "120px 150px" }}>
          <path d="M60 110 Q54 40 120 38 Q186 40 180 110 Q184 150 166 160 L74 160 Q56 150 60 110 Z" style={{ fill: hair }} className="p-hair" />
          <ellipse cx="68" cy="112" rx="9" ry="14" style={{ fill: skinShade }} className="p-skin" />
          <ellipse cx="172" cy="112" rx="9" ry="14" style={{ fill: skinShade }} className="p-skin" />
          <ellipse cx="120" cy="108" rx="50" ry="54" style={{ fill: skin }} className="p-skin" />
          <path d="M70 92 Q78 50 120 52 Q164 50 172 96 Q150 70 118 74 Q92 76 70 92 Z" style={{ fill: hair }} className="p-hair" />
          <g className="p-eyes">
            {[102, 138].map((x) => (
              <g key={x}>
                <path d={`M${x - 10} ${92 - brow} Q${x} ${86 - brow} ${x + 10} ${92 - brow}`} className="p-brow" />
                {blink ? <path d={`M${x - 8} 110 Q${x} 114 ${x + 8} 110`} className="p-lid" />
                  : <g><ellipse cx={x} cy="110" rx="8" ry="10" className="p-white" /><circle cx={x + look * 2.5} cy="111" r="5.5" className="p-iris" /><circle cx={x + look * 2.5 + 2} cy="108" r="1.8" className="p-glint" /></g>}
              </g>
            ))}
          </g>
          <ellipse cx="92" cy="130" rx="8" ry="4" className="p-blush" />
          <ellipse cx="148" cy="130" rx="8" ry="4" className="p-blush" />
          {open > 0.05
            ? <g><ellipse cx="120" cy="138" rx={8 + open * 4} ry={2.5 + open * 9} className="p-mouth" /><ellipse cx="120" cy={140 + open * 5} rx={5 + open * 2} ry={1 + open * 3} className="p-tongue" /></g>
            : <path d="M108 136 Q120 145 132 136" className="p-smile" />}
          <Accessory role={role} />
        </g>
      </g>
      {/* podium */}
      <path d="M40 262 L200 262 L188 320 L52 320 Z" className="p-podium" />
      <rect x="40" y="262" width="160" height="10" rx="3" className="p-podium-top" />
      <circle cx="120" cy="294" r="11" className="p-podium-logo" />
      <path d="M116 288 L127 294 L116 300 Z" className="p-podium-play" />
    </svg>
  );
}

// ---------- background and transitions ----------
function Particles({ t }) {
  return (
    <svg className="particles" viewBox="0 0 1280 720" aria-hidden="true">
      {Array.from({ length: 22 }, (_, k) => {
        const speed = 8 + rnd(k) * 22, size = 2 + rnd(k + 9) * 6;
        const y = 760 - ((rnd(k + 3) * 760 + t * speed) % 800);
        const x = rnd(k + 5) * 1280 + Math.sin(t * 0.6 + k) * 18;
        return k % 3 ? <circle key={k} cx={x} cy={y} r={size} className={k % 2 ? "pa" : "pb"} />
          : <rect key={k} x={x} y={y} width={size * 2} height={size * 2} rx="2" className="pb" transform={`rotate(${t * 30 + k * 40} ${x + size} ${y + size})`} />;
      })}
    </svg>
  );
}

function visualScene(scene) {
  if (scene.type !== "code_walkthrough") return scene;
  const items = (scene.narration.match(/[^.!?।]+[.!?।]?/g) || [scene.narration])
    .map((part) => part.trim().split(/\s+/).slice(0, 5).join(" "))
    .filter(Boolean)
    .slice(0, 4)
    .map((part) => part.length > 38 ? `${part.slice(0, 35).trimEnd()}…` : part);
  while (items.length < 2) items.push(items.length ? "See what happens next" : "Follow each step");
  return { ...scene, type: "diagram", diagram_kind: "flow", items };
}

// stage geometry (px) — the scene area leaves room on the right for the presenter
const AREA = { left: 56, top: 78, width: 940, headH: 92 };
const SHOULDER = { x: 1036 + 80 * 0.92, y: 262 + 186 * 0.92 };

export function Stage({ lesson, t }) {
  const total = lesson.scenes.reduce((a, s) => a + s.duration, 0);
  const { i, local } = locate(lesson.scenes, t);
  const sc = lesson.scenes[i];
  const visual = visualScene(sc);
  const p = clamp(local / Math.max(0.1, sc.audio_seconds));
  const isDiagram = visual.type === "diagram" && visual.items?.length >= 2;
  const theme = lesson.theme || {};
  const palette = theme.palette || {};
  const phrases = PHRASES[lesson.language] || PHRASES.en;
  const themeStyle = {
    "--theme-bg": palette.background || "hsl(160 42% 10%)",
    "--theme-surface": palette.surface || "hsl(160 38% 17%)",
    "--theme-grid": palette.grid || "hsl(160 35% 29%)",
    "--theme-accent": palette.accent || "#ffc24b",
    "--theme-secondary": palette.secondary || "#86d3f5",
    "--theme-glow": palette.glow || "hsl(160 70% 30%)",
    "--character-color": palette.character || "#405c80",
    "--character-shade": palette.characterShade || "#243047",
  };

  // presenter: lip-sync from the voice-over loudness, point at the node being explained
  const speaking = local < sc.audio_seconds;
  const env = sc.mouth;
  const open = !speaking ? 0 : env?.length ? env[Math.min(env.length - 1, Math.floor(local * 25))] || 0
    : clamp(0.35 + 0.45 * Math.sin(local * 13) * Math.sin(local * 4.1 + i));
  let target = { x: AREA.left + AREA.width / 2, y: 300 }, jab = 0, bubble = phrases.intro, bubbleAge = local;
  if (isDiagram) {
    const f = focus(visual, p), node = layout(visual).nodes[f.nodeIndex];
    const s = AREA.width / W;
    target = { x: AREA.left + node.x * s, y: AREA.top + AREA.headH + node.y * s };
    jab = f.since < 0.25 ? Math.sin((f.since / 0.25) * Math.PI) : 0;
    bubble = visual.items[f.nodeIndex];
    bubbleAge = (f.since * sc.audio_seconds) / layout(visual).steps;
    if (local < 0.6) { bubble = phrases.look; bubbleAge = local; }
  } else if (i > 0) bubble = sc.heading.length <= 28 ? sc.heading : phrases.look;
  const point = clamp(Math.atan2(-(target.x - SHOULDER.x), target.y - SHOULDER.y) * 180 / Math.PI, 70, 150);
  const variant = parseInt((theme.id || "0").slice(0, 2), 16) || 0;

  // camera + transition
  const wipe = i > 0 ? inOut(local / 0.6) : 1;
  const fade = clamp((local - (i > 0 ? 0.15 : 0)) / 0.4);
  const zoom = 1 + 0.035 * inOut(local / Math.max(1, sc.duration));
  const drift = Math.sin(t * 0.35) * 6;
  const motion = theme.motion ?? 0;
  const enter = [
    `translateY(${(1 - outCubic(fade)) * 30}px)`,
    `scale(${0.92 + outBack(fade) * 0.08})`,
    `translateX(${(1 - outCubic(fade)) * -50}px)`,
    `rotate(${(1 - outCubic(fade)) * 2.5}deg) scale(${0.95 + outCubic(fade) * 0.05})`,
  ][motion % 4];
  const bubbleIn = outBack(bubbleAge / 0.35);

  return (
    <div className="stage" data-theme={theme.archetype || "studio"} lang={lesson.language} style={themeStyle}>
      <div className="stage-bg" style={{ transform: `translate(${drift}px, ${Math.cos(t * 0.3) * 5}px) rotate(${Math.sin(t * 0.1) * 3}deg)` }} />
      <div className="board-grid" style={{ backgroundPosition: `${t * 6}px ${t * 4}px` }} />
      <Particles t={t} />
      <div className="topbar">
        <span className="chip">{i + 1} / {lesson.scenes.length}</span>
        <span className="topic-title">{lesson.title}</span>
        <span className="brand"><i />Blu</span>
      </div>
      <div className="camera" style={{ transform: `scale(${zoom}) translate(${drift * 0.4}px, 0)` }}>
        <div className="scene" style={{ opacity: fade, transform: enter }}>
          {isDiagram ? <Diagram sc={visual} p={p} local={local} t={t} /> : <TitleCard sc={visual} local={local} t={t} />}
        </div>
      </div>
      <div className="presenter-wrap" style={{ transform: `translateY(${(1 - outBack(clamp(t / 0.8))) * 120 + Math.sin(t * 2.2) * 3}px)` }}>
        <div className="bubble" lang={lesson.language} style={{ opacity: clamp(bubbleIn * 1.5), transform: `scale(${0.6 + 0.4 * bubbleIn})` }}>{bubble}</div>
        <Presenter t={t} open={open} speaking={speaking} point={point} jab={jab}
          wave={i === 0 && local < 2.6 ? 1 : 0} role={theme.character} look={-1} variant={variant} />
      </div>
      {wipe < 1 && <div className="wipe" style={{ transform: `translateX(${lerp(-140, 140, wipe)}%) skewX(-16deg)` }}><i /></div>}
      <div lang={lesson.language}><Caption sc={sc} p={p} /></div>
      <div className="progress">
        <i style={{ width: `${clamp(t / total) * 100}%` }} />
        {lesson.scenes.slice(0, -1).reduce((acc, s) => [...acc, (acc.at(-1) || 0) + s.duration], []).map((at, k) => (
          <b key={k} style={{ left: `${(at / total) * 100}%` }} />
        ))}
      </div>
    </div>
  );
}
