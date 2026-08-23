# 十五审（2026-08-24）回归测试：server.py 弱类型入参防御（bool 归一 / float 强转）
# 覆盖 R1(include_intake/write_mode/include_household 字符串布尔) /
#      R2(pd_glucose_kcal_per_day / albumin_g_L 字符串浮点强转)
import os
os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_NUTRITION_ASSESSMENT_DATA_DIR", "C:/tmp/a207-r15")

import sys
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
import CKDNutri_nutrition_mcp.server as srv


results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


# ---- R1: include_intake="false" 不应查库 ----
r = srv.comprehensive_nutrition_assessment_tool(
    age_years=6.0, sex="M", weight_kg=20.0, height_cm=110.0, ckd_stage=3,
    is_edema=False, include_intake="false", patient_id="P0007")
notes = r.get("data", {}).get("notes", [])
looked = any("饮食日记" in n for n in notes)
check("R1-include_false_no_db", r.get("ok") is True and not looked)

# ---- R1: include_intake="true" 应查库 ----
r2 = srv.comprehensive_nutrition_assessment_tool(
    age_years=6.0, sex="M", weight_kg=20.0, height_cm=110.0, ckd_stage=3,
    is_edema=False, include_intake="true", patient_id="P0007")
notes2 = r2.get("data", {}).get("notes", [])
looked2 = any("饮食日记" in n for n in notes2)
check("R1-include_true_db", looked2)

# ---- R1: write_mode="false" 归一为 False（dry-run 不写）----
r3 = srv.upsert_food_diary_tool(
    patient_id="P0007",
    entries=[{"date": "2026-08-20", "meal": "早餐", "food": "鸡蛋", "amount": "1个"}],
    write_mode="false")
check("R1-upsert_write_false_ok", r3.get("ok") is True)

# ---- R1: include_household="false" 归一为 False ----
r4 = srv.lookup_food_nutrients_tool(food_name="鸡蛋", include_household="false")
check("R1-lookup_household_false_ok", r4.get("ok") is True)

# ---- R2: calc_prnt_targets_tool pd_glucose_kcal_per_day="50.0" 字符串不再 INTERNAL_ERROR ----
# 注：core 业务校验要求 pd 扣减仅适用于腹膜透析，故用 peritoneal 模式验证强转+扣减生效
r5 = srv.calc_prnt_targets_tool(
    age_years=6.0, sex="M", weight_kg=20.0, height_cm=110.0,
    ckd_stage=3, dialysis_mode="peritoneal", pd_glucose_kcal_per_day="50.0")
check("R2-prnt_str_pd_kcal_ok", r5.get("ok") is True)
if r5.get("ok"):
    e = r5["data"]["energy"]["target_kcal_per_day"]
    # 腹透扣减 50kcal 后目标应低于未扣减基准（验证确实生效而非被忽略）
    r5b = srv.calc_prnt_targets_tool(
        age_years=6.0, sex="M", weight_kg=20.0, height_cm=110.0,
        ckd_stage=3, dialysis_mode="peritoneal")
    e0 = r5b["data"]["energy"]["target_kcal_per_day"] if r5b.get("ok") else None
    check("R2-prnt_pd_deduction_applied", e0 is not None and e < e0)

# ---- R2: assess_pew_risk_tool albumin_g_L="35.5" 字符串正常（server 层强转后 core 接受）----
r6 = srv.assess_pew_risk_tool(
    avg_protein_g=40.0, avg_energy_kcal=1200.0,
    target_protein_g=45.0, target_energy_kcal=1300.0, albumin_g_L="35.5")
check("R2-pew_str_albumin_ok", r6.get("ok") is True)

# ---- R2: DAG pd_glucose_kcal_per_day="50.0" 字符串不崩（peritoneal 模式）----
r7 = srv.comprehensive_nutrition_assessment_tool(
    age_years=6.0, sex="M", weight_kg=20.0, height_cm=110.0, ckd_stage=3,
    dialysis_mode="peritoneal", is_edema=False, include_intake="false",
    pd_glucose_kcal_per_day="50.0",
    avg_protein_g=45.0, avg_energy_kcal=1300.0)
check("R2-dag_str_pd_kcal_ok", r7.get("ok") is True)

# ---- R2: DAG albumin_g_L 字符串不崩（血清白蛋白参与 PEW）----
r8 = srv.comprehensive_nutrition_assessment_tool(
    age_years=6.0, sex="M", weight_kg=20.0, height_cm=110.0, ckd_stage=3,
    is_edema=False, include_intake="false", serum_albumin_g_l="28.0",
    avg_protein_g=45.0, avg_energy_kcal=1300.0)
check("R2-dag_str_albumin_ok", r8.get("ok") is True)
if r8.get("ok"):
    pew = r8["data"].get("pew") or {}
    check("R2-dag_albumin_pew_score", "score" in pew)


# ---- 汇总 ----
failed = [n for n, c in results if not c]
print("\n==== 十五审回归汇总 ====")
print(f"总计 {len(results)} 项，通过 {len(results)-len(failed)}，失败 {len(failed)}")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
print("ALL PASS")
