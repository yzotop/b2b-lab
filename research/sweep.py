"""Устойчивость выводов к смене развилок. Счёт по данным, модель не зовём.

Каждый сюжет считается при базовых значениях развилок и при
альтернативных, объявленных в research/FORKS.md.
"""
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
CURSOR = "2025-10-01"


def con():
    c = duckdb.connect()
    c.execute(f"set file_search_path='{ROOT}'")
    for a, f in [("o", "orgs"), ("s", "subscriptions"), ("i", "invoices"),
                 ("p", "payments"), ("pl", "plan"), ("pp", "plans")]:
        c.execute(f"create view {a} as select * from 'data/{f}.parquet'")
    return c


def f(x):
    return float(x) if x is not None else 0.0


# ---------------------------------------------------------------- w1
def w1(c):
    print("\n=== w1 — ряд окончаний ===")
    win = "end_date between date '2023-04-01' and date '2023-10-31'"
    base = [r[1] for r in c.execute(
        f"select strftime(end_date,'%Y-%m'), count(*) from s where {win} "
        "group by 1 order by 1").fetchall()]
    print(f"  база (счёт подписок, 7 мес): {base}  размах {max(base)/min(base):.2f}")

    money = [f(r[1]) for r in c.execute(f"""
        select strftime(s.end_date,'%Y-%m'), sum(i.amount_due) from s
        join i on i.sub_id = s.sub_id where {win} group by 1 order by 1""").fetchall()]
    print(f"  F1 деньги: {[round(x/1e6,1) for x in money]} млн  размах {max(money)/min(money):.2f}")

    orgs = [r[1] for r in c.execute(
        f"select strftime(end_date,'%Y-%m'), count(distinct org_id) from s where {win} "
        "group by 1 order by 1").fetchall()]
    print(f"  F2 организации: {orgs}  размах {max(orgs)/min(orgs):.2f}")

    w13 = [r[1] for r in c.execute(
        "select strftime(end_date,'%Y-%m'), count(*) from s where end_date "
        "between date '2023-01-01' and date '2024-01-31' group by 1 order by 1").fetchall()]
    print(f"  F3 13 месяцев: {w13}  размах {max(w13)/min(w13):.2f}")

    print("  F1 деньги, размах в те же месяцы других лет:")
    for y in (2021, 2022, 2024):
        m = [f(r[1]) for r in c.execute(f"""
            select strftime(s.end_date,'%Y-%m'), sum(i.amount_due) from s
            join i on i.sub_id = s.sub_id where s.end_date between date '{y}-04-01'
            and date '{y}-10-31' group by 1 order by 1""").fetchall()]
        print(f"    {y}: размах {max(m)/min(m):.2f}")
    print("  F4 критерий гладкости — тот же размах в те же месяцы других лет:")
    for y in (2021, 2022, 2024):
        r = [x[1] for x in c.execute(
            f"select strftime(end_date,'%Y-%m'), count(*) from s where end_date "
            f"between date '{y}-04-01' and date '{y}-10-31' group by 1 order by 1").fetchall()]
        print(f"    {y}: {r}  размах {max(r)/min(r):.2f}")


# ---------------------------------------------------------------- w2
def w2(c):
    print("\n=== w2 — процент продления за март 2025 ===")

    def rate(asof, grace=None, unit="clients"):
        g = "" if grace is None else \
            f"and p.paid_at <= co.end_date + interval {grace} day"
        sel = "count(*), sum(rn)" if unit == "subs" else "count(*), sum(rn)"
        return c.execute(f"""
            with co as (select sub_id, org_id, seq_no, end_date from s
                        where strftime(end_date,'%Y-%m') = '2025-03'),
            nx as (select co.sub_id,
                     max(case when p.recorded_at <= date '{asof}' {g} then 1 else 0 end) rn
                   from co left join s n on n.org_id = co.org_id and n.seq_no = co.seq_no + 1
                   left join i on i.sub_id = n.sub_id
                   left join p on p.invoice_id = i.invoice_id group by 1)
            select {sel} from nx""").fetchone()

    print("  F1 дата расчёта (база 4 мес = 2025-07-31):")
    for lbl, asof in [("+1 мес", "2025-04-30"), ("+2", "2025-05-31"),
                      ("+4 БАЗА", "2025-07-31"), ("+6", "2025-09-30"),
                      ("+12", "2026-03-31")]:
        n, r = rate(asof)
        print(f"    {lbl:9s} {100*f(r)/n:5.1f}%")

    print("  F2 grace_days (на дате +4):")
    for g in (90, 120, 180, None):
        n, r = rate("2025-07-31", grace=g)
        print(f"    grace {str(g):5s} {100*f(r)/n:5.1f}%  (база — БЕЗ grace)")

    print("  F3 единица clients/subs: в этом мире цепочка одна, меры совпадают —")
    n, r = rate("2025-07-31")
    print(f"    подписок в марте {n}, организаций "
          f"{c.execute(chr(115)+chr(101)+chr(108)+chr(101)+chr(99)+chr(116)+' count(distinct org_id) from s '+chr(119)+'here strftime(end_date,'+chr(39)+'%Y-%m'+chr(39)+') = '+chr(39)+'2025-03'+chr(39)).fetchone()[0]}")


