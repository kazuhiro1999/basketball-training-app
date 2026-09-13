"""pose.jsonl から ID の安定性を数える (正解なしの目安)。

  uv run trackstats ../data/samples/vtest_analysis/*/pose.jsonl
  uv run trackstats ../data/samples/vtest_analysis          # 配下をまとめて表

指標
  ids        付いた ID の数 (少ないほど分断が少ない。実人数に近いのが理想)
  null%      ID 無し (未確定トラック) の人物検出の割合
  短寿命     10 フレーム未満で消えた ID の数 (誤検出 or 分断)
  再付与疑い 新しい ID が現れた直前 (30 フレーム以内) に、近く (bbox 高さの 1 倍以内) で
             別の ID が消えている回数 = オクルージョン後に別 ID になった疑い
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np


def analyze(path: Path, gap: int = 30) -> dict:
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    first: dict[int, tuple[int, np.ndarray]] = {}
    last: dict[int, tuple[int, np.ndarray]] = {}
    count: dict[int, int] = {}
    n_person = n_null = 0
    for r in rows:
        for p in r["persons"]:
            n_person += 1
            if p["id"] is None:
                n_null += 1
                continue
            c = np.array([(p["bbox"][0] + p["bbox"][2]) / 2, (p["bbox"][1] + p["bbox"][3]) / 2, p["bbox"][3] - p["bbox"][1]])
            first.setdefault(p["id"], (r["i"], c))
            last[p["id"]] = (r["i"], c)
            count[p["id"]] = count.get(p["id"], 0) + 1
    ids = sorted(first)
    reassign = 0
    for tid in ids:
        f, c = first[tid]
        if f == rows[0]["i"]:
            continue
        for other in ids:
            if other == tid:
                continue
            lf, lc = last[other]
            if 0 < f - lf <= gap and np.hypot(c[0] - lc[0], c[1] - lc[1]) <= max(c[2], lc[2]):
                reassign += 1
                break
    lengths = np.array([count[t] for t in ids]) if ids else np.zeros(1)
    return {
        "frames": len(rows),
        "persons_per_frame": n_person / max(len(rows), 1),
        "ids": len(ids),
        "null_pct": 100 * n_null / max(n_person, 1),
        "short": int((lengths < 10).sum()) if ids else 0,
        "len_median": float(np.median(lengths)) if ids else 0.0,
        "reassign": reassign,
    }


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = argv if argv is not None else sys.argv[1:]
    files: list[Path] = []
    for a in args or ["."]:
        p = Path(a)
        if p.is_dir():
            files += sorted(p.rglob("pose.jsonl"))
        else:
            files.append(p)
    print(f"{'run':<28}{'frames':>7}{'人/フレーム':>10}{'ids':>5}{'null%':>7}{'短寿命':>7}{'寿命中央':>9}{'再付与疑い':>11}")
    for f in files:
        s = analyze(f)
        name = f.parent.name if f.name == "pose.jsonl" else f.name
        print(f"{name:<28}{s['frames']:>7}{s['persons_per_frame']:>10.2f}{s['ids']:>5}{s['null_pct']:>7.1f}"
              f"{s['short']:>7}{s['len_median']:>9.0f}{s['reassign']:>11}")


if __name__ == "__main__":
    main()
