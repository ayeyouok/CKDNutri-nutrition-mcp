"""腹透葡萄糖吸收的能量估算（与 PRNT 2020 相关的目标计算已移至 M3）。

本模块不知道患者是谁：透析液糖量、留腹时长、交换次数、转运类型由调用方显式传入。
"""
from __future__ import annotations

from typing import Any

from a207_policy import enforce_read, get_caller

from .constants import (
    GLUCOSE_KCAL_PER_G,
    GUIDELINE,
    GUIDELINE_REF,
    MCP_NAME,
    PD_ABSORB_ANCHORS,
    PD_GLUCOSE_KCAL_PER_KG_REF,
    PD_TRANSPORT_FACTOR,
)


def _absorption_fraction(dwell_hours: float) -> float:
    anchors = PD_ABSORB_ANCHORS
    if dwell_hours <= anchors[0][0]:
        return anchors[0][1]
    if dwell_hours >= anchors[-1][0]:
        return anchors[-1][1]
    for index in range(len(anchors) - 1):
        lo_h, lo_f = anchors[index]
        hi_h, hi_f = anchors[index + 1]
        if lo_h <= dwell_hours <= hi_h:
            # 💭（2026-08-12 五包审查）：去掉重复解包（lo/hi 与 lo_h/hi_h 同值）
            if hi_h == lo_h:
                return lo_f
            ratio = (dwell_hours - lo_h) / (hi_h - lo_h)
            return lo_f + (hi_f - lo_f) * ratio
    return anchors[-1][1]


