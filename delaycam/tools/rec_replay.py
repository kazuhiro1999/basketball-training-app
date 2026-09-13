# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp>=3.9"]
# ///
"""録画を「偽のスマホ」としてサーバに流す。コートに行かずに表示側・解析側を開発するためのツール。

  uv run tools/rec_replay.py recordings/2026-09-13_17-40-12
  uv run tools/rec_replay.py recordings/2026-09-13_17-40-12 --loop --speed 2
  uv run tools/rec_replay.py <dir> --url wss://192.168.0.3:8443/ws/cam   (別PCのサーバへ)

frames.csv の offset/size で各フレームを切り出し、送信時と同じ 16 バイトヘッダを付けて
元のタイムスタンプ間隔で送る。サーバ側は本物のスマホと区別しない (config に replay:true が付くだけ)。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import ssl
import struct
import time
from pathlib import Path

import aiohttp

CHUNK_HDR = struct.Struct("<BBHdI")


def load_frames(rec: Path) -> dict[int, list[dict]]:
    per_seg: dict[int, list[dict]] = {}
    with open(rec / "frames.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            per_seg.setdefault(int(row["seg"]), []).append(
                {"ts": int(row["ts_us"]), "key": int(row["key"]), "off": int(row["offset"]), "size": int(row["size"])}
            )
    return per_seg


async def replay(args: argparse.Namespace) -> None:
    rec = Path(args.recording)
    meta = json.loads((rec / "meta.json").read_text(encoding="utf-8"))
    frames = load_frames(rec)
    ssl_ctx = None
    if args.url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE   # 自己署名証明書

    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(args.url, ssl=ssl_ctx, max_msg_size=0) as ws:
            print(f"接続: {args.url}")
            while True:
                for seg in meta["segments"]:
                    rows = frames.get(seg["seg"], [])
                    if not rows:
                        continue
                    cfg = {
                        "type": "config", "session": f"replay{int(time.time()) % 100000}_{seg['seg']}",
                        "codec": seg["codec"], "width": seg["width"], "height": seg["height"],
                        "fps": seg["fps"], "bitrate": seg["bitrate"], "test": seg.get("test", False),
                        "replay": True, "replay_of": meta["recording_id"], "replay_seg": seg["seg"],
                        **{k: v for k, v in (seg.get("phone") or {}).items() if v is not None},
                    }
                    await ws.send_str(json.dumps(cfg))
                    print(f"seg{seg['seg']:02d}: {seg['codec']} {seg['width']}x{seg['height']} {len(rows)} フレーム")
                    t_wall0 = time.perf_counter()
                    ts0 = rows[0]["ts"]
                    with open(rec / seg["file"], "rb") as f:
                        for i, r in enumerate(rows):
                            f.seek(r["off"])
                            payload = f.read(r["size"])
                            hdr = CHUNK_HDR.pack(1, r["key"], 0, float(r["ts"]), 0)
                            due = t_wall0 + (r["ts"] - ts0) / 1e6 / args.speed
                            wait = due - time.perf_counter()
                            if wait > 0:
                                await asyncio.sleep(wait)
                            await ws.send_bytes(hdr + payload)
                            if i % 300 == 0 and i:
                                print(f"  {i}/{len(rows)}  {(r['ts'] - ts0) / 1e6:.0f}s", flush=True)
                if not args.loop:
                    break
                print("ループ")
            print("送信終了")


def main() -> None:
    p = argparse.ArgumentParser(description="録画をサーバへ再送する")
    p.add_argument("recording", help="録画フォルダ (meta.json があるところ)")
    p.add_argument("--url", default="ws://localhost:8080/ws/cam")
    p.add_argument("--speed", type=float, default=1.0, help="再生速度 (2 で倍速)")
    p.add_argument("--loop", action="store_true", help="繰り返す")
    args = p.parse_args()
    try:
        asyncio.run(replay(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
