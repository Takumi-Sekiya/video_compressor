"""run() の終了コードと dry-run の不変条件。実 ffmpeg は起動せず、フェイクを注入する。"""

from __future__ import annotations

import csv
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import compress
import workspace
from ffmpeg_io import FFmpegNotFoundError, ProbeError
from policy import EncodeResult, Layout, VideoInfo
from tests.helpers import make_info

ALL_OPTIONS = (
    "--root", "--crf", "--preset", "--short-side", "--no-downscale", "--allow-hdr",
    "--min-gain-ratio", "--bpp-efficient", "--bpp-legacy", "--strip-location",
    "--no-verify-duration", "--dry-run", "--keep-in", "--debug",
)


# ---------------------------------------------------------------------------
# フェイク
# ---------------------------------------------------------------------------


def fake_check_tools() -> tuple[str, str]:
    return "ffmpeg version fake", "ffprobe version fake"


def missing_tools() -> tuple[str, str]:
    raise FFmpegNotFoundError("ffmpeg が見つからない")


def fake_probe(path: Path) -> VideoInfo:
    if "broken" in path.name:
        raise ProbeError(path, "Invalid data found when processing input")
    if "hdr" in path.name:
        return make_info(path=path, size_bytes=path.stat().st_size, color_transfer="arib-std-b67")
    return make_info(
        path=path, size_bytes=path.stat().st_size,
        width=3840, height=2160, rotation=90, video_bit_rate_bps=40_000_000,
    )


def fake_encode(args: tuple[str, ...]) -> EncodeResult:
    Path(args[-1]).write_bytes(b"o" * 10)
    return EncodeResult(0, "", 0.1)


def fake_duration(_: Path) -> float | None:
    return 10.0


def run(
    argv: list[str],
    *,
    check_tools_fn: Callable[[], tuple[str, str]] = fake_check_tools,
    encode_fn: Callable[[tuple[str, ...]], EncodeResult] = fake_encode,
) -> int:
    return compress.run(
        argv,
        check_tools_fn=check_tools_fn,
        probe_fn=fake_probe,
        encode_fn=encode_fn,
        duration_fn=fake_duration,
    )


def add_source(layout: Layout, name: str, size: int = 1000) -> Path:
    layout.in_dir.mkdir(parents=True, exist_ok=True)
    path = layout.in_dir / name
    path.write_bytes(b"s" * size)
    past = time.time() - 86400
    os.utime(path, (past, past))
    return path


def snapshot(root: Path) -> dict[str, tuple[bool, int, int]]:
    """root 自身と配下すべての (ディレクトリか, サイズ, mtime_ns)。"""
    entries = {".": (True, 0, root.stat().st_mtime_ns)}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            stat = path.stat()
            key = str(path.relative_to(root))
            entries[key] = (path.is_dir(), 0 if path.is_dir() else stat.st_size, stat.st_mtime_ns)
    return entries


def read_csv(layout: Layout) -> list[dict[str, str]]:
    [csv_path] = list(layout.logs_dir.glob("*.csv"))
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# --help / parse_args
# ---------------------------------------------------------------------------


