# Blu Visual Teacher

Turn a topic or a PDF into a narrated, animated lesson — in English and Indian languages, running **fully offline** on a
CPU laptop.

## What it does
- **Topic → animated video.** Type a concept; a local LLM plans the scenes, an animated presenter explains them with
  diagrams, voice-over and word-by-word captions. The video starts playing as soon as the first scene is voiced.
- **PDF → study pack.** Drag and drop a PDF and choose language, level and output:
  - **Listen mode** — an audiobook of the document, chapter by chapter.
  - **Visual mode** — an animated video lesson.
  - **Exam mode** — key concepts, revision notes, flashcards and practice questions.

  Everything is checked against the PDF: unsupported statements are dropped and each answer cites its source sentence.
- **7 languages:** English, Hindi, Kannada, Tamil, Telugu, Bengali, Marathi.
- **Extras:** rotating quizzes, MP4 (720p) + `.srt` export, lesson history.

## Install
Requirements: Linux/macOS, Python 3.11, Node.js 20.19+ or 22.12+, [Ollama](https://ollama.com), FFmpeg, Poppler, eSpeak NG.

```bash
sudo apt install ffmpeg poppler-utils espeak-ng fonts-noto   # macOS: brew install ffmpeg poppler espeak-ng
ollama pull qwen2.5:3b && ollama pull llama3.2:3b             # lesson planner + translation fallback
./scripts/setup.sh                                            # venv, Python packages, Chromium, frontend build
```

Voice models (place under `models/`, paths in `config.yaml`):
- **Kokoro** (English, Hindi): `models/kokoro/kokoro-v1.0.int8.onnx` and `voices-v1.0.bin`
- **AI4Bharat Indic Parler-TTS** (Kannada, Tamil, Telugu, Bengali, Marathi): `models/indic-parler-tts`;
  needs `pip install torch transformers git+https://github.com/huggingface/parler-tts.git`

Recommended — **IndicTrans2** for fast, accurate translation (otherwise Ollama is used). Accept the terms at
[huggingface.co/ai4bharat/indictrans2-en-indic-dist-200M](https://huggingface.co/ai4bharat/indictrans2-en-indic-dist-200M), then:
```bash
.venv/bin/hf auth login
.venv/bin/hf download ai4bharat/indictrans2-en-indic-dist-200M
```

## Run
```bash
./scripts/run.sh          # open http://localhost:8000   (another port: BLU_PORT=8001 ./scripts/run.sh)
```
Run **one** server at a time: each loads the voice model (~3.6 GB RAM) and they share Ollama.
All settings (models, voices, languages, export) live in `config.yaml`.

Tests: `PYTHONPATH=. .venv/bin/python tests/test_documents.py` (also `test_translate.py`, `test_language_and_themes.py`)
and `cd frontend && npm test`.

## Licence
This repository has no licence file yet — add a `LICENSE` before you publish or share it.

It uses third-party models and tools under their own licences; check each before redistribution:

| Component | Used for | Licence |
|-----------|----------|---------|
| Qwen2.5-3B / Llama 3.2-3B (via Ollama) | lesson planning, translation fallback | Qwen research licence / Llama 3.2 Community Licence |
| Ollama | local LLM runtime | MIT |
| Kokoro-82M, kokoro-onnx | English & Hindi voices | Apache-2.0 |
| AI4Bharat Indic Parler-TTS | Indian-language voices | Apache-2.0 |
| AI4Bharat IndicTrans2 | translation | MIT |
| eSpeak NG | fallback voices, phonemes | GPL-3.0 |
| Poppler (`pdftotext`) | PDF text extraction | GPL-2.0 |
| FFmpeg / x264 | MP4 and audiobook encoding | LGPL / GPL (depends on build) |
| Noto Sans fonts | Indic captions | SIL OFL 1.1 |
| React, Vite, FastAPI, Playwright | app | MIT / MIT / MIT / Apache-2.0 |
