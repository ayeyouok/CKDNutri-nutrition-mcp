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
import threading
from typing import Optional

from a207_policy import enforce_nutrition_tool, get_caller

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
# S3（2026-08-12 五包审查）：懒加载并发锁（double-checked locking）
_FOODS_LOCK = threading.Lock()


def _load_foods() -> list[dict]:
    global _FOODS
    if _FOODS is None:
        with _FOODS_LOCK:
            if _FOODS is None:  # S3：防多线程首调重复 I/O
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
    """达成率。能量/蛋白是"摄入目标"（越高越好，超 100 保留）；钾/磷/钠是
    **限制性上限目标**（超限有害）——达成率 cap 在 100%，超标单独以 *_exceeded 标注，
    避免"磷超标 150%"被读成更高达成率拉高整体（BUG-41 修复，2026-08-12）。"""
    def pct(actual: float, target: float) -> int:
        if target <= 0:
            return 0
        return round(actual / target * 100)

    def cap_pct(actual: float, target: float) -> int:
        if target <= 0:
            return 0
        return min(round(actual / target * 100), 100)

    return {
        "energy_pct": pct(tot["energy_kcal"], t_energy),
        "protein_pct": pct(tot["protein_g"], t_protein),
        "potassium_pct": cap_pct(tot["potassium_mg"], t_k),
        "phosphorus_pct": cap_pct(tot["phosphorus_mg"], t_p),
        "sodium_pct": cap_pct(tot["sodium_mg"], t_na),
        # 限制性上限目标：是否超限（true 表示当日该营养素超过上限）
        "potassium_exceeded": tot["potassium_mg"] > t_k if t_k > 0 else False,
        "phosphorus_exceeded": tot["phosphorus_mg"] > t_p if t_p > 0 else False,
        "sodium_exceeded": tot["sodium_mg"] > t_na if t_na > 0 else False,
    }


