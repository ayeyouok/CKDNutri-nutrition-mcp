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
    """foods_ckd.json 与 food_data.csv 双源偏差。

    说明（X5，2026-08-14）：foods_ckd.json 为 CKD「近似值子集」（mealplan 已声明跨源
    以全量库为准），生熟/加工状态差异天然存在（米粉熟 110 vs 干 349、白菜脱水等），
    且基名匹配存在脱水/罐头歧义——故**只 warn 不 fail**（不阻塞 CI），偏差清单打印
    供人工核对；真正硬失败的是 test_csv_no_new_duplicate_conflicts（同源自相矛盾）。
    """
    rows = _load_csv()
    # 基名 → 代表值行（优先「代表值」行，其次 unit_grams==100）
    def _score(r):
        return 2 if "代表值" in r["name"] else (1 if r.get("unit_grams") == "100" else 0)

    by_base: dict[str, dict[str, str]] = {}
    for r in rows:
        b = _base_name(r["name"])
        if b not in by_base or _score(r) > _score(by_base[b]):
            by_base[b] = r
    ckd = json.loads(CKD_JSON_PATH.read_text(encoding="utf-8"))
    ckd_foods = ckd if isinstance(ckd, list) else ckd.get("foods", [])

    drifts = []
    for f in ckd_foods:
        csv_hit = by_base.get(_base_name(f["name"]))
        if not csv_hit:
            continue
        for key, col in (("energy_per_100g", "energy_kcal"), ("protein_per_100g", "protein_g"),
                         ("potassium_per_100g", "potassium_mg"), ("phosphorus_per_100g", "phosphorus_mg")):
            try:
                a, b = float(f.get(key) or 0), float(csv_hit.get(col) or 0)
            except (TypeError, ValueError):
                continue
            if a <= 0 or b <= 0:
                continue
            diff = abs(a - b) / max(a, b)
            if diff > SRC_ALLOWED_DIFF:
                drifts.append((f["name"], col, a, b, f"{diff*100:.0f}%", csv_hit["name"]))

    # warn-only：打印供人工核对（近似值子集 + 生熟/加工口径差异为设计内行为）
    if drifts:
        print(f"[warn] foods_ckd.json vs food_data.csv 偏差 >25% 共 {len(drifts)} 处"
              "（近似值子集/生熟口径差异，待营养师核对）:")
        for n, c, a, b, d, hit in drifts[:10]:
            print(f"  {n}.{c}: {a} vs {b} ({d} @ {hit})")


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
