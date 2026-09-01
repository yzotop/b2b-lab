"""Конвейер: считает 20 ячеек по заданным параметрам.

Каждая задача считается своей процедурой. Дефект вносится в одну
процедуру (или во все, вариант D4) и означает снос фильтра года:
вместо даты оплаты payments.paid_at берётся invoices.period_start.
"""
import argparse
import json
import sys
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFECTS = {
    "D0": set(),
    "D1": {"T05"},
    "D2": {"T18"},
    "D3": {"T01"},
    "D4": "все",
}

BASE = """
select round(sum(p.amount), 2)
from 'data/payments.parquet' p
join 'data/invoices.parquet' i on i.invoice_id = p.invoice_id
join 'data/subscriptions.parquet' s on s.sub_id = i.sub_id
join 'data/plans.parquet' pl on pl.plan_id = s.plan_id
join 'data/orgs.parquet' o on o.org_id = p.org_id
where year({date_col}) = {year}
"""

COLS = {"region": "o.region", "channel": "s.channel", "tier": "pl.tier"}


def procedure(con, task, year, defective):
    """Процедура одной задачи. defective — съехал ли фильтр года."""
    date_col = "i.period_start" if defective else "p.paid_at"
    sql = BASE.format(date_col=date_col, year=year)
    for axis, val in task["fix"].items():
        lit = val if isinstance(val, int) else f"'{val}'"
        sql += f"  and {COLS[axis]} = {lit}\n"
    return con.execute(sql).fetchone()[0] or 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--defect", default="D0", choices=sorted(DEFECTS))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    spec = yaml.safe_load((ROOT / "pipeline" / "tasks.yaml").read_text(encoding="utf-8"))
    con = duckdb.connect()
    con.execute(f"set file_search_path='{ROOT}'")

    bad = DEFECTS[args.defect]
    rows = []
    for t in spec["tasks"]:
        defective = (bad == "все") or (t["id"] in bad)
        rows.append({"id": t["id"], "kind": t["kind"], "fix": t["fix"],
                     "value": procedure(con, t, spec["year"], defective),
                     "defective": defective})

    out = Path(args.out or ROOT / "pipeline" / f"cells_{args.defect}.json")
    out.write_text(json.dumps({"defect": args.defect, "year": spec["year"],
                               "cells": rows}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    n = sum(r["defective"] for r in rows)
    print(f"{args.defect}: посчитано {len(rows)} ячеек, съехавших процедур {n} -> {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
