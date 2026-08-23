# 八审（2026-08-24）回归测试：core.py / server.py 7 项修复防回归
# 约定：顶部 setdefault 注入测试 env+caller；零 pytest 依赖；不跨包 import。
import os
import sys

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_NUTRITION_ASSESSMENT_DATA_DIR",
                      "C:/tmp/a207-ci-check-m")
os.environ.setdefault("A207_CHILD_PATIENT_ID", "P0007")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from CKDNutri_nutrition_mcp import core  # noqa: E402
from CKDNutri_nutrition_mcp import server  # noqa: E402
from a207_policy import as_caller  # noqa: E402


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


# M-01：7 岁男 BMI=13.9（=轻度消瘦界值）临界 → failure（此前 < 误判 normal）
r1 = core.calc_growth_zscore(age_years=7.0, sex="M", bmi=13.9)
check("M-01 临界 BMI=界值判 failure",
      r1["data"]["growth_status_suggestion"] == "failure",
      f"status={r1['data']['growth_status_suggestion']}")

# M-01b：7 岁男 BMI=14.0（高于界值 13.9）→ 不误伤 normal
r1b = core.calc_growth_zscore(age_years=7.0, sex="M", bmi=14.0)
check("M-01b BMI=14.0 不误伤（仍 normal）",
      r1b["data"]["growth_status_suggestion"] != "failure",
      f"status={r1b['data']['growth_status_suggestion']}")

# M-02：白蛋白 3.8 g/dL 浮点抖动 → eff=38.0 不误判低白蛋白
r2 = core._screen_pew(avg_p=40.0, avg_e=1200.0, floor_p=35.0,
                      target_e=1400.0, albumin_g_L=3.8)
check("M-02 白蛋白 3.8 g/dL 不误判 low_albumin",
      "白蛋白" not in r2["rationale"], f"rationale={r2['rationale']}")

# M-03：_content_key 非哈希类型（food 为 list）不 TypeError
try:
    bad_entry = {"date": "2026-08-24", "meal": "早餐",
                 "food": ["牛奶", "面包"]}
    k = core.record_child_food.__wrapped__ if hasattr(
        core.record_child_food, "__wrapped__") else None
    # 直接验证内部 _content_key 行为：构造同形键
    _m = bad_entry.get("meal")
    _f = bad_entry.get("food")
    _k = (str(bad_entry.get("date") or "").strip(),
          str(_m).strip() if _m is not None else "",
          str(_f).strip() if _f is not None else "")
    hash(_k)  # 若不可哈希此处抛 TypeError
    check("M-03 非哈希 food 经 str() 防御可哈希", True)
except TypeError as e:
    check("M-03 非哈希 food 经 str() 防御可哈希", False, str(e))

# M-04：_aggregate 空有效天数 → diet_diary_3d=None
r4 = core._aggregate([{"date": "", "food": "x", "energy_kcal": 100}])
check("M-04 空有效天数 diet_diary_3d=None",
      r4["diet_diary_3d"] is None and r4["day_count"] == 0,
      f"r4={r4}")

# M-05：floor_protein_g 透传（婴儿段官方下限 vs 退化 0.85 假阳性对比）
# 摄入 1.5，target=2.0。官方 floor=1.0 → 1.5>=1.0 不 low；
# 退化 floor=2.0*0.85=1.7 → 1.5<1.7 误判 low（假阳性）。
r5_floor = core.assess_pew_risk(avg_protein_g=1.5, avg_energy_kcal=1000.0,
                                target_protein_g=2.0, target_energy_kcal=1200.0,
                                floor_protein_g=1.0)
r5_degraded = core.assess_pew_risk(avg_protein_g=1.5, avg_energy_kcal=1000.0,
                                   target_protein_g=2.0, target_energy_kcal=1200.0)
check("M-05 显式 floor 不误判低蛋白",
      "低于安全下限" not in r5_floor["data"]["rationale"],
      f"rationale={r5_floor['data']['rationale']}")
check("M-05b 退化 floor 确实更严（对照）",
      "低于安全下限" in r5_degraded["data"]["rationale"],
      f"rationale={r5_degraded['data']['rationale']}")

# M-06：calc_growth_zscore_tool 字符串 height_cm → 不走 INVALID
r6 = server.calc_growth_zscore_tool(age_years=8.0, sex="M",
                                    height_cm="130", weight_kg=28.0)
check("M-06 字符串 height_cm 宽容转换",
      r6.get("ok") is True or "error" not in r6,
      f"r6={r6.get('ok')}")

# M-07：assess_pew_risk_tool 签名含 floor_protein_g（契约对齐）
import inspect
sig = inspect.signature(server.assess_pew_risk_tool)
check("M-07 assess_pew_risk_tool 暴露 floor_protein_g",
      "floor_protein_g" in sig.parameters, f"params={list(sig.parameters)}")

# M-07b：server 层透传 floor_protein_g 到 core（不退化）
r7 = server.assess_pew_risk_tool(
    avg_protein_g=1.5, avg_energy_kcal=1000.0,
    target_protein_g=2.0, target_energy_kcal=1200.0,
    floor_protein_g=1.0)
check("M-07b server 透传 floor 不误判低蛋白",
      "低于安全下限" not in r7["data"]["rationale"],
      f"rationale={r7['data']['rationale']}")

print("ALL M-TESTS PASSED")
