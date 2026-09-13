# /// script
# requires-python = ">=3.10"
# dependencies = ["ultralytics>=8.3", "onnx>=1.16", "onnxslim", "onnxruntime"]
# ///
"""Ultralytics YOLO11 を ONNX に書き出す (一度だけ実行)。

  cd analyzer
  uv run tools/export_yolo.py                 # yolo11n-pose + yolo11n (ボール用) を models/ に
  uv run tools/export_yolo.py --sizes n s     # 複数サイズ

このスクリプトだけが torch / ultralytics を使う (uv が一時環境に入れる)。
解析本体 (hoop_analyzer) は onnxruntime だけで動く。
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"


def export(name: str, imgsz: int) -> Path:
    from ultralytics import YOLO

    MODELS_DIR.mkdir(exist_ok=True)
    dst = MODELS_DIR / f"{name}.onnx"
    if dst.exists():
        print(f"既にあります: {dst}")
        return dst
    work = MODELS_DIR / "_work"
    work.mkdir(exist_ok=True)
    model = YOLO(str(work / f"{name}.pt"))               # 無ければ自動ダウンロード
    out = model.export(format="onnx", imgsz=imgsz, opset=12, simplify=True, dynamic=False, nms=False)
    shutil.move(str(out), dst)
    print(f"書き出し: {dst}")
    return dst


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sizes", nargs="*", default=["n"], help="n s m ...")
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--no-pose", action="store_true")
    p.add_argument("--no-det", action="store_true")
    args = p.parse_args()
    for s in args.sizes:
        if not args.no_pose:
            export(f"yolo11{s}-pose", args.imgsz)
        if not args.no_det:
            export(f"yolo11{s}", args.imgsz)


if __name__ == "__main__":
    main()
