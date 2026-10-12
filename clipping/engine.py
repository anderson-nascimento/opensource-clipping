"""
clipping.engine — Download, Transcription & Gemini AI Analysis

Maps to Cell 2 (The Engine) of the notebook.
"""

import json
import os
import re
import time

from yt_dlp import YoutubeDL
from faster_whisper import WhisperModel


# ==============================================================================
# TAHAP 1: DOWNLOAD VIDEO
# ==============================================================================

def _build_ydl_format_selector(download_source_height: str | int) -> str:
    """
    Build a yt-dlp format selector string for source-quality preference.
    """
    # Skip AV1 codec as it lacks HW acceleration on many platforms (e.g., Colab T4)
    # and causes decoding failures in OpenCV/FFmpeg software fallbacks.
    # Note: Using [vcodec!*=av01] to safely ensure it does not contain 'av01' anywhere.
    codec_filter = "[vcodec!*=av01]"

    if download_source_height == "max":
        return f"bestvideo{codec_filter}+bestaudio/best{codec_filter}"

    try:
        h_val = int(download_source_height)
    except (ValueError, TypeError):
        h_val = 0

    if 0 < h_val <= 1080:
        # For standard resolutions, strictly prefer native MP4 (H.264/AAC), ensuring no AV1 in mp4
        return (
            f"bestvideo[height<=?{h_val}][ext=mp4]{codec_filter}+bestaudio[ext=m4a]/"
            f"bestvideo[height<=?{h_val}]{codec_filter}+bestaudio/"
            f"best[height<=?{h_val}][ext=mp4]{codec_filter}/"
            f"best[height<=?{h_val}]{codec_filter}"
        )

    return (
        f"bestvideo[height<=?{download_source_height}]{codec_filter}+bestaudio/"
        f"best[height<=?{download_source_height}]{codec_filter}"
    )


_PLATFORM_LABELS = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "gdrive": "Google Drive",
}


def _extract_gdrive_file_id(url: str) -> str | None:
    """Extract the Google Drive file ID from various URL formats."""
    import re as _re
    m = _re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = _re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    return None


def _download_gdrive(url: str, output_path: str) -> None:
    """Download a video from Google Drive using gdown (more reliable than yt-dlp)."""
    import gdown

    file_id = _extract_gdrive_file_id(url)
    if not file_id:
        raise RuntimeError(
            f"Could not extract file ID from Google Drive URL: {url}\n"
            "      Supported formats:\n"
            "        • https://drive.google.com/file/d/FILE_ID/view\n"
            "        • https://drive.google.com/open?id=FILE_ID"
        )

    download_url = f"https://drive.google.com/uc?id={file_id}"
    print(f"      📥 File ID: {file_id}")
    gdown.download(download_url, output_path, quiet=False)


def _ydl_progress_hook(d: dict) -> None:
    """Render satu baris progress bar download dari data hook yt-dlp.

    yt-dlp mengunduh stream video dan audio secara terpisah, jadi hook ini
    dipanggil untuk masing-masing; newline saat "finished" menjaga tiap bar
    berada di barisnya sendiri.
    """
    status = d.get("status")
    if status == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)
        speed = d.get("speed")
        eta = d.get("eta")
        spd = f"{speed / 1024 / 1024:4.1f}MB/s" if speed else "  --MB/s"
        eta_s = f"{eta:>3}s" if eta is not None else " --s"
        if total:
            pct = downloaded / total * 100
            filled = int(20 * downloaded / total)
            bar = "█" * filled + " " * (20 - filled)
            print(
                f"\r      Unduh: {pct:3.0f}%|{bar}| "
                f"{downloaded / 1048576:.0f}/{total / 1048576:.0f}MB {spd} ETA {eta_s}   ",
                end="", flush=True,
            )
        else:
            # Ukuran tidak diketahui (live/streamed manifest) — tampilkan byte + speed saja.
            print(
                f"\r      Unduh: {downloaded / 1048576:.0f}MB {spd}   ",
                end="", flush=True,
            )
    elif status == "finished":
        print(flush=True)  # tutup baris bar untuk stream ini


def _apply_cookies(opts: dict, cookies: str | None) -> None:
    """Apply cookies to yt-dlp options dictionary."""
    if not cookies:
        return
    if cookies.endswith(".txt") or os.path.isfile(cookies):
        opts["cookiefile"] = cookies
    else:
        # Browser format: (browser, profile, keyring, container)
        opts["cookiesfrombrowser"] = (cookies, None, None, None)


