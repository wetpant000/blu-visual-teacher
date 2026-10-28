// Browser narration through the Web Speech API. Voices load asynchronously and differ per browser/OS.
import { useEffect, useState } from "react";

const synth = () => (typeof window !== "undefined" ? window.speechSynthesis : undefined);

export function voiceFor(lang, voices = synth()?.getVoices() || []) {
  const matches = voices.filter((v) => v.lang?.toLowerCase().replace("_", "-").split("-")[0] === lang);
  return matches.find((v) => /-IN$/i.test(v.lang) && v.localService) || matches.find((v) => /-IN$/i.test(v.lang))
    || matches.find((v) => v.localService) || matches[0] || null;
}

// re-renders when the browser's voice list arrives
export function useBrowserVoices() {
  const [voices, setVoices] = useState(() => synth()?.getVoices() || []);
  useEffect(() => {
    const s = synth();
    if (!s) return;
    const update = () => setVoices(s.getVoices());
    update();
    s.addEventListener?.("voiceschanged", update);
    return () => s.removeEventListener?.("voiceschanged", update);
  }, []);
  return voices;
}

export const sentences = (text) => {
  const out = [];
  for (const m of text.matchAll(/[^.!?।]+[.!?।]?/g)) if (m[0].trim()) out.push({ text: m[0], at: m.index });
  return out.length ? out : [{ text, at: 0 }];
};

/** Speak `text` from character `offset`. Chrome cuts off long utterances, so each sentence is queued separately.
 *  onProgress(charIndex) reports how far speech got; onEnd fires after the last sentence. Returns a cancel function. */
export function speak(text, { lang, voice, rate = 1, volume = 1, offset = 0, onProgress, onEnd }) {
  const s = synth();
  if (!s) return () => {};
  s.cancel();
  let live = true;
  const parts = sentences(text).filter((p) => p.at + p.text.length > offset);
  parts.forEach((p, k) => {
    const from = Math.max(p.at, offset);
    const u = new SpeechSynthesisUtterance(text.slice(from, p.at + p.text.length));
    u.lang = voice?.lang || lang;
    if (voice) u.voice = voice;
    u.rate = rate;
    u.volume = volume;
    u.onstart = () => live && onProgress?.(from);
    u.onboundary = (e) => live && onProgress?.(from + (e.charIndex || 0), true);
    // an error (e.g. a voice that cannot speak this text) ends the sentence so playback never stalls
    u.onend = u.onerror = () => {
      if (!live) return;
      onProgress?.(p.at + p.text.length);
      if (k === parts.length - 1) onEnd?.();
    };
    s.speak(u);
  });
  if (!parts.length) onEnd?.();
  return () => { live = false; s.cancel(); };
}

export const stopSpeaking = () => synth()?.cancel();
