#!/usr/bin/env python3
"""Разметка агентских ячеек и прогон инвариантов по ним."""
import json
import sys
from collections import Counter
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parent.parent
TOL = 0.005          # относительный допуск совпадения с эталоном
INV_TOL = 0.01       # абсолютный допуск сходимости инварианта

BASE = """
select round(sum({amt}), 2)
from 'data/payments.parquet' p
join 'data/invoices.parquet' i on i.invoice_id = p.invoice_id
join 'data/subscriptions.parquet' s on s.sub_id = i.sub_id
join 'data/plans.parquet' pl on pl.plan_id = s.plan_id
join 'data/orgs.parquet' o on o.org_id = p.org_id
where year(p.paid_at) = {year}
"""
COLS = {"region": "o.region", "channel": "s.channel", "tier": "pl.tier"}


def truth(con, spec, amt):
    out = {}
    for t in spec["tasks"]:
        sql = BASE.format(amt=amt, year=spec["year"])
        for a, v in t["fix"].items():
            sql += f"  and {COLS[a]} = {v if isinstance(v, int) else repr(v)}\n"
        out[t["id"]] = float(con.execute(sql).fetchone()[0] or 0)
    return out


def close(a, b):
    return b and abs(a - b) / abs(b) < TOL


def main() -> int:
    spec = yaml.safe_load((ROOT / "pipeline" / "tasks.yaml").read_text(encoding="utf-8"))
    con = duckdb.connect()
    con.execute(f"set file_search_path='{ROOT}'")
    g_net = truth(con, spec, "p.amount")
    g_gross = truth(con, spec, "i.amount_list * (p.amount / nullif(i.amount_due,0))")

    rows = [json.loads(l) for l in
            (ROOT / "pipeline" / "agent_cells.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    reps = sorted({r["repeat"] for r in rows})

    print(f"эталоны: net итог {g_net['T01']:,.2f}   gross итог {g_gross['T01']:,.2f} "
          f"(разница {100*(g_gross['T01']/g_net['T01']-1):.1f}%)\n")

    verdicts = {}
    for r in rows:
        v = r["value"]
        if v is None:
            k = "нет числа"
        elif close(v, g_net[r["id"]]):
            k = "net"
        elif close(v, g_gross[r["id"]]):
            k = "gross"
        else:
            k = "мимо"
        verdicts[(r["id"], r["repeat"])] = k

    print("РАЗМЕТКА ЯЧЕЕК")
    c = Counter(verdicts.values())
    for k in ("net", "gross", "мимо", "нет числа"):
        print(f"  {k:10s} {c[k]:3d}  {100*c[k]/len(verdicts):5.1f}%")
    bad = c["мимо"] + c["нет числа"]
    print(f"  не совпало ни с одним эталоном: {bad}/{len(verdicts)} "
          f"({100*bad/len(verdicts):.1f}%)")

    print("\n  по ячейкам (повтор 1 / повтор 2):")
    for t in spec["tasks"]:
        vs = [verdicts.get((t["id"], rp), "—") for rp in reps]
        mark = "  <-- расходятся" if len(set(vs)) > 1 else ""
        print(f"    {t['id']} {t['kind']:7s} {' / '.join(vs)}{mark}")

    print("\nИНВАРИАНТЫ ПО ОСЯМ, отдельно на каждом повторе")
    for rp in reps:
        val = {r["id"]: r["value"] for r in rows if r["repeat"] == rp}
        print(f"\n  повтор {rp}:")
        missing = [t["id"] for t in spec["tasks"] if val.get(t["id"]) is None]
        if missing:
            print(f"    ячейки без числа: {missing} — складывать нечего")
        broken = []
        for inv in spec["invariants"]:
            if any(val.get(x) is None for x in inv["parts"] + [inv["total"]]):
                print(f"    {inv['id']} {inv['name']:26s} НЕ ВЫЧИСЛИМ")
                continue
            s = round(sum(val[p] for p in inv["parts"]), 2)
            tot = val[inv["total"]]
            d = round(s - tot, 2)
            ok = abs(d) < INV_TOL
            print(f"    {inv['id']} {inv['name']:26s} расхождение {d:>16,.2f}  "
                  f"{'сходится' if ok else 'РАСХОЖДЕНИЕ'}")
            if not ok:
                broken.append(inv)
        wrong = [t["id"] for t in spec["tasks"]
                 if verdicts.get((t["id"], rp)) in ("мимо", "нет числа")]
        print(f"    неверных ячеек на самом деле: {len(wrong)} — {wrong}")
        if not broken:
            print("    ВЕРДИКТ проверки: расхождений нет"
                  f"{'  <-- ПРОМОЛЧАЛА' if wrong else ''}")
            continue
        sus = None
        for inv in broken:
            s = set(inv["parts"]) | {inv["total"]}
            sus = s if sus is None else (sus & s)
        print(f"    ВЕРДИКТ проверки: разошлось {len(broken)}/3, "
              f"подозреваемых {len(sus)} — {sorted(sus)}")
        hit = [w for w in wrong if w in sus]
        print(f"    из них действительно неверных: {len(hit)} — {hit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
