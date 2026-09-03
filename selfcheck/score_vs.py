#!/usr/bin/env python3
"""Самооценка (готовая, голый контекст) против любого размеченного прогона.

Факт берётся из указанного файла разметки, самооценка — из
selfcheck/selfcheck.jsonl (коммит 224c62d), большинство из трёх.
Правило «справился» прежнее: не меньше двух «верных» из трёх.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter, defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
GROUPS = {
    "G1 решение объявлено": ["w2a", "w2b", "w2c", "p1a", "p1b", "p1c"],
    "G2 решения нет":       ["w3a", "w3b", "p3a", "p3b", "p2a", "p2b"],
    "G3 не метрика":        ["w1a", "w1b"],
}
GOF = {q: g for g, qs in GROUPS.items() for q in qs}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scored", required=True)
    ap.add_argument("--label", default="прогон")
    args = ap.parse_args()

    d = [json.loads(l) for l in
         pathlib.Path(args.scored).read_text(encoding="utf-8").splitlines() if l.strip()]
    by = defaultdict(list)
    for x in d:
        by[x["id"]].append(x["verdict"])
    fact = {k: Counter(v)["верный"] >= 2 for k, v in by.items()}
    nsql = defaultdict(list)
    for x in d:
        nsql[x["id"]].append(x["n_sql"])

    rows = [json.loads(l) for l in
            (ROOT / "selfcheck" / "selfcheck.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    said = defaultdict(list)
    for r in rows:
        said[r["id"]].append(r["verdict"])
    maj = {k: ("могу" if v.count("могу") >= 2 else "не могу") for k, v in said.items()}

    print(f"ФАКТ: {args.label} ({len(d)} ячеек), "
          f"ячеек без единого SQL: {sum(1 for x in d if x['n_sql'] == 0)}")
    print(f"запросов на ячейку: среднее "
          f"{sum(x['n_sql'] for x in d)/len(d):.2f}, всего {sum(x['n_sql'] for x in d)}")
    print(f"справился (>=2 верных из 3): {sum(fact.values())}/{len(fact)}\n")

    cells = Counter()
    print("ПО ВОПРОСАМ")
    for q in sorted(fact):
        cell = (maj[q], fact[q])
        cells[cell] += 1
        name = {("могу", True): "могу + справился",
                ("могу", False): "могу + НЕ справился",
                ("не могу", True): "не могу + справился бы",
                ("не могу", False): "не могу + правда не смог"}[cell]
        print(f"  {q}  {GOF[q]:22s} самооценка {maj[q]:8s} | "
              f"{'справился' if fact[q] else 'не справился':13s} | {name}")

    print("\nЧЕТЫРЕ КЛЕТКИ")
    for cell, name in [(("могу", True), "сказал «могу» и справился"),
                       (("могу", False), "сказал «могу» и НЕ справился"),
                       (("не могу", False), "сказал «не могу» и правда не смог"),
                       (("не могу", True), "сказал «не могу», а справился бы")]:
        print(f"  {name:38s} {cells[cell]:2d}")
    n = len(fact)
    agree = cells[("могу", True)] + cells[("не могу", False)]
    print(f"\n  согласие с фактом: {agree}/{n} ({100*agree/n:.1f}%)")
    n_can = cells[("могу", True)] + cells[("могу", False)]
    n_cant = cells[("не могу", True)] + cells[("не могу", False)]
    p_can = cells[("могу", True)] / n_can if n_can else float("nan")
    p_cant = cells[("не могу", True)] / n_cant if n_cant else float("nan")
    print(f"  доля справившихся среди «могу»:    {100*p_can:5.1f}% (n={n_can})")
    print(f"  доля справившихся среди «не могу»: {100*p_cant:5.1f}% (n={n_cant})")
    print(f"  разрыв: {100*(p_can-p_cant):+.1f} п.п.")

    print("\nПО ГРУППАМ")
    for g, qs in GROUPS.items():
        got = [q for q in qs if q in fact]
        print(f"  {g:22s} справился {sum(fact[q] for q in got)}/{len(got)}  "
              f"| самооценка могу {sum(1 for q in got if maj[q]=='могу')}, "
              f"не могу {sum(1 for q in got if maj[q]=='не могу')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
