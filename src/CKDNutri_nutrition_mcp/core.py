# -*- coding: utf-8 -*-
"""M3 核心逻辑（纯函数，无 fastmcp 依赖，可单测）。

内容：
1. PRNT 2020 能量/蛋白质 SDI 目标引擎（按年龄×性别分段 + CKD 分期/透析/素食/生长状态调整）
2. 3 日饮食日记聚合（diet_diary_3d）
3. 摄入达成率与 PEW（蛋白质-能量消耗）风险筛查

数据来源（权威标尺，Wave 2 以本文件为准）：
  Shaw V, Polderman N, Renken-Terhaerdt J, et al. Energy and protein requirements for
  children with CKD stages 2-5 and on dialysis - clinical practice recommendations from
  the Pediatric Renal Nutrition Taskforce. Pediatr Nephrol. 2020;35:519-531.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from a207_policy import (
    NUTRITION_ASSESSMENT_WRITE_ALLOWED,
    atomic_write_json,
    enforce_read,
    get_caller,
    resolve_state_path,
)
from .constants import DIALYSIS_ALIAS

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
GUIDELINE = "PRNT 2020 (Shaw et al., Pediatr Nephrol 35:519-531)"
MCP_NAME = "CKDNutri-nutrition-mcp"

# P1-3：运行时写库不落安装目录。A207_NUTRITION_ASSESSMENT_DATA_DIR 为开发/测试 override。
_DATA_DIR_ENV = "A207_NUTRITION_ASSESSMENT_DATA_DIR"


def _require(value: Any, name: str) -> Any:
    """入口参数校验（F6）：必填数值/参数传 None 时显式抛出域错误，避免下游 TypeError。

    配合 server 层的 try/except → _invalid()，最终以 {ok:False, error:"INVALID_INPUT"}
    信封返回，而非把未捕获的 TypeError 暴露给调用方。
    """
    if value is None:
        raise ValueError(f"{name} 不能为 None")
    return value


def _state_path(filename: str) -> str:
    override = os.environ.get(_DATA_DIR_ENV)
    if override:
        root = Path(override)
        root.mkdir(parents=True, exist_ok=True)
        return str(root / filename)
    return str(resolve_state_path(filename))


DIARY_STORE = _state_path("diary_store.json")

# 允许调用方集合（仅 upsert 写工具做 MX-3 收口校验用）
# P1-1：唯一事实源在 a207_policy，本包不再维护第二份。
_WRITE_ALLOWED_CALLERS = NUTRITION_ASSESSMENT_WRITE_ALLOWED

# 素食蛋白倍数：蛋奶素 1.2 / 纯素 1.3（植物蛋白生物利用度低）
_VEG_MULT = {"mixed": 1.0, "ovo_lacto": 1.2, "vegan": 1.3}

# 透析蛋白额外补充（g/kg/day）：PD 0.15-0.3，HD 0.1
_DIALYSIS_EXTRA = {
    "none": (0.0, 0.0),
    "hemodialysis": (0.10, 0.10),
    "peritoneal": (0.15, 0.30),
}

# --- Schofield / 水肿 / 腹透葡萄糖（从 M5 移植，使 M3 成为唯一 PRNT 权威引擎）---
# 移植目的：去重后 M5 不再提供目标计算，但其 Schofield 交叉校验、水肿理想体重校正、
# 腹透葡萄糖吸收扣减等临床特性有价值，故并入 M3，保证单一权威引擎不丢能力。
SCHOFIELD = {
    ("M", 3): (0.0007, 6.349, -2.584),
    ("M", 10): (0.082, 0.545, 1.736),
    ("M", 18): (0.071, 2.132, -1.184),
    ("F", 3): (0.068, 4.281, -1.730),
    ("F", 10): (0.071, 0.677, 1.553),
    ("F", 18): (0.035, 1.948, 0.837),
}
PAL_DEFAULT = 1.4          # CKD 患儿常见轻体力活动系数
KJ_PER_KCAL = 4.184
BMI_P50 = {
    2: (16.4, 16.1), 3: (16.0, 15.7), 4: (15.8, 15.4), 5: (15.5, 15.3),
    6: (15.5, 15.3), 7: (15.7, 15.5), 8: (16.0, 15.9), 9: (16.4, 16.4),
    10: (16.9, 16.9), 11: (17.4, 17.5), 12: (18.0, 18.2), 13: (18.6, 18.9),
    14: (19.3, 19.6), 15: (19.9, 20.2), 16: (20.5, 20.7), 17: (21.1, 21.1),
    18: (21.7, 21.4),
}
GLUCOSE_KCAL_PER_G = 3.4   # 葡萄糖一水合物（腹透液用糖）
PD_ABSORB_ANCHORS = ((1.0, 0.30), (2.0, 0.38), (4.0, 0.55),
                     (6.0, 0.65), (8.0, 0.72), (12.0, 0.80))
PD_TRANSPORT_FACTOR = {"high": 1.15, "high_average": 1.05, "average": 1.0,
                       "low_average": 0.92, "low": 0.85}
PD_GLUCOSE_KCAL_PER_KG_REF = (7.5, 9.08)  # PRNT 引用的日吸收参考区间

# ---------------------------------------------------------------------------
# PRNT 2020 SDI 表
# 每条：(age_min, age_max, 标签, 能量_M(lo,hi), 能量_F(lo,hi), 蛋白(lo,hi), 每日蛋白总量)
#   婴儿段无性别拆分 → M/F 同值；蛋白总量对 15-17 岁按性别拆分（dict）。
# 单位为：能量 kcal/kg/day；蛋白 g/kg/day；每日总量 g。
# ---------------------------------------------------------------------------
_PRNT_BANDS = [
    (0.0,    0.0833, "足月新生儿(0月)", (93, 107), (93, 107), (1.52, 2.50), (8, 12)),
    (0.0833, 0.1667, "1月龄",          (93, 120), (93, 120), (1.52, 1.80), (8, 12)),
    (0.1667, 0.4167, "3月龄",          (82, 98),  (82, 98),  (1.40, 1.52), (8, 12)),
    (0.4167, 1.0,    "6-9月龄",        (72, 82),  (72, 82),  (1.10, 1.30), (9, 14)),
    (1.0,    1.5,    "12月龄",         (72, 120), (72, 120), (0.90, 1.14), (11, 14)),
    (1.5,    3.0,    "2岁",            (81, 95),  (79, 92),  (0.90, 1.05), (11, 15)),
    (3.0,    7.0,    "4-6岁",          (67, 93),  (64, 90),  (0.85, 0.95), (16, 22)),
    (7.0,    12.0,   "9-10岁",         (55, 69),  (49, 63),  (0.90, 0.95), (26, 40)),
    (12.0,   18.01,  "15-17岁",        (40, 55),  (36, 46),  (0.80, 0.90),
     {"M": (52, 65), "F": (45, 49)}),
]


def _round(x: float, n: int = 2) -> float:
    return round(x, n)


def _band_for_age(age: float, sex: str) -> dict[str, Any]:
    """按年龄选 PRNT 段；≥18 取最后一段。返回结构化字典。"""
    if age < 0:
        age = 0.0
    chosen = _PRNT_BANDS[0]
    for b in _PRNT_BANDS:
        if b[0] <= age < b[1]:
            chosen = b
            break
    else:
        chosen = _PRNT_BANDS[-1]
    age_min, age_max, label, e_m, e_f, prot, prot_total = chosen
    energy = e_m if sex == "M" else e_f
    if isinstance(prot_total, dict):
        prot_total_val = prot_total.get(sex, prot_total.get("M", (0, 0)))
    else:
        prot_total_val = prot_total
    return {
        "label": label,
        "energy_sdi": list(energy),         # [lo, hi] kcal/kg/day
        "protein_sdi": list(prot),          # [lo=floor, hi=target] g/kg/day
        "protein_total_daily": list(prot_total_val),
    }


def _interp(lo_anchor: float, hi_anchor: float, lo_value: float, hi_value: float,
            point: float) -> float:
    if hi_anchor == lo_anchor:
        return lo_value
    ratio = (point - lo_anchor) / (hi_anchor - lo_anchor)
    return lo_value + (hi_value - lo_value) * ratio


def _pd_absorption_fraction(dwell_hours: float) -> float:
    anchors = PD_ABSORB_ANCHORS
    if dwell_hours <= anchors[0][0]:
        return anchors[0][1]
    if dwell_hours >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(len(anchors) - 1):
        lo_h, lo_f = anchors[i]
        hi_h, hi_f = anchors[i + 1]
        if lo_h <= dwell_hours <= hi_h:
            if hi_h == lo_h:
                return lo_f
            return lo_f + (hi_f - lo_f) * (dwell_hours - lo_h) / (hi_h - lo_h)
    return anchors[-1][1]


def schofield_bmr_kcal(sex: str, age_years: float, weight_kg: float,
                       height_cm: float) -> float | None:
    """Schofield 体重+身高方程，返回 kcal/d。"""
    if weight_kg <= 0 or height_cm <= 0:
        return None
    bracket = 3 if age_years < 3 else (10 if age_years < 10 else 18)
    coefficients = SCHOFIELD.get((sex, bracket))
    if not coefficients:
        return None
    a, b, c = coefficients
    megajoules = a * weight_kg + b * (height_cm / 100.0) + c
    return round(megajoules * 1000.0 / KJ_PER_KCAL, 1)


def ideal_body_weight_kg(age_years: float, sex: str, height_cm: float) -> float | None:
    """按 BMI 第 50 百分位反推参考体重，用于水肿时避免以水重开处方。"""
    if height_cm <= 0:
        return None
    years = sorted(BMI_P50)
    age = min(max(age_years, years[0]), years[-1])
    lower = max(y for y in years if y <= age)
    upper = min(y for y in years if y >= age)
    index = 0 if sex == "M" else 1
    bmi = BMI_P50[lower][index] if lower == upper else _interp(
        lower, upper, BMI_P50[lower][index], BMI_P50[upper][index], age)
    return round(bmi * (height_cm / 100.0) ** 2, 2)


# ---------------------------------------------------------------------------
# 1. PRNT 2020 目标引擎
# ---------------------------------------------------------------------------
def calc_prnt_targets(
    age_years: float,
    sex: str,
    weight_kg: float,
    height_cm: float = 0.0,
    ckd_stage: int = 1,
    dialysis_mode: str = "none",
    vegetarian_mode: str = "mixed",
    growth_status: str = "normal",
    is_edema: bool = False,
    pd_glucose_kcal_per_day: float | None = None,
) -> dict[str, Any]:
    """计算儿童 CKD 每日能量与蛋白质目标（PRNT 2020 权威口径，M3 唯一权威引擎）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。

    能量：初始=SDI 100%；growth_status=failure 取 SDI 上限，overweight 取下限，normal 取中点。
    蛋白质：目标=SDI 上限；绝对下限=SDI 下限（绝不可低于）；透析额外补充叠加于上限与下限之上
            （补偿透析丢失，避免负氮平衡）；素食按倍数上调。

    移植自 M5 的临床特性（去重后 M3 成为唯一标尺，能力不丢）：
      - is_edema=True：以 BMI-P50 理想体重（干体重）替代实际体重开处方，避免以"水重"高估需求。
      - pd_glucose_kcal_per_day：腹透患者从透析液吸收葡萄糖供能，等量减少膳食能量目标，避免超额。
      - Schofield 交叉校验：独立估算 BMR，信息性对照 SDI 目标，不覆盖权威数。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    _require(age_years, "age_years")
    _require(weight_kg, "weight_kg")
    sex = sex if sex in ("M", "F") else "M"
    if vegetarian_mode not in _VEG_MULT:
        vegetarian_mode = "mixed"
    # F7：用 DIALYSIS_ALIAS 单一事实源归一化（兼容 pd/腹透/hemodialysis 等别名），
    # 避免裸 _DIALYSIS_EXTRA 成员判断把 "pd" 等别名静默降级为 "none"。
    dialysis_mode = DIALYSIS_ALIAS.get(dialysis_mode, "none") if dialysis_mode else "none"

    band = _band_for_age(age_years, sex)
    e_lo, e_hi = band["energy_sdi"]
    p_lo, p_hi = band["protein_sdi"]
    veg = _VEG_MULT[vegetarian_mode]
    d_lo, d_hi = _DIALYSIS_EXTRA[dialysis_mode]
    d_mid = (d_lo + d_hi) / 2.0 if d_hi > d_lo else d_lo

    # 能量取点
    if growth_status == "failure":
        e_pt = e_hi
        e_basis = "生长不良：向 SDI 上限调整"
    elif growth_status == "overweight":
        e_pt = e_lo
        e_basis = "超重/肥胖：向下调整以实现适宜体重增长（不损害营养状况）"
    else:
        e_pt = (e_lo + e_hi) / 2.0
        e_basis = "生长正常：取 SDI 中点（约 100% SDI）"

    energy_per_kg = e_pt

    # 水肿校正：用 BMI-P50 理想体重替代实际体重开处方（dry weight 原则）
    eff_weight = weight_kg
    weight_basis = "实际体重"
    if is_edema:
        ibw = ideal_body_weight_kg(age_years, sex, height_cm)
        if ibw and ibw > 0:
            eff_weight = ibw
            weight_basis = "水肿校正理想体重(BMI-P50)"
            e_basis += "；水肿：采用理想体重开处方（dry weight 原则）"

    energy_day = energy_per_kg * eff_weight

    # 腹透葡萄糖供能扣减：PD 患者从腹透液吸收葡萄糖，等量减少膳食能量目标
    pd_deduction = 0.0
    if pd_glucose_kcal_per_day is not None and pd_glucose_kcal_per_day > 0:
        pd_deduction = float(pd_glucose_kcal_per_day)
        energy_day = max(energy_day - pd_deduction, 0.0)
        e_basis += f"；腹透葡萄糖供能扣减 {_round(pd_deduction, 1)} kcal/day"

    # 蛋白质：上限为目标，下限为安全底；透析额外补充叠加（补偿丢失）
    protein_target_per_kg = p_hi * veg + d_mid
    protein_floor_per_kg = p_lo * veg + d_mid
    protein_target_g = protein_target_per_kg * eff_weight
    protein_floor_g = protein_floor_per_kg * eff_weight

    # Schofield 交叉校验（信息性，不改变 SDI 权威数）：PAL×BMR 与 SDI 目标对照
    schofield_bmr = schofield_bmr_kcal(sex, age_years, weight_kg, height_cm)
    schofield_cross = None
    if schofield_bmr:
        pal_adjusted = round(schofield_bmr * PAL_DEFAULT, 1)
        deviation = (energy_day - pal_adjusted) / pal_adjusted * 100.0 if pal_adjusted > 0 else 0.0
        schofield_cross = {
            "bmr_kcal_per_day": schofield_bmr,
            "pal_adjusted_kcal_per_day": pal_adjusted,
            "deviation_pct_vs_sdi_target": _round(deviation, 1),
            "flag": "divergent" if abs(deviation) > 25 else "consistent",
            "note": "Schofield 为独立估算，SDI 目标为权威；偏差>25% 提示复核身高/体重/年龄。",
        }

    warnings: list[str] = []
    if ckd_stage == 1:
        warnings.append("PRNT 2020 覆盖 CKD 2-5D；stage 1 暂沿用同表，请结合临床判断。")
    if dialysis_mode != "none":
        warnings.append(
            f"透析额外补充蛋白 {_round(d_mid,2)} g/kg/day（PD 0.15-0.30 / HD 0.10），已叠加于目标与下限。"
        )
    if vegetarian_mode != "mixed":
        warnings.append(
            f"素食模式蛋白需求×{veg}（蛋奶素 1.2 / 纯素 1.3，因植物蛋白生物利用度低）。"
        )
    if is_edema:
        warnings.append(
            f"已启用水肿校正：以理想体重 {_round(eff_weight,1)}kg（BMI-P50）替代实际体重 "
            f"{_round(weight_kg,1)}kg 开处方。"
        )
    if pd_glucose_kcal_per_day is not None and pd_glucose_kcal_per_day > 0:
        warnings.append(
            f"已扣减腹透葡萄糖供能 {_round(pd_deduction,1)} kcal/day，避免能量超额。"
        )
        if dialysis_mode != "peritoneal":
            warnings.append("提供了腹透葡萄糖供能，但当前非腹膜透析模式，请确认处方场景。")

    return {
        "ok": True,
        "data": {
            "guideline": GUIDELINE,
            "age_band": band["label"],
            "sex": sex,
            "ckd_stage": ckd_stage,
            "dialysis_mode": dialysis_mode,
            "vegetarian_mode": vegetarian_mode,
            "growth_status": growth_status,
            "is_edema": is_edema,
            "weight_used_kg": _round(eff_weight, 2),
            "weight_basis": weight_basis,
            "energy": {
                "sdi_kcal_per_kg": [e_lo, e_hi],
                "target_kcal_per_kg": _round(energy_per_kg, 2),
                "target_kcal_per_day": _round(energy_day, 1),
                "pd_glucose_deduction_kcal": _round(pd_deduction, 1),
                "basis": e_basis,
            },
            "protein": {
                "sdi_g_per_kg": [p_lo, p_hi],
                "vegetarian_multiplier": veg,
                "dialysis_extra_g_per_kg": [d_lo, d_hi],
                "target_g_per_kg": _round(protein_target_per_kg, 3),
                "floor_g_per_kg": _round(protein_floor_per_kg, 3),
                "target_g_per_day": _round(protein_target_g, 1),
                "floor_g_per_day": _round(protein_floor_g, 1),
                "note": "目标取 SDI 上限；绝对不可低于 SDI 下限(floor)，透析叠加额外补充。",
            },
            "pe_ratio": {
                "ideal_pct": [7, 12],
                "ckd_acceptable_pct": [5.3, 6.4],
                "requires_total_protein_ge_100pct": True,
            },
            "schofield_cross_check": schofield_cross,
            "warnings": warnings,
        },
    }


