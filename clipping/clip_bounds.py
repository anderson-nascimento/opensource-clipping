"""
clip_bounds — Snap AI clip boundaries to sentence boundaries using word timestamps.

The AI picks start/end times from segment-level timestamps and usually returns round
seconds, so clips often open mid-sentence ("...bem prático.") or end on the first word of
the next sentence or one word short of the last one. This module moves each boundary to
the nearest sentence edge, with limits so a clip never grows or shrinks by much:

  start  -> back to the start of the sentence it cuts into (up to MAX_START_BACK), else
            forward to the next sentence start (up to MAX_START_FWD)
  end    -> forward to the end of the sentence it cuts (up to MAX_END_FWD_SHORT); if that
            is further, back to the previous sentence end when the dangling fragment is
            short (up to MAX_END_BACK); else forward up to MAX_END_FWD

A sentence ends at a word ending in . ? ! … or before a pause of SENTENCE_PAUSE seconds.
Starts get a small lead-in and ends a short tail, never overlapping a neighbouring word.
"""

import re

SENTENCE_PAUSE = 0.7      # a silence this long also ends a sentence
MAX_START_BACK = 5.0
MAX_START_FWD = 3.0
MAX_END_FWD_SHORT = 2.5   # extend to finish the sentence when it ends this soon
MAX_END_BACK = 3.0        # otherwise drop a dangling fragment up to this long
MAX_END_FWD = 6.0
LEAD_IN = 0.2
TAIL = 0.35
_TERMINAL = re.compile(r"[.?!…]+[\"'”»)]*$")
_TRAILING_OFF = re.compile(r"(\.\.\.|…)[\"'”»)]*$")


def flat_words(transcript_segments: list[dict]) -> list[dict]:
    """[{text, start, end}] in time order from Whisper/JSON3 segments."""
    words = []
    for seg in transcript_segments:
        for w in seg.get("words", []):
            text = str(w.get("word", w.get("text", ""))).strip()
            if text:
                words.append({"text": text, "start": float(w["start"]), "end": float(w["end"])})
    words.sort(key=lambda w: w["start"])
    return words


def _mid(w: dict) -> float:
    return (w["start"] + w["end"]) / 2


def _ends_sentence(words: list[dict], i: int) -> bool:
    if _TERMINAL.search(words[i]["text"]):
        return True
    return i + 1 < len(words) and words[i + 1]["start"] - words[i]["end"] >= SENTENCE_PAUSE


def _strong_end(words: list[dict], i: int) -> bool:
    """A sentence end that is a good place to finish a clip (not a trailing '...')."""
    return _ends_sentence(words, i) and not _TRAILING_OFF.search(words[i]["text"])


def _start_at(words: list[dict], k: int, floor: float) -> float:
    t = words[k]["start"] - LEAD_IN
    if k > 0:
        t = max(t, words[k - 1]["end"] + 0.02)
    return max(t, floor)


def _end_at(words: list[dict], j: int, ceil: float | None) -> float:
    t = words[j]["end"] + TAIL
    if j + 1 < len(words):
        t = min(t, words[j + 1]["start"] - 0.02)
    return min(t, ceil) if ceil is not None else t


def snap_start(words: list[dict], t: float, floor: float = 0.0) -> float:
    """Start time moved to a sentence start (see module docstring)."""
    k = next((i for i, w in enumerate(words) if _mid(w) >= t), None)   # first word mostly inside
    if k is None:
        return t
    if k == 0 or _ends_sentence(words, k - 1):
        return _start_at(words, k, floor)
    back = next((s for s in range(k - 1, -1, -1) if s == 0 or _ends_sentence(words, s - 1)), None)
    if back is not None and t - words[back]["start"] <= MAX_START_BACK and words[back]["start"] >= floor:
        return _start_at(words, back, floor)
    fwd = next((s for s in range(k + 1, len(words)) if _ends_sentence(words, s - 1)), None)
    if fwd is not None and words[fwd]["start"] - t <= MAX_START_FWD:
        return _start_at(words, fwd, floor)
    return _start_at(words, k, floor)


def snap_end(words: list[dict], t: float, ceil: float | None = None) -> float:
    """End time moved to a sentence end (see module docstring)."""
    j = next((i for i in range(len(words) - 1, -1, -1) if _mid(words[i]) < t), None)   # last word mostly inside
    if j is None:
        return t
    if _strong_end(words, j):
        return _end_at(words, j, ceil)
    fwd = next((e for e in range(j, len(words)) if _strong_end(words, e)), None)
    back = next((e for e in range(j - 1, -1, -1) if _strong_end(words, e)), None)
    fwd_cost = words[fwd]["end"] - t if fwd is not None else float("inf")
    back_cost = t - words[back]["end"] if back is not None else float("inf")
    if fwd_cost <= MAX_END_FWD_SHORT:
        return _end_at(words, fwd, ceil)
    if back_cost <= MAX_END_BACK:
        return _end_at(words, back, ceil)
    if fwd_cost <= MAX_END_FWD:
        return _end_at(words, fwd, ceil)
    return _end_at(words, j, ceil)


def snap_clips(clips: list[dict], transcript_segments: list[dict]) -> None:
    """Snap start/end (and keep_segments) of every clip in place; prints what changed."""
    words = flat_words(transcript_segments)
    if not words:
        return
    print("\n✂️ Snapping clip cuts to sentence boundaries...")
    for clip in clips:
        old = (float(clip["start_time"]), float(clip["end_time"]))
        segs = clip.get("keep_segments") or []
        if len(segs) > 1:
            snapped = []
            for seg in segs:
                s = snap_start(words, float(seg["start_time"]))
                e = snap_end(words, float(seg["end_time"]))
                if snapped and s <= snapped[-1]["end_time"]:
                    snapped[-1]["end_time"] = round(max(snapped[-1]["end_time"], e), 2)   # merged after snapping
                elif e > s:
                    snapped.append({"start_time": round(s, 2), "end_time": round(e, 2)})
            clip["keep_segments"] = snapped
            start, end = snapped[0]["start_time"], snapped[-1]["end_time"]
        else:
            start, end = snap_start(words, old[0]), snap_end(words, old[1])
            if segs:
                clip["keep_segments"] = [{"start_time": round(start, 2), "end_time": round(end, 2)}]
        if end - start < 3.0:   # never let snapping collapse a clip
            continue
        clip["start_time"], clip["end_time"] = round(start, 2), round(end, 2)
        # keep the v1 hook teaser inside the clip if it was inside before
        for key in ("hook_start_time", "hook_end_time"):
            if key in clip and old[0] <= float(clip[key]) <= old[1]:
                clip[key] = round(min(max(float(clip[key]), clip["start_time"]), clip["end_time"]), 2)
        if (clip["start_time"], clip["end_time"]) != old:
            print(f"   Rank {clip.get('rank')}: {old[0]:.2f}–{old[1]:.2f} → "
                  f"{clip['start_time']:.2f}–{clip['end_time']:.2f}")
