"""
caption_presets — Social-media caption styles rendered as ASS subtitles.

Presets (selected with --caption-style):
  - "box":        whole phrase in white, the spoken word gets a colored box behind it
                  (CapCut / Submagic look).
  - "yellow_pop": bold white phrase with a thick outline, the spoken word turns the
                  accent color and pops slightly bigger (Hormozi look).
  - "one_word":   a single large word at a time, popping in to the rhythm of speech
                  (MrBeast look). Words from the AI typography plan use the accent color.

The "classic" style is the original renderer in subtitles.py.
"""

import os
import string

from PIL import ImageFont

PRESETS = ("box", "yellow_pop", "one_word")

DEFAULT_ACCENTS = {
    "box": "#8B5CF6",        # violet box
    "yellow_pop": "#FFE600",  # yellow word
    "one_word": "#FFE600",    # yellow for emphasized words
}

WHITE = "&H00FFFFFF"
BLACK = "&H00000000"


def hex_to_ass(color: str) -> str:
    """Convert '#RRGGBB' into an ASS colour '&H00BBGGRR'."""
    c = color.strip().lstrip("#")
    if len(c) != 6:
        raise ValueError(f"Invalid color '{color}', expected #RRGGBB")
    r, g, b = c[0:2], c[2:4], c[4:6]
    return f"&H00{b}{g}{r}".upper()


def _libass_matched_font(font_path, font_sz):
    """Load a PIL font whose glyph widths match what libass renders at `font_sz`.

    libass scales a font so that ascent + descent equals the style Fontsize, while
    PIL scales by the em square. Fonts with tall line metrics (e.g. Anton) therefore
    render noticeably narrower in libass than PIL measures at the same size.
    """
    if not os.path.exists(font_path):
        return None
    probe = ImageFont.truetype(font_path, font_sz)
    ascent, descent = probe.getmetrics()
    if ascent + descent <= 0:
        return probe
    return ImageFont.truetype(font_path, max(1, round(font_sz * font_sz / (ascent + descent))))


def _fmt_time(d):
    return f"{int(d // 3600)}:{int((d % 3600) // 60):02d}:{int(d % 60):02d}.{int((d - int(d)) * 100):02d}"


def _word_windows(seg, start_clip, end_clip):
    """Yield (word_text, start, end) with each word active until the next one starts."""
    words = seg["words"]
    clip_len = end_clip - start_clip
    for i, w in enumerate(words):
        w_s = max(0.0, w["start"] - start_clip)
        nxt = words[i + 1]["start"] if i < len(words) - 1 else w["end"]
        w_e = min(clip_len, nxt - start_clip)
        if w_s < w_e:
            yield w["word"].strip(), w_s, w_e


