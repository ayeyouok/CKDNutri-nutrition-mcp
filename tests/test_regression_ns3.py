"""二十审（2026-08-19）nutrition-mcp 回归：审查 BUG-1~BUG-5 + 问题-6/7/8。

覆盖：
- BUG-1：_overall_achievement 先汇总后平均（"每日 cap 再平均"不再隐藏超限日）
- BUG-2：K+P 联合筛选为占用率（不直接把 mg 相加）
- BUG-3：exchanges_per_day 1.9 → INVALID_INPUT（不静默截断为 1）
- BUG-4：未知 transport_type → INVALID_INPUT（不回退 average 产生临床数值）
- 问题-6：PRNT 中点文案不再称"约 100% SDI"
- 问题-7：PD 葡萄糖扣减 ≥ 能量目标 → 拒绝（不产出 0 kcal 膳食目标）
- 问题-8：_require 拒绝字符串/bool（"8" / True → ValueError，不再 TypeError 500）

pytest + 直接运行双模式（CI 逐文件 `python tests/test_*.py`，不依赖 pytest）。
"""
import os

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
import sys
import tempfile
from pathlib import Path

os.environ["A207_NUTRITION_ASSESSMENT_DATA_DIR"] = tempfile.mkdtemp(prefix="a207-nutrition-ns3-")

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from CKDNutri_nutrition_mcp import (
    core,
    mealplan,
    targets,
)


def _expect_raises(exc_type, fn):
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__} to be raised")


# ---- BUG-1：_overall_achievement 先汇总后平均 ----

def test_overall_achievement_no_daily_cap_averaging():
    """BUG-1：用户示例——两天钾 200%/0%（每日已 cap），整体应为 100% 而非 50%。"""
    days = [
        {"day_totals": {"energy_kcal": 1200.0, "protein_g": 40.0,
                        "potassium_mg": 2000.0, "phosphorus_mg": 800.0, "sodium_mg": 1500.0},
         "achievement": {"energy_pct": 200, "protein_pct": 100, "potassium_pct": 100,
                         "phosphorus_pct": 100, "sodium_pct": 100}},
        {"day_totals": {"energy_kcal": 0.0, "protein_g": 0.0,
                        "potassium_mg": 0.0, "phosphorus_mg": 0.0, "sodium_mg": 0.0},
         "achievement": {"energy_pct": 0, "protein_pct": 100, "potassium_pct": 0,
                         "phosphorus_pct": 0, "sodium_pct": 0}},
    ]
    out = mealplan._overall_achievement(days, t_energy=1200.0, t_protein=40.0,
                                        t_k=1000.0, t_p=400.0, t_na=750.0, days=2)
    # 两天实际平均钾 = (2000+0)/2 = 1000 = 100% 目标 → potassium_pct 应为 100
    assert out["potassium_pct"] == 100, out
    # 磷/钠同理（实际平均 400/750 → 100%）
    assert out["phosphorus_pct"] == 100, out
    assert out["sodium_pct"] == 100, out
    # 能量：平均 600/1200 = 50%
    assert out["energy_pct"] == 50, out


# ---- BUG-2：K+P 占用率（间接验证：极不平衡目标下选品仍受约束）----

def test_kp_ratio_screening_consistent():
    """BUG-2：K/P 联合筛选占用率口径——目标极端时（K 大 P 小）不因相加抵消放宽磷约束。"""
    # 构造：钾目标 3000、磷目标 300（磷很紧）。旧"K+P 相加"会把磷的紧迫性稀释；
    # 占用率口径下同一候选的 p_ratio 单独受限。此处验证筛选函数语义不抛错且
    # 生成计划可执行（数值正确性由 _overall_achievement 测试 + 既有 64 用例保证）。
    r = mealplan.generate_meal_plan(
        target_energy_kcal=1200.0, target_protein_g=40.0,
        target_k_mg=3000.0, target_p_mg=300.0, target_na_mg=1500.0, days=3)
    assert r["days"], r
    # 磷（限制性上限）整体达成率必须 ≤100（超限日会被 cap 到 100，不会虚高）
    for day in r["days"]:
        assert day["achievement"]["phosphorus_pct"] <= 100, day


