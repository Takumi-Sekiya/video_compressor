"""エントリポイント。CLI、逐次ループ、1 素材の処理手順、集計、終了コード。

判定ロジックそのものは持たない。素材の性質を見た分岐は policy.py に置く。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import ffmpeg_io
import policy
import workspace
from ffmpeg_io import ProbeError
from policy import (
    Action,
    EncodeResult,
    FailureReason,
    Layout,
    Options,
    Outcome,
    Plan,
    Reason,
    Result,
    SkipReason,
    VideoInfo,
)


@dataclass(frozen=True)
class CliArgs:
    root: Path
    options: Options
    dry_run: bool
    keep_in: bool
    debug: bool


def make_plan(source: Path, info: VideoInfo, layout: Layout, options: Options) -> Plan:
    """classify() と build_ffmpeg_args() から Plan を組み立てる。dry-run と本実行で共用する。

    ENCODE の場合は出力パスを予約し、ffmpeg の書き込み先をその .part にする。
    予約はファイルを作らないので、dry-run から呼んでも <root> を変更しない。
    """
    reason = policy.classify(info, options)
    if reason is not None:
        return Plan(source, Action.SKIP, reason, None, ())
    final = workspace.reserve_output_path(layout, source)
    args = policy.build_ffmpeg_args(info, workspace.part_path(final), options)
    return Plan(source, Action.ENCODE, None, final, args)


@dataclass(frozen=True)
class _EncodeOutcome:
    outcome: Outcome
    reason: Reason | None
    output_size: int | None
    encode: EncodeResult | None
    detail: str | None


def _encode(
    plan: Plan,
    final: Path,
    info: VideoInfo,
    layout: Layout,
    options: Options,
    encode_fn: Callable[[tuple[str, ...]], EncodeResult],
    duration_fn: Callable[[Path], float | None],
) -> _EncodeOutcome:
    """エンコードから採否判定、リネームまで。原本には触れない。

    .part を最終名にするのは、採用が確定した後だけ（H-2）。
    """
    part = workspace.part_path(final)
    encode: EncodeResult | None = None
    output_size: int | None = None
    try:
        if not workspace.check_free_space(layout, info.size_bytes):
            return _EncodeOutcome(
                Outcome.FAILED, FailureReason.ENCODE_FAILED, None, None,
                f"空き容量が不足している（原本サイズ {info.size_bytes} バイト分が必要）",
            )
        encode = encode_fn(plan.ffmpeg_args)
        if encode.returncode != 0:
            workspace.discard_output(part)
            return _EncodeOutcome(Outcome.FAILED, FailureReason.ENCODE_FAILED, None, encode, None)

        output_size = workspace.file_size(part)
        if not policy.is_worth_keeping(info.size_bytes, output_size, options.min_gain_ratio):
            workspace.discard_output(part)
            return _EncodeOutcome(Outcome.SKIPPED, SkipReason.NO_GAIN, output_size, encode, None)

        if options.verify_duration:
            output_duration = duration_fn(part)
            if not policy.is_duration_consistent(info, output_duration):
                workspace.discard_output(part)
                return _EncodeOutcome(
                    Outcome.FAILED, FailureReason.OUTPUT_INVALID, output_size, encode,
                    f"再生時間が一致しない: 原本 {info.duration_sec} 秒 / 出力 {output_duration} 秒",
                )

        workspace.finalize_output(part, final)
    except OSError as exc:
        with suppress(OSError):
            workspace.discard_output(part)
        return _EncodeOutcome(
            Outcome.FAILED, FailureReason.ENCODE_FAILED, output_size, encode,
            f"エンコード中のファイル操作に失敗した: {exc}",
        )
    workspace.copy_mtime(plan.source, final)
    return _EncodeOutcome(Outcome.ENCODED, None, output_size, encode, None)


def _move_source(
    source: Path, layout: Layout, outcome: Outcome, reason: Reason | None, keep_in: bool
) -> tuple[str | None, str | None]:
    """原本を resolve_dest_dir() の決めた先へ移す。(moved_to, エラー内容) を返す。

    すべての経路の合流点。原本の移動は、その素材の処理がすべて終わった後の最後の操作にする。
    """
    if keep_in:
        return None, None
    dest_dir = policy.resolve_dest_dir(layout, outcome, reason)
    if dest_dir is None:
        return None, None
    try:
        moved = workspace.move_source(source, dest_dir)
    except OSError as exc:
        return None, f"原本を {dest_dir} へ移動できなかった（in/ に残っている）: {exc}"
    return str(moved.relative_to(layout.root)), None


def _join(*parts: str | None) -> str | None:
    text = "\n".join(p for p in parts if p)
    return text or None


def _size_or_zero(path: Path) -> int:
    try:
        return workspace.file_size(path)
    except OSError:
        return 0


def process_one(
    source: Path,
    layout: Layout,
    args: CliArgs,
    run_id: str,
    *,
    stable_fn: Callable[[Path], bool] = workspace.wait_until_stable,
    probe_fn: Callable[[Path], VideoInfo] = ffmpeg_io.probe,
    encode_fn: Callable[[tuple[str, ...]], EncodeResult] = ffmpeg_io.run_ffmpeg,
    duration_fn: Callable[[Path], float | None] = ffmpeg_io.probe_duration,
) -> Result:
    """1 素材を処理し、Result を返す。CSV への書き込みは行わない。

    手順は design.md「データフロー」節を参照。

    素材ローカルな失敗（ProbeError, OSError, ffmpeg の非ゼロ終了）は
    Result に畳み込んで返す（F-8）。
    実行全体を止めるべき例外（FFmpegNotFoundError）は再送出する。

    OSError の畳み込み先:
      - 安定待ち中（原本が消えた等）       -> SKIPPED / NOT_READY（原本は動かさない）
      - probe 中                           -> FAILED / PROBE_FAILED
      - エンコード〜リネーム中             -> FAILED / ENCODE_FAILED（.part を消し、原本は in/ に残す）
      - 原本の移動                         -> outcome はそのまま。moved_to を None にし error_tail に記録
    """
    started = time.monotonic()

    def without_plan(reason: Reason, detail: str | None) -> Result:
        source_size = _size_or_zero(source)
        outcome = Outcome.SKIPPED if isinstance(reason, SkipReason) else Outcome.FAILED
        moved_to, move_error = _move_source(source, layout, outcome, reason, args.keep_in)
        return policy.build_result_without_plan(
            run_id, source, source_size, reason, time.monotonic() - started,
            _join(detail, move_error), moved_to,
        )

    try:
        stable = stable_fn(source)
        stable_detail = None if stable else "転送完了を確認できなかった（安定待ちタイムアウト）"
    except OSError as exc:
        stable = False
        stable_detail = f"安定待ち中にファイルを確認できなくなった: {exc}"
    if not stable:
        return without_plan(SkipReason.NOT_READY, stable_detail)

    try:
        info = probe_fn(source)
    except ProbeError as exc:
        return without_plan(FailureReason.PROBE_FAILED, exc.detail)
    except OSError as exc:
        return without_plan(FailureReason.PROBE_FAILED, str(exc))

    try:
        plan = make_plan(source, info, layout, args.options)
    except OSError as exc:
        return without_plan(FailureReason.ENCODE_FAILED, f"出力先を予約できなかった: {exc}")

    final = plan.output_path
    if plan.action is Action.SKIP or final is None:
        result = _EncodeOutcome(Outcome.SKIPPED, plan.reason, None, None, None)
    else:
        result = _encode(plan, final, info, layout, args.options, encode_fn, duration_fn)

    moved_to, move_error = _move_source(source, layout, result.outcome, result.reason, args.keep_in)
    return policy.build_result(
        run_id, plan, info, result.outcome, result.reason, time.monotonic() - started,
        output_size=result.output_size,
        encode=result.encode,
        moved_to=moved_to,
        detail=_join(result.detail, move_error),
    )