def calc_pd_glucose_absorption(dialysate_glucose_g: float, dwell_hours: float,
                               exchanges_per_day: int = 1,
                               transport_type: str = "average",
                               weight_kg: float | None = None) -> dict[str, Any]:
    """腹透葡萄糖倒灌：估算吸收克数与额外能量（须从膳食能量中扣减）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。

    P0-7 修复（2026-08-13）：NaN/Inf 显式拒绝——此前 `dialysate_glucose_g < 0`
    对 NaN 恒 False，NaN 会静默落到"80% 吸收率假设"，产出自相矛盾的估算。
    """
    import math

    get_caller()  # P0-1 身份校验副作用（未注入 A207_CALLER 抛 CallerUnknown）；本函数返回值未用
    enforce_read(MCP_NAME)
    for _name, _val in (("dialysate_glucose_g", dialysate_glucose_g),
                        ("dwell_hours", dwell_hours),
                        ("exchanges_per_day", exchanges_per_day)):
        if isinstance(_val, (int, float)) and not isinstance(_val, bool) \
                and (math.isnan(_val) or math.isinf(_val)):
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": f"{_name} 必须为有效的有限数值，收到 {_val!r}"}
    # BUG-04（2026-08-23）：weight_kg 此前未纳入强类型校验，传入 str("20")/bool 会在
    # 下方 `if weight_kg and weight_kg > 0` 抛未捕获 TypeError（500 崩溃），NaN 亦应
    # 显式拒绝。现与 Fail-Closed 契约对齐：非数值/布尔/非有限/非正数一律 INVALID_INPUT。
    if weight_kg is not None:
        if isinstance(weight_kg, bool) or not isinstance(weight_kg, (int, float)) \
                or math.isnan(weight_kg) or math.isinf(weight_kg) or weight_kg <= 0:
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": f"weight_kg 必须为正数，收到 {weight_kg!r}"}
    # BUG-TYPE-1（2026-08-23 审查）：dialysate_glucose_g / dwell_hours 此前仅在"已是
    # 数值且非 bool"时才查 NaN/Inf，str 或 bool 会绕过并流入下方比较——`'50' < 0`
    # 抛未捕获 TypeError（500 崩溃，违背 Fail-Closed），bool 被 float() 静默转 1.0
    # （布尔注入临床误算）。统一白名单：非 None、非 bool、是 int/float、有限，否则拒绝。
    for _name, _val in (("dialysate_glucose_g", dialysate_glucose_g),
                        ("dwell_hours", dwell_hours)):
        if _val is None or isinstance(_val, bool) or not isinstance(_val, (int, float)) \
                or math.isnan(_val) or math.isinf(_val):
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": f"{_name} 必须为有效数值，收到 {_val!r}"}
    if dialysate_glucose_g < 0:
        return {"ok": False, "error": "INVALID_INPUT", "detail": "dialysate_glucose_g 不能为负"}
    if dwell_hours <= 0:
        return {"ok": False, "error": "INVALID_INPUT", "detail": "dwell_hours 必须为正数"}

    transport = str(transport_type or "average").strip().lower()
    factor = PD_TRANSPORT_FACTOR.get(transport)
    if factor is None:
        # 审查（2026-08-19，BUG-4）：未知 transport_type **fail-closed**——此前回退
        # average(1.0) 仍产生临床数值（错误输入被静默接受并给出看似合法的结果）。
        # 营养计算（尤其 PD 葡萄糖吸收）不得对非法输入产出数值，显式 INVALID_INPUT。
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"未知 transport_type={transport!r}，合法值：{'/'.join(PD_TRANSPORT_FACTOR)}"}
    warnings: list[str] = []
    fraction = min(max(_absorption_fraction(float(dwell_hours)) * factor, 0.20), 0.90)
    # 审查（2026-08-19，BUG-3）：exchanges_per_day 是"次数"，必须正整数——
    # 旧 `int(exchanges_per_day or 1)` 把 1.9 → 1（静默截断，真实输入被算错）。
    # bool 是 int 子类须排除；float 非整数值显式拒绝（不静默截断）。
    if isinstance(exchanges_per_day, bool) or not isinstance(exchanges_per_day, int) \
            or exchanges_per_day <= 0:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"exchanges_per_day 必须为正整数，收到：{exchanges_per_day!r}"}
    exchanges = exchanges_per_day

    absorbed_per_exchange = float(dialysate_glucose_g) * fraction
    absorbed_total = absorbed_per_exchange * exchanges
    kcal_total = absorbed_total * GLUCOSE_KCAL_PER_G

    per_kg = None
    if weight_kg and weight_kg > 0:
        per_kg = round(kcal_total / weight_kg, 2)
        lo, hi = PD_GLUCOSE_KCAL_PER_KG_REF
        if per_kg > hi * 1.3:
            warnings.append(f"估算吸收 {per_kg} kcal/kg/d 明显高于 PRNT 参考 {lo}-{hi} kcal/kg/d，"
                            f"提示糖浓度或留腹时间偏高，建议与肾科评估处方。")
        elif per_kg < lo * 0.6:
            warnings.append(f"估算吸收 {per_kg} kcal/kg/d 低于 PRNT 参考 {lo}-{hi} kcal/kg/d，"
                            f"请核对交换次数与每袋糖量是否填全。")
    if transport in ("high", "high_average"):
        warnings.append("高转运型腹膜葡萄糖吸收更快，长留腹时能量倒灌更明显，宜缩短留腹时间。")

    return {
        "ok": True,
        "data": {
            "input": {"dialysate_glucose_g_per_exchange": float(dialysate_glucose_g),
                      "dwell_hours": float(dwell_hours), "exchanges_per_day": exchanges,
                      "transport_type": transport, "weight_kg": weight_kg},
            "absorption_fraction": round(fraction, 3),
            "absorbed_glucose_g_per_day": round(absorbed_total, 1),
            "absorbed_energy_kcal_per_day": round(kcal_total, 1),
            "absorbed_energy_kcal_per_kg": per_kg,
            "reference_kcal_per_kg": list(PD_GLUCOSE_KCAL_PER_KG_REF),
            "action": "该能量属于非膳食来源，须从每日总能量目标中扣减后再安排膳食。",
            "method": f"留腹时长插值吸收率 × 转运型系数 {factor}；葡萄糖 {GLUCOSE_KCAL_PER_G} kcal/g",
            "warnings": warnings,
            "guideline": GUIDELINE,
            "reference": GUIDELINE_REF,
        },
    }
