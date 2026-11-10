# CLAUDE.md

動画ファイル一括圧縮ツールの開発ガイド。`design.md` v0.3 に沿って実装する。

**このファイルは毎回コンテキストに載る。追記するときは「知らないと事故る情報」だけにする。**
背景・経緯・判断理由は `design.md` に書く。ここには書かない。

---

## プロジェクト概要

iPhone から SMB 共有にコピーした動画を、ffmpeg で一括圧縮して返すローカル専用ツール。
CLI で 1 回実行 = 1 バッチ。常駐しない。作者 1 人が使う。

Python 3.11 / Windows / 標準ライブラリのみ（テストの pytest を除く）。

---

## 最優先の 3 つのルール

### 1. 実素材と実 `<root>` に触らない

`--root` に指定された実ディレクトリに対して `compress.py` を実行しないこと。
原本の動画は取り返しがつかない。テストは必ず `tmp_path` 上で行う。

実素材での試運転が必要な段階（段階 11）に来たら、**実行せずに手順を提示して確認を取る。**

`ffprobe` の読み取りのみは許可。`ffmpeg` のエンコード実行は確認を取ってから。

### 2. `design.md` に書いていないことを実装しない

「ついでに便利だから」で機能を足さない。`design.md` の
「意図的に採用しなかった選択肢」に並んでいるものは、検討済みで却下されている。
実装したくなったら、その表を読んでから相談する。

とくに足しがちなもの: 設定ファイル、ffprobe 結果のキャッシュ、リトライ機構、並列エンコード、
進捗ファイル、`Stage` 抽象、`models.py` / `config.py` の分離、`dataclasses.fields()` による CSV 列の自動導出。
すべて却下済み。

### 3. 設計の誤りを見つけたら止まる

実装中に `design.md` の記述が不可能・矛盾していると分かったら、
黙って回避せずに報告する。合意できたら `design.md` を先に直してからコードを書く。
コードと設計書が食い違ったまま進めない。

---

## コマンド

```bash
pytest                          # 全テスト。1 回 10 秒以内に収める
pytest tests/test_policy.py -v  # 単体
pytest -k rotation              # 絞り込み

python compress.py --help
python compress.py --root /tmp/testroot --dry-run   # tmp 上のダミーに対してのみ
```

外部パッケージを `pip install` しない。pytest 以外を入れたくなったら、それは設計違反。

---

## 実装順序

`design.md` の「実装順序」に 11 段階と各段階の完了条件がある。**順番を飛ばさない。**
下流（`compress.py`）から書き始めると、上流のインターフェースが後から変わって手戻りする。

各段階の完了条件を満たしたら `pytest` を通し、その段階を 1 コミットにする。

### 進捗

- [x] 段階 1: `policy.py` のデータ構造と定数
- [x] 段階 2: `policy.py` の純粋関数 + `tests/test_policy.py`
- [ ] 段階 3: `fixtures/probe/` の収集（**要: 実素材。ここで人の作業が要る**）← 未収集
- [ ] 段階 4: `parse_probe_json()` + `tests/test_probe_parse.py` ← 実装済み。fixture 未収集のためテストの大半が skip。揃ったら skip が消えることを確認して完了
- [ ] 段階 5: `ffmpeg_io` のプロセス起動 ← 実装済み。実素材での probe() 一致確認（README 手順 2）と Windows での確認が残り
- [x] 段階 6: `workspace.py` + `tests/test_workspace.py`
- [ ] 段階 7: `runlog.py`
- [ ] 段階 8: `process_one()` + `tests/test_process_one.py`（**最重要**）
- [ ] 段階 9: `compress.py` の CLI とループ
- [ ] 段階 10: `tests/test_architecture.py`
- [ ] 段階 11: 実素材での試運転

完了したらこのチェックボックスを更新する。

---

## 実装上の罠

素直に書くと間違える箇所。すべて設計レビューで実際に見つかった欠陥。

### 採否判定はリネームの「前」

```
run_ffmpeg → file_size(.part) → is_worth_keeping → is_duration_consistent → finalize_output
```

`.part` を `.mp4` にリネームしてよいのは、その出力を採用すると確定した後だけ。
先にリネームすると、中断時に不採用品が完成品として `out/` に残り、ユーザーがそれを iPhone に戻す。

### `Plan` に `dest_dir` はない

原本の移動先は `policy.resolve_dest_dir(layout, outcome, reason)` が決める。
`NO_GAIN` はエンコード後にしか分からないので、`Plan` 生成時には確定できない。
`Plan` に移動先を持たせたくなったら、それは設計を戻している。

### 解像度は 2 種類ある

- `VideoInfo.width` / `height` = 格納寸法（回転前）
- `policy.display_size(info)` = 表示寸法（回転適用後）

`scale_filter()` は**表示寸法**で短辺を判定する。`bits_per_pixel()` は**格納寸法**を使う。
iPhone の縦位置動画は `1920x1080 + rotation=90` として記録される。混同すると縦動画が壊れる。

### fps は `avg_frame_rate` 優先

`r_frame_rate` ではない。スローモーション素材で 4 倍ずれて bpp 判定が壊れる。
採用元を `VideoInfo.fps_source` に記録する。

