#!/usr/bin/env python3
"""
Независимый пересчёт мира B2B по данным.

Генератор сюда НЕ импортируется. Единственный канал между ними —
generator/truth.json: объявленные параметры и объявленные ожидания
трёх сюжетов. verify читает JSON и меряет parquet через DuckDB.
Если заложенное не находится в данных — падаем.

Числа сюжетов здесь НЕ продублированы: truth объявляет, что обязано быть
верно (месяц пуст, три числа растут, формулы завышают), а сами величины
меряются по данным. Скопируй сюда число — и проверка станет декорацией.

Пороги — свойство измерения, а не мира, поэтому живут здесь.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((bool(ok), name, detail))
    print(f"  [{'ok ' if ok else 'FAIL'}] {name:44s} {detail}")


def setup(con, data: Path) -> None:
    for v, f in [("o", "orgs"), ("s", "subscriptions"), ("i", "invoices"),
                 ("p", "payments"), ("u", "usage_events"), ("pl", "plans")]:
        con.execute(f"create or replace view {v} as "
                    f"select * from read_parquet('{data / f}.parquet')")


# --- 1. Целостность ---------------------------------------------------------

def check_integrity(con, t) -> None:
    n_o, n_s, n_i, n_p = (con.execute(f"select count(*) from {v}").fetchone()[0]
                          for v in ("o", "s", "i", "p"))
    check("объём непустой", min(n_o, n_s, n_i, n_p) > 0,
          f"orgs {n_o}, subs {n_s}, invoices {n_i}, payments {n_p}")
    check("счёт на каждую подписку", n_i == n_s, f"{n_i} = {n_s}")

    orph = con.execute("select count(*) from s left join o using(org_id) "
                       "where o.org_id is null").fetchone()[0]
    check("нет подписок без организации", orph == 0, f"сирот {orph}")

    bad = con.execute("select count(*) from s where end_date <= start_date").fetchone()[0]
    check("период подписки положителен", bad == 0, f"нарушений {bad}")

    dup = con.execute("select count(*) from (select org_id, seq_no from s "
                      "group by 1,2 having count(*)>1)").fetchone()[0]
    check("нумерация подписок без дублей", dup == 0, f"дублей {dup}")

    gap = con.execute("""select count(*) from s a join s b
        on b.org_id=a.org_id and b.seq_no=a.seq_no+1
        where b.start_date <> a.end_date""").fetchone()[0]
    check("продление стыкуется с концом предыдущей", gap == 0, f"разрывов {gap}")

    rec = con.execute("select count(*) from p where recorded_at < paid_at").fetchone()[0]
    check("запись не раньше события", rec == 0, f"нарушений {rec}")


# --- 2. Мир: сегменты и тарифы ----------------------------------------------

def check_segments(con, t) -> None:
    n = con.execute("select count(*) from o").fetchone()[0]
    nulls = con.execute("select count(*) from o where size_segment is null").fetchone()[0]
    exp = t["size_null_share"]
    check("РУЧКА 5: доля пустого размера бизнеса",
          abs(nulls / n - exp) <= 0.012,
          f"{nulls / n:.3f} против объявленных {exp}")

    regs = con.execute("select count(distinct region) from o").fetchone()[0]
    check("регионы все встретились", regs == len(t["region_mix"]),
          f"{regs} из {len(t['region_mix'])}")

    chans = con.execute("select count(distinct channel) from s").fetchone()[0]
    check("каналы привлечения все встретились", chans == len(t["channel_mix"]),
          f"{chans} из {len(t['channel_mix'])}")

    # Оси живут на разных уровнях: размер на организации, канал на подписке.
    lvl = con.execute("""select count(*) from (
        select org_id from s group by org_id having count(distinct channel) > 1)""").fetchone()[0]
    check("канал постоянен внутри организации", lvl == 0,
          f"организаций с двумя каналами: {lvl}")


# --- 3. Ручки задержек ------------------------------------------------------

def check_delays(con, t) -> None:
    liq, rec = con.execute("""select count(*), count(liquidation_recorded_at)
        from o where liquidated_at is not null""").fetchone()
    check("ликвидация всегда получает запись", liq == rec, f"{rec} из {liq}")
    mean_d = con.execute("""select avg(date_diff('day', liquidated_at, liquidation_recorded_at))
        from o where liquidated_at is not null""").fetchone()[0]
    exp = t["liq_record_delay_days_mean"]
    check("РУЧКА 4: средняя задержка признака ликвидации",
          abs(mean_d - exp) <= exp * 0.15, f"{mean_d:.1f} дн. против {exp}")

    early = con.execute("""select count(*) from p join i using(invoice_id)
        where date_diff('day', paid_at, period_start) >= 32""").fetchone()[0]
    renew = con.execute("select count(*) from s where seq_no > 1").fetchone()[0]
    check("РУЧКА 2: забегания есть и это продления",
          t["early_pay_share"] * 0.6 <= early / renew <= t["early_pay_share"] * 1.4,
          f"{early / renew:.3f} против объявленных {t['early_pay_share']}")

    late = con.execute("""select count(*) from p join i using(invoice_id)
        where date_diff('day', period_start, paid_at) >= 10""").fetchone()[0]
    check("РУЧКА 3: добегание есть и это продления",
          t["late_pay_share"] * 0.6 <= late / renew <= t["late_pay_share"] * 1.4,
          f"{late / renew:.3f} против объявленных {t['late_pay_share']}")


# --- 4. Сюжет 1: смена лага продления ---------------------------------------

def check_s1(con, t) -> None:
    c = t["scenarios"]["s1_renewal_lag"]
    empty = c["empty_plan_month"]

    n_empty = con.execute("select count(*) from s where planned_renewal_month = ?",
                          [empty]).fetchone()[0]
    check("СЮЖЕТ 1: месяц смены лага пуст в плане", n_empty == 0,
          f"план на {empty}: {n_empty}")

    # Правило лага пересчитывается из объявленных входов, а не берётся из колонки.
    mism = con.execute("""
        select count(*) from s
        where planned_renewal_month <> (
          case when strftime(date_trunc('month', end_date) - interval (?) month, '%Y-%m') < ?
               then strftime(date_trunc('month', end_date) - interval (?) month, '%Y-%m')
               else strftime(date_trunc('month', end_date) - interval (?) month, '%Y-%m')
          end)""",
        [t["renewal_lag_before"], t["renewal_lag_switch"][:7],
         t["renewal_lag_before"], t["renewal_lag_after"]]).fetchone()[0]
    check("СЮЖЕТ 1: плановый месяц выводится из правила", mism == 0,
          f"расхождений {mism}")

    ends = dict(con.execute("""select strftime(end_date,'%Y-%m'), count(*) from s
        where end_date between date '2023-04-01' and date '2023-10-31'
        group by 1""").fetchall())
    plan = dict(con.execute("""select planned_renewal_month, count(*) from s
        where planned_renewal_month between '2023-04' and '2023-10' group by 1""").fetchall())
    months = sorted(ends)
    jumps_e = [abs(ends[b] - ends[a]) / ends[a] for a, b in zip(months, months[1:])]
    check("СЮЖЕТ 1: бизнес гладкий (окончания не прыгают)",
          max(jumps_e) <= c["ends_smooth_max_jump"],
          f"макс. скачок окончаний {max(jumps_e):.2f} <= {c['ends_smooth_max_jump']}")

    pm = sorted(set(plan) | set(months))
    jumps_p = []
    for a, b in zip(pm, pm[1:]):
        va, vb = plan.get(a, 0), plan.get(b, 0)
        jumps_p.append(1.0 if va == 0 or vb == 0 else abs(vb - va) / va)
    check("СЮЖЕТ 1: план прыгает, хотя бизнес нет",
          max(jumps_p) >= c["plan_min_jump"],
          f"макс. скачок плана {max(jumps_p):.2f} >= {c['plan_min_jump']}")


# --- 5. Сюжет 2: одна когорта, три даты расчёта -----------------------------

def renewal_rate(con, month: str, asof: str) -> tuple[int, int]:
    return con.execute(f"""
        with co as (select sub_id, org_id, seq_no from s
                    where strftime(end_date,'%Y-%m') = '{month}'),
        nx as (select co.sub_id,
                 max(case when pay.recorded_at <= date '{asof}' then 1 else 0 end) rn
               from co
               left join s n on n.org_id = co.org_id and n.seq_no = co.seq_no + 1
               left join i on i.sub_id = n.sub_id
               left join p pay on pay.invoice_id = i.invoice_id
               group by 1)
        select count(*), sum(rn) from nx""").fetchone()


def check_s2(con, t) -> None:
    c = t["scenarios"]["s2_asof"]
    rates = []
    for asof in c["dates"]:
        n, r = renewal_rate(con, c["cohort_month"], asof)
        rates.append(100.0 * r / n)
    check("СЮЖЕТ 2: три даты расчёта дают три разных числа",
          len(set(round(x, 1) for x in rates)) == 3,
          " -> ".join(f"{x:.1f}%" for x in rates))
    check("СЮЖЕТ 2: числа строго растут",
          all(a < b for a, b in zip(rates, rates[1:])),
          f"{rates[0]:.1f} < {rates[1]:.1f} < {rates[2]:.1f}")
    spread = rates[-1] - rates[0]
    check("СЮЖЕТ 2: разброс не декоративный",
          spread >= c["min_spread_pp"],
          f"{spread:.1f} п.п. >= {c['min_spread_pp']}")


# --- 6. Сюжет 3: LT тремя способами -----------------------------------------

def check_s3(con, t) -> None:
    c = t["scenarios"]["s3_lifetime"]
    cutoff, w0, w1 = c["cohort_before"], *c["churn_window"]

    tot, cens = con.execute(f"""
        with life as (select org_id, min(start_date) f,
             max(case when end_date > date '{t['cursor']}' then 1 else 0 end) cs
             from s group by 1)
        select count(*), sum(cs) from life where f < date '{cutoff}'""").fetchone()
    check("СЮЖЕТ 3: цензура старых когорт мала",
          cens / tot <= c["max_censored_share"],
          f"{cens / tot:.3f} <= {c['max_censored_share']}")

    n, ch, liq = con.execute(f"""
        with e as (select s.sub_id,
            (select count(*) from s n where n.org_id=s.org_id and n.seq_no=s.seq_no+1) rn,
            o.liquidated_at
          from s join o using(org_id)
          where s.end_date between date '{w0}' and date '{w1}')
        select count(*), sum(case when rn=0 then 1 else 0 end),
               sum(case when rn=0 and liquidated_at is not null then 1 else 0 end) from e""").fetchone()
    gross, net = ch / n, (ch - liq) / n
    lt_gross, lt_net = 1 / gross, 1 / net

    lt_cohort = con.execute(f"""
        with life as (select org_id, min(start_date) f, max(end_date) l,
             max(case when end_date > date '{t['cursor']}' then 1 else 0 end) cs
             from s group by 1)
        select avg(date_diff('day', f, l) / 365.25)
        from life where f < date '{cutoff}' and cs = 0""").fetchone()[0]

    check("СЮЖЕТ 3: 1/общий отток завышает",
          lt_gross / lt_cohort >= c["min_overestimate_gross"],
          f"{lt_gross:.2f} против когортных {lt_cohort:.2f} = ×{lt_gross / lt_cohort:.2f}")
    check("СЮЖЕТ 3: 1/чистый отток завышает сильнее",
          lt_net / lt_cohort >= c["min_overestimate_net"],
          f"{lt_net:.2f} = ×{lt_net / lt_cohort:.2f}")
    check("СЮЖЕТ 3: чистый выше общего", (lt_net > lt_gross) == c["net_above_gross"],
          f"{lt_net:.2f} > {lt_gross:.2f}")

    # Двугорбость: первый период уносит больше, чем любой следующий.
    dist = dict(con.execute(f"""
        with life as (select org_id, count(*) k, min(start_date) f,
             max(case when end_date > date '{t['cursor']}' then 1 else 0 end) cs
             from s group by 1)
        select k, count(*) from life where f < date '{cutoff}' and cs=0 group by 1""").fetchall())
    first = dist.get(1, 0)
    rest = sum(v for k, v in dist.items() if k > 1)
    check("СЮЖЕТ 3: распределение двугорбое (первый период уносит половину)",
          first >= 0.40 * (first + rest),
          f"ушли после 1 периода: {first} из {first + rest} ({100 * first / (first + rest):.0f}%)")


# --- 7. Две меры выручки ----------------------------------------------------

def check_two_revenues(con, t) -> None:
    c = t["scenarios"]["s4_two_revenues"]
    y = c["year"]
    cash = con.execute(f"""select sum(amount) from p
        where paid_at between date '{y}-01-01' and date '{y}-12-31'""").fetchone()[0]
    accr = con.execute(f"""select sum(amount_due) from i
        where period_start between date '{y}-01-01' and date '{y}-12-31'""").fetchone()[0]
    gap = abs(cash - accr) / accr
    check("РУЧКА 6: две меры выручки расходятся внутри года",
          c["min_year_gap"] <= gap <= c["max_year_gap"],
          f"{gap:.4f} за {y}")
    tc = con.execute("select sum(amount) from p").fetchone()[0]
    ta = con.execute("select sum(amount_due) from i").fetchone()[0]
    check("РУЧКА 6: за весь период меры сходятся",
          abs(tc - ta) <= c["total_tolerance"], f"разница {abs(tc - ta):.2f}")


# --- 8. Курсор --------------------------------------------------------------

def check_cursor(con, t) -> None:
    cur = t["cursor"]
    beyond = con.execute(f"select count(*) from p where recorded_at > date '{cur}'").fetchone()[0]
    check("курсор: за ним есть незрелый хвост", beyond > 0,
          f"платежей записано позже курсора: {beyond}")
    total = con.execute("select count(*) from p").fetchone()[0]
    check("курсор: хвост не съедает мир", beyond / total < 0.20,
          f"{beyond / total:.3f} от всех платежей")


# --- 9. Числа, напечатанные в документации ----------------------------------

def check_docs(con, t) -> None:
    doc = ROOT / "SCENARIOS.md"
    if not doc.exists():
        check("документация: SCENARIOS.md на месте", False, "файла нет")
        return
    text = doc.read_text(encoding="utf-8")
    c = t["scenarios"]["s2_asof"]
    rates = []
    for asof in c["dates"]:
        n, r = renewal_rate(con, c["cohort_month"], asof)
        rates.append(f"{100.0 * r / n:.1f}%")
    missing = [x for x in rates if x not in text]
    check("документация: проценты продления совпадают с данными",
          not missing, f"не найдено в тексте: {missing}" if missing else " ".join(rates))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    args = ap.parse_args()
    t = json.loads((ROOT / "generator" / "truth.json").read_text(encoding="utf-8"))
    con = duckdb.connect()
    setup(con, Path(args.data))

    for fn in (check_integrity, check_segments, check_delays, check_s1,
               check_s2, check_s3, check_two_revenues, check_cursor, check_docs):
        print(f"\n{fn.__name__}")
        fn(con, t)

    bad = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} пройдено")
    if bad:
        print("ПРОВАЛЕНО:")
        for _, name, detail in bad:
            print(f"  {name}: {detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
