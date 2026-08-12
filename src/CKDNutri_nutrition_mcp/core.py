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
import threading
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from a207_policy import (
    atomic_write_json,
    enforce_nutrition_tool,
    get_caller,
    resolve_state_path,
    verify_guardian_token,
)
from .constants import DIALYSIS_ALIAS

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
GUIDELINE = "PRNT 2020 (Shaw et al., Pediatr Nephrol 35:519-531)"
MCP_NAME = "CKDNutri-nutrition-mcp"

# P1-3：运行时写库不落安装目录。A207_NUTRITION_ASSESSMENT_DATA_DIR 为开发/测试 override。
_DATA_DIR_ENV = "A207_NUTRITION_ASSESSMENT_DATA_DIR"

# BUG-53（2026-08-12）：日记/PEW 存储 read-modify-write 并发保护（与 P3 care 同口径）
_STORE_LOCK = threading.Lock()


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


# BUG-18：DIARY_STORE/PEW_STORE 不再在模块加载时固化路径（env 变化需重启才能生效），
# 改为每次读写时解析，测试/部署中切换 A207_DATA_DIR 立即生效。
DIARY_STORE_FILENAME = "diary_store.json"
PEW_STORE_FILENAME = "pew_history_store.json"


def _diary_store_path() -> str:
    return _state_path(DIARY_STORE_FILENAME)


def _pew_store_path() -> str:
    return _state_path(PEW_STORE_FILENAME)

# 写权判定经 enforce_nutrition_tool 工具级 ACL（P1-1：单一事实源在 a207_policy），
# 本包不再维护本地写白名单（2026-08-12 双轨制清理）。

# ---------------------------------------------------------------------------
# 家长-患儿绑定核验（与 P1 his.py 共享 guardian_tokens.json 状态库）
# 需求：家长受限视图必须经监护人令牌绑定核验（同 HIS get_labs 的 _guard_guardian）。
# 2026-08-12 修复：此前 M3 饮食日记读写均无绑定校验，家长可跨患者读写任意患儿日记。
# 令牌由 P1 his.issue_guardian_token（仅 doctor）签发并持久化到
# resolve_state_path("guardian_tokens.json") —— 本包经同一路径读取，零跨包 import。
# BUG-30/36（2026-08-12）：令牌校验统一收敛到 a207_policy.verify_guardian_token
# （含 expires_at 过期校验 + 恒定时间比对 + 旧令牌兼容），删除本地副本——
# 此前本地 _token_matches 无过期校验，令牌轮换后旧令牌在本包仍有效。
# ---------------------------------------------------------------------------


def _guard_guardian(caller: str, patient_id: str, guardian_token: str | None,
                    tool: str) -> dict[str, Any] | None:
    """家长每次读写日记前必须通过绑定核验，缺 token 即拒绝，不给降级视图。

    校验走 a207_policy.verify_guardian_token（统一实现，含过期校验，BUG-30/36）。
    """
    if caller != "parent_assistant":
        return None
    if not guardian_token:
        return {"ok": False, "error": "GUARDIAN_UNVERIFIED",
                "detail": f"caller=parent_assistant 调用 {tool} 必须携带 guardian_token"}
    if not verify_guardian_token(patient_id, guardian_token):
        return {"ok": False, "error": "FORBIDDEN",
                "detail": f"guardian_token 与 patient_id={patient_id} 不匹配或已过期"}
    return None

