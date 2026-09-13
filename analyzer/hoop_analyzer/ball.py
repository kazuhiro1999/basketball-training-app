"""ボール検出 (YOLO11 COCO 'sports ball' クラス) と簡易トラッキング。

まずは COCO 学習済みモデルで試す。精度が足りなければ録画からアノテーションして
バスケットボール専用にファインチューンし、同じ ONNX 形式で差し替える。
"""
from __future__ import annotations

import numpy as np

from .types import Ball
from .yolo_onnx import MODELS_DIR, YoloOnnx, cxcywh_to_xyxy, nms_xyxy, unletterbox_xyxy

COCO_SPORTS_BALL = 32


class BallDetector:
    def __init__(self, size: str = "n", conf_thr: float = 0.25, iou_thr: float = 0.5,
                 class_id: int = COCO_SPORTS_BALL, model_path: str | None = None) -> None:
        self.size = size
        self.conf_thr, self.iou_thr, self.class_id = conf_thr, iou_thr, class_id
        self.model_path = model_path or MODELS_DIR / f"yolo11{size}.onnx"
        self.model = YoloOnnx(self.model_path)

    def infer(self, frame_bgr: np.ndarray) -> list[Ball]:
        pred, r, pad = self.model.run(frame_bgr)          # (N, 84)
        if self.class_id is None:                          # 専用モデル (1 クラス) の場合
            scores = pred[:, 4:].max(axis=1)
        else:
            scores = pred[:, 4 + self.class_id]
        m = scores > self.conf_thr
        if not m.any():
            return []
        boxes = cxcywh_to_xyxy(pred[m, :4])
        scores = scores[m]
        keep = nms_xyxy(boxes, scores, self.conf_thr, self.iou_thr)
        boxes = unletterbox_xyxy(boxes[keep], r, pad)
        balls = [Ball(bbox=b.astype(np.float32), score=float(s)) for b, s in zip(boxes, scores[keep])]
        balls.sort(key=lambda b: -b.score)
        return balls

    def describe(self) -> dict:
        return {"detector": "yolo", "size": self.size, "model": getattr(self.model_path, "name", str(self.model_path)),
                "class_id": self.class_id, "conf_thr": self.conf_thr}


def roi_around_persons(frame_shape: tuple[int, ...], persons, min_size: int = 640, expand: float = 1.3,
                       top_extra: float = 0.6) -> tuple[int, int, int, int] | None:
    """人物 bbox の和集合を広げた矩形 (x1, y1, x2, y2)。ボールは手元か頭上の弧にあるので上方向を多めに取る。
    フレーム全体を 640 に縮めるとボールが 10px 程度になって検出できないため、この ROI で実効解像度を上げる。"""
    if not persons:
        return None
    h, w = frame_shape[:2]
    bb = np.array([p.bbox for p in persons], dtype=np.float32)
    x1, y1, x2, y2 = bb[:, 0].min(), bb[:, 1].min(), bb[:, 2].max(), bb[:, 3].max()
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    bw, bh = (x2 - x1) * expand, (y2 - y1) * expand
    y1n = cy - bh / 2 - (y2 - y1) * top_extra
    side = max(bw, bh + (y2 - y1) * top_extra, float(min_size))
    rx1 = int(max(0, min(cx - side / 2, w - side)))
    ry1 = int(max(0, min(y1n, h - side)))
    rx2, ry2 = int(min(w, rx1 + side)), int(min(h, ry1 + side))
    if rx2 - rx1 >= w - 2 and ry2 - ry1 >= h - 2:
        return None                                   # ほぼ全画面なら ROI の意味がない
    return rx1, ry1, rx2, ry2


def detect_ball(det: "BallDetector", frame_bgr: np.ndarray, persons=None, use_roi: bool = True) -> list[Ball]:
    """ROI があればその中で検出し、座標をフレーム座標へ戻す。"""
    roi = roi_around_persons(frame_bgr.shape, persons) if (use_roi and persons) else None
    if roi is None:
        return det.infer(frame_bgr)
    x1, y1, x2, y2 = roi
    balls = det.infer(frame_bgr[y1:y2, x1:x2])
    for b in balls:
        b.bbox = b.bbox + np.array([x1, y1, x1, y1], dtype=np.float32)
    return balls


class BallTracker:
    """1 個のボールを追う。前フレームの位置+速度から予測し、最も近い検出を採用。
    見失ったら max_lost フレームまで等速で外挿 (predicted=True)。"""

    def __init__(self, max_lost: int = 10, gate_px: float = 150.0, min_score: float = 0.0) -> None:
        self.max_lost, self.gate_px, self.min_score = max_lost, gate_px, min_score
        self.pos: np.ndarray | None = None      # 中心 (x, y)
        self.vel = np.zeros(2, dtype=np.float32)
        self.size = 0.0                         # bbox の一辺
        self.lost = 0
        self.next_id = 1
        self.track_id: int | None = None

    def update(self, dets: list[Ball]) -> Ball | None:
        dets = [d for d in dets if d.score >= self.min_score]
        pred = None if self.pos is None else self.pos + self.vel
        chosen = None
        if dets:
            if pred is None:
                chosen = dets[0]                                    # スコア順で先頭
            else:
                dist = [np.hypot(*(np.array(d.center) - pred)) for d in dets]
                j = int(np.argmin(dist))
                chosen = dets[j] if dist[j] <= self.gate_px * (1 + 0.5 * self.lost) else None
                if chosen is None and self.lost >= self.max_lost:
                    chosen = dets[0]                                # 追跡を諦めて取り直し
                    self.track_id = None
        if chosen is not None:
            c = np.array(chosen.center, dtype=np.float32)
            if self.pos is not None and self.lost == 0:
                self.vel = 0.6 * self.vel + 0.4 * (c - self.pos)
            elif self.pos is not None:
                self.vel = (c - self.pos) / (self.lost + 1)
            self.pos, self.lost = c, 0
            self.size = chosen.radius * 2
            if self.track_id is None:
                self.track_id = self.next_id
                self.next_id += 1
            chosen.track_id = self.track_id
            return chosen
        # 検出なし: 外挿
        if self.pos is None or self.lost >= self.max_lost:
            self.pos = None
            self.track_id = None
            return None
        self.lost += 1
        self.pos = self.pos + self.vel
        h = self.size / 2
        return Ball(bbox=np.array([self.pos[0] - h, self.pos[1] - h, self.pos[0] + h, self.pos[1] + h], dtype=np.float32),
                    score=0.0, track_id=self.track_id, predicted=True)
