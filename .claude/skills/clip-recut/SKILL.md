---
name: clip-recut
description: Design and render the motion graphics of a short clip exported by `main.py --handoff` (a folder like outputs/handoff/rank_1 with BRIEF.md, STYLE.md, transcript.json and public/input-video.mp4) using HyperFrames — split layouts, full-screen takeovers, kinetic type, one-word captions, SFX and optional AI images — then render output.mp4. Use when the user asks to recut, design, animate or render a handoff package/clip, or runs /clip-recut <folder>.
---

# clip-recut

Turns a handoff package into a finished 1080x1920 Short. The project already did the
mechanical part (download, Whisper transcription, clip selection, face reframing); this skill
does the creative part with HyperFrames (HTML + GSAP rendered to MP4) and delivers
`output.mp4` in the package folder.

It adapts HeyGen's `talking-head-recut` workflow to this project: the transcript ships in the
package (never run `hyperframes transcribe`: it is English-only `small.en` and whisper.cpp is
usually not installed), the pace comes from `STYLE.md` (much faster than the skill default),
and the video framing changes per beat.

## Inputs (the package folder, `PKG`)

| file | what it is |
|---|---|
| `BRIEF.md` | read first: clip summary, front-matter contract (`mode`, `language`, `max_image_credits`) |
| `STYLE.md` | rhythm, layouts, captions, design and sound rules; they are requirements |
| `transcript.json` | `[{text, start, end}]` on the clip timeline, already correct |
| `context.json` | full AI plan: title, hook, reason, keywords, typography and B-roll ideas |
| `metadata.json` | ffprobe of `public/input-video.mp4` (duration, size, fps) |
| `assets/manifest.json` | `files` shipped in `public/` (fonts, SFX) + `requests` (image ideas) |
| `public/` | the HyperFrames project root: `input-video.mp4`, `fonts/`, `vendor/gsap.min.js`, `assets/` |

With `mode: autonomous`, do not ask questions; record every decision in `DELIVERY.md`.

## Workflow

### 1. Check and read
- `npx hyperframes doctor` (needs Node 22+, FFmpeg, Chrome; whisper/Docker failures are fine).
- Read all package files above. Clamp everything to `metadata.json` duration, rounded down to
  whole frames (`floor(duration*30)/30`).

### 2. Storyboard (`PKG/storyboard.json`)
Plan beats on the word timestamps, following STYLE.md:
- a composition change every 2.5–3.5s, landing on a word start;
- 20–30% of the runtime as full-screen takeovers, only on turning points;
- the first 3–6s as the hook (split + title banner on the seam);
- turn facts into designed objects (dates → calendar, plans → progress bar, numbers → counters,
  lists → items landing on each spoken word), don't just repeat the words;
- pick at most 3 visual worlds and 1–2 accent colors that fit the tone.

Shape (schema v3 from talking-head-recut): `composition {fps, width, height, durationSeconds,
layout:"portrait"}`, `videoTrack`, `subtitles {enabled, windows}`, `cards[] {id, intent,
startSec, endSec, accentIndex, zone, contentHints}`. Add `layout` per beat in `contentHints`
(`split` / `full-bleed` / `takeover` / `punch`).

### 3. Images (optional)
`assets/manifest.json → requests` lists image ideas from the planner. You may add your own.
Generate them only when the `google-flow` MCP tools are available:
1. `flow_estimate_credits` with `type: image`, `ratio: 9:16` (model `Nano Banana 2.1`).
2. Only if `credits_per_generation` ≤ `max_image_credits` from BRIEF.md (default 0):
   `flow_generate_image` with `ratio: 9:16`, `auto_confirm: true`, `max_credits` set to that
   limit, a `label` like `clip-recut/<pkg>/<id>`, and a cinematic, text-free prompt that
   matches the clip's tone (no real people's faces, nothing graphic).
3. Copy each returned file to `PKG/public/assets/<id>.jpg`, look at it, and add it to
   `manifest.json → files`.
Skip silently (note it in DELIVERY.md) when the tools are missing or images cost credits.
Typography-only cards are fine.

### 4. Captions
Decide the caption windows (hide captions while a card already writes the same words), then:
```bash
python .claude/skills/clip-recut/scripts/captions.py PKG --windows "5.88-9.95@693,13.55-15.95@1230" --keys "word1,word2"
```
It writes `PKG/captions.html` (one timed element per word) and `PKG/captions.js` (pop-in
tweens); paste both into the composition. `@693` is the seam in the split layout, `@1230`
the lower body in full-bleed. Keys are the highlight tier (names, numbers, dates, the metaphor).