def download_video(
    url: str,
    output_path: str,
    use_dlp_subs: bool = False,
    download_source_height: str | int = "max",
    source_platform: str = "youtube",
    yt_cookies: str | None = None,
) -> None:
    """
    Download a video to *output_path* with configurable source height.

    Parameters
    ----------
    source_platform : str
        One of ``"youtube"`` (default), ``"tiktok"``, ``"instagram"``,
        or ``"gdrive"``.
    """
    platform_label = _PLATFORM_LABELS.get(source_platform, source_platform)
    uses_youtube_format = source_platform == "youtube"

    print(f"[1/3] Downloading video from {platform_label}...")
    if download_source_height == "max":
        print("      🎯 Source quality: highest available", flush=True)
    else:
        print(f"      🎯 Source quality: up to {download_source_height}p", flush=True)

    # --- Google Drive: use gdown instead of yt-dlp ---
    if source_platform == "gdrive":
        _download_gdrive(url, output_path)
        if not os.path.exists(output_path):
            raise RuntimeError(
                f"❌ Google Drive download failed — file not found at {output_path}"
            )
        print(f"      ✅ Video downloaded successfully from Google Drive.", flush=True)
        return

    # --- Build yt-dlp options per platform ---
    if uses_youtube_format:
        # YouTube: complex format selector + AV1 filter + remote components
        ydl_opts = {
            "format": _build_ydl_format_selector(download_source_height),
            "outtmpl": output_path,
            "quiet": True,
            "merge_output_format": "mp4",
            "remote_components": ["ejs:github"],
            "progress_hooks": [_ydl_progress_hook],
            "extractor_args": {"youtube": ["player_client=android,web"]},
        }
    else:
        # TikTok / Instagram: ensure video and audio are merged
        # We explicitly prefer H.264 over H.265 (TikTok's bytevc1) to prevent 
        # PyAV/faster-whisper from crashing with IndexError on Kaggle/Colab.
        ydl_opts = {
            "format": "bestvideo[vcodec^=h264]+bestaudio/best[vcodec^=h264]/best",
            "outtmpl": output_path,
            "quiet": True,
            "merge_output_format": "mp4",
            "progress_hooks": [_ydl_progress_hook],
        }

    _apply_cookies(ydl_opts, yt_cookies)

    # --- Subtitle download — only supported for YouTube ---
    if use_dlp_subs and uses_youtube_format:
        print("      Looking for automatic subtitles (en / id)...")
        import glob

        for lang in ["en", "id"]:
            ydl_opts_subs = ydl_opts.copy()
            ydl_opts_subs.update({
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [lang],
                "subtitlesformat": "json3",
                "skip_download": True,  # Hanya fokus download subtitle
            })

            try:
                with YoutubeDL(ydl_opts_subs) as ydl:
                    ydl.download([url])

                # Cek apakah json3 untuk bahasa ini benar-benar terdownload
                if glob.glob(output_path.replace(".mp4", f".*.json3")):
                    print(f"      ✅ Subtitle '{lang}' found. Continuing to video...")
                    break
            except Exception as e:
                print(f"      ⚠️ Failed to fetch subtitle '{lang}' ({e}). Trying next option...")
    elif use_dlp_subs and not uses_youtube_format:
        print(f"      ℹ️ {platform_label} does not provide automatic subtitles. Whisper will be used.")

    # Jalankan download video terpisah dari urusan subtitle
    with YoutubeDL(ydl_opts) as ydl:
        # Extra step to verify resolution before downloading
        try:
            info = ydl.extract_info(url, download=False)
            best_h = info.get("height", "unknown")
            v_codec = info.get("vcodec", "unknown")
            print(f"      ✅ Downloading: {best_h}p (Codec: {v_codec})", flush=True)
        except Exception as e:
            print(f"      ⚠️ Failed to check detailed info: {e}", flush=True)

        ydl.download([url])

    # --- Post-download verification ---
    if not os.path.exists(output_path):
        raise RuntimeError(
            f"❌ Download from {platform_label} failed — video file not found at {output_path}.\n"
            "      Make sure the URL is valid and publicly accessible."
        )


# ==============================================================================
# TAHAP 2: TRANSKRIPSI WHISPER & JSON3 FALLBACK
# ==============================================================================

def parse_youtube_json3_subs(json_path: str, max_words_per_subtitle: int = 5) -> tuple[str, list[dict]]:
    """
    Parse downloaded YouTube JSON3 subtitles into full_transcript and transcript_segments.
    Returns empty string/list if parsing fails.
    """
    import json

    print("[2/3] Processing JSON3 subtitles from YouTube...")
    full_transcript = ""
    transcript_segments = []

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            subs_data = json.load(f)

        events = subs_data.get("events", [])

        flat_words = []
        for event in events:
            # YouTube timestamps are in ms
            t_start = event.get("tStartMs", 0) / 1000.0
            d_duration = event.get("dDurationMs", 0) / 1000.0
            event_end = t_start + d_duration

            segs = event.get("segs", [])
            for i, seg in enumerate(segs):
                text = seg.get("utf8", "")
                if not text.strip() or text == "\n":
                    continue

                # tOffsetMs is offset from t_start
                offset = seg.get("tOffsetMs", 0) / 1000.0
                seg_start = t_start + offset

                # Determine end of this segment
                if i < len(segs) - 1:
                    next_offset = segs[i + 1].get("tOffsetMs", 0) / 1000.0
                    seg_end = t_start + next_offset
                else:
                    seg_end = event_end

                if seg_end <= seg_start:
                    seg_end = seg_start + 1.0  # Fallback duration

                # Clean up YouTube subtitle artifacts
                clean_text = text.replace("\n", " ").replace("\u200b", "").strip()
                # Remove HTML tags (e.g., <i>, </i>, <b>, </b>, <font color="...">)
                clean_text = re.sub(r"<[^>]+>", "", clean_text)
                # Remove YouTube annotation brackets: [Music], [Applause], [Laughter], etc.
                clean_text = re.sub(r"\[[\w\s]+\]", "", clean_text)
                # Remove speaker change markers: >> 
                clean_text = re.sub(r">>\s*", "", clean_text)
                # Remove music symbols: ♪, ♫, etc.
                clean_text = re.sub(r"[♪♫♬♩]", "", clean_text)
                # Remove leading dashes often used for speaker identification
                clean_text = re.sub(r"^\s*-\s+", "", clean_text)
                # Collapse multiple spaces into one
                clean_text = re.sub(r"\s{2,}", " ", clean_text).strip()

                if clean_text:
                    # Memecah teks menjadi kata tunggal agar karaoke per-kata bekerja seperti whisper
                    words_in_seg = clean_text.split()
                    if not words_in_seg:
                        continue

                    duration_per_word = (seg_end - seg_start) / len(words_in_seg)

                    for w_idx, w_text in enumerate(words_in_seg):
                        w_start = seg_start + (w_idx * duration_per_word)
                        w_end = w_start + duration_per_word

                        flat_words.append({
                            "word": w_text,
                            "start": w_start,
                            "end": w_end,
                        })

        # Adjust end times based on the start time of the next word to prevent overlaps
        for i in range(len(flat_words) - 1):
            if flat_words[i]["end"] > flat_words[i + 1]["start"]:
                flat_words[i]["end"] = max(flat_words[i]["start"] + 0.1, flat_words[i + 1]["start"])

        # Group them into segments
        chunk_words = []
        chunk_start = 0.0

        for i, w in enumerate(flat_words):
            if len(chunk_words) == 0:
                chunk_start = w["start"]

            chunk_words.append(w)

            if len(chunk_words) == max_words_per_subtitle or i == len(flat_words) - 1:
                chunk_text = " ".join([cw["word"] for cw in chunk_words])
                chunk_end = w["end"]
                full_transcript += f"[{chunk_start:.1f} - {chunk_end:.1f}] {chunk_text}\n"

                transcript_segments.append({
                    "start": chunk_start,
                    "end": chunk_end,
                    "words": chunk_words,
                })
                chunk_words = []

        return full_transcript, transcript_segments

    except Exception as e:
        print(f"⚠️ Failed to parse JSON3: {e}")
        return "", []


