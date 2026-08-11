# -*- coding: utf-8 -*-
"""M7 食谱生成纯函数：按 PRNT 目标生成一周/多日食谱（3 餐 + 加餐），并算达成率。

不依赖 fastmcp，可直接 import 单测。本包自带 CKD 适宜食物子集（data/foods_ckd.json），
不跨包 import M5；生产环境应将子集替换为调用 M5 lookup_food_nutrients（见 README 集成说明）。

生成策略（确定性，便于单测复现）：
- 每日：主食供 ~50% 能量、蛋白源供 ~70% 蛋白，蔬菜/水果固定 100g，油脂补足剩余能量。
- 餐次分配：主食[早0.35/午0.35/晚0.30]、蛋白[午0.5/晚0.5]、蔬果[午0.5/晚0.5]、水果[加餐1.0]、油脂[早0.3/午0.3/晚0.4]。
- 多日通过轮换食物选择产生多样性（day % len）。
- 返回每日餐次明细、每日汇总、达成率（实际/目标 %）、整体达成率。
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from ._policy import enforce_read, get_caller

MCP_NAME = "CKDNutri-nutrition-mcp"

_MEAL_NAMES = ["早餐", "午餐", "晚餐", "加餐"]
# 各品类 -> 餐次克数占比
_MEAL_SPLIT = {
    "staple": [0.35, 0.35, 0.30, 0.0],
    "protein": [0.0, 0.50, 0.50, 0.0],
    "veg": [0.0, 0.50, 0.50, 0.0],
    "fruit": [0.0, 0.0, 0.0, 1.0],
    "fat": [0.30, 0.30, 0.40, 0.0],
}

_FOODS_PATH = os.path.join(os.path.dirname(__file__), "data", "foods_ckd.json")
_FOODS: Optional[list[dict]] = None


def _load_foods() -> list[dict]:
    global _FOODS
    if _FOODS is None:
        with open(_FOODS_PATH, "r", encoding="utf-8") as fh:
            _FOODS = json.load(fh)["foods"]
    return _FOODS


def _item(food: dict, grams: int) -> dict:
    f = grams / 100.0
    return {
        "food": food["name"],
        "cat": food["cat"],
        "grams": grams,
        "energy_kcal": round(food["energy_per_100g"] * f, 1),
        "protein_g": round(food["protein_per_100g"] * f, 2),
        "potassium_mg": round(food["potassium_per_100g"] * f, 1),
        "phosphorus_mg": round(food["phosphorus_per_100g"] * f, 1),
        "sodium_mg": round(food["sodium_per_100g"] * f, 1),
    }


def _sum_items(items: list[dict]) -> dict:
    tot: dict[str, float] = {k: 0.0 for k in ("energy_kcal", "protein_g", "potassium_mg", "phosphorus_mg", "sodium_mg")}
    for it in items:
        for k in tot:
            tot[k] += it[k]
    return {k: round(v, 1) for k, v in tot.items()}


def _achievement(tot: dict, t_energy: float, t_protein: float, t_k: float, t_p: float, t_na: float) -> dict:
    def pct(actual: float, target: float) -> int:
        if target <= 0:
            return 0
        return round(actual / target * 100)
    return {
        "energy_pct": pct(tot["energy_kcal"], t_energy),
        "protein_pct": pct(tot["protein_g"], t_protein),
        "potassium_pct": pct(tot["potassium_mg"], t_k),
        "phosphorus_pct": pct(tot["phosphorus_mg"], t_p),
        "sodium_pct": pct(tot["sodium_mg"], t_na),
    }


def _split_meals(items: list[dict]) -> list[dict]:
    meals = [{ "meal": name, "items": [], "totals": None } for name in _MEAL_NAMES]
    for it in items:
        split = _MEAL_SPLIT.get(it["cat"], [0, 0, 0, 0])
        for mi, ratio in enumerate(split):
            g = round(it["grams"] * ratio)
            if g <= 0:
                continue
            f = g / 100.0
            food = next(x for x in _load_foods() if x["name"] == it["food"])
            meals[mi]["items"].append({
                "food": it["food"], "cat": food["cat"], "grams": g,
                "energy_kcal": round(food["energy_per_100g"] * f, 1),
                "protein_g": round(food["protein_per_100g"] * f, 2),
                "potassium_mg": round(food["potassium_per_100g"] * f, 1),
                "phosphorus_mg": round(food["phosphorus_per_100g"] * f, 1),
                "sodium_mg": round(food["sodium_per_100g"] * f, 1),
            })
    for m in meals:
        m["totals"] = _sum_items(m["items"])
    return meals


def _overall_achievement(days_out: list[dict], t_energy: float, t_protein: float, t_k: float,
                         t_p: float, t_na: float, days: int) -> dict:
    keys = ("energy_pct", "protein_pct", "potassium_pct", "phosphorus_pct", "sodium_pct")
    agg = {k: 0 for k in keys}
    for d in days_out:
        for k in keys:
            agg[k] += d["achievement"][k]
    return {k: round(v / days) for k, v in agg.items()}


def generate_meal_plan(
    target_energy_kcal: float,
    target_protein_g: float,
    target_k_mg: float,
    target_p_mg: float,
    target_na_mg: float,
    days: int = 7,
    vegetarian: bool = False,
    exclude_foods: Optional[list[str]] = None,
) -> dict:
    """按 PRNT 目标生成多日食谱（3 餐 + 加餐），返回餐次明细、每日汇总与达成率。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    if target_energy_kcal <= 0 or target_protein_g <= 0:
        raise ValueError("target_energy_kcal 与 target_protein_g 必须 > 0")
    if days <= 0:
        raise ValueError("days 必须 > 0")

    foods = _load_foods()
    excl = set(exclude_foods or [])

    def filt(cat: str, veg_only: bool = False) -> list[dict]:
        return [f for f in foods if f["cat"] == cat and f["name"] not in excl
                and (not veg_only or f.get("veg"))]

    staples = filt("staple")
    prots = filt("protein", veg_only=vegetarian)
    vegs = filt("veg")
    fruits = filt("fruit")
    fats = filt("fat")
    if not (staples and prots and vegs and fruits and fats):
        raise ValueError("食物库过滤后为空：请检查 exclude_foods / vegetarian 设置（可能把所有蛋白源都排除了）")

    days_out: list[dict] = []
    for d in range(days):
        staple = staples[d % len(staples)]
        prot = prots[d % len(prots)]
        veg = vegs[d % len(vegs)]
        fr = fruits[d % len(fruits)]
        fat = fats[0]

        staple_g = max(10, round(target_energy_kcal * 0.5 / staple["energy_per_100g"] * 100))
        prot_g = max(10, round(target_protein_g * 0.7 / prot["protein_per_100g"] * 100))
        veg_g = 100
        fruit_g = 100

        e_staple = staple_g * staple["energy_per_100g"] / 100
        e_prot = prot_g * prot["protein_per_100g"] * 4 / 100  # 蛋白 4 kcal/g
        e_veg = veg_g * veg["energy_per_100g"] / 100
        e_fruit = fruit_g * fr["energy_per_100g"] / 100
        rem = target_energy_kcal - (e_staple + e_prot + e_veg + e_fruit)
        fat_g = max(0, round(rem / (fat["energy_per_100g"] / 100))) if fat["energy_per_100g"] > 0 else 0

        items = [
            _item(staple, staple_g), _item(prot, prot_g),
            _item(veg, veg_g), _item(fr, fruit_g), _item(fat, fat_g),
        ]
        meals = _split_meals(items)
        day_tot = _sum_items(items)
        ach = _achievement(day_tot, target_energy_kcal, target_protein_g, target_k_mg, target_p_mg, target_na_mg)
        days_out.append({"day": d + 1, "meals": meals, "day_totals": day_tot, "achievement": ach})

    overall = _overall_achievement(days_out, target_energy_kcal, target_protein_g,
                                    target_k_mg, target_p_mg, target_na_mg, days)
    return {
        "days": days_out,
        "overall_achievement": overall,
        "targets": {
            "energy_kcal": target_energy_kcal, "protein_g": target_protein_g,
            "potassium_mg": target_k_mg, "phosphorus_mg": target_p_mg, "sodium_mg": target_na_mg,
        },
        "vegetarian": vegetarian,
        "days_count": days,
    }


def get_meal_plan_nutrients(plan: dict) -> dict:
    """从已生成 plan 重新汇总整体营养（校验用）。返回整体平均每日营养素。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    totals = {k: 0.0 for k in ("energy_kcal", "protein_g", "potassium_mg", "phosphorus_mg", "sodium_mg")}
    n = len(plan["days"])
    for d in plan["days"]:
        for k in totals:
            totals[k] += d["day_totals"][k]
    return {k: round(v / n, 1) for k, v in totals.items()}
