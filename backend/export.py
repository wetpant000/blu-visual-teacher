"""Stage 5: MP4 export. Playwright screenshots the React player frame by frame (deterministic time seek),
FFmpeg muxes frames + padded scene audio. Captions are rendered in the frames; an .srt is written too."""
import asyncio, base64, json, logging, math, os, shutil, subprocess, tempfile, time
from pathlib import Path
from .config import CFG, DATA
from . import pipeline, tts

log = logging.getLogger("export")


def _ts(t):
    return f"{int(t // 3600):02}:{int(t % 3600 // 60):02}:{int(t % 60):02},{int(t % 1 * 1000):03}"


def _video_filter():
    # camera motion and scene transitions are already drawn by the stage itself, so frames only need a fade-in
    return "fade=t=in:st=0:d=0.25,format=yuv420p"


def _validate_export(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not video or video.get("codec_name") != "h264" or video.get("width") != 1280 or video.get("height") != 720:
        raise RuntimeError("MP4 export did not contain the expected 720p H.264 video stream")
    if not audio or audio.get("codec_name") != "aac":
        raise RuntimeError("MP4 export did not contain the expected AAC audio stream")


async def _render(url, n, fps, out, report):
    """Screenshot every frame. Frames are pure functions of time, so several isolated pages render
    separate frame ranges in parallel; CDP screenshots skip Playwright's slower screenshot pipeline."""
    from playwright.async_api import async_playwright
    workers = max(1, min(CFG["export"].get("workers") or max(1, (os.cpu_count() or 2) // 3), 8, n))
    done = 0

    async def work(browser, frames):
        nonlocal done
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()
        await page.goto(url)
        await page.wait_for_function("window.__ready === true", timeout=60000)
        cdp = await context.new_cdp_session(page)
        for f in frames:
            await page.evaluate("t => window.__seek(t)", f / fps)
            shot = await cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 85, "optimizeForSpeed": True})
            (out / f"{f:05d}.jpg").write_bytes(base64.b64decode(shot["data"]))
            done += 1
            if done % 8 == 0:
                report("rendering", done / n)
        await context.close()

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        size = math.ceil(n / workers)
        await asyncio.gather(*(work(browser, range(k, min(n, k + size))) for k in range(0, n, size)))
        await browser.close()


def export(i, lang, report):
    d = DATA / "lessons" / i / lang
    lesson = json.loads((d / "lesson.json").read_text(encoding="utf-8"))
    if any(not s.get("audio") for s in lesson["scenes"]):
        # browser-voiced lesson: an MP4 needs real audio, so record it once with a fast local voice
        lesson = pipeline.localize(i, lang, tts.fast_voice(lang)["id"], lambda stage, progress=None: report(
            "recording voice" if stage == "narrating" else stage, progress))
    scenes = lesson["scenes"]
    total = sum(s["duration"] for s in scenes)
    fps, base = CFG["export"]["fps"], CFG["export"]["frontend_url"].rstrip("/")

    srt, at = [], 0.0
    for k, s in enumerate(scenes):
        srt.append(f"{k + 1}\n{_ts(at)} --> {_ts(at + s['audio_seconds'])}\n{s['narration']}\n")
        at += s["duration"]
    (d / "lesson.srt").write_text("\n".join(srt), encoding="utf-8")

    tmp, n = Path(tempfile.mkdtemp()), math.ceil(total * fps)
    try:
        report("rendering", 0)
        t0 = time.time()
        asyncio.run(_render(f"{base}/#/render/{i}/{lang}", n, fps, tmp, report))
        log.info("rendered %d frames in %.1fs", n, time.time() - t0)
        report("encoding", None)
        video_filter = _video_filter()
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", str(tmp / "%05d.jpg")]
        for s in scenes:
            cmd += ["-i", str(DATA / s["audio"].split("?")[0][len("/data/"):])]
        fc = f"[0:v]{video_filter}[v];"
        fc += ";".join(f"[{k + 1}:a]aresample=24000,aformat=channel_layouts=mono,apad=whole_dur={s['duration']:.3f}[a{k}]"
                       for k, s in enumerate(scenes))
        fc += ";" + "".join(f"[a{k}]" for k in range(len(scenes))) + f"concat=n={len(scenes)}:v=0:a=1[a]"
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                "-preset", "veryfast", "-tune", "animation", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-shortest", "-movflags", "+faststart",
                str(d / "lesson.mp4")]
        t0 = time.time()
        subprocess.run(cmd, check=True)
        log.info("encoded in %.1fs", time.time() - t0)
        _validate_export(d / "lesson.mp4")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    v = int(time.time())
    return {"mp4": f"/data/lessons/{i}/{lang}/lesson.mp4?v={v}", "srt": f"/data/lessons/{i}/{lang}/lesson.srt?v={v}"}
