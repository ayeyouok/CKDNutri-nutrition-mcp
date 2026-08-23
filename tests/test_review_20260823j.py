"""夜审 2026-08-23 第二批（j）裁定固化测试。

覆盖：
- P2-2：_z_from_bands 相邻界值相等（除零防御）不抛 ZeroDivisionError
- P3-3：comprehensive_nutrition_assessment_tool 摄入评估失败时熔断 PEW（不产出虚假 high）
- P1-1 驳回复核：record_child_food 同批次同名收敛（后写覆盖，不重复落库）
- P4-4 驳回复核：generate_meal_plan 底层已 enforce（非越权面，此处只验证不破坏正常临床调用）

纪律：零 pytest 依赖，sys.path 注入 src + a207-policy/src，A207_ENV=test。
"""
import sys
import os
import json
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "a207-policy", "src"))

from CKDNutri_nutrition_mcp import core
from CKDNutri_nutrition_mcp import server
from CKDNutri_nutrition_mcp import mealplan

FAILURES = []


def _run():
    # --- P2-2：_z_from_bands 除零防御 ---
    # 构造相邻界值相等的退化 bands（如热更/微调端点异常）
    degenerate = (10.0, 10.0, 20.0, 30.0, 40.0, 50.0, 50.0)  # n3==n2, p2==p3
    try:
        for x in (5.0, 10.0, 15.0, 25.0, 35.0, 45.0, 55.0):
            z = core._z_from_bands(x, degenerate)
            if not isinstance(z, float) or z != z:  # NaN 检查
                FAILURES.append(f"P2-2 _z_from_bands({x}) 返回非有限值: {z!r}")
    except ZeroDivisionError:
        FAILURES.append("P2-2 _z_from_bands 退化 bands 抛 ZeroDivisionError（除零防御失效）")
    except Exception as e:  # noqa
        FAILURES.append(f"P2-2 _z_from_bands 异常: {e!r}")

    # 正常 bands 仍产出合理 Z（回归：单调且界值精确对应整数）
    normal = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    if abs(core._z_from_bands(3.0, normal) - 0.0) > 1e-9:
        FAILURES.append("P2-2 正常 bands 中位数 Z 不为 0")
    if abs(core._z_from_bands(4.0, normal) - 1.0) > 1e-9:
        FAILURES.append("P2-2 正常 bands +1SD 不为 1")

    # --- P3-3：DAG 摄入失败熔断 PEW ---
    # 构造一个让 assess_intake_vs_target 返回 ok=False 的 diet（非法字段触发内部校验失败）
    # 直接 monkeypatch assess_intake_vs_target 以稳定复现 intake 失败，避免依赖内部细节
    real_assess = server.assess_intake_vs_target
    server.assess_intake_vs_target = lambda *a, **k: {"ok": False, "error": "INVALID_INPUT",
                                                       "detail": "复现摄入评估失败"}
    try:
        res = server.comprehensive_nutrition_assessment_tool(
            age_years=5.0, sex="M", weight_kg=18.0, height_cm=110.0,
            ckd_stage=3, avg_protein_g=30.0, avg_energy_kcal=1200.0,
        )
        if not res.get("ok"):
            FAILURES.append(f"P3-3 DAG 整体返回失败（不应崩）: {res}")
            server.assess_intake_vs_target = real_assess
            return
        d = res["data"]
        ia = d.get("intake_assessment")
        if ia is None or ia.get("ok") is not False:
            FAILURES.append(f"P3-3 intake_assessment 未记录失败: {ia}")
        pew = d.get("pew")
        # 关键：PEW 必须为失败态，绝不能是 ok=True 的虚假 high
        if pew is None or pew.get("ok") is not False:
            FAILURES.append(f"P3-3 摄入失败却产出 PEW（可能为虚假高危）: {pew}")
        else:
            if pew.get("error") != "INTAKE_ASSESSMENT_FAILED":
                FAILURES.append(f"P3-3 PEW 熔断错误码不符: {pew.get('error')}")
    except Exception as e:  # noqa
        FAILURES.append(f"P3-3 DAG 抛异常（应受控返回）: {e!r}")
    finally:
        server.assess_intake_vs_target = real_assess

    # 对照：intake 成功时 PEW 仍正常产出
    res_ok = server.comprehensive_nutrition_assessment_tool(
        age_years=5.0, sex="M", weight_kg=18.0, height_cm=110.0,
        ckd_stage=3, avg_protein_g=30.0, avg_energy_kcal=1200.0,
    )
    if res_ok.get("ok"):
        pew_ok = res_ok["data"].get("pew")
        if not (isinstance(pew_ok, dict) and ("score" in pew_ok or "level" in pew_ok or pew_ok.get("ok") is True)):
            FAILURES.append(f"P3-3 对照：intake 成功时 PEW 未正常产出: {pew_ok}")

    # --- P1-1 驳回复核：record_child_food 同批次同名收敛 ---
    # 同 (date,meal,food) 两条 → 后写覆盖（entry_count 不重复）
    tmp = tempfile.mkdtemp()
    os.environ["A207_NUTRITION_ASSESSMENT_DATA_DIR"] = tmp  # 正确 env 名（nutrition_repository._DATA_DIR_ENV）
    try:
        core._CHILD_STORE_DIR = tmp  # 隔离持久化（若模块用此变量）
    except Exception:  # noqa
        pass
    # 直接调底层（绕过 MCP 入口的 get_caller 需 env caller=child_assistant）
    os.environ["A207_CALLER"] = "child_assistant"
    os.environ["A207_CHILD_PATIENT_ID"] = "P0007"  # child 绑定患儿铁律（P0-1），测试注入
    os.environ["A207_STORAGE_BACKEND"] = "json"   # 走 LocalJson 后端（测试隔离）
    os.environ["A207_ACCEPT_DEV_STORAGE"] = "1"   # 允许 json 后端（fail-closed 护栏）
    # 清 repo 缓存 + 用新 DATA_DIR 重建，避免批量跑时被更早测试的缓存实例污染
    from CKDNutri_nutrition_mcp import nutrition_repository
    nutrition_repository._REPO_CACHE.clear()
    try:
        r1 = core.record_child_food(
            "P0007",
            entries=[
                {"date": "2026-08-20", "meal": "早餐", "food": "鸡蛋", "amount": "1个"},
                {"date": "2026-08-20", "meal": "早餐", "food": "鸡蛋", "amount": "20个"},  # 同键后写
            ],
            write_mode=True,
        )
        if not r1.get("ok"):
            FAILURES.append(f"P1-1 record_child_food 返回失败: {r1}")
        else:
            ec = r1["data"].get("entry_count")
            # 同 (date,meal,food) 收敛为 1 条（后写"20个"覆盖"1个"），不重复落库
            if ec != 1:
                FAILURES.append(f"P1-1 同批次同名未收敛（期望1条，得{ec}）: 与 D-01/D-02 幂等契约相悖")
    except Exception as e:  # noqa
        FAILURES.append(f"P1-1 record_child_food 异常: {e!r}")
    finally:
        os.environ.pop("A207_NUTRITION_ASSESSMENT_DATA_DIR", None)
        os.environ.pop("A207_CALLER", None)
        os.environ.pop("A207_CHILD_PATIENT_ID", None)
        os.environ.pop("A207_STORAGE_BACKEND", None)
        os.environ.pop("A207_ACCEPT_DEV_STORAGE", None)

    # --- P4-4 驳回复核：generate_meal_plan 底层已 enforce（正常临床调用不崩） ---
    os.environ["A207_CALLER"] = "doctor_assistant"
    try:
        plan = mealplan.generate_meal_plan(
            float(1200), float(30), 0.0, 0.0, 0.0, days=1, vegetarian=False, exclude_foods=None,
        )
        if not isinstance(plan, dict):
            FAILURES.append("P4-4 generate_meal_plan 返回异常类型")
    except Exception as e:  # noqa
        FAILURES.append(f"P4-4 正常临床调用 generate_meal_plan 异常: {e!r}")
    finally:
        os.environ.pop("A207_CALLER", None)


if __name__ == "__main__":
    _run()
    if FAILURES:
        print("FAIL")
        for f in FAILURES:
            print(" -", f)
        sys.exit(1)
    print("PASS test_review_20260823j")
    sys.exit(0)
