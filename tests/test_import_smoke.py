"""P2 冒烟自测：导入 server 不报错 + 代表性工具可调用。

运行：pytest tests/test_import_smoke.py  (或 python tests/test_import_smoke.py)
依赖：a207-policy 已随 pip install -e . 安装。
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

os.environ.setdefault("A207_CALLER", "doctor_assistant")
# v0.5（2026-08-13）：存储默认 Tablestore（生产）；测试显式用 json 后端（LocalJson，
# 与旧行为一致），Tablestore 后端行为由 test_repository_backend 覆盖。
os.environ.setdefault("A207_STORAGE_BACKEND", "json")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_server_imports():
    """导入 server 不可抛错（回归：合并时 calc_pnpr/calc_pd_glucose_absorption 等导入错位）。"""
    mod = importlib.import_module("CKDNutri_nutrition_mcp.server")
    assert mod.mcp is not None


def test_calc_prnt_targets():
    from CKDNutri_nutrition_mcp import core

    r = core.calc_prnt_targets(age_years=6, sex="F", weight_kg=20, height_cm=130, ckd_stage=1)
    assert "ok" in r and "data" in r


def test_calc_prnt_targets_validation():
    """S2（2026-08-12 五包审查）回归：fail-closed 校验 + lacto_ovo 同义别名。"""
    from CKDNutri_nutrition_mcp import core

    def _raises(fn, label):
        try:
            fn()
        except ValueError:
            return
        raise AssertionError(f"期望 {label} 抛 ValueError")

    # weight_kg <= 0 拒绝（此前静默产出 0/负能量目标）
    _raises(lambda: core.calc_prnt_targets(age_years=6, sex="F", weight_kg=0, height_cm=130),
            "weight_kg=0")
    # 负年龄拒绝（与 calc_growth_zscore 同口径）
    _raises(lambda: core.calc_prnt_targets(age_years=-1, sex="F", weight_kg=20, height_cm=130),
            "age_years=-1")
    # growth_status 非法值拒绝（此前静默取 SDI 中点）
    _raises(lambda: core.calc_prnt_targets(age_years=6, sex="F", weight_kg=20, height_cm=130,
                                           growth_status="invalid"), "growth_status=invalid")
    # vegetarian_mode 非法值拒绝（此前静默降级 mixed，蛋白需求低估 20%）
    _raises(lambda: core.calc_prnt_targets(age_years=6, sex="F", weight_kg=20, height_cm=130,
                                           vegetarian_mode="ovo-lacto"), "vegetarian_mode=ovo-lacto")
    # lacto_ovo 与 ovo_lacto 同义：按文档传 lacto_ovo 应生效蛋奶素倍数 1.2（此前静默降级 1.0）
    r = core.calc_prnt_targets(age_years=6, sex="F", weight_kg=20, height_cm=130,
                               vegetarian_mode="lacto_ovo")
    assert r["ok"] is True and r["data"]["protein"]["vegetarian_multiplier"] == 1.2


def test_corrupt_store_fail_closed():
    """B1（2026-08-12 五包审查）回归：损坏/类型错误的状态库必须抛 RuntimeError，
    不得静默返回空库被 RMW 覆盖清空（对齐 care BUG-65/67）。"""
    import tempfile

    from CKDNutri_nutrition_mcp import core

    tmp = tempfile.mkdtemp(prefix="a207-nutri-corrupt-")
    # 日记库：损坏 JSON
    (Path(tmp) / core.DIARY_STORE_FILENAME).write_text("{broken json", encoding="utf-8")
    # PEW 库：合法 JSON 但非 dict（[1,2]）
    (Path(tmp) / core.PEW_STORE_FILENAME).write_text("[1,2]", encoding="utf-8")
    os.environ["A207_NUTRITION_ASSESSMENT_DATA_DIR"] = tmp
    try:
        try:
            core._load_store()
        except RuntimeError:
            pass
        else:
            raise AssertionError("损坏日记库应抛 RuntimeError（B1）")
        try:
            core._load_pew_store()
        except RuntimeError:
            pass
        else:
            raise AssertionError("非 dict PEW 库应抛 RuntimeError（B1）")
    finally:
        os.environ.pop("A207_NUTRITION_ASSESSMENT_DATA_DIR", None)


def test_diary_target_normalize():
    """五审（2026-08-13）回归：sum_diet_intake 传 PRNT 完整信封时目标对照不再静默为空。"""
    from CKDNutri_nutrition_mcp import core
    from CKDNutri_nutrition_mcp.diary import sum_diet_intake

    prnt = core.calc_prnt_targets(age_years=6, sex="F", weight_kg=20, height_cm=130)
    r = sum_diet_intake(
        [{"food": "米饭", "grams": 150}, {"food": "鸡蛋", "grams": 50}],
        target=prnt)
    assert r.get("ok") is True, r
    ach = r["data"].get("achievement")
    assert ach and ach["items"], "PRNT 信封目标对照不应为空（此前嵌套结构导致静默丢失）"
    fields = {i["field"] for i in ach["items"]}
    assert {"energy_kcal", "protein_g"} <= fields, fields
    # 扁平简表兼容不受影响
    flat = sum_diet_intake(
        [{"food": "米饭", "grams": 100}],
        target={"energy_kcal": 1200, "protein_g": 40})
    assert flat["data"]["achievement"]["items"], "扁平简表目标对照应保留"


def test_s4_unauthorized_nan_unit():
    """S4（2026-08-13）补全：越权 / NaN / 单位一致性。"""
    from math import isclose

    from a207_policy import PermissionDenied

    from CKDNutri_nutrition_mcp import core
    from CKDNutri_nutrition_mcp.targets import calc_pd_glucose_absorption

    # ① 越权：家长对计算面工具（calc_prnt_targets）工具级 ACL 拒绝
    os.environ["A207_CALLER"] = "parent_assistant"
    try:
        try:
            core.calc_prnt_targets(age_years=6, sex="F", weight_kg=20, height_cm=130)
        except PermissionDenied:
            pass
        else:
            raise AssertionError("家长调用 calc_prnt_targets 应抛 PermissionDenied")
    finally:
        os.environ["A207_CALLER"] = "doctor_assistant"

    # ② NaN：weight/age=NaN 此前静默产出 NaN 目标（S4 修复有限性校验），现拒绝
    try:
        core.calc_prnt_targets(age_years=6, sex="F", weight_kg=float("nan"), height_cm=130)
    except ValueError:
        pass
    else:
        raise AssertionError("weight_kg=NaN 应抛 ValueError")
    try:
        core.calc_prnt_targets(age_years=float("nan"), sex="F", weight_kg=20, height_cm=130)
    except ValueError:
        pass
    else:
        raise AssertionError("age_years=NaN 应抛 ValueError")

    # ③ 单位一致性：腹透葡萄糖吸收能量 = 吸收克数 × 3.4 kcal/g（方法注释即单位契约）
    r = calc_pd_glucose_absorption(dialysate_glucose_g=25, dwell_hours=8)
    assert r.get("ok") is True, r
    d = r["data"]
    assert d["absorbed_energy_kcal_per_day"] > 0
    assert isclose(d["absorbed_energy_kcal_per_day"],
                   d["absorbed_glucose_g_per_day"] * 3.4, rel_tol=1e-9)
    assert "3.4 kcal/g" in d["method"]


def test_repository_backend():
    """v0.5（2026-08-13）回归：DAO 后端语义——缺省 tablestore（缺参 fail-fast）；
    显式 json 用 LocalJson；LocalJson 日记/PEW 读写 + 损坏 fail-closed。"""
    import tempfile

    from CKDNutri_nutrition_mcp import nutrition_repository as repo_mod

    saved = os.environ.pop("A207_STORAGE_BACKEND", None)
    try:
        # 缺省后端 = tablestore（生产），缺 OTS 参数必须 fail-fast（不静默回退）
        try:
            repo_mod.get_repository()
        except RuntimeError as exc:
            assert "A207_OTS_" in str(exc), exc
        else:
            raise AssertionError("缺省 tablestore 后端缺参应 fail-fast")
    finally:
        if saved is not None:
            os.environ["A207_STORAGE_BACKEND"] = saved

    # 显式 json → LocalJson（开发模式）
    os.environ["A207_STORAGE_BACKEND"] = "json"
    try:
        repo = repo_mod.get_repository()
        assert isinstance(repo, repo_mod.LocalJsonRepository), type(repo)
    finally:
        if saved is not None:
            os.environ["A207_STORAGE_BACKEND"] = saved

    # LocalJson 读写（日记 + PEW）+ 损坏 fail-closed
    tmp = tempfile.mkdtemp(prefix="a207-nutri-repo-")
    os.environ["A207_NUTRITION_ASSESSMENT_DATA_DIR"] = tmp
    try:
        repo = repo_mod.get_repository()
        assert repo.load_diary() == {"entries": []}
        repo.save_diary({"entries": [{"patient_id": "P001", "date": "2026-08-13"}]})
        assert repo.load_diary()["entries"][0]["patient_id"] == "P001"
        assert repo.load_pew() == {}
        repo.save_pew({"P001": [{"date": "2026-08-13", "score": 1.0}]})
        assert repo.load_pew()["P001"][0]["score"] == 1.0
        (Path(tmp) / repo_mod.DIARY_STORE_FILENAME).write_text("{broken", encoding="utf-8")
        try:
            repo.load_diary()
        except RuntimeError:
            pass
        else:
            raise AssertionError("损坏日记库应抛 RuntimeError")
    finally:
        os.environ.pop("A207_NUTRITION_ASSESSMENT_DATA_DIR", None)


if __name__ == "__main__":
    test_server_imports()
    test_calc_prnt_targets()
    test_calc_prnt_targets_validation()
    test_corrupt_store_fail_closed()
    test_diary_target_normalize()
    test_s4_unauthorized_nan_unit()
    test_repository_backend()
    print("P2 SMOKE OK")
