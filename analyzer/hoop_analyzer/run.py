"""録画 (または動画ファイル) に骨格推定・ボール検出・トラッキングを掛けて pose.jsonl を書く。

  uv run analyze ../delaycam/recordings/2026-09-13_17-40-12 --pose rtmo --size s --ball yolo --overlay
  uv run analyze sample.mp4 --pose rtmpose --size m --tracker bytetrack --max-frames 300

出力 (既定: <入力>/analysis/<pose>-<size>_<tracker>/ 、動画ファイルなら <動画名>_analysis/<pose>-<size>_<tracker>/):
  pose.jsonl          1 行 1 フレーム: {"i","seg","n","ts_us","persons":[{id,bbox,score,kp:[[x,y,c]x17]}],"ball":{...}|null}
  analysis_meta.json  使ったモデル・パラメータ・処理時間
  overlay.mp4         --overlay 指定時
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

from . import __version__
from .backends import REGISTRY, create_pose_backend
from .ball import BallDetector, BallTracker, detect_ball
from .ort_config import default_threads, set_threads
from .overlay import OverlayWriter, draw_frame
from .tracking import create_tracker
from .types import FrameResult
from .video import FrameSource


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="骨格推定 + ボール検出 + トラッキング")
    p.add_argument("input", help="録画フォルダ (meta.json のある場所) または動画ファイル")
    p.add_argument("--pose", default="rtmo", choices=[*REGISTRY, "none"], help="姿勢推定バックエンド (既定 rtmo)")
    p.add_argument("--size", default=None, help="モデルサイズ (rtmpose: s/m/x, rtmo: t/s/m/l, yolo: n/s/m)")
    p.add_argument("--det-thr", type=float, default=0.5, help="人物検出のしきい値")
    p.add_argument("--kp-thr", type=float, default=0.3, help="キーポイント信頼度のしきい値 (描画/bbox 用)")
    p.add_argument("--ball", default="yolo", choices=["yolo", "none"], help="ボール検出 (既定 yolo)")
    p.add_argument("--ball-size", default="n", help="ボール検出 YOLO のサイズ n/s/m")
    p.add_argument("--ball-thr", type=float, default=0.25)
    p.add_argument("--ball-roi", default="persons", choices=["persons", "none"],
                   help="persons: 人物の周囲を切り出して検出 (小さいボール向け, 既定) / none: 全画面")
    p.add_argument("--tracker", default="simple", choices=["simple", "bytetrack", "ocsort", "sort", "none"],
                   help="人物の ID 付け (bytetrack/ocsort/sort は uv sync --extra trackers)")
    p.add_argument("--out", default=None, help="出力フォルダ")
    p.add_argument("--overlay", action="store_true", help="確認用の overlay.mp4 を書く")
    p.add_argument("--start", type=int, default=0, help="このフレーム番号から処理")
    p.add_argument("--max-frames", type=int, default=0, help="処理するフレーム数の上限 (0=全部)")
    p.add_argument("--stride", type=int, default=1, help="k フレームごとに処理 (動作確認用)")
    p.add_argument("--threads", type=int, default=0, help=f"ONNX Runtime のスレッド数/セッション (0=自動: {default_threads()})")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)

    set_threads(args.threads or None)
    src = FrameSource(args.input)
    tag = (f"{args.pose}-{args.size}" if args.size else args.pose) + f"_{args.tracker}"
    if args.out:
        out_dir = Path(args.out)
    elif src.is_recording:
        out_dir = src.path / "analysis" / tag
    else:
        out_dir = src.path.parent / f"{src.path.stem}_analysis" / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    pose = None if args.pose == "none" else create_pose_backend(args.pose, size=args.size, det_thr=args.det_thr, kp_thr=args.kp_thr)
    if pose:
        tag = f"{pose.name}-{pose.size}_{args.tracker}"
        if not args.out:
            out_dir = out_dir.parent / tag
            out_dir.mkdir(parents=True, exist_ok=True)
    ball_det = BallDetector(size=args.ball_size, conf_thr=args.ball_thr) if args.ball != "none" else None
    ball_trk = BallTracker() if ball_det else None
    tracker = create_tracker(args.tracker, fps=src.fps)
    if pose and src.width:
        pose.warmup((src.height, src.width))
    load_sec = time.perf_counter() - t0

    meta = {
        "analyzer_version": __version__,
        "input": str(src.path), "input_type": "recording" if src.is_recording else "video",
        "width": src.width, "height": src.height, "fps": src.fps,
        "pose": pose.describe() if pose else None,
        "ball": {**ball_det.describe(), "roi": args.ball_roi} if ball_det else None,
        "tracker": tracker.name,
        "ort_threads": args.threads or default_threads(),
        "keypoint_format": "coco17",
        "args": vars(args),
        "platform": platform.platform(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if not args.quiet:
        print(f"入力: {src.path}  {src.width}x{src.height} @ {src.fps:.1f}fps  {src.total or '?'} フレーム")
        print(f"姿勢: {meta['pose']}\nボール: {meta['ball']}\nトラッカー: {tracker.name}  (読み込み {load_sec:.1f}s)")
        print(f"出力: {out_dir}")

    writer = None
    timing = {"pose": 0.0, "ball": 0.0, "track": 0.0, "overlay": 0.0, "decode": 0.0}
    n_done = 0
    n_persons = 0
    n_ball = 0
    t_start = time.perf_counter()
    t_prev = t_start
    with open(out_dir / "pose.jsonl", "w", encoding="utf-8") as fout:
        for fr in src.frames():
            t_dec = time.perf_counter()
            timing["decode"] += t_dec - t_prev
            if fr.index < args.start or (fr.index - args.start) % args.stride != 0:
                t_prev = time.perf_counter()
                continue
            res = FrameResult(index=fr.index, seg=fr.seg, n=fr.n, ts_us=fr.ts_us)

            t1 = time.perf_counter()
            persons = pose.infer(fr.image) if pose else []
            t2 = time.perf_counter()
            res.persons = tracker.update(persons, fr.image)
            t3 = time.perf_counter()
            if ball_det:
                res.ball = ball_trk.update(detect_ball(ball_det, fr.image, res.persons, args.ball_roi == "persons"))
            t4 = time.perf_counter()
            timing["pose"] += t2 - t1
            timing["track"] += t3 - t2
            timing["ball"] += t4 - t3

            fout.write(json.dumps(res.to_json(), ensure_ascii=False) + "\n")
            if args.overlay:
                if writer is None:
                    writer = OverlayWriter(out_dir / "overlay.mp4", src.fps / args.stride, (fr.image.shape[1], fr.image.shape[0]))
                text = f"{tag}  seg{fr.seg} #{fr.n}  t={fr.ts_us / 1e6:.2f}s  persons={len(res.persons)}"
                writer.write(draw_frame(fr.image, res.persons, res.ball, text, args.kp_thr))
                timing["overlay"] += time.perf_counter() - t4

            n_done += 1
            n_persons += len(res.persons)
            n_ball += 1 if (res.ball and not res.ball.predicted) else 0
            if not args.quiet and n_done % 100 == 0:
                el = time.perf_counter() - t_start
                print(f"  {n_done} フレーム  {n_done / el:.1f} fps  人物平均 {n_persons / n_done:.2f}  ボール検出率 {n_ball / n_done:.0%}")
            if args.max_frames and n_done >= args.max_frames:
                break
            t_prev = time.perf_counter()
    if writer:
        writer.close()

    elapsed = time.perf_counter() - t_start
    meta.update({
        "frames": n_done,
        "elapsed_sec": round(elapsed, 2),
        "fps_processed": round(n_done / elapsed, 2) if elapsed else None,
        "avg_ms": {k: round(v / max(n_done, 1) * 1000, 2) for k, v in timing.items()},
        "avg_persons": round(n_persons / max(n_done, 1), 3),
        "ball_detect_rate": round(n_ball / max(n_done, 1), 3),
        "outputs": {"pose": "pose.jsonl", "overlay": "overlay.mp4" if writer else None},
    })
    (out_dir / "analysis_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        ms = meta["avg_ms"]
        print(f"完了: {n_done} フレーム {elapsed:.1f}s ({meta['fps_processed']} fps)  "
              f"姿勢 {ms['pose']}ms  ボール {ms['ball']}ms  追跡 {ms['track']}ms  復号 {ms['decode']}ms / フレーム")
        print(f"→ {out_dir / 'pose.jsonl'}")


if __name__ == "__main__":
    main()
