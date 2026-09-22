# /// script
# requires-python = ">=3.10"
# dependencies = ["av>=12"]
# ///
"""普通の動画を delaycam の録画フォルダ形式に変換する (開発・テスト用)。

  uv run tools/video_to_recording.py clip.mp4 recordings/clip
  uv run tools/video_to_recording.py clip.mp4 recordings/clip --fps 30 --bitrate 4

H.264 (Annex B, キーフレーム 1 秒ごと, B フレーム無し) に再エンコードし、
seg01.h264 / frames.csv / meta.json を書く。できたフォルダは rec_replay.py で偽スマホとして流せるし、
analyzer にもそのまま渡せる。
"""
from __future__ import annotations

import argparse
import json
import time
from fractions import Fraction
from pathlib import Path

import av


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("out", help="作成する録画フォルダ")
    p.add_argument("--fps", type=float, default=0, help="出力 fps (0=元動画のまま)")
    p.add_argument("--bitrate", type=float, default=4, help="Mbps")
    p.add_argument("--max-frames", type=int, default=0)
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    src = av.open(args.video)
    vs = src.streams.video[0]
    fps = args.fps or float(vs.average_rate or 30)
    w, h = vs.codec_context.width, vs.codec_context.height
    w, h = w - w % 2, h - h % 2

    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = w, h, "yuv420p"
    enc.time_base = Fraction(1, 90000)
    enc.framerate = Fraction(int(round(fps)), 1)
    enc.bit_rate = int(args.bitrate * 1e6)
    enc.options = {"preset": "veryfast", "tune": "zerolatency", "profile": "baseline", "g": str(int(round(fps))),
                   "keyint_min": str(int(round(fps))), "bf": "0", "x264-params": "annexb=1:repeat-headers=1"}
    enc.open()

    session = "conv" + format(int(time.time()) % 100000, "05d")
    now_ms = int(time.time() * 1000)
    seg_path = out / "seg01.h264"
    n = 0
    first_ts = None
    with open(seg_path, "wb") as vf, open(out / "frames.csv", "w", encoding="utf-8", newline="") as cf:
        cf.write("seg,n,ts_us,key,offset,size,recv_unix_ms\n")

        def write_packets(packets):
            nonlocal n, first_ts
            for pkt in packets:
                n += 1
                ts_us = int(n * 1e6 / fps)
                first_ts = first_ts if first_ts is not None else ts_us
                data = bytes(pkt)
                off = vf.tell()
                vf.write(data)
                cf.write(f"1,{n},{ts_us},{1 if pkt.is_keyframe else 0},{off},{len(data)},{now_ms + int(n * 1000 / fps)}\n")

        i = 0
        for frame in src.decode(vs):
            if frame.width != w or frame.height != h:
                frame = frame.reformat(width=w, height=h, format="yuv420p")
            else:
                frame = frame.reformat(format="yuv420p")
            frame.pts = int(i * 90000 / fps)
            frame.time_base = Fraction(1, 90000)
            write_packets(enc.encode(frame))
            i += 1
            if args.max_frames and i >= args.max_frames:
                break
        write_packets(enc.encode(None))

    meta = {
        "recording_id": out.name, "format_version": 1, "app_version": "video_to_recording",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "started_unix_ms": now_ms,
        "ended_at": None, "ended_unix_ms": now_ms + int(n * 1000 / fps), "duration_sec": round(n / fps, 3),
        "preroll_sec": 0, "delay_sec_at_start": None,
        "setup": {"view": "other", "note": f"converted from {Path(args.video).name}"},
        "viewer": None, "pc": {}, "segments": [{
            "seg": 1, "file": "seg01.h264", "container": "annexb", "session": session, "codec": "avc1.42E01F",
            "width": w, "height": h, "fps": fps, "bitrate": int(args.bitrate * 1e6), "test": False,
            "phone": {"ua": "video_to_recording", "orientation": "landscape" if w >= h else "portrait", "camera": None, "settings": None},
            "started_unix_ms": now_ms, "first_ts_us": first_ts, "last_ts_us": int(n * 1e6 / fps), "frames": n,
            "bytes": seg_path.stat().st_size,
        }],
        "frames_total": n, "bytes_total": seg_path.stat().st_size,
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{n} フレーム {w}x{h}@{fps:g}fps → {out}")


if __name__ == "__main__":
    main()
