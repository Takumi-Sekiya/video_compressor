from __future__ import annotations

import csv
import dataclasses
import json
import re
from pathlib import Path

import pytest

import policy
import runlog
from policy import Layout, Outcome, Result
from tests.helpers import make_context


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    layout = Layout(tmp_path)
    layout.logs_dir.mkdir()
    return layout


def _result(name: str, outcome: Outcome = Outcome.ENCODED, **overrides: object) -> Result:
    base = Result(
        run_id="20260831-143000",
        source_name=name,
        outcome=outcome,
        reason=None,
        source_size=1000,
        output_name=f"{name}_c.mp4",
        output_size=400,
        size_ratio=0.4,
        moved_to=f"done/{name}",
        encode_sec=12.5,
        total_sec=13.0,
        video_codec="hevc",
        resolution="1080x1920",
        stored_resolution="1920x1080",
        rotation=90,
        fps=29.97,
        fps_source="avg",
        bit_rate_bps=10_000_000,
        bit_rate_source="video_stream",
        bpp=0.16,
        color_transfer="bt709",
        audio_codec="aac",
        ffmpeg_returncode=0,
        error_tail=None,
    )
    return dataclasses.replace(base, **overrides)


# --- create_run_log ---


def test_create_run_log_same_run_id_does_not_overwrite(layout: Layout) -> None:
    context = make_context(layout.root)
    first_csv, first_id = runlog.create_run_log(layout, context)
    runlog.append_result(first_csv, _result("a.mov"))
    first_content = first_csv.read_bytes()

    second_csv, second_id = runlog.create_run_log(layout, context)
    third_csv, third_id = runlog.create_run_log(layout, context)

    assert first_id == "20260831-143000"
    assert second_id == "20260831-143000-2"
    assert third_id == "20260831-143000-3"
    assert second_csv == layout.logs_dir / "20260831-143000-2.csv"
    assert (layout.logs_dir / "20260831-143000-2.meta.json").exists()
    assert first_csv.read_bytes() == first_content
    assert third_csv.exists()


def test_meta_json_round_trips(layout: Layout) -> None:
    context = make_context(layout.root, git_commit="a3f19c2-dirty")
    runlog.create_run_log(layout, context)
    runlog.create_run_log(layout, context)

    meta = json.loads((layout.logs_dir / "20260831-143000-2.meta.json").read_text(encoding="utf-8"))

    assert meta["run_id"] == "20260831-143000-2"
    assert meta["started_at"] == "2026-08-31T14:30:00+09:00"
    assert meta["git_commit"] == "a3f19c2-dirty"
    assert meta["tool_version"] == policy.TOOL_VERSION
    assert meta["argv"] == ["--root", str(layout.root)]
    assert meta["dry_run"] is False
    assert meta["keep_in"] is False
    assert set(meta["options"]) == {f.name for f in dataclasses.fields(policy.Options)}
    assert meta["options"]["audio_copy_codecs"] == ["aac"]
    assert meta["options"]["crf"] == 26
    assert meta["policy_constants"] == policy.export_constants()


def test_csv_header_only_and_bom(layout: Layout) -> None:
    csv_path, _ = runlog.create_run_log(layout, make_context(layout.root))
    raw = csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows == [list(runlog.CSV_COLUMNS)]


# --- append_result ---


def test_append_result_three_rows(layout: Layout) -> None:
    csv_path, _ = runlog.create_run_log(layout, make_context(layout.root))
    runlog.append_result(csv_path, _result("動画1.mov"))
    runlog.append_result(
        csv_path,
        _result(
            "b.mov",
            Outcome.SKIPPED,
            reason="no_gain",
            output_name=None,
            moved_to="skipped/no_gain/b.mov",
        ),
    )
    runlog.append_result(
        csv_path,
        _result(
            "c.mov",
            Outcome.FAILED,
            reason="encode_failed",
            output_name=None,
            output_size=None,
            size_ratio=None,
            moved_to=None,
            ffmpeg_returncode=1,
            error_tail="line1\nline2, with comma",
        ),
    )

    assert csv_path.read_bytes().count(b"\xef\xbb\xbf") == 1
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 3
    assert [r["source_name"] for r in rows] == ["動画1.mov", "b.mov", "c.mov"]
    assert [r["outcome"] for r in rows] == ["encoded", "skipped", "failed"]
    assert rows[0]["reason"] == ""
    assert rows[1]["reason"] == "no_gain"
    assert rows[2]["output_size"] == ""
    assert rows[2]["error_tail"] == "line1\nline2, with comma"
    assert rows[0]["rotation"] == "90"


# --- collect_git_commit / collect_environment ---


def test_collect_git_commit_without_repository(tmp_path: Path) -> None:
    assert runlog.collect_git_commit(tmp_path) is None


def test_collect_git_commit_missing_directory(tmp_path: Path) -> None:
    assert runlog.collect_git_commit(tmp_path / "missing") is None


def test_collect_git_commit_default_is_this_repository() -> None:
    commit = runlog.collect_git_commit()
    assert commit is None or re.fullmatch(r"[0-9a-f]{4,}(-dirty)?", commit)


def test_collect_environment() -> None:
    python_version, platform_name, tool_version = runlog.collect_environment()
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", python_version)
    assert platform_name
    assert tool_version == policy.TOOL_VERSION


# --- dump_raw_probe ---


def test_dump_raw_probe(layout: Layout) -> None:
    payload: dict[str, object] = {"streams": [], "format": {"filename": "日本語.mov"}}
    runlog.dump_raw_probe(layout, "rid", Path("in/日本語.mov"), payload)
    saved = layout.logs_dir / "rid-probe" / "日本語.mov.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == payload


def test_dump_raw_probe_never_raises(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "file"
    not_a_dir.write_bytes(b"")
    runlog.dump_raw_probe(Layout(not_a_dir), "rid", Path("a.mov"), {"x": 1})
