"""P2 营养计算域 MCP 服务：PRNT目标 + 食物DB + 食谱 + 生长评估 + DAG。

合并自 M3 (nutrition-assessment) + M5 (nutrition-calc) + M7 (meal-plan)。
v2.3 新增 DAG: comprehensive_nutrition_assessment（Z→PRNT→PEW 一键）。
Tool Masking: diet/food 组全角色可见；recipe/clinical 组仅临床助手。

v0.3.2 修复：工具包装层签名与 core 全面对齐（此前 11 个工具因位置参数错位而崩溃
或静默算错，详见 docs 复盘）。本层只做「入参归一化 + 透传」，不再自造参数语义。
"""
from __future__ import annotations

import logging
import os
from typing import Any

from a207_policy import enforce_nutrition_tool, get_caller, translate_error
from fastmcp import FastMCP

from .constants import DIALYSIS_ALIAS
from .core import (
    assess_intake_vs_target,
    assess_pew_risk,
    calc_growth_zscore,
    calc_prnt_targets,
    get_food_diary_summary,
    get_pew_history,
    record_child_food,
    record_pew_risk,
    upsert_food_diary,
)
from .diary import sum_diet_intake
from .foods import (
    lookup_food_nutrients,
    substitute_food,
)
from .mealplan import generate_meal_plan, get_meal_plan_nutrients
from .pharma import check_drug_nutrient_interaction
from .targets import calc_pd_glucose_absorption

mcp = FastMCP("CKDNutri-nutrition-mcp")

# B2（2026-08-12 五包审查）：异常分级归类统一到 care/assessment 口径——
# ① ValueError（core 业务/参数校验）归 INVALID_INPUT 且 detail 保留；
# ② 内部数据错误（文件/JSON/RuntimeError）归 INTERNAL_ERROR；
# ③ 未知系统异常（TypeError/KeyError/AttributeError 等内部 Code Bug）归
#   INTERNAL_ERROR 且 detail **脱敏**（不裸暴露 str(exc)，完整堆栈仅服务端日志）。
logger = logging.getLogger("CKDNutri-nutrition-mcp")


def _invalid(exc):
    # B2 中心化（2026-08-15）：异常翻译收敛到 a207_policy.translate_error 单实现
    return translate_error(exc, domain="P2", logger=logger)


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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")  # C2（2026-08-15）：生产 stdout 可采集
    logger = logging.getLogger(__name__)
    # P2-7（2026-08-18）：启动自检按后端条件分支（对齐 P1 临床数据域）——本地 JSON
    # 开发模式（A207_STORAGE_BACKEND=json）此前无条件走 OTS 自检：LocalJsonRepository
    # 无 _get_client → AttributeError 被误报为"无法连接表格存储"（误导排障），开发
    # 环境无法启动。json → 跳过 OTS 自检；未知后端 → fail-fast SystemExit(1)。
    from .nutrition_repository import STORAGE_BACKEND_ENV

    backend = os.environ.get(STORAGE_BACKEND_ENV, "tablestore").strip().lower()
    if backend not in ("tablestore", "json"):
        logger.error("[selfcheck] FAIL A207_STORAGE_BACKEND=%r 非法（仅支持 "
                     "tablestore/json），服务未启动。", backend)
        raise SystemExit(1)
    if backend == "json":
        logger.info("[selfcheck] OK 本地 JSON 开发模式（A207_STORAGE_BACKEND=json），跳过 OTS 自检")
    else:
        # A3（2026-08-15）：启动 OTS 自检 fail-fast（对齐 P1）——此前缺 A207_OTS_* 参数时
        # "服务活着但每个工具 INTERNAL_ERROR"（比启动失败更难发现，医疗数据读写全挂）。
        try:
            from .nutrition_repository import get_repository

            repo = get_repository()
            tables = repo._get_client().list_table()
            logger.info("[ots-selfcheck] OK 已连通表格存储，表=%s", sorted(tables))
            # D4（2026-08-18）：部署即初始化缺失表（幂等）——此前 ensure_tablestore_tables
            # 零调用点，魔搭新实例部署后 food_diary/pew_history 表不存在，读写全
            # INTERNAL_ERROR（NUTR_DATA）。ensure_tables 仅建缺失表，不影响已建表。
            from .nutrition_repository import ensure_tablestore_tables

            ensure_tablestore_tables()
        except Exception as exc:
            logger.error(
                "[ots-selfcheck] FAIL 无法连接表格存储（%s）。检查 A207_OTS_* "
                "环境变量与网络后重试；服务未启动。", type(exc).__name__)
            raise SystemExit(1) from exc
    mcp.run()


