"""バックエンドの登録簿。名前 → クラス。`create_pose_backend("rtmo", size="s")` で生成。"""
from __future__ import annotations

from .base import PoseBackend

REGISTRY: dict[str, str] = {
    "rtmpose": "hoop_analyzer.backends.rtmpose:RTMPoseBackend",   # top-down: YOLOX 人検出 → RTMPose
    "rtmo": "hoop_analyzer.backends.rtmo:RTMOBackend",            # one-stage
    "yolo": "hoop_analyzer.backends.yolo_pose:YoloPoseBackend",   # YOLO11-pose (ONNX)
    "mediapipe": "hoop_analyzer.backends.mediapipe_pose:MediaPipePoseBackend",   # MediaPipe Pose Landmarker (検証用, 1人向け)
    "holistic": "hoop_analyzer.backends.mediapipe_pose:HolisticBackend",         # MediaPipe Holistic (1人, 手も出る)
}


def backend_class(name: str) -> type[PoseBackend]:
    if name not in REGISTRY:
        raise KeyError(f"不明なバックエンド '{name}'。選択肢: {', '.join(REGISTRY)}")
    import importlib

    mod, cls = REGISTRY[name].split(":")
    return getattr(importlib.import_module(mod), cls)   # 依存ライブラリは使うものだけ読み込む


def create_pose_backend(name: str, size: str | None = None, **kwargs) -> PoseBackend:
    return backend_class(name)(size=size, **kwargs)


__all__ = ["PoseBackend", "REGISTRY", "backend_class", "create_pose_backend"]
