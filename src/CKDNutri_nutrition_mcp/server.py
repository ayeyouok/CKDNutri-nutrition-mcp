"""P2 营养计算域 MCP 服务：PRNT目标 + 食物DB + 食谱 + 生长评估 + DAG。

合并自 M3 (nutrition-assessment) + M5 (nutrition-calc) + M7 (meal-plan)。
v2.3 新增 DAG: comprehensive_nutrition_assessment（Z→PRNT→PEW 一键）。
Tool Masking: diet/food 组全角色可见；recipe/clinical 组仅临床助手。

v0.3.2 修复：工具包装层签名与 core 全面对齐（此前 11 个工具因位置参数错位而崩溃
或静默算错，详见 docs 复盘）。本层只做「入参归一化 + 透传」，不再自造参数语义。
"""
from __future__ import annotations

from typing import Any, Optional

from fastmcp import FastMCP

from a207_policy import CallerError, enforce_nutrition_tool, get_caller

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


def _stage_int(value: Any, default: int = 1) -> int:
    """把 '3' / 'G3a' / '3a' / 3 一律归一为 int 分期，供 core 的数值比较使用。"""
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not value:
        return default
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits[0]) if digits else default


def _unwrap_plan(plan: Any) -> Any:
    """食谱既可直接传 core 原始 plan，也可传 generate_meal_plan_tool 的 {ok,data} 信封。"""
    if isinstance(plan, dict) and "days" not in plan and isinstance(plan.get("data"), dict):
        return plan["data"]
    return plan


def main():
    mcp.run()


# ---- diet 组（饮食记录，全角色可见） ----

