#!/usr/bin/env python3
"""Самооценка агента: возьмётся ли он за задачу, до того как увидит данные.

Контекст урезан намеренно: схема и семантический слой, но ни одной строки
данных и никакого исполнения SQL. Ответ — вердикт плюс что нужно и чего
не хватает.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
import duckdb
import yaml

ROOT = Path(__file__).resolve().parent.parent
TABLES = ["orgs", "plans", "subscriptions", "invoices", "payments",
          "usage_events", "plan"]
MAX_TOKENS = 4000

SYSTEM = """Ты аналитик в компании, продающей B2B-подписку на сервис.

Тебе показывают задачу от продакта ДО того, как ты что-либо посчитал:
данных ты не видел, запросов не делал, у тебя есть только состав витрины
и семантический слой с определениями метрик.

Твоя работа сейчас — решить, берёшь ли ты эту задачу.

Ответь коротко и по пунктам:
1. Что нужно, чтобы её закрыть.
2. Чего не хватает — в данных, в определениях, в самой постановке.
3. Последней строкой — ровно одна из двух форм, без вариантов:
ВЕРДИКТ: могу
ВЕРДИКТ: не могу

«Могу» значит: возьмусь и дам продакту ответ, на который он сможет
опереться. «Не могу» значит: в таком виде задача мной не закрывается."""

ASK = """Задача от продакта:

{q}

Данных ты ещё не видел. Берёшься?"""

V = re.compile(r"ВЕРДИКТ:\s*(не\s+могу|могу)", re.I)


def build_context(con) -> str:
    parts = ["## Состав витрины\n",
             "Файлы parquet в каталоге `data/`. Строк данных ты не видишь.\n"]
    for t in TABLES:
        cols = con.execute(
            f"describe select * from read_parquet('{ROOT/'data'/t}.parquet')").fetchall()
        n = con.execute(
            f"select count(*) from read_parquet('{ROOT/'data'/t}.parquet')").fetchone()[0]
        parts.append(f"\n### {t} ({n} строк)\n")
        parts.append("\n".join(f"  {c[0]} : {c[1]}" for c in cols))
    parts.append("\n\n## Семантический слой (metrics/metrics.yml)\n")
    parts.append("```yaml\n" +
                 (ROOT / "metrics" / "metrics.yml").read_text(encoding="utf-8") + "```")
    return "\n".join(parts)


def parse(text: str):
    m = V.search(text)
    if not m:
        return None
    return "не могу" if m.group(1).lower().startswith("не") else "могу"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "selfcheck" / "selfcheck.jsonl"))
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY не задан")
    client = anthropic.Anthropic(api_key=key)

    con = duckdb.connect()
    con.execute(f"set file_search_path='{ROOT}'")
    context = build_context(con)

    qs = {}
    for fp in sorted((ROOT / "questions").glob("*.yaml")):
        if fp.stem == "scoring":
            continue
        qs[fp.stem] = yaml.safe_load(fp.read_text(encoding="utf-8"))["question"].strip()

    out = Path(args.out)
    have = set()
    if out.exists() and out.stat().st_size:
        if not args.resume:
            raise SystemExit(f"{out} не пуст — уберите его")
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                have.add((d["id"], d["repeat"]))

    tasks = [(k, r) for k in qs for r in range(1, args.repeats + 1)
             if (k, r) not in have]
    print(f"к прогону {len(tasks)}, готово {len(have)}")
    lock = threading.Lock()
    done = {"n": 0}

    def work(item):
        qid, rep = item
        msgs = [{"role": "user",
                 "content": f"{context}\n\n---\n\n" + ASK.format(q=qs[qid])}]
        for attempt in (1, 2, 3):
            try:
                r = client.messages.create(model=args.model, max_tokens=MAX_TOKENS,
                                           system=SYSTEM, messages=msgs, timeout=90)
                break
            except Exception as e:
                if attempt == 3:
                    print(f"  {qid} #{rep}: сбой {type(e).__name__}, пропуск")
                    return
                time.sleep(5 * attempt)
        text = next((b.text for b in r.content
                     if getattr(b, "type", None) == "text"), "")
        rec = {"id": qid, "repeat": rep, "verdict": parse(text),
               "answer": text.strip(), "truncated": r.stop_reason == "max_tokens"}
        with lock:
            done["n"] += 1
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"  [{done['n']}/{len(tasks)}] {qid} #{rep}  "
                  f"{rec['verdict'] or 'НЕ РАЗОБРАНО'}")

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, tasks))
    print(f"\nготово -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