def transcribe_video(
    video_path: str,
    max_words_per_subtitle: int = 5,
    model_size: str = "large-v3",
    device: str = "cuda",
    compute_type: str = "float16",
    whisper_model_instance=None,
) -> tuple[str, list[dict]]:
    """
    Transcribe *video_path* using Faster-Whisper.

    Parameters
    ----------
    whisper_model_instance : WhisperModel or None
        Optional pre-loaded WhisperModel instance.  When provided the
        *model_size*, *device* and *compute_type* arguments are ignored
        and the given model is reused directly.  This avoids expensive
        re-loading when processing multiple videos in batch mode.

    Returns
    -------
    full_transcript : str
        Human-readable transcript with timestamps.
    transcript_segments : list[dict]
        Word-level segments grouped by *max_words_per_subtitle*.
    """
    print("[2/3] Starting transcription with Faster-Whisper (word level)...")

    # Langkah-langkah ini berjalan tanpa output di dalam faster-whisper sebelum
    # segmen pertama dihasilkan, jadi kita umumkan tiap fase — kalau tidak, run
    # pertama di CPU (download model + decode seluruh audio) terlihat seperti hang.
    if whisper_model_instance is not None:
        model = whisper_model_instance
        print(
            f"      ♻️ Reusing already loaded Whisper model (batch mode)",
            flush=True,
        )
    else:
        print(
            f"      ⏳ Loading Whisper model '{model_size}' ({device})"
            " — the first download may take a while...",
            flush=True,
        )
        model = WhisperModel(model_size, device=device, compute_type=compute_type)

    print("      ⏳ Decoding audio & extracting features (no output yet)...", flush=True)
    segments, info = model.transcribe(video_path, beam_size=5, word_timestamps=True)

    full_transcript = ""
    transcript_segments: list[dict] = []

    # Progress bar berdasarkan timestamp audio. faster-whisper men-stream segmen
    # secara lazy, jadi bar dimajukan ke waktu akhir tiap segmen saat tiba.
    from tqdm import tqdm

    total_dur = round(info.duration, 2)
    progress = tqdm(
        total=total_dur,
        unit="s",
        desc="      Transcription",
        bar_format="{desc}: {percentage:3.0f}%|{bar}| {n:.0f}/{total:.0f}s [{elapsed}<{remaining}]",
    )

    for segment in segments:
        # Clamp agar floating-point drift melewati durasi tidak overshoot.
        progress.update(min(segment.end, total_dur) - progress.n)
        full_transcript += f"[{segment.start:.1f} - {segment.end:.1f}] {segment.text}\n"

        if segment.words:
            chunk_words: list[dict] = []
            chunk_start = 0.0

            for i, w in enumerate(segment.words):
                if len(chunk_words) == 0:
                    chunk_start = w.start

                chunk_words.append({
                    "word": w.word.strip(),
                    "start": w.start,
                    "end": w.end,
                })

                if len(chunk_words) == max_words_per_subtitle or i == len(segment.words) - 1:
                    transcript_segments.append({
                        "start": chunk_start,
                        "end": w.end,
                        "words": chunk_words,
                    })
                    chunk_words = []

    progress.update(total_dur - progress.n)  # snap ke 100% saat selesai
    progress.close()
    return full_transcript, transcript_segments


# ==============================================================================
# TAHAP 3: ANALISIS GEMINI AI
# ==============================================================================

TARGET_ACCOUNTS = {
    "Business": {
        "target_account": "Business.Mereska",
        "angle_desc": "When the angle is business, brand, revenue, sales, founder, marketing, or small businesses (SMEs).",
        "bio": "Business insights, founder stories & local brands. Business | Founder | Finance | Beauty | Marketing"
    },
    "Life": {
        "target_account": "Life.Mereska",
        "angle_desc": "When the angle is personal life, lifestyle, skincare, career, mindset, relationships, personal finance, or self growth.",
        "bio": "Insight clips to upgrade your life & mindset. Podcast | Career | Finance | Beauty | Self Growth"
    },
    "Creator": {
        "target_account": "Creator.Mereska",
        "angle_desc": "When the angle is digital content, AI, affiliate, tools, clipping, monetization, or ways to make money from content.",
        "bio": "Digging into digital content so it can make money. AI | Affiliate | Clips | Tools | Monetize"
    },
    "Muslim": {
        "target_account": "Muslim.Mereska",
        "angle_desc": "When the angle is religion, sustenance (rezeki), prayer, worship, working for Allah, Islamic family, or business with Islamic values.",
        "bio": "Reminders about work, sustenance & living with Islamic values. Islamic | Rezeki | Family | Work | Business"
    }
}

def _build_account_classification_prompt() -> str:
    lines = []
    for acc_type, data in TARGET_ACCOUNTS.items():
        lines.append(f"- {acc_type}: {data['angle_desc']}")
        lines.append(f"  (target_account: \"{data['target_account']}\", bio: \"{data['bio']}\")")
    return "\n".join(lines)


