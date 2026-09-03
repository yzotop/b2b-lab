#!/usr/bin/env python3
"""Вмешательство в слой: база -> лечение -> возврат."""
import json, pathlib, re, sys
from collections import Counter, defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
Q = ["w3a", "w3b", "p3a", "p3b", "p2a", "p2b"]
TREATED = {"w3a", "p3a"}
SPILL = {"w3b", "p3b"}


def load(p, only=None):
    rows = [json.loads(l) for l in pathlib.Path(p).read_text(encoding="utf-8").splitlines()
            if l.strip()]
    d = defaultdict(list)
    for r in rows:
        if r["id"] in Q:
            d[r["id"]].append(r)
    return d


def tag(q):
    return "лечён" if q in TREATED else ("побочно" if q in SPILL else "КОНТРОЛЬ")


def main() -> int:
    states = [("база", load(ROOT / "selfcheck" / "selfcheck.jsonl")),
              ("лечение", load(ROOT / "selfcheck" / "sc_treated.jsonl")),
              ("возврат", load(ROOT / "selfcheck" / "sc_restored.jsonl"))]

    print("ВЕРДИКТЫ ПО СОСТОЯНИЯМ (могу / не могу / не разобрано, из 3)")
    print(f"  {'вопрос':6s} {'роль':9s} " + " ".join(f"{n:^22s}" for n, _ in states))
    for q in Q:
        cells = []
        for _, d in states:
            c = Counter(r["verdict"] for r in d[q])
            cells.append(f"могу {c['могу']} / нет {c['не могу']} / ? {c[None]}")
        print(f"  {q:6s} {tag(q):9s} " + " ".join(f"{c:^22s}" for c in cells))

    print("\nСВОДНО по ячейкам (18 на состояние)")
    for n, d in states:
        c = Counter(r["verdict"] for q in Q for r in d[q])
        print(f"  {n:9s} «могу» {c['могу']:2d}   «не могу» {c['не могу']:2d}   "
              f"не разобрано {c[None]}")

    print("\nПРИЧИНА ОТКАЗА ДОСЛОВНО — база")
    for q in Q:
        for r in states[0][1][q][:1]:
            m = re.search(r"(?:^|\n)#*\s*2[.)]?\s*Чего не хватает(.{0,420})", r["answer"], re.S)
            txt = (m.group(1) if m else r["answer"][-420:]).strip()
            print(f"\n  --- {q} ({tag(q)}) ---\n  " + txt.replace("\n", "\n  ")[:420])

    print("\n\nПРИЧИНА ОТКАЗА ДОСЛОВНО — возврат")
    for q in Q:
        ref = [r for r in states[2][1][q] if r["verdict"] == "не могу"]
        if not ref:
            print(f"\n  --- {q} ({tag(q)}) --- ОТКАЗ НЕ ВЕРНУЛСЯ")
            continue
        m = re.search(r"(?:^|\n)#*\s*2[.)]?\s*Чего не хватает(.{0,420})", ref[0]["answer"], re.S)
        txt = (m.group(1) if m else ref[0]["answer"][-420:]).strip()
        print(f"\n  --- {q} ({tag(q)}) ---\n  " + txt.replace("\n", "\n  ")[:420])

    print("\n\nССЫЛАЕТСЯ ЛИ ОТКАЗ НА СЛОЙ (доля отказов, где назван слой)")
    pat = re.compile(r"семантическ\w+ сло|в слое|metrics\.yml|не объявлен|"
                     r"намеренно не|решени\w+ владельц|владелец", re.I)
    for n, d in states:
        ref = [r for q in Q for r in d[q] if r["verdict"] == "не могу"]
        hit = sum(1 for r in ref if pat.search(r["answer"]))
        print(f"  {n:9s} отказов {len(ref):2d}, со ссылкой на слой {hit:2d}"
              + (f" ({100*hit/len(ref):.0f}%)" if ref else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
