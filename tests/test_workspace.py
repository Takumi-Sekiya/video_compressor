from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import workspace
from policy import Layout, SkipReason


def _age(path: Path, seconds: float) -> None:
    past = time.time() - seconds
    os.utime(path, (past, past))


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return workspace.ensure_layout(tmp_path)


# --- ensure_layout ---


def test_ensure_layout_creates_all_directories(tmp_path: Path) -> None:
    layout = workspace.ensure_layout(tmp_path)
    for directory in (layout.in_dir, layout.out_dir, layout.done_dir, layout.failed_dir, layout.logs_dir):
        assert directory.is_dir()
    for reason in SkipReason:
        assert layout.skipped_dir(reason).is_dir() is (reason is not SkipReason.NOT_READY)


def test_ensure_layout_is_idempotent_and_leaves_no_smoke_file(tmp_path: Path) -> None:
    first = workspace.ensure_layout(tmp_path)
    second = workspace.ensure_layout(tmp_path)
    assert first == second
    assert list(first.in_dir.iterdir()) == []
    assert list(first.done_dir.iterdir()) == []


def test_ensure_layout_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        workspace.ensure_layout(tmp_path / "missing")


def test_ensure_layout_rejects_file_root(tmp_path: Path) -> None:
    file_root = tmp_path / "file"
    file_root.write_bytes(b"")
    with pytest.raises(NotADirectoryError):
        workspace.ensure_layout(file_root)


# --- acquire_lock ---


def test_acquire_lock_creates_and_removes(layout: Layout) -> None:
    with workspace.acquire_lock(layout):
        assert layout.lock_path.exists()
        assert f"pid={os.getpid()}" in layout.lock_path.read_text(encoding="utf-8")
    assert not layout.lock_path.exists()


def test_acquire_lock_twice_raises_and_keeps_existing_lock(layout: Layout) -> None:
    with workspace.acquire_lock(layout):
        with pytest.raises(workspace.LockHeldError):
            with workspace.acquire_lock(layout):
                pass
        assert layout.lock_path.exists()
    assert not layout.lock_path.exists()


def test_acquire_lock_removed_on_exception(layout: Layout) -> None:
    with pytest.raises(KeyError):
        with workspace.acquire_lock(layout):
            raise KeyError("boom")
    assert not layout.lock_path.exists()


# --- wait_until_stable ---


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_wait_until_stable_quiet_mtime_returns_without_sleep(tmp_path: Path) -> None:
    path = tmp_path / "a.mov"
    path.write_bytes(b"x" * 100)
    _age(path, 60)
    clock = FakeClock()
    assert workspace.wait_until_stable(path, sleep_fn=clock.sleep, now_fn=clock.monotonic)
    assert clock.sleeps == []


def test_wait_until_stable_growing_file_times_out(tmp_path: Path) -> None:
    path = tmp_path / "a.mov"
    path.write_bytes(b"x")
    clock = FakeClock()

    def grow(seconds: float) -> None:
        clock.sleep(seconds)
        with path.open("ab") as f:
            f.write(b"x")

    started = time.monotonic()
    result = workspace.wait_until_stable(
        path, interval_sec=5.0, timeout_sec=900.0, sleep_fn=grow, now_fn=clock.monotonic
    )
    assert result is False
    assert time.monotonic() - started < 1.0
    assert clock.now >= 900.0


def test_wait_until_stable_recent_but_constant_size(tmp_path: Path) -> None:
    path = tmp_path / "a.mov"
    path.write_bytes(b"x" * 10)
    clock = FakeClock()
    assert workspace.wait_until_stable(
        path, interval_sec=5.0, required_matches=3, sleep_fn=clock.sleep, now_fn=clock.monotonic
    )
    assert clock.sleeps == [5.0, 5.0]


def test_wait_until_stable_file_vanishes(tmp_path: Path) -> None:
    path = tmp_path / "a.mov"
    path.write_bytes(b"x")
    clock = FakeClock()

    def vanish(seconds: float) -> None:
        clock.sleep(seconds)
        path.unlink()

    with pytest.raises(FileNotFoundError):
        workspace.wait_until_stable(path, sleep_fn=vanish, now_fn=clock.monotonic)


# --- reserve_output_path / part_path ---


def test_reserve_output_path_numbers_sequentially(layout: Layout) -> None:
    source = layout.in_dir / "IMG_0001.MOV"
    first = workspace.reserve_output_path(layout, source)
    assert first == layout.out_dir / "IMG_0001_c.mp4"
    first.write_bytes(b"")
    second = workspace.reserve_output_path(layout, source)
    assert second == layout.out_dir / "IMG_0001_c_2.mp4"
    second.write_bytes(b"")
    assert workspace.reserve_output_path(layout, source) == layout.out_dir / "IMG_0001_c_3.mp4"