# ---- Retry Config ----
MAX_ATTEMPTS = 10
INITIAL_WAIT_SECONDS = 60
WAIT_INCREMENT_SECONDS = 30
REQUEST_TIMEOUT_MS = 15 * 60 * 1000  # 15 menit
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def _extract_status_code(exc: Exception):
    for attr in ("status_code", "code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)

    match = re.search(r"\b(408|429|500|502|503|504)\b", str(exc))
    return int(match.group(1)) if match else None


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, json.JSONDecodeError):
        return True

    code = _extract_status_code(exc)
    if code in RETRYABLE_STATUS_CODES:
        return True

    msg = str(exc).lower()
    keywords = (
        "timeout", "temporarily unavailable", "deadline",
        "connection reset", "connection aborted", "service unavailable",
    )
    return any(k in msg for k in keywords)


def _generate_json_with_retry(client, model, fallback_model, contents, config):
    last_exc = None
    status_code = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            print(f"[Gemini] Attempt {attempt}/{MAX_ATTEMPTS}...")

            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

            text = getattr(response, "text", None)
            if not text or not text.strip():
                raise ValueError("Gemini returned an empty response.text.")

            return json.loads(text)

        except Exception as exc:
            last_exc = exc
            status_code = _extract_status_code(exc)
            retryable = _is_retryable(exc)

            print(
                f"[Gemini] Attempt {attempt}/{MAX_ATTEMPTS} failed | "
                f"status={status_code} | error={exc}"
            )

            if (not retryable) or attempt == MAX_ATTEMPTS:
                break

            wait_seconds = INITIAL_WAIT_SECONDS + ((attempt - 1) * WAIT_INCREMENT_SECONDS)
            print(f"[Gemini] Retrying in {wait_seconds} seconds...")
            time.sleep(wait_seconds)

    print(f"[Gemini] Attempt with primary model ({model}) failed.")
    if fallback_model:
        print(f"[Gemini] Trying once more with fallback model ({fallback_model})...")
        try:
            response = client.models.generate_content(
                model=fallback_model,
                contents=contents,
                config=config,
            )
            text = getattr(response, "text", None)
            if not text or not text.strip():
                raise ValueError("Gemini fallback returned an empty response.text.")

            return json.loads(text)
        except Exception as exc_fallback:
            print(f"[Gemini] Fallback model failed | error={exc_fallback}")
            raise RuntimeError(
                f"Failed to call Gemini primary & fallback. "
                f"Primary report status={status_code}, error={last_exc} | "
                f"Fallback report error={exc_fallback}"
            ) from exc_fallback

    raise RuntimeError(
        f"Failed to call Gemini after {MAX_ATTEMPTS} attempts. Last error: {last_exc}"
    ) from last_exc


# ==== KONFIGURASI DURASI KLIP ====
# Ubah nilai di bawah ini jika ingin mengganti batas durasi klip (dalam detik)
MIN_CLIP_DURATION = 20
MAX_CLIP_DURATION = 179

