#!/usr/bin/env python3
"""
Слой выгрузок поверх готовых parquet.

До сих пор портился источник. Здесь портится путь от источника
к аналитику: в хранилище данные верны, а в выгрузке — нет.

Генераторы миров этот модуль не трогает и не импортирует. Он читает
готовые parquet и производит из них файлы того вида, в каком выгрузку
обычно отдают человеку: CSV с заголовком, без служебных колонок.

Три ручки, все по умолчанию ВЫКЛЮЧЕНЫ:

  multiplier     часть колонок умножена на константу. Заголовок тот же,
                 пропорции верны, порядок величины — нет.
  hidden_filter  выгрузка сделана с условием, которого в данных не видно:
                 колонки фильтра в выходе нет по построению.
  as_of          строки старше даты выгрузки. Один и тот же прошлый месяц
                 в выгрузке от вчера и от сегодня даёт разные цифры.

Каждая выгрузка сопровождается <имя>.truth.json: что применено и какое
число правильное. Проверяет это exports/verify_exports.py, и он
пересчитывает всё по источнику, а не переписывает числа из truth.

Использование:
    python3 exports/make_export.py exports/specs/payments_thousands.json
    python3 exports/make_export.py --all
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent


def _src(spec) -> str:
    """Источник выгрузки: таблица целиком или объявленный в спеке SQL.

    SQL нужен там, где аналитику отдают не таблицу, а сшивку — например
    платежи вместе с периодом, к которому они относятся. Именно на такой
    сшивке видно доезжание: период закрыт, а платежи по нему ещё идут.
    """
    base = Path(spec["source_dir"])
    if spec.get("source_sql"):
        sql = spec["source_sql"]
        for t in spec.get("source_tables", []):
            sql = sql.replace(f"{{{t}}}", f"read_parquet('{base / t}.parquet')")
        return f"({sql})"
    return f"read_parquet('{base / spec['table']}.parquet')"


def _sums(con, sql: str, cols: list[str]) -> dict:
    if not cols:
        return {}
    sel = ", ".join(f"sum({c}) as {c}" for c in cols)
    row = con.execute(f"select {sel} from ({sql})").fetchone()
    return {c: (None if v is None else round(float(v), 4)) for c, v in zip(cols, row)}


def build(spec: dict, out_dir: Path) -> dict:
    con = duckdb.connect()
    src = _src(spec)

    all_cols = [r[0] for r in con.execute(f"describe select * from {src}").fetchall()]
    numeric = [r[0] for r in con.execute(f"describe select * from {src}").fetchall()
               if r[1] in ("DOUBLE", "FLOAT", "BIGINT", "INTEGER", "HUGEINT", "DECIMAL")]

    mult = spec.get("multiplier") or {}
    hidden = spec.get("hidden_filter")
    as_of = spec.get("as_of")
    as_of_col = spec.get("as_of_column")

    # Колонка скрытого фильтра из выхода убирается: иначе фильтр перестаёт
    # быть скрытым и проверяется по самим строкам.
    drop = set(spec.get("hidden_filter_columns") or [])
    out_cols = [c for c in all_cols if c not in drop]
    if spec.get("columns"):
        out_cols = [c for c in spec["columns"] if c not in drop]

    proj = ", ".join(
        (f"{c} * {mult[c]} as {c}" if c in mult else c) for c in out_cols)

    where = []
    if hidden:
        where.append(f"({hidden})")
    if as_of:
        if not as_of_col:
            raise SystemExit("as_of задан, а as_of_column — нет")
        where.append(f"{as_of_col} <= date '{as_of}'")
    w = (" where " + " and ".join(where)) if where else ""

    export_sql = f"select {proj} from {src}{w}"
    clean_sql = f"select {', '.join(out_cols)} from {src}"          # без ручек
    unfiltered_sql = f"select {', '.join(out_cols)} from {src}"     # без фильтра и даты

    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = spec.get("format", "csv")
    path = out_dir / f"{spec['name']}.{fmt}"
    if fmt == "csv":
        con.execute(f"copy ({export_sql}) to '{path}' (format csv, header)")
    else:
        con.execute(f"copy ({export_sql}) to '{path}' (format parquet)")

    watch = [c for c in (spec.get("control_columns") or numeric) if c in out_cols]
    n_src = con.execute(f"select count(*) from {src}").fetchone()[0]
    n_out = con.execute(f"select count(*) from ({export_sql})").fetchone()[0]

    truth = {
        "name": spec["name"],
        "source": {"dir": spec["source_dir"], "table": spec.get("table"),
                   "sql": spec.get("source_sql"), "tables": spec.get("source_tables")},
        "format": fmt,
        "columns": out_cols,
        "handles": {
            "multiplier": mult or None,
            "hidden_filter": hidden,
            "hidden_filter_columns": sorted(drop) or None,
            "as_of": as_of,
            "as_of_column": as_of_col if as_of else None,
        },
        # Контроль: что лежит в источнике и что обязано получиться в выгрузке.
        # verify пересчитывает это по parquet, а не верит записанному.
        "control": {
            "rows_source": n_src,
            "rows_exported": n_out,
            "sums_source_unfiltered": _sums(con, unfiltered_sql, watch),
            "sums_exported": _sums(con, export_sql, watch),
        },
        # Правильный ответ: что аналитик обязан получить, если снимет искажение.
        "correct": {
            "note": spec.get("correct_note", ""),
            "undo_multiplier": {c: 1.0 / k for c, k in mult.items()} or None,
            "rows_without_hidden_filter": n_src if hidden else None,
        },
    }
    (out_dir / f"{spec['name']}.truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return truth


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "out"))
    args = ap.parse_args()

    specs = (sorted((ROOT / "specs").glob("*.json")) if args.all
             else [Path(args.spec)])
    if not specs:
        raise SystemExit("нечего собирать")
    for sp in specs:
        spec = json.loads(sp.read_text(encoding="utf-8"))
        t = build(spec, Path(args.out))
        h = t["handles"]
        on = [k for k in ("multiplier", "hidden_filter", "as_of") if h[k]]
        print(f"  {t['name']:26s} строк {t['control']['rows_exported']:6d}  "
              f"ручки: {', '.join(on) if on else 'нет (чистая)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
