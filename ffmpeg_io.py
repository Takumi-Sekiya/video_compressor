"""ffprobe / ffmpeg のプロセス起動と、その出力の内部形式への変換。

判定は持たない。ファイルの移動・削除もしない。
"""

from __future__ import annotations

import math
from pathlib import Path

from policy import VideoInfo


class FFmpegNotFoundError(RuntimeError):
    """ffmpeg または ffprobe が PATH 上に見つからない。実行全体を中止すべき事象。"""


class ProbeError(RuntimeError):
    """ffprobe の実行またはパースに失敗した。素材ローカルな失敗。

    Attributes:
        path: 対象ファイル。
        detail: ffprobe の stderr 末尾、またはパースエラーの内容。
    """

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail


# ---------------------------------------------------------------------------
# ffprobe の JSON の解釈
# ---------------------------------------------------------------------------


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    return {}


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def _as_float(value: object) -> float | None:
    # ffprobe の JSON は bit_rate や duration を文字列で、width などを数値で返す
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_int(value: object) -> int | None:
    number = _as_float(value)
    if number is None or not math.isfinite(number):
        return None
    return int(number)


def _parse_rate(value: object) -> float | None:
    """"30000/1001" のような有理数表記を float にする。0/0 や 0 は None。"""
    text = _as_str(value)
    if text is None:
        return None
    num_text, sep, den_text = text.partition("/")
    num = _as_float(num_text)
    den = _as_float(den_text) if sep else 1.0
    if num is None or den is None or den == 0 or num <= 0:
        return None
    return num / den


def _normalize_rotation(clockwise_degrees: float) -> int:
    return (round(clockwise_degrees / 90) * 90) % 360


def _rotation(stream: dict[str, object]) -> int:
    for item in _as_list(stream.get("side_data_list")):
        side_data = _as_dict(item)
        if side_data.get("side_data_type") != "Display Matrix":
            continue
        counter_clockwise = _as_float(side_data.get("rotation"))
        if counter_clockwise is not None:
            # Display Matrix の rotation は反時計回り。tags.rotate（時計回り）に揃える
            return _normalize_rotation(-counter_clockwise)
    clockwise = _as_float(_as_dict(stream.get("tags")).get("rotate"))
    if clockwise is not None:
        return _normalize_rotation(clockwise)
    return 0


def _is_cover_art(stream: dict[str, object]) -> bool:
    return _as_int(_as_dict(stream.get("disposition")).get("attached_pic")) == 1


def parse_probe_json(payload: dict[str, object], path: Path, size_bytes: int) -> VideoInfo:
    """ffprobe の JSON 出力を VideoInfo へ変換する。副作用を持たない。

    映像はカバー画像（disposition.attached_pic）を除く最初の映像ストリーム、
    音声は最初の音声ストリームを採る（build_ffmpeg_args() の -map と一致させる）。
    fps: avg_frame_rate を優先し、0/0 または欠落のときのみ r_frame_rate を使う。
         採用元を fps_source に記録する。
    rotation: 時計回りの角度（tags.rotate と同じ向き。iPhone の縦位置は 90）。
              side_data_list の Display Matrix を優先し、なければ tags.rotate。
              Display Matrix の rotation は反時計回りの角度なので符号を反転してから、
              0 / 90 / 180 / 270 に正規化する（負値は +360 して丸める）。

    Raises:
        ProbeError: 映像ストリームが 1 本も見つからない、または
            width / height / fps のいずれかが取得できないとき。
    """
    streams = [_as_dict(s) for s in _as_list(payload.get("streams"))]
    fmt = _as_dict(payload.get("format"))

    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not _is_cover_art(s)),
        None,
    )
    if video is None:
        raise ProbeError(path, "映像ストリームが見つからない")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    width = _as_int(video.get("width"))
    height = _as_int(video.get("height"))
    if width is None or height is None or width <= 0 or height <= 0:
        raise ProbeError(path, f"映像の寸法が取得できない: width={width}, height={height}")

    fps = _parse_rate(video.get("avg_frame_rate"))
    fps_source = "avg"
    if fps is None:
        fps = _parse_rate(video.get("r_frame_rate"))
        fps_source = "r"
    if fps is None:
        raise ProbeError(path, "フレームレートが取得できない（avg_frame_rate / r_frame_rate とも無効）")

    duration = _as_float(fmt.get("duration"))
    if duration is None:
        duration = _as_float(video.get("duration"))

    audio_codec: str | None = None
    audio_bit_rate: int | None = None
    if audio is not None:
        audio_codec = _as_str(audio.get("codec_name")) or "unknown"
        audio_bit_rate = _as_int(audio.get("bit_rate"))

    return VideoInfo(
        path=path,
        size_bytes=size_bytes,
        duration_sec=duration,
        width=width,
        height=height,
        rotation=_rotation(video),
        fps=fps,
        fps_source=fps_source,
        video_codec=_as_str(video.get("codec_name")) or "unknown",
        pix_fmt=_as_str(video.get("pix_fmt")) or "unknown",
        video_bit_rate_bps=_as_int(video.get("bit_rate")),
        container_bit_rate_bps=_as_int(fmt.get("bit_rate")),
        audio_codec=audio_codec,
        audio_bit_rate_bps=audio_bit_rate,
        color_transfer=_as_str(video.get("color_transfer")),
        color_primaries=_as_str(video.get("color_primaries")),
    )
