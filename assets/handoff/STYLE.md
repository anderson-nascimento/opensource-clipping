# STYLE.md — "dynamic Short" editing rules

Rules for the motion design of every handoff clip, distilled from a high-retention
reference Short (46s, ~14 composition blocks, ~53 cuts). The clip's BRIEF.md may override
any of them. Edit this file (assets/handoff/STYLE.md) to change the house style.

## Rhythm
- The composition changes every **2.5–3.5s**; never hold the same composition for more than 4s.
- Inside a split block, the content panel changes or evolves every **1.5–2.5s**.
- Every change lands **on the start of a spoken word** (use transcript.json timestamps).
- Full-screen takeovers (video hidden) cover **20–30%** of the runtime, only on turning
  points: the title, a strong number or date, the punchline, a result. Each lasts 1–3.5s.
- The speaker's face is visible ~70–80% of the time.

## Layouts (1080x1920 canvas)
| name | video (#video-wrap) | content |
|---|---|---|
| `split` | top:768 height:1152 (bottom 60%), face kept in view | panel 0–768 on top |
| `full-bleed` | whole canvas | captions / transparent lower-third only |
| `takeover` | covered by an opaque full-screen card | the card |
| `punch` | whole canvas at scale 1.12–1.2 (hard cut, slow creep after) | emphasis, ending, CTA |

Use hard cuts between layouts (tl.set), not long eased moves.

## Hook banner
- For the first 3–6s, a strip on the seam (y≈768) with the clip title: torn-paper edges,
  the accent color, white text with one highlighted word.

## Captions
- **One word at a time**, Anton, uppercase, white with a black outline/shadow.
- Position: on the seam in `split`; at ~65% of the height in `full-bleed`/`punch`.
- **Two tiers**: keywords (names, numbers, dates, the central metaphor) are ~1.4x larger,
  in the highlight color, with a soft glow.
- Hide captions while a card already writes the same sentence on screen.

## Design
- At most 3 visual "worlds" per clip; one dominant accent color plus one support color.
- Type: Anton (display) + Inter (support). Portrait titles 110–220px.
- Hand-made touches: torn edges, hand-drawn circles (draw-path), strike-throughs.
- Lists / rule of three: each item enters exactly on its spoken word.
- Turn facts in the speech into designed objects (a calendar for dates, a progress bar for
  a plan, a counter for numbers) instead of repeating the words.
- Use files from assets/manifest.json when they fit; never reference external URLs.

## Sound
- Subtle SFX (volume 0.2–0.5): whoosh into/out of takeovers, pop on list items, a deep
  impact only on the climax (once per clip), a riser into the climax.
- Program audio comes from the video; never lower the voice.

## Tone
- Match the energy to the subject: loss/grief stories get firm rhythm but a sober palette,
  no emojis, no jokes; light or tech content can use vivid colors, 3D icons and humor.
