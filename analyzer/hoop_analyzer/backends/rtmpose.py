"""RTMPose (top-down): 人検出 (YOLOX, HumanArt 学習) → 各人物に RTMPose。

rtmlib (ONNX Runtime, CPU) を使う。モデルは初回に download.openmmlab.com から
~/.cache/rtmlib/ へ自動ダウンロードされる。
"""
from __future__ import annotations

import numpy as np

from ..ort_config import retune_rtmlib
from ..types import Person
from .base import PoseBackend, bbox_from_keypoints


class RTMPoseBackend(PoseBackend):
    name = "rtmpose"
    # rtmlib の Body.MODE に対応: s=lightweight (YOLOX-tiny + RTMPose-s), m=balanced (YOLOX-m + RTMPose-m), x=performance
    sizes = ("s", "m", "x")
    default_size = "m"
    _MODE = {"s": "lightweight", "m": "balanced", "x": "performance"}

    def __init__(self, size: str | None = None, det_thr: float = 0.5, kp_thr: float = 0.3,
                 max_persons: int = 0, det_every: int = 1) -> None:
        super().__init__(size, det_thr, kp_thr)
        self.max_persons = max_persons      # 0 = 制限なし。ライブ用: 大きく写る順に上位だけ姿勢推定して時間を抑える
        # 人検出は姿勢推定より重い (実測で 1 回 110ms 対 1 人 20ms)。ライブでは毎回検出せず、
        # 間のフレームは前回の姿勢から作った枠を使い回す。新しく入ってきた人は次の検出で拾う
        self.det_every = max(1, det_every)
        self._since_det = 10**9
        self._last_boxes: np.ndarray | None = None
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
        reused = self.det_every > 1 and self._since_det < self.det_every and self._last_boxes is not None and len(self._last_boxes)
        if reused:
            bboxes, det_scores = self._last_boxes, None
            self._since_det += 1
        else:
            bboxes, det_scores = self._detect(frame_bgr)
            self._since_det = 1
            self._last_boxes = bboxes if len(bboxes) else None
        if len(bboxes) == 0:
            self._last_boxes = None
            return []
        if self.max_persons and len(bboxes) > self.max_persons:
            order = np.argsort(-(bboxes[:, 3] - bboxes[:, 1]))[: self.max_persons]
            bboxes = bboxes[order]
            det_scores = det_scores[order] if det_scores is not None else None
        kps, scs = self.pose(frame_bgr, bboxes=bboxes)
        persons = []
        for i, (bbox, kp, sc) in enumerate(zip(bboxes, kps, scs)):
            persons.append(Person(
                bbox=np.asarray(bbox, dtype=np.float32),
                score=float(det_scores[i]) if det_scores is not None else float(np.mean(sc)),
                keypoints=np.asarray(kp, dtype=np.float32),
                kp_scores=np.asarray(sc, dtype=np.float32),
            ))
        if self.det_every > 1 and persons:
            # 次のフレームで使い回す枠は、今回の骨格から作り直す (人が動いてもついていく)
            self._last_boxes = np.array([bbox_from_keypoints(p.keypoints, p.kp_scores, self.kp_thr, pad=0.25)
                                         for p in persons], dtype=np.float32)
        return persons

    def describe(self) -> dict:
        d = super().describe()
        d.update({
            "max_persons": self.max_persons, "det_every": self.det_every,
            "det_model": self.cfg["det"].rsplit("/", 1)[-1], "det_input": list(self.cfg["det_input_size"]),
            "pose_model": self.cfg["pose"].rsplit("/", 1)[-1], "pose_input": list(self.cfg["pose_input_size"]),
        })
        return d
