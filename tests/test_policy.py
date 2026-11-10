from __future__ import annotations

import json
from pathlib import Path

import pytest

import policy
from policy import Action, Plan, SkipReason

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
