"""入力の読み込み。delaycam の録画フォルダ (meta.json + segNN.h264 + frames.csv) と、
普通の動画ファイル (mp4/avi/…) の両方を同じインタフェースで扱う。"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np


@dataclass
class Frame:
    index: int          # 通し番号 (0 始まり)
    seg: int
    n: int              # セグメント内 1 始まり
    ts_us: int
    image: np.ndarray   # BGR


class FrameSource:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.is_recording = (self.path / "meta.json").exists()
        self.meta: dict = {}
        self.width = self.height = 0
        self.fps = 30.0
        self.total: int | None = None
        self.name = self.path.name if self.is_recording else self.path.stem
        if self.is_recording:
            self.meta = json.loads((self.path / "meta.json").read_text(encoding="utf-8"))
            segs = self.meta.get("segments", [])
            if segs:
                self.width, self.height = int(segs[0].get("width") or 0), int(segs[0].get("height") or 0)
                self.fps = float(segs[0].get("fps") or 30)
            self.total = int(self.meta.get("frames_total") or 0) or None
        else:
            import av
            with av.open(str(self.path)) as c:
                s = c.streams.video[0]
                self.width, self.height = s.codec_context.width, s.codec_context.height
                self.fps = float(s.average_rate or s.guessed_rate or 30)
                self.total = s.frames or None

    def frames(self) -> Iterator[Frame]:
        return self._rec_frames() if self.is_recording else self._file_frames()

    # ---- 録画フォルダ

    def _load_frames_csv(self) -> dict[int, list[dict]]:
        per_seg: dict[int, list[dict]] = {}
        p = self.path / "frames.csv"
        if not p.exists():
            return per_seg
        with open(p, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                per_seg.setdefault(int(row["seg"]), []).append(row)
        return per_seg

    def _rec_frames(self) -> Iterator[Frame]:
        import av

        rows_by_seg = self._load_frames_csv()
        index = 0
        for seg in self.meta.get("segments", []):
            rows = rows_by_seg.get(seg["seg"], [])
            fps = float(seg.get("fps") or 30)
            f = self.path / seg["file"]
            if not f.exists():
                continue
            n = 0
            with av.open(str(f)) as c:
                for frame in c.decode(video=0):
                    n += 1
                    if n <= len(rows):
                        ts = int(rows[n - 1]["ts_us"])
                    else:                                   # frames.csv と食い違ったら等間隔で補う
                        ts = int((n - 1) * 1e6 / fps)
                    yield Frame(index, int(seg["seg"]), n, ts, frame.to_ndarray(format="bgr24"))
                    index += 1
            if rows and n != len(rows):
                print(f"[warn] seg{seg['seg']:02d}: 復号 {n} フレーム / frames.csv {len(rows)} 行")

    # ---- 動画ファイル

    def _file_frames(self) -> Iterator[Frame]:
        import av

        with av.open(str(self.path)) as c:
            s = c.streams.video[0]
            tb = float(s.time_base) if s.time_base else None
            for i, frame in enumerate(c.decode(s)):
                if frame.pts is not None and tb:
                    ts = int(frame.pts * tb * 1e6)
                else:
                    ts = int(i * 1e6 / self.fps)
                yield Frame(i, 1, i + 1, ts, frame.to_ndarray(format="bgr24"))
