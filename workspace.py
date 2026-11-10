"""作業ディレクトリの準備、ロック、スキャン、安定検知、パス予約、ファイル移動。

判定は持たない。プロセスも起動しない。CSV / JSON の書式も持たない。
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from policy import (
    OUTPUT_EXTENSION,
    OUTPUT_SUFFIX,
    PART_EXTENSION,
    STABILITY_INTERVAL_SEC,
    STABILITY_MTIME_QUIET_SEC,
    STABILITY_REQUIRED_MATCHES,
    STABILITY_TIMEOUT_SEC,
    STALE_PART_AGE_SEC,
    TARGET_EXTENSIONS,
    Layout,
    SkipReason,
)

# Windows の ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION。他プロセスが書き込み中であることを示す
_WINERROR_IN_USE: frozenset[int] = frozenset({32, 33})


class LockHeldError(RuntimeError):
    """別の実行が同じ <root> を使用中。"""


class SameVolumeError(RuntimeError):
    """作業ディレクトリ間の rename ができない（同一ボリュームでない等。R-8）。"""


@contextmanager
def acquire_lock(layout: Layout) -> Iterator[None]:
    """<root>/.lock を排他作成する。

    os.open(path, O_CREAT | O_EXCL) で作成し、PID と開始時刻を書き込む。
    終了時に必ず削除する。

    Raises:
        LockHeldError: 既に .lock が存在するとき。
    """
    try:
        fd = os.open(layout.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise LockHeldError(
            f"{layout.lock_path} が存在する。別の実行が使用中か、前回が異常終了した"
            "（実行中でないことを確認してから削除する）"
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            lock_file.write(f"pid={os.getpid()}\nstarted_at={started_at}\n")
        yield
    finally:
        layout.lock_path.unlink(missing_ok=True)


def ensure_layout(root: Path) -> Layout:
    """作業ディレクトリ一式を作成し、Layout を返す。

    skipped/ 配下は `for r in SkipReason` で回して作る（NOT_READY を除く）。

    同一ボリューム検査は rename のスモークテストで行う（R-8）。
    in/ に 0 バイトの一時ファイルを作り、done/ へ os.rename() を試し、削除する。
    st_dev の比較は行わない。

    Raises:
        NotADirectoryError: root が存在しない、またはディレクトリでないとき。
        SameVolumeError: rename のスモークテストに失敗したとき（RuntimeError の派生）。
        PermissionError: 作成権限がないとき。
    """
    if not root.is_dir():
        raise NotADirectoryError(f"--root がディレクトリでない: {root}")
    layout = Layout(root)
    directories = [
        layout.in_dir,
        layout.out_dir,
        layout.done_dir,
        layout.failed_dir,
        layout.logs_dir,
        *(layout.skipped_dir(r) for r in SkipReason if r is not SkipReason.NOT_READY),
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    _rename_smoke_test(layout)
    return layout


def _rename_smoke_test(layout: Layout) -> None:
    # 拡張子を TARGET_EXTENSIONS に入れないので、並行する dry-run の scan_inbox には現れない
    name = f".rename-test-{os.getpid()}.tmp"
    src = layout.in_dir / name
    dst = layout.done_dir / name
    try:
        src.touch()
        dst.unlink(missing_ok=True)
        os.rename(src, dst)
    except OSError as exc:
        raise SameVolumeError(
            f"{layout.in_dir} から {layout.done_dir} への rename に失敗した。"
            f"in / done は同一ボリュームに置く必要がある（R-8）: {exc}"
        ) from exc
    finally:
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


def check_free_space(layout: Layout, required_bytes: int) -> bool:
    """root のあるボリュームに required_bytes 以上の空きがあるか確認する。"""
    return shutil.disk_usage(layout.root).free >= required_bytes


def cleanup_stale_parts(
    layout: Layout, max_age_sec: float = STALE_PART_AGE_SEC
) -> list[Path]:
    """out/ に残った古い *.part を削除する（R-6）。

    mtime が max_age_sec より古いものだけを削除する。
    dry-run では呼ばない。削除したファイルは標準出力に警告として出す。
    """
    if not layout.out_dir.is_dir():
        return []
    now = time.time()
    removed: list[Path] = []
    for path in sorted(layout.out_dir.iterdir()):
        if not path.name.endswith(PART_EXTENSION) or not path.is_file():
            continue
        try:
            age = now - path.stat().st_mtime
        except FileNotFoundError:
            continue
        if age > max_age_sec:
            path.unlink(missing_ok=True)
            removed.append(path)
            print(f"警告: 前回の中断で残った一時ファイルを削除した: {path}")
    return removed


def scan_inbox(layout: Layout) -> list[Path]:
    """in/ 直下の処理対象ファイルを列挙する。

    TARGET_EXTENSIONS に一致する拡張子のみ（大文字小文字を区別しない）。
    サブディレクトリは辿らない。名前順にソートして返す。
    """
    found = [
        path
        for path in layout.in_dir.iterdir()
        if path.suffix.lower() in TARGET_EXTENSIONS and path.is_file()
    ]
    return sorted(found, key=lambda p: p.name)


def _is_being_written(path: Path) -> bool:
    try:
        with open(path, "r+b"):
            return False
    except PermissionError as exc:
        # 読み取り専用属性などの権限不足は書き込み中の証拠ではないので、共有違反だけを見る
        return getattr(exc, "winerror", None) in _WINERROR_IN_USE


def wait_until_stable(
    path: Path,
    interval_sec: float = STABILITY_INTERVAL_SEC,
    required_matches: int = STABILITY_REQUIRED_MATCHES,
    timeout_sec: float = STABILITY_TIMEOUT_SEC,
    quiet_sec: float = STABILITY_MTIME_QUIET_SEC,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.monotonic,
) -> bool:
    """ファイルの転送が完了したと見なせるまで待つ（F-2）。

    最初に mtime を確認し、現在時刻から quiet_sec 以上前であれば
    ポーリングせずに True を返す（転送中のファイルは mtime が更新され続けるため）。
    mtime との比較は壁時計（time.time）で行う。now_fn はタイムアウトの計測にのみ使う。
    そうでなければ、サイズが required_matches 回連続で一致し、
    かつ排他オープンできることを確認する。

    これは書き込み完了の証明ではなくヒューリスティックである（R-1）。
    呼び出し側は、True を返した後の ffprobe 成功を追加の根拠とする。

    Returns:
        安定したと判断したとき True。タイムアウトしたとき False。

    Raises:
        FileNotFoundError: 待機中にファイルが消えたとき。
    """
    stat = path.stat()
    if time.time() - stat.st_mtime >= quiet_sec:
        return True

    deadline = now_fn() + timeout_sec
    last_size = stat.st_size
    matches = 1
    while True:
        if matches >= required_matches and not _is_being_written(path):
            return True
        if now_fn() >= deadline:
            return False
        sleep_fn(interval_sec)
        size = path.stat().st_size
        if size == last_size:
            matches += 1
        else:
            last_size = size
            matches = 1


def reserve_output_path(layout: Layout, source: Path) -> Path:
    """出力先パスを決定する。ファイルは作成しない。

    <元の名前>_c.mp4 を基本とし、同名が存在する場合は _c_2.mp4 のように連番を付ける。
    同一実行内での二重予約は起きない（1 素材ずつ逐次処理するため）。
    実行をまたぐ衝突は acquire_lock() が防ぐ。
    """
    base = f"{source.stem}{OUTPUT_SUFFIX}"
    candidate = layout.out_dir / f"{base}{OUTPUT_EXTENSION}"
    number = 2
    while candidate.exists():
        candidate = layout.out_dir / f"{base}_{number}{OUTPUT_EXTENSION}"
        number += 1
    return candidate


def part_path(final: Path) -> Path:
    """最終出力パスに対応する一時ファイルのパス（<final>.part）を返す。"""
    return final.with_name(final.name + PART_EXTENSION)


def file_size(path: Path) -> int:
    """ファイルのバイト数を返す。finalize とは独立に呼べる。"""
    return path.stat().st_size


def finalize_output(part: Path, final: Path) -> None:
    """一時ファイルを最終名にリネームする。

    採否の判定が済んだ後にのみ呼ぶこと。

    Raises:
        FileNotFoundError: part が存在しないとき。
        FileExistsError: final が既に存在するとき。
    """
    if not part.exists():
        raise FileNotFoundError(f"一時ファイルが存在しない: {part}")
    # POSIX の rename は既存ファイルを黙って上書きするので、事前に確認する
    if final.exists():
        raise FileExistsError(f"出力先が既に存在する: {final}")
    os.rename(part, final)


def discard_output(part: Path) -> None:
    """一時ファイルを削除する。存在しなくてもエラーにしない。"""
    part.unlink(missing_ok=True)


def move_source(source: Path, dest_dir: Path) -> Path:
    """原本を dest_dir へ移動する。

    同一ボリューム内の rename として実行する（R-8）。
    移動先に同名があれば連番を付ける。原本の上書きは行わない（F-8）。

    Returns:
        移動後のパス。

    Raises:
        OSError: 移動に失敗したとき。呼び出し側は原本が in/ に残っている前提で継続する。
    """
    candidate = dest_dir / source.name
    number = 2
    while candidate.exists():
        candidate = dest_dir / f"{source.stem}_{number}{source.suffix}"
        number += 1
    os.rename(source, candidate)
    return candidate


def copy_mtime(source: Path, dest: Path) -> None:
    """原本の mtime / atime を出力にコピーする。

    iPhone へ戻したときの並び順を保つため。失敗しても例外を送出しない。
    """
    try:
        stat = source.stat()
        os.utime(dest, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    except OSError:
        pass
