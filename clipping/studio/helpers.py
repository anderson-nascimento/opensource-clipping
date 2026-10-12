"""
General helper utilities for Studio rendering workflow.
"""


def format_seconds(seconds):
    """
    Format a duration in seconds into HH:MM:SS.

    Args:
        seconds: Numeric duration in seconds.

    Returns:
        Duration string in `HH:MM:SS` format, clamped to non-negative.
    """
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def escape_ffmpeg_filter_value(value: str) -> str:
    """
    Escape a file path so it is safe as an FFmpeg filter option value.

    FFmpeg parses a filter value twice: once as an option value and once as part
    of the filtergraph, so special characters are escaped for both levels.
    Backslashes become forward slashes first (FFmpeg accepts them on Windows), so
    paths such as C:\\Users\\... no longer break the parser on the drive colon.

    Args:
        value: Raw value to place inside an FFmpeg filter string.

    Returns:
        Escaped value string for FFmpeg filter usage.
    """
    def _esc(text, special):
        return "".join("\\" + c if c in special else c for c in text)

    v = str(value).replace("\\", "/")
    v = _esc(v, "\\':")        # option-value level
    return _esc(v, "\\'[],;")  # filtergraph level

