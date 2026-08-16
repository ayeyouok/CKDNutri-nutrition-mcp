# -*- coding: utf-8 -*-
"""X5（2026-08-14）：数据质量校验——food_data.csv 重名行数值冲突 + foods_ckd.json 双源偏差。

策略：已知冲突行登记在 DOCUMENTED_CONFLICTS（人工核对/待营养师修正，仅告警）；
**新增**冲突行（不在清单中）直接 fail——CI 从此能抓住数据回归，而不是"数据错测试照样绿"。

运行：python tests/test_data_quality.py
"""
# ---------------------------------------------------------------- 数据路径
import os
os.environ.setdefault("A207_ENV", "test")  # N-SEC-1（2026-08-14）：测试进程显式声明测试环境（守卫 fail-closed 默认拒绝）
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏（2026-08-15）：测试进程显式确认 json 后端为开发模式
import csv
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

DATA_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "CKDNutri_nutrition_mcp" / "data"
CSV_PATH = DATA_DIR / "food_data.csv"
CKD_JSON_PATH = DATA_DIR / "foods_ckd.json"

# 生物变异上限：同基名多行间 K/P 最大倍差 > 此值判"数据冲突"（find_food 同口径 3 倍）
CONFLICT_RATIO = 3.0
# 已知冲突登记（2026-08-14 已按中国食物成分表第 6 版修正 11 组重名行——登记清空，
# 后续若有新数据冲突必须显式登记人工核对后才能放行）
DOCUMENTED_CONFLICTS: set[tuple[str, str]] = set()
# 双源允许偏差（近似值子集声明，mealplan 注释：与全量成分表可能不同）
SRC_ALLOWED_DIFF = 0.25


def _load_csv() -> list[dict[str, str]]:
    raw = CSV_PATH.read_bytes()
    return list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))


def test_csv_no_duplicate_rows():
    """数据修正后强断言（A 项，2026-08-14）：**精确重名行为 0**。

    此前 11 组精确重名（松蘑 K 93 vs 2402 等）已按中国食物成分表第 6 版
    （CDC 官方平台 nlc.chinanutri.cn 核对）合并为权威行。新增任何精确重名
    → fail（数据回归拦截）。注意：加工状态差异（榛蘑（干）vs 榛蘑（水发））
    是不同行名，不算重名——由 find_food 状态优先级处理。
    """
    rows = _load_csv()
    by_name: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by_name.setdefault(r["name"], []).append(r)
    dups = {k: v for k, v in by_name.items() if len(v) > 1}
    assert not dups, (
        f"发现 {len(dups)} 组精确重名行（数据修正回归，需人工核对）: "
        + "; ".join(f"{k}x{len(v)}" for k, v in list(dups.items())[:8]))


def test_csv_no_new_duplicate_conflicts():
    """（保留）重名行内同列数值倍差 >3 且不在已知清单 → fail。"""
    rows = _load_csv()
    by_name: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by_name.setdefault(r["name"], []).append(r)

    new_conflicts = []
    for name, group in by_name.items():
        if len(group) < 2:
            continue
        for col in ("energy_kcal", "protein_g", "potassium_mg", "phosphorus_mg"):
            vals = []
            for r in group:
                try:
                    vals.append(float(r.get(col) or 0))
                except ValueError:
                    continue
            vals = [v for v in vals if v > 0]
            if len(vals) >= 2 and max(vals) / min(vals) > CONFLICT_RATIO:
                if (name, col) not in DOCUMENTED_CONFLICTS:
                    new_conflicts.append((name, col, min(vals), max(vals)))

    assert not new_conflicts, (
        f"新增重名行数据冲突 {len(new_conflicts)} 组（需人工核对或登记到 DOCUMENTED_CONFLICTS）: "
        + "; ".join(f"{n}.{c}={lo}vs{hi}" for n, c, lo, hi in new_conflicts[:8]))


def _base_name(name: str) -> str:
    """去括号与修饰（鸡蛋（代表值）→ 鸡蛋），用于跨源精确匹配。"""
    return re.split(r"[（(]", name)[0].strip()


