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
