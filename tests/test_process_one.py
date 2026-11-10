"""process_one() の結合テスト。実 ffmpeg を起動せず、probe / encode / duration にフェイクを注入する。"""

from __future__ import annotations

import dataclasses
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import workspace
from compress import CliArgs, process_one
from ffmpeg_io import FFmpegNotFoundError, ProbeError
from policy import EncodeResult, Layout, Outcome, Result, VideoInfo, default_options
from tests.helpers import make_info

SOURCE_SIZE = 1000
SOURCE_NAME = "IMG_0001.MOV"
RUN_ID = "20260831-143000"


# ---------------------------------------------------------------------------
# フェイクと共通の準備
# ---------------------------------------------------------------------------


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return workspace.ensure_layout(tmp_path)


@pytest.fixture
def source(layout: Layout) -> Path:
    path = layout.in_dir / SOURCE_NAME
    path.write_bytes(b"s" * SOURCE_SIZE)
    past = time.time() - 86400
    os.utime(path, (past, past))
    return path


def cli_args(layout: Layout, *, keep_in: bool = False, **option_overrides: object) -> CliArgs:
    options = dataclasses.replace(default_options(), **option_overrides)
    return CliArgs(root=layout.root, options=options, dry_run=False, keep_in=keep_in, debug=False)


def fake_probe(**overrides: object) -> Callable[[Path], VideoInfo]:
    def probe(path: Path) -> VideoInfo:
        return make_info(path=path, size_bytes=SOURCE_SIZE, **overrides)

    return probe


def failing_probe(path: Path) -> VideoInfo:
    raise ProbeError(path, "Invalid data found when processing input")


class FakeEncoder:
    """.part に指定サイズのダミーを書いて returncode を返すだけのフェイク。"""

    def __init__(self, output_size: int, returncode: int = 0) -> None:
        self.output_size = output_size
        self.returncode = returncode
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: tuple[str, ...]) -> EncodeResult:
        self.calls.append(args)
        Path(args[-1]).write_bytes(b"o" * self.output_size)
        return EncodeResult(self.returncode, "frame=  300 fps= 30\nConversion failed!", 1.5)


def probe_not_called(path: Path) -> VideoInfo:
    raise AssertionError(f"probe が呼ばれた: {path}")


def encode_not_called(args: tuple[str, ...]) -> EncodeResult:
    raise AssertionError(f"encode が呼ばれた: {args}")


def duration_not_called(path: Path) -> float | None:
    raise AssertionError(f"duration が呼ばれた: {path}")


def run(
    layout: Layout,
    source: Path,
    *,
    keep_in: bool = False,
    stable: bool = True,
    probe: Callable[[Path], VideoInfo] | None = None,
    encode: Callable[[tuple[str, ...]], EncodeResult] | None = None,
    duration: float | None = 10.0,
    **option_overrides: object,
) -> Result:
    return process_one(
        source,
        layout,
        cli_args(layout, keep_in=keep_in, **option_overrides),
        RUN_ID,
        stable_fn=lambda _: stable,
        probe_fn=probe or fake_probe(),
        encode_fn=encode or FakeEncoder(400),
        duration_fn=lambda _: duration,
    )


def files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def assert_required_fields(result: Result) -> None:
    """None を許容しないフィールドがすべて埋まっていること。"""
    for field in dataclasses.fields(Result):
        if "None" not in str(field.type):
            assert getattr(result, field.name) is not None, field.name
    assert result.run_id == RUN_ID
    assert result.source_name == SOURCE_NAME
    assert result.source_size == SOURCE_SIZE
    assert result.total_sec >= 0


def assert_probed_fields(result: Result) -> None:
    """probe できた素材では、解析結果の列がすべて埋まっていること。"""
    for name in (
        "video_codec", "resolution", "stored_resolution", "rotation",
        "fps", "fps_source", "bit_rate_bps", "bit_rate_source", "bpp",
    ):
        assert getattr(result, name) is not None, name


# ---------------------------------------------------------------------------
# 各経路
# ---------------------------------------------------------------------------


def test_encode_success(layout: Layout, source: Path) -> None:
    encoder = FakeEncoder(400)
    result = run(layout, source, encode=encoder)

    assert result.outcome is Outcome.ENCODED
    assert result.reason is None
    assert files(layout.out_dir) == ["IMG_0001_c.mp4"]
    assert (layout.out_dir / "IMG_0001_c.mp4").stat().st_size == 400
    assert files(layout.in_dir) == []
    assert files(layout.done_dir) == [SOURCE_NAME]
    assert result.output_name == "IMG_0001_c.mp4"
    assert result.output_size == 400
    assert result.size_ratio == pytest.approx(0.4)
    assert result.moved_to == str(Path("done") / SOURCE_NAME)
    assert result.encode_sec == 1.5
    assert result.ffmpeg_returncode == 0
    assert result.error_tail is None
    assert encoder.calls[0][-1].endswith("IMG_0001_c.mp4.part")
    assert_required_fields(result)
    assert_probed_fields(result)


