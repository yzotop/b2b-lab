#!/usr/bin/env python3
"""Самооценка против факта: четыре клетки."""
import json, pathlib, re, sys
from collections import Counter, defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    sc = [json.loads(l) for l in
          (ROOT / "results" / "scored.jsonl").read_text(encoding="utf-8").splitlines()]
    fact = {}
    by = defaultdict(list)
    for x in sc:
        by[x["id"]].append(x["verdict"])
    for k, v in by.items():
        fact[k] = Counter(v)["верный"] >= 2

    rows = [json.loads(l) for l in
            (ROOT / "selfcheck" / "selfcheck.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    said = defaultdict(list)
    for r in rows:
        said[r["id"]].append(r["verdict"])

    print(f"самооценок: {len(rows)}, не разобрано: {sum(1 for r in rows if not r['verdict'])}")
    c = Counter(r["verdict"] for r in rows)
    print(f"  «могу» {c['могу']} ({100*c['могу']/len(rows):.1f}%), "
          f"«не могу» {c['не могу']} ({100*c['не могу']/len(rows):.1f}%)\n")

    print("ПО ВОПРОСАМ (самооценка 3 повтора | факт)")
    unstable = 0
    cells = Counter()
    for k in sorted(said):
        v = said[k]
        maj = "могу" if v.count("могу") >= 2 else "не могу"
        if len(set(v)) > 1:
            unstable += 1
        cell = (maj, fact[k])
        cells[cell] += 1
        名 = {("могу", True): "могу + справился",
              ("могу", False): "могу + НЕ справился",
              ("не могу", True): "не могу + справился бы",
              ("не могу", False): "не могу + правда не смог"}[cell]
        flag = "  ~нестабильно" if len(set(v)) > 1 else ""
        print(f"  {k}  [{' '.join(x or '?' for x in v)}] -> {maj:8s} | "
              f"{'справился' if fact[k] else 'не справился':13s} | {名}{flag}")

    print(f"\nнестабильных вопросов: {unstable} из {len(said)}")
    print("\nЧЕТЫРЕ КЛЕТКИ (по вопросам, большинство из трёх)")
    for cell, name in [(("могу", True), "сказал «могу» и справился"),
                       (("могу", False), "сказал «могу» и НЕ справился"),
                       (("не могу", False), "сказал «не могу» и правда не смог"),
                       (("не могу", True), "сказал «не могу», а справился бы")]:
        print(f"  {name:38s} {cells[cell]:2d}  {100*cells[cell]/len(said):5.1f}%")

    agree = cells[("могу", True)] + cells[("не могу", False)]
    print(f"\n  согласие самооценки с фактом: {agree}/{len(said)} "
          f"({100*agree/len(said):.1f}%)")
    n_can = cells[("могу", True)] + cells[("могу", False)]
    n_cant = cells[("не могу", True)] + cells[("не могу", False)]
    p_can = cells[("могу", True)] / n_can if n_can else float("nan")
    p_cant = cells[("не могу", True)] / n_cant if n_cant else float("nan")
    print(f"  доля справившихся среди «могу»:     {100*p_can:5.1f}%  (n={n_can})")
    print(f"  доля справившихся среди «не могу»:  {100*p_cant:5.1f}%  (n={n_cant})")
    print(f"  разрыв: {100*(p_can-p_cant):+.1f} п.п. "
          f"(отрицательный = самооценка направлена в обратную сторону)")

    print("\nОТДЕЛЬНАЯ ПРОВЕРКА: назван ли недостающий год в w1a/w1b")
    pat = re.compile(r"год\s+не\s+(назван|указан)|не\s+указано,\s+о\s+каком\s+год|"
                     r"о\s+каком\s+год|уточнени[ея]\s+года|уточнить\s+год|"
                     r"без\s+года\s+неоднозначн", re.I)
    for r in rows:
        if r["id"] in ("w1a", "w1b"):
            hit = pat.search(r["answer"])
            print(f"  {r['id']} #{r['repeat']}: {'НАЗВАН — ' + repr(hit.group()[:40]) if hit else 'не назван'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