### 音声は 3 分岐

`audio_codec is None` → `-an` / `in options.audio_copy_codecs` → `-c:a copy` / それ以外 → `-c:a aac`。
None を忘れると音声なし素材でエンコードが落ちる。

### `process_one()` は CSV を書かない

`Result` を返すだけ。`append_result()` を呼ぶのは `run()` のループ。

### 例外は 2 種類に分ける

- 素材ローカル（`ProbeError`, `OSError`, ffmpeg の非ゼロ終了）→ `Result` に畳み込む
- 実行全体（`FFmpegNotFoundError`）→ **再送出する**。全素材で失敗を積み上げてから終わるのは間違い

### dry-run は `<root>` を一切変更しない

`.part` の掃除も `meta.json` の書き出しも `wait_until_stable()` も dry-run では走らない。
`ensure_layout()`（mkdir と rename スモークテスト）と `acquire_lock()` も走らない。dry-run の分岐は `parse_args` の直後。
これは不変条件。テストで検証する。

### `SkipReason` と `FailureReason` は別の enum

`skipped/` に行くものと `failed/` に行くものを 1 つの enum にまとめない。
`skipped_dir()` は全入力に対して値を返す全域関数でなければならない（例外を投げさせない）。

---

## アーキテクチャの不変条件

`tests/test_architecture.py` が `ast` で検査する（段階 10）。それ以前も手で守る。

1. `policy.py` はプロジェクト内の他モジュールを import しない
2. `policy.py` は `subprocess` / `shutil` / `os` を import しない。`Path` は値として持つだけで `exists()` / `stat()` を呼ばない
3. `ffmpeg_io.py` / `workspace.py` / `runlog.py` は互いを import しない
4. `compress.py` に判定を書かない。素材の性質を見て `if` で分岐したくなったら `policy.py` に置く

依存の矢印はすべて `policy` に向かい、`policy` からは出ない。

---

## テスト方針

**実 ffmpeg を起動するテストを書かない。** `pytest` 全体で 10 秒以内。

- `policy` の関数 → `VideoInfo` を手で組む。テスト側に `make_info(**overrides)` ヘルパを置く
- `parse_probe_json()` → `fixtures/probe/` の固定 JSON
- `workspace` → `tmp_path`。`wait_until_stable()` は `sleep_fn` / `now_fn` を注入して実時間を消費しない
- `process_one()` → `probe_fn` / `encode_fn` / `duration_fn` にフェイクを注入。
  フェイクの `encode_fn` は「`.part` に指定サイズのダミーを書いて `returncode=0` を返す」だけ

`monkeypatch` でモジュール属性を差し替えない。差し替えたくなったら、それは依存注入の口が足りていない。

### fixture を捏造しない

`fixtures/probe/*.json` は**実素材に対して `ffprobe` を実行した生の出力**でなければならない。
手で書いた「それらしい JSON」は、実際の ffprobe が返すフィールド構成と必ずずれる。
このツールの目的は現実の素材を捌くことなので、捏造した fixture でテストが通っても意味がない。

必要な 6 種類（横位置 HEVC / 縦位置 rot90 / HDR HLG / スローモーション 240fps / 音声なし / bit_rate 欠落）が
揃っていなければ、段階 4 に進まずに収集を依頼する。

### 段階 8 の必須ケース

本設計で最もバグが出やすい箇所。以下は設計 v0.1 で実際に壊れていた経路。

- `NO_GAIN` → `out/` に `.mp4` も `.part` もなく、原本が `skipped/no_gain/`
- `OUTPUT_INVALID` → `out/` が空で、原本が `in/` に残る
- `PROBE_FAILED` → `outcome == FAILED` で、原本が `failed/`

---

## コードスタイル

- Python 3.11。全ファイルの先頭に `from __future__ import annotations`
- 公開関数・クラスに型ヒントを付ける。`Any` を使わない
- docstring は Google スタイル、**日本語**。`design.md` の記述をそのまま使ってよい
- 自明なコードにコメントを付けない。コメントは「なぜそうしたか」だけ
- `dataclass(frozen=True)` を既定にする。mutable なフィールド（`dict`, `list`）を frozen dataclass に入れない
- 例外は独自クラスを定義して投げる。`raise Exception(...)` や `assert` による入力検証を書かない
- パスは `pathlib.Path`。文字列連結でパスを作らない

---

## Windows 固有の注意

- ファイル名に日本語が入る。`subprocess` は `encoding="utf-8", errors="replace"` を明示（`text=True` に頼らない）
- CSV は `utf-8-sig`（Excel で開くため）
- `os.stat().st_dev` は SMB / UNC パスで信用できない。同一ボリューム検査は rename のスモークテストで行う
- パス区切りをハードコードしない

---

## 作業の進め方

- 1 段階 = 1 コミット。段階の途中でコミットしない
- コミットメッセージは日本語。「段階 6: workspace.py とテスト」のように段階番号を含める
- 実装前に該当段階の完了条件を読み、それを満たすテストから書く
- 完了条件を満たせない場合、勝手に条件を緩めない。報告する
- `design.md` を更新したら、その旨をコミットメッセージに書く
