# 十一审（2026-08-24）回归测试：core.py 5 项修复防回归（P0-1/P0-2/P1-6/P1-7/P2-8）
# 约定：顶部 setdefault 注入测试 env+caller；零 pytest 依赖；不跨包 import。
import os
import sys
import math

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_NUTRITION_ASSESSMENT_DATA_DIR",
                      "C:/tmp/a207-ci-check-o")
os.environ.setdefault("A207_CHILD_PATIENT_ID", "P0007")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from CKDNutri_nutrition_mcp import core  # noqa: E402


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


def test_o01_albumin_unit_no_false_negative():
    """O-01（十一审 P0-1）：真实极重度低白蛋白 9.0 g/L 不得被盲目×10 误判为 90 g/L。
    此前 eff_alb<=10 盲目换算会把危重患儿漏诊为健康。"""
    # 9.0 g/L 危重：蛋白/能量达标，仅白蛋白低——应判 medium 且 rationale 含低白蛋白
    r = core.assess_pew_risk(
        avg_protein_g=20.0, avg_energy_kcal=1000.0,
        target_protein_g=22.0, target_energy_kcal=1100.0, albumin_g_L=9.0)
    pr = r["data"]
    check("O-01 9g/L 不误判正常", pr["pew_risk"] in ("medium", "high"), pr["pew_risk"])
    check("O-01 rationale 含低白蛋白信号", "9.0" in pr["rationale"] or "低白蛋白" in pr["rationale"],
          pr["rationale"])
    check("O-01 未出现 90 g/L 误导", "90" not in pr["rationale"], pr["rationale"])
    # 对照：4.2 g/dL（=42 g/L 正常）仍正确换算为正常，不误判低白蛋白
    r2 = core.assess_pew_risk(
        avg_protein_g=20.0, avg_energy_kcal=1000.0,
        target_protein_g=22.0, target_energy_kcal=1100.0, albumin_g_L=4.2)
    pr2 = r2["data"]
    check("O-01 4.2g/dL 换算后正常", "低白蛋白" not in pr2["rationale"], pr2["rationale"])
    check("O-01 4.2 换算风险低", pr2["pew_risk"] == "low", pr2["pew_risk"])


def test_o02_pd_schofield_not_divergent():
    """O-02（十一审 P0-2）：PD 患儿扣除腹透糖供能后，Schofield 交叉校验须比对扣减前
    总能量需求，不得系统性偏负误报 divergent。"""
    # 非 PD 基线（schofield_cross_check 可能为 None，仅作信息参考，不硬断言）
    r_base = core.calc_prnt_targets(
        age_years=10, sex="M", weight_kg=30.0, height_cm=140.0,
        dialysis_mode="none", ckd_stage=3)
    base_flag = (r_base["data"].get("schofield_cross_check") or {}).get("flag")
    # PD 患儿：腹透葡萄糖供能 300 kcal/day（占目标相当比例）
    r_pd = core.calc_prnt_targets(
        age_years=10, sex="M", weight_kg=30.0, height_cm=140.0,
        dialysis_mode="peritoneal", ckd_stage=5,
        pd_glucose_kcal_per_day=300.0)
    pd_scc = r_pd["data"]["schofield_cross_check"]
    check("O-02 PD 有 schofield 对照", pd_scc is not None, r_pd["data"].get("warnings"))
    pd_flag = pd_scc["flag"]
    pd_dev = pd_scc["deviation_pct_vs_sdi_target"]
    check("O-02 PD 不误报 divergent", pd_flag != "divergent",
          f"flag={pd_flag}, deviation={pd_dev}")
    # 修复验证：deviation 应接近 0 附近（扣减前总目标 vs TDEE），而非系统性偏负 >25%
    # 净膳食(扣减后) = 总目标 - 300，若用净膳食比对会偏负 ~40% 误报 divergent
    check("O-02 deviation 未系统性偏负", pd_dev > -25.0,
          f"deviation={pd_dev}（净膳食比对会≈-41% 误报）")


