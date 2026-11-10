# video_compressor

iPhone から SMB 共有にコピーした動画を ffmpeg（libx265）で一括圧縮し、iPhone へ戻すためのローカル専用ツール。
1 回の実行で `in/` にある素材を 1 本ずつ処理して終わる。常駐はしない。

設計は `design.md`、要件は `requirements.md` を参照。

## セットアップ

1. Python 3.11 をインストールする（標準ライブラリのみで動く）
2. ffmpeg / ffprobe を PATH に通す。要件どおり winget の Gyan.FFmpeg ビルドに固定して運用する

   ```powershell
   winget install Gyan.FFmpeg
   ffmpeg -version
   ffprobe -version
   ```

3. テストを回す場合だけ pytest を入れる（pytest 以外は入れない）

   ```powershell
   py -3.11 -m venv .venv
   .venv\Scripts\pip install pytest
   .venv\Scripts\pytest
   ```

## SMB 共有の設定

- 共有するのは `--root` に指定するフォルダ 1 つだけ。`in / out / skipped / failed / done / logs` は初回実行時にその中へ作られる
- 共有には共有専用のローカルユーザーを作って使う（Microsoft アカウントでは iOS からの認証が通らないことが多い）
- ネットワークプロファイルは「プライベート」にする。LAN の外へは公開しない
- `in / out / done` などは必ず同じボリュームに置く（起動時に rename で検査し、失敗したら終了コード 2 で止まる）

## 使い方

```powershell
# 判定と ffmpeg コマンドを表示するだけ。<root> は一切変更しない（ロックも取らない）
python compress.py --root D:\share --dry-run

# 本実行
python compress.py --root D:\share
```

主なオプション（全一覧は `python compress.py --help`）:

| オプション | 既定値 | 用途 |
|---|---|---|
| `--crf` / `--preset` | 26 / slow | 画質とサイズ（B-1 / B-2） |
| `--short-side` / `--no-downscale` | 1080 / 縮小する | 短辺の上限（偶数） |
| `--min-gain-ratio` | 0.90 | 出力 / 原本 がこれを超えたら破棄して `skipped/no_gain/` へ（B-4） |
| `--bpp-efficient` / `--bpp-legacy` | 0.045 / 0.075 | これ未満の素材は既に高圧縮としてスキップ（B-3） |
| `--allow-hdr` | 無効 | HDR 素材もエンコードする |
| `--strip-location` | 無効 | 出力から位置情報タグを消す |
| `--no-verify-duration` | 無効 | 出力の再生時間の検証を省く |
| `--keep-in` | 無効 | 原本を `in/` から動かさない（閾値調整用） |
| `--debug` | 無効 | ffprobe の生 JSON を `logs/<run_id>-probe/` に保存する（本実行のみ） |

終了コード: 0 = 失敗なし / 1 = 失敗した素材がある / 2 = 起動時のエラー（ffmpeg 不在、`--root` 不正、ロック中、容量不足）/ 3 = 実行中に ffmpeg が起動できなくなった

### 実行後のディレクトリの意味

| 場所 | 意味 |
|---|---|
| `in/` | 未処理、または `encode_failed` / `output_invalid` / `not_ready` で次回再試行 |
| `out/*.mp4` | 採用が確定した完成品。iPhone へ戻す |
| `out/*.part` | 中断の痕跡。1 時間以上古ければ次回の本実行で削除される |
| `done/` | 処理を終えた原本。目視確認のうえ手動で削除する |
| `skipped/<理由>/` | 対象外と判定した原本（`hdr` / `low_bpp` / `unsupported` / `no_gain`） |
| `failed/` | ffprobe で解析できなかった原本。壊れている可能性がある |
| `logs/<run_id>.csv` / `.meta.json` | 1 実行 1 組。素材ごとの結果と、実行条件の全記録 |

## 運用上の注意