### 5. Composition (`PKG/public/index.html`)
Start from `references/example-composition.html` (a complete, lint-clean, rendered example)
and write a new design for this clip. Contract:
- `#stage` 1080x1920 with `data-composition-id="talking-head-recut"`, `data-start="0"`,
  `data-duration`, `data-fps="30"`, `data-width`, `data-height`; one paused master timeline
  registered as `window.__timelines["talking-head-recut"]`, built synchronously.
- Video: `#video-wrap > #video-inner > video#bg-video.clip` (`src="input-video.mp4"`,
  `data-has-audio="true"`, track 1). Change layouts with `tl.set` on `#video-wrap`
  (`top/height/scale`) and `#video-inner` (`y` to keep the face in view: about -110 in split).
- Every timed element has an `id`, `class="… clip"`, `data-start`, `data-duration`,
  `data-track-index`. Cards on track 2, overlapping banners on 3, captions on 5.
- SFX: `<audio id=… class="clip" src="assets/sfx_….mp3" data-start data-duration
  data-track-index data-volume>` with its own track index each; `data-media-start` to use the
  tail of a long file (e.g. the riser).
- Fonts only through `@font-face` on `fonts/…` files; body `font-family` lists concrete names
  (`"Inter", "Anton", sans-serif`), never only a CSS variable. No external URLs at all.
- No `Math.random`, `Date.now`, `setTimeout`, Promises or media `play()`.

Pitfalls found in practice:
- **`fromTo` renders its start state immediately.** For any element already visible before the
  tween's time (flashes, the video wrapper, a cell that changes mid-card) add
  `immediateRender: false`, or the start state leaks backwards (a white flash covered a whole
  card for a second).
- **Never put a CSS `transform` on an element GSAP animates** (lint `gsap_css_transform_conflict`);
  set the initial transform with `tl.set` at 0.
- **Text revealed with `clip-path`/`overflow:hidden` needs `line-height ≥ 1.2`**, or accents
  (Â, Ê, Ç) are cut off.
- **Stacking follows DOM order, not `data-track-index`.** A card-host declared later covers an
  earlier one where they overlap (a later top panel hid half of the seam banner). Give
  overlapping layers an explicit `z-index` (banner `z-index:5`).
- **Round line caps show as dots before a draw-path starts.** Hide each path until its draw
  begins: `tl.set(el, {strokeDasharray: L, strokeDashoffset: L, opacity: 0}, 0)` then
  `tl.set(el, {opacity: 1}, at)`.
- **Accents on big Anton words reach above the line box** (Ê, Ã at 200–300px). Leave ~0.25×
  font-size of space above a stacked line, or the accent touches the line above.
- **Wide shots need their own framing.** When the source cuts to a wide shot, change
  `#video-inner` `y` for that beat (e.g. −420 in split) so the face lands in the panel; check
  the clean clip with `ffmpeg -vf "fps=1,scale=180:-1,tile=10x2"` before planning — and plan
  takeovers over stretches where the reframe lost the face.
- Long titles in Anton: ~0.5×font-size per character; keep a single line under ~920px.
- Keep text off the face: in split, the face sits roughly between y=950 and y=1450.

### 6. QA (required)
```bash
cd PKG
npx hyperframes lint public            # 0 errors (warnings about density/ids are fine)
npx hyperframes snapshot public -o qa --no-end --timeout 15000 --at <one hero time per card, plus each transition +0.3s>
```
Open `qa/contact-sheet*.jpg` and check every frame: overlaps, clipped letters, text over the
face, captions duplicating card text, elements visible too early, wrong framing. Fix and
snapshot again until clean.

### 7. Render
```bash
cd PKG
npx hyperframes render public --skill=talking-head-recut -o output.mp4 --fps 30
```
About 2 minutes for a 26s clip on a laptop CPU. Then sample the result
(`ffmpeg -ss <t> -i output.mp4 -frames:v 1 …`) at a few beats to confirm it matches the snapshots.

### 8. Deliver
Write `PKG/DELIVERY.md`: beat list (time, layout, idea), images generated (prompt, credits),
captions windows, render time, known issues. Tell the user the clip is ready and that
`python -m clipping.handoff collect <outputs_dir>` makes the uploaders publish `output.mp4`
instead of the clean clip.
