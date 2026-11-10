"""判定・引数生成・移動先の決定を担う純粋関数と、定数・データ構造。

このモジュールは外部 I/O を持たない。プロジェクト内の他モジュールを import せず、
subprocess / shutil / os も import しない。Path は値として持つだけで、
exists() / stat() などの問い合わせは呼ばない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

TOOL_VERSION: str = "0.2.0"

DEFAULT_VIDEO_CODEC: str = "libx265"
DEFAULT_CRF: int = 26
DEFAULT_PRESET: str = "slow"
DEFAULT_SHORT_SIDE: int = 1080
DEFAULT_MIN_GAIN_RATIO: float = 0.90

BPP_THRESHOLD_EFFICIENT: float = 0.045
BPP_THRESHOLD_LEGACY: float = 0.075
EFFICIENT_CODECS: frozenset[str] = frozenset({"hevc", "av1", "vp9"})

HDR_TRANSFERS: frozenset[str] = frozenset({"smpte2084", "arib-std-b67"})
UNSUPPORTED_CODECS: frozenset[str] = frozenset({"prores", "dnxhd"})

TARGET_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".mov", ".m4v"})
OUTPUT_EXTENSION: str = ".mp4"
OUTPUT_SUFFIX: str = "_c"
PART_EXTENSION: str = ".part"

DEFAULT_AUDIO_COPY_CODECS: frozenset[str] = frozenset({"aac"})
DEFAULT_AUDIO_FALLBACK_BITRATE: str = "128k"

DURATION_TOLERANCE_SEC: float = 1.0

STABILITY_INTERVAL_SEC: float = 5.0
STABILITY_REQUIRED_MATCHES: int = 3
STABILITY_TIMEOUT_SEC: float = 900.0
STABILITY_MTIME_QUIET_SEC: float = 30.0
FFPROBE_TIMEOUT_SEC: float = 60.0
STDERR_TAIL_LINES: int = 20
STALE_PART_AGE_SEC: float = 3600.0


def export_constants() -> dict[str, object]:
    """meta.json に記録する定数のスナップショットを返す。

    Options に入らない定数のうち、判定結果に影響するものをすべて含める。
    frozenset はソート済み list に変換する（JSON 化のため）。
    """
    return {
        "EFFICIENT_CODECS": sorted(EFFICIENT_CODECS),
        "HDR_TRANSFERS": sorted(HDR_TRANSFERS),
        "UNSUPPORTED_CODECS": sorted(UNSUPPORTED_CODECS),
        "TARGET_EXTENSIONS": sorted(TARGET_EXTENSIONS),
        "STABILITY_INTERVAL_SEC": STABILITY_INTERVAL_SEC,
        "STABILITY_REQUIRED_MATCHES": STABILITY_REQUIRED_MATCHES,
        "STABILITY_TIMEOUT_SEC": STABILITY_TIMEOUT_SEC,
        "STABILITY_MTIME_QUIET_SEC": STABILITY_MTIME_QUIET_SEC,
        "DURATION_TOLERANCE_SEC": DURATION_TOLERANCE_SEC,
    }


# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------


class PolicyError(ValueError):
    """policy の関数・データ構造に、矛盾した値または範囲外の値が渡された。"""


# ---------------------------------------------------------------------------
# 列挙型
# ---------------------------------------------------------------------------


class Action(Enum):
    """Plan で決めた素材の扱い。"""

    ENCODE = "encode"
    SKIP = "skip"


class SkipReason(Enum):
    """処理対象外と判定した理由。原本は skipped/<value>/ へ移す。

    NOT_READY のみ例外で、原本は in/ に残す（次回実行で再試行するため）。
    """

    HDR = "hdr"
    LOW_BPP = "low_bpp"
    UNSUPPORTED = "unsupported"
    NO_GAIN = "no_gain"
    NOT_READY = "not_ready"


class FailureReason(Enum):
    """処理に失敗した理由。compress.py が設定する。"""

    PROBE_FAILED = "probe_failed"
    ENCODE_FAILED = "encode_failed"
    OUTPUT_INVALID = "output_invalid"


class Outcome(Enum):
    """1 素材の最終結果。CSV の分類軸となる列。"""

    ENCODED = "encoded"
    SKIPPED = "skipped"
    FAILED = "failed"


Reason = SkipReason | FailureReason


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """作業ディレクトリのパス集合。実在は保証しない（それは workspace の責務）。"""

    root: Path

    @property
    def in_dir(self) -> Path:
        return self.root / "in"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    @property
    def done_dir(self) -> Path:
        return self.root / "done"

    @property
    def failed_dir(self) -> Path:
        return self.root / "failed"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def lock_path(self) -> Path:
        return self.root / ".lock"

    def skipped_dir(self, reason: SkipReason) -> Path:
        """`<root>/skipped/<reason.value>/` を返す。全入力に対して値を返す。"""
        return self.root / "skipped" / reason.value


@dataclass(frozen=True)
class VideoInfo:
    """ffprobe から得た素材の仕様。ffmpeg_io.parse_probe_json() が生成する。

    ffprobe の出力は素材によってフィールドが欠落するため、
    欠落しうる項目は None を許容する（R-2）。

    Attributes:
        width: 格納寸法（回転前）の幅。
        height: 格納寸法（回転前）の高さ。
        rotation: 時計回りの回転角。0 / 90 / 180 / 270 に正規化済み。
        fps_source: fps の採用元。"avg" または "r"。
        video_bit_rate_bps: 映像ストリームの bit_rate。
    """

    path: Path
    size_bytes: int
    duration_sec: float | None
    width: int
    height: int
    rotation: int
    fps: float
    fps_source: str
    video_codec: str
    pix_fmt: str
    video_bit_rate_bps: int | None
    container_bit_rate_bps: int | None
    audio_codec: str | None
    audio_bit_rate_bps: int | None
    color_transfer: str | None
    color_primaries: str | None


@dataclass(frozen=True)
class Options:
    """判定とエンコードのパラメータ。CLI 引数から compress.py が組み立てる。

    出力または判定に影響するパラメータのうち、素材ごとに変えうるものをすべて持つ。
    素材によらず固定の集合定数は policy.export_constants() が meta.json に出す。
    """

    video_codec: str
    crf: int
    preset: str
    short_side: int
    downscale: bool
    allow_hdr: bool
    min_gain_ratio: float
    bpp_threshold_efficient: float
    bpp_threshold_legacy: float
    audio_copy_codecs: frozenset[str]
    audio_fallback_bitrate: str
    strip_location: bool
    verify_duration: bool


@dataclass(frozen=True)
class Plan:
    """エンコード前に確定できることだけを持つ。

    --dry-run は「Plan を作るところで止める」ことで実現する。

    Attributes:
        output_path: 本実行時に予約される最終出力パス（.part ではない）。
        ffmpeg_args: subprocess にそのまま渡せる完全な引数列（先頭の "ffmpeg" を含む）。
    """

    source: Path
    action: Action
    reason: SkipReason | None
    output_path: Path | None
    ffmpeg_args: tuple[str, ...]

    def __post_init__(self) -> None:
        """action と他フィールドの組み合わせの不変条件を実行時に検査する。

        Raises:
            ValueError: action と他フィールドの組み合わせが矛盾するとき。
        """
        if self.action is Action.SKIP:
            if self.reason is None:
                raise PolicyError("SKIP の Plan には reason が必要")
            if self.output_path is not None:
                raise PolicyError("SKIP の Plan に output_path は持たせない")
            if self.ffmpeg_args != ():
                raise PolicyError("SKIP の Plan に ffmpeg_args は持たせない")
        elif self.action is Action.ENCODE:
            if self.reason is not None:
                raise PolicyError("ENCODE の Plan に reason は持たせない")
            if self.output_path is None:
                raise PolicyError("ENCODE の Plan には output_path が必要")
            if not self.ffmpeg_args or self.ffmpeg_args[0] != "ffmpeg":
                raise PolicyError('ENCODE の Plan の ffmpeg_args は "ffmpeg" で始まる完全な引数列')
        else:
            raise PolicyError(f"未知の action: {self.action!r}")


@dataclass(frozen=True)
class EncodeResult:
    """ffmpeg プロセスの実行結果。ffmpeg_io.run_ffmpeg() が返す。"""

    returncode: int
    stderr_tail: str
    elapsed_sec: float


@dataclass(frozen=True)
class Result:
    """1 素材の最終結果。CSV の 1 行に対応する。

    Attributes:
        reason: SkipReason / FailureReason の value。
        moved_to: 移動後のパス（<root> からの相対）。移動しなければ None。
        encode_sec: ffmpeg プロセスの実行時間のみ。
        total_sec: 安定待ちと probe を含む素材全体の所要時間。
        resolution: 表示寸法（回転適用後）。
        stored_resolution: 格納寸法（回転前）。
        bit_rate_source: "video_stream" | "container_minus_audio" | "filesize"。
    """

    run_id: str
    source_name: str
    outcome: Outcome
    reason: str | None
    source_size: int
    output_name: str | None
    output_size: int | None
    size_ratio: float | None
    moved_to: str | None
    encode_sec: float | None
    total_sec: float
    video_codec: str | None
    resolution: str | None
    stored_resolution: str | None
    rotation: int | None
    fps: float | None
    fps_source: str | None
    bit_rate_bps: int | None
    bit_rate_source: str | None
    bpp: float | None
    color_transfer: str | None
    audio_codec: str | None
    ffmpeg_returncode: int | None
    error_tail: str | None


@dataclass(frozen=True)
class RunContext:
    """1 回の実行を再現するために必要な情報のすべて。

    判定に影響する定数は実行中に変わらないため持たない。
    runlog.create_run_log() が policy.export_constants() から meta.json に書く。
    """

    run_id: str
    started_at: datetime
    argv: tuple[str, ...]
    root: Path
    options: Options
    dry_run: bool
    keep_in: bool
    ffmpeg_version: str
    ffprobe_version: str
    tool_version: str
    git_commit: str | None
    python_version: str
    platform: str


# ---------------------------------------------------------------------------
# 関数
# ---------------------------------------------------------------------------

# -metadata key= は ffmpeg ではタグの削除になる。iPhone の素材が位置情報を持つキー。
_LOCATION_TAGS: tuple[str, ...] = (
    "location",
    "location-eng",
    "com.apple.quicktime.location.ISO6709",
)


def default_options() -> Options:
    """CLI 引数を何も指定しなかったときの Options を返す。"""
    return Options(
        video_codec=DEFAULT_VIDEO_CODEC,
        crf=DEFAULT_CRF,
        preset=DEFAULT_PRESET,
        short_side=DEFAULT_SHORT_SIDE,
        downscale=True,
        allow_hdr=False,
        min_gain_ratio=DEFAULT_MIN_GAIN_RATIO,
        bpp_threshold_efficient=BPP_THRESHOLD_EFFICIENT,
        bpp_threshold_legacy=BPP_THRESHOLD_LEGACY,
        audio_copy_codecs=DEFAULT_AUDIO_COPY_CODECS,
        audio_fallback_bitrate=DEFAULT_AUDIO_FALLBACK_BITRATE,
        strip_location=False,
        verify_duration=True,
    )


def display_size(info: VideoInfo) -> tuple[int, int]:
    """回転を適用した表示上の (幅, 高さ) を返す。

    rotation が 90 / 270 のとき width と height を入れ替える。
    """
    if info.rotation in (90, 270):
        return info.height, info.width
    return info.width, info.height


def effective_bit_rate(info: VideoInfo) -> tuple[int, str] | None:
    """判定に用いる映像ビットレートと、その取得経路を返す。

    優先順位:
      1. video_bit_rate_bps（映像ストリームの bit_rate）        -> "video_stream"
      2. container_bit_rate_bps - audio_bit_rate_bps            -> "container_minus_audio"
      3. size_bytes * 8 / duration_sec（音声込みの近似）        -> "filesize"

    2 はコンテナと音声の両方の bit_rate が取れたときだけ使う。

    Returns:
        (bps, 経路名)。duration_sec が None または 0 で 3 も使えない場合は None。
    """
    if info.video_bit_rate_bps is not None and info.video_bit_rate_bps > 0:
        return info.video_bit_rate_bps, "video_stream"
    if info.container_bit_rate_bps is not None and info.audio_bit_rate_bps is not None:
        video_bps = info.container_bit_rate_bps - info.audio_bit_rate_bps
        if video_bps > 0:
            return video_bps, "container_minus_audio"
    if info.duration_sec is not None and info.duration_sec > 0 and info.size_bytes > 0:
        return int(info.size_bytes * 8 / info.duration_sec), "filesize"
    return None


def bits_per_pixel(info: VideoInfo) -> tuple[float, str] | None:
    """bpp = 映像ビットレート / (格納幅 x 格納高さ x fps) を求める。

    格納寸法を使う（表示寸法ではない）。実際に符号化されている画素数のため。

    Returns:
        (bpp, ビットレートの取得経路)。ビットレートか画素レートが求まらない場合は None。
    """
    bit_rate = effective_bit_rate(info)
    if bit_rate is None:
        return None
    pixels_per_sec = info.width * info.height * info.fps
    if pixels_per_sec <= 0:
        return None
    bps, source = bit_rate
    return bps / pixels_per_sec, source


def is_hdr(info: VideoInfo) -> bool:
    """color_transfer が PQ または HLG のとき True。

    color_transfer が None（ffprobe が返さなかった）場合は False を返す。
    この挙動は A-2 の確認結果によって再検討する（未解決の設計判断 Q-1）。
    """
    return info.color_transfer in HDR_TRANSFERS


def scale_filter(info: VideoInfo, short_side: int) -> str | None:
    """短辺を short_side に制限する scale フィルタ式を生成する。

    短辺の判定には display_size()（回転適用後）を用いる。ffmpeg は既定で回転を
    フレームに適用してからフィルタに渡すため、式も表示寸法に対して組み立てる。
    元の短辺が short_side 以下の場合は拡大しないため None を返す。

    Raises:
        ValueError: short_side が 2 未満、または奇数のとき。
    """
    if short_side < 2 or short_side % 2 != 0:
        raise PolicyError(f"short_side は 2 以上の偶数: {short_side}")
    width, height = display_size(info)
    if min(width, height) <= short_side:
        return None
    if width <= height:
        return f"scale={short_side}:-2"
    return f"scale=-2:{short_side}"


def classify(info: VideoInfo, options: Options) -> SkipReason | None:
    """スキップすべきなら理由を、エンコードすべきなら None を返す。

    評価順:
      1. video_codec が UNSUPPORTED_CODECS に含まれる -> UNSUPPORTED
      2. is_hdr() かつ allow_hdr が False             -> HDR
      3. bpp が閾値未満                                -> LOW_BPP
         閾値は video_codec が EFFICIENT_CODECS に含まれるなら
         bpp_threshold_efficient、そうでなければ bpp_threshold_legacy
      4. 上記のいずれにも該当しない                     -> None

    bpp が求まらない（None）場合は LOW_BPP と判定しない。判定不能を
    スキップ理由にすると、解析しにくい素材が無条件に対象外になるため。
    """
    if info.video_codec in UNSUPPORTED_CODECS:
        return SkipReason.UNSUPPORTED
    if is_hdr(info) and not options.allow_hdr:
        return SkipReason.HDR
    bpp = bits_per_pixel(info)
    if bpp is not None:
        if info.video_codec in EFFICIENT_CODECS:
            threshold = options.bpp_threshold_efficient
        else:
            threshold = options.bpp_threshold_legacy
        if bpp[0] < threshold:
            return SkipReason.LOW_BPP
    return None


def build_ffmpeg_args(
    info: VideoInfo, output_path: Path, options: Options
) -> tuple[str, ...]:
    """ffmpeg に渡す完全な引数列を組み立てる。

    output_path は ffmpeg が書き込む先（呼び出し側は .part のパスを渡す）。

    - -c:v は options.video_codec、-crf / -preset は options の値
    - -tag:v hvc1 を必ず含める（C-2）
    - -map 0:V:0（カバー画像を除く最初の映像）と、音声があれば -map 0:a:0 を明示する。
      parse_probe_json() が VideoInfo に採ったストリームと一致させるため
    - 音声は 3 分岐:
        audio_codec is None                        -> -an
        audio_codec in options.audio_copy_codecs   -> -c:a copy
        それ以外                                    -> -c:a aac -b:a <fallback>
    - -map_metadata 0 と -movflags +faststart を常に付与する
    - -f mp4 を付与する（書き込み先が .part で、拡張子から形式を推定できないため）
    - -y を付与する（書き込み先は常にロック下の .part。前回の異常終了で残った .part を
      上書きできないと、確認待ちのまま失敗するため）
    - options.strip_location が True のとき、位置情報タグを空文字で上書きする

    Raises:
        ValueError: options.downscale が True で short_side が不正なとき。
    """
    args: list[str] = ["ffmpeg", "-hide_banner", "-y", "-i", str(info.path), "-map", "0:V:0"]
    if info.audio_codec is not None:
        args += ["-map", "0:a:0"]

    args += [
        "-c:v", options.video_codec,
        "-crf", str(options.crf),
        "-preset", options.preset,
        "-tag:v", "hvc1",
    ]
    if options.downscale:
        vf = scale_filter(info, options.short_side)
        if vf is not None:
            args += ["-vf", vf]

    if info.audio_codec is None:
        args += ["-an"]
    elif info.audio_codec in options.audio_copy_codecs:
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", "aac", "-b:a", options.audio_fallback_bitrate]

    args += ["-map_metadata", "0"]
    if options.strip_location:
        for key in _LOCATION_TAGS:
            args += ["-metadata", f"{key}="]

    args += ["-movflags", "+faststart", "-f", OUTPUT_EXTENSION.removeprefix("."), str(output_path)]
    return tuple(args)


def is_worth_keeping(
    source_size: int, output_size: int, min_gain_ratio: float
) -> bool:
    """出力を採用するかどうかを判定する（F-6）。

    出力サイズが元の min_gain_ratio 倍を超える場合は採用しない。ちょうどは採用する。

    Raises:
        ValueError: source_size が 0 以下のとき。
    """
    if source_size <= 0:
        raise PolicyError(f"source_size は正の値: {source_size}")
    return output_size / source_size <= min_gain_ratio


def is_duration_consistent(
    source: VideoInfo, output_duration_sec: float | None
) -> bool:
    """出力の再生時間が原本と一致するかを判定する。

    差が DURATION_TOLERANCE_SEC 以内なら一致とみなす。
    どちらかが None のときは True を返す（判定できないことを失敗にしない）。
    """
    if source.duration_sec is None or output_duration_sec is None:
        return True
    return abs(source.duration_sec - output_duration_sec) <= DURATION_TOLERANCE_SEC


def _check_outcome_reason(outcome: Outcome, reason: Reason | None) -> None:
    if outcome is Outcome.ENCODED:
        ok = reason is None
    elif outcome is Outcome.SKIPPED:
        ok = isinstance(reason, SkipReason)
    else:
        ok = isinstance(reason, FailureReason)
    if not ok:
        raise PolicyError(f"outcome と reason の組み合わせが矛盾する: {outcome}, {reason}")


def resolve_dest_dir(
    layout: Layout, outcome: Outcome, reason: Reason | None
) -> Path | None:
    """原本の移動先を決める。in/ に残す場合は None を返す。

    | outcome | reason        | 移動先              |
    |---------|---------------|---------------------|
    | ENCODED | None          | done/               |
    | SKIPPED | NOT_READY     | None（in/ に残す）  |
    | SKIPPED | その他         | skipped/<value>/    |
    | FAILED  | PROBE_FAILED  | failed/             |
    | FAILED  | その他         | None（in/ に残す）  |

    Raises:
        ValueError: 上の表にない組み合わせ（ENCODED に reason がある、
            SKIPPED に FailureReason を渡す等）のとき。
    """
    _check_outcome_reason(outcome, reason)
    if outcome is Outcome.ENCODED:
        return layout.done_dir
    if isinstance(reason, SkipReason):
        if reason is SkipReason.NOT_READY:
            return None
        return layout.skipped_dir(reason)
    if reason is FailureReason.PROBE_FAILED:
        return layout.failed_dir
    return None


def _resolution(width: int, height: int) -> str:
    return f"{width}x{height}"


def build_result(
    run_id: str,
    plan: Plan,
    info: VideoInfo,
    outcome: Outcome,
    reason: Reason | None,
    total_sec: float,
    *,
    output_size: int | None = None,
    encode: EncodeResult | None = None,
    moved_to: str | None = None,
    detail: str | None = None,
) -> Result:
    """Plan と VideoInfo がある素材の Result を組み立てる。

    output_size はエンコードが成功した場合に渡す（NO_GAIN / OUTPUT_INVALID でも渡す。
    B-4 の閾値調整に破棄した出力のサイズ比が要るため）。output_name は ENCODED のときだけ埋める。
    error_tail には ffmpeg が非ゼロ終了したときの stderr 末尾と detail を連結して入れる。

    Raises:
        ValueError: outcome と reason の組み合わせが矛盾するとき。
    """
    _check_outcome_reason(outcome, reason)
    bit_rate = effective_bit_rate(info)
    bpp = bits_per_pixel(info)
    shown_width, shown_height = display_size(info)

    errors: list[str] = []
    if encode is not None and encode.returncode != 0 and encode.stderr_tail:
        errors.append(encode.stderr_tail)
    if detail:
        errors.append(detail)

    output_name = None
    if outcome is Outcome.ENCODED and plan.output_path is not None:
        output_name = plan.output_path.name
    size_ratio = None
    if output_size is not None and info.size_bytes > 0:
        size_ratio = output_size / info.size_bytes

    return Result(
        run_id=run_id,
        source_name=plan.source.name,
        outcome=outcome,
        reason=None if reason is None else reason.value,
        source_size=info.size_bytes,
        output_name=output_name,
        output_size=output_size,
        size_ratio=size_ratio,
        moved_to=moved_to,
        encode_sec=None if encode is None else encode.elapsed_sec,
        total_sec=total_sec,
        video_codec=info.video_codec,
        resolution=_resolution(shown_width, shown_height),
        stored_resolution=_resolution(info.width, info.height),
        rotation=info.rotation,
        fps=info.fps,
        fps_source=info.fps_source,
        bit_rate_bps=None if bit_rate is None else bit_rate[0],
        bit_rate_source=None if bit_rate is None else bit_rate[1],
        bpp=None if bpp is None else bpp[0],
        color_transfer=info.color_transfer,
        audio_codec=info.audio_codec,
        ffmpeg_returncode=None if encode is None else encode.returncode,
        error_tail="\n".join(errors) or None,
    )


def build_result_without_plan(
    run_id: str,
    source: Path,
    source_size: int,
    reason: Reason,
    total_sec: float,
    detail: str | None,
    moved_to: str | None = None,
) -> Result:
    """Plan が存在しない素材（安定待ちタイムアウト、probe 失敗）の Result を組み立てる。

    outcome は reason の型から決まる（SkipReason -> SKIPPED、FailureReason -> FAILED）。
    """
    outcome = Outcome.SKIPPED if isinstance(reason, SkipReason) else Outcome.FAILED
    return Result(
        run_id=run_id,
        source_name=source.name,
        outcome=outcome,
        reason=reason.value,
        source_size=source_size,
        output_name=None,
        output_size=None,
        size_ratio=None,
        moved_to=moved_to,
        encode_sec=None,
        total_sec=total_sec,
        video_codec=None,
        resolution=None,
        stored_resolution=None,
        rotation=None,
        fps=None,
        fps_source=None,
        bit_rate_bps=None,
        bit_rate_source=None,
        bpp=None,
        color_transfer=None,
        audio_codec=None,
        ffmpeg_returncode=None,
        error_tail=detail or None,
    )
