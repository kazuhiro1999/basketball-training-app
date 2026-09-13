# hoop-analyzer — 録画に対する骨格推定・ボール検出・トラッキング

delaycam の録画（または普通の動画）を入力に、**複数人の骨格（COCO-17）に ID を付け、ボールを追跡**して
`pose.jsonl` を書く。姿勢推定は 3 方式を差し替え可能。すべて **ONNX Runtime (CPU)** で動き、
torch / TensorFlow はランタイムに不要。GPU や環境変数には触らない。

| `--pose` | 方式 | モデル | サイズ | 特徴 |
|---|---|---|---|---|
| `rtmo` | one-stage | RTMO (rtmlib) | t / **s** / m / l | 人数が増えても速度がほぼ一定。軽量で最初の候補 |
| `rtmpose` | top-down | YOLOX 人検出 + RTMPose (rtmlib) | s / **m** / x | 精度が高い。人数に比例して遅くなる（観客が写ると重い） |
| `yolo` | one-stage | YOLO11-pose (Ultralytics → ONNX) | **n** / s / m | 最も手軽。ボール検出と同じ枠組み |

トラッカー（`--tracker`）: `simple`（IoU + キーポイント一致 + 服の色で対応付け、依存なし、既定）/ `bytetrack` / `ocsort` / `sort`（roboflow `trackers`）。
ボール（`--ball yolo`）: YOLO11 の COCO `sports ball` クラス + 等速外挿の簡易トラッカー。

## セットアップ

```bash
cd analyzer
uv sync                      # 依存関係 (onnxruntime, rtmlib, opencv, av, scipy)
uv sync --extra trackers     # ByteTrack / OC-SORT も使う場合
uv run tools/export_yolo.py  # YOLO11n の ONNX を models/ に作る (一度だけ。この時だけ torch を一時環境に入れる)
```

RTMO / RTMPose のモデルは初回実行時に `~/.cache/rtmlib/` へ自動ダウンロードされる（要ネット、30〜100MB）。

## 使い方

```bash
# 録画フォルダをそのまま渡す (出力は <録画>/analysis/<pose>-<size>/)
uv run analyze ../delaycam/recordings/2026-09-13_17-40-12 --pose rtmo --size s --ball yolo --overlay

# 普通の動画でも可 (出力は <動画名>_analysis/<pose>-<size>/)
uv run analyze clip.mp4 --pose rtmpose --size m --tracker bytetrack
uv run analyze clip.mp4 --pose yolo --size n --max-frames 300 --stride 2   # 動作確認用に間引く

# 速度比較 (ライブ表示に載せる軽量モデル選び)
uv run bench clip.mp4 --frames 100
uv run bench clip.mp4 --configs rtmo:t rtmo:s yolo:n

# ID の安定性の目安 (ID 数、短寿命トラック数、オクルージョン後の再付与疑い)
uv run trackstats clip_analysis
```

主なオプション: `--det-thr`（人物検出しきい値、既定 0.5）、`--kp-thr`（キーポイント信頼度、描画/bbox 用）、
`--ball-thr`（既定 0.25）、`--ball-roi persons|none`（人物周辺を切り出して検出、既定 persons）、
`--threads`（ONNX Runtime のスレッド数/セッション、既定は CPU の半分・最大 8）、`--out`、`--overlay`。

## 出力

```
analysis/rtmo-s_simple/            (<pose>-<size>_<tracker>)
  pose.jsonl          1 行 1 フレーム
  analysis_meta.json  モデル・パラメータ・処理時間・平均人数・ボール検出率
  overlay.mp4         --overlay 時。骨格 + ID + ボールを描いた確認用動画 (H.264, ブラウザでも再生可)
```

`pose.jsonl` の 1 行:

```json
{"i": 120, "seg": 1, "n": 121, "ts_us": 4363210,
 "persons": [{"id": 3, "bbox": [x1, y1, x2, y2], "score": 0.82,
              "kp": [[x, y, conf], ... 17 点 (COCO 順: nose, l_eye, r_eye, l_ear, r_ear, l_shoulder, r_shoulder,
                                              l_elbow, r_elbow, l_wrist, r_wrist, l_hip, r_hip, l_knee, r_knee, l_ankle, r_ankle)]}],
 "ball": {"id": 1, "bbox": [...], "center": [x, y], "score": 0.71, "predicted": false}}
```

- `ts_us` は録画の `frames.csv` と同じ値。delaycam の表示側・操作ログ（`events.jsonl` の `display_ts_us`）と突き合わせられる
- `id` はトラッカーが付けた 1 始まりの通し番号。生まれたばかり（未確定）のトラックは `null`
- `ball.predicted = true` は検出できず速度で外挿したフレーム（最大 10 フレーム）

## 設計

```
hoop_analyzer/
  backends/            姿勢推定。base.PoseBackend を継承して infer(frame)->list[Person] を実装し、
    __init__.py        REGISTRY に登録すれば --pose で選べる
    rtmpose.py rtmo.py yolo_pose.py
  ball.py              BallDetector (YOLO ONNX) + roi_around_persons + BallTracker
  tracking.py          SimpleTracker (IoU + キーポイント一致 + 服の色ヒストグラム) / RoboflowTracker (bytetrack, ocsort, sort)
  trackstats.py        ID 安定性の集計
  yolo_onnx.py         Ultralytics ONNX の letterbox / NMS / 座標復元 (torch 不要)
  ort_config.py        ONNX Runtime のスレッド設定 (下記)
  video.py             録画フォルダ / 動画ファイルの共通リーダ (PyAV)
  overlay.py           描画と overlay.mp4
  run.py bench.py      CLI
```

**ONNX Runtime のスレッド**: 1 プロセスに複数セッション（姿勢 + ボール、検出 + 姿勢）があると、
既定では各セッションが全コア分のスレッドを作ってスピン待機し、互いに奪い合って **10 倍以上遅くなる**
（実測: rtmo-s + ball で 160 + 112 ms → 68 + 29 ms）。`ort_config.py` で全セッションのスレッド数を抑え、
スピンを切っている。rtmlib が内部で作るセッションも同じ設定で作り直す。

## 現状の性能目安（CPU のみ、1280×720、24 論理コア）

| backend | ms/frame | 備考 |
|---|---|---|
| rtmo-t | 30 | 416×416 入力。ライブ表示向け候補 |
| rtmo-s | 64 | 既定 |
| yolo-n (pose) | 38 | |
| rtmpose-s | 220 (15 人時) | 人数に比例。数人なら 60〜80 ms |
| rtmpose-m | 1000 (50 人時) | 観客込みのサンプルでの値。コート数人なら 150 ms 前後 |
| ball yolo-n | 28 | ROI 切り出し込み |

## 分かっていること・次にやること

- COCO 学習済みの `sports ball` は、**手に持ったボール**や 15px 程度の小さいボールはほぼ拾えない（合成した飛翔中のボールは 50% 検出 + 外挿で追跡できた）。実データが録れたら数百枚アノテーションしてバスケットボール専用にファインチューンする（同じ ONNX 形式で `--ball` に差し替え可能）
- 小さく写る人（60px 程度）は RTMO-s の既定しきい値 0.5 で落ちる。`--det-thr 0.3` で拾えるが誤検出も増える
- シューターの特定（ボールを持っている人 / シュート位置にいる人）とショット区間の切り出しは、実データを見てから設計する
- サンプル動画（`data/samples/`, git 管理外）: OpenCV の `vtest.avi`、Wikimedia Commons の写真（CC BY-SA 4.0, Michael Barera）から作った `freethrow*.mp4`
