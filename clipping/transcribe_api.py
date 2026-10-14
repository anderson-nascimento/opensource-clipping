"""
clipping.transcribe_api — Transcription through a hosted Whisper API

Sends only the audio (mono, 16 kHz, compressed) to an OpenAI-compatible
``/audio/transcriptions`` endpoint and returns the same
``(full_transcript, transcript_segments)`` pair as
``engine.transcribe_video``. Long audio is split into ~20 minute chunks,
cut at silences, and the word timestamps are shifted back by each chunk's
offset.

If the API fails, ``transcribe_with_fallback`` falls back to local Whisper,
so the pipeline never stops because of the API.
"""

import json
import os
import re
import subprocess
import tempfile
import time

import requests

# Hourly price in USD, used only to print an estimate. Providers bill at least
# MIN_BILLED_SECONDS per request.
PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/audio/transcriptions",
        "env_key": "GROQ_API_KEY",
        "default_model": "whisper-large-v3",
        "price_per_hour": {
            "whisper-large-v3": 0.111,
            "whisper-large-v3-turbo": 0.04,
        },
        "min_billed_seconds": 10,
    },
}

AUDIO_BITRATE = "48k"  # 20 min ≈ 7 MB, well under the 25 MB upload limit
SILENCE_SEARCH_WINDOW = 60.0  # look for a silence up to this far from each cut
MAX_RETRIES = 4
MAX_RETRY_WAIT = 60.0


def _run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def _probe_duration(path: str) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=True,
    )
    return float(out.stdout.strip())


def extract_audio(video_path: str, audio_path: str, start: float = 0.0, duration: float | None = None) -> str:
    """Extract mono 16 kHz MP3 audio (optionally a [start, start+duration] slice)."""
    args = []
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    if duration is not None:
        args += ["-t", f"{duration:.3f}"]
    args += [
        "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", AUDIO_BITRATE, audio_path,
    ]
    _run_ffmpeg(["-loglevel", "error", *args])
    return audio_path


def _detect_silences(audio_path: str) -> list[tuple[float, float]]:
    proc = _run_ffmpeg([
        "-i", audio_path, "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-",
    ])
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", proc.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    return list(zip(starts, ends))


def plan_chunks(duration: float, chunk_seconds: float, silences: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """
    Split [0, duration] into (start, end) chunks of about *chunk_seconds*,
    moving each cut to the middle of the nearest silence within
    SILENCE_SEARCH_WINDOW seconds, so no word is cut in half.
    """
    if duration <= chunk_seconds:
        return [(0.0, duration)]

    mids = [(s + e) / 2 for s, e in silences]
    chunks = []
    start = 0.0
    while duration - start > chunk_seconds:
        target = start + chunk_seconds
        near = [m for m in mids if start + 1.0 < m and abs(m - target) <= SILENCE_SEARCH_WINDOW]
        cut = min(near, key=lambda m: abs(m - target)) if near else target
        chunks.append((start, cut))
        start = cut
    chunks.append((start, duration))
    return chunks


def _post_with_retry(url: str, api_key: str, audio_path: str, fields: list[tuple[str, str]]) -> dict:
    for attempt in range(1, MAX_RETRIES + 1):
        with open(audio_path, "rb") as f:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                data=fields,
                files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
                timeout=600,
            )
        if resp.status_code == 200:
            return resp.json()

        retryable = resp.status_code == 429 or resp.status_code >= 500
        if not retryable or attempt == MAX_RETRIES:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            wait = float(resp.headers.get("retry-after", ""))
        except ValueError:
            wait = 2.0 ** attempt
        if wait > MAX_RETRY_WAIT:
            raise RuntimeError(
                f"HTTP {resp.status_code}: rate limited for {wait:.0f}s — {resp.text[:200]}"
            )
        print(f"      ⏳ HTTP {resp.status_code}, retrying in {wait:.0f}s ({attempt}/{MAX_RETRIES})...", flush=True)
        time.sleep(wait)
    raise RuntimeError("unreachable")