# ---------------------------------------------------------------------------
# 2. 摄入 vs 目标 + PEW 筛查
# ---------------------------------------------------------------------------
def assess_intake_vs_target(
    diet: dict[str, Any],
    age_years: float,
    sex: str,
    weight_kg: float,
    ckd_stage: int = 1,
    dialysis_mode: str = "none",
    vegetarian_mode: str = "mixed",
    growth_status: str = "normal",
    height_cm: float = 0.0,
    is_edema: bool = False,
    pd_glucose_kcal_per_day: float | None = None,
) -> dict[str, Any]:
    """对照 PRNT 目标评估 3 日饮食日记均值，给出达成率、缺口/过量、PEW 风险。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    diet 需含：avg_energy_kcal / avg_protein_g / avg_potassium_mg / avg_phosphorus_mg /
              avg_sodium_mg（与 PCP nutrition_assessment.diet_diary_3d 对齐）。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    _require(age_years, "age_years")
    _require(weight_kg, "weight_kg")
    required = ("avg_energy_kcal", "avg_protein_g")
    if not all(k in diet for k in required):
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "diet 需含 avg_energy_kcal 与 avg_protein_g"}
    avg_e = float(diet.get("avg_energy_kcal", 0.0))
    avg_p = float(diet.get("avg_protein_g", 0.0))
    avg_k = float(diet.get("avg_potassium_mg", 0.0))
    avg_ph = float(diet.get("avg_phosphorus_mg", 0.0))
    avg_na = float(diet.get("avg_sodium_mg", 0.0))

    tgt = calc_prnt_targets(
        age_years=age_years, sex=sex, weight_kg=weight_kg, height_cm=height_cm,
        ckd_stage=ckd_stage, dialysis_mode=dialysis_mode, vegetarian_mode=vegetarian_mode,
        growth_status=growth_status, is_edema=is_edema,
        pd_glucose_kcal_per_day=pd_glucose_kcal_per_day,
    )
    if not tgt["ok"]:
        return tgt
    t = tgt["data"]
    target_e = t["energy"]["target_kcal_per_day"]
    target_p = t["protein"]["target_g_per_day"]
    floor_p = t["protein"]["floor_g_per_day"]

    e_pct = (avg_e / target_e * 100.0) if target_e > 0 else 0.0
    p_pct_vs_target = (avg_p / target_p * 100.0) if target_p > 0 else 0.0

    # 状态判定
    if e_pct < 80:
        e_status = "deficit"
    elif e_pct > 120:
        e_status = "excess"
    else:
        e_status = "ok"

    if avg_p < floor_p:
        p_status = "below_floor"
    elif p_pct_vs_target > 130:
        p_status = "excess"
    else:
        p_status = "ok"

    # PE 比（蛋白 4 kcal/g）
    pe_ratio = (avg_p * 4.0 / avg_e * 100.0) if avg_e > 0 else 0.0

    # PEW 风险（简化筛查，非完整诊断）
    pew = _screen_pew(avg_p, avg_e, floor_p, target_e, albumin_g_L=None)

    flags: list[str] = []
    if e_status == "deficit":
        flags.append(f"能量摄入仅达目标 {_round(e_pct,1)}%，低于 80% 建议线，存在生长/分解代谢风险。")
    if p_status == "below_floor":
        flags.append(f"蛋白质摄入 {_round(avg_p,1)}g 低于 PRNT 安全下限 {_round(floor_p,1)}g/day，严禁继续限制。")
    if p_status == "excess":
        flags.append(f"蛋白质摄入达目标 {_round(p_pct_vs_target,1)}%，超过 130%，需监测 BUN/血磷。")
    if pe_ratio and (pe_ratio < 5.3):
        flags.append(f"蛋白-能量比 {_round(pe_ratio,1)}% 偏低，提示蛋白被当作能量供能（PEW 倾向）。")

    return {
        "ok": True,
        "data": {
            "guideline": GUIDELINE,
            "energy": {
                "avg_kcal": _round(avg_e, 1),
                "target_kcal": _round(target_e, 1),
                "achievement_pct": _round(e_pct, 1),
                "status": e_status,
            },
            "protein": {
                "avg_g": _round(avg_p, 1),
                "target_g": _round(target_p, 1),
                "floor_g": _round(floor_p, 1),
                "achievement_pct_vs_target": _round(p_pct_vs_target, 1),
                "status": p_status,
            },
            "electrolytes_avg_mg": {
                "potassium": _round(avg_k, 1),
                "phosphorus": _round(avg_ph, 1),
                "sodium": _round(avg_na, 1),
            },
            "pe_ratio_pct": _round(pe_ratio, 2),
            "pew_risk": pew["risk"],
            "pew_rationale": pew["rationale"],
            "flags": flags,
            "note": "钾/磷/钠上限由临床医生在 clinician_limits 设定，此处仅展示 3 日实测均值。",
        },
    }


def _screen_pew(avg_p: float, avg_e: float, floor_p: float, target_e: float,
                albumin_g_L: float | None) -> dict[str, str]:
    """简化 PEW 风险筛查：蛋白低于安全下限 + 能量低于 80% 目标 → 高风险。"""
    protein_deficit = avg_p < floor_p
    energy_deficit = (avg_e / target_e) < 0.8 if target_e > 0 else False
    low_albumin = (albumin_g_L is not None and albumin_g_L < 35)

    if protein_deficit and energy_deficit:
        risk = "high"
        rationale = "蛋白质低于安全下限且能量摄入 <80% 目标，符合蛋白质-能量消耗（PEW）高风险特征。"
    elif protein_deficit or energy_deficit or low_albumin:
        risk = "medium"
        parts = []
        if protein_deficit:
            parts.append("蛋白质低于安全下限")
        if energy_deficit:
            parts.append("能量 <80% 目标")
        if low_albumin:
            parts.append(f"白蛋白 {albumin_g_L} g/L <35")
        rationale = "存在以下 PEW 预警信号：" + "；".join(parts) + "。"
    else:
        risk = "low"
        rationale = "蛋白质与能量摄入均达 PRNT 安全范围，PEW 风险低。"

    if albumin_g_L is None:
        rationale += "（未提供白蛋白，建议结合血清白蛋白 <35 g/L 与人体测量综合判定）"
    return {"risk": risk, "rationale": rationale}


def assess_pew_risk(
    avg_protein_g: float,
    avg_energy_kcal: float,
    target_protein_g: float,
    target_energy_kcal: float,
    albumin_g_L: float | None = None,
) -> dict[str, Any]:
    """独立 PEW 风险筛查接口（供编排层直接传入已算好的均值与目标）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    _require(avg_protein_g, "avg_protein_g")
    _require(avg_energy_kcal, "avg_energy_kcal")
    _require(target_protein_g, "target_protein_g")
    _require(target_energy_kcal, "target_energy_kcal")
    floor_p = target_protein_g * 0.85  # 以目标 85% 作为保守安全下限近似
    pew = _screen_pew(avg_protein_g, avg_energy_kcal, floor_p, target_energy_kcal, albumin_g_L)
    return {"ok": True, "data": {"pew_risk": pew["risk"], "rationale": pew["rationale"]}}


