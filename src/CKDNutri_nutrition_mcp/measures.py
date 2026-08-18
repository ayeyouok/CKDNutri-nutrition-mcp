# -*- coding: utf-8 -*-
"""家庭量具与克重的双向换算。

设计目标：家长看到的是"半碗饭""掌心一块肉"，医护看到的是克重与营养素。
本模块把两种表达打通，并保证换算依据可解释（返回 basis 字段）。
"""
from __future__ import annotations

import re
from typing import Any

# 量词 -> 通用参考克重（当食物自带量具与查询量词不一致时兜底）
GENERIC_UNIT_GRAMS = {
    "大碗": 250.0, "小碗": 100.0, "碗": 150.0, "杯": 200.0,
    "汤勺": 15.0, "瓷勺": 10.0, "小勺": 5.0, "茶勺": 5.0, "勺": 10.0,
    "小把": 20.0, "把": 20.0, "捧": 100.0, "掌心": 100.0,
    "片": 30.0, "块": 100.0, "个": 100.0, "根": 100.0, "段": 100.0,
    "颗": 15.0, "只": 15.0, "朵": 5.0, "串": 100.0, "棵": 150.0,
    "瓣": 50.0, "盒": 150.0, "袋": 100.0, "份": 100.0,
    "斤": 500.0, "两": 50.0,  # P1（2026-08-18）：市制重量单位（1 斤=500g、1 两=50g）
}
_UNIT_ORDER = sorted(GENERIC_UNIT_GRAMS, key=len, reverse=True)

CN_NUMBER = {"零": 0.0, "一": 1.0, "两": 2.0, "二": 2.0, "三": 3.0, "四": 4.0,
             "五": 5.0, "六": 6.0, "七": 7.0, "八": 8.0, "九": 9.0, "十": 10.0}
CN_FRACTION = {"半": 0.5, "小半": 0.3, "大半": 0.7, "多半": 0.7,
               "四分之一": 0.25, "三分之一": 0.33, "四分之三": 0.75,
               "一半": 0.5}  # P1（2026-08-18）："一半碗"=0.5 碗（此前被解析成 1+0.5=1.5 碗，3× 高估）

# 家长语言锚点：用于把营养素数字翻成生活化表达
PROTEIN_ANCHORS = (("个鸡蛋", 7.0), ("块掌心瘦肉(约100g)", 20.0), ("杯牛奶(200mL)", 6.0))
ENERGY_ANCHORS = (("小瓷勺油", 81.0), ("平碗米饭(150g)", 174.0), ("个鸡蛋", 70.0))

_GRAM_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(kg|千克|公斤|g|克|ml|毫升|mL|cc|l|L)$", re.IGNORECASE)
_NUM_RE = re.compile(r"^(\d+(?:\.\d+)?)")


def _parse_quantity(text: str) -> tuple[float, str]:
    """从量具串首部剥离数量，返回 (数量, 剩余量词串)。"""
    for word, value in sorted(CN_FRACTION.items(), key=lambda kv: -len(kv[0])):
        if text.startswith(word):
            return value, text[len(word):]
    match = _NUM_RE.match(text)
    if match:
        return float(match.group(1)), text[match.end():]
    if text and text[0] in CN_NUMBER:
        head = CN_NUMBER[text[0]]
        rest = text[1:]
        if rest.startswith("点") and len(rest) > 1 and rest[1] in CN_NUMBER:
            return head + CN_NUMBER[rest[1]] / 10.0, rest[2:]
        if rest.startswith("半"):
            return head + 0.5, rest[1:]
        return head, rest
    return 1.0, text


def _find_unit(text: str) -> str | None:
    for unit in _UNIT_ORDER:
        if unit in text:
            return unit
    return None


