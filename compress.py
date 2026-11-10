"""エントリポイント。CLI、逐次ループ、1 素材の処理手順、集計、終了コード。

判定ロジックそのものは持たない。素材の性質を見た分岐は policy.py に置く。
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import ffmpeg_io
import policy
import runlog
import workspace
from ffmpeg_io import FFmpegNotFoundError, ProbeError
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
    RunContext,
    SkipReason,
    VideoInfo,
)

EXIT_OK: int = 0
EXIT_MATERIAL_FAILED: int = 1
EXIT_STARTUP_ERROR: int = 2
EXIT_FATAL: int = 3


@dataclass(frozen=True)
class CliArgs:
    """解析済みのコマンドライン引数。判定とエンコードのパラメータは options にまとめる。"""

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _even_short_side(text: str) -> int:
    value = int(text)
    if value < 2 or value % 2 != 0:
        raise argparse.ArgumentTypeError(f"2 以上の偶数を指定する: {text}")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError(f"正の数を指定する: {text}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compress.py",
        description="<root>/in/ の動画を libx265 で一括圧縮し、<root>/out/ に書き出す。",
    )
    parser.add_argument("--root", type=Path, required=True, help="作業ディレクトリ（SMB 共有のルート）")
    parser.add_argument("--crf", type=int, default=policy.DEFAULT_CRF, help=f"x265 の CRF（既定 {policy.DEFAULT_CRF}）")
    parser.add_argument("--preset", default=policy.DEFAULT_PRESET, help=f"x265 の preset（既定 {policy.DEFAULT_PRESET}）")
    parser.add_argument(
        "--short-side", type=_even_short_side, default=policy.DEFAULT_SHORT_SIDE,
        help=f"ダウンスケール後の短辺の上限。偶数（既定 {policy.DEFAULT_SHORT_SIDE}）",
    )
    parser.add_argument("--no-downscale", action="store_true", help="ダウンスケールしない")
    parser.add_argument("--allow-hdr", action="store_true", help="HDR 素材もスキップせずにエンコードする")
    parser.add_argument(
        "--min-gain-ratio", type=_positive_float, default=policy.DEFAULT_MIN_GAIN_RATIO,
        help=f"出力 / 原本 のサイズ比がこれを超えたら出力を破棄する（既定 {policy.DEFAULT_MIN_GAIN_RATIO}）",
    )
    parser.add_argument(
        "--bpp-efficient", type=_positive_float, default=policy.BPP_THRESHOLD_EFFICIENT,
        help=f"HEVC / AV1 / VP9 の bpp がこれ未満ならスキップ（既定 {policy.BPP_THRESHOLD_EFFICIENT}）",
    )
    parser.add_argument(
        "--bpp-legacy", type=_positive_float, default=policy.BPP_THRESHOLD_LEGACY,
        help=f"それ以外のコーデックの bpp がこれ未満ならスキップ（既定 {policy.BPP_THRESHOLD_LEGACY}）",
    )
    parser.add_argument("--strip-location", action="store_true", help="出力から位置情報タグを消す")
    parser.add_argument("--no-verify-duration", action="store_true", help="出力の再生時間を検証しない")
    parser.add_argument("--dry-run", action="store_true", help="判定と ffmpeg コマンドを表示するだけで <root> を変更しない")
    parser.add_argument("--keep-in", action="store_true", help="原本を in/ から動かさない（閾値調整用）")
    parser.add_argument("--debug", action="store_true", help="ffprobe の生 JSON を logs/<run_id>-probe/ に保存する")
    return parser


def parse_args(argv: list[str] | None = None) -> CliArgs:
    """コマンドライン引数を解析する。

    --root --crf --preset --short-side --no-downscale --allow-hdr
    --min-gain-ratio --bpp-efficient --bpp-legacy   （閾値の CLI 昇格）
    --strip-location --no-verify-duration
    --dry-run --keep-in --debug

    Raises:
        SystemExit: 引数が不正なとき（argparse の既定動作）。
    """
    ns = _build_parser().parse_args(argv)
    defaults = policy.default_options()
    options = Options(
        video_codec=defaults.video_codec,
        crf=ns.crf,
        preset=ns.preset,
        short_side=ns.short_side,
        downscale=not ns.no_downscale,
        allow_hdr=ns.allow_hdr,
        min_gain_ratio=ns.min_gain_ratio,
        bpp_threshold_efficient=ns.bpp_efficient,
        bpp_threshold_legacy=ns.bpp_legacy,
        audio_copy_codecs=defaults.audio_copy_codecs,
        audio_fallback_bitrate=defaults.audio_fallback_bitrate,
        strip_location=ns.strip_location,
        verify_duration=not ns.no_verify_duration,
    )
    return CliArgs(root=ns.root, options=options, dry_run=ns.dry_run, keep_in=ns.keep_in, debug=ns.debug)


def _quote(arg: str) -> str:
    if arg and not any(c in arg for c in ' "\t&|<>^()'):
        return arg
    return '"' + arg.replace('"', '\\"') + '"'


def format_plan(plan: Plan, info: VideoInfo) -> str:
    """dry-run 表示用に Plan を 1 素材分の文字列へ整形する。

    ENCODE の場合、実行される ffmpeg コマンドをそのまま含める。
    ただし output_path は「本実行時に再計算される予約パス」であり、
    dry-run と本実行で一致するとは限らない（out/ の状態に依存するため）。
    その旨を出力に注記する。
    """
    shown_width, shown_height = policy.display_size(info)
    bit_rate = policy.effective_bit_rate(info)
    bpp = policy.bits_per_pixel(info)
    bit_rate_text = "不明" if bit_rate is None else f"{bit_rate[0] / 1_000_000:.2f} Mbps（{bit_rate[1]}）"
    bpp_text = "不明" if bpp is None else f"{bpp[0]:.4f}"
    lines = [
        plan.source.name,
        f"  解析: {info.video_codec} 格納 {info.width}x{info.height} / 表示 {shown_width}x{shown_height}"
        f"（rotation {info.rotation}） {info.fps:.3f} fps（{info.fps_source}）",
        f"        映像 {bit_rate_text} bpp {bpp_text} transfer {info.color_transfer or '-'}"
        f" 音声 {info.audio_codec or 'なし'}",
    ]
    if plan.action is Action.SKIP:
        reason = "-" if plan.reason is None else plan.reason.value
        lines.append(f"  判定: SKIP（{reason}）")
    else:
        lines.append("  判定: ENCODE")
        lines.append(
            f"  出力: {plan.output_path}"
            "（本実行時に再計算される予約パス。out/ の状態によっては dry-run と一致しない）"
        )
        lines.append("  コマンド: " + " ".join(_quote(a) for a in plan.ffmpeg_args))
    return "\n".join(lines)


def build_run_context(
    args: CliArgs,
    argv: Sequence[str],
    started_at: datetime,
    ffmpeg_version: str,
    ffprobe_version: str,
) -> RunContext:
    """1 回の実行の再現情報を組み立てる。run_id は開始時刻（秒精度）から作る。"""
    python_version, platform_name, tool_version = runlog.collect_environment()
    return RunContext(
        run_id=started_at.strftime("%Y%m%d-%H%M%S"),
        started_at=started_at,
        argv=tuple(argv),
        root=args.root,
        options=args.options,
        dry_run=args.dry_run,
        keep_in=args.keep_in,
        ffmpeg_version=ffmpeg_version,
        ffprobe_version=ffprobe_version,
        tool_version=tool_version,
        git_commit=runlog.collect_git_commit(),
        python_version=python_version,
        platform=platform_name,
    )


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------


def _error(message: str) -> None:
    print(f"エラー: {message}", file=sys.stderr)


def _total_size(sources: list[Path]) -> int:
    return sum(_size_or_zero(s) for s in sources)


def _format_result(result: Result) -> str:
    text = f"  -> {result.outcome.value}"
    if result.reason is not None:
        text += f"（{result.reason}）"
    if result.size_ratio is not None:
        text += f" サイズ比 {result.size_ratio:.2f}"
    if result.output_name is not None:
        text += f" 出力 {result.output_name}"
    if result.error_tail:
        text += f"\n     {result.error_tail.splitlines()[-1]}"
    return text


def _summary(results: list[Result]) -> str:
    counts = Counter(r.outcome for r in results)
    reasons = Counter(r.reason for r in results if r.reason is not None)
    text = " / ".join(f"{o.value} {counts.get(o, 0)}" for o in Outcome)
    if reasons:
        text += "（" + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())) + "）"
    return f"完了: {len(results)} 本: {text}"


def _debug_probe(layout: Layout, run_id: str) -> Callable[[Path], VideoInfo]:
    def probe(path: Path) -> VideoInfo:
        payload = ffmpeg_io.probe_json(path)
        # parse に失敗した素材の生 JSON も残すため、parse より先に保存する
        runlog.dump_raw_probe(layout, run_id, path, payload)
        try:
            size_bytes = path.stat().st_size
        except OSError as exc:
            raise ProbeError(path, f"ファイルサイズを取得できない: {exc}") from exc
        return ffmpeg_io.parse_probe_json(payload, path, size_bytes)

    return probe


def _run_dry(
    args: CliArgs,
    check_tools_fn: Callable[[], tuple[str, str]],
    probe_fn: Callable[[Path], VideoInfo],
) -> int:
    """dry-run。<root> を一切変更しない（ensure_layout もロックも .part 掃除もしない）。"""
    if not args.root.is_dir():
        _error(f"--root がディレクトリでない: {args.root}")
        return EXIT_STARTUP_ERROR
    layout = Layout(args.root)
    try:
        check_tools_fn()
    except FFmpegNotFoundError as exc:
        _error(str(exc))
        return EXIT_STARTUP_ERROR
    sources = workspace.scan_inbox(layout) if layout.in_dir.is_dir() else []
    if not workspace.check_free_space(layout, _total_size(sources)):
        _error("空き容量が素材の合計サイズに満たない")
        return EXIT_STARTUP_ERROR

    print(f"[dry-run] {layout.in_dir}: {len(sources)} 本（<root> は変更しない）")
    for source in sources:
        try:
            info = probe_fn(source)
            plan = make_plan(source, info, layout, args.options)
        except ProbeError as exc:
            print(f"{source.name}\n  判定: PROBE_FAILED（{exc.detail}）")
            continue
        except OSError as exc:
            print(f"{source.name}\n  判定: 解析できない（{exc}）")
            continue
        except FFmpegNotFoundError as exc:
            _error(str(exc))
            return EXIT_FATAL
        print(format_plan(plan, info))
    return EXIT_OK


def _run_batch(
    args: CliArgs,
    argv: Sequence[str],
    layout: Layout,
    check_tools_fn: Callable[[], tuple[str, str]],
    probe_fn: Callable[[Path], VideoInfo],
    encode_fn: Callable[[tuple[str, ...]], EncodeResult],
    duration_fn: Callable[[Path], float | None],
) -> int:
    try:
        ffmpeg_version, ffprobe_version = check_tools_fn()
    except FFmpegNotFoundError as exc:
        _error(str(exc))
        return EXIT_STARTUP_ERROR
    sources = workspace.scan_inbox(layout)
    if not workspace.check_free_space(layout, _total_size(sources)):
        _error("空き容量が素材の合計サイズに満たない（原本を残したまま出力を書くため）")
        return EXIT_STARTUP_ERROR

    workspace.cleanup_stale_parts(layout)
    context = build_run_context(args, argv, datetime.now().astimezone(), ffmpeg_version, ffprobe_version)
    csv_path, run_id = runlog.create_run_log(layout, context)
    probe = _debug_probe(layout, run_id) if args.debug else probe_fn

    print(f"run_id {run_id}: {len(sources)} 本を処理する（ログ {csv_path}）")
    results: list[Result] = []
    for index, source in enumerate(sources, start=1):
        print(f"[{index}/{len(sources)}] {source.name}", flush=True)
        try:
            result = process_one(
                source, layout, args, run_id,
                probe_fn=probe, encode_fn=encode_fn, duration_fn=duration_fn,
            )
        except FFmpegNotFoundError as exc:
            _error(f"ffmpeg / ffprobe を起動できなくなったため中止する: {exc}")
            return EXIT_FATAL
        runlog.append_result(csv_path, result)
        results.append(result)
        print(_format_result(result), flush=True)

    print(_summary(results))
    if any(r.outcome is Outcome.FAILED for r in results):
        return EXIT_MATERIAL_FAILED
    return EXIT_OK


def run(
    argv: list[str] | None = None,
    *,
    check_tools_fn: Callable[[], tuple[str, str]] = ffmpeg_io.check_tools,
    probe_fn: Callable[[Path], VideoInfo] = ffmpeg_io.probe,
    encode_fn: Callable[[tuple[str, ...]], EncodeResult] = ffmpeg_io.run_ffmpeg,
    duration_fn: Callable[[Path], float | None] = ffmpeg_io.probe_duration,
) -> int:
    """バッチ全体を実行する。

    注入した関数は process_one() と dry-run の probe にそのまま渡す。
    --debug（本実行のみ）のときは probe_fn の代わりに、生 JSON を保存してから
    parse_probe_json() する関数を組み立てて使う。

    Returns:
        0: 失敗なし
        1: 素材レベルの失敗が 1 件以上
        2: 起動時のエラー（ffmpeg 不在、ディレクトリ不正、ロック取得失敗、容量不足）
        3: 実行中に致命的エラー（ffmpeg が途中で消えた等）
    """
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(raw_argv)
    if args.dry_run:
        return _run_dry(args, check_tools_fn, probe_fn)

    try:
        layout = workspace.ensure_layout(args.root)
    except (OSError, RuntimeError) as exc:
        _error(f"作業ディレクトリを準備できない: {exc}")
        return EXIT_STARTUP_ERROR
    try:
        with workspace.acquire_lock(layout):
            return _run_batch(args, raw_argv, layout, check_tools_fn, probe_fn, encode_fn, duration_fn)
    except workspace.LockHeldError as exc:
        _error(str(exc))
        return EXIT_STARTUP_ERROR


def main() -> None:
    """エントリポイント。run() の返り値で sys.exit する。"""
    sys.exit(run())


if __name__ == "__main__":
    main()
