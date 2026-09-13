# /// script
# requires-python = ">=3.10"
# ///
"""録画フォルダの要約を表示する。

  uv run tools/rec_info.py recordings/2026-09-13_17-40-12
  uv run tools/rec_info.py recordings            # 配下の録画を一覧
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}"


def summarize(rec: Path) -> None:
    meta = json.loads((rec / "meta.json").read_text(encoding="utf-8"))
    print(f"== {meta['recording_id']}  ({rec})")
    print(f"   開始 {meta['started_at']}  終了 {meta.get('ended_at') or '(未確定)'}  "
          f"長さ {fmt_dur(meta.get('duration_sec') or 0)}  遅延設定 {meta.get('delay_sec_at_start')}s  "
          f"プリロール {meta.get('preroll_sec')}s")
    setup = meta.get("setup") or {}
    print(f"   撮影条件: 視点={setup.get('view')} 高さ={setup.get('height_m')}m 距離={setup.get('distance_m')}m "
          f"練習={setup.get('drill')} メモ={setup.get('note') or ''}")
    print(f"   PC: {meta.get('pc', {}).get('host')}  app v{meta.get('app_version')}  "
          f"合計 {meta.get('frames_total')} フレーム {meta.get('bytes_total', 0) / 1e6:.1f} MB")

    # frames.csv からセグメントごとの実測 fps と欠落 (ts の飛び) を出す
    per_seg: dict[int, list[tuple[int, int]]] = {}
    fcsv = rec / "frames.csv"
    if fcsv.exists():
        with open(fcsv, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                per_seg.setdefault(int(row["seg"]), []).append((int(row["ts_us"]), int(row["key"])))
    for seg in meta.get("segments", []):
        rows = per_seg.get(seg["seg"], [])
        line = (f"   seg{seg['seg']:02d} {seg['file']:<12} {seg['codec']:<12} {seg.get('width')}x{seg.get('height')}"
                f"@{seg.get('fps')}  {seg['frames']} フレーム {seg['bytes'] / 1e6:.1f} MB")
        if len(rows) > 1:
            dur = (rows[-1][0] - rows[0][0]) / 1e6
            fps = seg.get("fps") or 30
            gaps = sum(1 for a, b in zip(rows, rows[1:]) if (b[0] - a[0]) > 2.5e6 / fps)
            keys = sum(k for _, k in rows)
            line += f"  実測 {fmt_dur(dur)} {len(rows) / dur:.1f}fps  キー {keys}  欠落疑い {gaps}"
        phone = seg.get("phone") or {}
        if phone.get("orientation") or phone.get("camera"):
            line += f"\n          端末: {phone.get('orientation')} / {phone.get('camera')} / {(phone.get('ua') or '')[:60]}"
        if seg.get("test"):
            line += "  (テストパターン)"
        print(line)

    ev = rec / "events.jsonl"
    if ev.exists():
        types: Counter = Counter()
        ui: Counter = Counter()
        with open(ev, encoding="utf-8") as f:
            for ln in f:
                try:
                    e = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                types[e.get("type")] += 1
                if e.get("type") == "ui":
                    ui[e.get("action")] += 1
        print("   イベント: " + ", ".join(f"{k}={v}" for k, v in sorted(types.items())))
        if ui:
            print("   画面操作: " + ", ".join(f"{k}={v}" for k, v in sorted(ui.items())))
    print()


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "recordings")
    if (target / "meta.json").exists():
        summarize(target)
        return
    recs = sorted(p for p in target.iterdir() if (p / "meta.json").exists()) if target.exists() else []
    if not recs:
        print(f"録画が見つかりません: {target}")
        return
    for r in recs:
        summarize(r)


if __name__ == "__main__":
    main()