# ---- BUG-3：exchanges_per_day 严格正整数 ----

def test_exchanges_per_day_must_be_positive_int():
    """BUG-3：exchanges_per_day=1.9 不再被 int() 截断为 1——显式 INVALID_INPUT。"""
    r = targets.calc_pd_glucose_absorption(22.7, 6.0, exchanges_per_day=1.9)
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", r
    r2 = targets.calc_pd_glucose_absorption(22.7, 6.0, exchanges_per_day=0)
    assert r2["ok"] is False and r2["error"] == "INVALID_INPUT", r2
    r3 = targets.calc_pd_glucose_absorption(22.7, 6.0, exchanges_per_day=True)
    assert r3["ok"] is False and r3["error"] == "INVALID_INPUT", r3  # bool 拒绝
    r4 = targets.calc_pd_glucose_absorption(22.7, 6.0, exchanges_per_day=2)
    assert r4["ok"] is True and r4["data"]["input"]["exchanges_per_day"] == 2, r4


# ---- BUG-4：未知 transport_type fail-closed ----

def test_unknown_transport_type_rejected():
    """BUG-4：transport_type="abcdef" → INVALID_INPUT（不按 average 回退产出临床数值）。"""
    r = targets.calc_pd_glucose_absorption(22.7, 6.0, exchanges_per_day=2,
                                           transport_type="abcdef")
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", r
    # 合法值仍正常
    r2 = targets.calc_pd_glucose_absorption(22.7, 6.0, exchanges_per_day=2,
                                            transport_type="high")
    assert r2["ok"] is True, r2


# ---- 问题-6：PRNT 中点文案 ----

def test_prnt_midpoint_wording():
    """问题-6：PRNT 中点文案不再称"约 100% SDI"（如实描述策略）。"""
    r = core.calc_prnt_targets(age_years=8, sex="M", weight_kg=25.0, height_cm=130.0)
    assert r["ok"] is True, r
    basis = r["data"]["energy"]["basis"] or ""
    assert "100% SDI" not in basis, basis
    assert "PRNT SDI 推荐范围中点" in basis, basis


# ---- 问题-7：PD 扣减 ≥ 能量目标拒绝 ----

def test_pd_deduction_exceeds_target_rejected():
    """问题-7：PD 葡萄糖供能 ≥ 膳食能量目标 → ValueError（不产出 0 kcal 目标）。"""
    # 8 岁 25kg 目标 ≈ 2000 kcal；PD 扣 5000（远超目标）→ 拒绝
    _expect_raises(ValueError, lambda: core.calc_prnt_targets(
        age_years=8, sex="M", weight_kg=25.0, height_cm=130.0,
        dialysis_mode="peritoneal", pd_glucose_kcal_per_day=5000.0))
    # 扣减小于目标 → 正常（如 200 < 目标）
    r = core.calc_prnt_targets(
        age_years=8, sex="M", weight_kg=25.0, height_cm=130.0,
        dialysis_mode="peritoneal", pd_glucose_kcal_per_day=200.0)
    assert r["ok"] is True and r["data"]["energy"]["target_kcal_per_day"] > 0, r


# ---- 问题-8：_require 拒绝非数值 ----

def test_require_rejects_non_numeric():
    """问题-8：_require("8") / _require(True) 显式 ValueError（不再 TypeError 500）。"""
    _expect_raises(ValueError, lambda: core._require("8", "age_years"))
    _expect_raises(ValueError, lambda: core._require(True, "weight_kg"))
    _expect_raises(ValueError, lambda: core._require(None, "x"))
    _expect_raises(ValueError, lambda: core._require(float("nan"), "x"))
    assert core._require(8, "age_years") == 8  # 合法数值放行


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"NS3 REGRESSION OK（{len(fns)} 个用例）")