def get_analysis_prompt(full_transcript: str, num_clips: int, hook_duration: int, cfg=None) -> str:
    """Centralized prompt for both Gemini and NVIDIA providers."""
    # Build optional Hook V2 prompt section
    _hook_v2_prompt = ""
    if cfg and getattr(cfg, "hook_v2", False):
        _hook_v2_items = getattr(cfg, "hook_v2_items", 3)
        _hook_v2_style = getattr(cfg, "hook_v2_style", "controversial_fast_glitch")
        _hook_v2_prompt = f"""

HOOK V2 (MULTI-HOOK INTRO — REQUIRED):
- Besides the standard hook, also create "hook_v2" containing {_hook_v2_items} short cuts (0.5-2 seconds) taken from the most striking/controversial/emotional moments inside the clip.
- Style: {_hook_v2_style}
- Each item must contain: start_time, end_time, and text (short on-screen text of 2-5 words).
- Items must be ordered from strongest to weakest.
- Transitions between items will be added automatically (white flash / glitch) by the system.
- Fill the "hook_v2" field as an object with:
  - "enabled": true
  - "items": array of objects (start_time, end_time, text)
  - "transition": object with "type" ("white_flash" or "glitch")
"""

    # Build optional Segment Trimming prompt section
    _segment_prompt = ""
    if cfg and not getattr(cfg, "no_segment_trim", False):
        _silence_hint = ""
        if cfg and getattr(cfg, "silence_trim", False):
            _silence_hint = "\n- AGGRESSIVELY remove silent parts/silence/dead air. Do not include pauses longer than 0.5 seconds."
        _segment_prompt = f"""

SEGMENT-BASED TRIMMING (KEEP SEGMENTS — REQUIRED):
- For each clip, analyze whether there are less interesting, too quiet, rambling, or filler parts in the middle.
- If so, split the clip into several "keep_segments" — only the best parts are kept.
- Each segment contains: start_time and end_time.
- Segments must be in chronological order and must not overlap.
- If the whole clip is already dense and engaging, just create 1 segment covering the entire duration.{_silence_hint}
- Fill the "keep_segments" field as an array of objects (start_time, end_time).
"""
    return f"""
You are an Art Director, Video Editor, and Short-Form Content Metadata Strategist for TikTok, Reels, and YouTube Shorts.

Read the following video transcript. Transcript format:
[start_second - end_second] text

MAIN TASK:
- Find the {num_clips} most interesting, strongest, most shareable moments with the highest viral potential to turn into short clips.
- Sort the clips by viral_score from highest (most viral potential) to lowest. The "rank" is only a sequence number (1, 2, 3...).
- For each clip, produce the clip timing, hook, typography plan, b-roll plan, selection reason, cross-platform metadata, and target account classification.
- All output must be highly relevant to the clip's content, not to the full video in general.

CLIP SELECTION & VIRALITY RULES:
- Clip duration must be {MIN_CLIP_DURATION}-{MAX_CLIP_DURATION} seconds.
- Pick parts with emotion, conflict, surprise, insight, strong opinions, practical lessons, or a clear punchline.
- Evaluate the viral strength (virality) and give a "viral_score" (1-100) representing how viral a clip could be.
  - 90-100: Very high FYP/viral potential, strong emotion/conflict, very punchy hook.
  - 80-89: Interesting, likely to perform well.
  - 70-79: Standard, informative but maybe lacking punch.
- Prioritize parts that stay interesting even when watched without the full video's context.
- Avoid clips whose content is too similar to each other.
- Do not pick clips that feel flat, rambling, or have no clear payoff.

RETENTION & CLIP STRUCTURE RULES:
- Make sure the first 3 seconds of the clip have a strong pull: a hook, conflict, curiosity, a sharp statement, emotion, or an implicit question.
- An ideal clip has the structure:
  hook -> short context -> tension/insight -> payoff.
- Do not pick clips that only become interesting after running too long.
- If the beginning of the segment is too slow, move start_time to a stronger sentence.
- If the payoff is already over, do not extend the clip without a reason.
- Do not include intros, small talk, long pauses, or transitions that do not add appeal.
- Prioritize clips that make viewers want to:
  1. stop scrolling,
  2. watch until the end,
  3. comment,
  4. share,
  5. save,
  6. or feel "this is so me".

TIMING CUT RULES:
- start_time must begin as close as possible to the first strong moment, not just the start of the topic.
- end_time must stop after the payoff, conclusion, punchline, or main emotional beat is finished.
- Do not cut too early if a sentence is still unfinished.
- Do not continue the clip too long after the core message is finished.
- The clip must still be understandable without watching the parts before or after it.
- If two strong moments are very close together and support each other, they may be merged as long as the duration stays {MIN_CLIP_DURATION}-{MAX_CLIP_DURATION} seconds.
- If two strong moments have different angles, split them into separate clip candidates.

INTERNAL VIRAL_SCORE ASSESSMENT:
Score viral_score 1-100 based on the following components. This is only for internal assessment, DO NOT add new fields to the JSON.
- Hook strength: 1-20
- Emotional intensity: 1-20
- Shareability/comment potential: 1-20
- Standalone clarity: 1-20
- Payoff/retention: 1-20

Scoring guide:
- Hook strength: how strongly the first 3 seconds make people stop scrolling.
- Emotional intensity: how strong the emotion, conflict, frustration, humor, emotional touch, anger, awe, or relatability is.
- Shareability/comment potential: how likely people are to comment, debate, tag friends, share, or save.
- Standalone clarity: how easy the clip is to understand without the full video's context.
- Payoff/retention: how clear the reward for watching until the end is, such as a punchline, insight, twist, conclusion, or practical lesson.
- Do not pick clips with a viral_score below 70 unless the number of good moments in the transcript is very limited.

TARGET ACCOUNT CLASSIFICATION (FOR EACH CLIP):
Determine the target account based on the clip's video ANGLE. Do not judge only by topic (e.g. beauty does not automatically go to Life). Judge by angle:
{_build_account_classification_prompt()}

SPECIAL CLASSIFICATION RULES:
1. Beauty does not automatically go to Life. (Talking about revenue/brand -> Business. Review/lifestyle -> Life. Affiliate/content -> Creator).
2. Finance does not automatically go to Business. (Talking about revenue/business -> Business. Personal finance/saving -> Life. How to make finance content -> Creator).
3. Owner stories are split by angle. (Brand struggle -> Business. Personal/family life -> Life. Sustenance/worship -> Muslim).

HOOK (REQUIRED):
- Take the single punchiest sentence that IS INSIDE the clip.
- The hook must feel strong and grab attention within the first ~{hook_duration} seconds.
- Save it as hook_start_time and hook_end_time.
- The hook must make people want to keep watching, but no fake clickbait.
- Make sure the hook is natural and actually spoken in the transcript.
- If the best hook is not right at the start of the clip candidate, adjust start_time so the hook appears as early as possible.
- The hook must work as on-screen opening text to keep viewers in the first 3 seconds.

TYPOGRAPHY PLAN (KINETIC TYPOGRAPHY):
- Pick the 3-6 most weighty, emotional, or emphasis-worthy SINGLE words from each clip.
- For each word, specify:
  1. 'main_word': that specific word, spelled exactly as in the transcript.
  2. 'scale_level': choose 1, 2, or 3.
     - 1 = normal/small
     - 2 = large/emphasis
     - 3 = huge/very crucial
  3. 'style': choose "main" or "special".
  4. 'animation': choose "bounce_pop" or "stagger_up".
- Do not pick long phrases. Single words only.
- Prioritize the words that are strongest in emotion, meaning, or visual retention.

B-ROLL (REQUIRED IF RELEVANT):
- Find at most 1-3 moments in the clip that are a great fit for inserting B-roll / stock footage.
- Each B-roll lasts 3-7 seconds.
- Provide:
  - start_time
  - end_time
  - search_query
- search_query must be short, clear, and in English.
- Do not place B-roll at the exact same seconds as the hook.
- Only add B-roll if it truly helps visualize what is being said.
- If there is no suitable moment, fill broll_list with an empty array [].

VISUAL B-ROLL HOOK (FIRST 0-3 SECONDS):
- Give 2-5 opening B-roll ideas that are contrasting, funny, dramatic, or spark curiosity before the original video starts.
- Include YouTube/TikTok search keywords for the editor.
- If there is a gesture that can be used as a visual hook, also give a reference for it.
- This is stored in the 'recommended_visual_broll_hook' object and only serves as a reference if the editor wants to find footage manually for the first 3 seconds.

BGM MOOD (BACKGROUND MUSIC):
- Analyze the emotion and topic of this clip.
- Pick ONE background music mood that fits best from this fixed list: [chill, epic, sad, upbeat, suspense].
- Make sure the mood matches the story. (Example: a story of hard struggle = sad/epic, a funny/relaxed story = chill/upbeat).

SLOW CLOSING:
- end_time MUST include padding of +0.10 to +0.85 seconds after the last word so the ending feels relaxed and is not cut off abruptly.

SELECTION REASON:
- Fill the 'reason' field with a short explanation of why this clip is worth picking.
- Focus on emotional value, hook strength, retention potential, shareability, and payoff.
- Explain the main viral trigger of this clip.
- Explain why people are likely to watch until the end.
- Explain why this clip stays interesting even when watched without the full video's context.

METADATA LANGUAGE RULES:
- title_indonesian is still required for internal compatibility / fallback.
- title_indonesian MUST be in natural Indonesian and at most 100 characters.
- All main cross-platform metadata must be in natural English.
- This applies to:
  - title_english
  - hashtags
  - description_hook
  - description_context
  - keyword_tags
  - tiktok_caption
- Specifically for the Indonesian TikTok version, also create:
  - tiktok_title_id
  - tiktok_caption_id
- tiktok_title_id and tiktok_caption_id MUST be in natural Indonesian.
- tiktok_title_id must be more descriptive than title_indonesian, may be longer than 100 characters if needed, and must clearly explain what the clip/video is about.
- tiktok_caption_id must be natural, informative, suitable for an Indonesian audience, and may be a bit longer if that helps explain the clip.
- Do not mix Indonesian and English within the same field.
- Use English that is natural, concise, easy to read, and suitable for short-form content.
- Avoid stiff literal translations.

CROSS-PLATFORM METADATA:
For each clip, produce the following metadata:

1. title_indonesian
- Natural, short, and relevant Indonesian.
- This is only for internal compatibility / fallback.
- At most 100 characters.

2. title_english
- Natural, strong, sharp, and easy-to-read English.
- This is the main title for platform metadata.
- At most 100 characters.
- Focus on 1 main idea.
- Relevant to the clip's content, not the full video in general.
- No cheap clickbait.
- Do not overuse capital letters.
- Avoid excessive punctuation like !!! ??? ...
- Do not be too generic.

3. hashtags
- Fill with only 2 to 3 hashtags in a single string.
- All hashtags MUST be in English.
- Separate them with spaces.
- Must be directly relevant to the clip's topic.
- No duplicates.
- Avoid overly generic hashtags like #fyp #viral #trending unless they are truly very relevant.
- Use a format like: #mindset #career #productivity

4. description_hook
- Exactly 1 sentence.
- MUST be in English.
- This is the opening sentence of the metadata.
- Must be short, strong, and spark curiosity.
- No fake clickbait.

5. description_context
- Exactly 1 sentence.
- MUST be in English.
- Concisely explains the main context of the clip.
- Must be relevant to what is said in the clip.

6. keyword_tags
- Contains 5 to 8 short keywords.
- MUST be in English.
- Not hashtags.
- Must be a list of short phrases relevant to the clip's content.
- Avoid keyword spam.
- Prioritize keywords that people would actually search for.
- This field is mainly for YouTube metadata.

7. tiktok_title_id
- Natural Indonesian.
- Longer and more descriptive of the video's content than title_indonesian.
- Does not need to be limited to 100 characters, but must still be concise, clear, and easy to read.
- Must be relevant to the clip's content, not the long video in general.
- No cheap clickbait.

8. tiktok_caption_id
- 1 to 2 sentences.
- MUST be in Indonesian.
- May be a bit longer than the English caption if that helps explain the clip.
- Natural, light, and easy-to-read style.
- Stay true to the clip's content.
- Do not just copy-paste the title.
- Do not be too formal.

9. tiktok_caption
- 1 to 2 short sentences.
- MUST be in English.
- A more natural, light, and conversational style.
- Stay true to the clip's content.
- Do not just copy-paste the title.
- Do not be too formal.
- Try not to exceed 140 characters.

METADATA QUALITY RULES:
- All metadata must match the clip's content, not the long video in general.
- Do not make promises that are not discussed in the clip.
- Do not use fake hyperbole like "100% guaranteed", "you'll definitely get rich", etc. unless it is very clearly stated.
- If there are numbers, strong phrases, or sharp statements in the original speech, prioritize them as inspiration for the title/caption.
- Title, descriptions, and caption must complement each other, not repeat the same sentence.
- All metadata fields used for platforms must be in natural English, not stiff literal translations.
- Specifically for tiktok_title_id and tiktok_caption_id, use natural, clear Indonesian that better explains the clip for an Indonesian audience.

OUTPUT RULES:
- The output MUST be a valid JSON array.
- Do not give any explanation outside the JSON.
- All fields are required.
- When in doubt, prioritize accuracy to the clip's content over excessive creativity.
{_hook_v2_prompt}{_segment_prompt}

REQUIRED JSON STRUCTURE (follow these field names strictly):
[
  {{
    "rank": 1,
    "viral_score": 95,
    "start_time": 30.5,
    "end_time": 90.0,
    "hook_start_time": 30.5,
    "hook_end_time": 35.0,
    "bgm_mood": "mood_here",
    "typography_plan": [{{ "main_word": "...", "scale_level": 2, "style": "main", "animation": "bounce_pop" }}],
    "broll_list": [{{ "start_time": 40.0, "end_time": 45.0, "search_query": "..." }}],
    "recommended_visual_broll_hook": [
      {{ "broll_idea": "...", "search_keyword": "...", "why_it_works": "..." }}
    ],
    "hook_v2": {{
      "enabled": true,
      "items": [{{ "start_time": 31.0, "end_time": 32.5, "text": "KEY WORD" }}],
      "transition": {{ "type": "white_flash" }}
    }},
    "keep_segments": [
      {{ "start_time": 30.5, "end_time": 55.0 }},
      {{ "start_time": 58.0, "end_time": 90.0 }}
    ],
    "title_indonesian": "...",
    "title_english": "...",
    "hashtags": "#hashtag1 #hashtag2",
    "description_hook": "...",
    "description_context": "...",
    "keyword_tags": ["tag1", "tag2"],
    "tiktok_title_id": "...",
    "tiktok_caption_id": "...",
    "tiktok_caption": "...",
    "reason": "...",
    "account_classification": {{
      "account_type": "Creator",
      "target_account": "Creator.Mereska",
      "confidence": 87,
      "main_angle": "Monetizing digital content from the beauty niche",
      "reason": "...",
      "supporting_keywords": ["affiliate", "monetization"],
      "account_bio": "...",
      "alternative_account": {{
        "account_type": "Life",
        "target_account": "Life.Mereska",
        "reason": "..."
      }}
    }}
  }}
]

Transcript:
{full_transcript}
"""


