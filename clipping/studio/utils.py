import bisect
import html
import importlib.util
import json
import math
import os
import random
import re
import shutil
import string
import subprocess
import textwrap
import time
import urllib.parse
import urllib.request

import cv2
import mediapipe as mp
import numpy as np
import requests
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision
from PIL import Image, ImageDraw, ImageFont
from yt_dlp import YoutubeDL

FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0"

def _load_studio_internal_module(file_name: str, module_alias: str):
    """
    Load an internal studio module dynamically by file path.

    Args:
        file_name (str): The filename of the module to load.
        module_alias (str): The alias to assign to the loaded module.

    Returns:
        module: The dynamically loaded Python module.

    Side Effects:
        Loads and executes the module code.

    Raises:
        ImportError: If the module specification cannot be found or loaded.
    """
    module_path = os.path.join(os.path.dirname(__file__), file_name)
    spec = importlib.util.spec_from_file_location(module_alias, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to load internal module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

_helpers = _load_studio_internal_module("helpers.py", "clipping_studio_helpers")
_ffmpeg_utils = _load_studio_internal_module("ffmpeg_utils.py", "clipping_studio_ffmpeg_utils")
format_seconds = _helpers.format_seconds
escape_ffmpeg_filter_value = _helpers.escape_ffmpeg_filter_value
detect_video_encoder = _ffmpeg_utils.detect_video_encoder
get_ts_encode_args = _ffmpeg_utils.get_ts_encode_args
get_mp4_encode_args = _ffmpeg_utils.get_mp4_encode_args
open_ffmpeg_video_writer = _ffmpeg_utils.open_ffmpeg_video_writer
build_ffmpeg_progress_cmd = _ffmpeg_utils.build_ffmpeg_progress_cmd
run_ffmpeg_with_progress = _ffmpeg_utils.run_ffmpeg_with_progress


class SequentialFrameSampler:
    """
    Read frames at increasing timestamps by decoding forward instead of seeking.

    Seeking with `cap.set(CAP_PROP_POS_MSEC, ...)` makes the decoder jump back to
    the previous keyframe and decode up to the target on every call. Face analysis
    samples every 0.25s, so on VP9 sources that was ~180 ms per sample, while
    decoding forward returns the very same frames (the first one at or after each
    timestamp) about 12x faster.
    """

    def __init__(self, cap, start_s):
        self.cap = cap
        self.pos_s = None  # timestamp of the last grabbed frame, in seconds
        cap.set(cv2.CAP_PROP_POS_MSEC, start_s * 1000)
        self.start_s = start_s

    def read_at(self, t):
        """Return `(ok, frame)` for the first frame at or after `start_s + t`."""
        target = self.start_s + t
        if self.pos_s is not None and target < self.pos_s - 1e-3:
            # Going backwards is never needed by the samplers, but stay correct.
            self.cap.set(cv2.CAP_PROP_POS_MSEC, target * 1000)
            self.pos_s = None
        while self.pos_s is None or self.pos_s < target - 1e-3:
            if not self.cap.grab():
                return False, None
            self.pos_s = self.cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        return self.cap.retrieve()


def interpolate_track(samples, times, t, keys, jump, max_gap=None):
    """
    Values of *keys* at time *t* from time-sorted camera samples [{"time", key, ...}].
    *times* is ``[s["time"] for s in samples]``, built once by the caller.

    Interpolates linearly between the two samples around *t*, except:
      - when "cx" changes by more than *jump* pixels (a cut to another face or camera
        angle), the value steps at the midpoint instead of panning across the frame;
      - when the samples are more than *max_gap* seconds apart (the face was not seen in
        between), the earlier value is held until the next sample, so a crop never slides
        slowly over the background while its face is missing.

    Returns a tuple with one value per key, or None when there are no samples.
    """
    if not samples:
        return None
    if t <= samples[0]["time"]:
        return tuple(samples[0][k] for k in keys)
    if t >= samples[-1]["time"]:
        return tuple(samples[-1][k] for k in keys)

    i = bisect.bisect_right(times, t) - 1
    a, b = samples[i], samples[i + 1]
    t1, t2 = a["time"], b["time"]
    if t2 <= t1:
        return tuple(a[k] for k in keys)
    if max_gap is not None and t2 - t1 > max_gap:
        return tuple(a[k] for k in keys)
    if abs(b["cx"] - a["cx"]) > jump:
        return tuple((a if t < (t1 + t2) / 2 else b)[k] for k in keys)
    frac = (t - t1) / (t2 - t1)
    return tuple(a[k] + (b[k] - a[k]) * frac for k in keys)


def _get_cv2_interpolation(cfg=None):
    """
    Resolve OpenCV interpolation mode from runtime config.

    Args:
        cfg: Runtime config that may contain `video_scale_algo`.

    Returns:
        OpenCV interpolation constant.
    """
    algo = str(
        getattr(cfg, "video_scale_algo", os.environ.get("OSC_VIDEO_SCALE_ALGO", "bicubic"))
    ).lower()
    mapping = {
        "lanczos": cv2.INTER_LANCZOS4,
        "bicubic": cv2.INTER_CUBIC,
        "bilinear": cv2.INTER_LINEAR,
        "area": cv2.INTER_AREA,
    }
    return mapping.get(algo, cv2.INTER_LANCZOS4)


def _resize_frame(frame, size, cfg=None):
    """
    Resize frame using configured interpolation algorithm.

    Args:
        frame: Input image/frame array.
        size: Target `(width, height)`.
        cfg: Runtime config that may define `video_scale_algo`.

    Returns:
        Resized frame.
    """
    return cv2.resize(frame, size, interpolation=_get_cv2_interpolation(cfg))



# ── Aspect Ratio Helpers ────────────────────────────────────────────────
# Maps a ratio string to (width_part, height_part).
RATIO_MAP = {
    "9:16": (9, 16),
    "16:9": (16, 9),
    "1:1":  (1, 1),
    "3:4":  (3, 4),
    "4:5":  (4, 5),
}

# Ratios that require face-tracking crop (narrower than the source).
VERTICAL_RATIOS = {"9:16", "1:1", "3:4", "4:5"}


def _is_vertical_ratio(ratio: str) -> bool:
    """Return True when the ratio needs a face-tracked crop from the source."""
    return ratio in VERTICAL_RATIOS


def _get_render_dims(cfg, ratio, source_h=1080):
    """
    Calculate target output resolution based on config and aspect ratio.
    If mode is 'source', it uses the provided source_h as the base dimension.

    Supported ratios: 9:16, 16:9, 1:1, 3:4, 4:5.

    Args:
        cfg: Runtime config object that specifies `render_output_height`.
        rasio (str): The target aspect ratio.
        source_h (int): The original source video height in pixels.

    Returns:
        tuple: A tuple containing (target_width, target_height).

    Side Effects:
        None

    Raises:
        ValueError: If config contains an invalid non-integer height when not 'source'.
    """
    mode = str(getattr(cfg, "render_output_height", "1080")).lower()
    if mode == "source":
        target_h_base = source_h
    else:
        try:
            target_h_base = int(mode)
        except (ValueError, TypeError):
            target_h_base = 1080

    w_part, h_part = RATIO_MAP.get(ratio, (16, 9))

    if _is_vertical_ratio(ratio):
        # target_h_base is treated as the 'short' side (width)
        out_w = target_h_base
        out_h = int(target_h_base * h_part / w_part)
    else:
        # 16:9 — target_h_base is height
        out_h = target_h_base
        out_w = int(target_h_base * w_part / h_part)

    # Ensure even dimensions for codec compatibility
    if out_w % 2 != 0:
        out_w += 1
    if out_h % 2 != 0:
        out_h += 1

    return out_w, out_h