def group_words(segments: list[dict], max_words_per_subtitle: int) -> tuple[str, list[dict]]:
    """
    Turn Whisper-style segments ``{start, end, text, words: [{word, start, end}]}``
    into ``(full_transcript, transcript_segments)``, the same shape as
    ``engine.transcribe_video``: subtitle chunks never cross a segment boundary.
    """
    full_transcript = ""
    transcript_segments: list[dict] = []
    for seg in segments:
        full_transcript += f"[{seg['start']:.1f} - {seg['end']:.1f}] {seg['text'].strip()}\n"
        words = seg.get("words") or []
        for i in range(0, len(words), max_words_per_subtitle):
            chunk = words[i:i + max_words_per_subtitle]
            transcript_segments.append({
                "start": chunk[0]["start"],
                "end": chunk[-1]["end"],
                "words": chunk,
            })
    return full_transcript, transcript_segments


def _attach_words(response: dict, offset: float) -> list[dict]:
    """Assign the response's top-level word list to its segments, shifted by *offset*."""
    words = [
        {
            "word": w["word"].strip(),
            "start": round(w["start"] + offset, 3),
            "end": round(w["end"] + offset, 3),
        }
        for w in response.get("words") or []
        if w.get("word", "").strip()
    ]
    raw_segments = response.get("segments") or []
    if not raw_segments:
        text = response.get("text", "")
        if not words:
            return []
        raw_segments = [{"start": 0.0, "end": words[-1]["end"] - offset, "text": text}]

    segments = []
    wi = 0
    for si, s in enumerate(raw_segments):
        seg_end = s["end"] + offset
        last = si == len(raw_segments) - 1
        seg_words = []
        # A word belongs to the segment it starts in; the last segment takes the rest.
        while wi < len(words) and (last or words[wi]["start"] < seg_end):
            seg_words.append(words[wi])
            wi += 1
        segments.append({
            "start": round(s["start"] + offset, 3),
            "end": round(seg_end, 3),
            "text": s.get("text", ""),
            "words": seg_words,
        })
    return segments


def transcribe_api(
    video_path: str,
    provider: str = "groq",
    model: str | None = None,
    max_words_per_subtitle: int = 5,
    chunk_minutes: float = 20.0,
    language: str | None = None,
    raw_output_path: str | None = None,
) -> tuple[str, list[dict]]:
    """
    Transcribe *video_path* through a hosted Whisper API.

    Raises on any failure (missing key, HTTP error, empty result) so the
    caller can fall back to local Whisper.
    """
    spec = PROVIDERS[provider]
    api_key = os.environ.get(spec["env_key"], "")
    if not api_key:
        raise RuntimeError(f"{spec['env_key']} is not set (add it to .env)")
    model = model or spec["default_model"]

    print(f"[2/3] Starting transcription via API ({provider}, {model})...")
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix="clip_audio_") as tmp:
        full_audio = extract_audio(video_path, os.path.join(tmp, "audio.mp3"))
        duration = _probe_duration(full_audio)
        chunk_seconds = chunk_minutes * 60
        silences = _detect_silences(full_audio) if duration > chunk_seconds else []
        chunks = plan_chunks(duration, chunk_seconds, silences)
        print(
            f"      🎧 Audio: {duration / 60:.1f} min, "
            f"{os.path.getsize(full_audio) / 1e6:.1f} MB, {len(chunks)} chunk(s)",
            flush=True,
        )

        fields = [
            ("model", model),
            ("response_format", "verbose_json"),
            ("timestamp_granularities[]", "word"),
            ("timestamp_granularities[]", "segment"),
            ("temperature", "0"),
        ]
        if language:
            fields.append(("language", language))

        segments: list[dict] = []
        raw_responses = []
        billed = 0.0
        for idx, (start, end) in enumerate(chunks, 1):
            if len(chunks) == 1:
                audio = full_audio
            else:
                audio = extract_audio(full_audio, os.path.join(tmp, f"chunk_{idx}.mp3"), start, end - start)
            print(f"      📤 Chunk {idx}/{len(chunks)} [{start:.0f}s - {end:.0f}s]...", flush=True)
            response = _post_with_retry(spec["url"], api_key, audio, fields)
            raw_responses.append({"offset": start, "response": response})
            segments.extend(_attach_words(response, start))
            billed += max(end - start, spec["min_billed_seconds"])

    if raw_output_path:
        with open(raw_output_path, "w", encoding="utf-8") as f:
            json.dump(raw_responses, f, ensure_ascii=False, indent=2)

    full_transcript, transcript_segments = group_words(segments, max_words_per_subtitle)
    if not transcript_segments:
        raise RuntimeError("the API returned no word timestamps")

    price = spec["price_per_hour"].get(model)
    cost = f", est. cost US$ {billed / 3600 * price:.4f}" if price is not None else ""
    print(
        f"      ✅ {len(transcript_segments)} subtitle chunks in {time.time() - t0:.0f}s{cost}",
        flush=True,
    )
    return full_transcript, transcript_segments


