# hoop-analyzer — 録画に対する骨格推定・ボール検出・トラッキング

delaycam の録画（または普通の動画）を入力に、**複数人の骨格（COCO-17）に ID を付け、ボールを追跡**して
`pose.jsonl` を書く。姿勢推定は 3 方式を差し替え可能。すべて **ONNX Runtime (CPU)** で動き、
torch / TensorFlow はランタイムに不要。GPU や環境変数には触らない。

| `--pose` | 方式 | モデル | サイズ | 特徴 |
|---|---|---|---|---|
| `rtmo` | one-stage | RTMO (rtmlib) | t / **s** / m / l | 人数が増えても速度がほぼ一定。軽量で最初の候補 |
| `rtmpose` | top-down | YOLOX 人検出 + RTMPose (rtmlib) | s / **m** / x | 精度が高い。人数に比例して遅くなる（観客が写ると重い） |
| `yolo` | one-stage | YOLO11-pose (Ultralytics → ONNX) | **n** / s / m | 最も手軽。ボール検出と同じ枠組み |
| `mediapipe` | top-down (内蔵) | MediaPipe Pose Landmarker (TFLite 内蔵) | lite / **full** / heavy | 検証用・1 人向け。33 点 + 3D world landmarks。`--num-poses` で複数も可だが ID 追跡なし |
| `holistic` | top-down (内蔵) | MediaPipe Holistic Landmarker | - | 検証用・1 人。姿勢 33 点 + 両手 21 点ずつ（リリースの指が見える） |

MediaPipe 系は `uv sync --extra mediapipe`。BlazePose の人検出は画像を 224px に縮めて行うので広い画角では人を見つけられない。
そのため既定 (`--mp-roi auto`) では **YOLO11n で一番大きく写る人を正方形に切り出し、その中で MediaPipe を動かす**
（枠は人が端に寄るか 30 フレームごとに取り直す。VIDEO モードは入力サイズ固定が必要なので 512×512 にリサイズ）。
1 人が画面いっぱいに写る録画なら `--mp-roi none` でよい。

トラッカー（`--tracker`）: `simple`（IoU + キーポイント一致 + 服の色で対応付け、依存なし、既定）/ `bytetrack` / `ocsort` / `sort`（roboflow `trackers`）。
ボール（`--ball yolo`）: YOLO11 の COCO `sports ball` クラス + 等速外挿の簡易トラッカー。

## セットアップ

```bash
cd analyzer
uv sync                                    # 依存関係 (onnxruntime, rtmlib, opencv, av, scipy)
uv sync --extra trackers --extra mediapipe # ByteTrack / OC-SORT、MediaPipe も使う場合
uv run tools/export_yolo.py                # YOLO11n の ONNX を models/ に作る (一度だけ。この時だけ torch を一時環境に入れる)
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
`--threads`（ONNX Runtime のスレッド数/セッション、既定は CPU の半分・最大 8）、`--out`、`--overlay`、
`--num-poses`（mediapipe の人数上限）、`--mp-roi auto|none`（mediapipe/holistic の切り出し）。

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
- `mediapipe` / `holistic` は上記に加えて `mp33`（BlazePose 33 点 `[x, y, z, visibility, presence]`、ピクセル）、
  `world`（33 点の 3D world landmarks、腰中心・メートル）、holistic はさらに `hands: {left, right}`（各 21 点 `[x, y, z]`）を持つ

## 設計

```
hoop_analyzer/
  backends/            姿勢推定。base.PoseBackend を継承して infer(frame)->list[Person] を実装し、
    __init__.py        REGISTRY に登録すれば --pose で選べる
    rtmpose.py rtmo.py yolo_pose.py mediapipe_pose.py
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

## パイプラインと処理時間

