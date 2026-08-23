"""CKDNutri-nutrition-mcp BUG-01~04 复核回归测试（2026-08-23）。

覆盖本轮审查「属实·已修」项的防回归（零 pytest 依赖，直接运行）：
- BUG-01（diary 时区）：未来日期判定改用北京业务日（UTC+8），中国早晨（UTC 仍前一天）
  记录的「当天」日记不再被误判为未来而静默跳过。
- BUG-02（fooddb 代表值）：代表值优先仅在同源基名组内生效，杜绝跨食材劫持。
- BUG-03（diary 天数稀释）：缺失/非法日期不另算一天，日均值分母仅计 ISO 日期。
- BUG-04（targets 强类型）：weight_kg 纳入数值校验，str/bool/NaN/≤0 显式 INVALID_INPUT。

驳回/澄清项：
- BUG-03 报告中「2026/08/23 斜杠格式另成第三桶」前提不实——_normalize_date 已支持
  YYYY/M/D，斜杠会被归一化为 2026-08-23 与同日合并；真正致稀释的是「未标注日期」桶。
- BUG-02 报告「苹果梨→苹果（代表值）」在当前数据与 0.80 difflib 阈值下不可复现
  （苹果梨 对 苹果（代表值） score=99，不入 hits）；但代码确实违反 N-S2 文档「组内
  代表值」契约，修复为防御性对齐，对正常查询行为完全等价。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime as _RealDT
from datetime import timedelta, timezone
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
from CKDNutri_nutrition_mcp import diary  # noqa: E402
from CKDNutri_nutrition_mcp import fooddb  # noqa: E402
from CKDNutri_nutrition_mcp import targets  # noqa: E402


def _ok(name: str, cond: bool) -> None:
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        raise SystemExit(f"断言失败: {name}")


# ---- BUG-01：北京业务日判定（中国早晨当天日记不被误判未来）----
class _FakeNow(_RealDT):
    """模拟中国早晨：UTC 仍停留在前一天 23:30，北京 = 当天 07:30。

    旧实现用 datetime.now(timezone.utc).date() → 前一天，会把「当天」(2026-08-24)
    日记误判为未来跳过；新实现用 datetime.now(_BIZ_TZ).date() → 当天，不跳过。
    """

    @classmethod
    def now(cls, tz=None):
        if tz is diary._BIZ_TZ:
            return _RealDT(2026, 8, 24, 7, 30, tzinfo=diary._BIZ_TZ)
        return _RealDT(2026, 8, 23, 23, 30, tzinfo=timezone.utc)


_real_dt = diary.datetime
diary.datetime = _FakeNow
try:
    with as_caller("doctor_assistant"):
        _b1 = diary.sum_diet_intake(
            [{"food": "米饭", "grams": 100, "date": "2026-08-24"}], target=None)
        _b1_future = diary.sum_diet_intake(
            [{"food": "米饭", "grams": 100, "date": "2026-08-24"},
             {"food": "米饭", "grams": 100, "date": "2026-08-25"}], target=None)
finally:
    diary.datetime = _real_dt

_b1_dates = [r["date"] for r in _b1["data"]["per_day"]]
_ok("BUG-01 北京早晨「当天」日记不被误判未来（2026-08-24 入桶）",
    "2026-08-24" in _b1_dates)
_ok("BUG-01 真正未来日期仍被跳过（2026-08-25 不入桶）",
    "2026-08-25" not in [r["date"] for r in _b1_future["data"]["per_day"]])


# ---- BUG-02：代表值仅在同源组内优先（防跨食材劫持）----
_orig_search = fooddb.search_food
_fuji = {"name": "红富士苹果", "aliases": [], "category": "仁果类", "subcategory": "",
         "edible_pct": 100.0, "unit_name": "100g", "unit_grams": 100.0, "unit_desc": "",
         "note": "", "energy_kcal": 52.0, "protein_g": 0.7, "fat_g": 0.4, "carb_g": 11.7,
         "potassium_mg": 115.0, "phosphorus_mg": 11.0, "sodium_mg": 3.0,
         "calcium_mg": 5.0, "missing_nutrients": []}
_banana = {"name": "香蕉（代表值）", "aliases": ["香蕉"], "category": "仁果类",
           "subcategory": "", "edible_pct": 100.0, "unit_name": "100g", "unit_grams": 100.0,
           "unit_desc": "", "note": "", "energy_kcal": 93.0, "protein_g": 1.4, "fat_g": 0.2,
           "carb_g": 22.0, "potassium_mg": 358.0, "phosphorus_mg": 28.0, "sodium_mg": 1.0,
           "calcium_mg": 7.0, "missing_nutrients": []}
# 构造命中：查询"红富士"，hits 含「红富士苹果」(base=红富士苹果) 与「香蕉（代表值）」
# (base=香蕉)。旧逻辑在全局 hits 扫描代表值会先返回排在前面的「香蕉（代表值）」(劫持)；
# 新逻辑只在 base==hits[0].base 的组内找代表值 → 返回同源「红富士苹果」。
fooddb.search_food = lambda q, limit=10: [_fuji, _banana]
try:
    _b2 = fooddb.find_food("红富士")
finally:
    fooddb.search_food = _orig_search
_ok("BUG-02 代表值仅在组内优先（返回同源红富士苹果，非跨组香蕉代表值）",
    _b2 is not None and _b2["name"] == "红富士苹果")


# ---- BUG-03：缺失日期不另算一天，日均值不被稀释 ----
_b3_diary = [
    {"food": "米饭", "grams": 100, "date": "2026-08-23"},
    {"food": "米饭", "grams": 100},                  # 缺失日期 → "未标注日期"
    {"food": "米饭", "grams": 100, "date": "2026/08/23"},  # 斜杠→归一化 2026-08-23
]
with as_caller("doctor_assistant"):
    _b3 = diary.sum_diet_intake(_b3_diary, target=None)
_b3_d = _b3["data"]
# per_day 应为 {2026-08-23(2项), 未标注日期(1项)}；有效 ISO 天仅 1 天
_ok("BUG-03 缺失日期不另算一天（days=1）", _b3_d["days"] == 1)
# 米饭 116 kcal/100g × 3 = 348；day_count=1 → 均值 348；若被稀释成 days=2 → 174
_ok("BUG-03 日均值未被稀释（energy_kcal=348.0）",
    abs(_b3_d["daily_average"]["energy_kcal"] - 348.0) < 1e-6)


# ---- BUG-04：weight_kg 强类型校验 ----
with as_caller("doctor_assistant"):
    _r1 = targets.calc_pd_glucose_absorption(100.0, 8.0, 3, "average", weight_kg="20")
    _r2 = targets.calc_pd_glucose_absorption(100.0, 8.0, 3, "average", weight_kg=float("nan"))
    _r3 = targets.calc_pd_glucose_absorption(100.0, 8.0, 3, "average", weight_kg=20.0)
_ok("BUG-04 weight_kg=str 不崩溃→INVALID_INPUT",
    _r1.get("ok") is False and _r1.get("error") == "INVALID_INPUT")
_ok("BUG-04 weight_kg=nan 显式拒绝", _r2.get("ok") is False)
_ok("BUG-04 weight_kg=20.0 正常产出 per_kg",
    _r3.get("ok") is True and _r3["data"]["absorbed_energy_kcal_per_kg"] is not None)

print("\nALL PASS: BUG-01/02/03/04 防回归通过")
