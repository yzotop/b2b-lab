#!/usr/bin/env python3
"""Самооценка против свежего факта, с разбивкой по трём группам."""
import json, pathlib, sys
from collections import Counter, defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
GROUPS = {
    "G1 решение объявлено": ["w2a", "w2b", "w2c", "p1a", "p1b", "p1c"],
    "G2 решения нет":       ["w3a", "w3b", "p3a", "p3b", "p2a", "p2b"],
    "G3 не метрика":        ["w1a", "w1b"],
}
GOF = {q: g for g, qs in GROUPS.items() for q in qs}


def facts(path):
    d = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    by = defaultdict(list)
    for x in d:
        by[x["id"]].append(x["verdict"])
    return {k: Counter(v)["верный"] >= 2 for k, v in by.items()}, by


def main() -> int:
    fresh_p = ROOT / "results" / "scored_fresh.jsonl"
    old, old_raw = facts(ROOT / "results" / "scored.jsonl")
    new, new_raw = facts(fresh_p)

    rows = [json.loads(l) for l in
            (ROOT / "selfcheck" / "selfcheck.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    said = defaultdict(list)
    for r in rows:
        said[r["id"]].append(r["verdict"])
    maj = {k: ("могу" if v.count("могу") >= 2 else "не могу") for k, v in said.items()}

    print("СВЕЖИЙ ПРОГОН ПРОТИВ ПРЕЖНЕГО")
    moved = []
    for q in sorted(new):
        if old[q] != new[q]:
            moved.append(q)
        print(f"  {q}  {dict(Counter(old_raw[q]))} -> {dict(Counter(new_raw[q]))}"
              f"{'   <-- сменил статус' if old[q] != new[q] else ''}")
    print(f"\n  справился прежде: {sum(old.values())}/14, свежо: {sum(new.values())}/14")
    print(f"  сменили статус: {len(moved)} — {moved}")

    print("\nПО ГРУППАМ (свежий факт)")
    for g, qs in GROUPS.items():
        ok = sum(new[q] for q in qs)
        can = sum(1 for q in qs if maj[q] == "могу")
        print(f"  {g:22s} справился {ok}/{len(qs)}   «могу» {can}/{len(qs)}, "
              f"«не могу» {len(qs)-can}/{len(qs)}")

    print("\nЧЕТЫРЕ КЛЕТКИ на свежем факте")
    cells = Counter((maj[q], new[q]) for q in new)
    names = {("могу", True): "сказал «могу» и справился",
             ("могу", False): "сказал «могу» и НЕ справился",
             ("не могу", False): "сказал «не могу» и правда не смог",
             ("не могу", True): "сказал «не могу», а справился бы"}
    for cell, name in names.items():
        qs = [q for q in sorted(new) if (maj[q], new[q]) == cell]
        print(f"  {name:36s} {cells[cell]:2d}  {' '.join(qs)}")

    agree = cells[("могу", True)] + cells[("не могу", False)]
    n_can = cells[("могу", True)] + cells[("могу", False)]
    n_cant = cells[("не могу", True)] + cells[("не могу", False)]
    p_can = cells[("могу", True)] / n_can if n_can else float("nan")
    p_cant = cells[("не могу", True)] / n_cant if n_cant else float("nan")
    print(f"\n  согласие с фактом: {agree}/14 ({100*agree/14:.1f}%)")
    print(f"  справились среди «могу»    {100*p_can:5.1f}% (n={n_can})")
    print(f"  справились среди «не могу» {100*p_cant:5.1f}% (n={n_cant})")
    print(f"  разрыв {100*(p_can-p_cant):+.1f} п.п.")

    print("\nОТКАЗЫ ПО ГРУППАМ: прав ли отказ")
    for g, qs in GROUPS.items():
        ref = [q for q in qs if maj[q] == "не могу"]
        verdict = {"G1 решение объявлено": "ОШИБКА — решение в слое есть",
                   "G2 решения нет": "прав — одного числа нет",
                   "G3 не метрика": "прав — величины не существует"}[g]
        if ref:
            print(f"  {g:22s} отказов {len(ref)}: {' '.join(ref)}  -> {verdict}")
        else:
            print(f"  {g:22s} отказов нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
