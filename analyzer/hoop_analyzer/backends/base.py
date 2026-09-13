"""姿勢推定バックエンドの共通インタフェース。

新しいバックエンドを足すときは PoseBackend を継承して `infer()` を実装し、
backends/__init__.py の REGISTRY に登録するだけ。出力は必ず COCO-17 (types.py 参照)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from ..types import Person


class PoseBackend(ABC):
    name: str = "base"
    sizes: tuple[str, ...] = ()        # 受け付けるモデルサイズ
    default_size: str = ""

    def __init__(self, size: str | None = None, det_thr: float = 0.5, kp_thr: float = 0.3) -> None:
        self.size = size or self.default_size
        if self.sizes and self.size not in self.sizes:
            raise ValueError(f"{self.name}: size は {self.sizes} から選んでください (指定: {self.size})")
        self.det_thr = det_thr
        self.kp_thr = kp_thr

    @abstractmethod
    def infer(self, frame_bgr: np.ndarray) -> list[Person]:
        """1 フレーム (BGR, HxWx3) から人物リストを返す。座標は元画像のピクセル。"""

    def describe(self) -> dict:
        return {"backend": self.name, "size": self.size, "det_thr": self.det_thr, "kp_thr": self.kp_thr}

    def warmup(self, shape: tuple[int, int] = (720, 1280)) -> None:
        self.infer(np.zeros((shape[0], shape[1], 3), dtype=np.uint8))


def bbox_from_keypoints(kp: np.ndarray, sc: np.ndarray, thr: float, pad: float = 0.1) -> np.ndarray:
    """信頼できるキーポイントを囲む矩形。RTMO のように bbox を返さないモデル用。"""
    good = sc > thr
    pts = kp[good] if good.sum() >= 2 else kp
    x1, y1 = pts.min(axis=0)
    x2, y2 = pts.max(axis=0)
    w, h = x2 - x1, y2 - y1
    return np.array([x1 - w * pad, y1 - h * pad, x2 + w * pad, y2 + h * pad], dtype=np.float32)
