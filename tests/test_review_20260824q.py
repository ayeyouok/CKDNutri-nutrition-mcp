# 十三审（2026-08-24）回归测试：server.py DAG 加固（R1/R2/R3/R4）防回归
# 约定：顶部 setdefault 注入测试 env+caller；零 pytest 依赖；不跨包 import。
import os
import sys

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_NUTRITION_ASSESSMENT_DATA_DIR",
                      "C:/tmp/a207-ci-check-q")
os.environ.setdefault("A207_CHILD_PATIENT_ID", "P0007")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from CKDNutri_nutrition_mcp import server  # noqa: E402


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(f"{name} FAILED :: {detail}")


# R2-a：_stage_int 必须排除 bool（bool(True)=1 / bool(False)=0 旧实现会酿成非法分期）
def test_r2a_stage_int_bool():
    check("R2-a _stage_int(True)->default", server._stage_int(True) == 1, repr(server._stage_int(True)))
    check("R2-a _stage_int(False)->default", server._stage_int(False) == 1, repr(server._stage_int(False)))
    check("R2-a _stage_int(3) 正常", server._stage_int(3) == 3)
    check("R2-a _stage_int('G3a')->3", server._stage_int("G3a") == 3)
    check("R2-a _stage_int(9) 越界->default", server._stage_int(9) == 1)
    check("R2-a _stage_int(None)->default", server._stage_int(None) == 1)


# R2-b：_to_bool 严格解析，防御 'false'/'0' 字符串陷阱
def test_r2b_to_bool():
    check("R2-b _to_bool('false')->False", server._to_bool("false") is False)
    check("R2-b _to_bool('0')->False", server._to_bool("0") is False)
    check("R2-b _to_bool('False')->False", server._to_bool("False") is False)
    check("R2-b _to_bool(True)->True", server._to_bool(True) is True)
    check("R2-b _to_bool('true')->True", server._to_bool("true") is True)
    check("R2-b _to_bool('1')->True", server._to_bool("1") is True)
    check("R2-b _to_bool(None)->default False", server._to_bool(None) is False)
    check("R2-b _to_bool(0)->False", server._to_bool(0) is False)
    check("R2-b _to_bool(1)->True", server._to_bool(1) is True)


# R1：腹透患儿漏传 pd_dwell_hours 不应静默算 0 扣减（必须提示未扣减）
def test_r1_pd_missing_dwell_hours():
    # 仅传 volume + conc，漏传 dwell_hours，必须落在"参数不全"分支而非 0kcal 扣减
    # 通过 DAG 输出 notes 不含"已从膳食能量目标扣减"且含"未扣减"来验证
    res = server.comprehensive_nutrition_assessment_tool(
        age_years=5.0, sex="M", weight_kg=18.0, height_cm=105.0,
        ckd_stage=5, dialysis_mode="peritoneal",
        pd_dialysate_volume_ml=1000, pd_glucose_conc_pct=1.5,
        # 故意不传 pd_dwell_hours
    )
    check("R1 DAG ok", res.get("ok") is True, repr(res))
    notes = " ".join(res["data"].get("notes", []))
    check("R1 未出现'已扣减'假象", "已从膳食能量目标扣减" not in notes, notes)
    check("R1 提示参数不全未扣减", "未扣减" in notes, notes)


# R1-对照：完整腹透参数（含 dwell_hours）应正常扣减
def test_r1_pd_complete():
    res = server.comprehensive_nutrition_assessment_tool(
        age_years=5.0, sex="M", weight_kg=18.0, height_cm=105.0,
        ckd_stage=5, dialysis_mode="peritoneal",
        pd_dialysate_volume_ml=1000, pd_glucose_conc_pct=1.5,
        pd_dwell_hours=14.0,
    )
    check("R1-complete DAG ok", res.get("ok") is True, repr(res))
    notes = " ".join(res["data"].get("notes", []))
    check("R1-complete 出现'已扣减'", "已从膳食能量目标扣减" in notes, notes)


# R3：孤立传入 avg_protein_g 但同时给 include_intake+patient_id 时，应优先查库补全
# 用无日记患儿验证：即便孤立传参，也不应误判"仅部分数据跳过"，而是尝试查库后报"暂无日记"
def test_r3_intake_branch_priority():
    res = server.comprehensive_nutrition_assessment_tool(
        age_years=5.0, sex="M", weight_kg=18.0, height_cm=105.0,
        ckd_stage=3, dialysis_mode="none",
        avg_protein_g=20.0,  # 孤立传入（缺 avg_energy_kcal），但同时给了查库参数
        include_intake=True, patient_id="P0099",  # 不存在/无日记
    )
    check("R3 DAG ok", res.get("ok") is True, repr(res))
    notes = " ".join(res["data"].get("notes", []))
    # 不应出现"仅提供了部分摄入数据"的孤立告警（那是末位分支），而应尝试查库
    check("R3 未命中孤立告警短路", "仅提供了部分摄入数据" not in notes, notes)
    # 查库后该患者无日记 → 提示暂无日记（证明走了查库分支而非短路）
    check("R3 走查库分支报无日记", "暂无饮食日记" in notes or "饮食日记未成功" in notes, notes)


# R4：ok:True 响应不内联底层 detail 原文（中性提示）
def test_r4_no_detail_leak():
    # 非法 patient_id 场景（非法 id 被 validate_patient_id 拒），DAG 应中性提示，不拼 detail
    res = server.comprehensive_nutrition_assessment_tool(
        age_years=5.0, sex="M", weight_kg=18.0, height_cm=105.0,
        ckd_stage=3, dialysis_mode="none",
        include_intake=True, patient_id="invalid_id",
    )
    check("R4 DAG ok（摄入段失败不崩）", res.get("ok") is True, repr(res))
    notes = " ".join(res["data"].get("notes", []))
    # 不应出现底层 detail 原文（如 "必须为字符串" 之类业务文案）
    check("R4 不内联 detail 原文", "必须为字符串" not in notes and "匹配" not in notes, notes)
    check("R4 中性提示", "饮食日记未成功" in notes or "暂无饮食日记" in notes, notes)


if __name__ == "__main__":
    test_r2a_stage_int_bool()
    test_r2b_to_bool()
    test_r1_pd_missing_dwell_hours()
    test_r1_pd_complete()
    test_r3_intake_branch_priority()
    test_r4_no_detail_leak()
    print("\n[ALL PASS] 十三审回归 R1/R2/R3/R4 OK")