# 素食蛋白倍数：蛋奶素 1.2 / 纯素 1.3（植物蛋白生物利用度低）
# S2（2026-08-12 五包审查）：补 "lacto_ovo" 同义别名键——server 层 docstring 长期写
# "lacto_ovo" 而本表只有 "ovo_lacto"，调用方按文档传 lacto_ovo 会被旧逻辑静默降级
# mixed（蛋白需求低估 20%）。两键同值，均合法。
_VEG_MULT = {"mixed": 1.0, "ovo_lacto": 1.2, "lacto_ovo": 1.2, "vegan": 1.3}

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
    # BUG-64（2026-08-13）：婴儿段严格按 PRNT 2020（Shaw et al., Pediatr Nephrol 2020;35:519-531）
    # Table 1 SDI 逐月拆分（0/1/2/3/4/5/6-9/10-11/12 月）——原代码 [2,5) 与 [5,12) 两段过粗：
    # 2 月龄被错用 3-4 月参数（能量 82-98 vs 应 93-120，低估 ~14%），5 月龄被错用 6-9 月参数
    # （蛋白 1.10-1.30 vs 应 1.30-1.52，低估 ~15%），10-11 月龄蛋白 g/day 也偏高。
    # 列：(age_min, age_max, label, 能量M, 能量F, 蛋白(g/kg), 蛋白总量(g/day))，年龄单位=岁。
    (0.0,    0.0833, "0月",             (93, 107), (93, 107), (1.52, 2.50), (8, 12)),
    (0.0833, 0.1667, "1月",             (93, 120), (93, 120), (1.52, 1.80), (8, 12)),
    (0.1667, 0.25,   "2月",             (93, 120), (93, 120), (1.40, 1.52), (8, 12)),
    (0.25,   0.3333, "3月",             (82, 98),  (82, 98),  (1.40, 1.52), (8, 12)),
    (0.3333, 0.4167, "4月",             (82, 98),  (82, 98),  (1.30, 1.52), (9, 13)),
    (0.4167, 0.5,    "5月",             (72, 82),  (72, 82),  (1.30, 1.52), (9, 13)),
    (0.5,    0.75,   "6-9月",           (72, 82),  (72, 82),  (1.10, 1.30), (9, 14)),
    (0.75,   1.0,    "10-11月",         (72, 82),  (72, 82),  (1.10, 1.30), (9, 15)),
    (1.0,    1.5,    "12月龄",          (72, 120), (72, 120), (0.90, 1.14), (11, 14)),
    (1.5,    3.0,    "2岁",             (81, 95),  (79, 92),  (0.90, 1.05), (11, 15)),
    (3.0,    7.0,    "4-6岁",           (67, 93),  (64, 90),  (0.85, 0.95), (16, 22)),
    (7.0,    12.0,   "9-10岁",          (55, 69),  (49, 63),  (0.90, 0.95), (26, 40)),
    (12.0,   18.01,  "15-17岁",         (40, 55),  (36, 46),  (0.80, 0.90),
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


def schofield_bmr_kcal(sex: str, age_years: float, weight_kg: float,
                       height_cm: float) -> float | None:
    """Schofield 体重+身高方程，返回 kcal/d。"""
    if weight_kg <= 0 or height_cm <= 0:
        return None
    # bracket 语义（Schofield 年龄分段）：3=<3 岁、10=3-10 岁、18=>10 岁——键 (sex, 分段)
    # 中的数字是"分段标识"而非年龄/系数本身，避免维护者误读为"3 岁专属系数"。
    bracket = 3 if age_years < 3 else (10 if age_years < 10 else 18)
    coefficients = SCHOFIELD.get((sex, bracket))
    if not coefficients:
        return None
    a, b, c = coefficients
    megajoules = a * weight_kg + b * (height_cm / 100.0) + c
    return round(megajoules * 1000.0 / KJ_PER_KCAL, 1)


def ideal_body_weight_kg(age_years: float, sex: str, height_cm: float) -> float | None:
    """按 BMI 第 50 百分位反推参考体重，用于水肿时避免以水重开处方。
    BMI_P50 表仅覆盖 2–18 岁；age_years < 2 时自动夹取 2 岁基准，婴儿体成分与学龄儿童
    差异较大，推算值仅供参考。
    """
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
    # BUG-01 修复：临床组工具必须工具级 ACL 收口（仅 doctor），矩阵读权不足以表达
    # "临床判读仅临床角色"（parent 对 P2 拥有 R/W）。
    enforce_nutrition_tool(caller, "calc_prnt_targets")
    _require(age_years, "age_years")
    _require(weight_kg, "weight_kg")
    # BUG-19：sex 非法值不再静默按男性处理，显式报错（calc_prnt_targets 与 calc_growth_zscore 同口径）
    if sex not in ("M", "F"):
        raise ValueError(f"sex 必须是 'M' 或 'F'，收到：{sex!r}")
    # S2（2026-08-12 五包审查）：fail-closed 补齐——
    # ① 负年龄显式拒绝（与 calc_growth_zscore 同口径，不再静默钳位 0.0）；
    # ② weight_kg 必须 > 0（0/负值此前会静默产出 0/负能量目标，无法检出配置错误）；
    # ③ growth_status 枚举校验（此前非法值静默落 else 取 SDI 中点，掩盖配置错误）；
    # ④ vegetarian_mode 枚举校验（此前非法值静默降级 mixed——如拼错的 "ovo-lacto"
    #    会被当杂食计算，蛋白需求低估 20%）。同义别名 lacto_ovo 已在 _VEG_MULT 收录。
    if age_years < 0:
        raise ValueError("age_years 不能为负")
    if weight_kg <= 0:
        raise ValueError("weight_kg 必须 > 0")
    if growth_status not in ("normal", "failure", "overweight"):
        raise ValueError(
            f"growth_status 必须是 normal / failure / overweight 之一，收到：{growth_status!r}")
    if vegetarian_mode not in _VEG_MULT:
        raise ValueError(
            f"vegetarian_mode 必须是 mixed / ovo_lacto / vegan 之一（lacto_ovo 与 ovo_lacto "
            f"同义），收到：{vegetarian_mode!r}")
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
    # BUG-49（2026-08-12）：用 eff_weight（水肿校正后理想体重）而非原始 weight_kg——
    # 水肿患儿的"水重"会让 BMR 虚高、deviation 偏负，误触发 divergent 提示。
    schofield_bmr = schofield_bmr_kcal(sex, age_years, eff_weight, height_cm)
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
    albumin_g_L: float | None = None,
) -> dict[str, Any]:
    """对照 PRNT 目标评估 3 日饮食日记均值，给出达成率、缺口/过量、PEW 风险。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    diet 需含：avg_energy_kcal / avg_protein_g / avg_potassium_mg / avg_phosphorus_mg /
              avg_sodium_mg（与 PCP nutrition_assessment.diet_diary_3d 对齐）。
    albumin_g_L（BUG-61，2026-08-12）：血清白蛋白 g/L，参与 PEW 筛查
    （<35 记低白蛋白预警）；此前硬编码 None，白蛋白始终不参与本路径评估。
    """
    caller = get_caller()
    # BUG-01：临床判读工具级 ACL（仅 doctor）
    enforce_nutrition_tool(caller, "assess_intake_vs_target")
    _require(age_years, "age_years")
    _require(weight_kg, "weight_kg")
    if sex not in ("M", "F"):
        raise ValueError(f"sex 必须是 'M' 或 'F'，收到：{sex!r}")
    # BUG-61：diet 空值/非字典显式拒绝（此前 diet=None 会触发未捕获 TypeError）
    if not isinstance(diet, dict) or not diet:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "diet 需为字典且含 avg_energy_kcal / avg_protein_g"}
    required = ("avg_energy_kcal", "avg_protein_g")
    if not all(k in diet for k in required):
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "diet 需含 avg_energy_kcal 与 avg_protein_g"}
    # BUG-61 后补（2026-08-12）：键存在但值为 None（JSON null）时 .get(k, 0.0) 仍返回
    # None，float(None) 会抛未捕获 TypeError——统一 `or 0.0` 清洗，空值按 0 处理。
    avg_e = float(diet.get("avg_energy_kcal") or 0.0)
    avg_p = float(diet.get("avg_protein_g") or 0.0)
    avg_k = float(diet.get("avg_potassium_mg") or 0.0)
    avg_ph = float(diet.get("avg_phosphorus_mg") or 0.0)
    avg_na = float(diet.get("avg_sodium_mg") or 0.0)

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

    # PEW 风险（简化筛查，非完整诊断）；BUG-61：白蛋白透传，不再硬编码 None
    pew = _screen_pew(avg_p, avg_e, floor_p, target_e, albumin_g_L)

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
        # BUG-63（2026-08-12）：明确标注"简化筛查"——避免把摄入不足直接读成 PEW 确诊，
        # 防止对短期厌食患儿过度干预（管饲等）；确诊需结合人体测量与生化指标。
        rationale = ("蛋白质低于安全下限且能量摄入 <80% 目标，提示 PEW 高风险。"
                     "（本结果为基于摄入/白蛋白的简化筛查；确诊需结合体重丢失、"
                     "中臂肌围等人体测量与生化指标）")
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
    floor_protein_g: float | None = None,
) -> dict[str, Any]:
    """独立 PEW 风险筛查接口（供编排层直接传入已算好的均值与目标）。

    floor_protein_g（BUG-42 修复，2026-08-12）：PRNT 官方蛋白安全下限（=SDI 下限×体重），
    应传入 calc_prnt_targets 返回的 protein.floor_g_per_day。缺省时退化为
    "目标×85%"近似（婴儿段会偏高：0 月段官方下限 1.52 远低于 85% 目标 2.13，
    会把合规摄入误判为低于安全下限）——独立调用方建议显式传官方下限。
    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    # BUG-01：临床判读工具级 ACL（仅 doctor）
    enforce_nutrition_tool(caller, "assess_pew_risk")
    _require(avg_protein_g, "avg_protein_g")
    _require(avg_energy_kcal, "avg_energy_kcal")
    _require(target_protein_g, "target_protein_g")
    _require(target_energy_kcal, "target_energy_kcal")
    floor_p = floor_protein_g if floor_protein_g is not None else target_protein_g * 0.85
    pew = _screen_pew(avg_protein_g, avg_energy_kcal, floor_p, target_energy_kcal, albumin_g_L)
    return {"ok": True, "data": {"pew_risk": pew["risk"], "rationale": pew["rationale"]}}


# ---------------------------------------------------------------------------
# 3. 3 日饮食日记：写入(store) + 聚合(diet_diary_3d) + 读取
# ---------------------------------------------------------------------------
def _load_store() -> dict[str, Any]:
    # B1（2026-08-12 五包审查）：损坏/类型错误文件禁止静默返回空库——否则下次
    # upsert_food_diary 的 load→append→save 会用"仅新条目"覆盖整个日记库，历史数据
    # 永久丢失。对齐 care _load_store（BUG-65/67）：JSON 损坏或非 dict 顶层一律抛
    # RuntimeError（server._invalid 归 INTERNAL_ERROR），运维可发现并恢复备份。
    path = _diary_store_path()
    if not os.path.exists(path):
        return {"entries": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"日记库 {Path(path).name} JSON 损坏，拒绝加载（防止静默清空），"
            f"请检查磁盘/恢复备份: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(
            f"日记库 {Path(path).name} 读取失败，拒绝加载（防止静默清空）: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"日记库 {Path(path).name} 数据类型错误：期望 dict，实际为 {type(data).__name__}，"
            f"拒绝加载（防止静默清空）")
    return data


def _save_store(store: dict[str, Any]) -> None:
    # OD-014（P2-3）：原子写，避免半写截断静默丢数据
    atomic_write_json(_diary_store_path(), store)


def _aggregate(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """取最近 3 个不重复日期的条目，按**天数**求每日均值，返回 diet_diary_3d 形状。

    BUG-61（2026-08-12）：分母必须是天数而非餐次条目数——原除以 len(used) 得到的是
    "平均每餐"，患者一天记 3 餐时日均值被缩 3 倍，与日目标（target_kcal_per_day）
    对照会触发能量不足误报与 PEW 假阳性。
    """
    by_day: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        by_day.setdefault(e.get("date", ""), []).append(e)
    days = sorted(by_day.keys(), reverse=True)[:3]
    used = [e for d in days for e in by_day[d]]
    num_days = max(len(days), 1)
    avg = {
        "avg_energy_kcal": sum(e.get("energy_kcal", 0.0) for e in used) / num_days,
        "avg_protein_g": sum(e.get("protein_g", 0.0) for e in used) / num_days,
        "avg_potassium_mg": sum(e.get("potassium_mg", 0.0) for e in used) / num_days,
        "avg_phosphorus_mg": sum(e.get("phosphorus_mg", 0.0) for e in used) / num_days,
        "avg_sodium_mg": sum(e.get("sodium_mg", 0.0) for e in used) / num_days,
    }
    return {"day_count": len(days), "entry_count": len(used), "diet_diary_3d": avg}


def _normalize_date(value: Any, field: str = "date") -> str:
    """把日期串归一化为 ISO YYYY-MM-DD（BUG-60，2026-08-12）。

    接受 YYYY-MM-DD / YYYY/M/D / YYYY.M.D（容忍前后空白）；解析失败显式抛错
    （fail-closed）。此前日期作为不透明字符串直接落库，"2026-3-1" 与 "2026-03-01"
    会被 _aggregate 当作不同日期分桶，跨天统计被拆散、平均值失真。
    """
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"{field} 必须为有效日期（YYYY-MM-DD），收到：{value!r}")


def upsert_food_diary(
    patient_id: str,
    entries: list[dict[str, Any]] | None = None,
    write_mode: bool = True,
    guardian_token: str | None = None,
) -> dict[str, Any]:
    """写入/追加饮食条目（MX-3 收口：家长/医生，2026-08-12 需求对齐临床=✔）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    条目由调用方经 M5 计算养分后填入（能量/蛋白/钾/磷/钠），M3 仅做存储与聚合，
    不反向调用 M5，保证各包自包含。
    家长写入必须携带 guardian_token 完成患儿绑定核验（2026-08-12 修复：
    此前无绑定校验，家长可向任意患儿写入日记污染数据）。
    """
    caller = get_caller()
    enforce_nutrition_tool(caller, "upsert_food_diary")
    denied = _guard_guardian(caller, patient_id, guardian_token, "upsert_food_diary")
    if denied:
        return denied
    if not entries:
        return {"ok": False, "error": "INVALID_INPUT", "detail": "entries 不能为空"}

    with _STORE_LOCK:
        store = _load_store()
        existing = store.get("entries", [])
        stamped = []
        for e in entries:
            # BUG-60：写入前归一化日期（容忍 YYYY-M-D / YYYY/M/D 等变体，非法日期显式拒绝）
            raw_date = e.get("date") or datetime.now().strftime("%Y-%m-%d")
            stamped.append({
                "patient_id": patient_id,
                "date": _normalize_date(raw_date, "date"),
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

    # 仅聚合当前患者记录，防止跨患者数据泄露
    patient_entries = [e for e in all_entries if e.get("patient_id") == patient_id]
    agg = _aggregate(patient_entries)
    return {
        "ok": True,
        "data": {
            "patient_id": patient_id,
            # S5（2026-08-12 五包审查）：write_mode=False（预演）时 stored_count 归 0，
            # 并补 persisted 显式标注（对齐 clinical-data upsert_lab_result 的 persisted
            # 字段）——此前预演也返回 stored_count=len(stamped)，"存了 N 条"与实际未写矛盾。
            "stored_count": len(stamped) if write_mode else 0,
            "persisted": bool(write_mode),
            "write_mode": write_mode,
            "day_count": agg["day_count"],
            "entry_count": agg["entry_count"],
            "diet_diary_3d": {k: _round(v, 1) for k, v in agg["diet_diary_3d"].items()},
            "note": ("聚合最近 3 个不重复日期；diet_diary_3d 已对齐 PCP nutrition_assessment "
                     "形状。write_mode=False 时仅预演不落盘（stored_count=0）。"),
        },
    }


def get_food_diary_summary(patient_id: str, guardian_token: str | None = None) -> dict[str, Any]:
    """读取并聚合某患者的饮食日记（只读，家长/医生/临床角色可读）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    家长读取必须携带 guardian_token 完成患儿绑定核验（2026-08-12 修复：
    此前无绑定校验，家长可跨患者读取任意患儿日记原始条目）。
    """
    caller = get_caller()
    enforce_nutrition_tool(caller, "get_food_diary_summary")
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_food_diary_summary")
    if denied:
        return denied
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
# S3（2026-08-12 五包审查）：懒加载并发锁（double-checked locking，对齐 assessment）
_GROWTH_REF_LOCK = threading.Lock()


def _load_growth_ref() -> dict:
    global _GROWTH_REF
    if _GROWTH_REF is None:
        with _GROWTH_REF_LOCK:
            if _GROWTH_REF is None:  # S3：防多线程首调重复 I/O
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


def _valid_sd(s: Any) -> bool:
    """SD 必须为正的有限数值（Z=(X-m)/s；s<=0 或 NaN 会产生无意义/反向 Z 分）。

    BUG-59（2026-08-12）：原判 `s in (None, 0)` 仅拦零值，负数与 NaN（NaN<=0 为 False）
    仍会穿透；统一收紧为"正有限数"。
    """
    return isinstance(s, (int, float)) and s == s and s > 0


# BUG-59（2026-08-12）：身高参照表静态化缓存（数据只随 growth_ref_cn.json 变化，
# 与 _GROWTH_REF 同生命周期）——此前 calc_growth_zscore 每次评估都重新合并+排序。
_HEIGHT_TABLE_CACHE: dict[str, list] = {}


def _height_table(sex: str) -> list:
    """合并 height_under7(月) 与 height_7_18(岁→月)，返回按月升序的 [age_months, m, s]。"""
    cached = _HEIGHT_TABLE_CACHE.get(sex)
    if cached is not None:
        return cached
    ref = _load_growth_ref()
    merged = [list(r) for r in ref["height_under7"][sex]]
    for age_years, m, s in ref["height_7_18"][sex]:
        merged.append([age_years * 12, m, s])
    merged.sort(key=lambda r: r[0])
    _HEIGHT_TABLE_CACHE[sex] = merged
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
    # BUG-01：临床判读工具级 ACL（仅 doctor）
    enforce_nutrition_tool(caller, "calc_growth_zscore")
    _require(age_years, "age_years")
    # BUG-19：sex 非法值显式报错，不再静默按男性计算
    if sex not in ("M", "F"):
        raise ValueError(f"sex 必须是 'M' 或 'F'，收到：{sex!r}")
    # BUG-60：负年龄显式拒绝——此前会按 0 月边界钳位算出无临床意义的 Z 分
    if age_years < 0:
        raise ValueError("age_years 不能为负")
    # Code Smells-15（2026-08-12）：身高/体重必须为正——此前 height_cm=0 会算出
    # (0-中位数)/SD 的 -26 级荒谬 Z 分，静默返回"生长迟缓"误导临床。
    if height_cm is not None and height_cm <= 0:
        raise ValueError("height_cm 必须 > 0")
    if weight_kg is not None and weight_kg <= 0:
        raise ValueError("weight_kg 必须 > 0")
    # BUG-63（2026-08-12）：生理学合理上界——过高/过重（录入错误）会算出荒谬 Z 分
    # （如 300cm → Z≈+26 判"上"）；**不做下限收紧**（早产儿 40-45cm 合法，下限保持 >0）。
    if height_cm is not None and height_cm > 250:
        raise ValueError(f"height_cm {height_cm} 超出生理学合理范围（≤250 cm），请核查数据")
    if weight_kg is not None and weight_kg > 200:
        raise ValueError(f"weight_kg {weight_kg} 超出生理学合理范围（≤200 kg），请核查数据")
    ref = _load_growth_ref()
    age_months = age_years * 12.0
    results: dict[str, Any] = {"ok": True, "data": {}}
    d = results["data"]
    d["age_years"] = _round(age_years, 3)
    d["age_months"] = _round(age_months, 1)
    d["sex"] = sex
    d["standards"] = ref["meta"]["standards"]
    warnings: list[str] = []
    # BUG-63（2026-08-12）：原始 Z 分（未 round）用于 growth_status 临床判定——
    # d["haz"]["z"] 等是 round(…,2) 后的展示值，直接用它判定会在 -2 边界引入 ±0.005
    # 抖动（如 -2.004 round 成 -2.00 → `-2.0 < -2` 为 False 漏判生长迟缓）。
    haz_z: float | None = None
    waz_z: float | None = None
    baz_z: float | None = None

    # BUG-60：>18 岁（>216 月）超出 WS/T 423/612 适用域——不再静默按 216 月边界钳位，
    # 显式标注越界并提示结果仅供参考（负年龄已在入口拒绝）。
    if age_months > 216:
        d["age_out_of_bounds"] = True
        warnings.append(
            "age_years 超出中国卫健委标准适用上限（18 岁），Z 评分按 18 岁边界参考值计算，"
            "仅供参考，不作临床判定依据。")

    # HAZ（身高别年龄）—— 合并 0-18
    if height_cm is not None:
        m, s = _interp_sd(_height_table(sex), age_months)
        if m is None or not _valid_sd(s):
            warnings.append("身高参考数据缺失，无法计算 HAZ。")
        else:
            haz = (height_cm - m) / s
            haz_z = haz  # BUG-63：原始值用于判定
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

    # BUG-61（2026-08-12）：BMI 自动推算提前——原写在 <84 月分块内，≥7 岁（age_months>=84）
    # 且未显式传 bmi 时恒为 None，下方"BMI>24 学龄超重粗判"分支永不触发，超重漏诊并误判 normal。
    if bmi is None and height_cm and weight_kg:
        h_m = height_cm / 100.0
        bmi = weight_kg / (h_m * h_m)

    # WAZ / BAZ（仅 <84 月，WS/T 423）
    if age_months < 84:
        if weight_kg is not None:
            m, s = _interp_sd(ref["weight"][sex], age_months)
            if m is None or not _valid_sd(s):
                warnings.append("体重参考数据缺失，无法计算 WAZ。")
            else:
                waz = (weight_kg - m) / s
                waz_z = waz  # BUG-63：原始值用于判定
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
        if bmi is not None:
            m, s = _interp_sd(ref["bmi"][sex], age_months)
            if m is None or not _valid_sd(s):
                warnings.append("BMI 参考数据缺失，无法计算 BAZ。")
            else:
                baz = (bmi - m) / s
                baz_z = baz  # BUG-63：原始值用于判定
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
    # 优先级：HAZ 生长迟缓 > WAZ 低体重/消瘦 > BAZ/BMI 超重
    # BUG-63：haz_z/waz_z/baz_z 均为未 round 的原始值（grade/nutrition 一直用原始值，
    # 此前 growth_status 误用 d[*]["z"] 的 round 后值，-2 边界 ±0.005 抖动）
    if haz_z is not None and haz_z < -2:
        growth_status = "failure"          # 生长迟缓 → 能量取 SDI 上限
    elif waz_z is not None and waz_z < -2:
        growth_status = "failure"          # 低体重/消瘦 → 能量取 SDI 上限
    elif baz_z is not None and baz_z >= 1:
        growth_status = "overweight"        # BAZ≥1 → 超重/肥胖，能量向下调整
    elif baz_z is None and bmi is not None and bmi > 24:
        # ≥7 岁无 BAZ 标准时，用 BMI>24 粗判超重趋势（中国学龄儿童超重界值）
        growth_status = "overweight"
        warnings.append("≥7 岁 BAZ 标准不可用，基于 BMI>24 粗判超重，建议结合腰围/体脂综合评估。")
    elif baz_z is None and bmi is not None and age_months >= 84:
        # BUG-63（2026-08-12）：≥7 岁 BAZ 缺失且 BMI 未达超重界值——补消瘦/极低 BMI 粗筛
        # 提示，杜绝"严重消瘦被判 normal 且无警告"的漏诊。**不改 growth_status**
        # （7-8 岁 BMI 14 属正常范围，扁平阈值直接调能量会误伤；仅提示人工评估）。
        if bmi < 14:
            warnings.append(
                f"≥7 岁无 BAZ 标准，BMI {bmi:.1f} 极低（<14），提示消瘦/营养不良可能，"
                "请立即结合中臂肌围等人体测量人工评估。")
        else:
            warnings.append(
                f"≥7 岁无 BAZ 标准，BMI {bmi:.1f} 未达超重界值（≤24）；"
                "消瘦/营养不足判定请结合中臂肌围等人体测量人工评估。")
        growth_status = "normal"
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
# 存储为 <state>/pew_history_store.json，按 patient_id 追加历史点（路径延迟解析，见 BUG-18）。
_PEW_LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}


def _load_pew_store() -> dict:
    # B1（2026-08-12 五包审查）：同 _load_store——PEW 历史库损坏/类型错误禁止静默
    # 返回 {}（否则 record_pew_risk 的 RMW 会清空全部 PEW 时间线），fail-closed 抛错。
    path = _pew_store_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"PEW 历史库 {Path(path).name} JSON 损坏，拒绝加载（防止静默清空），"
            f"请检查磁盘/恢复备份: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(
            f"PEW 历史库 {Path(path).name} 读取失败，拒绝加载（防止静默清空）: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"PEW 历史库 {Path(path).name} 数据类型错误：期望 dict，实际为 {type(data).__name__}，"
            f"拒绝加载（防止静默清空）")
    return data


def _save_pew_store(store: dict) -> None:
    # OD-014（P2-3）：原子写，避免半写截断静默丢数据
    atomic_write_json(_pew_store_path(), store)


def record_pew_risk(patient_id: str, date: str, score: float, level: str) -> dict[str, Any]:
    """按 ADR-007，PEW 历史由 M3 拥有并落库。

    每次 assess_pew_risk 评估后，由编排层（router/PCP）调用本函数持久化一个历史点。
    :param patient_id: 患者标识（与 PCP 一致，^P[0-9]{4,}$）
    :param date: 评估日期 YYYY-MM-DD
    :param score: PEW 数值分（来自 assess_pew_risk 返回的 score 字段）
    :param level: PEW 风险等级 low / medium / high
    :return: 落库后该患者的完整历史点列表（身份由部署环境注入，P0-1）
    """
    caller = get_caller()
    enforce_nutrition_tool(caller, "record_pew_risk")  # 仅临床角色可落 PEW 历史
    if level not in _PEW_LEVEL_ORDER:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "level 必须是 low / medium / high"}
    # BUG-60：PEW 历史按日期排序去重，日期必须归一化，否则异形日期破坏时间线
    date = _normalize_date(date, "date")
    with _STORE_LOCK:
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
    # BUG-01 修复：PEW 历史属临床判读（NUTRITION_ASSESSMENT_CLINICAL_TOOLS 已登记），
    # 原实现用 enforce_read 导致家长（矩阵 R/W）可读 PEW 临床历史。
    enforce_nutrition_tool(caller, "get_pew_history")
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

