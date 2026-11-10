"""parse_probe_json() のテスト。

fixtures/probe/*.json は実素材に対する ffprobe の生出力でなければならない（CLAUDE.md
「fixture を捏造しない」）。未収集の fixture を使うテストは skip する。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import policy
from ffmpeg_io import ProbeError, parse_probe_json

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "probe"
FIXTURE_NAMES = [
    "landscape_hevc",
    "portrait_rot90",
    "hdr_hlg",
    "slowmo_240fps",
    "no_audio",
    "missing_bitrate",
]


def load_fixture(name: str) -> dict[str, object]:
    path = FIXTURE_DIR / f"{name}.json"
    if not path.exists():
        pytest.skip(f"fixture 未収集（段階 3）: {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def parse_fixture(name: str) -> policy.VideoInfo:
    payload = load_fixture(name)
    return parse_probe_json(payload, Path(f"in/{name}.mov"), 100_000_000)


def first_video_stream(payload: dict[str, object]) -> dict[str, object]:
    streams = payload["streams"]
    assert isinstance(streams, list)
    for stream in streams:
        if stream.get("codec_type") == "video" and stream.get("disposition", {}).get("attached_pic") != 1:
            return stream
    raise AssertionError("fixture に映像ストリームがない")


def rate(text: str) -> float:
    num, _, den = text.partition("/")
    return float(num) / float(den or 1)


# ---------------------------------------------------------------------------
# 実 fixture（段階 3 で収集）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_all_fixtures_convert(name: str) -> None:
    info = parse_fixture(name)
    assert info.width > 0 and info.height > 0
    assert info.fps > 0
    assert info.fps_source in ("avg", "r")
    assert info.rotation in (0, 90, 180, 270)
    assert info.video_codec != "unknown"


def test_landscape_hevc() -> None:
    info = parse_fixture("landscape_hevc")
    assert info.video_codec == "hevc"
    assert info.rotation in (0, 180)
    width, height = policy.display_size(info)
    assert width > height


def test_portrait_rotation_is_90() -> None:
    info = parse_fixture("portrait_rot90")
    assert info.rotation == 90
    width, height = policy.display_size(info)
    assert width < height


def test_hdr_hlg() -> None:
    info = parse_fixture("hdr_hlg")
    assert info.color_transfer == "arib-std-b67"
    assert policy.is_hdr(info)


def test_slowmo_uses_avg_frame_rate() -> None:
    payload = load_fixture("slowmo_240fps")
    info = parse_fixture("slowmo_240fps")
    video = first_video_stream(payload)
    assert info.fps_source == "avg"
    assert info.fps == pytest.approx(rate(str(video["avg_frame_rate"])))


def test_no_audio() -> None:
    info = parse_fixture("no_audio")
    assert info.audio_codec is None
    assert info.audio_bit_rate_bps is None


def test_missing_bitrate() -> None:
    info = parse_fixture("missing_bitrate")
    assert info.video_bit_rate_bps is None
    bit_rate = policy.effective_bit_rate(info)
    assert bit_rate is None or bit_rate[1] != "video_stream"


def test_classify_with_fixture_hdr() -> None:
    info = parse_fixture("hdr_hlg")
    assert policy.classify(info, policy.default_options()) is policy.SkipReason.HDR


# ---------------------------------------------------------------------------
# 異常系（実 fixture を壊して使う）
# ---------------------------------------------------------------------------


def test_fps_falls_back_to_r_frame_rate() -> None:
    payload = copy.deepcopy(load_fixture("landscape_hevc"))
    video = first_video_stream(payload)
    video["avg_frame_rate"] = "0/0"
    info = parse_probe_json(payload, Path("x.mov"), 1)
    assert info.fps_source == "r"
    assert info.fps == pytest.approx(rate(str(video["r_frame_rate"])))


def test_missing_fps_raises() -> None:
    payload = copy.deepcopy(load_fixture("landscape_hevc"))
    video = first_video_stream(payload)
    video["avg_frame_rate"] = "0/0"
    video["r_frame_rate"] = "0/0"
    with pytest.raises(ProbeError):
        parse_probe_json(payload, Path("x.mov"), 1)


def test_missing_width_raises() -> None:
    payload = copy.deepcopy(load_fixture("landscape_hevc"))
    del first_video_stream(payload)["width"]
    with pytest.raises(ProbeError):
        parse_probe_json(payload, Path("x.mov"), 1)


def test_no_video_stream_in_fixture_raises() -> None:
    payload = copy.deepcopy(load_fixture("landscape_hevc"))
    streams = payload["streams"]
    assert isinstance(streams, list)
    payload["streams"] = [s for s in streams if s.get("codec_type") != "video"]
    with pytest.raises(ProbeError):
        parse_probe_json(payload, Path("x.mov"), 1)


# ---------------------------------------------------------------------------
# 異常系（fixture 不要）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [{}, {"streams": [], "format": {}}], ids=["empty", "no_streams"])
def test_no_video_stream_raises(payload: dict[str, object]) -> None:
    with pytest.raises(ProbeError) as excinfo:
        parse_probe_json(payload, Path("x.mov"), 1)
    assert excinfo.value.path == Path("x.mov")
