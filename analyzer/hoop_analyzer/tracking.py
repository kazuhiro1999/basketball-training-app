"""人物トラッキング (ID 付与)。姿勢バックエンドとは独立。

- simple   : IoU + キーポイント一致 + 服の色 (HSV ヒストグラム) で対応付け。等速予測で見失いを跨ぐ。依存なし
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

    def update(self, persons: list[Person], frame: np.ndarray | None = None) -> list[Person]:
        return persons


def keypoint_similarity(kp_a: np.ndarray, sc_a: np.ndarray, kp_b: np.ndarray, sc_b: np.ndarray,
                        scale: float, thr: float = 0.3) -> float:
    """OKS 風のキーポイント一致度 (0..1)。両方で信頼できる関節だけを使う。"""
    good = (sc_a > thr) & (sc_b > thr)
    if good.sum() < 3:
        return 0.0
    d2 = ((kp_a[good] - kp_b[good]) ** 2).sum(axis=1)
    return float(np.mean(np.exp(-d2 / (2 * (0.1 * max(scale, 1.0)) ** 2))))


def torso_histogram(frame: np.ndarray, p: Person, thr: float = 0.3) -> np.ndarray | None:
    """肩〜腰の領域の HSV ヒストグラム (服の色)。オクルージョン後の再対応付けの手掛かり。"""
    import cv2

    kp, sc = p.keypoints, p.kp_scores
    idx = [5, 6, 11, 12]
    if all(sc[i] > thr for i in idx):
        pts = kp[idx]
        x1, y1 = pts.min(axis=0)
        x2, y2 = pts.max(axis=0)
    else:
        bx1, by1, bx2, by2 = p.bbox
        h = by2 - by1
        x1, x2 = bx1 + (bx2 - bx1) * 0.25, bx2 - (bx2 - bx1) * 0.25
        y1, y2 = by1 + h * 0.2, by1 + h * 0.55
    H, W = frame.shape[:2]
    x1, x2 = int(max(0, x1)), int(min(W, x2))
    y1, y2 = int(max(0, y1)), int(min(H, y2))
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    hsv = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256]).flatten()
    n = hist.sum()
    return hist / n if n > 0 else None


class SimpleTracker(Tracker):
    """IoU + キーポイント一致 + 服の色 で対応付けるトラッカー (依存なし)。

    - 追跡中のトラック: IoU とキーポイント一致度の合成で対応付け (重なり時に bbox だけより取り違えにくい)
    - 見失ったトラック: 等速予測位置からの距離 + 服の色の類似で再対応付け (max_lost フレームまで待つ)
    """

    name = "simple"

    class _Track:
        __slots__ = ("id", "bbox", "vel", "lost", "hits", "kp", "sc", "hist")

        def __init__(self, tid: int, p: Person, hist) -> None:
            self.id, self.bbox, self.vel, self.lost, self.hits = tid, p.bbox.astype(np.float32), np.zeros(4, np.float32), 0, 1
            self.kp, self.sc, self.hist = p.keypoints, p.kp_scores, hist

        def predict(self) -> np.ndarray:
            return self.bbox + self.vel

        def height(self) -> float:
            return float(max(self.bbox[3] - self.bbox[1], 1.0))

    def __init__(self, iou_thr: float = 0.25, max_lost: int = 45, min_hits: int = 2,
                 w_iou: float = 0.5, w_kp: float = 0.5, use_appearance: bool = True) -> None:
        self.iou_thr, self.max_lost, self.min_hits = iou_thr, max_lost, min_hits
        self.w_iou, self.w_kp, self.use_appearance = w_iou, w_kp, use_appearance
        self.tracks: list[SimpleTracker._Track] = []
        self.next_id = 1

    def _cost(self, t: "_Track", p: Person, hist) -> float:
        """小さいほど良い。対応付け不可なら inf。"""
        pred = t.predict()
        iou = float(iou_matrix(pred[None], p.bbox[None])[0, 0])
        kps = keypoint_similarity(t.kp, t.sc, p.keypoints, p.kp_scores, t.height())
        if t.lost == 0:
            if iou < self.iou_thr and kps < 0.3:
                return np.inf
            return 1.0 - (self.w_iou * iou + self.w_kp * kps)
        # 見失い中: 予測位置からの距離 (bbox 高さで正規化) と服の色
        pc = np.array([(pred[0] + pred[2]) / 2, (pred[1] + pred[3]) / 2])
        dc = np.array([(p.bbox[0] + p.bbox[2]) / 2, (p.bbox[1] + p.bbox[3]) / 2])
        dist = float(np.hypot(*(pc - dc))) / t.height()
        gate = 1.0 + 0.05 * t.lost
        if dist > gate:
            return np.inf
        app = 0.5
        if self.use_appearance and hist is not None and t.hist is not None:
            app = float(np.minimum(hist, t.hist).sum())          # ヒストグラム交差 (0..1)
            if app < 0.25:
                return np.inf
        return 1.0 + dist / gate + (1.0 - app)                      # 追跡中の対応より常に後回し

    def update(self, persons: list[Person], frame: np.ndarray | None = None) -> list[Person]:
        hists = [torso_histogram(frame, p) if (self.use_appearance and frame is not None) else None for p in persons]
        cost = np.full((len(self.tracks), len(persons)), np.inf, dtype=np.float64)
        for i, t in enumerate(self.tracks):
            for j, p in enumerate(persons):
                cost[i, j] = self._cost(t, p, hists[j])
        matched_t, matched_d = set(), set()
        if cost.size:
            finite = np.where(np.isfinite(cost), cost, 1e6)
            rows, cols = linear_sum_assignment(finite)
            for r, c in zip(rows, cols):
                if not np.isfinite(cost[r, c]):
                    continue
                t, p = self.tracks[r], persons[c]
                new_vel = p.bbox - t.bbox
                t.vel = 0.7 * t.vel + 0.3 * new_vel if t.lost == 0 else new_vel / (t.lost + 1)
                t.bbox, t.lost, t.hits = p.bbox.astype(np.float32), 0, t.hits + 1
                t.kp, t.sc = p.keypoints, p.kp_scores
                if hists[c] is not None:
                    t.hist = hists[c] if t.hist is None else 0.9 * t.hist + 0.1 * hists[c]
                p.track_id = t.id
                matched_t.add(r)
                matched_d.add(c)
        for i, t in enumerate(self.tracks):
            if i not in matched_t:
                t.lost += 1
                t.bbox = t.predict()
                t.vel *= 0.8
        self.tracks = [t for t in self.tracks if t.lost <= self.max_lost]
        for c, p in enumerate(persons):
            if c not in matched_d:
                t = SimpleTracker._Track(self.next_id, p, hists[c])
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

    def update(self, persons: list[Person], frame: np.ndarray | None = None) -> list[Person]:
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
