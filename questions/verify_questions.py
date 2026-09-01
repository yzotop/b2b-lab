#!/usr/bin/env python3
"""
Проверка вопросов и эталонов.

Числа, напечатанные в эталонах, пересчитываются по данным. Эталон,
разошедшийся с миром, хуже отсутствующего: он выглядит проверенным.

Проверяется также структура: у каждого сюжета два-три вопроса, у
каждого вопроса оба эталона заполнены, форма нейтральная.

Прогонов здесь нет и не будет: этот файл сверяет эталоны с данными,
а не ответы модели с эталонами.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parent.parent
QDIR = Path(__file__).resolve().parent
RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name, detail))
    print(f"  [{'ok ' if ok else 'FAIL'}] {name:52s} {detail}")


def load() -> dict:
    return {f.stem: yaml.safe_load(f.read_text(encoding="utf-8"))
            for f in sorted(QDIR.glob("*.yaml"))}


def check_structure(qs: dict) -> None:
    check("вопросов заведено", len(qs) >= 12, f"{len(qs)}")
    per = Counter(q["scenario"] for q in qs.values())
    check("сюжетов покрыто шесть", len(per) == 6, ", ".join(sorted(per)))
    bad = {k: v for k, v in per.items() if not 2 <= v <= 3}
    check("на каждый сюжет два-три вопроса", not bad, str(dict(per)))
    empty = [k for k, q in qs.items()
             if not all(q["truth"].get(x) for x in ("answer", "decision_rule", "known_by"))
             or not all(q["naive"].get(x) for x in ("answer", "why"))]
    check("оба эталона заполнены везде", not empty, f"пусто у: {empty}" if empty else "14 из 14")
    forms = {q.get("form") for q in qs.values()}
    check("форма всюду нейтральная", forms == {"нейтральная"}, str(forms))


def check_numbers(con, qs: dict) -> None:
    """Каждое число ниже пересчитывается по данным и ищется в тексте эталона."""
    def txt(*ids) -> str:
        return " ".join(qs[i]["truth"]["answer"] + qs[i]["naive"]["answer"] for i in ids)

    ends = [r[1] for r in con.execute("""select strftime(end_date,'%Y-%m'), count(*)
        from s where end_date between date '2023-04-01' and date '2023-10-31'
        group by 1 order by 1""").fetchall()]
    check("w1: ряд окончаний подписок",
          ", ".join(map(str, ends)) in txt("w1a"), ", ".join(map(str, ends)))

    plan_aug = con.execute("""select count(*) from s
        where planned_renewal_month = '2023-08'""").fetchone()[0]
    check("w1: план на август", str(plan_aug) in txt("w1b"), str(plan_aug))

    rates = []
    for asof in ("2025-04-30", "2025-05-31", "2025-07-31"):
        n, r = con.execute(f"""with co as (select sub_id, org_id, seq_no from s
              where strftime(end_date,'%Y-%m') = '2025-03'),
            nx as (select co.sub_id,
                     max(case when p.recorded_at <= date '{asof}' then 1 else 0 end) rn
                   from co left join s n on n.org_id = co.org_id and n.seq_no = co.seq_no + 1
                   left join i on i.sub_id = n.sub_id
                   left join p on p.invoice_id = i.invoice_id group by 1)
            select count(*), sum(rn) from nx""").fetchone()
        rates.append(f"{100.0 * r / n:.1f}%")
    miss = [x for x in rates if x not in txt("w2a", "w2b", "w2c")]
    check("w2: три процента продления", not miss, " ".join(rates))

    orgs24, pays24 = con.execute("""select count(distinct org_id), count(*) from p
        where paid_at between date '2024-01-01' and date '2024-12-31'""").fetchone()
    check("p1: клиентов и платежей за 2024",
          str(orgs24) in txt("p1c") and str(pays24) in txt("p1c"),
          f"{orgs24} организаций, {pays24} платежей")

    prem = con.execute("select sum(revenue_plan_premium) from pl").fetchone()[0]
    prem_s = f"{float(prem):,.0f}".replace(",", " ")
    check("p2: величина надбавки", prem_s in txt("p2a"), prem_s)

    fact, ini, appr = con.execute("""select sum(pay.amount), sum(pl.revenue_plan_initial),
        sum(pl.revenue_plan_approved)
        from pl join (select year(p.paid_at) y, o.size_segment seg, sum(p.amount) amount
                      from p join o using(org_id) group by 1,2) pay
          on pay.y = pl.plan_year and pay.seg is not distinct from pl.size_segment
        where pl.revision_date is not null""").fetchone()
    e = (f"{100 * float(fact) / float(ini):.1f}%", f"{100 * float(fact) / float(appr):.1f}%")
    miss = [x for x in e if x not in txt("p3a", "p3b")]
    check("p3: два процента выполнения", not miss, " ".join(e))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    args = ap.parse_args()
    data = Path(args.data)
    con = duckdb.connect()
    for v, f in [("o", "orgs"), ("s", "subscriptions"), ("i", "invoices"),
                 ("p", "payments"), ("pl", "plan")]:
        con.execute(f"create view {v} as select * from read_parquet('{data / f}.parquet')")
    qs = load()
    print("\nструктура")
    check_structure(qs)
    print("\nчисла в эталонах против данных")
    check_numbers(con, qs)
    bad = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} пройдено")
    for _, n, d in bad:
        print(f"  ПРОВАЛЕНО {n}: {d}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
