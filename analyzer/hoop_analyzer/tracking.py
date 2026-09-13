"""人物トラッキング (ID 付与)。姿勢バックエンドとは独立。

- simple   : IoU + ハンガリアン法。等速予測で短いオクルージョンを跨ぐ。依存なし
- bytetrack / ocsort / sort: roboflow `trackers` パッケージ (uv sync --extra trackers)
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linear_sum_assignment

from .types import Person


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a (N,4), b (M,4) xyxy → (N,M)"""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)), dtype=np.float32)
    x1 = np.maximum(a[:, None, 0], b[None, :, 0])
    y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2])
    y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.clip(area_a[:, None] + area_b[None, :] - inter, 1e-6, None)


class Tracker:
    name = "none"

    def update(self, persons: list[Person]) -> list[Person]:
        return persons


class SimpleTracker(Tracker):
    name = "simple"

    class _Track:
        __slots__ = ("id", "bbox", "vel", "lost", "hits")

        def __init__(self, tid: int, bbox: np.ndarray) -> None:
            self.id, self.bbox, self.vel, self.lost, self.hits = tid, bbox.astype(np.float32), np.zeros(4, np.float32), 0, 1

        def predict(self) -> np.ndarray:
            return self.bbox + self.vel

    def __init__(self, iou_thr: float = 0.3, max_lost: int = 30, min_hits: int = 2) -> None:
        self.iou_thr, self.max_lost, self.min_hits = iou_thr, max_lost, min_hits
        self.tracks: list[SimpleTracker._Track] = []
        self.next_id = 1

    def update(self, persons: list[Person]) -> list[Person]:
        preds = np.array([t.predict() for t in self.tracks], dtype=np.float32).reshape(-1, 4)
        dets = np.array([p.bbox for p in persons], dtype=np.float32).reshape(-1, 4)
        iou = iou_matrix(preds, dets)
        matched_t, matched_d = set(), set()
        if iou.size:
            rows, cols = linear_sum_assignment(-iou)
            for r, c in zip(rows, cols):
                if iou[r, c] >= self.iou_thr:
                    t = self.tracks[r]
                    new_vel = dets[c] - t.bbox
                    t.vel = 0.7 * t.vel + 0.3 * new_vel if t.lost == 0 else new_vel / (t.lost + 1)
                    t.bbox, t.lost, t.hits = dets[c], 0, t.hits + 1
                    persons[c].track_id = t.id
                    matched_t.add(r)
                    matched_d.add(c)
        for i, t in enumerate(self.tracks):
            if i not in matched_t:
                t.lost += 1
                t.bbox = t.predict()
                t.vel *= 0.9
        self.tracks = [t for t in self.tracks if t.lost <= self.max_lost]
        for c, p in enumerate(persons):
            if c not in matched_d:
                t = SimpleTracker._Track(self.next_id, p.bbox)
                self.next_id += 1
                self.tracks.append(t)
                p.track_id = t.id
        # 生まれたばかりのトラック (min_hits 未満) は ID を伏せる: 誤検出の ID を増やさない
        young = {t.id for t in self.tracks if t.hits < self.min_hits}
        for p in persons:
            if p.track_id in young:
                p.track_id = None
        return persons


class RoboflowTracker(Tracker):
    """roboflow `trackers` パッケージのラッパ (ByteTrack / OC-SORT / SORT)。uv sync --extra trackers"""

    _CLASSES = {"bytetrack": "ByteTrackTracker", "ocsort": "OCSORTTracker", "sort": "SORTTracker"}

    def __init__(self, kind: str = "bytetrack", fps: float = 30.0, lost_sec: float = 1.0,
                 track_thresh: float = 0.5, min_consecutive: int = 2) -> None:
        try:
            import supervision as sv
            import trackers
        except ImportError as e:
            raise ImportError(f"{kind} には trackers パッケージが必要です: uv sync --extra trackers") from e
        self.name = kind
        self.sv = sv
        import inspect

        cls = getattr(trackers, self._CLASSES[kind])
        wanted = {"lost_track_buffer": int(lost_sec * fps), "frame_rate": fps,
                  "track_activation_threshold": track_thresh, "minimum_consecutive_frames": min_consecutive}
        accepted = inspect.signature(cls.__init__).parameters
        self.tracker = cls(**{k: v for k, v in wanted.items() if k in accepted})   # トラッカーごとに引数が違う

    def update(self, persons: list[Person]) -> list[Person]:
        sv = self.sv
        if not persons:
            self.tracker.update(sv.Detections.empty())
            return persons
        det = sv.Detections(
            xyxy=np.array([p.bbox for p in persons], dtype=np.float32),
            confidence=np.array([p.score for p in persons], dtype=np.float32),
            class_id=np.zeros(len(persons), dtype=int),
        )
        out = self.tracker.update(det)
        # トラッカーは検出を並べ替え/間引くので bbox で元の Person と対応付ける
        for p in persons:
            p.track_id = None
        if len(out) and out.tracker_id is not None:
            iou = iou_matrix(np.array([p.bbox for p in persons], dtype=np.float32), out.xyxy)
            for i, p in enumerate(persons):
                j = int(np.argmax(iou[i]))
                tid = int(out.tracker_id[j])
                if iou[i, j] > 0.5 and tid >= 0:          # -1 は未確定トラック
                    p.track_id = tid + 1                    # 1 始まりに揃える
        return persons


def create_tracker(name: str, fps: float = 30.0) -> Tracker:
    if name in ("none", "", None):
        return Tracker()
    if name == "simple":
        return SimpleTracker()
    if name in RoboflowTracker._CLASSES:
        return RoboflowTracker(name, fps=fps)
    raise KeyError(f"不明なトラッカー '{name}' (simple / bytetrack / ocsort / sort / none)")