def write_preset_ass(
    preset,
    transcript_segments,
    start_clip,
    end_clip,
    ass_filename,
    play_res_x,
    play_res_y,
    scale_factor,
    margin_v,
    is_split,
    cfg,
    typography_plan=None,
):
    """Write an ASS subtitle file for one of the social-media caption presets."""
    if preset not in PRESETS:
        raise ValueError(f"Unknown caption preset '{preset}'")

    accent = hex_to_ass(getattr(cfg, "caption_color", None) or DEFAULT_ACCENTS[preset])
    emphasized = {
        str(p.get("main_word", "")).lower().strip(string.punctuation)
        for p in (typography_plan or [])
    }

    # Bold display font of the active font style (e.g. Anton for HORMOZI)
    font_info = cfg.font_list[cfg.active_font_style]["special"]
    font_name = str(font_info["name"]).replace("{", "").replace("}", "").strip()
    font_path = os.path.join(cfg.font_dir, font_info["file"])
    bold = 1 if int(font_info.get("bold", 0)) else 0

    base_size = 190 if preset == "one_word" else 112
    font_sz = int(base_size * scale_factor)
    outline = 7 * scale_factor if preset != "box" else 5 * scale_factor
    box_pad = 14 * scale_factor
    margin_lr = int(70 * scale_factor)

    header = (
        "[Script Info]\n"
        f"PlayResX: {play_res_x}\n"
        f"PlayResY: {play_res_y}\n"
        "WrapStyle: 2\n"
        "ScriptType: v4.00+\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Base,{font_name},{font_sz},{WHITE},{WHITE},{BLACK},&H80000000,{bold},0,0,0,100,100,0,0,1,"
        f"{outline:.1f},{2 * scale_factor:.1f},2,0,0,0,1\n"
        f"Style: Box,{font_name},{font_sz},{WHITE},{WHITE},{accent},{accent},{bold},0,0,0,100,100,0,0,3,"
        f"{box_pad:.1f},0,2,0,0,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events = []
    clip_len = end_clip - start_clip

    if preset == "one_word":
        cx = play_res_x // 2
        cy = int(play_res_y / 2) if is_split else int(play_res_y - margin_v - font_sz * 0.6)
        for seg in transcript_segments:
            for text, w_s, w_e in _word_windows(seg, start_clip, end_clip):
                color = accent if text.lower().strip(string.punctuation) in emphasized else WHITE
                tag = (
                    f"{{\\an5\\pos({cx},{cy})\\c{color}\\fscx70\\fscy70"
                    f"\\t(0,80,\\fscx112\\fscy112)\\t(80,150,\\fscx100\\fscy100)}}"
                )
                events.append(f"Dialogue: 0,{_fmt_time(w_s)},{_fmt_time(w_e)},Base,,0,0,0,,{tag}{text.upper()}")
        _write(ass_filename, header, events)
        return

    pil_font = _libass_matched_font(font_path, font_sz)

    def measure(text):
        if pil_font is not None and hasattr(pil_font, "getlength"):
            return pil_font.getlength(text)
        return len(text) * font_sz * 0.55

    space_w = measure(" ") * 0.85
    max_line_w = play_res_x - 2 * margin_lr
    line_h = font_sz * 1.25

    for seg in transcript_segments:
        seg_s = max(0.0, seg["start"] - start_clip)
        seg_e = min(clip_len, seg["end"] - start_clip)
        if seg_s >= seg_e:
            continue
        windows = list(_word_windows(seg, start_clip, end_clip))
        if not windows:
            continue
        seg_s = min(seg_s, windows[0][1])
        seg_e = max(seg_e, windows[-1][2])

        # Greedy line wrapping using real glyph widths
        lines, cur, cur_w = [], [], 0.0
        for text, w_s, w_e in windows:
            up = text.upper()
            w = measure(up)
            if cur and cur_w + space_w + w > max_line_w:
                lines.append((cur, cur_w))
                cur, cur_w = [], 0.0
            x_off = cur_w + (space_w if cur else 0)
            cur.append((up, w, x_off, w_s, w_e))
            cur_w = x_off + w
        if cur:
            lines.append((cur, cur_w))

        total_h = len(lines) * line_h
        top = (play_res_y - total_h) / 2 if is_split else play_res_y - margin_v - total_h

        for li, (words, width) in enumerate(lines):
            base_y = int(top + (li + 1) * line_h)
            start_x = (play_res_x - width) / 2
            for up, w, x_off, w_s, w_e in words:
                x = int(start_x + x_off + w / 2)
                a = int((w_s - seg_s) * 1000)
                e = int((w_e - seg_s) * 1000)
                if preset == "yellow_pop":
                    tag = (
                        f"{{\\an2\\pos({x},{base_y})"
                        f"\\t({a},{a},\\c{accent})\\t({a},{a + 80},\\fscx116\\fscy116)"
                        f"\\t({a + 80},{a + 160},\\fscx108\\fscy108)"
                        f"\\t({e},{e},\\c{WHITE}\\fscx100\\fscy100)}}"
                    )
                    events.append(f"Dialogue: 1,{_fmt_time(seg_s)},{_fmt_time(seg_e)},Base,,0,0,0,,{tag}{up}")
                else:  # box
                    events.append(
                        f"Dialogue: 0,{_fmt_time(seg_s)},{_fmt_time(seg_e)},Base,,0,0,0,,"
                        f"{{\\an2\\pos({x},{base_y})}}{up}"
                    )
                    events.append(
                        f"Dialogue: 1,{_fmt_time(w_s)},{_fmt_time(w_e)},Box,,0,0,0,,"
                        f"{{\\an2\\pos({x},{base_y})\\fscx94\\fscy94\\t(0,70,\\fscx104\\fscy104)}}{up}"
                    )

    _write(ass_filename, header, events)


def _write(path, header, events):
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")
