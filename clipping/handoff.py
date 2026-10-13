"""
handoff — Export rendered clips as HyperFrames handoff packages for a Claude Code chat.

With --handoff the pipeline renders each clip clean (reframed to 9:16, no captions, hook,
B-roll, music, watermark or glow) and writes one self-contained package per clip:

  outputs/handoff/rank_<n>/
    BRIEF.md                 what the clip is about + delivery contract (read first)
    STYLE.md                 editing rules (copied from assets/handoff/STYLE.md)
    transcript.json          clip-relative words [{text, start, end}]; no re-transcription
    metadata.json            ffprobe of public/input-video.mp4
    context.json             AI plan fields (title, hook, reason, keywords, B-roll ideas)
    assets/manifest.json     files shipped with the package + image requests for the chat
    public/input-video.mp4   the clean clip at 30 fps with a keyframe every second (seekable)
    public/fonts/, public/vendor/gsap.min.js, public/assets/sfx_*.mp3

The motion design is done in Claude Code with the project skill /clip-recut
(.claude/skills/clip-recut), which writes public/index.html and renders output.mp4.
Then `python -m clipping.handoff collect` points render_manifest.json at those renders so
the uploaders publish them.
"""

import json
import os
import shutil
import subprocess
import sys

import requests

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STYLE_TEMPLATE = os.path.join(REPO_DIR, "assets", "handoff", "STYLE.md")
CACHE_DIR = os.path.join(REPO_DIR, ".cache", "handoff")

FPS = 30
GSAP_URL = "https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js"
FONTS = {
    "Anton-Regular.ttf": "https://cdn.jsdelivr.net/fontsource/fonts/anton@latest/latin-400-normal.ttf",
    "Inter-Regular.ttf": "https://cdn.jsdelivr.net/fontsource/fonts/inter@latest/latin-400-normal.ttf",
    "Inter-Bold.ttf": "https://cdn.jsdelivr.net/fontsource/fonts/inter@latest/latin-700-normal.ttf",
}
# Sound effects bundled with the HyperFrames media-use skill (Pixabay Content License),
# pinned to a commit so the files never change under a package.
SFX_BASE = ("https://raw.githubusercontent.com/heygen-com/hyperframes/"
            "ab8a4da53f31653350632980fa63cb07e957f0f6/skills/media-use/audio/assets/sfx/")
SFX = {
    "whoosh-short": "fast cut into or out of a full-screen card",
    "whoosh-cinematic": "slower, heavier transition",
    "pop": "list item or small card popping in",
    "click-soft": "number, date or checkbox tick",
    "riser": "tension build before the climax (10s long; use data-media-start for the tail)",
    "impact-bass-1": "deep hit on the climax, once per clip",
    "sparkle": "positive reveal or result",
    "glitch-1": "tech or error moment",
}
MAX_IMAGE_REQUESTS = 6


def _download(url: str, path: str) -> bool:
    """Download url to path once (cached); False when it fails."""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        r = requests.get(url, timeout=60, headers={"User-Agent": "opensource-clipping"})
        r.raise_for_status()
        with open(path + ".part", "wb") as f:
            f.write(r.content)
        os.replace(path + ".part", path)
        return True
    except Exception as e:  # noqa: BLE001 — a missing optional asset must not stop the export
        print(f"   ⚠️ [Handoff] Download failed ({os.path.basename(path)}): {e}")
        return False


def _shared_assets() -> dict:
    """Fetch GSAP, fonts and SFX into the local cache; returns {relative public path: cached file}."""
    files = {}
    if _download(GSAP_URL, os.path.join(CACHE_DIR, "gsap.min.js")):
        files["vendor/gsap.min.js"] = os.path.join(CACHE_DIR, "gsap.min.js")
    for name, url in FONTS.items():
        if _download(url, os.path.join(CACHE_DIR, "fonts", name)):
            files[f"fonts/{name}"] = os.path.join(CACHE_DIR, "fonts", name)
    for name in SFX:
        cached = os.path.join(CACHE_DIR, "sfx", f"{name}.mp3")
        if _download(SFX_BASE + f"{name}.mp3", cached):
            files[f"assets/sfx_{name}.mp3"] = cached
    return files


def clip_ranges(clip: dict, cfg) -> list[tuple[float, float]]:
    """Source-time ranges that make up the rendered clip, in order (same rule as studio.process_clip)."""
    keep = clip.get("keep_segments")
    if keep and len(keep) > 1 and not getattr(cfg, "no_segment_trim", False):
        return [(float(s["start_time"]), float(s["end_time"])) for s in keep]
    return [(float(clip["start_time"]), float(clip["end_time"]))]


def to_clip_time(t: float, ranges: list[tuple[float, float]]) -> float | None:
    """Map a source timestamp to the clip timeline (ranges are joined back to back); None if cut out."""
    offset = 0.0
    for start, end in ranges:
        if start <= t < end:
            return offset + t - start
        offset += end - start
    return None


