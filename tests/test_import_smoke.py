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


if __name__ == "__main__":
    test_server_imports()
    test_calc_prnt_targets()
    test_calc_prnt_targets_validation()
    test_corrupt_store_fail_closed()
    test_diary_target_normalize()
    print("P2 SMOKE OK")
