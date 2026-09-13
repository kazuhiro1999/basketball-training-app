"""ONNX Runtime のスレッド設定。

1 プロセスで複数セッション (姿勢 + ボール、あるいは検出 + 姿勢) を持つと、既定では
各セッションが全コア分のスレッドを作り、しかも仕事が無い間もスピン待機するため、
互いに (そして復号処理まで) 奪い合って数倍〜十数倍遅くなる。ここで
  - スレッド数を抑える
  - スピン待機を切る
を全セッションに一律に掛ける。環境変数や GPU 設定には触らない (CPU EP のみ)。
"""
from __future__ import annotations

import os

import onnxruntime as ort

PROVIDERS = ["CPUExecutionProvider"]
_threads: int | None = None


def default_threads() -> int:
    cpu = os.cpu_count() or 4
    return max(1, min(8, cpu // 2))


def set_threads(n: int | None) -> None:
    global _threads
    _threads = n


def session_options(threads: int | None = None) -> ort.SessionOptions:
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads or _threads or default_threads()
    so.inter_op_num_threads = 1
    so.add_session_config_entry("session.intra_op.allow_spinning", "0")
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return so


def make_session(model_path: str, threads: int | None = None) -> ort.InferenceSession:
    return ort.InferenceSession(str(model_path), sess_options=session_options(threads), providers=PROVIDERS)


def retune_rtmlib(tool, model_url: str, threads: int | None = None) -> None:
    """rtmlib のツール (RTMO / RTMPose / YOLOX …) が内部で作ったセッションを、上記設定で作り直す。"""
    from rtmlib.tools.base import download_checkpoint

    path = model_url if os.path.exists(model_url) else download_checkpoint(model_url)
    tool.session = make_session(path, threads)
