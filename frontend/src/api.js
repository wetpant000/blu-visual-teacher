export const OUTDATED = "The server is running an older version of Blu that cannot read PDFs. Stop it (Ctrl+C) and start it again with ./scripts/run.sh, then reload this page.";
// FastAPI errors arrive as {"detail": "..."}; show the message, not the JSON
async function errorText(r) {
  const text = await r.text();
  try { const d = JSON.parse(text).detail; return typeof d === "string" ? d : JSON.stringify(d); } catch { return text || `HTTP ${r.status}`; }
}
const j = async (u, o) => {
  const r = await fetch(u, o);
  if (!r.ok) throw new Error(await errorText(r));
  return r.json();
};
const post = (u, b) => j(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: b ? JSON.stringify(b) : undefined });

export const getConfig = () => j("/api/config");
export const getHistory = () => j("/api/history");
export const getLesson = (id, l) => j(`/api/lessons/${id}/${l}`);
export const startGenerate = (b) => post("/api/generate", b);
export const startRevoice = (id, l, voice) => post(`/api/lessons/${id}/${l}`, voice ? { voice } : undefined);
export const startExport = (id, l) => post(`/api/lessons/${id}/${l}/export`);

export async function waitJob(job, onTick) {
  for (;;) {
    const s = await j(`/api/jobs/${job}`);
    onTick?.(s);
    if (s.status === "done") return s.result;
    if (s.status === "error") throw new Error(s.error);
    await new Promise((r) => setTimeout(r, 600));
  }
}

// ---------- PDF study packs ----------
const query = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
export async function startDocument(file, { language, level, formats, voice, length }) {
  const r = await fetch(`/api/documents?${query({ filename: file.name, language, level, formats, voice, length })}`, {
    method: "POST", headers: { "Content-Type": "application/pdf" }, body: file,
  });
  if (r.status === 404 || r.status === 405) throw new Error(OUTDATED);
  if (!r.ok) throw new Error(await errorText(r));
  return r.json();
}
export const extendDocument = (id, level, lang, { formats, voice, length } = {}) =>
  post(`/api/documents/${id}/${level}/${lang}?${query({ formats, voice, length })}`);
export const getDocument = (id, level, lang) => j(`/api/documents/${id}/${level}/${lang}`);
export const getDocuments = () => j("/api/documents");
