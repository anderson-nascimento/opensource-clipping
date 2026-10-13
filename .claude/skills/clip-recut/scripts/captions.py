"""
One-word-at-a-time captions for a clip-recut composition.

Reads <pkg>/transcript.json and writes two snippets to paste into public/index.html:
  <out>.html  one timed <div class="cap clip"> per word (track 5), inside the given windows
  <out>.js    the pop-in tween per word for the master timeline (expects a `tl` variable)

Usage:
  python captions.py <pkg> --windows "5.88-9.95@693,13.55-15.95@1230" --keys "lançar,19,junho" [--out <pkg>/captions]

  --windows  start-end@top_px, comma separated: caption on-screen windows and the caption box
             top (150px tall box; seam in the split layout is ~693, lower body ~1230)
  --keys     words rendered in the larger highlight tier (case/punctuation-insensitive)
"""

import argparse
import json
import os
import re

MAX_WORD_HOLD = 1.2  # seconds a single word may stay on screen


def norm(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pkg")
    ap.add_argument("--windows", required=True)
    ap.add_argument("--keys", default="")
    ap.add_argument("--out")
    args = ap.parse_args()

    words = json.load(open(os.path.join(args.pkg, "transcript.json"), encoding="utf-8"))
    meta = json.load(open(os.path.join(args.pkg, "metadata.json"), encoding="utf-8"))
    duration = int(float(meta["format"]["duration"]) * 30) / 30
    windows = []
    for part in args.windows.split(","):
        span, top = part.strip().split("@")
        start, end = span.split("-")
        windows.append((float(start), min(float(end), duration), int(top)))
    keys = {norm(k) for k in args.keys.split(",") if k.strip()}

    html, js = [], []
    for i, w in enumerate(words):
        win = next((x for x in windows if x[0] - 0.05 <= w["start"] < x[1]), None)
        if not win:
            continue
        nxt = words[i + 1]["start"] if i + 1 < len(words) else duration
        start = max(w["start"], win[0])
        end = min(nxt, win[1], start + MAX_WORD_HOLD)
        if end - start < 1 / 30:
            continue
        text = re.sub(r"[.,;:?!…]+$", "", w["text"]).strip()
        key = norm(text) in keys
        cid = f"cap-{i:03d}"
        html.append(
            f'  <div id="{cid}" class="cap clip{" key" if key else ""}" data-start="{start:.3f}" '
            f'data-duration="{end - start:.3f}" data-track-index="5" style="top:{win[2]}px;">{text}</div>'
        )
        js.append(
            f'    tl.fromTo("#{cid}", {{ scale: {0.7 if key else 0.85} }}, '
            f'{{ scale: 1, duration: 0.12, ease: "back.out(2.5)", immediateRender: false }}, {start:.3f});'
        )

    out = args.out or os.path.join(args.pkg, "captions")
    with open(out + ".html", "w", encoding="utf-8") as f:
        f.write("\n".join(html) + "\n")
    with open(out + ".js", "w", encoding="utf-8") as f:
        f.write("\n".join(js) + "\n")
    print(f"{len(html)} captions -> {out}.html / {out}.js")


if __name__ == "__main__":
    main()
