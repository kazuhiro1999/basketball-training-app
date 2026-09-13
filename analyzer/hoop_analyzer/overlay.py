"""骨格・ボール・ID の描画と、確認用オーバーレイ動画の書き出し。"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .types import COCO17_SKELETON, Ball, Person

_PALETTE = [
    (255, 80, 80), (80, 200, 255), (80, 255, 120), (255, 200, 60), (220, 100, 255),
    (255, 140, 0), (0, 220, 220), (180, 180, 255), (255, 255, 120), (120, 255, 200),
]


def color_for(track_id: int | None) -> tuple[int, int, int]:
    if track_id is None:
        return (160, 160, 160)
    return _PALETTE[(track_id - 1) % len(_PALETTE)]


def draw_person(img: np.ndarray, p: Person, kp_thr: float = 0.3, thickness: int = 2) -> None:
    col = color_for(p.track_id)
    kp, sc = p.keypoints, p.kp_scores
    for a, b in COCO17_SKELETON:
        if sc[a] > kp_thr and sc[b] > kp_thr:
            cv2.line(img, (int(kp[a, 0]), int(kp[a, 1])), (int(kp[b, 0]), int(kp[b, 1])), col, thickness, cv2.LINE_AA)
    for i in range(len(kp)):
        if sc[i] > kp_thr:
            cv2.circle(img, (int(kp[i, 0]), int(kp[i, 1])), thickness + 1, col, -1, cv2.LINE_AA)
    x1, y1, x2, y2 = p.bbox.astype(int)
    cv2.rectangle(img, (x1, y1), (x2, y2), col, 1)
    label = f"#{p.track_id}" if p.track_id is not None else "?"
    cv2.putText(img, f"{label} {p.score:.2f}", (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)


def draw_ball(img: np.ndarray, b: Ball) -> None:
    cx, cy = b.center
    col = (0, 165, 255) if not b.predicted else (0, 90, 200)
    cv2.circle(img, (int(cx), int(cy)), max(4, int(b.radius)), col, 2 if not b.predicted else 1, cv2.LINE_AA)
    cv2.putText(img, f"ball {b.score:.2f}" if not b.predicted else "ball (pred)", (int(cx) + 6, int(cy) - 6),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)


def draw_frame(img: np.ndarray, persons: list[Person], ball: Ball | None, text: str = "", kp_thr: float = 0.3) -> np.ndarray:
    for p in persons:
        draw_person(img, p, kp_thr)
    if ball is not None:
        draw_ball(img, ball)
    if text:
        cv2.putText(img, text, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, text, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
    return img


class OverlayWriter:
    """確認用動画。PyAV (libx264, yuv420p) で書くのでブラウザや Slack でもそのまま再生できる。"""

    def __init__(self, path: str | Path, fps: float, size: tuple[int, int], crf: int = 23) -> None:
        import av

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        w, h = size
        self.container = av.open(str(self.path), mode="w")
        self.stream = self.container.add_stream("libx264", rate=max(1, int(round(fps))))
        self.stream.width, self.stream.height = w - w % 2, h - h % 2     # yuv420p は偶数サイズ
        self.stream.pix_fmt = "yuv420p"
        self.stream.options = {"crf": str(crf), "preset": "veryfast"}
        self._size = (self.stream.width, self.stream.height)

    def write(self, img: np.ndarray) -> None:
        import av

        if (img.shape[1], img.shape[0]) != self._size:
            img = img[: self._size[1], : self._size[0]]
        frame = av.VideoFrame.from_ndarray(img, format="bgr24")
        for pkt in self.stream.encode(frame):
            self.container.mux(pkt)

    def close(self) -> None:
        for pkt in self.stream.encode():
            self.container.mux(pkt)
        self.container.close()
