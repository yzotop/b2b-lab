#!/usr/bin/env python3
"""
Прогон вопросов через агента-аналитика.

Уровень контекста L3.5, как в ритейловой сетке продакта: определения
метрик даны ТЕКСТОМ (metrics.yml целиком), а SQL агент пишет сам.
Не L4 — там запрос собирается ИЗ слоя, и на вопросах, чьей метрики
в слое нет, агент физически не может ответить; четыре сюжета из шести
упёрлись бы в отказ по построению.

Петля: агент пишет SQL -> исполняем в DuckDB -> результат обратно ->
максимум три круга -> финальный ответ словами. Ошибка запроса тоже
возвращается агенту: чинить свой SQL это часть работы.

Оценивание здесь НЕ делается. Сюда пишется только то, что агент сказал
и какие запросы исполнил. Разметка — runner/score.py по правилу,
записанному до прогона (questions/scoring.yaml).

Возобновление: --resume пропускает уже сделанные ячейки.
"""

from __future__ import annotations

import argparse
import json
import threading
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import duckdb
import yaml

ROOT = Path(__file__).resolve().parent.parent
TABLES = ["orgs", "plans", "subscriptions", "invoices", "payments",
          "usage_events", "plan"]
MAX_ROUNDS = 3
# 2500 не хватает: у моделей 5-го поколения блок размышления съедает
# бюджет, ответ обрывается посреди ```sql, блок не закрывается и не
# распознаётся — агент выглядит «не написавшим ни одного запроса».
MAX_TOKENS = 8000


def build_context(con) -> str:
    """Слои схемы, колонок и значений плюс семантический слой текстом."""
    parts = ["## Схема данных\n",
             "Файлы parquet в каталоге `data/`. Читать так: "
             "`select * from read_parquet('data/payments.parquet')`.\n"]
    for t in TABLES:
        cols = con.execute(
            f"describe select * from read_parquet('{ROOT/'data'/t}.parquet')").fetchall()
        n = con.execute(
            f"select count(*) from read_parquet('{ROOT/'data'/t}.parquet')").fetchone()[0]
        parts.append(f"\n### {t} ({n} строк)\n")
        parts.append("\n".join(f"  {c[0]} : {c[1]}" for c in cols))
        if SHOW_SAMPLES:
            sample = con.execute(
                f"select * from read_parquet('{ROOT/'data'/t}.parquet') limit 2").fetchall()
            parts.append("\n  примеры строк:")
            for row in sample:
                parts.append("    " + " | ".join(str(x)[:28] for x in row))
    parts.append("\n\n## Семантический слой (metrics/metrics.yml)\n")
    parts.append("```yaml\n" + METRICS_PATH.read_text(encoding="utf-8") + "```")
    return "\n".join(parts)


SYSTEM = """Ты аналитик в компании, продающей B2B-подписку на сервис.
Тебе задаёт вопрос продакт — человек, который примет по твоему ответу решение.

У тебя есть данные в parquet и семантический слой с определениями метрик.
SQL ты пишешь сам.

Как работать:
- чтобы посчитать, выдай запрос в блоке ```sql ... ```; можно несколько;
- я исполню их и верну результат;
- после этого либо считай дальше, либо отвечай;
- финальный ответ дай обычным текстом, без блока sql.

Отвечай продакту по-человечески и по существу его вопроса. Если считаешь,
что вопрос нельзя корректно закрыть одним числом, скажи это прямо."""


def extract_sql(text: str) -> list[str]:
    return [m.strip() for m in re.findall(r"```sql\s*(.+?)```", text, re.S | re.I)]


def run_sql(con, q: str) -> str:
    try:
        rows = con.execute(q).fetchmany(30)
        cols = [d[0] for d in con.description] if con.description else []
        head = " | ".join(cols)
        body = "\n".join(" | ".join(str(x)[:40] for x in r) for r in rows)
        return f"{head}\n{body}" if body else f"{head}\n(пусто)"
    except Exception as e:
        return f"ОШИБКА: {type(e).__name__}: {e}"