def clip_words(transcript_segments: list[dict], ranges: list[tuple[float, float]]) -> list[dict]:
    """Flat word list on the clip timeline: [{text, start, end}], the format HyperFrames skills read."""
    words = []
    for seg in transcript_segments:
        for w in seg.get("words", []):
            text = str(w.get("word", w.get("text", ""))).strip()
            start = to_clip_time(float(w["start"]), ranges)
            if not text or start is None:
                continue
            # clamp the end to the range the word starts in
            range_end = next(e for s, e in ranges if s <= float(w["start"]) < e)
            end = to_clip_time(min(float(w["end"]), range_end - 1e-3), ranges)
            words.append({"text": text, "start": round(start, 3), "end": round(max(end or start, start), 3)})
    return words


def _image_requests(clip: dict, ranges: list[tuple[float, float]]) -> list[dict]:
    """Image ideas from the AI plan, for the chat to generate (e.g. Nano Banana via google-flow)."""
    reqs = []
    for i, item in enumerate(clip.get("recommended_visual_broll_hook") or [], 1):
        if not isinstance(item, dict):
            continue
        reqs.append({
            "id": f"img_hook_{i}",
            "idea": item.get("broll_idea") or item.get("search_keyword", ""),
            "keyword": item.get("search_keyword", ""),
            "why": item.get("why_it_works", ""),
            "when": "hook (first ~3s)",
        })
    for i, item in enumerate(clip.get("broll_list") or [], 1):
        start = to_clip_time(float(item.get("start_time", -1)), ranges)
        if start is None:
            continue
        end = to_clip_time(float(item["end_time"]) - 1e-3, ranges)
        reqs.append({
            "id": f"img_broll_{i}",
            "idea": item.get("search_query", ""),
            "keyword": item.get("search_query", ""),
            "why": "B-roll moment chosen by the clip planner",
            "when": [round(start, 2), round(end if end is not None else start + 3, 2)],
        })
    return [r for r in reqs if r["idea"]][:MAX_IMAGE_REQUESTS]


def _probe(path: str) -> dict:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-show_entries", "format=duration", "-of", "json", path,
    ])
    return json.loads(out)


def _fmt_ts(sec: float) -> str:
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


def _brief(row: dict, clip: dict, ranges, duration: float, n_requests: int, cfg) -> str:
    title = row.get("title") or clip.get("title") or f"Highlight {row['rank']}"
    keywords = ", ".join(clip.get("keyword_tags") or []) or "-"
    type_words = ", ".join(t.get("main_word", "") for t in clip.get("typography_plan") or [] if t.get("main_word")) or "-"
    span = " + ".join(f"{_fmt_ts(s)}–{_fmt_ts(e)}" for s, e in ranges)
    source_url = row.get("source_url") or getattr(cfg, "url_youtube", "") or "-"
    return f"""---
workflow: clip-recut
mode: autonomous            # no questions; put decisions in DELIVERY.md
input: public/input-video.mp4
transcript: transcript.json # already clip-relative; do NOT transcribe again
aspect: "9:16"              # 1080x1920 portrait
duration: {duration:.3f}
style: STYLE.md             # rhythm/design rules, override the HyperFrames default pace
assets: assets/manifest.json
captions: inside-composition
language: {getattr(cfg, "content_lang", "en")}
max_image_credits: 0        # generate images only when they cost 0 credits
---

# Clip rank {row["rank"]} — "{title}"

Produced by opensource-clipping (`main.py --handoff`). Full AI plan in `context.json`.

**Hook line:** {clip.get("description_hook") or "-"}

**Context:** {clip.get("description_context") or "-"}

**Why it can go viral:** {clip.get("reason") or "-"}

**Keywords:** {keywords}

**Typography ideas from the planner:** {type_words}

**Image requests:** {n_requests} in `assets/manifest.json` → `requests` (optional; design without them if
images cannot be generated for free).

**Source:** {source_url}, clip at {span} of the source{" (segments joined back to back)" if len(ranges) > 1 else ""}.

**Delivery:** `output.mp4` (1080x1920, 30 fps, original audio) in this folder + `DELIVERY.md` with the
decisions, the images used and the render time.
"""


