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

## ライブ（遅延再生への骨格表示）

```bash
uv run live                                  # ws://localhost:8080/ws/analyzer に接続。標準 (rtmpose-s), 2 フレームに 1 回, 自動調整
uv run live --preset light                   # 軽い (rtmpose-s, 最大 2 人)
uv run live --preset heavy --stride 1        # 重い (rtmpose-m), 毎フレーム
uv run live --pose rtmo --size t --stride 1  # バックエンド直接指定
```

**通常は delaycam の表示画面（「骨格」→「骨格推定を起動」）から起動する**ので、このコマンドを直接叩く必要はない。
`start_live.bat` は `uv run live --stride 2 --auto-stride` を実行する（uv が無ければ入れる）。
delaycam 側を `start_with_pose.bat` で起動すると、サーバ起動と同時に骨格推定も立ち上がる。

- delaycam サーバに **viewer と同じ形で映像を購読**し、復号 → `stride` フレームに 1 回だけ姿勢推定 + トラッカー →
  `{"type":"pose", session, ts_us, persons}` を送り返す。表示側は届いた結果を表示フレームに合わせて重ね、推論の間は補間する
- 遅延再生なので推論の遅れは問題にならない。追いつかない（待ちフレームが溜まる）ときは `--auto-stride` で間隔を自動で広げる
- 表示側の「骨格」カードから推論間隔とプリセット（light / medium / heavy）を切り替えられる（`analyzer_cmd` を受け取って再設定）
- プリセット（いずれも rtmpose 系）と、ノート PC（i7-14650HX, 768×576, 4〜6 人）での**定常**実測:

  | プリセット | モデル | 最大人数 | 検出間隔 | 1 回あたり | 毎秒 |
  |---|---|---|---|---|---|
  | 軽い `light` | rtmpose-s | 2 | 4 回に 1 回 | 96 ms | 約 10 回 |
  | 標準 `medium` | rtmpose-s | 6 | 3 回に 1 回 | 181 ms | 約 5.5 回 |
  | 重い `heavy` | rtmpose-m | 6 | 2 回に 1 回 | 588 ms | 約 1.7 回 |

- `max_persons` は大きく写る順に上位だけ姿勢推定して top-down の時間を抑える。
  `det_every` は**人検出を数回に 1 回だけ**行い、間のフレームは前回の骨格から作った枠を使い回す
  （人検出 110 ms に対し姿勢推定は 1 人 20 ms 程度なので、これだけで 1.7〜2 倍速くなる。
  新しく入ってきた人は次の検出で拾う）

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
  live.py              ライブアナライザ (delaycam に接続して pose を返す)
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

**計測の注意**: ノート PC の CPU は最初の数秒だけターボで回り、その後クロックが落ちる
（i7-14650HX で実測 3.7GHz → 2.0GHz、同じ処理が 2〜3 倍遅くなる）。
`uv run bench` は各構成を 30 秒回し、**開始直後**と**定常**（最後の 1/3）を分けて出す。実運用で意味があるのは定常の方。

定常の実測（i7-14650HX ノート、768×576、4〜6 人、ORT 8 スレッド）:

| backend | 開始直後 | 定常 | 定常 fps | 拾えた人数 |
|---|---|---|---|---|
| rtmpose-s (最大2人, 検出1/4) | 38 ms | 82 ms | 12.2 | 2.0 |
| rtmpose-s (最大2人) | 60 ms | 160 ms | 6.3 | 2.0 |
| rtmpose-s (最大6人, 検出1/3) | 66 ms | 181 ms | 5.5 | 4.7 |
| rtmpose-s (最大6人) | 105 ms | 241 ms | 4.2 | 4.6 |
| rtmpose-m (最大6人, 検出1/3) | 477 ms | 588 ms | 1.7 | 4.5 |
| rtmpose-m | 924 ms | 1043 ms | 1.0 | 4.1 |
| rtmo-t | 125 ms | 118 ms | 8.5 | 2.1 |
| rtmo-s | 308 ms | 298 ms | 3.4 | 2.1 |
| yolo-n | 142 ms | 165 ms | 6.1 | 0.8 |

rtmo 系はターボが切れてもほとんど落ちない（1 パスで軽い）が、小さく写る人を取りこぼす。

参考（古い計測、ターボ区間のみ・当てにならない）:

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
