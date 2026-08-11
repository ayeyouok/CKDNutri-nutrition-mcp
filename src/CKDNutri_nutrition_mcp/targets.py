# -*- coding: utf-8 -*-
"""腹透葡萄糖吸收的能量估算（与 PRNT 2020 相关的目标计算已移至 M3）。

本模块不知道患者是谁：透析液糖量、留腹时长、交换次数、转运类型由调用方显式传入。
"""
from __future__ import annotations

from typing import Any

from ._policy import enforce_read, get_caller

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
            lo, hi = anchors[index], anchors[index + 1]
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
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    if dialysate_glucose_g is None or dialysate_glucose_g < 0:
        return {"ok": False, "error": "INVALID_INPUT", "detail": "dialysate_glucose_g 不能为负"}
    if dwell_hours is None or dwell_hours <= 0:
        return {"ok": False, "error": "INVALID_INPUT", "detail": "dwell_hours 必须为正数"}

    transport = str(transport_type or "average").strip().lower()
    factor = PD_TRANSPORT_FACTOR.get(transport, 1.0)
    fraction = min(max(_absorption_fraction(float(dwell_hours)) * factor, 0.20), 0.90)
    exchanges = max(int(exchanges_per_day or 1), 1)

    absorbed_per_exchange = float(dialysate_glucose_g) * fraction
    absorbed_total = absorbed_per_exchange * exchanges
    kcal_total = absorbed_total * GLUCOSE_KCAL_PER_G

    warnings: list[str] = []
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
