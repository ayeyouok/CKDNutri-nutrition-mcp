"""CKDNutri-nutrition-mcp measures/mealplan/diary 复核回归测试（2026-08-23）。

覆盖本轮审查「属实·已修」项的防回归（零 pytest 依赖，直接运行）：

- 措施1（measures._cn_numeral）："二两"此前被无"十"简写分支解析成 22（2*10+2），
  修复后 = 2（"两"作重量单位，非个位）。连带 parse_portion("二两米饭") 归 100g。
- 措施2（measures._cn_numeral 百）："一百五十"此前 = 105（只取余部首字 5），
  修复后递归解析余部 = 150；"一百二十三"=123、"一百二十"=120。
- 措施6（mealplan day_tot）：day_tot 由分餐明细聚合，与 meal.totals 之和严格自洽。
- 措施7-2（mealplan days 类型）：days=3.5 抛 TypeError 前显式拒绝；days=True 拒绝。
- 措施8（diary 日期告警）：未来日期与「无法归一化」分开计数、独立告警文案。

驳回项（非 bug，附理由）：
- 措施3（fooddb 干品优先）：CKD 限钾磷场景有意的保守设计（干品/生品值优先、家长多称
  量干制原料），非缺陷。
- 措施4（diary 电解质拍平）：PRNT calc_prnt_targets 仅输出 energy/protein 嵌套子块，
  钾磷钠目标由调用方以扁平顶层键（potassium_mg_per_day 等）传入，无需拍平。
- 措施5（foods 蓄肉类其他"错字"）：food_data.csv 实际 category 即"蓄肉类其他"（与代码
  同字），lookup 命中正确；等能量三重 AND 为有意保守设计（P0-1）。
- 措施7-1（veg 缺 K 闸门）：超钾由逐日 potassium_exceeded 告警兜底，属增强项非缺陷。
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
from CKDNutri_nutrition_mcp import measures as M  # noqa: E402
from CKDNutri_nutrition_mcp import mealplan  # noqa: E402
from CKDNutri_nutrition_mcp import diary  # noqa: E402


def _ok(name: str, cond: bool) -> None:
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        raise SystemExit(f"断言失败: {name}")


# ---- 措施1：_cn_numeral "二两" = 2（非 22）----
_cn = M._cn_numeral
_ok("_cn_numeral 二两 = 2", _cn("二两") == 2.0)
_ok("_cn_numeral 二两米饭前缀 = 2", _cn("二两米饭") == 2.0)
_ok("_cn_numeral 两碗 = 2（数词首字仍可用）", _cn("两碗") == 2.0)
_ok("_cn_numeral 三五 = 35（无十简写不变）", _cn("三五") == 35.0)
_ok("_cn_numeral 二十三 = 23", _cn("二十三") == 23.0)

# parse_portion 端到端："二两米饭" 应解析为 2 两 = 100g
_row = {"name": "米饭", "aliases": [], "unit_name": "份", "unit_grams": 100.0,
        "unit_desc": "", "energy_kcal": 116.0, "protein_g": 2.6, "fat_g": 0.3,
        "carb_g": 25.9, "potassium_mg": 25.0, "phosphorus_mg": 28.0,
        "sodium_mg": 2.0, "calcium_mg": 7.0, "missing_nutrients": []}
_p = M.parse_portion("二两米饭", _row)
_ok("parse_portion 二两米饭 → 100g", abs(_p["grams"] - 100.0) < 1e-6)

# ---- 措施2：百位数余部完整解析 ----
_ok("_cn_numeral 一百五十 = 150", _cn("一百五十") == 150.0)
_ok("_cn_numeral 一百五十克前缀 = 150", _cn("一百五十克") == 150.0)
_ok("_cn_numeral 一百二十三 = 123", _cn("一百二十三") == 123.0)
_ok("_cn_numeral 一百二十 = 120", _cn("一百二十") == 120.0)
_ok("_cn_numeral 两百 = 200", _cn("两百") == 200.0)
_ok("_cn_numeral 一百零五 = 105（零分支不变）", _cn("一百零五") == 105.0)
_p2 = M.parse_portion("一百五十克", _row)
_ok("parse_portion 一百五十克 → 150g", abs(_p2["grams"] - 150.0) < 1e-6)

# ---- 措施6：day_tot 由分餐明细聚合，与各餐 totals 之和严格自洽 ----
with as_caller("doctor_assistant"):
    _plan = mealplan.generate_meal_plan(
        target_energy_kcal=1200.0, target_protein_g=40.0,
        target_k_mg=2000.0, target_p_mg=800.0, target_na_mg=1500.0, days=3)
# 逐日校验：把当天各餐 totals 累加后 round，必须 == day_totals（修复前用全天整份
# item 单独 round 求和，与已按餐次 round 的 meal.totals 存在 ±0.1/项 漂移）。
for d in _plan["days"]:
    agg = {k: 0.0 for k in ("energy_kcal", "protein_g", "potassium_mg",
                            "phosphorus_mg", "sodium_mg")}
    for m in d["meals"]:
        for k in agg:
            agg[k] += m["totals"][k]
    _ok(f"day_tot 与分餐求和自洽 day{d['day']}",
        all(abs(round(agg[k], 1) - d["day_totals"][k]) < 1e-6 for k in agg))

# ---- 措施7-2：days 类型校验 ----
for bad in (3.5, True, 2.0):
    try:
        with as_caller("doctor_assistant"):
            mealplan.generate_meal_plan(1200.0, 40.0, 2000.0, 800.0, 1500.0, days=bad)
        raise SystemExit(f"days={bad!r} 未拒绝")
    except ValueError:
        pass
_ok("days=3.5/True 均被显式拒绝", True)
# 正常 int 仍可用
with as_caller("doctor_assistant"):
    _ok("days=7 正常生成", mealplan.generate_meal_plan(
        1200.0, 40.0, 2000.0, 800.0, 1500.0, days=7)["days_count"] == 7)

# ---- 措施8：未来日期 vs 非法格式 分开告警 ----
_diary = [
    {"food": "米饭", "grams": 100, "date": "2020-01-01"},
    {"food": "米饭", "grams": 100, "date": "2099-01-01"},   # 未来 → 跳过
    {"food": "米饭", "grams": 100, "date": "不是日期"},      # 无法归一化 → 分桶
]
with as_caller("doctor_assistant"):
    _res = diary.sum_diet_intake(_diary, target=None)
_warns = _res["data"].get("warnings", [])
_ok("含未来日期告警", any("未来日期" in w for w in _warns))
_ok("含无法归一化告警", any("无法归一化" in w for w in _warns))
# 未来日期条目被跳过、未进入任何日桶（非法格式"不是日期"仍按原样分桶计入 item）。
_day_dates = [r["date"] for r in _res["data"]["per_day"]]
_ok("未来日期未进入汇总（不含 2099-01-01）", "2099-01-01" not in _day_dates)
# BUG12（2026-08-23）：非法格式日期（"不是日期"）不再分桶污染时间序列，改为归入
# bad_dates 并跳过；汇总 item = 仅有效日(2020-01-01) = 1；未来日期同样不计入。
_ok("未来+非法日期均被跳过（item_count=1）", _res["data"]["item_count"] == 1)

print("\nALL PASS: 措施1/2/6/7-2/8 防回归通过")
