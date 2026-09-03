# ffprobe fixture

`tests/test_probe_parse.py` が使う ffprobe の生出力を置く。

## 収集手順

```
ffprobe -v quiet -print_format json -show_format -show_streams "<素材>" > fixtures/probe/<名前>.json
```

## 必要な 6 種類

| ファイル名 | 素材の条件 | 撮影端末 / 設定 |
|---|---|---|
| `landscape_hevc.json` | 横位置・HEVC | （未収集） |
| `portrait_rot90.json` | 縦位置・`rotation` が 90 | （未収集） |
| `hdr_hlg.json` | HDR（`color_transfer` が `arib-std-b67`） | （未収集） |
| `slowmo_240fps.json` | スローモーション 240fps | （未収集） |
| `no_audio.json` | 音声ストリームなし | （未収集） |
| `missing_bitrate.json` | ストリームの `bit_rate` が欠落 | （未収集） |

## 規則

**手で書いた「それらしい JSON」を置かない。** 実素材に対して実際に `ffprobe` を
実行した生の出力でなければならない（CLAUDE.md「fixture を捏造しない」）。
実際の ffprobe が返すフィールド構成とずれた fixture でテストが通っても意味がない。

収集したら上の表の「撮影端末 / 設定」を埋める。
