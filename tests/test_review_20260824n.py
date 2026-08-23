# 十审（2026-08-24）回归测试：core.py 4 项修复防回归
# 约定：顶部 setdefault 注入测试 env+caller；零 pytest 依赖；不跨包 import。
import os
import sys
import math

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_NUTRITION_ASSESSMENT_DATA_DIR",
                      "C:/tmp/a207-ci-check-n")
os.environ.setdefault("A207_CHILD_PATIENT_ID", "P0007")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from CKDNutri_nutrition_mcp import core  # noqa: E402


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


def test_n01_whz_overweight_in_growth_status():
    """N-01（十审 Claim 1）：0-2 岁身长别体重超重（WHZ>2）须纳入 growth_status=overweight，
    与 d['whz']['wasting']='超重' 一致；此前决策链漏判 WHZ>2 致同函数输出自相矛盾。"""
    # 12 月龄、身高 75cm、体重 12kg → 明显超重，WHZ 应 >+2；BAZ 在 <2 岁不计算（baz_z=None）
    r = core.calc_growth_zscore(age_years=1.0, sex="M", height_cm=75, weight_kg=12)
    d = r["data"]
    check("N-01 ok", r["ok"], r)
    check("N-01 whz 存在且 >2", d.get("whz") is not None and d["whz"]["z"] > 2,
          f"whz_z={d.get('whz', {}).get('z')}")
    check("N-01 whz.wasting=超重", d["whz"]["wasting"] == "超重", d["whz"].get("wasting"))
    check("N-01 growth_status=overweight", d["growth_status_suggestion"] == "overweight",
          d["growth_status_suggestion"])
    # 对照：消瘦 WHZ<-2 仍判 failure（回归保险）
    r2 = core.calc_growth_zscore(age_years=1.0, sex="M", height_cm=75, weight_kg=5.5)
    check("N-01 消瘦仍 failure", r2["data"]["growth_status_suggestion"] == "failure",
          r2["data"]["growth_status_suggestion"])


def test_n02_edema_no_height_warning_not_fake():
    """N-02（十审 Claim 2）：is_edema=True 但缺身高（height_cm=0）未成功校正时，warnings
    不得谎报"已启用水肿校正：以理想体重..."，须提示缺身高按实际体重开处方。"""
    r = core.calc_prnt_targets(
        age_years=10, sex="M", weight_kg=25.0, height_cm=0.0,
        dialysis_mode="none", is_edema=True)
    check("N-02 ok", r["ok"], r)
    warns = r["data"].get("warnings", [])
    check("N-02 weight_basis 未校正", "水肿未校正" in r["data"]["weight_basis"],
          r["data"]["weight_basis"])
    check("N-02 不含谎报校正文案",
          not any("已启用水肿校正：以理想体重" in w for w in warns), warns)
    check("N-02 含缺身高提示",
          any("缺少有效身高" in w for w in warns), warns)
    # 对照：提供身高时仍正确报校正
    r3 = core.calc_prnt_targets(
        age_years=10, sex="M", weight_kg=25.0, height_cm=140.0,
        dialysis_mode="none", is_edema=True)
    warns3 = r3["data"].get("warnings", [])
    check("N-02 有身高正确校正文案",
          any("已启用水肿校正：以理想体重" in w for w in warns3), warns3)


def test_n03_bmi_upper_bound_rejected():
    """N-03（十审 Claim 3）：显式传入 bmi=250（误填）须抛 ValueError，不得算荒谬 Z 分。"""
    raised = False
    try:
        core.calc_growth_zscore(age_years=10, sex="M", height_cm=140, weight_kg=50, bmi=250)
    except ValueError as e:
        raised = True
        check("N-03 报错含生理上界", "80" in str(e), str(e))
    check("N-03 已抛 ValueError", raised)
    # 正常 bmi 不误伤
    r = core.calc_growth_zscore(age_years=10, sex="M", height_cm=140, weight_kg=50, bmi=18.0)
    check("N-03 正常 bmi 通过", r["ok"], r)


def test_n04_ideal_body_weight_nan_safe():
    """N-04（十审 Claim 4）：ideal_body_weight_kg 对 height_cm=NaN 须返回 None（fail-soft），
    不得穿透返回 NaN。"""
    r = core.ideal_body_weight_kg(5.0, "M", float("nan"))
    check("N-04 NaN→None", r is None, repr(r))
    # 正常身高仍返回有限值
    r2 = core.ideal_body_weight_kg(5.0, "M", 110.0)
    check("N-04 正常→有限值", r2 is not None and math.isfinite(r2), repr(r2))
    # 负身高仍 None
    check("N-04 负身高→None", core.ideal_body_weight_kg(5.0, "M", -10.0) is None)


if __name__ == "__main__":
    test_n01_whz_overweight_in_growth_status()
    test_n02_edema_no_height_warning_not_fake()
    test_n03_bmi_upper_bound_rejected()
    test_n04_ideal_body_weight_nan_safe()
    print("十审（2026-08-24）REGRESSION OK（4 用例）")
