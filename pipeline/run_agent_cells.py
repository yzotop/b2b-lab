#!/usr/bin/env python3
"""20 задач конвейера считает агент, а не моя процедура.

Контекст L3.5, тот же, что в runner/run_questions.py. Рецепт сведения
с осями не даётся: дефект должен возникнуть сам.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
import duckdb
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))
import run_questions as rq  # noqa: E402  (переиспользуем контекст и петлю SQL)

ROOT = Path(__file__).resolve().parent.parent

SYSTEM = """Ты аналитик в компании, продающей B2B-подписку на сервис.
Тебе дают задачу на пересчёт одной ячейки отчёта. Данные в parquet,
определения метрик — в семантическом слое. SQL пишешь сам.

Как работать:
- чтобы посчитать, выдай запрос в блоке ```sql ... ```; можно несколько;
- я исполню их и верну результат;
- потом либо считай дальше, либо отвечай.

Финальный ответ — одно число рублей выручки, без пробелов внутри числа,
в отдельной строке вида:
ОТВЕТ: 123456789.00
Перед ним можешь одной фразой сказать, как считал."""

ASK = """Задача {tid}. Посчитай выручку за {year} год{scope}.

Нужна одна величина."""

NUM = re.compile(r"ОТВЕТ:\s*([-+]?[\d\s]*\.?\d+)")
RU = {"region": "региону", "channel": "каналу", "tier": "тарифу"}


def scope_words(fix: dict) -> str:
    if not fix:
        return " целиком по компании, без разбивки"
    parts = [f"по {RU[k]} {v}" for k, v in fix.items()]
    return " " + " и ".join(parts)


def parse(text: str):
    m = NUM.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(" ", ""))
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "pipeline" / "agent_cells.jsonl"))
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    import os
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY не задан")
    client = anthropic.Anthropic(api_key=key)

    rq.METRICS_PATH = ROOT / "metrics" / "metrics.yml"
    con0 = duckdb.connect()
    con0.execute(f"set file_search_path='{ROOT}'")
    context = rq.build_context(con0)

    spec = yaml.safe_load((ROOT / "pipeline" / "tasks.yaml").read_text(encoding="utf-8"))
    out = Path(args.out)
    have = set()
    if out.exists() and out.stat().st_size:
        if not args.resume:
            raise SystemExit(f"{out} не пуст — уберите его, чтобы не смешать прогоны")
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                have.add((d["id"], d["repeat"]))

    tasks = [(t, r) for t in spec["tasks"] for r in range(1, args.repeats + 1)
             if (t["id"], r) not in have]
    print(f"к прогону {len(tasks)}, уже готово {len(have)}")
    lock = threading.Lock()
    local = threading.local()
    done = {"n": 0}

    def work(item):
        t, rep = item
        if not hasattr(local, "con"):
            local.con = duckdb.connect()
            local.con.execute(f"set file_search_path='{ROOT}'")
        rq_system = rq.SYSTEM
        rq.SYSTEM = SYSTEM
        try:
            q = ASK.format(tid=t["id"], year=spec["year"], scope=scope_words(t["fix"]))
            for attempt in (1, 2, 3):
                try:
                    res = rq.ask(client, args.model, context, q, local.con)
                    break
                except Exception as e:
                    if attempt == 3:
                        print(f"  {t['id']} #{rep}: сбой {type(e).__name__}, пропуск")
                        return
                    time.sleep(5 * attempt)
        finally:
            rq.SYSTEM = rq_system
        rec = {"id": t["id"], "repeat": rep, "kind": t["kind"], "fix": t["fix"],
               "value": parse(res["answer"]), "answer": res["answer"],
               "sql": res["sql"], "truncated": res["truncated"]}
        with lock:
            done["n"] += 1
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            v = "—" if rec["value"] is None else f"{rec['value']:,.2f}"
            print(f"  [{done['n']}/{len(tasks)}] {t['id']} #{rep}  {v}")

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, tasks))
    print(f"\nготово: {len(tasks)} ячеек -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
