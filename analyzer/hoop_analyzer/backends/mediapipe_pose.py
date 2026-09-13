"""MediaPipe Pose Landmarker / Holistic Landmarker (Tasks API, mediapipe 内蔵の TFLite, CPU)。

検証用。フリースローのように 1 人が明確に写る場面向け。
  - mediapipe : PoseLandmarker (lite / full / heavy)。num_poses で複数人も可だが ID 追跡は無い
  - holistic  : HolisticLandmarker (1 人)。姿勢 33 点 + 両手 21 点ずつ (+顔)
出力は他バックエンドと同じ COCO-17 に変換し、元の 33 点 (BlazePose) と 3D world landmarks、
手のランドマークは Person.extra に残す。

  uv sync --extra mediapipe
モデル (.task) は初回に storage.googleapis.com から models/ へダウンロードする。
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

import numpy as np

from ..types import Person
from ..yolo_onnx import MODELS_DIR
from .base import PoseBackend, bbox_from_keypoints

_MP = "https://storage.googleapis.com/mediapipe-models/"
POSE_MODELS = {
    "lite": _MP + "pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task",
    "full": _MP + "pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task",
    "heavy": _MP + "pose_landmarker/pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task",
}
HOLISTIC_MODEL = _MP + "holistic_landmarker/holistic_landmarker/float16/latest/holistic_landmarker.task"

# BlazePose 33 点 → COCO-17 の添字
BLAZE_TO_COCO = [0, 2, 5, 7, 8, 11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28]


def _download(url: str) -> Path:
    MODELS_DIR.mkdir(exist_ok=True)
    dst = MODELS_DIR / url.rsplit("/", 1)[-1]
    if not dst.exists():
        print(f"ダウンロード: {url}")
        urllib.request.urlretrieve(url, dst)
    return dst


def _landmarks_to_arrays(lms, w: int, h: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """NormalizedLandmark 列 → (xy ピクセル (N,2), 可視度 (N,), 全成分 (N,5): x, y, z, visibility, presence)"""
    full = np.array([[lm.x * w, lm.y * h, lm.z * w, lm.visibility or 0.0, lm.presence or 0.0] for lm in lms], dtype=np.float32)
    return full[:, :2], full[:, 3], full


class _MonotonicClock:
    """VIDEO モードはタイムスタンプが単調増加でなければならない。録画のセグメント境界で巻き戻っても壊れないようにする。"""

    def __init__(self) -> None:
        self.last = -1
        self.offset = 0

    def __call__(self, ts_ms: float | None) -> int:
        if ts_ms is None:
            t = self.last + 33
        else:
            t = int(ts_ms) + self.offset
            if t <= self.last:
                self.offset += self.last - t + 33
                t = self.last + 33
        self.last = t
        return t


class _RoiMixin:
    """BlazePose の人検出は 224px に縮めた画像で行うため、広い画角で人が小さいと見つけられない。
    roi='auto' では YOLO11n の person 検出で最も大きく写る人を正方形に切り出し、固定サイズ (ROI_SIZE) に
    リサイズしてから MediaPipe を動かす (VIDEO モードは入力サイズが毎回同じでないと落ちる)。
    以降は前フレームの結果の周囲を使い、30 フレームごと (または見失った時) に検出し直す。"""

    ROI_SIZE = 512

    def _init_roi(self, roi: str) -> None:
        self.roi_mode = roi
        self.person_det = None
        self.roi_age = 0
        self.cur_roi: tuple[int, int, int] | None = None     # (x1, y1, side)
        if roi == "auto":
            from ..yolo_onnx import YoloClassDetector

            self.person_det = YoloClassDetector(class_id=0, conf_thr=0.3)

    @staticmethod
    def _roi_from_box(box, w: int, h: int) -> tuple[int, int, int] | None:
        bw, bh = float(box[2] - box[0]), float(box[3] - box[1])
        cx, cy = float(box[0] + box[2]) / 2, float(box[1] + box[3]) / 2
        side = int(max(bw, bh) * 1.6)
        if side < 64:
            return None
        return int(round(cx - side / 2)), int(round(cy - side / 2)), side

    @staticmethod
    def _inside(box, roi: tuple[int, int, int], margin: float = 0.08) -> bool:
        x1, y1, side = roi
        m = side * margin
        return box[0] >= x1 + m and box[1] >= y1 + m and box[2] <= x1 + side - m and box[3] <= y1 + side - m

    def _prepare(self, frame: np.ndarray, last_persons: list) -> tuple[np.ndarray, tuple[float, float, float] | None]:
        """→ (推定に使う画像, 座標復元用 (x1, y1, scale) or None)
        切り出し枠は毎フレーム動かさない (MediaPipe の VIDEO モードは前フレームの位置から追跡するため)。
        人が枠の端に寄ったら枠を取り直し、30 フレームごとに YOLO で検出し直す。"""
        if self.roi_mode != "auto":
            return frame, None
        h, w = frame.shape[:2]
        if last_persons and self.cur_roi is not None and self.roi_age < 30:
            self.roi_age += 1
            if not self._inside(last_persons[0].bbox, self.cur_roi):
                self.cur_roi = self._roi_from_box(last_persons[0].bbox, w, h)
        else:
            dets = self.person_det.detect(frame)
            self.cur_roi = self._roi_from_box(max(dets, key=lambda t: t[0][3] - t[0][1])[0], w, h) if dets else None
            self.roi_age = 0
        if self.cur_roi is None:
            return frame, None
        x1, y1, side = self.cur_roi
        # 画像外にはみ出す分は黒で埋めて正方形を保つ
        canvas = np.zeros((side, side, 3), dtype=np.uint8)
        sx1, sy1 = max(0, x1), max(0, y1)
        sx2, sy2 = min(w, x1 + side), min(h, y1 + side)
        if sx2 <= sx1 or sy2 <= sy1:
            self.cur_roi = None
            return frame, None
        canvas[sy1 - y1:sy2 - y1, sx1 - x1:sx2 - x1] = frame[sy1:sy2, sx1:sx2]
        import cv2

        patch = cv2.resize(canvas, (self.ROI_SIZE, self.ROI_SIZE), interpolation=cv2.INTER_AREA if side > self.ROI_SIZE else cv2.INTER_LINEAR)
        return patch, (float(x1), float(y1), self.ROI_SIZE / side)

    @staticmethod
    def _map_back(persons: list, info: tuple[float, float, float] | None) -> list:
        if info is None:
            return persons
        x0, y0, sc = info
        for p in persons:
            p.bbox = np.array([p.bbox[0] / sc + x0, p.bbox[1] / sc + y0, p.bbox[2] / sc + x0, p.bbox[3] / sc + y0], dtype=np.float32)
            p.keypoints = p.keypoints / sc + np.array([x0, y0], dtype=np.float32)
            if p.extra and p.extra.get("mp33"):
                p.extra["mp33"] = [[round(x / sc + x0, 2), round(y / sc + y0, 2), round(z / sc, 2), v, pr] for x, y, z, v, pr in p.extra["mp33"]]
            if p.extra and p.extra.get("hands"):
                for side, pts in p.extra["hands"].items():
                    if pts:
                        p.extra["hands"][side] = [[round(x / sc + x0, 1), round(y / sc + y0, 1), round(z / sc, 1)] for x, y, z in pts]
            if p.extra and p.extra.get("face"):
                p.extra["face"] = [[round(x / sc + x0, 1), round(y / sc + y0, 1)] for x, y in p.extra["face"]]
        return persons


class MediaPipePoseBackend(_RoiMixin, PoseBackend):
    name = "mediapipe"
    sizes = ("lite", "full", "heavy")
    default_size = "full"

    def __init__(self, size: str | None = None, det_thr: float = 0.5, kp_thr: float = 0.3, num_poses: int = 1,
                 roi: str = "auto") -> None:
        super().__init__(size, det_thr, kp_thr)
        self._init_roi(roi)
        self.last_persons: list[Person] = []
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python.vision import PoseLandmarker, PoseLandmarkerOptions, RunningMode

        self.mp = mp
        self.num_poses = num_poses
        self.model_path = _download(POSE_MODELS[self.size])
        opts = PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(self.model_path)),
            running_mode=RunningMode.VIDEO,
            num_poses=num_poses,
            min_pose_detection_confidence=det_thr,
            min_pose_presence_confidence=det_thr,
            min_tracking_confidence=det_thr,
        )
        self.landmarker = PoseLandmarker.create_from_options(opts)
        self.clock = _MonotonicClock()

    def infer(self, frame_bgr: np.ndarray, ts_ms: float | None = None) -> list[Person]:
        src, info = self._prepare(frame_bgr, self.last_persons)
        h, w = src.shape[:2]
        rgb = np.ascontiguousarray(src[:, :, ::-1])
        img = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        res = self.landmarker.detect_for_video(img, self.clock(ts_ms))
        persons = []
        for i, lms in enumerate(res.pose_landmarks or []):
            xy, vis, full = _landmarks_to_arrays(lms, w, h)
            kp, sc = xy[BLAZE_TO_COCO], vis[BLAZE_TO_COCO]
            world = None
            if res.pose_world_landmarks and i < len(res.pose_world_landmarks):
                world = np.array([[lm.x, lm.y, lm.z] for lm in res.pose_world_landmarks[i]], dtype=np.float32)
            persons.append(Person(
                bbox=bbox_from_keypoints(xy, vis, self.kp_thr, pad=0.05),
                score=float(np.mean(vis)),
                keypoints=kp.astype(np.float32), kp_scores=sc.astype(np.float32),
                extra={"mp33": full.round(3).tolist(), "world": world.round(4).tolist() if world is not None else None},
            ))
        persons = self._map_back(persons, info)
        self.last_persons = persons
        return persons

    def describe(self) -> dict:
        d = super().describe()
        d.update({"model": self.model_path.name, "num_poses": self.num_poses, "roi": self.roi_mode, "keypoints_extra": "mp33 + world(3D m)"})
        return d


class HolisticBackend(_RoiMixin, PoseBackend):
    name = "holistic"
    sizes = ("default",)
    default_size = "default"

    def __init__(self, size: str | None = None, det_thr: float = 0.5, kp_thr: float = 0.3, with_face: bool = False,
                 roi: str = "auto") -> None:
        super().__init__(size, det_thr, kp_thr)
        self._init_roi(roi)
        self.last_persons: list[Person] = []
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python.vision import HolisticLandmarker, HolisticLandmarkerOptions, RunningMode

        self.mp = mp
        self.with_face = with_face
        self.model_path = _download(HOLISTIC_MODEL)
        opts = HolisticLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(self.model_path)),
            running_mode=RunningMode.VIDEO,
            min_pose_detection_confidence=det_thr,
            min_pose_landmarks_confidence=det_thr,
            min_hand_landmarks_confidence=det_thr,
        )
        self.landmarker = HolisticLandmarker.create_from_options(opts)
        self.clock = _MonotonicClock()

    def infer(self, frame_bgr: np.ndarray, ts_ms: float | None = None) -> list[Person]:
        src, info = self._prepare(frame_bgr, self.last_persons)
        h, w = src.shape[:2]
        rgb = np.ascontiguousarray(src[:, :, ::-1])
        img = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        res = self.landmarker.detect_for_video(img, self.clock(ts_ms))
        if not res.pose_landmarks:
            self.last_persons = []
            return []
        xy, vis, full = _landmarks_to_arrays(res.pose_landmarks, w, h)
        kp, sc = xy[BLAZE_TO_COCO], vis[BLAZE_TO_COCO]
        world = np.array([[lm.x, lm.y, lm.z] for lm in res.pose_world_landmarks], dtype=np.float32) if res.pose_world_landmarks else None
        hands = {}
        for side, lms in (("left", res.left_hand_landmarks), ("right", res.right_hand_landmarks)):
            hands[side] = [[round(lm.x * w, 1), round(lm.y * h, 1), round(lm.z * w, 1)] for lm in lms] if lms else None
        extra = {"mp33": full.round(3).tolist(), "world": world.round(4).tolist() if world is not None else None, "hands": hands}
        if self.with_face and res.face_landmarks:
            extra["face"] = [[round(lm.x * w, 1), round(lm.y * h, 1)] for lm in res.face_landmarks]
        persons = self._map_back([Person(
            bbox=bbox_from_keypoints(xy, vis, self.kp_thr, pad=0.05),
            score=float(np.mean(vis)),
            keypoints=kp.astype(np.float32), kp_scores=sc.astype(np.float32),
            extra=extra,
        )], info)
        self.last_persons = persons
        return persons

    def describe(self) -> dict:
        d = super().describe()
        d.update({"model": self.model_path.name, "roi": self.roi_mode,
                  "keypoints_extra": "mp33 + world(3D m) + hands(21x2)" + (" + face" if self.with_face else "")})
        return d