def _cache_path(video_path: str) -> str:
    return video_path + ".transcript.json"


def _cache_key(video_path: str, cfg) -> dict:
    """
    What the transcript depends on: the video file (its download source, size and
    mtime) and the transcription settings. Any change makes the cached copy stale.
    """
    try:
        with open(video_path + ".source", encoding="utf-8") as f:
            source = f.read()
    except OSError:
        source = ""
    st = os.stat(video_path)
    transcriber = getattr(cfg, "transcriber", "local")
    return {
        "source": source,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "transcriber": transcriber,
        "model": (getattr(cfg, "transcriber_model", None) if transcriber != "local" else cfg.whisper_model),
        "language": getattr(cfg, "transcriber_language", None),
        "max_words_per_subtitle": cfg.max_words_per_subtitle,
    }


def load_cached_transcript(video_path: str, cfg):
    """The saved ``(full_transcript, transcript_segments)`` for this video and settings, or None."""
    try:
        with open(_cache_path(video_path), encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("key") == _cache_key(video_path, cfg) and cached.get("segments"):
            return cached["full_transcript"], cached["segments"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def save_cached_transcript(video_path: str, cfg, full_transcript: str, segments: list[dict]) -> None:
    try:
        with open(_cache_path(video_path), "w", encoding="utf-8") as f:
            json.dump(
                {"key": _cache_key(video_path, cfg), "full_transcript": full_transcript, "segments": segments},
                f, ensure_ascii=False,
            )
    except OSError as e:
        print(f"⚠️ Could not save the transcript cache: {e}")


def transcribe_with_fallback(video_path: str, cfg, whisper_model_instance=None) -> tuple[str, list[dict]]:
    """
    Transcribe with ``cfg.transcriber`` ("local" or an API provider); if the
    API fails, fall back to local Faster-Whisper.

    The result is cached next to the video, so a re-run of the same video with the
    same settings (e.g. after the AI analysis failed) does not pay for the API again.
    """
    if getattr(cfg, "transcript_cache", True):
        cached = load_cached_transcript(video_path, cfg)
        if cached:
            print(
                f"[2/3] ♻️ Reusing the saved transcript ({os.path.basename(_cache_path(video_path))}); "
                "pass --no-transcript-cache to transcribe again."
            )
            return cached

    full_transcript, segments = _transcribe(video_path, cfg, whisper_model_instance)
    if segments:
        save_cached_transcript(video_path, cfg, full_transcript, segments)
    return full_transcript, segments


def _transcribe(video_path: str, cfg, whisper_model_instance=None) -> tuple[str, list[dict]]:
    from . import engine

    transcriber = getattr(cfg, "transcriber", "local")
    if transcriber != "local":
        outputs_dir = getattr(cfg, "outputs_dir", None)
        try:
            return transcribe_api(
                video_path,
                provider=transcriber,
                model=getattr(cfg, "transcriber_model", None),
                max_words_per_subtitle=cfg.max_words_per_subtitle,
                chunk_minutes=getattr(cfg, "transcriber_chunk_minutes", 20.0),
                language=getattr(cfg, "transcriber_language", None),
                raw_output_path=(
                    os.path.join(outputs_dir, "transcription_api_raw.json") if outputs_dir else None
                ),
            )
        except Exception as e:
            print(f"⚠️ API transcription failed ({e}); falling back to local Whisper.")

    return engine.transcribe_video(
        video_path,
        max_words_per_subtitle=cfg.max_words_per_subtitle,
        model_size=cfg.whisper_model,
        device=cfg.whisper_device,
        compute_type=cfg.whisper_compute_type,
        whisper_model_instance=whisper_model_instance,
    )
