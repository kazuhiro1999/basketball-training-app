"""バックエンドの速度比較 (CPU)。ライブ表示に載せる軽量モデルを選ぶための目安。

  uv run bench sample.mp4                       # rtmo-t/s, rtmpose-s/m, yolo-n を 100 フレームずつ
  uv run bench sample.mp4 --configs rtmo:t rtmo:s yolo:n --frames 200
"""
from __future__ import annotations

import argparse
import sys
import time

from .backends import create_pose_backend
from .ort_config import default_threads, set_threads
from .video import FrameSource

DEFAULT_CONFIGS = ["rtmo:t", "rtmo:s", "rtmpose:s", "rtmpose:m", "yolo:n"]


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("input")
    p.add_argument("--configs", nargs="*", default=DEFAULT_CONFIGS, help="backend:size ...")
    p.add_argument("--frames", type=int, default=100)
    p.add_argument("--det-thr", type=float, default=0.5)
    p.add_argument("--threads", type=int, default=0)
    args = p.parse_args(argv)
    set_threads(args.threads or None)

    src = FrameSource(args.input)
    frames = []
    for fr in src.frames():
        frames.append(fr.image)
        if len(frames) >= args.frames:
            break
    print(f"入力 {src.width}x{src.height}  {len(frames)} フレーム\n")
    print(f"{'backend':<12}{'load(s)':>9}{'ms/frame':>10}{'fps':>8}{'persons':>9}   model")
    for cfg in args.configs:
        name, _, size = cfg.partition(":")
        try:
            t0 = time.perf_counter()
            be = create_pose_backend(name, size=size or None, det_thr=args.det_thr)
            be.warmup((src.height, src.width))
            load = time.perf_counter() - t0
            t1 = time.perf_counter()
            n_p = 0
            for img in frames:
                n_p += len(be.infer(img))
            el = time.perf_counter() - t1
            d = be.describe()
            model = d.get("model") or f"{d.get('det_model')} + {d.get('pose_model')}"
            print(f"{be.name + '-' + be.size:<12}{load:>9.1f}{el / len(frames) * 1000:>10.1f}{len(frames) / el:>8.1f}"
                  f"{n_p / len(frames):>9.2f}   {model}")
        except Exception as e:  # noqa: BLE001
            print(f"{cfg:<12}  失敗: {e}")


if __name__ == "__main__":
    main()
