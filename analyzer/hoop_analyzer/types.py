"""共通のデータ型。全バックエンドは COCO-17 キーポイント形式に揃える。"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# COCO-17 (RTMPose body7 / RTMO / YOLO-pose すべてこの順)
COCO17_NAMES = [
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
]
COCO17_SKELETON = [
    (0, 1), (0, 2), (1, 3), (2, 4),            # 顔
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),   # 腕
    (5, 11), (6, 12), (11, 12),                # 胴
    (11, 13), (13, 15), (12, 14), (14, 16),    # 脚
]


@dataclass
class Person:
    bbox: np.ndarray                 # (4,) x1, y1, x2, y2  元画像のピクセル座標
    score: float                     # 人物としての信頼度 (検出スコア or キーポイント平均)
    keypoints: np.ndarray            # (17, 2) x, y  元画像のピクセル座標
    kp_scores: np.ndarray            # (17,)
    track_id: int | None = None
    extra: dict | None = None        # バックエンド固有の追加データ (MediaPipe の 33 点・3D・手など)

    def to_json(self, ndigits: int = 1) -> dict:
        kp = np.concatenate([self.keypoints, self.kp_scores[:, None]], axis=1)
        d = {
            "id": self.track_id,
            "bbox": [round(float(v), ndigits) for v in self.bbox],
            "score": round(float(self.score), 3),
            "kp": [[round(float(x), ndigits), round(float(y), ndigits), round(float(c), 3)] for x, y, c in kp],
        }
        if self.extra:
            d.update(self.extra)
        return d


@dataclass
class Ball:
    bbox: np.ndarray                 # (4,) x1, y1, x2, y2
    score: float
    track_id: int | None = None
    predicted: bool = False          # 検出できず速度で外挿したフレーム

    @property
    def center(self) -> tuple[float, float]:
        return float((self.bbox[0] + self.bbox[2]) / 2), float((self.bbox[1] + self.bbox[3]) / 2)

    @property
    def radius(self) -> float:
        return float(max(self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1]) / 2)

    def to_json(self, ndigits: int = 1) -> dict:
        cx, cy = self.center
        return {
            "id": self.track_id,
            "bbox": [round(float(v), ndigits) for v in self.bbox],
            "center": [round(cx, ndigits), round(cy, ndigits)],
            "score": round(float(self.score), 3),
            "predicted": self.predicted,
        }


@dataclass
class FrameResult:
    index: int                       # 入力全体での通し番号 (0 始まり)
    seg: int                         # 録画のセグメント番号 (動画ファイルなら 1)
    n: int                           # セグメント内のフレーム番号 (1 始まり, frames.csv と同じ)
    ts_us: int                       # フレームのタイムスタンプ (µs)
    persons: list[Person] = field(default_factory=list)
    ball: Ball | None = None
    timing_ms: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "i": self.index, "seg": self.seg, "n": self.n, "ts_us": self.ts_us,
            "persons": [p.to_json() for p in self.persons],
            "ball": self.ball.to_json() if self.ball else None,
        }