def test_o03_pew_history_sorted_trend():
    """O-03（十一审 P1-6）：乱序存储的 PEW 历史点经显式排序后，趋势不反转。
    构造：最新=high（恶化），但存储顺序倒放（最新在前）——未排序会取首=high 尾=low 判 improving。"""
    pid = "P2099"
    # 乱序：先放最新(high, 2026-08-20)，再放最早(low, 2026-08-10)
    core._save_patient_pew_store(pid, [
        {"date": "2026-08-20", "level": "high", "score": 80},
        {"date": "2026-08-10", "level": "low", "score": 10},
    ])
    r = core.get_pew_history(pid)
    check("O-03 ok", r["ok"], r)
    # 排序后：首=low(2026-08-10) 尾=high(2026-08-20) → worsening
    check("O-03 乱序后仍判 worsening", r["data"]["trend"] == "worsening", r["data"]["trend"])
    # 对照：正常顺序（最早在前）同样 worsening
    core._save_patient_pew_store(pid, [
        {"date": "2026-08-10", "level": "low", "score": 10},
        {"date": "2026-08-20", "level": "high", "score": 80},
    ])
    r2 = core.get_pew_history(pid)
    check("O-03 正序亦 worsening", r2["data"]["trend"] == "worsening", r2["data"]["trend"])


def test_o04_aggregate_span_warning():
    """O-04（十一审 P1-7）：最近 3 个不重复日期横跨 >14 天时，返回 span_warning 提示。"""
    # 跨月：1月/5月/8月各 1 天
    agg = core._aggregate([
        {"date": "2026-01-10", "energy_kcal": 100, "protein_g": 2,
         "potassium_mg": 10, "phosphorus_mg": 5, "sodium_mg": 1},
        {"date": "2026-05-15", "energy_kcal": 100, "protein_g": 2,
         "potassium_mg": 10, "phosphorus_mg": 5, "sodium_mg": 1},
        {"date": "2026-08-20", "energy_kcal": 100, "protein_g": 2,
         "potassium_mg": 10, "phosphorus_mg": 5, "sodium_mg": 1},
    ])
    check("O-04 含 day_span_days", agg.get("day_span_days", 0) > 14, agg.get("day_span_days"))
    check("O-04 含 span_warning", agg.get("span_warning") is not None, agg.get("span_warning"))
    # 对照：连续 3 天不报 warning
    agg2 = core._aggregate([
        {"date": "2026-08-18", "energy_kcal": 100, "protein_g": 2,
         "potassium_mg": 10, "phosphorus_mg": 5, "sodium_mg": 1},
        {"date": "2026-08-19", "energy_kcal": 100, "protein_g": 2,
         "potassium_mg": 10, "phosphorus_mg": 5, "sodium_mg": 1},
        {"date": "2026-08-20", "energy_kcal": 100, "protein_g": 2,
         "potassium_mg": 10, "phosphorus_mg": 5, "sodium_mg": 1},
    ])
    check("O-04 连续 3 天无 warning", agg2.get("span_warning") is None, agg2.get("span_warning"))
    check("O-04 连续跨度 <=14", agg2.get("day_span_days", 0) <= 14, agg2.get("day_span_days"))


def test_o05_ideal_body_weight_nan_age():
    """O-05（十一审 P2-8）：ideal_body_weight_kg 对 age_years=NaN 须返回 None，不抛 ValueError。"""
    r = core.ideal_body_weight_kg(float("nan"), "M", 100.0)
    check("O-05 NaN age→None", r is None, repr(r))
    # 对照：正常仍返回有限值
    r2 = core.ideal_body_weight_kg(5.0, "M", 110.0)
    check("O-05 正常→有限值", r2 is not None and math.isfinite(r2), repr(r2))


if __name__ == "__main__":
    test_o01_albumin_unit_no_false_negative()
    test_o02_pd_schofield_not_divergent()
    test_o03_pew_history_sorted_trend()
    test_o04_aggregate_span_warning()
    test_o05_ideal_body_weight_nan_age()
    print("十一审（2026-08-24）REGRESSION OK（5 用例）")
