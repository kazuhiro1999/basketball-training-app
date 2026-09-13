"""RTMO (one-stage): 1 モデルで複数人の検出+姿勢。人数が増えても処理時間がほぼ一定。

rtmlib の RTMO クラス (ONNX Runtime, CPU)。モデルは初回に自動ダウンロード。
"""
from __future__ import annotations

import numpy as np

from ..ort_config import retune_rtmlib
from ..types import Person
from .base import PoseBackend, bbox_from_keypoints

_BASE = "https://download.openmmlab.com/mmpose/v1/projects/rtmo/onnx_sdk/"
MODELS = {
    "t": ("rtmo-t_8xb32-600e_body7-416x416-f48f75cb_20231219.zip", (416, 416)),
    "s": ("rtmo-s_8xb32-600e_body7-640x640-dac2bf74_20231211.zip", (640, 640)),
    "m": ("rtmo-m_16xb16-600e_body7-640x640-39e78cc4_20231211.zip", (640, 640)),
    "l": ("rtmo-l_16xb16-600e_body7-640x640-b37118ce_20231211.zip", (640, 640)),
}


class RTMOBackend(PoseBackend):
    name = "rtmo"
    sizes = tuple(MODELS)
    default_size = "s"

    def __init__(self, size: str | None = None, det_thr: float = 0.5, kp_thr: float = 0.3) -> None:
        super().__init__(size, det_thr, kp_thr)
        from rtmlib import RTMO

        fname, input_size = MODELS[self.size]
        self.model_name, self.input_size = fname, input_size
        self.model = RTMO(_BASE + fname, model_input_size=input_size, score_thr=det_thr,
                          backend="onnxruntime", device="cpu")
        retune_rtmlib(self.model, _BASE + fname)

    def infer(self, frame_bgr: np.ndarray, ts_ms: float | None = None) -> list[Person]:
        kps, scs = self.model(frame_bgr)
        persons = []
        for kp, sc in zip(kps, scs):
            if float(np.mean(sc)) <= 0.0:      # 検出なしのとき rtmlib はゼロ埋めの 1 人を返す
                continue
            kp = np.asarray(kp, dtype=np.float32)
            sc = np.asarray(sc, dtype=np.float32)
            persons.append(Person(
                bbox=bbox_from_keypoints(kp, sc, self.kp_thr),
                score=float(np.mean(sc)),
                keypoints=kp, kp_scores=sc,
            ))
        return persons

    def describe(self) -> dict:
        d = super().describe()
        d.update({"model": self.model_name, "input": list(self.input_size)})
        return d
