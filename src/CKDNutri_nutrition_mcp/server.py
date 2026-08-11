"""P2 营养计算域 MCP 服务：PRNT目标 + 食物DB + 食谱 + 生长评估 + DAG。

合并自 M3 (nutrition-assessment) + M5 (nutrition-calc) + M7 (meal-plan)。
v2.3 新增 DAG: comprehensive_nutrition_assessment（Z→PRNT→PEW 一键）。
Tool Masking: diet/food 组全角色可见；recipe/clinical 组仅临床助手。
"""
from __future__ import annotations

from typing import Any, Optional

from fastmcp import FastMCP

from a207_policy import CallerError

from .core import (
    assess_intake_vs_target,
    assess_pew_risk,
    calc_growth_zscore,
    calc_prnt_targets,
    get_food_diary_summary,
    get_pew_history,
    record_pew_risk,
    upsert_food_diary,
)
from .diary import sum_diet_intake
from .foods import (
    calc_pnpr,
    convert_to_household_measure,
    lookup_food_nutrients,
    substitute_food,
)
from .mealplan import generate_meal_plan, get_meal_plan_nutrients
from .pharma import check_drug_nutrient_interaction
from .targets import calc_pd_glucose_absorption

mcp = FastMCP("CKDNutri-nutrition-mcp")


def _invalid(exc):
    if isinstance(exc, CallerError):
        raise
    return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}


def main():
    mcp.run()


# ---- diet 组（饮食记录，全角色可见） ----

@mcp.tool
def upsert_food_diary_tool(patient_id: str, entries: list, write_mode: bool = True) -> dict[str, Any]:
    """写入每日饮食日记。CKD 家庭助手可写。"""
    try:
        return upsert_food_diary(patient_id, entries, write_mode)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_food_diary_summary_tool(patient_id: str) -> dict[str, Any]:
    """查最近饮食日记脱敏摘要。"""
    try:
        return get_food_diary_summary(patient_id)
    except Exception as exc:
        return _invalid(exc)


# ---- food 组（食物查询，全角色可见） ----

@mcp.tool
def lookup_food_nutrients_tool(food_name: str) -> dict[str, Any]:
    """查食物营养成分。"""
    try:
        return lookup_food_nutrients(food_name)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def substitute_food_tool(food: str, constraint: str = "等能量", top_n: int = 4) -> dict[str, Any]:
    """按约束推荐替换食物。"""
    try:
        return substitute_food(food, constraint, top_n)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def convert_to_household_measure_tool(grams: float, food_name: str) -> dict[str, Any]:
    """克重 ↔ 家用单位换算。"""
    try:
        return convert_to_household_measure(grams, food_name)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def sum_diet_intake_tool(patient_id: str, days: int = 3) -> dict[str, Any]:
    """汇总近 N 日摄入。"""
    try:
        return sum_diet_intake(patient_id, days)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def calc_pnpr_tool(protein_intake_g: float, bun_mg_dl: float, weight_kg: float) -> dict[str, Any]:
    """估计蛋白氮呈现率。"""
    try:
        return calc_pnpr(protein_intake_g, bun_mg_dl, weight_kg)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def calc_pd_glucose_absorption_tool(dialysate_volume_ml: float, glucose_conc_pct: float) -> dict[str, Any]:
    """估计腹膜透析葡萄糖吸收量。"""
    try:
        return calc_pd_glucose_absorption(dialysate_volume_ml, glucose_conc_pct)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def check_drug_nutrient_interaction_tool(drug_name: str, food_name: str) -> dict[str, Any]:
    """检查药物-食物相互作用。"""
    try:
        return check_drug_nutrient_interaction(drug_name, food_name)
    except Exception as exc:
        return _invalid(exc)


# ---- recipe 组（食谱，仅临床助手） ----

