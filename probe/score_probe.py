#!/usr/bin/env python3
"""Разметка перечней «чем проверять» по маркерам из probe/TRAPS.yaml.

Маркеры записаны до кода раннера и до прогона. Здесь они только
применяются. Сверка идёт с ловушкой сюжета по построению, а не с числами
эталонов.

Старшинство объявлено в probe/HYPOTHESIS.md: маркер, входящий в развилку
своего сюжета, соседним для этого сюжета не считается.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter, defaultdict

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+(.+)$", re.M)


def hit(text: str, groups) -> bool:
    t = text.lower()
    return any(all(m.lower() in t for m in g) for g in (groups or []))


def which(text: str, groups) -> list:
    t = text.lower()
    return [" + ".join(g) for g in (groups or []) if all(m.lower() in t for m in g)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probes", default=str(ROOT / "probe" / "probe.jsonl"))
    ap.add_argument("--scored", default=str(ROOT / "probe" / "scored_probe.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "probe" / "probe_scored.jsonl"))
    args = ap.parse_args()

    traps = yaml.safe_load((ROOT / "probe" / "TRAPS.yaml").read_text(encoding="utf-8"))
    forks, neigh = traps["forks"], traps["neighbours"]

    own_answer = {}
    p = pathlib.Path(args.scored)
    if p.exists():
        for l in p.read_text(encoding="utf-8").splitlines():
            if l.strip():
                d = json.loads(l)
                own_answer[(d["id"], d["repeat"])] = d["verdict"]

    rows = [json.loads(l) for l in
            pathlib.Path(args.probes).read_text(encoding="utf-8").splitlines() if l.strip()]

    out = []
    for r in rows:
        sc = r["scenario"]
        text = r["probe"]
        items = [m.group(1).strip() for m in ITEM.finditer(text)]
        own_groups = [tuple(g) for g in forks[sc]["fork"]]
        fork_hits = which(text, forks[sc]["fork"])
        nb = {}
        for name, groups in neigh.items():
            got = [g for g in which(text, groups)
                   if tuple(g.split(" + ")) not in own_groups]
            if got:
                nb[name] = got
        if fork_hits:
            kind = "развилка"
        elif nb:
            kind = "соседнее"
        else:
            kind = "ни то ни другое"
        out.append({"id": r["id"], "repeat": r["repeat"], "scenario": sc,
                    "named": len(items) >= 2, "n_items": len(items),
                    "items": items, "fork": bool(fork_hits),
                    "fork_markers": fork_hits, "neighbours": nb,
                    "kind": kind,
                    "answer_verdict": own_answer.get((r["id"], r["repeat"])),
                    "probe_truncated": r.get("probe_truncated", False),
                    "n_sql": len(r["sql"])})
    pathlib.Path(args.out).write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in out) + "\n", encoding="utf-8")

    n = len(out)
    print(f"ячеек: {n}\n")
    named = sum(x["named"] for x in out)
    counts = sorted(x["n_items"] for x in out)
    med = counts[n // 2] if n % 2 else (counts[n // 2 - 1] + counts[n // 2]) / 2
    print("Г1 перечень назван (2+ пункта): "
          f"{named}/{n}  (предсказано >=38)")
    print(f"Г2 медиана пунктов: {med}  (предсказано 3-6), "
          f"мин {counts[0]}, макс {counts[-1]}")
    fork = sum(x["fork"] for x in out)
    print(f"Г3 развилка своего сюжета названа: {fork}/{n}  (предсказано 20-30)")
    w1 = [x for x in out if x["scenario"] == "world/1"]
    print(f"Г4 w1: развилка названа в {sum(x['fork'] for x in w1)}/{len(w1)}  "
          f"(предсказано <=1)")
    nb_any = sum(1 for x in out if x["neighbours"])
    print(f"Г6 соседняя ручка названа: {nb_any}/{n}  (предсказано >=15)")

    print("\nПО СЮЖЕТАМ")
    by = defaultdict(list)
    for x in out:
        by[x["scenario"]].append(x)
    for sc in sorted(by):
        g = by[sc]
        k = Counter(x["kind"] for x in g)
        print(f"  {sc:9s} n={len(g)}  развилка {sum(x['fork'] for x in g)}  "
              f"| соседнее {k['соседнее']}  ни то ни другое {k['ни то ни другое']}  "
              f"| пунктов медиана {sorted(x['n_items'] for x in g)[len(g)//2]}")

    print("\nПО ВОПРОСАМ")
    byq = defaultdict(list)
    for x in out:
        byq[x["id"]].append(x)
    for q in sorted(byq):
        g = byq[q]
        print(f"  {q}  развилка {sum(x['fork'] for x in g)}/{len(g)}  "
              f"маркеры: {sorted({m for x in g for m in x['fork_markers']})}")

    if own_answer:
        print("\nГ5 (в пределах ЭТОГО прогона; объявлено было против разметки 01.09)")
        bad = [x for x in out if x["answer_verdict"] and x["answer_verdict"] != "верный"]
        print(f"  ячеек с не-верным ответом: {len(bad)}, "
              f"из них развилка названа в перечне: {sum(x['fork'] for x in bad)}")
        good = [x for x in out if x["answer_verdict"] == "верный"]
        print(f"  ячеек с верным ответом:     {len(good)}, "
              f"из них развилка названа: {sum(x['fork'] for x in good)}")

    print("\nСОСЕДНИЕ РУЧКИ, СКОЛЬКО РАЗ")
    cc = Counter(k for x in out for k in x["neighbours"])
    for k, v in cc.most_common():
        print(f"  {k:22s} {v}")
    tr = sum(x["probe_truncated"] for x in out)
    print(f"\nобрывов перечня по max_tokens: {tr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
