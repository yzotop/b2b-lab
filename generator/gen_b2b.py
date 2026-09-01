#!/usr/bin/env python3
"""
Синтетическая B2B-подписка: события, не сводки.

Мир разыгрывается, а не рисуется. Продления, ликвидации, задержки
платежей — всё выходит из розыгрыша, а не проставляется постфактум.

Объявленные параметры лежат в WORLD и дампятся в generator/truth.json.
verify.py читает этот JSON и пересчитывает мир по данным, НЕ импортируя
генератор. Расхождение = падение.

Три сюжета, ради которых мир построен, — SCENARIOS.md. Что осознанно
не заложено — BACKLOG.md.

Общий код с генератором ритейла не выносился: в ритейле он не вынесен,
а выносить сейчас — отдельная работа (так решено в постановке).
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 1. Объявленный мир. Единственный источник правды; всё ниже — следствие.
# ---------------------------------------------------------------------------

WORLD = {
    "seed": 20260901,
    "start_date": "2015-01-01",
    "end_date": "2025-12-31",
    # Курсор: хвост в три месяца намеренно не дозрел. Всё, что записано
    # позже, в отчётах видно быть не должно.
    "cursor": "2025-10-01",

    # --- приток клиентов ---------------------------------------------------
    "acq_per_month_base": 145.0,
    "acq_growth_yearly": 0.015,
    # Пик в марте и сентябре: закупка софта под отчётные кампании.
    "acq_month_factor": [1.18, 1.05, 1.32, 0.96, 0.88, 0.80,
                         0.74, 0.92, 1.24, 1.10, 1.02, 0.79],

    # --- сегменты ----------------------------------------------------------
    "size_mix": {"micro": 0.46, "small": 0.33, "medium": 0.16, "large": 0.05},
    # РУЧКА 5: доля организаций с пустым размером бизнеса.
    "size_null_share": 0.09,
    "channel_mix": {"site": 0.36, "call": 0.23, "partner": 0.19,
                    "referral": 0.13, "selfserve": 0.09},
    "region_mix": {"R01": 0.24, "R02": 0.17, "R03": 0.14, "R04": 0.12,
                   "R05": 0.11, "R06": 0.09, "R07": 0.08, "R08": 0.05},

    # --- тарифы ------------------------------------------------------------
    "plans": {
        "basic":    {"tier": 1, "price_month": 1900.0},
        "standard": {"tier": 2, "price_month": 4200.0},
        "pro":      {"tier": 3, "price_month": 9800.0},
    },
    # Вероятность тарифа при первой подписке, по размеру бизнеса.
    "plan_by_size": {
        "micro":  {"basic": 0.74, "standard": 0.23, "pro": 0.03},
        "small":  {"basic": 0.41, "standard": 0.47, "pro": 0.12},
        "medium": {"basic": 0.14, "standard": 0.52, "pro": 0.34},
        "large":  {"basic": 0.04, "standard": 0.31, "pro": 0.65},
        None:     {"basic": 0.52, "standard": 0.36, "pro": 0.12},
    },
    "duration_mix": {"12": 0.74, "6": 0.19, "24": 0.07},
    "upgrade_share": 0.09,
    "downgrade_share": 0.05,

    # --- продления: двугорбое время жизни (сюжет 3) -------------------------
    # Первое продление даётся тяжело, дальше клиент держится.
    # Именно из-за этого формула 1/отток систематически завышает LT.
    "renew_p_first": 0.32,
    "renew_p_mature": 0.87,
    # Третья ступень: у очень старых клиентов риск снова растёт. Без неё
    # хвост геометрический и не наблюдается в окне данных — а тогда
    # когортный расчёт перестаёт быть верным ответом, и весь сюжет 3
    # держался бы на цензуре, а не на устройстве мира.
    "renew_p_late": 0.62,
    "renew_late_from_seq": 7,
    "liquidation_hazard_yearly": 0.028,

    # --- РУЧКА 1: лаг планового месяца продления ---------------------------
    # planned_renewal_month = месяц окончания минус лаг.
    # Лаг меняется в середине ряда; бизнес при этом не меняется.
    "renewal_lag_before": 2,
    "renewal_lag_after": 1,
    "renewal_lag_switch": "2023-07-01",

    # --- РУЧКА 2: забегания (ранний платёж в предыдущий период) ------------
    "early_pay_share": 0.14,
    "early_pay_days_min": 32,
    "early_pay_days_max": 78,

    # --- РУЧКА 3: добегание платежей после закрытия периода ----------------
    "late_pay_share": 0.27,
    "late_pay_days_mean": 62.0,
    "late_pay_days_max": 165,

    # --- РУЧКА 4: задержка признака ликвидации -----------------------------
    "liq_record_delay_days_mean": 74.0,
    "liq_record_delay_days_max": 240,

    # --- скидка ------------------------------------------------------------
    "discount_share_of_subs": 0.58,
    "discount_pct_mean": 0.12,
    "discount_pct_max": 0.35,

    # --- использование -----------------------------------------------------
    # Событий в месяц на активную подписку, по размеру бизнеса.
    "events_per_month": {"micro": 6.0, "small": 14.0, "medium": 38.0,
                         "large": 96.0, None: 11.0},
    "event_types": {"login": 0.46, "document": 0.31, "send": 0.14,
                    "calc": 0.06, "setup": 0.03},

    # --- запись в хранилище ------------------------------------------------
    "record_lag_days_max": 3,

    # --- три сюжета: ОЖИДАНИЯ, а не скопированные замеры --------------------
    # Здесь объявлено, что обязано быть верно по построению. Сами числа
    # verify.py меряет по данным; совпадение проверяется, а не переносится.
    "scenarios": {
        "s1_renewal_lag": {
            # Месяц смены лага не может быть ничьим плановым месяцем.
            "empty_plan_month": "2023-07",
            # Бизнес при этом гладкий: месячные окончания не должны прыгать
            # сильнее, чем на эту долю относительно соседей.
            "ends_smooth_max_jump": 0.45,
            # А план обязан прыгнуть сильнее вот этого.
            "plan_min_jump": 0.25,
        },
        "s2_asof": {
            "cohort_month": "2025-03",
            "dates": ["2025-04-30", "2025-05-31", "2025-07-31"],
            # Три числа обязаны строго расти и разойтись не меньше чем на:
            "min_spread_pp": 3.0,
        },
        "s3_lifetime": {
            # Когорты, наблюдаемые достаточно долго, чтобы цензура была мала.
            "cohort_before": "2017-01-01",
            "max_censored_share": 0.06,
            "churn_window": ["2021-01-01", "2023-12-31"],
            # Обе формулы обязаны завысить относительно когортного расчёта.
            "min_overestimate_gross": 1.05,
            "min_overestimate_net": 1.10,
            # Чистый отток меньше общего, значит завышает сильнее.
            "net_above_gross": True,
        },
        "s4_two_revenues": {
            # Две меры расходятся внутри года и сходятся за весь период.
            "year": 2024,
            "min_year_gap": 0.003,
            "max_year_gap": 0.05,
            "total_tolerance": 0.01,
        },
    },
}

MONTHS = 12


# ---------------------------------------------------------------------------
# 2. Вспомогательное
# ---------------------------------------------------------------------------

def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // MONTHS
    m = m % MONTHS + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
                      else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, day)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def pick(rng, mapping: dict):
    keys = list(mapping.keys())
    p = np.array([mapping[k] for k in keys], dtype=float)
    return keys[int(rng.choice(len(keys), p=p / p.sum()))]


def planned_month(end: date, lag_before: int, lag_after: int,
                  switch: date) -> str:
    """Плановый месяц продления.

    Лаг, действующий на момент планирования: если план по старому лагу
    попадал бы в месяц раньше даты смены — он так и составлен; иначе
    действует новый лаг. Из-за этого месяц смены остаётся пустым, а
    соседний меняет состав — при том что бизнес не изменился.
    """
    cand = add_months(end.replace(day=1), -lag_before)
    if cand < switch.replace(day=1):
        return month_key(cand)
    return month_key(add_months(end.replace(day=1), -lag_after))


# ---------------------------------------------------------------------------
# 3. Розыгрыш мира
# ---------------------------------------------------------------------------

def simulate(rng, W):
    start = datetime.strptime(W["start_date"], "%Y-%m-%d").date()
    end = datetime.strptime(W["end_date"], "%Y-%m-%d").date()
    switch = datetime.strptime(W["renewal_lag_switch"], "%Y-%m-%d").date()

    orgs, subs, invoices, payments = [], [], [], []
    org_no = sub_no = inv_no = pay_no = 0

    n_months = (end.year - start.year) * MONTHS + (end.month - start.month) + 1

    for mi in range(n_months):
        m_start = add_months(start.replace(day=1), mi)
        if m_start > end:
            break
        # Приток: тренд × месячный фактор, розыгрыш пуассоновский.
        years = mi / MONTHS
        lam = (W["acq_per_month_base"] * (1 + W["acq_growth_yearly"]) ** years
               * W["acq_month_factor"][m_start.month - 1])
        n_new = int(rng.poisson(lam))

        for _ in range(n_new):
            org_no += 1
            org_id = f"o{org_no:06d}"

            size = pick(rng, W["size_mix"])
            if rng.random() < W["size_null_share"]:
                size = None
            region = pick(rng, W["region_mix"])
            channel = pick(rng, W["channel_mix"])
            reg_days = int(rng.integers(120, 7300))
            registered_at = m_start - timedelta(days=reg_days)

            # Ликвидация как отдельный риск, независимый от продлений.
            liq_at = None
            haz = W["liquidation_hazard_yearly"]
            plan_name = pick(rng, W["plan_by_size"][size])

            cur_start = m_start + timedelta(days=int(rng.integers(0, 28)))
            seq = 0
            while cur_start <= end:
                seq += 1
                dur = int(pick(rng, W["duration_mix"]))
                cur_end = add_months(cur_start, dur)

                sub_no += 1
                sub_id = f"s{sub_no:07d}"
                price = W["plans"][plan_name]["price_month"] * dur
                if rng.random() < W["discount_share_of_subs"]:
                    disc_pct = min(W["discount_pct_max"],
                                   float(rng.exponential(W["discount_pct_mean"])))
                else:
                    disc_pct = 0.0
                amount_list = round(price, 2)
                discount = round(price * disc_pct, 2)
                amount_due = round(amount_list - discount, 2)

                subs.append((sub_id, org_id, plan_name, seq, channel,
                             cur_start, cur_end, dur,
                             planned_month(cur_end, W["renewal_lag_before"],
                                           W["renewal_lag_after"], switch)))

                # Счёт выставляется до начала периода.
                issued = cur_start - timedelta(days=int(rng.integers(1, 21)))
                inv_no += 1
                inv_id = f"i{inv_no:07d}"
                invoices.append((inv_id, sub_id, org_id, issued,
                                 cur_start, cur_end,
                                 amount_list, discount, amount_due))

                # Платёж: вовремя / забегание / добегание.
                r = rng.random()
                if seq > 1 and r < W["early_pay_share"]:
                    off = -int(rng.integers(W["early_pay_days_min"],
                                            W["early_pay_days_max"]))
                elif seq > 1 and r < W["early_pay_share"] + W["late_pay_share"]:
                    off = int(min(W["late_pay_days_max"],
                                  rng.exponential(W["late_pay_days_mean"]) + 3))
                else:
                    off = int(rng.integers(-12, 6))
                paid_at = cur_start + timedelta(days=off)
                pay_no += 1
                rec_lag = int(rng.integers(0, W["record_lag_days_max"] + 1))
                payments.append((f"p{pay_no:07d}", inv_id, org_id, paid_at,
                                 amount_due, paid_at + timedelta(days=rec_lag)))

                # Ликвидация внутри периода?
                p_liq = 1 - (1 - haz) ** (dur / MONTHS)
                if rng.random() < p_liq:
                    span = (cur_end - cur_start).days
                    liq_at = cur_start + timedelta(days=int(rng.integers(1, max(2, span))))
                    break

                if seq == 1:
                    p_renew = W["renew_p_first"]
                elif seq >= W["renew_late_from_seq"]:
                    p_renew = W["renew_p_late"]
                else:
                    p_renew = W["renew_p_mature"]
                if rng.random() >= p_renew:
                    break
                if rng.random() < W["upgrade_share"]:
                    plan_name = {"basic": "standard", "standard": "pro",
                                 "pro": "pro"}[plan_name]
                elif rng.random() < W["downgrade_share"]:
                    plan_name = {"pro": "standard", "standard": "basic",
                                 "basic": "basic"}[plan_name]
                cur_start = cur_end

            liq_rec = None
            if liq_at is not None:
                d = int(min(W["liq_record_delay_days_max"],
                            rng.exponential(W["liq_record_delay_days_mean"]) + 1))
                liq_rec = liq_at + timedelta(days=d)
            orgs.append((org_id, size, region, registered_at, liq_at, liq_rec))

    return orgs, subs, invoices, payments


def build_usage(rng, W, orgs, subs):
    """События использования. Привязаны к организации: пользователей в мире нет."""
    size_by_org = {o[0]: o[1] for o in orgs}
    end = datetime.strptime(W["end_date"], "%Y-%m-%d").date()
    types = list(W["event_types"].keys())
    tp = np.array([W["event_types"][t] for t in types], dtype=float)
    tp = tp / tp.sum()

    ev_id, out = 0, []
    for sub_id, org_id, _plan, _seq, _ch, s_start, s_end, dur, _pm in subs:
        rate = W["events_per_month"][size_by_org[org_id]]
        n = int(rng.poisson(rate * dur))
        if n == 0:
            continue
        span = max(1, (min(s_end, end) - s_start).days)
        offs = rng.integers(0, span, size=n)
        kinds = rng.choice(len(types), size=n, p=tp)
        lags = rng.integers(0, W["record_lag_days_max"] + 1, size=n)
        for k in range(n):
            ev_id += 1
            ts = s_start + timedelta(days=int(offs[k]))
            out.append((f"e{ev_id:08d}", org_id, types[int(kinds[k])], ts,
                        ts + timedelta(days=int(lags[k]))))
    return out


# ---------------------------------------------------------------------------
# 4. Запись
# ---------------------------------------------------------------------------

def write(out: Path, name: str, cols: dict, schema: pa.Schema) -> int:
    tbl = pa.table(cols, schema=schema)
    pq.write_table(tbl, out / f"{name}.parquet", compression="zstd")
    return tbl.num_rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    W = WORLD
    rng = np.random.default_rng(W["seed"])
    orgs, subs, invoices, payments = simulate(rng, W)
    usage = build_usage(rng, W, orgs, subs)

    n_o = write(out, "orgs", {
        "org_id": [o[0] for o in orgs],
        "size_segment": [o[1] for o in orgs],
        "region": [o[2] for o in orgs],
        "registered_at": [o[3] for o in orgs],
        "liquidated_at": [o[4] for o in orgs],
        "liquidation_recorded_at": [o[5] for o in orgs],
    }, pa.schema([("org_id", pa.string()), ("size_segment", pa.string()),
                  ("region", pa.string()), ("registered_at", pa.date32()),
                  ("liquidated_at", pa.date32()),
                  ("liquidation_recorded_at", pa.date32())]))

    n_p = write(out, "plans", {
        "plan_id": list(W["plans"].keys()),
        "tier": [W["plans"][k]["tier"] for k in W["plans"]],
        "price_month": [W["plans"][k]["price_month"] for k in W["plans"]],
    }, pa.schema([("plan_id", pa.string()), ("tier", pa.int32()),
                  ("price_month", pa.float64())]))

    n_s = write(out, "subscriptions", {
        "sub_id": [s[0] for s in subs], "org_id": [s[1] for s in subs],
        "plan_id": [s[2] for s in subs], "seq_no": [s[3] for s in subs],
        "channel": [s[4] for s in subs], "start_date": [s[5] for s in subs],
        "end_date": [s[6] for s in subs], "duration_months": [s[7] for s in subs],
        "planned_renewal_month": [s[8] for s in subs],
    }, pa.schema([("sub_id", pa.string()), ("org_id", pa.string()),
                  ("plan_id", pa.string()), ("seq_no", pa.int32()),
                  ("channel", pa.string()), ("start_date", pa.date32()),
                  ("end_date", pa.date32()), ("duration_months", pa.int32()),
                  ("planned_renewal_month", pa.string())]))

    n_i = write(out, "invoices", {
        "invoice_id": [i[0] for i in invoices], "sub_id": [i[1] for i in invoices],
        "org_id": [i[2] for i in invoices], "issued_at": [i[3] for i in invoices],
        "period_start": [i[4] for i in invoices], "period_end": [i[5] for i in invoices],
        "amount_list": [i[6] for i in invoices], "discount": [i[7] for i in invoices],
        "amount_due": [i[8] for i in invoices],
    }, pa.schema([("invoice_id", pa.string()), ("sub_id", pa.string()),
                  ("org_id", pa.string()), ("issued_at", pa.date32()),
                  ("period_start", pa.date32()), ("period_end", pa.date32()),
                  ("amount_list", pa.float64()), ("discount", pa.float64()),
                  ("amount_due", pa.float64())]))

    n_pay = write(out, "payments", {
        "payment_id": [p[0] for p in payments], "invoice_id": [p[1] for p in payments],
        "org_id": [p[2] for p in payments], "paid_at": [p[3] for p in payments],
        "amount": [p[4] for p in payments], "recorded_at": [p[5] for p in payments],
    }, pa.schema([("payment_id", pa.string()), ("invoice_id", pa.string()),
                  ("org_id", pa.string()), ("paid_at", pa.date32()),
                  ("amount", pa.float64()), ("recorded_at", pa.date32())]))

    n_e = write(out, "usage_events", {
        "event_id": [e[0] for e in usage], "org_id": [e[1] for e in usage],
        "event_type": [e[2] for e in usage], "event_ts": [e[3] for e in usage],
        "recorded_at": [e[4] for e in usage],
    }, pa.schema([("event_id", pa.string()), ("org_id", pa.string()),
                  ("event_type", pa.string()), ("event_ts", pa.date32()),
                  ("recorded_at", pa.date32())]))

    (Path(__file__).resolve().parent / "truth.json").write_text(
        json.dumps(W, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"orgs {n_o}  plans {n_p}  subscriptions {n_s}  "
          f"invoices {n_i}  payments {n_pay}  usage_events {n_e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
