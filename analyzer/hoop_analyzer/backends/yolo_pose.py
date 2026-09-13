"""YOLO-pose (Ultralytics YOLO11-pose): 1 モデルで人検出 + 17 点。onnxruntime のみで実行。

事前に analyzer/ で `uv run tools/export_yolo.py` を実行して models/yolo11{n,s,m}-pose.onnx を作る。
"""
from __future__ import annotations

import numpy as np

from ..types import Person
from ..yolo_onnx import MODELS_DIR, YoloOnnx, cxcywh_to_xyxy, nms_xyxy, unletterbox_xyxy
from .base import PoseBackend


class YoloPoseBackend(PoseBackend):
    name = "yolo"
    sizes = ("n", "s", "m")
    default_size = "n"

    def __init__(self, size: str | None = None, det_thr: float = 0.5, kp_thr: float = 0.3,
                 iou_thr: float = 0.5, model_path: str | None = None) -> None:
        super().__init__(size, det_thr, kp_thr)
        self.iou_thr = iou_thr
        self.model_path = model_path or MODELS_DIR / f"yolo11{self.size}-pose.onnx"
        self.model = YoloOnnx(self.model_path)

    def infer(self, frame_bgr: np.ndarray, ts_ms: float | None = None) -> list[Person]:
        pred, r, pad = self.model.run(frame_bgr)          # (N, 56)
        conf = pred[:, 4]
        m = conf > self.det_thr
        if not m.any():
            return []
        pred, conf = pred[m], conf[m]
        boxes = cxcywh_to_xyxy(pred[:, :4])
        keep = nms_xyxy(boxes, conf, self.det_thr, self.iou_thr)
        persons = []
        for i in keep:
            kp = pred[i, 5:].reshape(17, 3)
            xy = kp[:, :2].copy()
            xy[:, 0] = (xy[:, 0] - pad[0]) / r
            xy[:, 1] = (xy[:, 1] - pad[1]) / r
            persons.append(Person(
                bbox=unletterbox_xyxy(boxes[i:i + 1], r, pad)[0].astype(np.float32),
                score=float(conf[i]),
                keypoints=xy.astype(np.float32),
                kp_scores=kp[:, 2].astype(np.float32),
            ))
        return persons

    def describe(self) -> dict:
        d = super().describe()
        d.update({"model": self.model_path.name if hasattr(self.model_path, "name") else str(self.model_path),
                  "input": list(self.model.input_size), "iou_thr": self.iou_thr})
        return d