def test_help_lists_all_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        compress.parse_args(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for option in ALL_OPTIONS:
        assert option in out, option


def test_parse_args_defaults(tmp_path: Path) -> None:
    args = compress.parse_args(["--root", str(tmp_path)])
    assert args.root == tmp_path
    assert args.options == compress.policy.default_options()
    assert (args.dry_run, args.keep_in, args.debug) == (False, False, False)


def test_parse_args_overrides(tmp_path: Path) -> None:
    args = compress.parse_args([
        "--root", str(tmp_path), "--crf", "24", "--preset", "medium", "--short-side", "720",
        "--no-downscale", "--allow-hdr", "--min-gain-ratio", "0.8", "--bpp-efficient", "0.03",
        "--bpp-legacy", "0.06", "--strip-location", "--no-verify-duration",
        "--dry-run", "--keep-in", "--debug",
    ])
    options = args.options
    assert (options.crf, options.preset, options.short_side) == (24, "medium", 720)
    assert options.downscale is False
    assert options.allow_hdr is True
    assert options.min_gain_ratio == 0.8
    assert (options.bpp_threshold_efficient, options.bpp_threshold_legacy) == (0.03, 0.06)
    assert options.strip_location is True
    assert options.verify_duration is False
    assert (args.dry_run, args.keep_in, args.debug) == (True, True, True)


@pytest.mark.parametrize("value", ["1081", "0", "-2"])
def test_parse_args_rejects_invalid_short_side(tmp_path: Path, value: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        compress.parse_args(["--root", str(tmp_path), "--short-side", value])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# dry-run は <root> を一切変更しない
# ---------------------------------------------------------------------------


def test_dry_run_does_not_modify_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    layout = Layout(tmp_path)
    add_source(layout, "IMG_0001.MOV")
    add_source(layout, "IMG_0002_hdr.MOV")
    add_source(layout, "IMG_0003_broken.MOV")
    stale_part = tmp_path / "out" / "old_c.mp4.part"
    stale_part.parent.mkdir()
    stale_part.write_bytes(b"x")
    past = time.time() - 7200
    os.utime(stale_part, (past, past))
    before = snapshot(tmp_path)

    code = run(["--root", str(tmp_path), "--dry-run", "--debug"])

    assert code == 0
    assert snapshot(tmp_path) == before
    out = capsys.readouterr().out
    assert "判定: ENCODE" in out
    assert "判定: SKIP（hdr）" in out
    assert "PROBE_FAILED" in out
    assert "-tag:v hvc1" in out
    assert "scale=1080:-2" in out
    assert "予約パス" in out


def test_dry_run_on_fresh_root_creates_nothing(tmp_path: Path) -> None:
    assert run(["--root", str(tmp_path), "--dry-run"]) == 0
    assert list(tmp_path.iterdir()) == []


def test_dry_run_ignores_lock_of_running_batch(tmp_path: Path) -> None:
    layout = workspace.ensure_layout(tmp_path)
    add_source(layout, "IMG_0001.MOV")
    with workspace.acquire_lock(layout):
        before = snapshot(tmp_path)
        assert run(["--root", str(tmp_path), "--dry-run"]) == 0
        assert snapshot(tmp_path) == before


def test_dry_run_rejects_missing_root(tmp_path: Path) -> None:
    assert run(["--root", str(tmp_path / "missing"), "--dry-run"]) == 2


def test_dry_run_without_tools(tmp_path: Path) -> None:
    assert run(["--root", str(tmp_path), "--dry-run"], check_tools_fn=missing_tools) == 2


# ---------------------------------------------------------------------------
# 終了コード
# ---------------------------------------------------------------------------


def test_lock_held_exits_2_and_keeps_lock(tmp_path: Path) -> None:
    layout = workspace.ensure_layout(tmp_path)
    add_source(layout, "IMG_0001.MOV")
    layout.lock_path.write_text("pid=1\n", encoding="utf-8")

    assert run(["--root", str(tmp_path)]) == 2

    assert layout.lock_path.read_text(encoding="utf-8") == "pid=1\n"
    assert [p.name for p in layout.in_dir.iterdir()] == ["IMG_0001.MOV"]
    assert list(layout.logs_dir.iterdir()) == []


def test_no_materials_exits_0(tmp_path: Path) -> None:
    assert run(["--root", str(tmp_path)]) == 0
    layout = Layout(tmp_path)
    assert read_csv(layout) == []
    assert len(list(layout.logs_dir.glob("*.meta.json"))) == 1
    assert not layout.lock_path.exists()


def test_all_success_exits_0(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    add_source(layout, "IMG_0001.MOV")
    add_source(layout, "IMG_0002_hdr.MOV")

    assert run(["--root", str(tmp_path)]) == 0

    rows = read_csv(layout)
    assert [(r["source_name"], r["outcome"], r["reason"]) for r in rows] == [
        ("IMG_0001.MOV", "encoded", ""),
        ("IMG_0002_hdr.MOV", "skipped", "hdr"),
    ]
    assert [p.name for p in layout.out_dir.iterdir()] == ["IMG_0001_c.mp4"]
    assert not layout.lock_path.exists()


def test_one_failure_exits_1(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    add_source(layout, "IMG_0001.MOV")
    add_source(layout, "IMG_0002_broken.MOV")

    assert run(["--root", str(tmp_path)]) == 1

    rows = read_csv(layout)
    assert [r["outcome"] for r in rows] == ["encoded", "failed"]
    assert [p.name for p in layout.failed_dir.iterdir()] == ["IMG_0002_broken.MOV"]
    assert not layout.lock_path.exists()


def test_missing_tools_exits_2(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    add_source(layout, "IMG_0001.MOV")
    assert run(["--root", str(tmp_path)], check_tools_fn=missing_tools) == 2
    assert not layout.lock_path.exists()
    assert [p.name for p in layout.in_dir.iterdir()] == ["IMG_0001.MOV"]


def test_invalid_root_exits_2(tmp_path: Path) -> None:
    assert run(["--root", str(tmp_path / "missing")]) == 2


def test_ffmpeg_vanishing_mid_run_exits_3(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    add_source(layout, "IMG_0001.MOV")
    add_source(layout, "IMG_0002.MOV")

    def vanish(_: tuple[str, ...]) -> EncodeResult:
        raise FFmpegNotFoundError("ffmpeg が消えた")

    assert run(["--root", str(tmp_path)], encode_fn=vanish) == 3
    assert not layout.lock_path.exists()
    assert sorted(p.name for p in layout.in_dir.iterdir()) == ["IMG_0001.MOV", "IMG_0002.MOV"]


def test_real_run_cleans_only_stale_parts(tmp_path: Path) -> None:
    layout = workspace.ensure_layout(tmp_path)
    stale = layout.out_dir / "old_c.mp4.part"
    fresh = layout.out_dir / "new_c.mp4.part"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    past = time.time() - 7200
    os.utime(stale, (past, past))

    assert run(["--root", str(tmp_path)]) == 0

    assert not stale.exists()
    assert fresh.exists()


def test_keep_in_run_leaves_sources(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    add_source(layout, "IMG_0001.MOV")
    add_source(layout, "IMG_0002_broken.MOV")

    assert run(["--root", str(tmp_path), "--keep-in"]) == 1

    assert sorted(p.name for p in layout.in_dir.iterdir()) == ["IMG_0001.MOV", "IMG_0002_broken.MOV"]
    assert [p.name for p in layout.out_dir.iterdir()] == ["IMG_0001_c.mp4"]
