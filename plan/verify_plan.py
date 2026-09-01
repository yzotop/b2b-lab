#!/usr/bin/env python3
"""
Проверка слоя плана.

Генератор плана сюда не импортируется. Величины пересчитываются по
фактовым parquet и по самому plan.parquet; из truth читаются только
объявленные ожидания — коридоры и пороги, а не числа.

Проверяются три сюжета из plan/SCENARIOS.md, записанных до постройки.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[tuple[bool, str, str]] = []


def f(x) -> float:
    """DuckDB отдаёт суммы как DECIMAL. Без приведения проверка падает
    с TypeError вместо честного FAIL — это нашла фальсификация."""
    return float(x) if x is not None else 0.0


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name, detail))
    print(f"  [{'ok ' if ok else 'FAIL'}] {name:50s} {detail}")


def setup(con, data: Path) -> None:
    for v, f in [("o", "orgs"), ("p", "payments"), ("pl", "plan")]:
        con.execute(f"create or replace view {v} as "
                    f"select * from read_parquet('{data / f}.parquet')")
    # Факт в трёх единицах счёта сразу — ровно то, что план смешивает.
    con.execute("""create or replace view fact as
        select year(p.paid_at) as y, o.size_segment as seg,
               sum(p.amount) as revenue,
               count(distinct p.org_id) as orgs,
               count(*) as payments
        from p join o using(org_id) group by 1, 2""")
    con.execute("""create or replace view j as
        select pl.*, f.revenue, f.orgs, f.payments
        from pl join fact f
          on f.y = pl.plan_year
         and f.seg is not distinct from pl.size_segment""")


def check_integrity(con, t) -> None:
    n_pl, n_j = (con.execute(f"select count(*) from {v}").fetchone()[0] for v in ("pl", "j"))
    check("каждая плановая строка нашла факт", n_pl == n_j, f"{n_j} = {n_pl}")
    yrs = con.execute("select count(distinct plan_year) from pl").fetchone()[0]
    check("годы плана те, что объявлены", yrs == len(t["years"]),
          f"{yrs} из {len(t['years'])}")
    basis = con.execute("select distinct units_basis from pl").fetchall()
    check("единица счёта плана объявлена в самих данных",
          len(basis) == 1 and basis[0][0] == t["units_basis"],
          f"units_basis = {basis[0][0]!r}")
    ident = con.execute("""select count(*) from pl
        where abs(revenue_plan_approved - (revenue_plan_model + revenue_plan_premium)) > 0.02""").fetchone()[0]
    check("согласованный план = модель + надбавка", ident == 0, f"нарушений {ident}")


def check_s1(con, t) -> None:
    c = t["scenarios"]["s1_units_basis"]
    over = con.execute("select sum(payments)::double / sum(orgs) from j").fetchone()[0]
    check("СЮЖЕТ 1: платежей на организацию больше единицы",
          c["overcount_min"] <= over <= c["overcount_max"],
          f"x{over:.3f} в коридоре [{c['overcount_min']}, {c['overcount_max']}]")

    up, uo, upay = (f(x) for x in con.execute(
        "select sum(units_plan), sum(orgs), sum(payments) from j").fetchone())
    dev_orgs = (uo - up) / up
    dev_pay = (upay - up) / up
    check("СЮЖЕТ 1: в платежах факт перевыполняет план",
          dev_pay >= c["payments_dev_min"], f"{dev_pay:+.1%}")
    check("СЮЖЕТ 1: в организациях расхождение — плановая погрешность",
          abs(dev_orgs) <= c["orgs_dev_max"], f"{dev_orgs:+.1%}")
    check("СЮЖЕТ 1: приведение единиц снимает перевыполнение",
          abs(dev_orgs) < abs(dev_pay),
          f"|{dev_orgs:+.1%}| < |{dev_pay:+.1%}|")

    # Построчно, а не только в сумме: испорченная одна строка в сумме тонет.
    lim = t["units_error_pct"] * 2 + 0.02
    worst, yr = con.execute(f"""select max(abs(units_plan::double / orgs - 1)),
        arg_max(plan_year, abs(units_plan::double / orgs - 1)) from j""").fetchone()
    check("СЮЖЕТ 1: ни одна строка плана не выставлена в платежах",
          worst <= lim, f"худшая строка {yr}: {worst:+.1%} при пороге {lim:.1%}")

    # Средний чек считается от МОДЕЛЬНОЙ части: надбавка — сюжет 2,
    # и смешивать два эффекта в одной цифре нельзя.
    rev_plan, rev_fact = (f(x) for x in con.execute(
        "select sum(revenue_plan_model), sum(revenue) from j").fetchone())
    avg_plan = rev_plan / up
    avg_pay, avg_orgs = rev_fact / upay, rev_fact / uo
    check("СЮЖЕТ 1: средний чек в платежах ниже планового",
          avg_pay < avg_plan, f"{avg_pay:,.0f} против {avg_plan:,.0f}")
    check("СЮЖЕТ 1: в организациях средний чек возвращается к плановому",
          abs(avg_orgs - avg_plan) < abs(avg_pay - avg_plan),
          f"{avg_orgs:,.0f} ближе к {avg_plan:,.0f}")


def check_s2(con, t) -> None:
    c = t["scenarios"]["s2_premium"]
    model, approved, prem, fact = (f(x) for x in con.execute("""select sum(revenue_plan_model),
        sum(revenue_plan_approved), sum(revenue_plan_premium), sum(revenue) from j""").fetchone())
    dev_model = (fact - model) / model
    dev_appr = (fact - approved) / approved
    check("СЮЖЕТ 2: факт от модельной части недалеко",
          abs(dev_model) <= c["model_dev_max"], f"{dev_model:+.1%}")
    check("СЮЖЕТ 2: от согласованного плана факт отстаёт заметно",
          -dev_appr >= c["approved_dev_min"], f"{dev_appr:+.1%}")

    # «Необъяснённое» = разница двух отклонений в деньгах. Обязано равняться надбавке.
    unexplained = (fact - approved) - (fact - model)
    check("СЮЖЕТ 2: необъяснённое отклонение равно надбавке",
          prem > 0 and abs(abs(unexplained) - prem) <= prem * c["premium_tolerance"],
          f"{abs(unexplained):,.0f} против надбавки {prem:,.0f}")
    share = prem / model
    check("СЮЖЕТ 2: доля надбавки та, что объявлена",
          abs(share - t["premium_share"]) <= 0.005,
          f"{share:.1%} против объявленных {t['premium_share']:.0%}")


def check_s3(con, t) -> None:
    c = t["scenarios"]["s3_revision"]
    bad = con.execute("""select count(*) from pl where revision_date is null
        and abs(revenue_plan_initial - revenue_plan_approved) > 0.02""").fetchone()[0]
    check("СЮЖЕТ 3: непересмотренные строки имеют оба поля равными",
          bad == 0, f"нарушений {bad}")

    n_rev = con.execute("select count(*) from pl where revision_date is not null").fetchone()[0]
    check("СЮЖЕТ 3: пересмотренные строки есть", n_rev > 0, f"{n_rev} строк")

    worst = con.execute(f"""select max(abs(revenue_plan_approved
        / (revenue_plan_initial * (1 + {t['revision_uplift']})) - 1))
        from pl where revision_date is not null""").fetchone()[0]
    check("СЮЖЕТ 3: ревизия ровно на объявленную величину",
          worst <= 1e-4, f"худшее расхождение {worst:.2e}")

    fact, ini, appr = (f(x) for x in con.execute("""select sum(revenue), sum(revenue_plan_initial),
        sum(revenue_plan_approved) from j where revision_date is not null""").fetchone())
    e_ini, e_appr = 100 * fact / ini, 100 * fact / appr
    check("СЮЖЕТ 3: два процента выполнения, оба верные",
          abs(e_ini - e_appr) >= c["min_execution_gap_pp"],
          f"к исходному {e_ini:.1f}%, к согласованному {e_appr:.1f}%, "
          f"разрыв {e_ini - e_appr:.1f} п.п.")
    check("СЮЖЕТ 3: разрыв выводится из величины ревизии, а не из факта",
          abs(e_ini / e_appr - (1 + t["revision_uplift"])) <= 1e-3,
          f"{e_ini / e_appr:.4f} = 1 + {t['revision_uplift']}")


def check_docs(con, t) -> None:
    doc = Path(__file__).resolve().parent / "SCENARIOS.md"
    check("документация: SCENARIOS.md на месте", doc.exists(),
          "записан до постройки" if doc.exists() else "файла нет")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    args = ap.parse_args()
    t = json.loads((Path(__file__).resolve().parent / "truth.json").read_text(encoding="utf-8"))
    con = duckdb.connect()
    setup(con, Path(args.data))
    for fn in (check_integrity, check_s1, check_s2, check_s3, check_docs):
        print(f"\n{fn.__name__}")
        fn(con, t)
    bad = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} пройдено")
    if bad:
        print("ПРОВАЛЕНО:")
        for _, n, d in bad:
            print(f"  {n}: {d}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
