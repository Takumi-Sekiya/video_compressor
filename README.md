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