def test_ckd_json_vs_csv_no_new_drift():
    """foods_ckd.json 的 csv_name 必须能在 food_data.csv **精确解析**且**同基名**。

    N3 修复（2026-08-16，九审）：此前本测试比较 JSON 内嵌 *_per_100g 与 CSV
    偏差（28 处 >25% 只 warn 不 fail）——但 H2/H3 修复（2026-08-15）后 mealplan
    已**不再消费内嵌值**（数值单一权威源 = food_data.csv，经 find_food(csv_name)
    解析），内嵌值成了死数据，偏差测试是"死数据 vs 权威源"的空转，且掩盖真实
    映射错误（如米粉(熟) csv_name 指向米饭（蒸，代表值）——可解析但取到 116 kcal
    米饭值而非米粉值）。现改为**硬断言 csv_name 解析一致性**：
    ① 每个 csv_name 必须能被 find_food 精确解析（fail-fast 同口径）；
    ② 解析命中行的基名必须与 JSON 条目基名一致（防"米粉→米饭"式错配）；
    ③ 明确"熟制/干制"状态语义：state=cooked 的条目不得指向干制行
    （能量/钾磷差 3 倍+，如米粉熟 109 vs 干 349）。
    """
    rows = _load_csv()
    by_name = {r["name"]: r for r in rows}
    ckd = json.loads(CKD_JSON_PATH.read_text(encoding="utf-8"))
    ckd_foods = ckd if isinstance(ckd, list) else ckd.get("foods", [])

    from CKDNutri_nutrition_mcp.fooddb import find_food

    failures: list[str] = []
    for f in ckd_foods:
        name = f.get("name") or ""
        csv_name = f.get("csv_name") or name
        row = find_food(csv_name)
        if row is None:
            failures.append(f"{name!r}: csv_name={csv_name!r} 无法在 food_data.csv 精确解析")
            continue
        # ② 基名一致性：JSON 条目名与 CSV 命中行必须同基名（去括号修饰）
        if _base_name(csv_name) != _base_name(row["name"]):
            failures.append(
                f"{name!r}: csv_name={csv_name!r} 解析命中 {row['name']!r}——基名不一致（疑似错配）")
            continue
        # ③ 生熟/干湿语义：cooked 状态不得映射干制行（干米粉 349 vs 熟 109）
        state = f.get("state")
        row_name = row["name"]
        if state == "cooked" and any(tag in row_name for tag in ("干", "（干）", "（干，细）")):
            failures.append(
                f"{name!r}: state=cooked 但 csv_name 指向干制行 {row_name!r}"
                "（能量/钾磷差 3 倍+，疑似错配）")

    assert not failures, (
        f"foods_ckd.json csv_name 解析一致性失败 {len(failures)} 处: "
        + "; ".join(failures[:8]))


def test_csv_duplicate_names_registry_complete():
    """已知冲突登记与实际冲突行一致性：登记过的 (name, col) 必须是真实存在的冲突。"""
    rows = _load_csv()
    by_name: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by_name.setdefault(r["name"], []).append(r)

    real = set()
    for name, group in by_name.items():
        if len(group) < 2:
            continue
        for col in ("energy_kcal", "protein_g", "potassium_mg", "phosphorus_mg"):
            vals = []
            for r in group:
                try:
                    vals.append(float(r.get(col) or 0))
                except ValueError:
                    continue
            vals = [v for v in vals if v > 0]
            if len(vals) >= 2 and max(vals) / min(vals) > CONFLICT_RATIO:
                real.add((name, col))
    stale = DOCUMENTED_CONFLICTS - real
    # 只告警不 fail（登记可能指向已修复的行）
    if stale:
        print(f"[warn] DOCUMENTED_CONFLICTS 中 {len(stale)} 条已不再冲突（可清理登记）: "
              + "; ".join(f"{n}.{c}" for n, c in sorted(stale)[:8]))


if __name__ == "__main__":
    test_csv_no_duplicate_rows()
    test_csv_no_new_duplicate_conflicts()
    test_ckd_json_vs_csv_no_new_drift()
    test_csv_duplicate_names_registry_complete()
    print("DATA QUALITY OK")
