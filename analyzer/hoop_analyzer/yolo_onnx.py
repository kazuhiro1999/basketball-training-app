"""Ultralytics YOLO (v8/11) の ONNX を onnxruntime だけで動かす共通部品。

torch / ultralytics はランタイムに不要。ONNX は tools/export_yolo.py で一度だけ書き出す。
出力形式 (nms 無しエクスポート):
  detect : (1, 4+80, N)  cx, cy, w, h, class0..79
  pose   : (1, 4+1+17*3, N)  cx, cy, w, h, conf, (x, y, conf) x 17
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .ort_config import make_session

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"


class YoloOnnx:
    def __init__(self, model_path: str | Path) -> None:
        self.path = Path(model_path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"{self.path} がありません。analyzer/ で `uv run tools/export_yolo.py` を実行して ONNX を作ってください")
        self.sess = make_session(self.path)
        inp = self.sess.get_inputs()[0]
        self.input_name = inp.name
        h, w = inp.shape[2], inp.shape[3]
        self.input_size = (int(h) if isinstance(h, int) else 640, int(w) if isinstance(w, int) else 640)

    def letterbox(self, img: np.ndarray) -> tuple[np.ndarray, float, tuple[float, float]]:
        ih, iw = self.input_size
        h, w = img.shape[:2]
        r = min(ih / h, iw / w)
        nw, nh = int(round(w * r)), int(round(h * r))
        dw, dh = (iw - nw) / 2, (ih - nh) / 2
        resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR) if (nw, nh) != (w, h) else img
        top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
        left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
        padded = cv2.copyMakeBorder(resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114))
        return padded, r, (left, top)

    def run(self, img_bgr: np.ndarray) -> tuple[np.ndarray, float, tuple[float, float]]:
        """→ (pred (N, C), ratio, (pad_x, pad_y))"""
        padded, r, pad = self.letterbox(img_bgr)
        blob = cv2.dnn.blobFromImage(padded, scalefactor=1 / 255.0, swapRB=True)   # BGR→RGB, CHW, float32
        out = self.sess.run(None, {self.input_name: blob})[0]
        return out[0].T, r, pad


class YoloClassDetector:
    """COCO 検出モデル (yolo11n.onnx) の特定クラスだけを返す。→ [(xyxy, score), ...] スコア降順"""

    def __init__(self, class_id: int, size: str = "n", conf_thr: float = 0.3, iou_thr: float = 0.5) -> None:
        self.class_id, self.conf_thr, self.iou_thr = class_id, conf_thr, iou_thr
        self.model = YoloOnnx(MODELS_DIR / f"yolo11{size}.onnx")

    def detect(self, frame_bgr: np.ndarray) -> list[tuple[np.ndarray, float]]:
        pred, r, pad = self.model.run(frame_bgr)
        scores = pred[:, 4 + self.class_id]
        m = scores > self.conf_thr
        if not m.any():
            return []
        boxes = cxcywh_to_xyxy(pred[m, :4])
        scores = scores[m]
        keep = nms_xyxy(boxes, scores, self.conf_thr, self.iou_thr)
        boxes = unletterbox_xyxy(boxes[keep], r, pad)
        out = [(b.astype(np.float32), float(s)) for b, s in zip(boxes, scores[keep])]
        out.sort(key=lambda t: -t[1])
        return out


def nms_xyxy(boxes: np.ndarray, scores: np.ndarray, conf_thr: float, iou_thr: float) -> np.ndarray:
    if len(boxes) == 0:
        return np.zeros(0, dtype=int)
    xywh = np.column_stack([boxes[:, 0], boxes[:, 1], boxes[:, 2] - boxes[:, 0], boxes[:, 3] - boxes[:, 1]])
    keep = cv2.dnn.NMSBoxes(xywh.tolist(), scores.astype(float).tolist(), conf_thr, iou_thr)
    return np.asarray(keep, dtype=int).reshape(-1)


def cxcywh_to_xyxy(b: np.ndarray) -> np.ndarray:
    return np.column_stack([b[:, 0] - b[:, 2] / 2, b[:, 1] - b[:, 3] / 2, b[:, 0] + b[:, 2] / 2, b[:, 1] + b[:, 3] / 2])


def unletterbox_xyxy(boxes: np.ndarray, r: float, pad: tuple[float, float]) -> np.ndarray:
    out = boxes.copy()
    out[:, [0, 2]] = (out[:, [0, 2]] - pad[0]) / r
    out[:, [1, 3]] = (out[:, [1, 3]] - pad[1]) / r
    return out