# ---------------------------------------------------------------- w3
def w3(c, grace=120, max_cohort=2016, unit="org", cursor=CURSOR):
    if unit == "org":
        life = f"""
        select o2.org_id, min(s.start_date) st, max(s.end_date) en
        from s join o o2 using(org_id) group by 1"""
    else:
        life = """select sub_id org_id, start_date st, end_date en from s"""
    row = c.execute(f"""
        with L as ({life}),
        C as (select *, year(st) coh,
                     case when en + interval {grace} day <= date '{cursor}'
                          then 1 else 0 end done,
                     date_diff('day', st, en)/365.25 lt
              from L)
        select count(*), sum(done), avg(case when done=1 then lt end)
        from C where coh <= {max_cohort}""").fetchone()
    n, done, lt = row[0], f(row[1]), f(row[2])
    return n, 1 - done / n, lt


def w3_block(c):
    print("\n=== w3 — lifetime ===")
    n, cens, lt = w3(c)
    print(f"  база (цепочка org, уход 120д, когорты<=2016, курсор {CURSOR}): "
          f"LT {lt:.2f} г, цензура {100*cens:.1f}%, n={n}")
    for lbl, kw in [("F1 уход 90д", {"grace": 90}), ("F1 уход 180д", {"grace": 180}),
                    ("F2 все когорты", {"max_cohort": 2026}),
                    ("F3 единица — подписка", {"unit": "sub"}),
                    ("F4 курсор max(recorded_at)", {"cursor": "2026-06-12"})]:
        n2, c2, l2 = w3(c, **kw)
        print(f"  {lbl:28s} LT {l2:.2f} г  ({(l2/lt-1)*100:+5.1f}% к базе), "
              f"цензура {100*c2:.1f}%, n={n2}")
    ch = c.execute("""
        with L as (select org_id, min(start_date) st, max(end_date) en from s group by 1)
        select count(*), sum(case when en + interval 120 day <= date '2025-10-01'
                                  then 1 else 0 end) from L""").fetchone()
    rows = c.execute("""
        with L as (select org_id, min(start_date) st, max(end_date) en from s group by 1)
        select y, count(*) filter (where st <= make_date(y,1,1) and en >= make_date(y,1,1)) alive,
               count(*) filter (where year(en) = y) gone
        from L, (select unnest(range(2016, 2024)) y) group by y order by y""").fetchall()
    rates = [g / a for _, a, g in rows if a]
    ch_avg = sum(rates) / len(rates)
    print(f"  формулы: годовой отток по годам {[round(r,3) for r in rates]}")
    print(f"    средний {ch_avg:.3f} -> 1/отток = {1/ch_avg:.2f} г")


# ---------------------------------------------------------------- p1
def p1(c, presence="active", unit="orgs", cut=True, year=2024):
    cur = f"and p.recorded_at <= date '{CURSOR}'" if cut else ""
    if presence == "active" and unit == "orgs":
        q = f"select count(distinct p.org_id) from p where year(p.paid_at)={year} {cur}"
    elif presence == "active" and unit == "subs":
        q = f"""select count(distinct i.sub_id) from p join i on i.invoice_id=p.invoice_id
                where year(p.paid_at)={year} {cur}"""
    else:  # served
        q = f"""select count(distinct s.org_id) from s
                where s.start_date <= date '{year}-12-31'
                  and s.end_date  >= date '{year}-01-01'"""
    fact = c.execute(q).fetchone()[0]
    plan = c.execute(f"select sum(units_plan) from pl where plan_year={year}").fetchone()[0]
    return fact, plan, 100 * (fact / f(plan) - 1)


