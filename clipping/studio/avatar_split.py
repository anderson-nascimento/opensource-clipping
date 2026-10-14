"""
clipping.studio.avatar_split — Clip + avatar vertical layout

The 9:16 frame is split horizontally: the face-tracked clip in one panel and the
channel's avatar (a looping video, or an image) in the other, with an optional
channel label (e.g. "@channel") centered on the seam. ``create_hybrid_video``
renders the clip panel and calls ``AvatarLayout.compose`` on every frame, so the
rest of the pipeline (hook, captions, music, watermark) receives a full frame.
"""

import importlib.util
import os

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
LABEL_FONT = "Montserrat-Black.ttf"
LABEL_FONT_URL = "https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-Black.ttf"
LABEL_FONT_PX = 52  # at 1920 px of output height
LABEL_PAD_X, LABEL_PAD_Y = 28, 14


def _even(v: float) -> int:
    return int(round(v / 2)) * 2


def _cover(frame, w, h):
    """Scale *frame* to cover (w, h) and center-crop it, like CSS object-fit: cover."""
    fh, fw = frame.shape[:2]
    scale = max(w / fw, h / fh)
    nw, nh = max(w, int(round(fw * scale))), max(h, int(round(fh * scale)))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    x0, y0 = (nw - w) // 2, (nh - h) // 2
    return resized[y0:y0 + h, x0:x0 + w]


class AvatarSource:
    """Frames of the avatar media at clip time *t*: a video loops, an image stays still."""

    def __init__(self, path, w, h):
        self.w, self.h = w, h
        self.static = None
        self.cap = None
        if path.lower().endswith(IMAGE_EXTS):
            img = cv2.imread(path, cv2.IMREAD_COLOR)
            if img is None:
                raise RuntimeError(f"Cannot read avatar image: {path}")
            self.static = _cover(img, w, h)
            return
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open avatar video: {path}")
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if fps and fps > 0 else 30.0
        self.loop_start = 0.0  # clip time at which the current loop began
        self.next_idx = 0      # index of the next frame to read in the current loop
        self.current = None

    def frame_at(self, t):
        if self.static is not None:
            return self.static
        want = int((t - self.loop_start) * self.fps)
        while self.next_idx <= want:
            ok, frame = self.cap.read()
            if not ok:
                if self.next_idx == 0:
                    raise RuntimeError("Avatar video has no readable frames")
                # End of the avatar video: start the loop again from this clip time.
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                self.loop_start, self.next_idx, want = t, 0, 0
                continue
            self.current = _cover(frame, self.w, self.h)
            self.next_idx += 1
        return self.current

    def close(self):
        if self.cap is not None:
            self.cap.release()


def _label_font_path(font_dir):
    path = os.path.join(font_dir, LABEL_FONT)
    if not os.path.exists(path):
        # Studio modules load each other by file path (see render_hybrid.py), not as a package.
        spec = importlib.util.spec_from_file_location(
            "clipping_studio_typography", os.path.join(os.path.dirname(__file__), "typography.py")
        )
        typography = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(typography)
        typography.download_google_font(LABEL_FONT_URL, LABEL_FONT, font_dir)
    return path


def render_label(text, out_h, font_dir):
    """Black text on a white box, as a BGR image plus an alpha mask (0..1)."""
    scale = out_h / 1920
    try:
        font = ImageFont.truetype(_label_font_path(font_dir), int(LABEL_FONT_PX * scale))
    except Exception:
        font = ImageFont.load_default()
    left, top, right, bottom = font.getbbox(text)
    pad_x, pad_y = int(LABEL_PAD_X * scale), int(LABEL_PAD_Y * scale)
    w, h = right - left + 2 * pad_x, bottom - top + 2 * pad_y
    img = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    ImageDraw.Draw(img).text((pad_x - left, pad_y - top), text, font=font, fill=(0, 0, 0, 255))
    rgba = np.array(img)
    return cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2BGR), rgba[:, :, 3:4].astype(np.float32) / 255.0


class AvatarLayout:
    """Geometry and per-frame composition of the clip + avatar layout."""

    def __init__(self, cfg, out_w, out_h):
        self.out_w, self.out_h = out_w, out_h
        self.avatar_h = _even(out_h * cfg.avatar_ratio)
        self.clip_h = out_h - self.avatar_h
        avatar_on_top = cfg.avatar_position == "top"
        self.clip_y = self.avatar_h if avatar_on_top else 0
        self.avatar_y = 0 if avatar_on_top else self.clip_h
        self.seam_y = self.avatar_h if avatar_on_top else self.clip_h

        self.source = AvatarSource(cfg.avatar_split, out_w, self.avatar_h)

        self.label = None
        if cfg.avatar_label:
            bgr, alpha = render_label(cfg.avatar_label, out_h, cfg.font_dir)
            lh, lw = bgr.shape[:2]
            x = (out_w - lw) // 2
            y = min(max(0, self.seam_y - lh // 2), out_h - lh)
            self.label = (bgr.astype(np.float32), alpha, x, y)

    def compose(self, clip_panel, t):
        """Return the full frame: *clip_panel* (out_w x clip_h) plus the avatar and label."""
        frame = np.empty((self.out_h, self.out_w, 3), dtype=np.uint8)
        frame[self.clip_y:self.clip_y + self.clip_h] = clip_panel
        frame[self.avatar_y:self.avatar_y + self.avatar_h] = self.source.frame_at(t)
        if self.label is not None:
            bgr, alpha, x, y = self.label
            lh, lw = bgr.shape[:2]
            region = frame[y:y + lh, x:x + lw].astype(np.float32)
            frame[y:y + lh, x:x + lw] = (bgr * alpha + region * (1 - alpha)).astype(np.uint8)
        return frame

    def close(self):
        self.source.close()
