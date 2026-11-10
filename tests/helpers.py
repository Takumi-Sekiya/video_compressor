"""テスト共通のヘルパ。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from policy import VideoInfo

_BASE = VideoInfo(
    path=Path("in/IMG_0001.MOV"),
    size_bytes=12_500_000,
    duration_sec=10.0,
    width=1920,
    height=1080,
    rotation=0,
    fps=30.0,
    fps_source="avg",
    video_codec="hevc",
    pix_fmt="yuv420p",
    video_bit_rate_bps=9_872_000,
    container_bit_rate_bps=10_000_000,
    audio_codec="aac",
    audio_bit_rate_bps=128_000,
    color_transfer="bt709",
    color_primaries="bt709",
)


def make_info(**overrides: object) -> VideoInfo:
    """横位置 1080p30 HEVC + AAC の VideoInfo を基準に、指定したフィールドだけ差し替える。

    基準値の bpp は 9_872_000 / (1920 * 1080 * 30) ≒ 0.159。
    """
    return replace(_BASE, **overrides)
