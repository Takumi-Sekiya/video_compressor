"""policy.py の単体テスト。

段階 1 の範囲は Plan.__post_init__ の不変条件と export_constants() の JSON 化のみ。
判定関数（classify() など）のテストは段階 2 で追加する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import policy
from policy import Action, Plan, SkipReason

_SOURCE = Path("in") / "IMG_0001.MOV"
_OUTPUT = Path("out") / "IMG_0001_c.mp4"
_ARGS = ("ffmpeg", "-i", str(_SOURCE), str(_OUTPUT))


def test_plan_skip_is_valid() -> None:
    plan = Plan(
        source=_SOURCE,
        action=Action.SKIP,
        reason=SkipReason.LOW_BPP,
        output_path=None,
        ffmpeg_args=(),
    )

    assert plan.action is Action.SKIP
    assert plan.reason is SkipReason.LOW_BPP


def test_plan_encode_is_valid() -> None:
    plan = Plan(
        source=_SOURCE,
        action=Action.ENCODE,
        reason=None,
        output_path=_OUTPUT,
        ffmpeg_args=_ARGS,
    )

    assert plan.action is Action.ENCODE
    assert plan.ffmpeg_args[0] == "ffmpeg"


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("skip_with_output_path", {"output_path": _OUTPUT}),
        ("skip_with_ffmpeg_args", {"ffmpeg_args": _ARGS}),
        ("skip_without_reason", {"reason": None}),
    ],
)
def test_plan_skip_rejects_inconsistent_fields(
    label: str, overrides: dict[str, object]
) -> None:
    fields: dict[str, object] = {
        "source": _SOURCE,
        "action": Action.SKIP,
        "reason": SkipReason.HDR,
        "output_path": None,
        "ffmpeg_args": (),
    }
    fields.update(overrides)

    with pytest.raises(ValueError):
        Plan(**fields)


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("encode_without_output_path", {"output_path": None}),
        ("encode_without_ffmpeg_args", {"ffmpeg_args": ()}),
        ("encode_with_reason", {"reason": SkipReason.NO_GAIN}),
    ],
)
def test_plan_encode_rejects_inconsistent_fields(
    label: str, overrides: dict[str, object]
) -> None:
    fields: dict[str, object] = {
        "source": _SOURCE,
        "action": Action.ENCODE,
        "reason": None,
        "output_path": _OUTPUT,
        "ffmpeg_args": _ARGS,
    }
    fields.update(overrides)

    with pytest.raises(ValueError):
        Plan(**fields)


def test_export_constants_is_json_serializable() -> None:
    payload = json.loads(json.dumps(policy.export_constants()))

    assert payload == policy.export_constants()


def test_export_constants_converts_frozensets_to_sorted_lists() -> None:
    constants = policy.export_constants()

    for key in (
        "EFFICIENT_CODECS",
        "HDR_TRANSFERS",
        "UNSUPPORTED_CODECS",
        "TARGET_EXTENSIONS",
    ):
        value = constants[key]
        assert isinstance(value, list)
        assert value == sorted(value)

    assert constants["EFFICIENT_CODECS"] == ["av1", "hevc", "vp9"]
    assert constants["TARGET_EXTENSIONS"] == [".m4v", ".mov", ".mp4"]


def test_export_constants_covers_the_documented_keys() -> None:
    """design.md「第 2 層・第 3 層」が meta.json に出すと定めた 9 項目と一致すること。"""
    assert set(policy.export_constants()) == {
        "EFFICIENT_CODECS",
        "HDR_TRANSFERS",
        "UNSUPPORTED_CODECS",
        "TARGET_EXTENSIONS",
        "STABILITY_INTERVAL_SEC",
        "STABILITY_REQUIRED_MATCHES",
        "STABILITY_TIMEOUT_SEC",
        "STABILITY_MTIME_QUIET_SEC",
        "DURATION_TOLERANCE_SEC",
    }


def test_layout_builds_paths_without_touching_the_filesystem(tmp_path: Path) -> None:
    layout = policy.Layout(root=tmp_path / "share")

    assert layout.in_dir == tmp_path / "share" / "in"
    assert layout.out_dir == tmp_path / "share" / "out"
    assert layout.done_dir == tmp_path / "share" / "done"
    assert layout.failed_dir == tmp_path / "share" / "failed"
    assert layout.logs_dir == tmp_path / "share" / "logs"
    assert layout.lock_path == tmp_path / "share" / ".lock"
    assert not layout.root.exists()


def test_skipped_dir_is_total_over_skip_reason(tmp_path: Path) -> None:
    """skipped_dir() は全 SkipReason に対して値を返す（例外を投げない）。"""
    layout = policy.Layout(root=tmp_path)

    for reason in SkipReason:
        assert layout.skipped_dir(reason) == tmp_path / "skipped" / reason.value
