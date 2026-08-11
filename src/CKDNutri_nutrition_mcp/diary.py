# -*- coding: utf-8 -*-
"""饮食日记汇总与目标达成率评估。"""
from __future__ import annotations

from typing import Any

from ._policy import enforce_read, get_caller

from .constants import FOOD_TABLE_REF, GUIDELINE, MCP_NAME
from .fooddb import find_food, scale_nutrients
from .measures import parse_portion

SUM_KEYS = ("energy_kcal", "protein_g", "fat_g", "carb_g",
            "potassium_mg", "phosphorus_mg", "sodium_mg", "calcium_mg")

# 目标字段的兼容别名：既接受本包的输出，也接受 PCP 风格字段
TARGET_ALIAS = {
    "energy_kcal": ("energy_kcal_per_day", "energy_kcal", "target_kcal_per_day", "avg_energy_kcal"),
    "protein_g": ("protein_g_per_day", "protein_g", "target_g_per_day", "avg_protein_g"),
    "potassium_mg": ("potassium_mg_per_day", "potassium_mg", "k_mg_per_day", "avg_potassium_mg"),
    "phosphorus_mg": ("phosphorus_mg_per_day", "phosphorus_mg", "p_mg_per_day", "avg_phosphorus_mg"),
    "sodium_mg": ("sodium_mg_per_day", "sodium_mg", "na_mg_per_day", "avg_sodium_mg"),
}
LIMIT_KEYS = ("potassium_mg", "phosphorus_mg", "sodium_mg")
FIELD_LABEL = {"energy_kcal": "能量", "protein_g": "蛋白质", "potassium_mg": "钾",
               "phosphorus_mg": "磷", "sodium_mg": "钠"}


def _blank_totals() -> dict[str, float]:
    return {key: 0.0 for key in SUM_KEYS}


