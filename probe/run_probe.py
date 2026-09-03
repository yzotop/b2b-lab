#!/usr/bin/env python3
"""Второй ход: чем проверять только что данный ответ.

Прогон вопросов идёт как обычно (L3.5, примеры строк, SQL до трёх кругов),
после финального ответа задаётся отдельный вопрос — перечень составляющих
для сверки. SQL на этом ходе не исполняется: спрашивается перечень, а не
счёт.

Разметка здесь не делается; она в probe/score_probe.py по маркерам,
записанным до кода (probe/TRAPS.yaml).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
import duckdb
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))
import run_questions as rq  # noqa: E402

PROBE = """Спасибо. Теперь отдельный ход, считать больше ничего не надо.

Чем этот ответ проверять? Перечисли составляющие, которые надо сверить,
чтобы поймать в нём ошибку. Нумерованным списком, по пункту на
составляющую, каждый пункт — что именно сверить и с чем.

Не оценивай свою уверенность и не пересказывай ответ: назови, что сверять."""


def cell(client, model, context, question, con):
    """Ответ как в обычном прогоне, затем ход про разложение."""
    msgs = [{"role": "user",
             "content": f"{context}\n\n---\n\nВопрос продакта:\n\n{question}"}]
    sqls, results = [], []
    answer, stop = "", None
    for rnd in range(rq.MAX_ROUNDS + 1):
        r = client.messages.create(model=model, max_tokens=rq.MAX_TOKENS,
                                   system=rq.SYSTEM, messages=msgs, timeout=120)
        text = next((b.text for b in r.content
                     if getattr(b, "type", None) == "text"), "")
        stop = r.stop_reason
        qs = rq.extract_sql(text) if rnd < rq.MAX_ROUNDS else []
        if not qs:
            answer = text.strip()
            msgs.append({"role": "assistant", "content": text})
            break
        msgs.append({"role": "assistant", "content": text})
        out = []
        for q in qs:
            sqls.append(q)
            res = rq.run_sql(con, q)
            results.append(res)
            out.append(f"Запрос:\n{q}\n\nРезультат:\n{res}")
        msgs.append({"role": "user", "content": "\n\n".join(out) +
                     "\n\nТеперь ответь продакту текстом, без блока sql."})

    msgs.append({"role": "user", "content": PROBE})
    r2 = client.messages.create(model=model, max_tokens=rq.MAX_TOKENS,
                                system=rq.SYSTEM, messages=msgs, timeout=120)
    probe = next((b.text for b in r2.content
                  if getattr(b, "type", None) == "text"), "").strip()
    return {"answer": answer, "probe": probe, "sql": sqls, "results": results,
            "rounds": len(results), "truncated": stop == "max_tokens",
            "probe_truncated": r2.stop_reason == "max_tokens",
            "stop_reason": stop}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "probe" / "probe.jsonl"))
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY не задан")
    client = anthropic.Anthropic(api_key=key)

    con = duckdb.connect()
    con.execute(f"set file_search_path='{ROOT}'")
    context = rq.build_context(con)

    qs = {}
    for f in sorted((ROOT / "questions").glob("*.yaml")):
        if f.stem == "scoring":
            continue
        qs[f.stem] = yaml.safe_load(f.read_text(encoding="utf-8"))
    if args.only:
        qs = {k: v for k, v in qs.items() if k in args.only}

    out = Path(args.out)
    done = set()
    if out.exists() and out.stat().st_size:
        if not args.resume:
            raise SystemExit(f"{out} не пуст — уберите его")
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                done.add((d["id"], d["repeat"]))

    tasks = [(q, r) for q in qs for r in range(1, args.repeats + 1)
             if (q, r) not in done]
    print(f"к прогону {len(tasks)}, готово {len(done)}, потоков {args.workers}")
    lock = threading.Lock()
    n = {"i": 0}

    def work(task):
        qid, rep = task
        c = duckdb.connect()
        c.execute(f"set file_search_path='{ROOT}'")
        t0 = time.monotonic()
        for attempt in (1, 2, 3):
            try:
                res = cell(client, args.model, context, qs[qid]["question"], c)
                break
            except Exception as e:
                if attempt == 3:
                    res = {"answer": f"СБОЙ: {type(e).__name__}: {e}", "probe": "",
                           "sql": [], "results": [], "rounds": 0,
                           "truncated": False, "probe_truncated": False,
                           "stop_reason": "error"}
                    break
                time.sleep(5 * attempt)
        rec = {"id": qid, "repeat": rep, "scenario": qs[qid]["scenario"],
               "model": args.model, "level": "L3.5",
               "question": qs[qid]["question"], **res,
               "latency_s": round(time.monotonic() - t0, 1),
               "ts": datetime.now(timezone.utc).isoformat()}
        with lock:
            n["i"] += 1
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"  [{n['i']}/{len(tasks)}] {qid} #{rep}  "
                  f"запросов {len(res['sql'])}  {rec['latency_s']}с  "
                  f"перечень {len(res['probe'])} зн.")

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, tasks))
    print(f"\nготово -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