# ---- diet 组（饮食记录，全角色可见） ----

@mcp.tool
def upsert_food_diary_tool(patient_id: str, entries: list, write_mode: bool = True,
                           guardian_token: str | None = None) -> dict[str, Any]:
    """写入每日饮食日记。家长/医生可写，家长须携带 guardian_token 完成患儿绑定。

    entries 每项：{date, meal, food, energy_kcal, protein_g, potassium_mg, phosphorus_mg, sodium_mg}
    """
    try:
        return upsert_food_diary(patient_id, entries, write_mode, guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_food_diary_summary_tool(patient_id: str, guardian_token: str | None = None) -> dict[str, Any]:
    """查最近饮食日记摘要（含最近 3 日均值 diet_diary_3d；摘要含食物名称，非脱敏展示）。家长须携带 guardian_token。

    2026-08-21 起双段输出：food_diary（家长/医生记录，diet_diary_3d 仅聚合此段）
    + child_foodlog（孩子自报，参考数据，不计入营养评估）。
    """
    try:
        return get_food_diary_summary(patient_id, guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def record_child_food_tool(patient_id: str, entries: list, write_mode: bool = True) -> dict[str, Any]:
    """孩子自报饮食记录（写 child_foodlog，**仅患儿身份 child_assistant 可写**）。

    参考数据（不作医疗结论、不进营养评估），家长/医生只读。每次成功写入 +1 分
    （同一天最多 +5，跨天重置），返回累计积分与"小肾侠"段位/鼓励话术。

    entries 每项：{date, meal?, food, amount?}（amount 为孩子自述量，自由文本）
    """
    try:
        return record_child_food(patient_id, entries, write_mode)
    except Exception as exc:
        return _invalid(exc)


# ---- food 组（食物查询，全角色可见） ----

@mcp.tool
def lookup_food_nutrients_tool(food_name: str, portion: str | None = None,
                               include_household: bool = True) -> dict[str, Any]:
    """查食物营养成分。portion 可传家庭量具（如“一碗”“半个”），缺省按 100 g。

    include_household=True（默认）时，输出内嵌 measure（家庭量具表达）与
    pnpr（磷蛋白比+分级），无需再单独调用换算/磷蛋白比工具。
    """
    try:
        return lookup_food_nutrients(food_name, portion, include_household=include_household)
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
def sum_diet_intake_tool(diary: list, target: dict | None = None) -> dict[str, Any]:
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
def check_drug_nutrient_interaction_tool(drug_name: str, nutrient: str | None = None) -> dict[str, Any]:
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
    exclude_foods: list | None = None,
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
    pd_glucose_kcal_per_day: float | None = None,
    height_age_years: float | None = None,
    high_urea_persistent: bool = False,
) -> dict[str, Any]:
    """计算 PRNT 2020 每日能量与蛋白目标。仅 CKD 临床助手。

    ckd_stage: CKD 分期(1-5D)。注意 PRNT 2020 的能量/蛋白数值目标与分期无关
    （仅由年龄×性别×体重驱动），ckd_stage 仅用于 stage=1 的沿用提示，不影响计算结果。
    dialysis_mode: none / peritoneal / hemodialysis（兼容 pd/腹透 等别名）；vegetarian_mode: mixed / lacto_ovo / vegan；
    growth_status: normal / failure / overweight（可取 calc_growth_zscore 的 growth_status_suggestion）；
    is_edema=True 时改用 BMI-P50 理想体重开处方（dry weight 原则）。
    N-S1（2026-08-14）：height_age_years=身高年龄（严重生长迟缓按身高年龄查 SDI，如 8 表示
    身高对应 8 岁）；high_urea_persistent=True=持续高尿素血症（排除脱水/分解代谢/激素后蛋白
    目标降至 SDI 下限）。透析/生长不良/身高年龄/高尿素患者返回 regimens=[standard, adjusted]
    双方案（data.energy/protein 为 adjusted 值），普通患者仅 1 个 standard 方案。
    """
    try:
        return calc_prnt_targets(
            age_years=float(age_years), sex=sex, weight_kg=float(weight_kg),
            height_cm=float(height_cm or 0.0), ckd_stage=_stage_int(ckd_stage),
            dialysis_mode=dialysis_mode, vegetarian_mode=vegetarian_mode,
            growth_status=growth_status, is_edema=bool(is_edema),
            pd_glucose_kcal_per_day=pd_glucose_kcal_per_day,
            height_age_years=float(height_age_years) if height_age_years is not None else None,
            # 原样传递，由 core 层做严格 bool 校验（bool("false")==True 陷阱；pydantic 已先行解析）
            high_urea_persistent=high_urea_persistent,
        )
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def calc_growth_zscore_tool(
    age_years: float,
    sex: str,
    height_cm: float | None = None,
    weight_kg: float | None = None,
    bmi: float | None = None,
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
    albumin_g_L: float | None = None,
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
    """落库一个 PEW 风险历史点（供后续趋势评估）。level: low / medium / high。"""
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
# N-S5（2026-08-14）：删除 _PEW_SCORE（low=0/medium=1/high=2）——PEW score 唯一口径
# 是 assess_pew_risk 的信号加权 0-100 分，双轨会被编排层误当同一标尺（历史分数污染）。


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
    serum_albumin_g_l: float | None = None,
    avg_protein_g: float | None = None,
    avg_energy_kcal: float | None = None,
    include_intake: bool = False,
    patient_id: str = "",
    pd_glucose_kcal_per_day: float | None = None,
    pd_dwell_hours: float | None = None,
    pd_dialysate_glucose_g: float | None = None,
    pd_dialysate_volume_ml: float | None = None,
    pd_glucose_conc_pct: float | None = None,
    pd_exchanges_per_day: int = 1,
    pd_transport_type: str = "average",
    height_age_years: float | None = None,
    high_urea_persistent: bool = False,
) -> dict[str, Any]:
    """一键营养评估：Z 评分 → PRNT 目标 → 摄入达成率 → PEW 评定。仅 CKD 临床助手。

    ckd_stage: CKD 分期(1-5D)。注意 PRNT 数值目标与分期无关（仅由年龄×性别×体重驱动），
    ckd_stage 仅用于 stage=1 的沿用提示，不影响能量/蛋白计算结果。
    执行流程：Z 评分 → PRNT 目标 → 摄入达成率 → PEW 评定，
    原 3-4 次 LLM 调用压缩为 1 次。生长状态由 Z 评分自动推导后回灌 PRNT，无需手填。

    摄入来源优先级：显式 avg_protein_g/avg_energy_kcal > include_intake+patient_id 查饮食日记。
    两者都没有则跳过摄入与 PEW 环节（仅出 Z 评分 + 目标）。

    腹透能量修正：pd_glucose_kcal_per_day 可直接传入（已算好的吸收能量）；
    也可以传 pd_dwell_hours + （pd_dialysate_glucose_g 或
    pd_dialysate_volume_ml+pd_glucose_conc_pct），由本 DAG 内部调用腹透葡萄糖吸收
    计算并自动扣减——LLM 无需先调独立工具。非腹透患儿不触发。
    """
    try:
        # BUG-03 修复：DAG 一键评估属临床工具（已登记 CLINICAL_TOOLS），入口显式收口，
        # 不依赖子函数各自的 enforce_*（防御纵深）。
        enforce_nutrition_tool(get_caller(), "comprehensive_nutrition_assessment")
        stage = _stage_int(ckd_stage)

        # v2.4：腹透吸收在 DAG 内部计算（C3 下沉）——非腹透一律不触发。
        # 显式 pd_glucose_kcal_per_day 优先；否则按透析模式 + 处方参数内部算。
        pd_kcal = pd_glucose_kcal_per_day
        pd_notes: list[str] = []
        normalized_dialysis = DIALYSIS_ALIAS.get(
            str(dialysis_mode or "").strip().lower(), "none")
        if pd_kcal is None and normalized_dialysis == "peritoneal":
            if pd_dialysate_glucose_g is None and (
                    pd_dialysate_volume_ml is None or pd_glucose_conc_pct is None):
                pd_notes.append("腹透患儿未提供透析液糖量/留腹时长，能量目标未扣减葡萄糖吸收。")
            else:
                glucose_g = pd_dialysate_glucose_g
                if glucose_g is None:
                    glucose_g = float(pd_dialysate_volume_ml) * float(pd_glucose_conc_pct) / 100.0
                pd_result = calc_pd_glucose_absorption(
                    float(glucose_g), float(pd_dwell_hours or 0.0),
                    exchanges_per_day=int(pd_exchanges_per_day or 1),
                    transport_type=pd_transport_type or "average",
                    weight_kg=float(weight_kg) if weight_kg else None,
                )
                if pd_result.get("ok"):
                    pd_kcal = pd_result["data"]["absorbed_energy_kcal_per_day"]
                    pd_notes.append(
                        f"DAG 内部计算腹透葡萄糖吸收 {pd_kcal} kcal/d，已从膳食能量目标扣减。")
                else:
                    # 九审（2026-08-16）：失败 detail 仅记服务端日志——此前内联进
                    # ok:True 的 notes（内部上下文外泄给调用方；且"成功响应携带失败
                    # 细节"破坏成功语义）。对外给中性提示，完整原因留日志排查。
                    logger.warning(
                        "comprehensive_nutrition_assessment DAG 腹透葡萄糖吸收计算失败: %s",
                        pd_result.get("detail", "未知原因"))
                    pd_notes.append("腹透葡萄糖吸收计算失败，能量目标未扣减，请核查透析处方参数。")

        # 1) 生长 Z 评分（一次调用同时给 HAZ/WAZ/BAZ 与 growth_status 建议）
        z = calc_growth_zscore(float(age_years), sex,
                               height_cm=float(height_cm) if height_cm else None,
                               weight_kg=float(weight_kg) if weight_kg else None)
        if not z.get("ok"):
            return z
        growth_status = z["data"].get("growth_status_suggestion", "normal")

        # 2) PRNT 目标（回灌生长状态与水肿校正；BUG-08：透传 pd_glucose_kcal_per_day）
        # N-S1（2026-08-14）：透传 height_age_years / high_urea_persistent 临床调整
        prnt = calc_prnt_targets(
            age_years=float(age_years), sex=sex, weight_kg=float(weight_kg),
            height_cm=float(height_cm or 0.0), ckd_stage=stage,
            dialysis_mode=dialysis_mode, vegetarian_mode=vegetarian_mode,
            growth_status=growth_status, is_edema=bool(is_edema),
            pd_glucose_kcal_per_day=pd_kcal,
            height_age_years=float(height_age_years) if height_age_years is not None else None,
            high_urea_persistent=high_urea_persistent,
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
                "notes": list(pd_notes),
            },
        }
        d = result["data"]

        # 3) 摄入均值：显式入参优先，其次查饮食日记
        diet: dict[str, Any] | None = None
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
                pd_glucose_kcal_per_day=pd_kcal,  # BUG-08：透传腹透扣减
                albumin_g_L=serum_albumin_g_l,  # BUG-61：白蛋白参与摄入路径 PEW 筛查
                # 八审（2026-08-16）：M1 修复不完整——DAG 此前只给 calc_prnt_targets
                # 透传 height_age_years/high_urea_persistent（:403-404），摄入评估段漏传，
                # 默认 high_urea_persistent=False 重算目标 → 同一患儿 PRNT 区块（蛋白下限
                # 目标）与摄入达成率（上限目标）出现两个矛盾数字。此处与 :403-404 同口径。
                height_age_years=float(height_age_years) if height_age_years is not None else None,
                high_urea_persistent=high_urea_persistent,
            )
            d["intake_assessment"] = intake.get("data") if intake.get("ok") else intake

            # 夜审（2026-08-23）P2-加固：摄入评估失败时**熔断** PEW 计算。
            # 此前无论 intake.ok 与否均继续 assess_pew_risk，且用 diet.get(..., 0.0) 回退
            # 0 蛋白/0 能量 → 在 intake 因上游参数非法返回 ok=False 时，PEW 会按"0 摄入"
            # 强行评估，产出虚假 high 临床报警。fail-closed：摄入不可信则 PEW 不可信。
            if not intake.get("ok"):
                d["pew"] = {
                    "ok": False,
                    "error": "INTAKE_ASSESSMENT_FAILED",
                    "detail": "摄入评估未通过，跳过 PEW 风险计算以防产生虚假高危结论",
                }
            else:
                pew = assess_pew_risk(
                    float(diet.get("avg_protein_g", 0.0)), float(diet.get("avg_energy_kcal", 0.0)),
                    float(target_p), float(target_e), serum_albumin_g_l,
                    # BUG-42：PEW 蛋白下限用 PRNT 官方 SDI 下限（floor_g_per_day），
                    # 不用"目标×85%"近似（婴儿段会把 1.52-2.1 g/kg 的合规摄入误判为低于安全下限）
                    floor_protein_g=float(prnt["data"]["protein"]["floor_g_per_day"]),
                )
                if pew.get("ok"):
                    # N-S5 修复（2026-08-14）：PEW score 统一为 assess_pew_risk 的信号加权
                    # 0-100 分——此前用 _PEW_SCORE（low=0/medium=1/high=2）覆盖并把 0/1/2
                    # 语义经 hint 引导传给 record_pew_risk 落库，历史 PEW 分数全错。
                    d["pew"] = {**pew["data"],
                                "score": pew["data"]["score"],
                                "hint": "如需留痕请调 record_pew_risk_tool(patient_id, date, score, level)，score 取本结果 data.score（0-100）"}
                else:
                    d["pew"] = pew
        return result
    except Exception as exc:
        return _invalid(exc)


if __name__ == "__main__":
    main()