def parse_portion(portion: str | None, row: dict[str, Any]) -> dict[str, Any]:
    """解析份量表达 -> 克重。支持 '半碗' '1个' '两小把' '150g' '一份'。"""
    unit_grams = row.get("unit_grams") or 100.0
    unit_name = row.get("unit_name") or "份"
    if portion is None or not str(portion).strip():
        return {"grams": float(unit_grams), "resolved": True,
                "basis": f"未指定份量，按该食物 1 {unit_name}（{row.get('unit_desc') or ''}）计"}
    text = str(portion).strip().replace(" ", "")
    for token in (row.get("name", ""), *row.get("aliases", [])):
        if token and text.endswith(token) and text != token:
            text = text[: -len(token)]
    text = text or "1份"

    match = _GRAM_RE.match(text)
    if match:
        value = float(match.group(1))
        unit = match.group(2).lower()
        if unit in ("kg", "千克", "公斤"):
            grams = value * 1000.0
            basis = f"按输入重量 {grams:.0f} g 计"
        elif unit in ("ml", "毫升", "cc"):
            # BUG-63（2026-08-12）：体积按水密度 1 g/ml 折算并显式标注——油≈0.92、
            # 奶≈1.03、蜂蜜≈1.42 等密度≠1 的食物会引入误差（油 100ml 高估约 8% 能量，
            # 对限能量患儿属偏保守方向），特殊食物请按实际密度换算克重。
            grams = value
            basis = (f"按体积 {value:.0f} ml 以水密度折算 {grams:.0f} g"
                     f"（密度≈1 g/ml；油/奶/蜂蜜等密度≠1，建议手动换算克重）")
        elif unit == "l":
            # P1（2026-08-18）：升（L）支持——此前 "1L" 未识别静默回落 100g。
            grams = value * 1000.0
            basis = (f"按体积 {value:.0f} L 以水密度折算 {grams:.0f} g"
                     f"（密度≈1 g/ml）")
        else:
            grams = value
            basis = f"按输入重量 {grams:.0f} g 计"
        return {"grams": grams, "resolved": True, "basis": basis}

    quantity, rest = _parse_quantity(text)
    unit = _find_unit(rest) or _find_unit(text)
    if unit is None:
        if "份" in text or not rest:
            grams = quantity * unit_grams
            return {"grams": grams, "resolved": True,
                    "basis": f"{_fmt_qty(quantity)} {unit_name} × {unit_grams:.0f} g/{unit_name}"}
        return {"grams": float(unit_grams), "resolved": False,
                "basis": f"无法解析份量「{portion}」，已回落为 1 {unit_name}"
                         f"（{unit_grams:.0f} g），请改用「半碗」「1个」或「120g」"}

    base = unit_grams if unit == unit_name else GENERIC_UNIT_GRAMS[unit]
    grams = quantity * base
    source = "该食物量具表" if unit == unit_name else "通用量具参考表"
    return {"grams": grams, "resolved": True,
            "basis": f"{_fmt_qty(quantity)} {unit} × {base:.0f} g/{unit}（{source}）"}


def _fmt_qty(value: float) -> str:
    if abs(value - 0.5) < 1e-6:
        return "半"
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value))}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _qty_phrase(count: float, unit: str) -> str:
    if abs(count - 0.5) < 0.06:
        return f"半{unit}"
    if abs(count - 0.25) < 0.05:
        return f"四分之一{unit}"
    if abs(count - 0.75) < 0.06:
        return f"大半{unit}"
    if abs(count - round(count)) < 0.08 and count >= 1:
        return f"{int(round(count))}{unit}"
    return f"约 {count:.1f}{unit}"


def to_household(row: dict[str, Any], grams: float) -> dict[str, Any]:
    """克重 -> 家庭量具表达（主表达 + 通用量具备选）。"""
    unit_grams = row.get("unit_grams") or 100.0
    unit_name = row.get("unit_name") or "份"
    count = grams / unit_grams if unit_grams else 0.0
    alternatives = []
    for unit in ("碗", "杯", "勺", "个", "片", "把"):
        if unit == unit_name:
            continue
        ref = GENERIC_UNIT_GRAMS[unit]
        if 0.2 <= grams / ref <= 6.0:
            alternatives.append({"unit": unit, "count": round(grams / ref, 2),
                                 "phrase": _qty_phrase(grams / ref, unit),
                                 "grams_per_unit": ref})
    return {
        "grams": round(grams, 1),
        "primary": {"unit": unit_name, "count": round(count, 2),
                    "phrase": f"{_qty_phrase(count, unit_name)}{row['name']}",
                    "grams_per_unit": unit_grams,
                    "unit_desc": row.get("unit_desc", "")},
        "alternatives": alternatives[:3],
    }


def nutrient_anchors(protein_g: float, energy_kcal: float) -> list[str]:
    """把蛋白与能量翻译成家长熟悉的锚点表达。"""
    lines = []
    if protein_g > 0:
        parts = [f"{protein_g / grams:.1f} {label}" for label, grams in PROTEIN_ANCHORS]
        lines.append(f"蛋白 {protein_g:.1f} g ≈ " + " / ".join(parts))
    if energy_kcal > 0:
        parts = [f"{energy_kcal / kcal:.1f} {label}" for label, kcal in ENERGY_ANCHORS]
        lines.append(f"能量 {energy_kcal:.0f} kcal ≈ " + " / ".join(parts))
    return lines