def ask(client, model: str, context: str, question: str, con) -> dict:
    msgs = [{"role": "user",
             "content": f"{context}\n\n---\n\nВопрос продакта:\n\n{question}"}]
    sqls, results = [], []
    for _ in range(MAX_ROUNDS):
        r = client.messages.create(model=model, max_tokens=MAX_TOKENS, system=SYSTEM,
                                   messages=msgs, timeout=120)
        text = next((b.text for b in r.content if getattr(b, "type", None) == "text"), "")
        truncated = r.stop_reason == "max_tokens"
        qs = extract_sql(text)
        if not qs:
            return {"answer": text.strip(), "sql": sqls, "results": results,
                    "rounds": len(results), "truncated": truncated,
                    "stop_reason": r.stop_reason}
        msgs.append({"role": "assistant", "content": text})
        out = []
        for q in qs:
            sqls.append(q)
            res = run_sql(con, q)
            results.append(res)
            out.append(f"Запрос:\n{q}\n\nРезультат:\n{res}")
        msgs.append({"role": "user", "content": "\n\n".join(out) +
                     "\n\nТеперь ответь продакту текстом, без блока sql."})
    r = client.messages.create(model=model, max_tokens=MAX_TOKENS, system=SYSTEM,
                               messages=msgs, timeout=120)
    text = next((b.text for b in r.content if getattr(b, "type", None) == "text"), "")
    return {"answer": text.strip(), "sql": sqls, "results": results,
            "rounds": MAX_ROUNDS, "truncated": r.stop_reason == "max_tokens",
            "stop_reason": r.stop_reason}


METRICS_PATH = ROOT / "metrics" / "metrics.yml"
# Примеры строк выключаются, чтобы контекст ответа совпал с контекстом
# самооценки (selfcheck/run_selfcheck.py): там строк данных не давалось.
SHOW_SAMPLES = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--out", default=str(ROOT / "results" / "answers.jsonl"))
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--metrics", default=str(ROOT / "metrics" / "metrics.yml"))
    ap.add_argument("--no-samples", action="store_true")
    args = ap.parse_args()

    global METRICS_PATH, SHOW_SAMPLES
    METRICS_PATH = Path(args.metrics)
    SHOW_SAMPLES = not args.no_samples

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY не задан")
    client = anthropic.Anthropic(api_key=key)
    con = duckdb.connect()
    con.execute(f"set file_search_path='{ROOT}'")
    context = build_context(con)

    qs = {}
    for f in sorted((ROOT / "questions").glob("*.yaml")):
        if f.stem == "scoring":
            continue
        qs[f.stem] = yaml.safe_load(f.read_text(encoding="utf-8"))
    if args.only:
        qs = {k: v for k, v in qs.items() if k in args.only}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if args.resume and out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                done.add((d["id"], d["repeat"]))

    tasks = [(qid, rep) for qid in qs for rep in range(1, args.repeats + 1)
             if (qid, rep) not in done]
    total = len(qs) * args.repeats
    print(f"к прогону: {len(tasks)} ячеек из {total} "
          f"(пропущено готовых: {total - len(tasks)}), потоков {args.workers}\n")
    lock = threading.Lock()
    counter = {"n": 0}

    def cell(task):
        qid, rep = task
        # Своё соединение на поток: одно duckdb-соединение не рассчитано
        # на параллельные execute.
        c = duckdb.connect()
        c.execute(f"set file_search_path='{ROOT}'")
        t0 = time.monotonic()
        try:
            res = ask(client, args.model, context, qs[qid]["question"], c)
        except Exception as e:
            res = {"answer": f"СБОЙ: {type(e).__name__}: {e}", "sql": [],
                   "results": [], "rounds": 0, "truncated": False,
                   "stop_reason": "error"}
        rec = {"id": qid, "repeat": rep, "scenario": qs[qid]["scenario"],
               "model": args.model, "level": "L3.5",
               "question": qs[qid]["question"], **res,
               "latency_s": round(time.monotonic() - t0, 1),
               "ts": datetime.now(timezone.utc).isoformat()}
        with lock:
            counter["n"] += 1
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"  [{counter['n']}/{len(tasks)}] {qid} #{rep}  "
                  f"запросов {len(res['sql'])}  {rec['latency_s']}с  "
                  f"{res['answer'][:56].replace(chr(10),' ')}…")

    with open(out, "a", encoding="utf-8") as fh:
        if args.workers > 1:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                list(pool.map(cell, tasks))
        else:
            for t in tasks:
                cell(t)
    print(f"\nготово: {counter['n']} ячеек -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