def _pick_target(target: dict[str, Any], field: str) -> float | None:
    for alias in TARGET_ALIAS[field]:
        value = target.get(alias)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def sum_diet_intake(diary: list[dict[str, Any]],
                    target: dict[str, Any] | None = None) -> dict[str, Any]:
    """汇总多日饮食日记，并对照目标给出达成率。

    diary 每项：{"food": 名称, "grams": 克重 或 "portion": 家庭量具,
                 "date": 日期(可选), "meal": 餐次(可选), "cooking": 烹调方式(可选)}
    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    if not isinstance(diary, list) or not diary:
        return {"ok": False, "error": "INVALID_INPUT", "detail": "diary 需为非空列表"}

    per_day: dict[str, dict[str, Any]] = {}
    unmatched: list[dict[str, Any]] = []
    contributions: list[dict[str, Any]] = []

    for index, entry in enumerate(diary):
        if not isinstance(entry, dict):
            unmatched.append({"index": index, "reason": "条目不是对象", "raw": str(entry)})
            continue
        name = str(entry.get("food") or entry.get("name") or "").strip()
        row = find_food(name) if name else None
        if row is None:
            unmatched.append({"index": index, "food": name,
                              "reason": "内置食物表中未匹配到该名称"})
            continue
        grams = entry.get("grams") or entry.get("weight_g")
        if isinstance(grams, (int, float)) and grams > 0:
            grams = float(grams)
            basis = f"按输入克重 {grams:.0f} g"
        else:
            resolved = parse_portion(entry.get("portion"), row)
            grams = resolved["grams"]
            basis = resolved["basis"]
        scaled = scale_nutrients(row, grams, entry.get("cooking"))

        date = str(entry.get("date") or "未标注日期")
        bucket = per_day.setdefault(date, {"date": date, "items": 0, "totals": _blank_totals()})
        bucket["items"] += 1
        for key in SUM_KEYS:
            bucket["totals"][key] += scaled[key]
        contributions.append({"food": row["name"], "date": date,
                              "meal": entry.get("meal"), "grams": scaled["grams"],
                              "basis": basis,
                              **{key: scaled[key] for key in
                                 ("energy_kcal", "protein_g", "potassium_mg", "phosphorus_mg")}})

    if not contributions:
        return {"ok": False, "error": "NO_MATCHED_ITEM",
                "detail": "日记中没有任何一项能在内置食物表中匹配，无法汇总",
                "unmatched": unmatched}

    days = sorted(per_day)
    day_rows = []
    total = _blank_totals()
    for date in days:
        bucket = per_day[date]
        day_rows.append({"date": date, "items": bucket["items"],
                         **{key: round(bucket["totals"][key], 1) for key in SUM_KEYS}})
        for key in SUM_KEYS:
            total[key] += bucket["totals"][key]

    day_count = len(days)
    average = {key: round(total[key] / day_count, 1) for key in SUM_KEYS}

    data: dict[str, Any] = {
        "days": day_count,
        "item_count": len(contributions),
        "per_day": day_rows,
        "total": {key: round(total[key], 1) for key in SUM_KEYS},
        "daily_average": average,
        "top_potassium_sources": _top(contributions, "potassium_mg"),
        "top_phosphorus_sources": _top(contributions, "phosphorus_mg"),
        "top_protein_sources": _top(contributions, "protein_g"),
        "unmatched": unmatched,
        "units": {"energy_kcal": "kcal/d", "protein_g": "g/d", "potassium_mg": "mg/d",
                  "phosphorus_mg": "mg/d", "sodium_mg": "mg/d", "calcium_mg": "mg/d"},
        "source": FOOD_TABLE_REF,
    }
    if unmatched:
        data["warnings"] = [f"有 {len(unmatched)} 条未匹配，汇总值偏低，"
                            f"请补录后重算再做临床判断。"]
    if target:
        data["achievement"] = _achievement(average, target)
        data["guideline"] = GUIDELINE
    return {"ok": True, "data": data}


def _top(items: list[dict[str, Any]], field: str, limit: int = 3) -> list[dict[str, Any]]:
    ranked = sorted(items, key=lambda item: item.get(field, 0.0), reverse=True)[:limit]
    return [{"food": item["food"], "date": item["date"], "grams": item["grams"],
             field: item[field]} for item in ranked if item.get(field, 0.0) > 0]


def _achievement(average: dict[str, float], target: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"items": [], "actions": []}
    for field in ("energy_kcal", "protein_g", *LIMIT_KEYS):
        goal = _pick_target(target, field)
        if goal is None or goal <= 0:
            continue
        actual = average[field]
        percent = actual / goal * 100.0
        if field in LIMIT_KEYS:
            verdict = "在限值内" if percent <= 100 else "超出限值"
            kind = "upper_limit"
        else:
            verdict = "达标" if 90 <= percent <= 110 else ("不足" if percent < 90 else "超出")
            kind = "target"
        result["items"].append({"field": field, "label": FIELD_LABEL[field], "kind": kind,
                                "target": round(goal, 1), "actual": actual,
                                "percent": round(percent, 1), "verdict": verdict})
        if field == "energy_kcal" and percent < 80:
            result["actions"].append(
                "经口能量摄入持续低于目标 80%：先排查呕吐/胃食管反流、代谢性酸中毒、"
                "容量过负荷与透析不充分等可逆原因，再考虑口服营养补充或管饲（PRNT 分级建议）。")
        if field == "protein_g" and percent < 90:
            result["actions"].append("蛋白摄入低于目标 90%，需优先补足优质蛋白，"
                                     "并复查白蛋白与生长速率。")
        if field == "potassium_mg" and percent > 100:
            result["actions"].append("钾摄入超限：优先削减高钾水果与薯类分量，"
                                     "并对叶菜与薯类做焯水弃汤处理。")
        if field == "phosphorus_mg" and percent > 100:
            result["actions"].append("磷摄入超限：削减加工食品与含磷添加剂饮料，"
                                     "并核对磷结合剂是否随餐服用。")
    return result
