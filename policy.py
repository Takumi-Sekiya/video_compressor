"""判定・引数生成・移動先の決定と、それらが使う定数とデータ構造。

このモジュールは外部 I/O を持たず、プロジェクト内の他モジュールを import しない。
依存の矢印はすべてここに向かい、ここからは出ない（design.md「守るべき不変条件」）。

`Path` は値として扱うだけで、`exists()` / `stat()` などの問い合わせは行わない。
ディレクトリの実在を保証するのは `workspace` の責務である。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

# --------------------------------------------------------------------------
# 定数
# --------------------------------------------------------------------------

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

# 実行時ディレクトリの名前。<root> 直下の構成は design.md「実行時ディレクトリ」を参照
_IN_DIR_NAME: str = "in"
_OUT_DIR_NAME: str = "out"
_SKIPPED_DIR_NAME: str = "skipped"
_FAILED_DIR_NAME: str = "failed"
_DONE_DIR_NAME: str = "done"
_LOGS_DIR_NAME: str = "logs"
_LOCK_FILE_NAME: str = ".lock"


# --------------------------------------------------------------------------
# 列挙型
# --------------------------------------------------------------------------


class Action(Enum):
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


# --------------------------------------------------------------------------
# データ構造
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """作業ディレクトリのパス集合。実在は保証しない（それは workspace の責務）。"""

    root: Path

    @property
    def in_dir(self) -> Path:
        return self.root / _IN_DIR_NAME

    @property
    def out_dir(self) -> Path:
        return self.root / _OUT_DIR_NAME

    @property
    def done_dir(self) -> Path:
        return self.root / _DONE_DIR_NAME

    @property
    def failed_dir(self) -> Path:
        return self.root / _FAILED_DIR_NAME

    @property
    def logs_dir(self) -> Path:
        return self.root / _LOGS_DIR_NAME

    @property
    def lock_path(self) -> Path:
        return self.root / _LOCK_FILE_NAME

    def skipped_dir(self, reason: SkipReason) -> Path:
        """`<root>/skipped/<reason.value>/` を返す。全入力に対して値を返す。

        Args:
            reason: スキップ理由。

        Returns:
            スキップ理由ごとのディレクトリ。
        """
        return self.root / _SKIPPED_DIR_NAME / reason.value


@dataclass(frozen=True)
class VideoInfo:
    """ffprobe から得た素材の仕様。ffmpeg_io.parse_probe_json() が生成する。

    ffprobe の出力は素材によってフィールドが欠落するため、
    欠落しうる項目は None を許容する（R-2）。
    """

    path: Path
    size_bytes: int
    duration_sec: float | None
    width: int                      # 格納寸法（回転前）
    height: int                     # 格納寸法（回転前）
    rotation: int                   # 0 / 90 / 180 / 270 に正規化
    fps: float
    fps_source: str                 # "avg" | "r"。どちらから採ったかを CSV に出す
    video_codec: str
    pix_fmt: str
    video_bit_rate_bps: int | None  # 映像ストリームの bit_rate
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
    原本の移動先は持たない。NO_GAIN はエンコード後にしか分からないため、
    Plan の生成時には確定できない（resolve_dest_dir() が決める）。

    ffmpeg_args は subprocess にそのまま渡せる完全な引数列で、
    先頭の "ffmpeg" を含む。
    """

    source: Path
    action: Action
    reason: SkipReason | None
    output_path: Path | None
    ffmpeg_args: tuple[str, ...]

    def __post_init__(self) -> None:
        """action と他フィールドの整合を実行時に検査する。

        コメントで表現していると classify() に分岐を足したときに静かに壊れるため、
        不変条件をコードに置く。

        Raises:
            ValueError: action と他フィールドの組み合わせが矛盾するとき。
        """
        if self.action is Action.SKIP:
            if self.reason is None:
                raise ValueError("action=SKIP には reason が必要です")
            if self.output_path is not None:
                raise ValueError("action=SKIP では output_path は None でなければなりません")
            if self.ffmpeg_args != ():
                raise ValueError("action=SKIP では ffmpeg_args は空でなければなりません")
        else:
            if self.reason is not None:
                raise ValueError("action=ENCODE では reason は None でなければなりません")
            if self.output_path is None:
                raise ValueError("action=ENCODE には output_path が必要です")
            if not self.ffmpeg_args:
                raise ValueError("action=ENCODE には ffmpeg_args が必要です")


@dataclass(frozen=True)
class EncodeResult:
    """ffmpeg プロセスの実行結果。ffmpeg_io.run_ffmpeg() が返す。"""

    returncode: int
    stderr_tail: str
    elapsed_sec: float


@dataclass(frozen=True)
class Result:
    """1 素材の最終結果。CSV の 1 行に対応する。"""

    run_id: str
    source_name: str
    outcome: Outcome
    reason: str | None          # SkipReason / FailureReason の value
    source_size: int
    output_name: str | None
    output_size: int | None
    size_ratio: float | None
    moved_to: str | None        # 移動後のパス（<root> からの相対）。移動しなければ None
    encode_sec: float | None    # ffmpeg プロセスの実行時間のみ
    total_sec: float            # 安定待ちと probe を含む素材全体の所要時間
    video_codec: str | None
    resolution: str | None      # 表示寸法（回転適用後）
    stored_resolution: str | None
    rotation: int | None
    fps: float | None
    fps_source: str | None
    bit_rate_bps: int | None
    bit_rate_source: str | None  # "video_stream" | "container_minus_audio" | "filesize"
    bpp: float | None
    color_transfer: str | None
    audio_codec: str | None
    ffmpeg_returncode: int | None
    error_tail: str | None


@dataclass(frozen=True)
class RunContext:
    """1 回の実行を再現するために必要な情報のすべて。

    policy_constants は export_constants() の結果をそのまま持つ。
    frozen dataclass に mutable な dict を入れる唯一の例外で、
    RunContext を hash しない前提で許容している（meta.json へ json.dumps() するため）。
    """

    run_id: str
    started_at: datetime
    argv: tuple[str, ...]
    root: Path
    options: Options
    policy_constants: dict[str, object]   # policy.export_constants() の結果
    dry_run: bool
    keep_in: bool
    ffmpeg_version: str
    ffprobe_version: str
    tool_version: str
    git_commit: str | None
    python_version: str
    platform: str


# --------------------------------------------------------------------------
# 関数
# --------------------------------------------------------------------------


def export_constants() -> dict[str, object]:
    """meta.json に記録する定数のスナップショットを返す。

    Options に入らない定数のうち、判定結果に影響するものをすべて含める。
    frozenset はソート済み list に変換する（JSON 化のため）。

    Returns:
        json.dumps() 可能な辞書。
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