def test_encode_success_copies_mtime(layout: Layout, source: Path) -> None:
    original_mtime = source.stat().st_mtime_ns
    run(layout, source)
    assert (layout.out_dir / "IMG_0001_c.mp4").stat().st_mtime_ns == original_mtime


def test_encode_success_does_not_overwrite_existing_output(layout: Layout, source: Path) -> None:
    existing = layout.out_dir / "IMG_0001_c.mp4"
    existing.write_bytes(b"previous")
    result = run(layout, source)
    assert result.output_name == "IMG_0001_c_2.mp4"
    assert existing.read_bytes() == b"previous"
    assert files(layout.out_dir) == ["IMG_0001_c.mp4", "IMG_0001_c_2.mp4"]


def test_no_gain_discards_output_and_skips_source(layout: Layout, source: Path) -> None:
    result = run(layout, source, encode=FakeEncoder(950))

    assert result.outcome is Outcome.SKIPPED
    assert result.reason == "no_gain"
    assert files(layout.out_dir) == []
    assert files(layout.in_dir) == []
    assert files(layout.root / "skipped" / "no_gain") == [SOURCE_NAME]
    assert result.output_name is None
    assert result.output_size == 950
    assert result.size_ratio == pytest.approx(0.95)
    assert result.moved_to == str(Path("skipped") / "no_gain" / SOURCE_NAME)
    assert_required_fields(result)
    assert_probed_fields(result)


def test_output_invalid_discards_output_and_keeps_source(layout: Layout, source: Path) -> None:
    result = run(layout, source, duration=3.0)

    assert result.outcome is Outcome.FAILED
    assert result.reason == "output_invalid"
    assert files(layout.out_dir) == []
    assert files(layout.in_dir) == [SOURCE_NAME]
    assert result.moved_to is None
    assert result.output_name is None
    assert result.error_tail is not None and "再生時間" in result.error_tail
    assert_required_fields(result)
    assert_probed_fields(result)


def test_verify_duration_disabled_skips_duration_check(layout: Layout, source: Path) -> None:
    result = process_one(
        source,
        layout,
        cli_args(layout, verify_duration=False),
        RUN_ID,
        stable_fn=lambda _: True,
        probe_fn=fake_probe(),
        encode_fn=FakeEncoder(400),
        duration_fn=duration_not_called,
    )
    assert result.outcome is Outcome.ENCODED


def test_unknown_output_duration_is_accepted(layout: Layout, source: Path) -> None:
    assert run(layout, source, duration=None).outcome is Outcome.ENCODED


def test_encode_failed_removes_part_and_keeps_source(layout: Layout, source: Path) -> None:
    result = run(layout, source, encode=FakeEncoder(123, returncode=1))

    assert result.outcome is Outcome.FAILED
    assert result.reason == "encode_failed"
    assert files(layout.out_dir) == []
    assert files(layout.in_dir) == [SOURCE_NAME]
    assert result.moved_to is None
    assert result.ffmpeg_returncode == 1
    assert result.error_tail is not None and "Conversion failed!" in result.error_tail
    assert result.output_size is None
    assert_required_fields(result)
    assert_probed_fields(result)


def test_encode_oserror_is_folded(layout: Layout, source: Path) -> None:
    def encode(args: tuple[str, ...]) -> EncodeResult:
        Path(args[-1]).write_bytes(b"partial")
        raise PermissionError("access denied")

    result = run(layout, source, encode=encode)

    assert result.outcome is Outcome.FAILED
    assert result.reason == "encode_failed"
    assert files(layout.out_dir) == []
    assert files(layout.in_dir) == [SOURCE_NAME]
    assert result.error_tail is not None and "access denied" in result.error_tail


