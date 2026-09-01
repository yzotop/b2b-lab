#!/usr/bin/env python3
"""
План поверх готового мира.

План — не ручка мира, а отдельная сущность со своей грануляцией. Он
строится ИЗ факта, но грубее его и с объявленными искажениями: так
устроен всякий реальный план, который считают по агрегатам, а сверяют
с транзакциями.

Мир этот модуль не трогает и не импортирует: читает `data/*.parquet`
и пишет `data/plan.parquet` плюс `plan/truth.json`.

Три сюжета, ради которых план и заводится, записаны ДО постройки —
`plan/SCENARIOS.md`, коммит раньше этого файла.

Грануляция — год x сегмент, и это не выбор из удобства: перекос
«платежи против организаций» существует только на годовой шкале
(x1.087), а на месячной он ровно единичный. На месяцах первого сюжета
нет вовсе.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent

PLAN = {
    "seed": 20260902,
    "years": list(range(2016, 2025)),          # 2015 неполный слева, 2025 обрезан курсором

    # Единица счёта плана. В факте её нет нигде — это и есть сюжет 1.
    "units_basis": "orgs",

    # Плановая погрешность: план не копия факта, но и не фантазия.
    "model_error_pct": 0.03,                   # выручка, +-
    "units_error_pct": 0.02,                   # штуки, +-

    # СЮЖЕТ 2. Надбавка сверху модельной части, одной строкой.
    # 0.22 выбрано намеренно далеко от перекоса единиц (~0.087):
    # в SCENARIOS.md записано, что если оба эффекта окажутся одного
    # порядка, их придётся разводить. Разведены здесь.
    "premium_share": 0.22,

    # СЮЖЕТ 3. Ревизия: план поднят в середине года, старое поле осталось.
    "revised_years": [2021, 2024],
    "revision_uplift": 0.15,                   # approved = initial x (1 + uplift)
    "revision_month": 7,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    for v, f in [("o", "orgs"), ("p", "payments")]:
        con.execute(f"create view {v} as select * from read_parquet('{ROOT / 'data' / f}.parquet')")

    # Факт по годам и сегментам: выручка, уникальные организации, платежи.
    rows = con.execute("""
        select year(p.paid_at) as y, o.size_segment as seg,
               sum(p.amount) as revenue,
               count(distinct p.org_id) as orgs,
               count(*) as payments
        from p join o using(org_id)
        group by 1, 2 order by 1, 2""").fetchall()

    rng = np.random.default_rng(PLAN["seed"])
    years = set(PLAN["years"])
    recs = []
    for y, seg, revenue, orgs, payments in rows:
        if y not in years:
            continue
        # Модельная часть: факт плюс плановая погрешность.
        e = float(rng.uniform(-PLAN["model_error_pct"], PLAN["model_error_pct"]))
        model = round(float(revenue) * (1 + e), 2)
        premium = round(model * PLAN["premium_share"], 2)
        approved = round(model + premium, 2)
        if y in PLAN["revised_years"]:
            initial = round(approved / (1 + PLAN["revision_uplift"]), 2)
            rev_date = f"{y}-{PLAN['revision_month']:02d}-01"
        else:
            initial = approved
            rev_date = None
        d = float(rng.uniform(-PLAN["units_error_pct"], PLAN["units_error_pct"]))
        units = int(round(orgs * (1 + d)))
        recs.append((y, seg, units, PLAN["units_basis"], model, premium,
                     approved, initial, rev_date))

    tbl = pa.table({
        "plan_year": [r[0] for r in recs],
        "size_segment": [r[1] for r in recs],
        "units_plan": [r[2] for r in recs],
        "units_basis": [r[3] for r in recs],
        "revenue_plan_model": [r[4] for r in recs],
        "revenue_plan_premium": [r[5] for r in recs],
        "revenue_plan_approved": [r[6] for r in recs],
        "revenue_plan_initial": [r[7] for r in recs],
        "revision_date": [r[8] for r in recs],
    }, schema=pa.schema([("plan_year", pa.int32()), ("size_segment", pa.string()),
                         ("units_plan", pa.int32()), ("units_basis", pa.string()),
                         ("revenue_plan_model", pa.float64()),
                         ("revenue_plan_premium", pa.float64()),
                         ("revenue_plan_approved", pa.float64()),
                         ("revenue_plan_initial", pa.float64()),
                         ("revision_date", pa.string())]))
    pq.write_table(tbl, out / "plan.parquet", compression="zstd")

    truth = dict(PLAN)
    truth["scenarios"] = {
        # Объявлены ОЖИДАНИЯ, а не скопированные замеры. Величины меряет verify.
        "s1_units_basis": {
            "overcount_min": 1.04, "overcount_max": 1.14,
            "orgs_dev_max": 0.06,        # план против факта в организациях
            "payments_dev_min": 0.03,    # план против факта в платежах
        },
        "s2_premium": {
            "model_dev_max": 0.05,       # факт от модельной части недалеко
            "approved_dev_min": 0.12,    # а от согласованного — на надбавку
            "premium_tolerance": 0.03,
        },
        "s3_revision": {
            "unrevised_must_match": True,
            "uplift_tolerance": 1e-6,
            "min_execution_gap_pp": 8.0,
        },
    }
    (Path(__file__).resolve().parent / "truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"plan.parquet: {len(recs)} строк "
          f"({len(PLAN['years'])} лет x сегменты), надбавка {PLAN['premium_share']:.0%}, "
          f"ревизия в {PLAN['revised_years']} на +{PLAN['revision_uplift']:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
