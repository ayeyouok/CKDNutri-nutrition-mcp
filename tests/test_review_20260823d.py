"""CKDNutri-nutrition-mcp BUG-P0-01/02、BUG-P1-02 复核回归测试（2026-08-23）。

覆盖本轮审查「属实·已修」项的防回归（零 pytest 依赖，直接运行）：
- BUG-P0-01（measures 前缀食物名）：「米饭2碗」「苹果3个」等食物名前置份量，parse_portion
  须先剥前缀再解析数量；此前只剥后缀，数量被截断为 1.0（摄入腰斩/低估 50%+）。
- BUG-P0-02（fooddb 组合烹调脏状态）：「焯水+红烧」红烧未知 → 此前 factors 被部分污染
  （非空）导致降级 raw 后仍套用污染的 0.7 折减系数；修复后全未知段不提交、按 raw 回落。
- BUG-P1-02（diary missing_foods 去重）：多餐重复同款缺失食物不再成倍虚夸种数与展示。

驳回/澄清项（本轮）：
- BUG-P1-01 报告「2026.08.22 点号格式无法归一化、被踢出分母致日均值虚高 50%」前提不实——
  _normalize_date 已支持 YYYY.M.D（见 core.py:1257），点号日期归一化为 2026-08-22，仍计入
  day_count，不会稀释；唯一致稀释的「未标注日期」桶已被 _is_iso_day 排除。本测试含 dot-date
  分母断言以固化该结论。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from a207_policy import as_caller  # noqa: E402

from CKDNutri_nutrition_mcp import measures, fooddb, diary  # noqa: E402
from CKDNutri_nutrition_mcp.diary import sum_diet_intake  # noqa: E402


_pass = 0
_fail = 0


def _ok(name: str, cond: bool) -> None:
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}")


# ---- BUG-P0-01：食物名前置份量解析 ----
def _row(name, aliases=(), unit_grams=100.0, unit_name="份"):
    return {"name": name, "aliases": list(aliases),
            "unit_grams": unit_grams, "unit_name": unit_name}


print("== BUG-P0-01 measures 前缀食物名 ==")
# 米饭：unit 150g/碗
_rice = _row("米饭", unit_grams=150.0, unit_name="碗")
_p = measures.parse_portion("米饭2碗", _rice)
_ok("米饭2碗 -> 300g（2×150）", abs(_p["grams"] - 300.0) < 1e-6)
_p2 = measures.parse_portion("苹果3个", _row("苹果", unit_grams=100.0, unit_name="个"))
_ok("苹果3个 -> 300g（3×100）", abs(_p2["grams"] - 300.0) < 1e-6)
# 后缀写法仍正确
_p3 = measures.parse_portion("2碗米饭", _rice)
_ok("2碗米饭（后缀）-> 300g", abs(_p3["grams"] - 300.0) < 1e-6)


# ---- BUG-P0-02：组合烹调脏状态 ----
print("== BUG-P0-02 fooddb 组合烹调脏状态 ==")
# 找一个已知烹调系数非 1 的食物行（如含钾蔬菜），用「焯水+未知」组合
_row_cooked = fooddb.find_food("菠菜") or fooddb.find_food("白菜") or fooddb.find_food("米饭")
_scale_dirty = fooddb.scale_nutrients(_row_cooked, 100.0, cooking="焯水+红烧")
# 红烧不在 COOKING_LOSS -> 全未知段，factors 不提交 -> 必须按 raw（系数 1.0）回落
_expected_raw = fooddb.scale_nutrients(_row_cooked, 100.0, cooking="raw")
_ok("「焯水+红烧」全未知段按 raw 回落（钾一致）",
    abs(_scale_dirty["potassium_mg"] - _expected_raw["potassium_mg"]) < 1e-6)
_ok("「焯水+红烧」cooking 标为 raw（非残留焯水系数）",
    _scale_dirty["cooking"] == "raw")
_ok("「焯水+红烧」带 cooking_warning（未知回落提示）",
    bool(_scale_dirty.get("cooking_warning")))
# 全已知组合仍正常相乘
_scale_combo = fooddb.scale_nutrients(_row_cooked, 100.0, cooking="焯水+浸泡")
_ok("「焯水+浸泡」全已知段正常生效（非 raw）",
    _scale_combo["cooking"] not in ("raw",))


# ---- BUG-P1-02：missing_foods 去重 ----
print("== BUG-P1-02 diary missing_foods 去重 ==")
with as_caller("doctor_assistant"):
    _diary = [
        {"food": "荔枝（干）", "grams": 50, "date": "2026-08-20", "meal": "早餐"},
        {"food": "荔枝（干）", "grams": 30, "date": "2026-08-20", "meal": "午餐"},
        {"food": "荔枝（干）", "grams": 40, "date": "2026-08-21", "meal": "晚餐"},
    ]
    _res = sum_diet_intake(_diary)
_ok("荔枝（干）缺失告警种数去重=1（非3）",
    any("1 种食物存在营养数据缺失" in w for w in _res["data"].get("warnings", [])))


# ---- BUG-P1-01 驳回固化：点号日期仍计入分母，不稀释 ----
print("== BUG-P1-01 驳回固化（dot-date 分母） ==")
with as_caller("doctor_assistant"):
    _d = [
        {"food": "米饭", "grams": 100, "date": "2026-08-20"},
        {"food": "米饭", "grams": 100, "date": "2026/08/21"},
        {"food": "米饭", "grams": 100, "date": "2026.08.22"},  # 点号格式
    ]
    _r = sum_diet_intake(_d)
    # 三天摄入相同 -> 日均应与单日一致（不除以2/被踢出分母）
    _day_energy = _r["data"]["per_day"][0]["energy_kcal"]
    _avg = _r["data"]["daily_average"]["energy_kcal"]
    _ok("dot/slash 日期均归一化计入 day_count=3", _r["data"]["days"] == 3)
    _ok("日均 ≈ 单日（无稀释）", abs(_avg - _day_energy) < 1e-6)


print(f"\n=== {_pass} passed, {_fail} failed ===")
raise SystemExit(1 if _fail else 0)