@mcp.tool
def upsert_food_diary_tool(patient_id: str, entries: list, write_mode: bool = True,
                           guardian_token: str = "") -> dict[str, Any]:
    """写入每日饮食日记。家长/医生可写（MX-3），家长须携带 guardian_token 完成患儿绑定。

    entries 每项：{date, meal, food, energy_kcal, protein_g, potassium_mg, phosphorus_mg, sodium_mg}
    """
    try:
        return upsert_food_diary(patient_id, entries, write_mode, guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_food_diary_summary_tool(patient_id: str, guardian_token: str = "") -> dict[str, Any]:
    """查最近饮食日记脱敏摘要（含最近 3 日均值 diet_diary_3d）。家长须携带 guardian_token。"""
    try:
        return get_food_diary_summary(patient_id, guardian_token)
    except Exception as exc:
        return _invalid(exc)


# ---- food 组（食物查询，全角色可见） ----

@mcp.tool
def lookup_food_nutrients_tool(food_name: str, portion: Optional[str] = None) -> dict[str, Any]:
    """查食物营养成分。portion 可传家庭量具（如“一碗”“半个”），缺省按 100 g。"""
    try:
        return lookup_food_nutrients(food_name, portion)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def substitute_food_tool(food: str, constraint: str = "等能量", top_n: int = 4) -> dict[str, Any]:
    """按约束推荐替换食物（等能量 / 低钾 / 低磷 / 低钠 等）。"""
    try:
        return substitute_food(food, constraint, top_n)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def convert_to_household_measure_tool(food_name: str, grams: float) -> dict[str, Any]:
    """克重 → 家庭量具换算，并同时给出该克重的营养素。"""
    try:
        return convert_to_household_measure(food_name, float(grams))
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def sum_diet_intake_tool(diary: list, target: Optional[dict] = None) -> dict[str, Any]:
    """汇总多日饮食日记并对照目标给出达成率。

    diary 每项：{"food": 名称, "grams": 克重 或 "portion": 家庭量具,
                 "date": 日期(可选), "meal": 餐次(可选), "cooking": 烹调方式(可选)}
    target 可传 calc_prnt_targets 的结果或 {energy_kcal, protein_g, ...} 简表。
    按 patient_id 查库汇总请改用 get_food_diary_summary_tool。
    """
    try:
        return sum_diet_intake(diary, target)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def calc_pnpr_tool(
    food: Optional[str] = None,
    protein_g: Optional[float] = None,
    phosphorus_mg: Optional[float] = None,
    grams: float = 100.0,
) -> dict[str, Any]:
    """磷蛋白比 PNPR（mg 磷 / g 蛋白）：同等蛋白供给下的磷负荷指标。

    两种用法：传 food（查内置食物表，按 grams 缩放）；或直接传 protein_g + phosphorus_mg。
    """
    try:
        return calc_pnpr(food, protein_g, phosphorus_mg, grams)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def calc_pd_glucose_absorption_tool(
    dwell_hours: float,
    dialysate_glucose_g: Optional[float] = None,
    dialysate_volume_ml: Optional[float] = None,
    glucose_conc_pct: Optional[float] = None,
    exchanges_per_day: int = 1,
    transport_type: str = "average",
    weight_kg: Optional[float] = None,
) -> dict[str, Any]:
    """腹透葡萄糖倒灌：估算吸收克数与额外能量（须从膳食能量目标中扣减）。

    每袋糖量二选一：直接给 dialysate_glucose_g；或给 dialysate_volume_ml + glucose_conc_pct
    （如 1000 ml 的 1.5% 糖液 = 15 g），本层自动换算。
    transport_type: low / low_average / average / high_average / high。
    """
    try:
        glucose_g = dialysate_glucose_g
        if glucose_g is None:
            if dialysate_volume_ml is None or glucose_conc_pct is None:
                return {"ok": False, "error": "INVALID_INPUT",
                        "detail": "需提供 dialysate_glucose_g，或同时提供 dialysate_volume_ml 与 glucose_conc_pct"}
            glucose_g = float(dialysate_volume_ml) * float(glucose_conc_pct) / 100.0
        return calc_pd_glucose_absorption(
            float(glucose_g), float(dwell_hours),
            exchanges_per_day=int(exchanges_per_day),
            transport_type=transport_type,
            weight_kg=weight_kg,
        )
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def check_drug_nutrient_interaction_tool(drug_name: str, nutrient: Optional[str] = None) -> dict[str, Any]:
    """检查药物-营养素相互作用。nutrient 传营养素名（如“钙”“铁”“钾”“磷”），不传则返回该药全部条目。"""
    try:
        return check_drug_nutrient_interaction(drug_name, nutrient)
    except Exception as exc:
        return _invalid(exc)


# ---- recipe 组（食谱，仅临床助手） ----

@mcp.tool
def generate_meal_plan_tool(
    target_energy_kcal: float,
    target_protein_g: float,
    target_k_mg: float = 0.0,
    target_p_mg: float = 0.0,
    target_na_mg: float = 0.0,
    days: int = 7,
    vegetarian: bool = False,
    exclude_foods: Optional[list] = None,
) -> dict[str, Any]:
    """按 PRNT 目标生成多日食谱（3 餐 + 加餐）。仅 CKD 临床助手。

    能量/蛋白目标请先用 calc_prnt_targets_tool 计算，取
    data.energy.target_kcal_per_day 与 data.protein.target_g_per_day 传入。
    """
    try:
        plan = generate_meal_plan(
            float(target_energy_kcal), float(target_protein_g),
            float(target_k_mg), float(target_p_mg), float(target_na_mg),
            days=int(days), vegetarian=bool(vegetarian), exclude_foods=exclude_foods,
        )
        return {"ok": True, "data": plan}
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_meal_plan_nutrients_tool(plan: dict) -> dict[str, Any]:
    """回算已生成食谱的每日平均养分（校验用）。plan 传 generate_meal_plan_tool 的返回。"""
    try:
        return {"ok": True, "data": get_meal_plan_nutrients(_unwrap_plan(plan))}
    except Exception as exc:
        return _invalid(exc)


# ---- clinical 组（临床评估，仅临床助手） ----

@mcp.tool
def calc_prnt_targets_tool(
    age_years: float,
    sex: str,
    weight_kg: float,
    height_cm: float = 0.0,
    ckd_stage: Any = 1,
    dialysis_mode: str = "none",
    vegetarian_mode: str = "mixed",
    growth_status: str = "normal",
    is_edema: bool = False,
    pd_glucose_kcal_per_day: Optional[float] = None,
) -> dict[str, Any]:
    """计算 PRNT 2020 每日能量与蛋白目标。仅 CKD 临床助手。

    ckd_stage: CKD 分期(1-5D)。注意 PRNT 2020 的能量/蛋白数值目标与分期无关
    （仅由年龄×性别×体重驱动），ckd_stage 仅用于 stage=1 的沿用提示，不影响计算结果。
    dialysis_mode: none / peritoneal / hemodialysis（兼容 pd/腹透 等别名）；vegetarian_mode: mixed / lacto_ovo / vegan；
    growth_status: normal / failure / overweight（可取 calc_growth_zscore 的 growth_status_suggestion）；
    is_edema=True 时改用 BMI-P50 理想体重开处方（dry weight 原则）。
    """
    try:
        return calc_prnt_targets(
            age_years=float(age_years), sex=sex, weight_kg=float(weight_kg),
            height_cm=float(height_cm or 0.0), ckd_stage=_stage_int(ckd_stage),
            dialysis_mode=dialysis_mode, vegetarian_mode=vegetarian_mode,
            growth_status=growth_status, is_edema=bool(is_edema),
            pd_glucose_kcal_per_day=pd_glucose_kcal_per_day,
        )
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def calc_growth_zscore_tool(
    age_years: float,
    sex: str,
    height_cm: Optional[float] = None,
    weight_kg: Optional[float] = None,
    bmi: Optional[float] = None,
) -> dict[str, Any]:
    """计算儿童生长 Z 评分 HAZ / WAZ / BAZ（WS/T 423-2022 + WS/T 612-2018）。

    至少提供 height_cm / weight_kg / bmi 之一；同时给出 PRNT growth_status_suggestion。
    """
    try:
        return calc_growth_zscore(float(age_years), sex,
                                  height_cm=height_cm, weight_kg=weight_kg, bmi=bmi)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def assess_pew_risk_tool(
    avg_protein_g: float,
    avg_energy_kcal: float,
    target_protein_g: float,
    target_energy_kcal: float,
    albumin_g_L: Optional[float] = None,
) -> dict[str, Any]:
    """PEW（蛋白质-能量消耗）风险筛查：传入已算好的摄入均值与 PRNT 目标。

    摄入均值可取 get_food_diary_summary_tool 的 diet_diary_3d；
    目标可取 calc_prnt_targets_tool 的 target_g_per_day / target_kcal_per_day。
    """
    try:
        return assess_pew_risk(float(avg_protein_g), float(avg_energy_kcal),
                               float(target_protein_g), float(target_energy_kcal),
                               albumin_g_L)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def record_pew_risk_tool(patient_id: str, date: str, score: float, level: str) -> dict[str, Any]:
    """落库一个 PEW 历史点（ADR-007：PEW 历史归属本包）。level: low / medium / high。"""
    try:
        return record_pew_risk(patient_id, date, float(score), level)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_pew_history_tool(patient_id: str) -> dict[str, Any]:
    """查 PEW 历史序列与趋势（improving / worsening / stable / no_data）。"""
    try:
        return get_pew_history(patient_id)
    except Exception as exc:
        return _invalid(exc)


# ---- DAG: 一键营养评估 (v2.3) ----

_PEW_SCORE = {"low": 0.0, "medium": 1.0, "high": 2.0}


@mcp.tool
def comprehensive_nutrition_assessment_tool(
    age_years: float,
    sex: str,
    weight_kg: float,
    height_cm: float,
    ckd_stage: Any = 1,
    dialysis_mode: str = "none",
    vegetarian_mode: str = "mixed",
    is_edema: bool = False,
    serum_albumin_g_l: Optional[float] = None,
    avg_protein_g: Optional[float] = None,
    avg_energy_kcal: Optional[float] = None,
    include_intake: bool = False,
    patient_id: str = "",
    pd_glucose_kcal_per_day: Optional[float] = None,
) -> dict[str, Any]:
    """一键营养评估：Z 评分 → PRNT 目标 → 摄入达成率 → PEW 评定。仅 CKD 临床助手。

    ckd_stage: CKD 分期(1-5D)。注意 PRNT 数值目标与分期无关（仅由年龄×性别×体重驱动），
    ckd_stage 仅用于 stage=1 的沿用提示，不影响能量/蛋白计算结果。
    内部串联 calc_growth_zscore → calc_prnt_targets → assess_intake_vs_target → assess_pew_risk，
    原 3-4 次 LLM 调用压缩为 1 次。生长状态由 Z 评分自动推导后回灌 PRNT，无需手填。

    摄入来源优先级：显式 avg_protein_g/avg_energy_kcal > include_intake+patient_id 查饮食日记。
    两者都没有则跳过摄入与 PEW 环节（仅出 Z 评分 + 目标）。

    pd_glucose_kcal_per_day: 腹透患儿从透析液吸收的葡萄糖供能（kcal/day），
    会扣减膳食能量目标（BUG-08 修复：此前 DAG 未透传该参数，腹透能量目标偏高）。
    """
    try:
        # BUG-03 修复：DAG 一键评估属临床工具（已登记 CLINICAL_TOOLS），入口显式收口，
        # 不依赖子函数各自的 enforce_*（防御纵深）。
        enforce_nutrition_tool(get_caller(), "comprehensive_nutrition_assessment")
        stage = _stage_int(ckd_stage)

        # 1) 生长 Z 评分（一次调用同时给 HAZ/WAZ/BAZ 与 growth_status 建议）
        z = calc_growth_zscore(float(age_years), sex,
                               height_cm=float(height_cm) if height_cm else None,
                               weight_kg=float(weight_kg) if weight_kg else None)
        if not z.get("ok"):
            return z
        growth_status = z["data"].get("growth_status_suggestion", "normal")

        # 2) PRNT 目标（回灌生长状态与水肿校正；BUG-08：透传 pd_glucose_kcal_per_day）
        prnt = calc_prnt_targets(
            age_years=float(age_years), sex=sex, weight_kg=float(weight_kg),
            height_cm=float(height_cm or 0.0), ckd_stage=stage,
            dialysis_mode=dialysis_mode, vegetarian_mode=vegetarian_mode,
            growth_status=growth_status, is_edema=bool(is_edema),
            pd_glucose_kcal_per_day=pd_glucose_kcal_per_day,
        )
        if not prnt.get("ok"):
            return prnt
        target_p = prnt["data"]["protein"]["target_g_per_day"]
        target_e = prnt["data"]["energy"]["target_kcal_per_day"]

        result: dict[str, Any] = {
            "ok": True,
            "data": {
                "growth": z["data"],
                "growth_status_used": growth_status,
                "prnt_targets": prnt["data"],
                "intake_assessment": None,
                "pew": None,
                "notes": [],
            },
        }
        d = result["data"]

        # 3) 摄入均值：显式入参优先，其次查饮食日记
        diet: Optional[dict[str, Any]] = None
        if avg_protein_g is not None and avg_energy_kcal is not None:
            diet = {"avg_protein_g": float(avg_protein_g),
                    "avg_energy_kcal": float(avg_energy_kcal)}
            d["notes"].append("摄入均值来自调用方直接提供。")
        elif include_intake and patient_id:
            # BUG-29 说明（2026-08-12）：DAG 仅临床角色可调（入口 enforce_nutrition_tool 已拦家长），
            # 故此处 get_food_diary_summary 以 doctor 身份调用，_guard_guardian 对非 parent 直接放行，
            # 无需（也不应）透传 guardian_token——若未来 DAG 入口放开给受限角色，此处须补绑定校验。
            summary = get_food_diary_summary(patient_id)
            dd = (summary.get("data") or {}).get("diet_diary_3d") if summary.get("ok") else None
            if dd:
                diet = dict(dd)
                d["notes"].append(f"摄入均值来自饮食日记最近 3 日（patient_id={patient_id}）。")
            else:
                d["notes"].append(f"患者 {patient_id} 暂无饮食日记，跳过摄入与 PEW 评估。")
        else:
            d["notes"].append("未提供摄入数据（avg_* 或 include_intake+patient_id），跳过摄入与 PEW 评估。")

        # 4) 摄入 vs 目标 + 5) PEW
        if diet:
            intake = assess_intake_vs_target(
                diet, age_years=float(age_years), sex=sex, weight_kg=float(weight_kg),
                ckd_stage=stage, dialysis_mode=dialysis_mode, vegetarian_mode=vegetarian_mode,
                growth_status=growth_status, height_cm=float(height_cm or 0.0),
                is_edema=bool(is_edema),
                pd_glucose_kcal_per_day=pd_glucose_kcal_per_day,  # BUG-08：透传腹透扣减
            )
            d["intake_assessment"] = intake.get("data") if intake.get("ok") else intake

            pew = assess_pew_risk(
                float(diet.get("avg_protein_g", 0.0)), float(diet.get("avg_energy_kcal", 0.0)),
                float(target_p), float(target_e), serum_albumin_g_l,
            )
            if pew.get("ok"):
                level = pew["data"]["pew_risk"]
                d["pew"] = {**pew["data"], "score": _PEW_SCORE.get(level, 0.0),
                            "hint": "如需留痕请调 record_pew_risk_tool(patient_id, date, score, level)"}
            else:
                d["pew"] = pew
        return result
    except Exception as exc:
        return _invalid(exc)


if __name__ == "__main__":
    main()