def p1_block(c):
    print("\n=== p1 — единица счёта ===")
    for lbl, kw in [("БАЗА orgs/active/2024", {}),
                    ("F1 presence=served", {"presence": "served"}),
                    ("F2 unit=subs", {"unit": "subs"}),
                    ("F3 без отсечки", {"cut": False}),
                    ("F4 год 2023", {"year": 2023})]:
        fact, plan, dev = p1(c, **kw)
        print(f"  {lbl:24s} факт {fact:6d}  план {plan:6d}  отклонение {dev:+6.1f}%")


# ---------------------------------------------------------------- p2 / p3
def money(c, basis="gross", recognition="cash", cut=True, year=2024):
    """gross = amount_list (прайс), net = amount_due (после скидки).
    discount в данных абсолютный: amount_list - discount = amount_due."""
    col = "i.amount_list" if basis == "gross" else "i.amount_due"
    if recognition == "cash":
        cur = f"and p.recorded_at <= date '{CURSOR}'" if cut else ""
        src = f"""select o.size_segment seg,
                    {col} * (p.amount / nullif(i.amount_due,0)) amt
                  from p join i on i.invoice_id = p.invoice_id
                  join o on o.org_id = p.org_id
                  where year(p.paid_at) = {year} {cur}"""
    else:
        src = f"""select o.size_segment seg, {col} amt from i
                  join o on o.org_id = i.org_id
                  where year(i.period_start) = {year}"""
    rows = c.execute(f"select seg, sum(amt) from ({src}) group by 1").fetchall()
    return {r[0]: f(r[1]) for r in rows}


def p2_block(c):
    print("\n=== p2 — остаток после сегментов ===")
    print("  БАЗА эталона = net/cash (payments.amount): проверено воспроизведением")
    for lbl, kw in [("БАЗА net/cash/2024", {"basis": "net"}),
                    ("F1 basis=gross (прайс)", {"basis": "gross"}),
                    ("F2 net/accrual", {"basis": "net", "recognition": "accrual"}),
                    ("F4 net/cash/2023", {"basis": "net", "year": 2023})]:
        y = kw.get("year", 2024)
        fact = money(c, **kw)
        pm, pa = c.execute(f"""select sum(revenue_plan_model), sum(revenue_plan_approved)
                               from pl where plan_year={y}""").fetchone()
        tot = sum(fact.values())
        tot_ns = sum(v for k, v in fact.items() if k is not None)
        print(f"  {lbl:24s} факт {tot/1e6:7.1f} млн  к модели {100*(tot/f(pm)-1):+6.1f}%  "
              f"к согласованному {100*(tot/f(pa)-1):+6.1f}%")
        if lbl.startswith("БАЗА"):
            print(f"    F3 без NULL-сегмента: факт {tot_ns/1e6:7.1f} млн  "
                  f"к модели {100*(tot_ns/f(pm)-1):+6.1f}%")


def p3_block(c):
    print("\n=== p3 — две версии плана ===")
    for lbl, kw in [("БАЗА gross/cash 2024", {}),
                    ("F1 basis=net", {"basis": "net"}),
                    ("F2 recognition=accrual", {"recognition": "accrual"}),
                    ("F3 без отсечки", {"cut": False})]:
        tot = sum(money(c, year=2024, **kw).values())
        ini, appr = c.execute("""select sum(revenue_plan_initial), sum(revenue_plan_approved)
                                 from pl where plan_year=2024""").fetchone()
        a, b = 100 * tot / f(ini), 100 * tot / f(appr)
        print(f"  {lbl:24s} к исходному {a:5.1f}%  к согласованному {b:5.1f}%  "
              f"отношение {a/b:.4f}")
    # Развилка, найденная при пересчёте: какие годы складывать.
    tot = sum(sum(money(c, year=y).values()) for y in (2021, 2024))
    ini, appr = c.execute("""select sum(revenue_plan_initial), sum(revenue_plan_approved)
                             from pl where revision_date is not null""").fetchone()
    a, b = 100 * tot / f(ini), 100 * tot / f(appr)
    print(f"  {'F5 оба ревизных года':24s} к исходному {a:5.1f}%  "
          f"к согласованному {b:5.1f}%  отношение {a/b:.4f}")


def main() -> int:
    c = con()
    w1(c); w2(c); w3_block(c); p1_block(c); p2_block(c); p3_block(c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
