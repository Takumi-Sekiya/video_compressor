"""CSV / meta.json の書き出しと、環境情報（git / Python / OS）の収集。

判定は持たない。素材ファイルも操作しない。
"""

from __future__ import annotations

import csv
import json
import platform
import subprocess
from pathlib import Path

import policy
from policy import Layout, Options, Result, RunContext

CSV_ENCODING: str = "utf-8-sig"
META_ENCODING: str = "utf-8"
GIT_TIMEOUT_SEC: float = 10.0

CSV_COLUMNS: tuple[str, ...] = (
    "run_id",
    "source_name",
    "outcome",
    "reason",
    "source_size",
    "output_name",
    "output_size",
    "size_ratio",
    "moved_to",
    "encode_sec",
    "total_sec",
    "video_codec",
    "resolution",
    "stored_resolution",
    "rotation",
    "fps",
    "fps_source",
    "bit_rate_bps",
    "bit_rate_source",
    "bpp",
    "color_transfer",
    "audio_codec",
    "ffmpeg_returncode",
    "error_tail",
)


def collect_environment() -> tuple[str, str, str]:
    """(python_version, platform, tool_version) を返す。"""
    return platform.python_version(), platform.platform(), policy.TOOL_VERSION


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT_SEC,
    )


def collect_git_commit(repo_dir: Path | None = None) -> str | None:
    """コードのバージョン識別子を取得する。

    git rev-parse --short HEAD を実行する。作業ツリーに未コミットの変更が
    あれば末尾に "-dirty" を付ける。

    Args:
        repo_dir: 既定は Path(__file__).resolve().parent。
            cwd を使わない（--root 側で実行されると別リポジトリを拾うため）。

    Returns:
        コミットハッシュ。git がない、リポジトリでない、失敗した場合は None。
        再現情報の取得失敗が実行を止めてはならないため、例外は送出しない。
    """
    directory = repo_dir if repo_dir is not None else Path(__file__).resolve().parent
    try:
        head = _git(["rev-parse", "--short", "HEAD"], directory)
        commit = head.stdout.strip()
        if head.returncode != 0 or not commit:
            return None
        status = _git(["status", "--porcelain", "--untracked-files=no"], directory)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if status.returncode == 0 and status.stdout.strip():
        commit += "-dirty"
    return commit


def _options_to_json(options: Options) -> dict[str, object]:
    return {
        "video_codec": options.video_codec,
        "crf": options.crf,
        "preset": options.preset,
        "short_side": options.short_side,
        "downscale": options.downscale,
        "allow_hdr": options.allow_hdr,
        "min_gain_ratio": options.min_gain_ratio,
        "bpp_threshold_efficient": options.bpp_threshold_efficient,
        "bpp_threshold_legacy": options.bpp_threshold_legacy,
        "audio_copy_codecs": sorted(options.audio_copy_codecs),
        "audio_fallback_bitrate": options.audio_fallback_bitrate,
        "strip_location": options.strip_location,
        "verify_duration": options.verify_duration,
    }


def _meta_to_json(context: RunContext, run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "started_at": context.started_at.isoformat(timespec="seconds"),
        "tool_version": context.tool_version,
        "git_commit": context.git_commit,
        "python_version": context.python_version,
        "platform": context.platform,
        "ffmpeg_version": context.ffmpeg_version,
        "ffprobe_version": context.ffprobe_version,
        "argv": list(context.argv),
        "root": str(context.root),
        "dry_run": context.dry_run,
        "keep_in": context.keep_in,
        "options": _options_to_json(context.options),
        "policy_constants": policy.export_constants(),
    }


def create_run_log(layout: Layout, context: RunContext) -> tuple[Path, str]:
    """CSV とメタ JSON を作成し、(CSV のパス, 確定した run_id) を返す。

    open(path, "x") で作成する。FileExistsError なら run_id に -2, -3 と
    suffix を付けて再試行する。同一秒の再起動で前回のログを truncate しないため。
    メタ JSON はこの時点で完成させて書き切る。policy_constants には
    policy.export_constants() の結果を書く。
    CSV はヘッダ行のみを書いた状態で返す。エンコーディングは utf-8-sig（R-7）。
    """
    attempt = 1
    while True:
        run_id = context.run_id if attempt == 1 else f"{context.run_id}-{attempt}"
        attempt += 1
        csv_path = layout.logs_dir / f"{run_id}.csv"
        meta_path = layout.logs_dir / f"{run_id}.meta.json"
        try:
            csv_file = open(csv_path, "x", encoding=CSV_ENCODING, newline="")
        except FileExistsError:
            continue
        try:
            meta_file = open(meta_path, "x", encoding=META_ENCODING)
        except FileExistsError:
            csv_file.close()
            csv_path.unlink()
            continue
        with csv_file:
            csv.writer(csv_file).writerow(CSV_COLUMNS)
        with meta_file:
            json.dump(_meta_to_json(context, run_id), meta_file, ensure_ascii=False, indent=2)
            meta_file.write("\n")
        return csv_path, run_id


def _cell(value: object) -> object:
    return "" if value is None else value


def _result_to_row(result: Result) -> dict[str, object]:
    return {
        "run_id": result.run_id,
        "source_name": result.source_name,
        "outcome": result.outcome.value,
        "reason": _cell(result.reason),
        "source_size": result.source_size,
        "output_name": _cell(result.output_name),
        "output_size": _cell(result.output_size),
        "size_ratio": _cell(result.size_ratio),
        "moved_to": _cell(result.moved_to),
        "encode_sec": _cell(result.encode_sec),
        "total_sec": result.total_sec,
        "video_codec": _cell(result.video_codec),
        "resolution": _cell(result.resolution),
        "stored_resolution": _cell(result.stored_resolution),
        "rotation": _cell(result.rotation),
        "fps": _cell(result.fps),
        "fps_source": _cell(result.fps_source),
        "bit_rate_bps": _cell(result.bit_rate_bps),
        "bit_rate_source": _cell(result.bit_rate_source),
        "bpp": _cell(result.bpp),
        "color_transfer": _cell(result.color_transfer),
        "audio_codec": _cell(result.audio_codec),
        "ffmpeg_returncode": _cell(result.ffmpeg_returncode),
        "error_tail": _cell(result.error_tail),
    }


def append_result(csv_path: Path, result: Result) -> None:
    """CSV に 1 行追記し、即座に flush して閉じる。

    長時間実行の途中で中断されても、そこまでの結果が残るようにするため。
    """
    # BOM はファイル先頭にだけ必要。追記で BOM を重ねないよう utf-8 で開く
    with open(csv_path, "a", encoding="utf-8", newline="") as csv_file:
        csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS).writerow(_result_to_row(result))
        csv_file.flush()


def dump_raw_probe(layout: Layout, run_id: str, path: Path, payload: dict[str, object]) -> None:
    """--debug 指定時に ffprobe の生 JSON を保存する。失敗しても例外を送出しない。

    保存先は logs/<run_id>-probe/<素材のファイル名>.json。
    """
    try:
        directory = layout.logs_dir / f"{run_id}-probe"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{path.name}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding=META_ENCODING)
    except (OSError, TypeError, ValueError):
        pass