def export_package(row: dict, clip: dict, transcript_segments: list[dict], cfg) -> str | None:
    """Write the handoff package for one rendered clip; returns its directory (None on failure)."""
    video = row.get("video_path")
    if row.get("status") != "success" or not video or not os.path.exists(video):
        return None
    pkg = os.path.join(cfg.outputs_dir, "handoff", f"rank_{row['rank']}")
    public = os.path.join(pkg, "public")
    os.makedirs(os.path.join(pkg, "assets"), exist_ok=True)
    os.makedirs(public, exist_ok=True)

    # Re-encode with a keyframe every second: the HyperFrames renderer seeks the video per
    # frame, and a sparse GOP freezes the picture under the overlays.
    input_video = os.path.join(public, "input-video.mp4")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", video,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-r", str(FPS),
        "-g", str(FPS), "-keyint_min", str(FPS), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", input_video,
    ], check=True)

    meta = _probe(input_video)
    duration = float(meta["format"]["duration"])
    ranges = clip_ranges(clip, cfg)
    expected = sum(e - s for s, e in ranges)
    if abs(expected - duration) > 0.5:
        print(f"   ⚠️ [Handoff] Rank {row['rank']}: video is {duration:.2f}s but the clip ranges add up "
              f"to {expected:.2f}s; caption timing may drift.")

    words = clip_words(transcript_segments, ranges)
    requests_ = _image_requests(clip, ranges)

    shipped = []
    for rel, src in _shared_assets().items():
        dst = os.path.join(public, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        if rel.startswith("assets/sfx_"):
            name = rel[len("assets/sfx_"):-4]
            shipped.append({"id": f"sfx_{name}", "file": f"public/{rel}", "type": "sfx", "use": SFX[name]})
    shipped.append({"id": "font_anton", "file": "public/fonts/Anton-Regular.ttf", "type": "font", "use": "display"})
    shipped.append({"id": "font_inter", "file": "public/fonts/Inter-*.ttf", "type": "font", "use": "support text (400/700)"})

    context = {k: clip.get(k) for k in (
        "title", "description_hook", "description_context", "reason", "keyword_tags", "hashtags",
        "typography_plan", "recommended_visual_broll_hook", "bgm_mood", "viral_score",
    )}
    context["source"] = {"url": row.get("source_url") or getattr(cfg, "url_youtube", None),
                         "ranges": [[s, e] for s, e in ranges]}

    def dump(name, data):
        with open(os.path.join(pkg, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    dump("transcript.json", words)
    dump("metadata.json", meta)
    dump("context.json", context)
    dump(os.path.join("assets", "manifest.json"), {"files": shipped, "requests": requests_})
    shutil.copyfile(STYLE_TEMPLATE, os.path.join(pkg, "STYLE.md"))
    with open(os.path.join(pkg, "BRIEF.md"), "w", encoding="utf-8") as f:
        f.write(_brief(row, clip, ranges, duration, len(requests_), cfg))

    print(f"   📦 [Handoff] Rank {row['rank']}: {len(words)} words, {len(requests_)} image requests → {pkg}")
    return pkg


def export_all(render_manifest: list[dict], ai_clips: list[dict], transcript_segments: list[dict], cfg) -> None:
    """Export a package for every successful clip and record its folder in the manifest rows."""
    print("\n📦 [Handoff] Exporting HyperFrames packages...")
    clips = {c["rank"]: c for c in ai_clips}
    exported = []
    for row in render_manifest:
        clip = clips.get(row.get("rank"))
        if not clip:
            continue
        try:
            pkg = export_package(row, clip, transcript_segments, cfg)
        except Exception as e:  # noqa: BLE001 — one bad clip must not drop the others
            print(f"   ⚠️ [Handoff] Rank {row.get('rank')} failed: {e}")
            pkg = None
        if pkg:
            row["handoff_dir"] = pkg
            row["handoff_status"] = "pending"
            exported.append(pkg)
    if exported:
        rel = os.path.relpath(exported[0], REPO_DIR).replace(os.sep, "/")
        out_rel = os.path.relpath(cfg.outputs_dir, REPO_DIR).replace(os.sep, "/")
        print(f"\n✅ [Handoff] {len(exported)} package(s) ready. Next steps:")
        print(f"   1. In Claude Code, from the project folder: /clip-recut {rel}")
        print(f"   2. After the renders: python -m clipping.handoff collect {out_rel}")


def collect(outputs_dir: str) -> int:
    """Point render_manifest.json rows at their handoff renders (output.mp4); returns how many are ready."""
    manifest_path = os.path.join(outputs_dir, "render_manifest.json")
    with open(manifest_path, encoding="utf-8") as f:
        rows = json.load(f)
    ready = 0
    for row in rows:
        pkg = row.get("handoff_dir")
        if not pkg:
            continue
        out = os.path.join(pkg, "output.mp4")
        if os.path.exists(out) and os.path.getsize(out) > 0:
            row.setdefault("clean_video_path", row["video_path"])
            row["video_path"] = out
            row["video_exists"] = True
            row["handoff_status"] = "rendered"
            ready += 1
            print(f"   ✅ Rank {row['rank']}: {out}")
        else:
            row["handoff_status"] = "pending"
            print(f"   ⏳ Rank {row['rank']}: no output.mp4 yet in {pkg}")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"💾 {manifest_path}: {ready} handoff render(s) in use.")
    return ready


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "collect":
        print("Usage: python -m clipping.handoff collect [outputs_dir]")
        sys.exit(1)
    collect(os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else "outputs"))