def test_probe_failed_moves_source_to_failed(layout: Layout, source: Path) -> None:
    result = run(layout, source, probe=failing_probe, encode=encode_not_called)

    assert result.outcome is Outcome.FAILED
    assert result.reason == "probe_failed"
    assert files(layout.failed_dir) == [SOURCE_NAME]
    assert files(layout.in_dir) == []
    assert files(layout.out_dir) == []
    assert result.moved_to == str(Path("failed") / SOURCE_NAME)
    assert result.error_tail == "Invalid data found when processing input"
    assert_required_fields(result)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"color_transfer": "arib-std-b67"}, "hdr"),
        ({"video_bit_rate_bps": 1000}, "low_bpp"),
        ({"video_codec": "prores"}, "unsupported"),
    ],
)
def test_pre_encode_skips(layout: Layout, source: Path, overrides: dict[str, object], reason: str) -> None:
    result = run(layout, source, probe=fake_probe(**overrides), encode=encode_not_called)

    assert result.outcome is Outcome.SKIPPED
    assert result.reason == reason
    assert files(layout.root / "skipped" / reason) == [SOURCE_NAME]
    assert files(layout.in_dir) == []
    assert files(layout.out_dir) == []
    assert result.moved_to == str(Path("skipped") / reason / SOURCE_NAME)
    assert result.encode_sec is None
    assert_required_fields(result)
    assert_probed_fields(result)


def test_not_ready_keeps_source(layout: Layout, source: Path) -> None:
    result = run(layout, source, stable=False, probe=probe_not_called, encode=encode_not_called)

    assert result.outcome is Outcome.SKIPPED
    assert result.reason == "not_ready"
    assert files(layout.in_dir) == [SOURCE_NAME]
    assert result.moved_to is None
    assert_required_fields(result)


def test_source_vanishing_during_stability_wait_is_not_ready(layout: Layout, source: Path) -> None:
    def vanished(path: Path) -> bool:
        path.unlink()
        raise FileNotFoundError(str(path))

    result = process_one(
        source, layout, cli_args(layout), RUN_ID,
        stable_fn=vanished,
        probe_fn=probe_not_called,
        encode_fn=encode_not_called,
        duration_fn=duration_not_called,
    )
    assert result.outcome is Outcome.SKIPPED
    assert result.reason == "not_ready"
    assert result.source_size == 0
    assert result.error_tail is not None


def test_move_failure_keeps_outcome_and_records_error(layout: Layout, source: Path) -> None:
    layout.done_dir.rmdir()
    result = run(layout, source)

    assert result.outcome is Outcome.ENCODED
    assert result.moved_to is None
    assert files(layout.in_dir) == [SOURCE_NAME]
    assert files(layout.out_dir) == ["IMG_0001_c.mp4"]
    assert result.error_tail is not None and "移動できなかった" in result.error_tail


# ---------------------------------------------------------------------------
# --keep-in
# ---------------------------------------------------------------------------

KEEP_IN_CASES: dict[str, dict[str, object]] = {
    "encoded": {},
    "no_gain": {"encode": FakeEncoder(950)},
    "output_invalid": {"duration": 3.0},
    "encode_failed": {"encode": FakeEncoder(10, returncode=1)},
    "probe_failed": {"probe": failing_probe},
    "hdr": {"probe": fake_probe(color_transfer="smpte2084")},
    "low_bpp": {"probe": fake_probe(video_bit_rate_bps=1000)},
    "unsupported": {"probe": fake_probe(video_codec="dnxhd")},
    "not_ready": {"stable": False},
}


@pytest.mark.parametrize("case", list(KEEP_IN_CASES))
def test_keep_in_never_moves_source(layout: Layout, source: Path, case: str) -> None:
    result = run(layout, source, keep_in=True, **KEEP_IN_CASES[case])

    expected_reason = None if case == "encoded" else case
    assert result.reason == expected_reason
    assert files(layout.in_dir) == [SOURCE_NAME]
    assert source.read_bytes() == b"s" * SOURCE_SIZE
    assert result.moved_to is None
    for directory in (layout.done_dir, layout.failed_dir, *(layout.root / "skipped").iterdir()):
        assert files(directory) == []
    expected_out = ["IMG_0001_c.mp4"] if case == "encoded" else []
    assert files(layout.out_dir) == expected_out


# ---------------------------------------------------------------------------
# 実行全体を止める例外
# ---------------------------------------------------------------------------


def test_ffmpeg_not_found_during_encode_is_reraised(layout: Layout, source: Path) -> None:
    def encode(_: tuple[str, ...]) -> EncodeResult:
        raise FFmpegNotFoundError("ffmpeg が見つからない")

    with pytest.raises(FFmpegNotFoundError):
        run(layout, source, encode=encode)
    assert files(layout.in_dir) == [SOURCE_NAME]
    assert files(layout.out_dir) == []


def test_ffmpeg_not_found_during_probe_is_reraised(layout: Layout, source: Path) -> None:
    def probe(_: Path) -> VideoInfo:
        raise FFmpegNotFoundError("ffprobe が見つからない")

    with pytest.raises(FFmpegNotFoundError):
        run(layout, source, probe=probe)
    assert files(layout.in_dir) == [SOURCE_NAME]
    assert files(layout.failed_dir) == []
