#!/usr/bin/env python3
"""
Разметка ответов по правилу, записанному ДО прогона.

Правило — questions/scoring.yaml, коммит 9c6bd50, раньше раннера.
Здесь оно только применяется: ни одного маркера после прогона не
добавлено и не снято. Если бы добавил — это была бы подгонка разметки
под ответы, и весь замер потерял бы смысл.

Четыре исхода, объявлены в questions/HYPOTHESIS.md:
  верный   — совпал с эталоном truth
  наивный  — попал в предсказанную ловушку naive
  мимо     — ни то, ни другое
  уточнил  — вопрос вместо ответа, и ответа по существу нет

Порядок разбора: уточнил -> верный -> наивный -> мимо.
Ячейки, где сработали ОБА набора маркеров, помечаются спорными
и выводятся списком для чтения глазами.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def hit(text: str, groups) -> bool:
    t = text.lower()
    return any(all(m.lower() in t for m in g) for g in (groups or []))


def classify(ans: str, rule: dict, common: dict) -> tuple[str, bool]:
    t = ans.lower()
    truth = hit(t, rule.get("truth"))
    naive = hit(t, rule.get("naive"))
    if not truth and not naive:
        # Уточнение засчитывается только при отсутствии ответа по существу.
        if any(all(m.lower() in t for m in g) for g in common["clarify_any"][:-1]):
            return "уточнил", False
    if truth and naive:
        return "верный", True          # спорная: решает чтение глазами
    if truth:
        return "верный", False
    if naive:
        return "наивный", False
    return "мимо", False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--answers", default=str(ROOT / "results" / "answers.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "results" / "scored.jsonl"))
    args = ap.parse_args()

    rules = yaml.safe_load((ROOT / "questions" / "scoring.yaml").read_text(encoding="utf-8"))
    qs = {}
    for f in sorted((ROOT / "questions").glob("*.yaml")):
        if f.stem != "scoring":
            qs[f.stem] = yaml.safe_load(f.read_text(encoding="utf-8"))

    # Сюжеты, закрытые семантическим слоем. Записано до прогона в HYPOTHESIS.md.
    COVERED = {"world/2", "plan/1"}

    recs = [json.loads(l) for l in Path(args.answers).read_text(encoding="utf-8").splitlines() if l.strip()]
    out, disputed = [], []
    for r in recs:
        verdict, disp = classify(r["answer"], rules["questions"][r["id"]], rules)
        r2 = {**{k: r[k] for k in ("id", "repeat", "scenario", "model")},
              "verdict": verdict, "disputed": disp,
              "covered": r["scenario"] in COVERED,
              "n_sql": len(r["sql"]), "truncated": r.get("truncated", False),
              "answer": r["answer"]}
        out.append(r2)
        if disp:
            disputed.append(r2)
    Path(args.out).write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in out) + "\n",
                              encoding="utf-8")

    n = len(out)
    print(f"ячеек: {n}\n")
    c = Counter(x["verdict"] for x in out)
    print("ИСХОДЫ")
    for k in ("верный", "наивный", "мимо", "уточнил"):
        print(f"  {k:10s} {c[k]:3d}  {c[k]/n:5.1%}")

    print("\nПО СЮЖЕТАМ")
    by = defaultdict(Counter)
    for x in out:
        by[x["scenario"]][x["verdict"]] += 1
    for sc in sorted(by):
        t = sum(by[sc].values())
        mark = "закрыт слоем" if sc in COVERED else "открыт"
        print(f"  {sc:9s} {mark:14s} верных {by[sc]['верный']}/{t}  "
              f"наивных {by[sc]['наивный']}  мимо {by[sc]['мимо']}  "
              f"уточнил {by[sc]['уточнил']}")

    print("\nГРАНИЦА «ЗАКРЫТО СЛОЕМ»")
    for cov, label in ((True, "закрыто (w2, p1)"), (False, "открыто (w1, w3, p2, p3)")):
        g = [x for x in out if x["covered"] == cov]
        cc = Counter(x["verdict"] for x in g)
        print(f"  {label:26s} n={len(g):3d}  верных {cc['верный']/len(g):5.1%}  "
              f"наивных {cc['наивный']/len(g):5.1%}  мимо {cc['мимо']/len(g):5.1%}")

    print("\nПО ВОПРОСАМ")
    byq = defaultdict(Counter)
    for x in out:
        byq[x["id"]][x["verdict"]] += 1
    for q in sorted(byq):
        v = byq[q]
        print(f"  {q}  верных {v['верный']}  наивных {v['наивный']}  "
              f"мимо {v['мимо']}  уточнил {v['уточнил']}")

    tr = sum(1 for x in out if x["truncated"])
    print(f"\nобрывов по max_tokens: {tr}")
    print(f"спорных (сработали оба набора маркеров): {len(disputed)}")
    for d in disputed:
        print(f"  {d['id']} #{d['repeat']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