# ---------------------------------------------------------------------------
# 3. 3 日饮食日记：写入(store) + 聚合(diet_diary_3d) + 读取
# ---------------------------------------------------------------------------
def _load_store() -> dict[str, Any]:
    if os.path.exists(DIARY_STORE):
        try:
            with open(DIARY_STORE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"entries": []}
    return {"entries": []}


def _save_store(store: dict[str, Any]) -> None:
    # OD-014（P2-3）：原子写，避免半写截断静默丢数据
    atomic_write_json(DIARY_STORE, store)


def _aggregate(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """取最近 3 个不重复日期的条目做均值，返回 diet_diary_3d 形状。"""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        by_day.setdefault(e.get("date", ""), []).append(e)
    days = sorted(by_day.keys(), reverse=True)[:3]
    used = [e for d in days for e in by_day[d]]
    n = max(len(used), 1)
    avg = {
        "avg_energy_kcal": sum(e.get("energy_kcal", 0.0) for e in used) / n,
        "avg_protein_g": sum(e.get("protein_g", 0.0) for e in used) / n,
        "avg_potassium_mg": sum(e.get("potassium_mg", 0.0) for e in used) / n,
        "avg_phosphorus_mg": sum(e.get("phosphorus_mg", 0.0) for e in used) / n,
        "avg_sodium_mg": sum(e.get("sodium_mg", 0.0) for e in used) / n,
    }
    return {"day_count": len(days), "entry_count": len(used), "diet_diary_3d": avg}


def upsert_food_diary(
    patient_id: str,
    entries: list[dict[str, Any]] | None = None,
    write_mode: bool = True,
) -> dict[str, Any]:
    """写入/追加饮食条目（MX-3 收口：仅 parent_assistant / child_companion）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    条目由调用方经 M5 计算养分后填入（能量/蛋白/钾/磷/钠），M3 仅做存储与聚合，
    不反向调用 M5，保证各包自包含。
    """
    caller = get_caller()
    if caller not in _WRITE_ALLOWED_CALLERS:
        return {
            "ok": False,
            "error": "FORBIDDEN",
            "detail": f"caller={caller} not allowed for upsert_food_diary (MX-3: parent_assistant|child_companion)",
        }
    if not entries:
        return {"ok": False, "error": "INVALID_INPUT", "detail": "entries 不能为空"}

    store = _load_store()
    existing = store.get("entries", [])
    stamped = []
    for e in entries:
        stamped.append({
            "patient_id": patient_id,
            "date": e.get("date", datetime.now().strftime("%Y-%m-%d")),
            "meal": e.get("meal", ""),
            "food": e.get("food", ""),
            "energy_kcal": float(e.get("energy_kcal", 0.0)),
            "protein_g": float(e.get("protein_g", 0.0)),
            "potassium_mg": float(e.get("potassium_mg", 0.0)),
            "phosphorus_mg": float(e.get("phosphorus_mg", 0.0)),
            "sodium_mg": float(e.get("sodium_mg", 0.0)),
        })
    all_entries = existing + stamped

    if write_mode:
        store["entries"] = all_entries
        _save_store(store)

    agg = _aggregate(all_entries)
    return {
        "ok": True,
        "data": {
            "patient_id": patient_id,
            "stored_count": len(stamped),
            "write_mode": write_mode,
            "day_count": agg["day_count"],
            "entry_count": agg["entry_count"],
            "diet_diary_3d": {k: _round(v, 1) for k, v in agg["diet_diary_3d"].items()},
            "note": "聚合最近 3 个不重复日期；diet_diary_3d 已对齐 PCP nutrition_assessment 形状。",
        },
    }


def get_food_diary_summary(patient_id: str) -> dict[str, Any]:
    """读取并聚合某患者的饮食日记（只读，所有 caller 可读）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    store = _load_store()
    entries = [e for e in store.get("entries", []) if e.get("patient_id") == patient_id]
    if not entries:
        return {
            "ok": True,
            "data": {
                "patient_id": patient_id,
                "day_count": 0,
                "entry_count": 0,
                "diet_diary_3d": None,
                "note": "暂无该患者的饮食日记记录。",
            },
        }
    agg = _aggregate(entries)
    return {
        "ok": True,
        "data": {
            "patient_id": patient_id,
            "day_count": agg["day_count"],
            "entry_count": agg["entry_count"],
            "diet_diary_3d": {k: _round(v, 1) for k, v in agg["diet_diary_3d"].items()},
            "recent_entries": entries[-10:],
        },
    }


# ---------------------------------------------------------------------------
# 中国卫健委行业标准：儿童生长 Z 评分（身高/体重/BMI 年龄别）
# 标准：WS/T 423-2022（7岁以下，Appendix B SD 值）+ WS/T 612-2018（7-18 身高，Appendix A）
# 注：不采用 WHO 2007 LMS —— 用户判定 WHO 已 20 年未更新、偏旧，改用中国官方标准。
# 数据：data/growth_ref_cn.json（<84月按整月，7-18岁按整岁；m=中位数, s=标准差）。
# ---------------------------------------------------------------------------
_GROWTH_REF_PATH = os.path.join(os.path.dirname(__file__), "data", "growth_ref_cn.json")
_GROWTH_REF = None


def _load_growth_ref() -> dict:
    global _GROWTH_REF
    if _GROWTH_REF is None:
        with open(_GROWTH_REF_PATH, "r", encoding="utf-8") as f:
            _GROWTH_REF = json.load(f)
    return _GROWTH_REF


def _interp_sd(table: list, age_key: float):
    """table: 按 age_key 升序的 [age_key, m, s]；线性插值返回 (m, s)。越界则取端点。"""
    if not table:
        return None, None
    if age_key <= table[0][0]:
        return table[0][1], table[0][2]
    if age_key >= table[-1][0]:
        return table[-1][1], table[-1][2]
    for i in range(len(table) - 1):
        a0, m0, s0 = table[i]
        a1, m1, s1 = table[i + 1]
        if a0 <= age_key <= a1:
            if a1 == a0:
                return m1, s1
            t = (age_key - a0) / (a1 - a0)
            return m0 + (m1 - m0) * t, s0 + (s1 - s0) * t
    return table[-1][1], table[-1][2]


def _height_table(sex: str) -> list:
    """合并 height_under7(月) 与 height_7_18(岁→月)，返回按月升序的 [age_months, m, s]。"""
    ref = _load_growth_ref()
    merged = [list(r) for r in ref["height_under7"][sex]]
    for age_years, m, s in ref["height_7_18"][sex]:
        merged.append([age_years * 12, m, s])
    merged.sort(key=lambda r: r[0])
    return merged


def _grade_5(z: float) -> str:
    """生长水平 5 等级（标准差法：表2 / WS/T 612 表3.3 同口径）。"""
    if z < -2:
        return "下"
    if z < -1:
        return "中下"
    if z <= 1:
        return "中"
    if z <= 2:
        return "中上"
    return "上"


def _haz_nutrition(z: float) -> str:
    """身高别年龄 营养状况（生长迟缓）。WS/T 423 表3 年龄别身长/身高行。"""
    if z < -3:
        return "重度生长迟缓"
    if z < -2:
        return "生长迟缓"
    return "正常"


def _waz_nutrition(z: float) -> str:
    """体重别年龄 营养状况（低体重）。WS/T 423 表3 年龄别体重行。"""
    if z < -3:
        return "重度低体重"
    if z < -2:
        return "低体重"
    return "正常"


def _baz_nutrition(z: float) -> str:
    """BMI 别年龄 营养状况。WS/T 423 表3 年龄别BMI行。"""
    if z >= 3:
        return "重度肥胖"
    if z >= 2:
        return "肥胖"
    if z >= 1:
        return "超重"
    if z < -3:
        return "重度消瘦"
    if z < -2:
        return "消瘦"
    return "正常"


def calc_growth_zscore(age_years: float, sex: str,
                       height_cm: float | None = None,
                       weight_kg: float | None = None,
                       bmi: float | None = None) -> dict[str, Any]:
    """按中国卫健委行业标准计算儿童生长 Z 评分（HAZ / WAZ / BAZ）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。

    标准：WS/T 423-2022（7岁以下 体重/身高/BMI 年龄别 SD 值）+ WS/T 612-2018（7-18 身高年龄别）。
    不使用 WHO 2007（用户判定偏旧）。Z = (实测 - 中位数) / SD，SD 由标准附录给出。

    - HAZ（身高别年龄）：0-18 岁全覆盖（<84月用 WS/T 423，≥7岁用 WS/T 612）。
    - WAZ（体重别年龄）/ BAZ（BMI别年龄）：仅 7 岁以下（WS/T 423 随附；7-18 体重/BMI 标准未提供，跳过）。
    - 返回各指标 z、5 等级、营养状况分类，并给出 PRNT `growth_status` 建议（failure/overweight/normal）。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    _require(age_years, "age_years")
    sex = sex if sex in ("M", "F") else "M"
    ref = _load_growth_ref()
    age_months = age_years * 12.0
    results: dict[str, Any] = {"ok": True, "data": {}}
    d = results["data"]
    d["age_years"] = _round(age_years, 3)
    d["age_months"] = _round(age_months, 1)
    d["sex"] = sex
    d["standards"] = ref["meta"]["standards"]
    warnings: list[str] = []

    # HAZ（身高别年龄）—— 合并 0-18
    if height_cm is not None:
        m, s = _interp_sd(_height_table(sex), age_months)
        if m is None or s in (None, 0):
            warnings.append("身高参考数据缺失，无法计算 HAZ。")
        else:
            haz = (height_cm - m) / s
            d["haz"] = {
                "z": _round(haz, 2),
                "median_cm": _round(m, 1),
                "sd_cm": _round(s, 2),
                "height_cm": _round(height_cm, 1),
                "grade": _grade_5(haz),
                "nutrition": _haz_nutrition(haz),
            }
    else:
        warnings.append("未提供 height_cm，跳过 HAZ。")

    # WAZ / BAZ（仅 <84 月，WS/T 423）
    if age_months < 84:
        if weight_kg is not None:
            m, s = _interp_sd(ref["weight"][sex], age_months)
            if m is None or s in (None, 0):
                warnings.append("体重参考数据缺失，无法计算 WAZ。")
            else:
                waz = (weight_kg - m) / s
                d["waz"] = {
                    "z": _round(waz, 2),
                    "median_kg": _round(m, 2),
                    "sd_kg": _round(s, 2),
                    "weight_kg": _round(weight_kg, 1),
                    "grade": _grade_5(waz),
                    "nutrition": _waz_nutrition(waz),
                }
        else:
            warnings.append("未提供 weight_kg，跳过 WAZ。")
        if bmi is None and height_cm and weight_kg:
            h_m = height_cm / 100.0
            bmi = weight_kg / (h_m * h_m)
        if bmi is not None:
            m, s = _interp_sd(ref["bmi"][sex], age_months)
            if m is None or s in (None, 0):
                warnings.append("BMI 参考数据缺失，无法计算 BAZ。")
            else:
                baz = (bmi - m) / s
                d["baz"] = {
                    "z": _round(baz, 2),
                    "median": _round(m, 2),
                    "sd": _round(s, 2),
                    "bmi": _round(bmi, 1),
                    "grade": _grade_5(baz),
                    "nutrition": _baz_nutrition(baz),
                }
        else:
            warnings.append("未提供 bmi/身高体重，跳过 BAZ。")
    else:
        warnings.append(
            "WAZ/BAZ 仅 7 岁以下可用（WS/T 423 随附）；7-18 体重/BMI 标准未提供，跳过。")

    # PRNT growth_status 建议（供 calc_prnt_targets 输入）
    haz_z = d.get("haz", {}).get("z")
    baz_z = d.get("baz", {}).get("z")
    if haz_z is not None and haz_z < -2:
        growth_status = "failure"          # 生长迟缓 → 能量取 SDI 上限
    elif baz_z is not None and baz_z >= 1:
        growth_status = "overweight"        # 超重/肥胖 → 能量向下调整
    else:
        growth_status = "normal"
    d["growth_status_suggestion"] = growth_status
    d["warnings"] = warnings
    return results


# ---------------------------------------------------------------------------
# PEW 历史存储（ADR-007：PEW 历史归属 M3）
# ---------------------------------------------------------------------------
# 依据 ADR-007：PEW 是 M3 assess_pew_risk 的产出，其历史时间线由 M3 拥有并落库；
# M4 仅作为聚合 facade 从 M3 读取（零跨包 import，M4 不自己存 PEW）。
# 存储为 data/pew_history_store.json，按 patient_id 追加历史点。
PEW_STORE = _state_path("pew_history_store.json")
_PEW_LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}