| backend | 段構成 | 1 フレームの流れ |
|---|---|---|
| `rtmpose` | **2 段** (検出 → 姿勢) | YOLOX (HumanArt 学習, 416〜640 入力) で人 bbox → 各 bbox をアフィン変換で 256×192 (x は 384×288) に切り出し → RTMPose (SimCC) → 17 点。ONNX セッション 2 本。時間 = 検出 + 人数 × 姿勢 |
| `rtmo` | **1 段** | 640×640 (t は 416) に 1 回通すと全員分の bbox + 17 点が出る → NMS。bbox はキーポイントから作り直す。時間は人数によらずほぼ一定 |
| `yolo` | **1 段** | YOLO11-pose 640×640 に 1 回 → 全員分の bbox + 17 点 → NMS。時間は人数によらず一定 |
| `mediapipe` / `holistic` | 2 段 (内蔵) | (auto ROI: YOLO11n で人 bbox → 正方形切り出し 512×512) → BlazePose の内蔵検出 + ランドマーク。VIDEO モードで前フレームから追跡 |

ボール検出 (YOLO11 COCO) と人物トラッカーは、どのバックエンドの後段にも同じものが付く。

処理時間（CPU のみ、24 論理コア、ONNX Runtime 8 スレッド、他の負荷なし）:

| backend | 1280×720, 15 人 (合成クリップ) | 768×576, 常時 3〜6 人 (vtest.avi) | vtest で拾えた人数/フレーム |
|---|---|---|---|
| rtmo-t | 30 ms | - | - |
| rtmo-s | 64 ms | 65〜95 ms | 2.8 |
| rtmo-l | - | 455 ms | 2.8 (大きくしても増えない) |
| yolo-n (pose) | 38 ms | 35 ms | 1.0 |
| yolo-m (pose) | - | 225 ms | 0.8 (同上) |
| rtmpose-s | 220 ms | 84 ms | 5.1 |
| rtmpose-m | 1000 ms (50 人検出時) | 250〜380 ms | 5.6 |
| mediapipe full (ROI 込み) | 20 ms | 23 ms | 1 (設計上) |
| holistic (ROI 込み) | 28 ms | 24 ms | 1 (設計上) |
| ball yolo-n | 28 ms | - | - |

top-down (rtmpose) は人数に比例するので、観客まで拾うと極端に遅くなる。コートで数人なら 100〜150 ms 程度の見込み。

## 分かっていること・次にやること

- COCO 学習済みの `sports ball` は、**手に持ったボール**や 15px 程度の小さいボールはほぼ拾えない（合成した飛翔中のボールは 50% 検出 + 外挿で追跡できた）。実データが録れたら数百枚アノテーションしてバスケットボール専用にファインチューンする（同じ ONNX 形式で `--ball` に差し替え可能）
- 小さく写る人（60px 程度）は RTMO / YOLO-pose (640 入力の one-stage) が苦手で、vtest.avi では常時 3〜6 人いるのに 1〜2.8 人しか拾えない。
  **モデルを大きくしても (rtmo-l, yolo-m) 増えない** = 入力 640 への縮小で人が 50px になるのが原因。
  rtmpose は 5〜6 人/フレーム拾う（検出器 + 人ごとに 256×192 で見るため）。rtmpose-s は m の 1/3 の時間で recall はほぼ同じ。
  コートでは人が大きく写るので差は縮むはずだが、精度重視なら rtmpose
- rtmlib の YOLOX (NMS 内蔵 ONNX) は `score_thr` を無視して 0.3 固定になるため、`rtmpose` バックエンドでは生出力から自前でしきい値を掛けている (`--det-thr` が効く。人物 score は検出スコア)
- シューターの特定（ボールを持っている人 / シュート位置にいる人）とショット区間の切り出しは、実データを見てから設計する
- サンプル動画（`data/samples/`, git 管理外）: OpenCV の `vtest.avi`、Wikimedia Commons の写真（CC BY-SA 4.0, Michael Barera）から作った `freethrow*.mp4`