@mcp.tool
def generate_meal_plan_tool(
    patient_id: str,
    age_years: float,
    sex: str,
    weight_kg: float,
    ckd_stage: str,
    dialysis: str = "none",
    allergies: list | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """生成个体化 5 餐次食谱。仅 CKD 临床助手。"""
    try:
        return generate_meal_plan(patient_id, age_years, sex, weight_kg, ckd_stage, dialysis, allergies, days)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_meal_plan_nutrients_tool(patient_id: str) -> dict[str, Any]:
    """回算已生成食谱的养分达成率。"""
    try:
        return get_meal_plan_nutrients(patient_id)
    except Exception as exc:
        return _invalid(exc)


# ---- clinical 组（临床评估，仅临床助手） ----

@mcp.tool
def calc_prnt_targets_tool(
    age_years: float,
    sex: str,
    weight_kg: float,
    height_cm: float,
    ckd_stage: str,
    dialysis: str = "none",
    is_edema: bool = False,
) -> dict[str, Any]:
    """计算 PRNT 能量与蛋白目标。仅 CKD 临床助手。"""
    try:
        return calc_prnt_targets(age_years, sex, weight_kg, height_cm, ckd_stage, dialysis, is_edema)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def calc_growth_zscore_tool(age_years: float, sex: str, measurement: float, metric: str = "height") -> dict[str, Any]:
    """计算身高/体重/BMI Z 评分。"""
    try:
        return calc_growth_zscore(age_years, sex, measurement, metric)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def assess_pew_risk_tool(
    age_years: float, sex: str, height_cm: float, weight_kg: float,
    ckd_stage: str, serum_albumin_g_l: float, protein_intake_g: float, protein_target_g: float,
) -> dict[str, Any]:
    """综合评定 PEW 风险等级。"""
    try:
        return assess_pew_risk(age_years, sex, height_cm, weight_kg, ckd_stage, serum_albumin_g_l, protein_intake_g, protein_target_g)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def record_pew_risk_tool(patient_id: str, pew_result: dict) -> dict[str, Any]:
    """落库 PEW 评估结果。"""
    try:
        return record_pew_risk(patient_id, pew_result)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_pew_history_tool(patient_id: str) -> dict[str, Any]:
    """查 PEW 历史序列。"""
    try:
        return get_pew_history(patient_id)
    except Exception as exc:
        return _invalid(exc)


# ---- DAG: 一键营养评估 (v2.3) ----

@mcp.tool
def comprehensive_nutrition_assessment_tool(
    age_years: float, sex: str, weight_kg: float, height_cm: float,
    ckd_stage: str, dialysis: str = "none", is_edema: bool = False,
    serum_albumin_g_l: Optional[float] = None,
    include_intake: bool = False, patient_id: str = "",
) -> dict[str, Any]:
    """一键营养评估：Z评分→PRNT目标→PEW评定 (可选摄入评估)。

    内部串联 calc_growth_zscore → calc_prnt_targets → assess_pew_risk。
    原 3-4 次 LLM 调用 → 1 次。仅 CKD 临床助手。
    """
    try:
        z_h = calc_growth_zscore(age_years, sex, height_cm, "height")
        z_w = calc_growth_zscore(age_years, sex, weight_kg, "weight")
        prnt = calc_prnt_targets(age_years, sex, weight_kg, height_cm, ckd_stage, dialysis, is_edema)
        pew = None
        if serum_albumin_g_l and prnt.get("protein_g"):
            pew = assess_pew_risk(age_years, sex, height_cm, weight_kg, ckd_stage, serum_albumin_g_l, 0, prnt["protein_g"])
        result = {"ok": True, "z_scores": {"height": z_h, "weight": z_w}, "prnt_targets": prnt, "pew_result": pew}
        if include_intake and patient_id:
            intake = sum_diet_intake(patient_id, 3)
            target = assess_intake_vs_target(patient_id, intake, prnt)
            result["intake_assessment"] = target
        return result
    except Exception as exc:
        return _invalid(exc)


if __name__ == "__main__":
    main()