def _load_pew_store() -> dict:
    try:
        with open(PEW_STORE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_pew_store(store: dict) -> None:
    # OD-014（P2-3）：原子写，避免半写截断静默丢数据
    atomic_write_json(PEW_STORE, store)


def record_pew_risk(patient_id: str, date: str, score: float, level: str) -> dict[str, Any]:
    """按 ADR-007，PEW 历史由 M3 拥有并落库。

    每次 assess_pew_risk 评估后，由编排层（router/PCP）调用本函数持久化一个历史点。
    :param patient_id: 患者标识（与 PCP 一致，^P[0-9]{4,}$）
    :param date: 评估日期 YYYY-MM-DD
    :param score: PEW 数值分（来自 assess_pew_risk 返回的 score 字段）
    :param level: PEW 风险等级 low / medium / high
    :param caller: 调用方角色（审计用）；缺省取部署注入的 A207_CALLER（P0-1）
    :return: 落库后该患者的完整历史点列表
    """
    caller = get_caller()
    if level not in _PEW_LEVEL_ORDER:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "level 必须是 low / medium / high"}
    store = _load_pew_store()
    pts = store.get(patient_id, [])
    pts.append({
        "date": date,
        "score": score,
        "level": level,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "caller": caller,
    })
    # 同日只保留最新一次（覆盖更新）
    by_date: dict = {}
    for p in pts:
        by_date[p["date"]] = p
    ordered = [by_date[k] for k in sorted(by_date.keys())]
    store[patient_id] = ordered
    _save_pew_store(store)
    return {"ok": True, "patient_id": patient_id, "points": ordered}


def get_pew_history(patient_id: str) -> dict[str, Any]:
    """读取某患者的 PEW 历史（ADR-007：存储归属 M3）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    :param patient_id: 患者标识
    :return: 按日期升序的历史点 + 趋势判断（improving / worsening / stable / no_data）
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    store = _load_pew_store()
    pts = store.get(patient_id, [])
    trend = "no_data"
    if len(pts) >= 2:
        first, last = pts[0], pts[-1]
        fo, lo = _PEW_LEVEL_ORDER.get(first["level"], 0), _PEW_LEVEL_ORDER.get(last["level"], 0)
        if lo > fo:
            trend = "worsening"
        elif lo < fo:
            trend = "improving"
        else:
            trend = "stable"
    return {
        "ok": True,
        "patient_id": patient_id,
        "count": len(pts),
        "points": pts,
        "trend": trend,
    }