def test_reserve_output_path_does_not_create_file(layout: Layout) -> None:
    workspace.reserve_output_path(layout, layout.in_dir / "a.mov")
    assert list(layout.out_dir.iterdir()) == []


def test_part_path() -> None:
    assert workspace.part_path(Path("out/a_c.mp4")) == Path("out/a_c.mp4.part")


# --- cleanup_stale_parts ---


def test_cleanup_stale_parts_removes_only_old_parts(layout: Layout) -> None:
    old_part = layout.out_dir / "old_c.mp4.part"
    new_part = layout.out_dir / "new_c.mp4.part"
    old_mp4 = layout.out_dir / "done_c.mp4"
    for path in (old_part, new_part, old_mp4):
        path.write_bytes(b"x")
    _age(old_part, 7200)
    _age(old_mp4, 7200)

    removed = workspace.cleanup_stale_parts(layout, max_age_sec=3600)

    assert removed == [old_part]
    assert not old_part.exists()
    assert new_part.exists()
    assert old_mp4.exists()


# --- scan_inbox ---


def test_scan_inbox_filters_and_sorts(layout: Layout) -> None:
    for name in ("b.MOV", "a.mp4", "c.M4V", "d.txt", "e.mp4.part", ".rename-test-1.tmp"):
        (layout.in_dir / name).write_bytes(b"")
    (layout.in_dir / "sub.mov").mkdir()
    (layout.in_dir / "nested").mkdir()
    (layout.in_dir / "nested" / "f.mov").write_bytes(b"")

    names = [p.name for p in workspace.scan_inbox(layout)]
    assert names == ["a.mp4", "b.MOV", "c.M4V"]


# --- file_size / finalize_output / discard_output ---


def test_file_size(tmp_path: Path) -> None:
    path = tmp_path / "x"
    path.write_bytes(b"12345")
    assert workspace.file_size(path) == 5


def test_finalize_output_renames(layout: Layout) -> None:
    final = layout.out_dir / "a_c.mp4"
    part = workspace.part_path(final)
    part.write_bytes(b"data")
    workspace.finalize_output(part, final)
    assert final.read_bytes() == b"data"
    assert not part.exists()


def test_finalize_output_refuses_to_overwrite(layout: Layout) -> None:
    final = layout.out_dir / "a_c.mp4"
    part = workspace.part_path(final)
    final.write_bytes(b"existing")
    part.write_bytes(b"new")
    with pytest.raises(FileExistsError):
        workspace.finalize_output(part, final)
    assert final.read_bytes() == b"existing"
    assert part.exists()


def test_finalize_output_missing_part(layout: Layout) -> None:
    final = layout.out_dir / "a_c.mp4"
    with pytest.raises(FileNotFoundError):
        workspace.finalize_output(workspace.part_path(final), final)


def test_discard_output_is_idempotent(layout: Layout) -> None:
    part = layout.out_dir / "a_c.mp4.part"
    part.write_bytes(b"x")
    workspace.discard_output(part)
    workspace.discard_output(part)
    assert not part.exists()


# --- move_source ---


def test_move_source_moves(layout: Layout) -> None:
    source = layout.in_dir / "a.mov"
    source.write_bytes(b"original")
    moved = workspace.move_source(source, layout.done_dir)
    assert moved == layout.done_dir / "a.mov"
    assert moved.read_bytes() == b"original"
    assert not source.exists()


def test_move_source_numbers_on_collision_without_overwrite(layout: Layout) -> None:
    (layout.done_dir / "a.mov").write_bytes(b"first")
    (layout.done_dir / "a_2.mov").write_bytes(b"second")
    source = layout.in_dir / "a.mov"
    source.write_bytes(b"third")

    moved = workspace.move_source(source, layout.done_dir)

    assert moved == layout.done_dir / "a_3.mov"
    assert (layout.done_dir / "a.mov").read_bytes() == b"first"
    assert (layout.done_dir / "a_2.mov").read_bytes() == b"second"
    assert moved.read_bytes() == b"third"


def test_move_source_failure_keeps_source(layout: Layout) -> None:
    source = layout.in_dir / "a.mov"
    source.write_bytes(b"x")
    with pytest.raises(OSError):
        workspace.move_source(source, layout.root / "no_such_dir")
    assert source.exists()


# --- copy_mtime / check_free_space ---


def test_copy_mtime(tmp_path: Path) -> None:
    source = tmp_path / "src"
    dest = tmp_path / "dst"
    source.write_bytes(b"")
    dest.write_bytes(b"")
    _age(source, 86400)
    workspace.copy_mtime(source, dest)
    assert dest.stat().st_mtime_ns == source.stat().st_mtime_ns


def test_copy_mtime_never_raises(tmp_path: Path) -> None:
    workspace.copy_mtime(tmp_path / "missing", tmp_path / "also_missing")


def test_check_free_space(layout: Layout) -> None:
    assert workspace.check_free_space(layout, 0)
    assert not workspace.check_free_space(layout, 1 << 62)
