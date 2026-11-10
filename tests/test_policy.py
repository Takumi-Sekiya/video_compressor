from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import policy
from policy import Action, FailureReason, Layout, Outcome, Plan, SkipReason
from tests.helpers import make_info

# ---------------------------------------------------------------------------
# 段階 1: データ構造と定数
# ---------------------------------------------------------------------------

SRC = Path("in/a.mov")
OUT = Path("out/a_c.mp4")
ARGS = ("ffmpeg", "-i", "in/a.mov", "out/a_c.mp4.part")


def test_plan_skip_valid() -> None:
    plan = Plan(SRC, Action.SKIP, SkipReason.HDR, None, ())
    assert plan.reason is SkipReason.HDR


def test_plan_encode_valid() -> None:
    plan = Plan(SRC, Action.ENCODE, None, OUT, ARGS)
    assert plan.output_path == OUT


@pytest.mark.parametrize(
    ("reason", "output_path", "ffmpeg_args"),
    [
        (None, None, ()),
        (SkipReason.HDR, OUT, ()),
        (SkipReason.HDR, None, ARGS),
    ],
    ids=["reason_missing", "output_path_present", "ffmpeg_args_present"],
)
def test_plan_skip_rejects_contradiction(
    reason: SkipReason | None, output_path: Path | None, ffmpeg_args: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        Plan(SRC, Action.SKIP, reason, output_path, ffmpeg_args)


@pytest.mark.parametrize(
    ("reason", "output_path", "ffmpeg_args"),
    [
        (SkipReason.LOW_BPP, OUT, ARGS),
        (None, None, ARGS),
        (None, OUT, ()),
        (None, OUT, ("-i", "in/a.mov")),
    ],
    ids=["reason_present", "output_path_missing", "ffmpeg_args_empty", "no_ffmpeg_head"],
)
def test_plan_encode_rejects_contradiction(
    reason: SkipReason | None, output_path: Path | None, ffmpeg_args: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        Plan(SRC, Action.ENCODE, reason, output_path, ffmpeg_args)


def test_export_constants_is_json_serializable() -> None:
    constants = policy.export_constants()
    restored = json.loads(json.dumps(constants))
    assert restored["EFFICIENT_CODECS"] == ["av1", "hevc", "vp9"]
    assert restored["HDR_TRANSFERS"] == ["arib-std-b67", "smpte2084"]
    assert restored["UNSUPPORTED_CODECS"] == ["dnxhd", "prores"]
    assert restored["TARGET_EXTENSIONS"] == [".m4v", ".mov", ".mp4"]
    assert restored["STABILITY_REQUIRED_MATCHES"] == 3
    assert restored["DURATION_TOLERANCE_SEC"] == 1.0


def test_layout_paths() -> None:
    layout = policy.Layout(Path("root"))
    assert layout.in_dir == Path("root/in")
    assert layout.out_dir == Path("root/out")
    assert layout.done_dir == Path("root/done")
    assert layout.failed_dir == Path("root/failed")
    assert layout.logs_dir == Path("root/logs")
    assert layout.lock_path == Path("root/.lock")


@pytest.mark.parametrize("reason", list(SkipReason))
def test_layout_skipped_dir_is_total(reason: SkipReason) -> None:
    layout = policy.Layout(Path("root"))
    assert layout.skipped_dir(reason) == Path("root/skipped") / reason.value


# ---------------------------------------------------------------------------
# 段階 2: 純粋関数
# ---------------------------------------------------------------------------

OPTS = policy.default_options()


def _with(**overrides: object) -> policy.Options:
    return replace(OPTS, **overrides)


# --- default_options ---


def test_default_options_matches_constants() -> None:
    assert OPTS.video_codec == "libx265"
    assert OPTS.crf == 26
    assert OPTS.preset == "slow"
    assert OPTS.short_side == 1080
    assert OPTS.downscale is True
    assert OPTS.allow_hdr is False
    assert OPTS.min_gain_ratio == 0.90
    assert OPTS.bpp_threshold_efficient == 0.045
    assert OPTS.bpp_threshold_legacy == 0.075
    assert OPTS.audio_copy_codecs == frozenset({"aac"})
    assert OPTS.audio_fallback_bitrate == "128k"
    assert OPTS.strip_location is False
    assert OPTS.verify_duration is True


# --- display_size ---


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [(0, (1920, 1080)), (90, (1080, 1920)), (180, (1920, 1080)), (270, (1080, 1920))],
)
def test_display_size_all_rotations(rotation: int, expected: tuple[int, int]) -> None:
    assert policy.display_size(make_info(rotation=rotation)) == expected


# --- effective_bit_rate ---


def test_effective_bit_rate_prefers_video_stream() -> None:
    assert policy.effective_bit_rate(make_info()) == (9_872_000, "video_stream")


def test_effective_bit_rate_container_minus_audio() -> None:
    info = make_info(video_bit_rate_bps=None)
    assert policy.effective_bit_rate(info) == (10_000_000 - 128_000, "container_minus_audio")


def test_effective_bit_rate_filesize() -> None:
    info = make_info(video_bit_rate_bps=None, container_bit_rate_bps=None)
    assert policy.effective_bit_rate(info) == (12_500_000 * 8 // 10, "filesize")


def test_effective_bit_rate_container_without_audio_bit_rate_falls_back_to_filesize() -> None:
    info = make_info(video_bit_rate_bps=None, audio_bit_rate_bps=None)
    assert policy.effective_bit_rate(info) == (10_000_000, "filesize")


@pytest.mark.parametrize("duration", [None, 0.0])
def test_effective_bit_rate_none_when_nothing_usable(duration: float | None) -> None:
    info = make_info(
        video_bit_rate_bps=None, container_bit_rate_bps=None, duration_sec=duration
    )
    assert policy.effective_bit_rate(info) is None


# --- bits_per_pixel ---


def test_bits_per_pixel_uses_stored_size() -> None:
    landscape = policy.bits_per_pixel(make_info())
    portrait = policy.bits_per_pixel(make_info(rotation=90))
    assert landscape is not None and portrait is not None
    assert landscape == portrait
    assert landscape[0] == pytest.approx(9_872_000 / (1920 * 1080 * 30))
    assert landscape[1] == "video_stream"


def test_bits_per_pixel_none_without_bit_rate() -> None:
    info = make_info(video_bit_rate_bps=None, container_bit_rate_bps=None, duration_sec=None)
    assert policy.bits_per_pixel(info) is None


# --- is_hdr ---


@pytest.mark.parametrize(
    ("transfer", "expected"),
    [("smpte2084", True), ("arib-std-b67", True), ("bt709", False), (None, False)],
)
def test_is_hdr(transfer: str | None, expected: bool) -> None:
    assert policy.is_hdr(make_info(color_transfer=transfer)) is expected


# --- scale_filter ---


def test_scale_filter_portrait_1080p_is_not_downscaled() -> None:
    info = make_info(width=1920, height=1080, rotation=90)
    assert policy.scale_filter(info, 1080) is None


def test_scale_filter_landscape_4k() -> None:
    assert policy.scale_filter(make_info(width=3840, height=2160), 1080) == "scale=-2:1080"


@pytest.mark.parametrize("rotation", [90, 270])
def test_scale_filter_portrait_4k_by_rotation(rotation: int) -> None:
    info = make_info(width=3840, height=2160, rotation=rotation)
    assert policy.scale_filter(info, 1080) == "scale=1080:-2"


def test_scale_filter_portrait_4k_stored_upright() -> None:
    info = make_info(width=2160, height=3840, rotation=0)
    assert policy.scale_filter(info, 1080) == "scale=1080:-2"


def test_scale_filter_upside_down_4k_stays_landscape() -> None:
    info = make_info(width=3840, height=2160, rotation=180)
    assert policy.scale_filter(info, 1080) == "scale=-2:1080"


def test_scale_filter_never_upscales() -> None:
    assert policy.scale_filter(make_info(width=1280, height=720), 1080) is None


@pytest.mark.parametrize("short_side", [0, 1, 1081, -2])
def test_scale_filter_rejects_invalid_short_side(short_side: int) -> None:
    with pytest.raises(ValueError):
        policy.scale_filter(make_info(), short_side)


# --- classify ---


def _info_with_bpp(bpp_bps: int, codec: str) -> policy.VideoInfo:
    # 1000 x 1000 x 1fps にすると bpp = bit_rate / 1_000_000 がちょうどになる
    return make_info(width=1000, height=1000, fps=1.0, video_codec=codec, video_bit_rate_bps=bpp_bps)


def test_classify_encode() -> None:
    assert policy.classify(make_info(), OPTS) is None


def test_classify_unsupported() -> None:
    assert policy.classify(make_info(video_codec="prores"), OPTS) is SkipReason.UNSUPPORTED


def test_classify_unsupported_precedes_hdr() -> None:
    info = make_info(video_codec="prores", color_transfer="smpte2084")
    assert policy.classify(info, OPTS) is SkipReason.UNSUPPORTED


def test_classify_hdr() -> None:
    assert policy.classify(make_info(color_transfer="arib-std-b67"), OPTS) is SkipReason.HDR


def test_classify_hdr_allowed() -> None:
    info = make_info(color_transfer="arib-std-b67")
    assert policy.classify(info, _with(allow_hdr=True)) is None


def test_classify_hdr_precedes_low_bpp() -> None:
    info = make_info(color_transfer="smpte2084", video_bit_rate_bps=1000)
    assert policy.classify(info, OPTS) is SkipReason.HDR


@pytest.mark.parametrize(
    ("codec", "bit_rate", "expected"),
    [
        ("hevc", 45_000, None),
        ("hevc", 44_999, SkipReason.LOW_BPP),
        ("av1", 44_999, SkipReason.LOW_BPP),
        ("h264", 75_000, None),
        ("h264", 74_999, SkipReason.LOW_BPP),
        ("h264", 45_000, SkipReason.LOW_BPP),
    ],
    ids=[
        "efficient_at_threshold",
        "efficient_below",
        "av1_is_efficient",
        "legacy_at_threshold",
        "legacy_below",
        "legacy_uses_legacy_threshold",
    ],
)
def test_classify_low_bpp_boundaries(
    codec: str, bit_rate: int, expected: SkipReason | None
) -> None:
    assert policy.classify(_info_with_bpp(bit_rate, codec), OPTS) is expected


def test_classify_uses_option_thresholds() -> None:
    info = _info_with_bpp(45_000, "hevc")
    assert policy.classify(info, _with(bpp_threshold_efficient=0.05)) is SkipReason.LOW_BPP


def test_classify_unknown_bpp_is_not_low_bpp() -> None:
    info = make_info(video_bit_rate_bps=None, container_bit_rate_bps=None, duration_sec=None)
    assert policy.classify(info, OPTS) is None


# --- build_ffmpeg_args ---

PART = Path("out/IMG_0001_c.mp4.part")


def _pairs(args: tuple[str, ...]) -> list[tuple[str, str]]:
    return list(zip(args, args[1:]))


def test_build_ffmpeg_args_basic_structure() -> None:
    args = policy.build_ffmpeg_args(make_info(), PART, OPTS)
    assert args[0] == "ffmpeg"
    assert args[-1] == str(PART)
    pairs = _pairs(args)
    assert ("-i", str(Path("in/IMG_0001.MOV"))) in pairs
    assert ("-c:v", "libx265") in pairs
    assert ("-crf", "26") in pairs
    assert ("-preset", "slow") in pairs
    assert ("-tag:v", "hvc1") in pairs
    assert ("-map_metadata", "0") in pairs
    assert ("-movflags", "+faststart") in pairs
    assert ("-f", "mp4") in pairs
    assert ("-map", "0:V:0") in pairs
    assert "-y" in args
    Plan(Path("in/IMG_0001.MOV"), Action.ENCODE, None, Path("out/IMG_0001_c.mp4"), args)


def test_build_ffmpeg_args_uses_options() -> None:
    args = policy.build_ffmpeg_args(make_info(), PART, _with(crf=24, preset="medium", video_codec="libx265"))
    pairs = _pairs(args)
    assert ("-crf", "24") in pairs
    assert ("-preset", "medium") in pairs


def test_build_ffmpeg_args_audio_none() -> None:
    args = policy.build_ffmpeg_args(make_info(audio_codec=None, audio_bit_rate_bps=None), PART, OPTS)
    assert "-an" in args
    assert "-c:a" not in args
    assert ("-map", "0:a:0") not in _pairs(args)


def test_build_ffmpeg_args_audio_copy() -> None:
    args = policy.build_ffmpeg_args(make_info(audio_codec="aac"), PART, OPTS)
    pairs = _pairs(args)
    assert ("-c:a", "copy") in pairs
    assert ("-map", "0:a:0") in pairs
    assert "-an" not in args
    assert "-b:a" not in args


def test_build_ffmpeg_args_audio_reencode() -> None:
    args = policy.build_ffmpeg_args(make_info(audio_codec="pcm_s16le"), PART, OPTS)
    pairs = _pairs(args)
    assert ("-c:a", "aac") in pairs
    assert ("-b:a", "128k") in pairs
    assert ("-map", "0:a:0") in pairs
    assert "-an" not in args


def test_build_ffmpeg_args_downscale_4k() -> None:
    args = policy.build_ffmpeg_args(make_info(width=3840, height=2160, rotation=90), PART, OPTS)
    assert ("-vf", "scale=1080:-2") in _pairs(args)


def test_build_ffmpeg_args_no_downscale_option() -> None:
    args = policy.build_ffmpeg_args(make_info(width=3840, height=2160), PART, _with(downscale=False))
    assert "-vf" not in args


def test_build_ffmpeg_args_no_scale_when_small() -> None:
    args = policy.build_ffmpeg_args(make_info(width=1920, height=1080, rotation=90), PART, OPTS)
    assert "-vf" not in args


def test_build_ffmpeg_args_location_kept_by_default() -> None:
    args = policy.build_ffmpeg_args(make_info(), PART, OPTS)
    assert "-metadata" not in args


def test_build_ffmpeg_args_strip_location() -> None:
    args = policy.build_ffmpeg_args(make_info(), PART, _with(strip_location=True))
    pairs = _pairs(args)
    assert ("-metadata", "location=") in pairs
    assert ("-metadata", "location-eng=") in pairs
    assert ("-metadata", "com.apple.quicktime.location.ISO6709=") in pairs
    # 位置情報の削除は -map_metadata 0 より後でないと効かない
    assert args.index("-map_metadata") < args.index("-metadata")


# --- is_worth_keeping ---


@pytest.mark.parametrize(
    ("output_size", "expected"),
    [(500, True), (900, True), (901, False), (1500, False)],
)
def test_is_worth_keeping(output_size: int, expected: bool) -> None:
    assert policy.is_worth_keeping(1000, output_size, 0.90) is expected


@pytest.mark.parametrize("source_size", [0, -1])
def test_is_worth_keeping_rejects_non_positive_source(source_size: int) -> None:
    with pytest.raises(ValueError):
        policy.is_worth_keeping(source_size, 100, 0.90)


# --- is_duration_consistent ---


@pytest.mark.parametrize(
    ("source_duration", "output_duration", "expected"),
    [
        (10.0, 10.0, True),
        (10.0, 11.0, True),
        (10.0, 9.0, True),
        (10.0, 11.5, False),
        (10.0, 3.0, False),
        (None, 3.0, True),
        (10.0, None, True),
    ],
)
def test_is_duration_consistent(
    source_duration: float | None, output_duration: float | None, expected: bool
) -> None:
    info = make_info(duration_sec=source_duration)
    assert policy.is_duration_consistent(info, output_duration) is expected


# --- resolve_dest_dir ---

LAYOUT = Layout(Path("root"))
_ALL_REASONS: list[policy.Reason | None] = [None, *SkipReason, *FailureReason]


def _expected_dest(outcome: Outcome, reason: policy.Reason | None) -> Path | None | type[ValueError]:
    if outcome is Outcome.ENCODED:
        return LAYOUT.done_dir if reason is None else ValueError
    if outcome is Outcome.SKIPPED:
        if not isinstance(reason, SkipReason):
            return ValueError
        return None if reason is SkipReason.NOT_READY else Path("root/skipped") / reason.value
    if not isinstance(reason, FailureReason):
        return ValueError
    return LAYOUT.failed_dir if reason is FailureReason.PROBE_FAILED else None


@pytest.mark.parametrize("outcome", list(Outcome))
@pytest.mark.parametrize("reason", _ALL_REASONS, ids=lambda r: "none" if r is None else r.value)
def test_resolve_dest_dir_all_combinations(outcome: Outcome, reason: policy.Reason | None) -> None:
    expected = _expected_dest(outcome, reason)
    if expected is ValueError:
        with pytest.raises(ValueError):
            policy.resolve_dest_dir(LAYOUT, outcome, reason)
    else:
        assert policy.resolve_dest_dir(LAYOUT, outcome, reason) == expected


def test_resolve_dest_dir_table_spot_checks() -> None:
    assert policy.resolve_dest_dir(LAYOUT, Outcome.ENCODED, None) == Path("root/done")
    assert policy.resolve_dest_dir(LAYOUT, Outcome.SKIPPED, SkipReason.NOT_READY) is None
    assert policy.resolve_dest_dir(LAYOUT, Outcome.SKIPPED, SkipReason.NO_GAIN) == Path("root/skipped/no_gain")
    assert policy.resolve_dest_dir(LAYOUT, Outcome.FAILED, FailureReason.PROBE_FAILED) == Path("root/failed")
    assert policy.resolve_dest_dir(LAYOUT, Outcome.FAILED, FailureReason.ENCODE_FAILED) is None
    assert policy.resolve_dest_dir(LAYOUT, Outcome.FAILED, FailureReason.OUTPUT_INVALID) is None