- **`out/` の出力を `in/` に戻さない。** 二重圧縮の検出はしていない（LOW_BPP がほぼ拾うが保証はしない）
- `--keep-in` で調整を繰り返すと、原本が `in/` に残るため毎回再処理され、`out/` に `_c_2.mp4`, `_c_3.mp4` と世代が溜まる。**調整サイクルの前に `out/` を空にする**
- `-map_metadata 0` により撮影日時と**位置情報も出力に引き継ぐ。これは意図した挙動**（Q-6）。出力を第三者に渡すなら `--strip-location` を付ける
- 同じ素材が毎回 `encode_failed` になる場合、ツールは回数を数えない（状態を持たない）。`in/` から `failed/` へ手動で退避する（Q-7）
- `.lock` が残っていて起動できない場合は、前回が異常終了している。実行中のプロセスがないことを確認してから `.lock` を削除する
- 転送中のファイルは、mtime が 30 秒以内なら 5 秒間隔でサイズが 3 回一致するまで待つ（最大 900 秒。超えたら `not_ready` で `in/` に残す）。コピー中は iPhone の画面を点灯させておく

## 手動確認の手順

自動テストは実 ffmpeg を起動しない。ffmpeg / ffprobe を実際に起動する部分は以下の手順で手動確認する。
**実 `<root>` の原本には実行しない。** 素材をコピーした作業用フォルダで行う。

### 段階 5: `ffmpeg_io` のプロセス起動

PowerShell でリポジトリ直下から実行する。`<素材>` は代表素材のコピー、`<日本語名の素材>` はファイル名に日本語を含む素材のコピー。

1. `check_tools()` がバージョン文字列を 2 つ返す

   ```powershell
   python -c "import ffmpeg_io; print(ffmpeg_io.check_tools())"
   ```

2. `probe()` が段階 4 と同じ `VideoInfo` を返す（`fixtures/probe/` を採った素材で、fixture から作った `VideoInfo` と一致すること）

   ```powershell
   python -c "import sys, json, pathlib, ffmpeg_io; p = pathlib.Path(sys.argv[1]); a = ffmpeg_io.probe(p); b = ffmpeg_io.parse_probe_json(json.loads(pathlib.Path(sys.argv[2]).read_text(encoding='utf-8')), p, p.stat().st_size); print(a); print('一致' if a == b else '不一致')" "<素材>" fixtures\probe\landscape_hevc.json
   ```

3. `run_ffmpeg()` が存在しない入力に対して、例外ではなく `returncode != 0` を返す
4. `stderr_tail` に日本語のファイル名が化けずに出る（R-7）

   ```powershell
   python -c "import ffmpeg_io; r = ffmpeg_io.run_ffmpeg(('ffmpeg', '-hide_banner', '-i', '存在しない動画_日本語.mov', '-f', 'null', '-')); print(r.returncode); print(r.stderr_tail)"
   ```

   `returncode` が 0 以外で、2 行目以降に `存在しない動画_日本語.mov` がそのまま表示されること。

5. 日本語名の素材で `probe()` が通る

   ```powershell
   python -c "import sys, pathlib, ffmpeg_io; print(ffmpeg_io.probe(pathlib.Path(sys.argv[1])))" "<日本語名の素材>"
   ```

確認結果（2026-11-10、Linux コンテナ上の ffmpeg 6.1.1、合成クリップ使用）: 1, 3, 4, 5 は期待どおり。
2 は実素材と fixture が未収集のため未実施。Windows 上での確認も未実施。

### 段階 7: CSV が Excel で文字化けしない

1. 本実行を 1 回行い（または `python -c` で `runlog.create_run_log()` と `append_result()` を呼び）、`logs\<run_id>.csv` を作る
2. エクスプローラーから CSV をダブルクリックして Excel で開く
3. 日本語のファイル名（`source_name` 列）が化けずに表示されること

CSV は先頭に BOM 付きの UTF-8（utf-8-sig）で書いており、BOM が先頭に 1 つだけあることは `tests/test_runlog.py` で確認している。
Excel での表示は未確認。