def _split_meals(items: list[dict]) -> list[dict]:
    # BUG-59（2026-08-12）：提前建 name→food 索引，避免循环内逐条 O(N) 线性扫描
    by_name = {f["name"]: f for f in _load_foods()}
    meals = [{ "meal": name, "items": [], "totals": None } for name in _MEAL_NAMES]
    for it in items:
        split = _MEAL_SPLIT.get(it["cat"], [0, 0, 0, 0])
        for mi, ratio in enumerate(split):
            g = round(it["grams"] * ratio)
            if g <= 0:
                continue
            f = g / 100.0
            food = by_name[it["food"]]
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
    # BUG-02 修复：recipe 组仅临床助手（需求 recipe 组 临床=✔ 家庭=✘），
    # 原实现仅 enforce_read，家长（矩阵 R/W）可调用食谱生成。
    enforce_nutrition_tool(caller, "generate_meal_plan")
    # P0-7 修复（2026-08-13）：NaN/Inf 显式拒绝（NaN<=0 恒 False 会穿透）
    import math as _math

    for _name, _val in (("target_energy_kcal", target_energy_kcal),
                        ("target_protein_g", target_protein_g),
                        ("target_k_mg", target_k_mg), ("target_p_mg", target_p_mg),
                        ("target_na_mg", target_na_mg), ("days", days)):
        if isinstance(_val, (int, float)) and not isinstance(_val, bool) \
                and (_math.isnan(_val) or _math.isinf(_val)):
            raise ValueError(f"{_name} 必须为有效的有限数值，收到 {_val!r}")
    if target_energy_kcal <= 0 or target_protein_g <= 0:
        raise ValueError("target_energy_kcal 与 target_protein_g 必须 > 0")
    if days <= 0:
        raise ValueError("days 必须 > 0")
    # P2 修复（2026-08-13）：vegetarian 显式 bool 校验——编排层直调 core 时若传
    # 字符串 "false"，bool("false")==True 会静默开素食（蛋白源减半）。FastMCP 层有
    # pydantic 拦截，但 core 是纯函数库可被绕过，入口显式拒绝非 bool。
    if not isinstance(vegetarian, bool):
        raise ValueError(f"vegetarian 必须为布尔值，收到 {vegetarian!r}（字符串 'false' 会被"
                         f"bool() 判为 True，静默开启素食模式）")
    # P2 修复（2026-08-13）：days 上限钳制（默认 7）——days=90 会把 90 天×4 餐刷进
    # LLM 上下文。食谱是"周计划"粒度，超出 14 天钳制并告警，不报错。
    _DAYS_MAX = 14
    if days > _DAYS_MAX:
        days = _DAYS_MAX

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
    # BUG-63（2026-08-12）：收集能量负平衡警告——主食+蛋白+蔬果已超目标时脂肪按 0 仍
    # 超标，此前静默返回不平衡食谱，仅靠达成率>100% 提示不够明确。
    plan_warnings: list[str] = []
    for d in range(days):
        staple = staples[d % len(staples)]
        prot = prots[d % len(prots)]
        veg = vegs[d % len(vegs)]
        fr = fruits[d % len(fruits)]
        fat = fats[0]

        # BUG-61（2026-08-12）：防御 0 能量/0 蛋白数据——主食/蛋白源除数必须 >0，
        # 否则 ZeroDivisionError（当前 foods_ckd.json 无此数据，属数据鲁棒性防护；
        # 与下方 fat 的 energy_per_100g>0 守卫同口径）
        if staple["energy_per_100g"] <= 0 or prot["protein_per_100g"] <= 0:
            raise ValueError(
                f"食物库含 0 能量/0 蛋白条目（主食={staple['name']}, 蛋白源={prot['name']}），"
                "无法按目标生成食谱，请检查数据")
        staple_g = max(10, round(target_energy_kcal * 0.5 / staple["energy_per_100g"] * 100))
        prot_g = max(10, round(target_protein_g * 0.7 / prot["protein_per_100g"] * 100))
        veg_g = 100
        fruit_g = 100

        e_staple = staple_g * staple["energy_per_100g"] / 100
        # BUG-30 修复（2026-08-12）：蛋白能量统一用食物表总能量（energy_per_100g），
        # 此前用"蛋白g × 4 kcal/g"简化——对鸡蛋/瘦肉等含脂肪蛋白源会低估其能量贡献，
        # 导致 rem（脂肪额度）偏高、day_totals 实际能量系统性超 target。
        e_prot = prot_g * prot["energy_per_100g"] / 100
        e_veg = veg_g * veg["energy_per_100g"] / 100
        e_fruit = fruit_g * fr["energy_per_100g"] / 100
        rem = target_energy_kcal - (e_staple + e_prot + e_veg + e_fruit)
        if rem < 0:
            plan_warnings.append(
                f"第 {d + 1} 天：主食+蛋白+蔬果能量已达 {round(e_staple + e_prot + e_veg + e_fruit, 0):.0f} kcal，"
                f"超过目标 {target_energy_kcal:.0f} kcal，脂肪按 0 计仍超标——请削减主食/蛋白分量")
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
        "warnings": plan_warnings,  # BUG-63：能量负平衡/需削减主食提示（空列表=无警告）
        "targets": {
            "energy_kcal": target_energy_kcal, "protein_g": target_protein_g,
            "potassium_mg": target_k_mg, "phosphorus_mg": target_p_mg, "sodium_mg": target_na_mg,
        },
        "vegetarian": vegetarian,
        "days_count": days,
        # BUG-31 透明标注（2026-08-12）：食谱基于 CKD 适宜食物子集（近似值），
        # 与 lookup_food_nutrients 的全量中国食物成分表数值可能不同——跨工具核对时以全量库为准。
        "source": "CKD 适宜食物子集（近似值）；如需精确成分请以 lookup_food_nutrients（全量食物成分表）核对",
    }


def get_meal_plan_nutrients(plan: dict) -> dict:
    """从已生成 plan 重新汇总整体营养（校验用）。返回整体平均每日营养素。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    # BUG-02 修复：recipe 组仅临床助手
    enforce_nutrition_tool(caller, "get_meal_plan_nutrients")
    totals = {k: 0.0 for k in ("energy_kcal", "protein_g", "potassium_mg", "phosphorus_mg", "sodium_mg")}
    n = len(plan["days"])
    for d in plan["days"]:
        for k in totals:
            totals[k] += d["day_totals"][k]
    return {k: round(v / n, 1) for k, v in totals.items()}