def analyze_with_nvidia(full_transcript: str, cfg) -> list[dict]:
    """Analyze transcript using NVIDIA NIM API (OpenAI compatible)."""
    from openai import OpenAI
    
    print(f"[3/3] Analyzing top {cfg.num_clips} moments using NVIDIA ({cfg.nvidia_model})...")
    
    if not cfg.api_key_nvidia:
        raise ValueError("NVIDIA_API_KEY not found in environment.")

    client = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=cfg.api_key_nvidia
    )
    
    prompt = get_analysis_prompt(full_transcript, cfg.num_clips, cfg.hook_duration, cfg=cfg)
    
    # Define the strict schema for Guided JSON (NVIDIA NIM specific)
    clips_schema = {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "rank": {"type": "integer"},
                "viral_score": {"type": "integer"},
                "start_time": {"type": "number"},
                "end_time": {"type": "number"},
                "hook_start_time": {"type": "number"},
                "hook_end_time": {"type": "number"},
                "bgm_mood": {
                    "type": "string",
                    "enum": ["chill", "epic", "sad", "upbeat", "suspense"]
                },
                "typography_plan": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "main_word": {"type": "string"},
                            "scale_level": {"type": "integer", "enum": [1, 2, 3]},
                            "style": {"type": "string", "enum": ["main", "special"]},
                            "animation": {"type": "string", "enum": ["bounce_pop", "stagger_up"]}
                        },
                        "required": ["main_word", "scale_level", "style", "animation"]
                    }
                },
                "broll_list": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "start_time": {"type": "number"},
                            "end_time": {"type": "number"},
                            "search_query": {"type": "string"}
                        },
                        "required": ["start_time", "end_time", "search_query"]
                    }
                },
                "recommended_visual_broll_hook": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "broll_idea": {"type": "string"},
                            "search_keyword": {"type": "string"},
                            "why_it_works": {"type": "string"}
                        },
                        "required": ["broll_idea", "search_keyword", "why_it_works"]
                    }
                },
                "title_indonesian": {"type": "string"},
                "title_english": {"type": "string"},
                "hashtags": {"type": "string"},
                "description_hook": {"type": "string"},
                "description_context": {"type": "string"},
                "keyword_tags": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "tiktok_title_id": {"type": "string"},
                "tiktok_caption_id": {"type": "string"},
                "tiktok_caption": {"type": "string"},
                "reason": {"type": "string"},
                "account_classification": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "account_type": {"type": "string", "enum": list(TARGET_ACCOUNTS.keys())},
                        "target_account": {"type": "string"},
                        "confidence": {"type": "integer"},
                        "main_angle": {"type": "string"},
                        "reason": {"type": "string"},
                        "supporting_keywords": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "account_bio": {"type": "string"},
                        "alternative_account": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "account_type": {"type": "string", "enum": list(TARGET_ACCOUNTS.keys())},
                                "target_account": {"type": "string"},
                                "reason": {"type": "string"}
                            },
                            "required": ["account_type", "target_account", "reason"]
                        }
                    },
                    "required": ["account_type", "target_account", "confidence", "main_angle", "reason", "supporting_keywords", "account_bio", "alternative_account"]
                },
                "hook_v2": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "start_time": {"type": "number"},
                                    "end_time": {"type": "number"},
                                    "text": {"type": "string"},
                                },
                                "required": ["start_time", "end_time", "text"],
                            },
                        },
                        "transition": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "type": {"type": "string", "enum": ["white_flash", "glitch"]},
                            },
                            "required": ["type"],
                        },
                    },
                    "required": ["enabled", "items", "transition"],
                },
                "keep_segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "start_time": {"type": "number"},
                            "end_time": {"type": "number"},
                        },
                        "required": ["start_time", "end_time"],
                    },
                },
            },
            "required": [
                "rank", "viral_score", "start_time", "end_time", "hook_start_time", "hook_end_time",
                "bgm_mood", "typography_plan", "broll_list", "recommended_visual_broll_hook", "title_indonesian",
                "title_english", "hashtags", "description_hook", "description_context",
                "keyword_tags", "tiktok_title_id", "tiktok_caption_id", "tiktok_caption",
                "reason", "account_classification", "hook_v2", "keep_segments"
            ]
        }
    }

    completion = client.chat.completions.create(
        model=cfg.nvidia_model,
        messages=[
            {"role": "system", "content": "You are a professional video editor and strategist. Return JSON only. Follow the provided JSON schema exactly."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.5,
        top_p=1,
        max_tokens=16384,
        extra_body={
            "chat_template_kwargs": {"thinking": False},
            "nvext": {
                "guided_json": clips_schema
            }
        }
    )
    
    content = completion.choices[0].message.content
    
    if "```" in content:
        content = re.sub(r"```(json)?", "", content).strip()
        content = content.split("```")[0].strip()
        
    result = json.loads(content)
    
    # Guided JSON should return an array directly if schema says type: array
    # but we keep the unwrapper just in case of non-conforming fallbacks
    if isinstance(result, dict):
        for key in ["clips", "data", "highlights"]:
            if key in result and isinstance(result[key], list):
                result = result[key]
                break
                
    if not isinstance(result, list):
        if isinstance(result, dict):
            return [result]
        raise ValueError(f"NVIDIA provider returned a non-list/dict format: {type(result)}")
        
    return result


def analyze_with_ai(full_transcript: str, cfg) -> list[dict]:
    """Dispatcher for AI analysis based on provider."""
    provider = getattr(cfg, "ai_provider", "gemini")
    
    if provider == "nvidia":
        if not cfg.api_key_nvidia:
            print("⚠️ NVIDIA_API_KEY not found! Falling back to Gemini...")
        else:
            try:
                return analyze_with_nvidia(full_transcript, cfg)
            except Exception as e:
                print(f"⚠️ NVIDIA API failed: {e}. Falling back to Gemini...")
    
    return analyze_with_gemini(full_transcript, cfg)


def analyze_with_gemini(
    full_transcript: str,
    cfg,
) -> list[dict]:
    """Analyse transcript with Gemini AI."""
    import google.genai as genai
    from google.genai import types

    print(f"[3/3] Analyzing top {cfg.num_clips} best moments using Gemini...")

    prompt = get_analysis_prompt(full_transcript, cfg.num_clips, cfg.hook_duration, cfg=cfg)

    # JSON Schema definitions (same as before)
    schema_broll = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "start_time": {"type": "NUMBER"},
                "end_time": {"type": "NUMBER"},
                "search_query": {"type": "STRING"},
            },
            "required": ["start_time", "end_time", "search_query"],
        },
    }

    schema_visual_broll_hook = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "broll_idea": {"type": "STRING"},
                "search_keyword": {"type": "STRING"},
                "why_it_works": {"type": "STRING"},
            },
            "required": ["broll_idea", "search_keyword", "why_it_works"],
        },
    }

    schema_typography = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "main_word": {"type": "STRING"},
                "scale_level": {"type": "INTEGER"},
                "style": {"type": "STRING"},
                "animation": {"type": "STRING"},
            },
            "required": ["main_word", "scale_level", "style", "animation"],
        },
    }

    classification_schema = {
        "type": "OBJECT",
        "properties": {
            "account_type": {"type": "STRING"},
            "target_account": {"type": "STRING"},
            "confidence": {"type": "INTEGER"},
            "main_angle": {"type": "STRING"},
            "reason": {"type": "STRING"},
            "supporting_keywords": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "account_bio": {"type": "STRING"},
            "alternative_account": {
                "type": "OBJECT",
                "properties": {
                    "account_type": {"type": "STRING"},
                    "target_account": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                },
                "required": ["account_type", "target_account", "reason"],
            },
        },
        "required": ["account_type", "target_account", "confidence", "main_angle", "reason", "supporting_keywords", "account_bio", "alternative_account"],
    }

    client = genai.Client(
        api_key=cfg.api_key_gemini,
        http_options=types.HttpOptions(
            timeout=REQUEST_TIMEOUT_MS,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )

    gemini_config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema={
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "rank": {"type": "INTEGER"},
                    "viral_score": {"type": "INTEGER"},
                    "hook_start_time": {"type": "NUMBER"},
                    "hook_end_time": {"type": "NUMBER"},
                    "start_time": {"type": "NUMBER"},
                    "end_time": {"type": "NUMBER"},
                    "typography_plan": schema_typography,
                    "broll_list": schema_broll,
                    "recommended_visual_broll_hook": schema_visual_broll_hook,
                    "reason": {"type": "STRING"},
                    "bgm_mood": {"type": "STRING"},
                    "title_indonesian": {"type": "STRING"},
                    "title_english": {"type": "STRING"},
                    "hashtags": {"type": "STRING"},
                    "description_hook": {"type": "STRING"},
                    "description_context": {"type": "STRING"},
                    "keyword_tags": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                    },
                    "tiktok_title_id": {"type": "STRING"},
                    "tiktok_caption_id": {"type": "STRING"},
                    "tiktok_caption": {"type": "STRING"},
                    "account_classification": classification_schema,
                    "hook_v2": {
                        "type": "OBJECT",
                        "properties": {
                            "enabled": {"type": "BOOLEAN"},
                            "items": {
                                "type": "ARRAY",
                                "items": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "start_time": {"type": "NUMBER"},
                                        "end_time": {"type": "NUMBER"},
                                        "text": {"type": "STRING"},
                                    },
                                    "required": ["start_time", "end_time", "text"],
                                },
                            },
                            "transition": {
                                "type": "OBJECT",
                                "properties": {
                                    "type": {"type": "STRING"},
                                },
                                "required": ["type"],
                            },
                        },
                        "required": ["enabled", "items", "transition"],
                    },
                    "keep_segments": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "start_time": {"type": "NUMBER"},
                                "end_time": {"type": "NUMBER"},
                            },
                            "required": ["start_time", "end_time"],
                        },
                    },
                },
                "required": [
                    "rank", "viral_score", "hook_start_time", "hook_end_time",
                    "start_time", "end_time", "typography_plan",
                    "broll_list", "recommended_visual_broll_hook", "reason", "bgm_mood",
                    "title_indonesian", "title_english", "hashtags",
                    "description_hook", "description_context",
                    "keyword_tags", "tiktok_title_id",
                    "tiktok_caption_id", "tiktok_caption",
                    "account_classification", "hook_v2", "keep_segments",
                ],
            },
        },
    )

    return _generate_json_with_retry(
        client=client,
        model=cfg.gemini_model,
        fallback_model=getattr(cfg, "gemini_fallback_model", None),
        contents=prompt,
        config=gemini_config,
    )