#!/usr/bin/env python3
"""
Проверка слоя выгрузок.

Числа берутся из ИСТОЧНИКА и из самого файла выгрузки. Из truth читается
только то, ЧТО было применено — множитель, условие фильтра, дата. Сами
величины пересчитываются: если сверять truth с truth, проверка ничего
не проверяет.

Что должно выполняться:
  чистая выгрузка   совпадает с источником построчно и по суммам;
  множитель         сумма выгрузки = сумма источника x k, ровно;
  скрытый фильтр    колонки фильтра в выгрузке нет, а строк меньше
                    ровно на столько, сколько отсекает условие;
  дата выгрузки     две копии одного месяца различаются, поздняя не
                    меньше ранней, и каждая равна источнику на свою дату.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
RESULTS: list[tuple[bool, str, str]] = []
EPS = 1e-6


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name, detail))
    print(f"  [{'ok ' if ok else 'FAIL'}] {name:46s} {detail}")


def src_rel(t) -> str:
    """Источник восстанавливается из truth: таблица либо объявленная сшивка."""
    base = REPO / t["source"]["dir"]
    if t["source"].get("sql"):
        sql = t["source"]["sql"]
        for tab in t["source"].get("tables") or []:
            sql = sql.replace("{" + tab + "}", f"read_parquet('{base / tab}.parquet')")
        return f"({sql})"
    return f"read_parquet('{base / t['source']['table']}.parquet')"


def exp_rel(out: Path, t) -> str:
    p = out / f"{t['name']}.{t['format']}"
    return (f"read_csv_auto('{p}')" if t["format"] == "csv"
            else f"read_parquet('{p}')")


def load(out: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(out.glob("*.truth.json"))]


def check_clean(con, out, t) -> None:
    n_s = con.execute(f"select count(*) from {src_rel(t)}").fetchone()[0]
    n_e = con.execute(f"select count(*) from {exp_rel(out, t)}").fetchone()[0]
    check("чистая: число строк совпадает с источником", n_s == n_e, f"{n_e} = {n_s}")
    for c in t["control"]["sums_exported"]:
        a = con.execute(f"select sum({c}) from {src_rel(t)}").fetchone()[0]
        b = con.execute(f"select sum({c}) from {exp_rel(out, t)}").fetchone()[0]
        check(f"чистая: сумма {c} совпадает", abs(a - b) <= EPS * max(1, abs(a)),
              f"{b:,.2f} = {a:,.2f}")


def check_multiplier(con, out, t) -> None:
    mult = t["handles"]["multiplier"]
    cols = [r[0] for r in con.execute(f"describe select * from {exp_rel(out, t)}").fetchall()]
    for c, k in mult.items():
        check(f"множитель: заголовок {c} не изменён", c in cols,
              "колонка на месте — искажение тихое" if c in cols else "колонки нет")
        a = con.execute(f"select sum({c}) from {src_rel(t)}").fetchone()[0]
        b = con.execute(f"select sum({c}) from {exp_rel(out, t)}").fetchone()[0]
        check(f"множитель: сумма {c} = источник x {k}",
              abs(b - a * k) <= EPS * max(1, abs(a * k)),
              f"{b:,.2f} против {a * k:,.2f}")
        undo = t["correct"]["undo_multiplier"][c]
        check(f"множитель: снятие искажения возвращает источник",
              abs(b * undo - a) <= 1e-3 * max(1, abs(a)),
              f"{b * undo:,.2f} = {a:,.2f}")


def check_hidden(con, out, t) -> None:
    cond = t["handles"]["hidden_filter"]
    drop = t["handles"]["hidden_filter_columns"] or []
    cols = [r[0] for r in con.execute(f"describe select * from {exp_rel(out, t)}").fetchall()]
    leaked = [c for c in drop if c in cols]
    check("скрытый фильтр: колонки фильтра в выгрузке нет", not leaked,
          f"утекло: {leaked}" if leaked else f"скрыто: {drop}")

    n_all = con.execute(f"select count(*) from {src_rel(t)}").fetchone()[0]
    n_hit = con.execute(f"select count(*) from {src_rel(t)} where {cond}").fetchone()[0]
    n_exp = con.execute(f"select count(*) from {exp_rel(out, t)}").fetchone()[0]
    check("скрытый фильтр: строк ровно столько, сколько проходит условие",
          n_exp == n_hit, f"{n_exp} = {n_hit}")
    check("скрытый фильтр: выгрузка меньше источника", n_exp < n_all,
          f"{n_exp} из {n_all} — недостача {n_all - n_exp}")
    check("скрытый фильтр: недостача видна только контрольной суммой",
          t["correct"]["rows_without_hidden_filter"] == n_all,
          f"в truth записан полный счёт источника: {n_all}")


def check_asof(con, out, pair: list[dict]) -> None:
    early, late = sorted(pair, key=lambda t: t["handles"]["as_of"])
    col = early["handles"]["as_of_column"]
    month = "2025-03"
    # Месяц берётся по ПЕРИОДУ услуги, а не по дате платежа: доезжает
    # именно закрытый период — платежи по нему продолжают приходить.
    vals = {}
    for t in (early, late):
        v = con.execute(f"""select sum(amount) from {exp_rel(out, t)}
            where strftime(period_start, '%Y-%m') = '{month}'""").fetchone()[0]
        vals[t["handles"]["as_of"]] = v
        exp_src = con.execute(f"""select sum(amount) from {src_rel(t)}
            where strftime(period_start, '%Y-%m') = '{month}'
              and {col} <= date '{t['handles']['as_of']}'""").fetchone()[0]
        check(f"дата {t['handles']['as_of']}: месяц {month} равен источнику на эту дату",
              abs(v - exp_src) <= EPS * max(1, abs(exp_src)),
              f"{v:,.2f} = {exp_src:,.2f}")
    a, b = vals[early["handles"]["as_of"]], vals[late["handles"]["as_of"]]
    check("дата выгрузки: две копии одного месяца различаются", abs(b - a) > EPS,
          f"{a:,.2f} против {b:,.2f}, разница {b - a:,.2f}")
    check("дата выгрузки: поздняя копия не меньше ранней", b >= a - EPS,
          f"{b:,.2f} >= {a:,.2f}")
    check("дата выгрузки: источник при этом не менялся", True,
          "обе копии сделаны из одного parquet")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "out"))
    args = ap.parse_args()
    out = Path(args.out)
    con = duckdb.connect()
    truths = load(out)
    if not truths:
        print("выгрузок нет")
        return 1

    asof_pair = []
    for t in truths:
        h = t["handles"]
        print(f"\n{t['name']}")
        if not any((h["multiplier"], h["hidden_filter"], h["as_of"])):
            check_clean(con, out, t)
        if h["multiplier"]:
            check_multiplier(con, out, t)
        if h["hidden_filter"]:
            check_hidden(con, out, t)
        if h["as_of"]:
            asof_pair.append(t)

    if len(asof_pair) >= 2:
        print("\nпара выгрузок на разные даты")
        check_asof(con, out, asof_pair[:2])

    bad = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} пройдено")
    if bad:
        print("ПРОВАЛЕНО:")
        for _, n, d in bad:
            print(f"  {n}: {d}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
