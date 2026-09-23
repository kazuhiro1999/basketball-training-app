"""バックエンドの速度比較 (CPU)。ライブ表示に載せる軽量モデルを選ぶための目安。

  uv run bench sample.mp4                       # 既定の 5 構成を 30 秒ずつ
  uv run bench sample.mp4 --configs rtmo:t yolo:n --seconds 60

ノート PC の CPU は最初の数秒だけターボで回り、その後クロックが落ちる (実測で 3.7GHz → 2.0GHz)。
短い計測だと 2〜3 倍速く見えるので、**最後の 1/3 の平均 (定常)** を本命の数字として出す。
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
    p.add_argument("--frames", type=int, default=100, help="読み込むフレーム数 (足りなければ先頭に戻って繰り返す)")
    p.add_argument("--seconds", type=float, default=30, help="1 構成あたりの計測時間 (既定 30 秒)")
    p.add_argument("--max-persons", type=int, default=0, help="rtmpose: 姿勢推定する人数の上限")
    p.add_argument("--det-every", type=int, default=1, help="rtmpose: 何回に 1 回 人検出をするか (間は前回の枠を使い回す)")
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
    print(f"入力 {src.width}x{src.height}  {len(frames)} フレーム  1 構成 {args.seconds:g} 秒  ORT {args.threads or default_threads()} スレッド")
    print()
    print(f"{'backend':<14}{'開始直後':>10}{'定常':>10}{'定常fps':>9}{'persons':>9}   model")
    for cfg in args.configs:
        name, _, size = cfg.partition(":")
        try:
            kw = {"det_thr": args.det_thr}
            if name == "rtmpose":
                if args.max_persons:
                    kw["max_persons"] = args.max_persons
                if args.det_every > 1:
                    kw["det_every"] = args.det_every
            be = create_pose_backend(name, size=size or None, **kw)
            be.warmup((src.height, src.width))
            times: list[float] = []
            n_p = 0
            end = time.perf_counter() + args.seconds
            i = 0
            while time.perf_counter() < end:
                img = frames[i % len(frames)]
                i += 1
                t0 = time.perf_counter()
                n_p += len(be.infer(img, ts_ms=i * 1000 / src.fps))
                times.append(time.perf_counter() - t0)
            k = max(1, len(times) // 3)
            first = sum(times[:k]) / k * 1000              # ターボが効いている区間
            last = sum(times[-k:]) / k * 1000              # クロックが落ちた後 = 現実的な値
            d = be.describe()
            model = d.get("model") or f"{d.get('det_model')} + {d.get('pose_model')}"
            print(f"{be.name + '-' + be.size:<14}{first:>9.0f}ms{last:>9.0f}ms{1000 / last:>9.1f}{n_p / len(times):>9.2f}   {model}")
        except Exception as e:  # noqa: BLE001
            print(f"{cfg:<14}  失敗: {e}")


if __name__ == "__main__":
    main()
