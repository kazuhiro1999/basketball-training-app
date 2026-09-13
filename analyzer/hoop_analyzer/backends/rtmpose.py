"""RTMPose (top-down): 人検出 (YOLOX, HumanArt 学習) → 各人物に RTMPose。

rtmlib (ONNX Runtime, CPU) を使う。モデルは初回に download.openmmlab.com から
~/.cache/rtmlib/ へ自動ダウンロードされる。
"""
from __future__ import annotations

import numpy as np

from ..ort_config import retune_rtmlib
from ..types import Person
from .base import PoseBackend


class RTMPoseBackend(PoseBackend):
    name = "rtmpose"
    # rtmlib の Body.MODE に対応: s=lightweight (YOLOX-tiny + RTMPose-s), m=balanced (YOLOX-m + RTMPose-m), x=performance
    sizes = ("s", "m", "x")
    default_size = "m"
    _MODE = {"s": "lightweight", "m": "balanced", "x": "performance"}

    def __init__(self, size: str | None = None, det_thr: float = 0.5, kp_thr: float = 0.3) -> None:
        super().__init__(size, det_thr, kp_thr)
        from rtmlib import RTMPose, YOLOX
        from rtmlib.tools.solution.body import Body

        cfg = Body.MODE[self._MODE[self.size]]
        self.cfg = cfg
        # 検出と姿勢を別々に持つ (Body クラスは bbox を返さないので)
        self.det = YOLOX(cfg["det"], model_input_size=cfg["det_input_size"], score_thr=det_thr,
                         backend="onnxruntime", device="cpu")
        self.pose = RTMPose(cfg["pose"], model_input_size=cfg["pose_input_size"],
                            backend="onnxruntime", device="cpu")
        retune_rtmlib(self.det, cfg["det"])
        retune_rtmlib(self.pose, cfg["pose"])

    def _detect(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
        """YOLOX の生出力から自分でしきい値を掛ける。
        (rtmlib は NMS 内蔵 ONNX に対して score_thr を無視し 0.3 固定にしてしまい、スコアも捨てるため)"""
        img, ratio = self.det.preprocess(frame_bgr)
        out = self.det.inference(img)[0]
        if out.ndim == 3 and out.shape[-1] == 5:          # NMS 内蔵: (1, N, [x1,y1,x2,y2,score])
            boxes, scores = out[0, :, :4] / ratio, out[0, :, 4]
            keep = scores > self.det_thr
            return boxes[keep], scores[keep]
        boxes = self.det.postprocess(out, ratio)           # NMS 無しモデルは rtmlib に任せる
        return np.asarray(boxes, dtype=np.float32).reshape(-1, 4), None

    def infer(self, frame_bgr: np.ndarray, ts_ms: float | None = None) -> list[Person]:
        bboxes, det_scores = self._detect(frame_bgr)
        if len(bboxes) == 0:
            return []
        kps, scs = self.pose(frame_bgr, bboxes=bboxes)
        persons = []
        for i, (bbox, kp, sc) in enumerate(zip(bboxes, kps, scs)):
            persons.append(Person(
                bbox=np.asarray(bbox, dtype=np.float32),
                score=float(det_scores[i]) if det_scores is not None else float(np.mean(sc)),
                keypoints=np.asarray(kp, dtype=np.float32),
                kp_scores=np.asarray(sc, dtype=np.float32),
            ))
        return persons

    def describe(self) -> dict:
        d = super().describe()
        d.update({
            "det_model": self.cfg["det"].rsplit("/", 1)[-1], "det_input": list(self.cfg["det_input_size"]),
            "pose_model": self.cfg["pose"].rsplit("/", 1)[-1], "pose_input": list(self.cfg["pose_input_size"]),
        })
        return d
