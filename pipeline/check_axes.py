"""Проверка: сходимость по осям. Внешняя правда не нужна.

Сравнивает краевые суммы задач с итогом. Отчёт: что разошлось,
на сколько и до чего проверка сужает виновника.
"""
import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TOL = 0.01


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cells")
    args = ap.parse_args()

    spec = yaml.safe_load((ROOT / "pipeline" / "tasks.yaml").read_text(encoding="utf-8"))
    data = json.loads(Path(args.cells).read_text(encoding="utf-8"))
    val = {c["id"]: c["value"] for c in data["cells"]}

    print(f"вариант {data['defect']}, год {data['year']}")
    broken = []
    for inv in spec["invariants"]:
        s = round(sum(val[p] for p in inv["parts"]), 2)
        tot = val[inv["total"]]
        d = round(s - tot, 2)
        ok = abs(d) < TOL
        print(f"  {inv['id']} {inv['name']:26s} сумма {s:>16,.2f}  итог {tot:>16,.2f}  "
              f"расхождение {d:>14,.2f}  {'сходится' if ok else 'РАСХОЖДЕНИЕ'}")
        if not ok:
            broken.append((inv, d))

    # Локализация: пересечение подозреваемых по разошедшимся инвариантам.
    covered = set()
    for inv in spec["invariants"]:
        covered |= set(inv["parts"]) | {inv["total"]}
    all_ids = {c["id"] for c in data["cells"]}
    print(f"\n  задач всего {len(all_ids)}, покрыто инвариантами {len(covered)}, "
          f"не покрыто {sorted(all_ids - covered)}")

    if not broken:
        print("  ВЕРДИКТ: расхождений нет. Дефект либо отсутствует, "
              "либо невидим для этой проверки.")
        return 0

    suspects = None
    for inv, _ in broken:
        s = set(inv["parts"]) | {inv["total"]}
        suspects = s if suspects is None else (suspects & s)
    print(f"  ВЕРДИКТ: разошлось инвариантов {len(broken)} из {len(spec['invariants'])}. "
          f"Подозреваемых задач: {len(suspects)} — {sorted(suspects)}")
    print(f"  сужение: до 1 задачи {'ДА' if len(suspects) == 1 else 'НЕТ'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
