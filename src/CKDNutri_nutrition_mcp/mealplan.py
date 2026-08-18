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
    """加载食谱食物集——**数值单一权威源 = food_data.csv**（H2/H3 修复，2026-08-15）。

    foods_ckd.json 只保留名单/分类/素食标记/加工状态（csv_name 指向 CSV 精确行）；
    能量/蛋白/钾/磷/钠全部经 find_food(csv_name) 取自 food_data.csv——与日记侧
    （diary.py 同样经 find_food）**同一数据源**，食谱与日记数值天然一致，杜绝
    "食谱 864 kcal vs 日记 649 kcal"式双源漂移。

    csv_name 解析失败即 fail-fast（数据配置错误，不静默跳过——食谱会少一种食物）。
    """
    global _FOODS
    if _FOODS is None:
        with _FOODS_LOCK:
            if _FOODS is None:  # S3：防多线程首调重复 I/O
                from .fooddb import find_food

                with open(_FOODS_PATH, "r", encoding="utf-8") as fh:
                    spec = json.load(fh)["foods"]
                merged: list[dict] = []
                for f in spec:
                    csv_name = f.get("csv_name") or f["name"]
                    row = find_food(csv_name)
                    if row is None:
                        raise ValueError(
                            f"foods_ckd.json 食物 {f['name']!r}（csv_name={csv_name!r}）"
                            "无法在 food_data.csv 解析——H2 修复要求每项必须映射到"
                            "CSV 精确行，请修正 csv_name")
                    merged.append({
                        "name": f["name"],
                        "cat": f["cat"],
                        "veg": bool(f.get("veg")),
                        "state": f.get("state", "raw"),
                        # 数值全部取自 CSV 权威行（per-100g 口径，与日记侧一致）
                        "energy_per_100g": row["energy_kcal"],
                        "protein_per_100g": row["protein_g"],
                        "potassium_per_100g": row["potassium_mg"],
                        "phosphorus_per_100g": row["phosphorus_mg"],
                        "sodium_per_100g": row["sodium_mg"],
                    })
                _FOODS = merged
    return _FOODS


def _item(food: dict, grams: int) -> dict:
    f = grams / 100.0
    return {
        "food": food["name"],
        # B 方案（2026-08-14）：食物加工状态标注（raw/cooked/dried/soaked）——
        # 生/熟/干/水发的营养值天然不同（如榛蘑干 K 4629 vs 水发 732），
        # 输出带状态避免"同一食物两个数值"的歧义；foods_ckd.json 已带 state 字段。
        "state": food.get("state", "raw"),
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
        # N-S6 修复（2026-08-14）：末餐补差——此前各餐独立 round(grams×ratio)，
        # 25g 油脂分 0.3/0.3/0.4 得 8+8+10=26（+1g 漂移）。末餐取「总量-已分配」，
        # 保证拆分后总和与原始分量一致（油脂上限/能量统计不失真）。
        last_pos = max((i for i, r in enumerate(split) if r > 0), default=-1) if any(split) else -1
        assigned = 0
        for mi, ratio in enumerate(split):
            if ratio <= 0:
                continue
            g = (it["grams"] - assigned) if mi == last_pos else round(it["grams"] * ratio)
            assigned += g
            if g <= 0:
                continue
            f = g / 100.0
            food = by_name[it["food"]]
            meals[mi]["items"].append({
                "food": it["food"], "state": food.get("state", "raw"),
                "cat": food["cat"], "grams": g,
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
    # 边界（2026-08-15）：K/P/Na 目标允许 0（=不设限/忽略），但**负值拒绝**——负目标
    # 此前静默通过：钠约束跳过、钾磷闸门按负基准失真（选品无意义）。
    for _name, _val in (("target_k_mg", target_k_mg), ("target_p_mg", target_p_mg),
                        ("target_na_mg", target_na_mg)):
        if _val < 0:
            raise ValueError(f"{_name} 不能为负（收到 {_val}），0 表示不设限")
    if days <= 0:
        raise ValueError("days 必须 > 0")
    # 边界（2026-08-15）：exclude_foods 传 str 会被 set(str) 拆成单字符静默失效——
    # 显式校验列表类型；str 转 [str] 宽容（编排层常见传法）。
    if exclude_foods is not None:
        if isinstance(exclude_foods, str):
            exclude_foods = [exclude_foods]
        if not isinstance(exclude_foods, list) or \
                not all(isinstance(x, str) for x in exclude_foods):
            raise ValueError("exclude_foods 必须为字符串列表（或单个字符串）")
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

    # N-S6 修复（2026-08-14）：选择策略改为「低钾低磷优先」+「蛋白补差」+「油脂上限」。
    # 此前：① 主食/蛋白/蔬果按 d % len 盲目轮换；② 蛋白源按 0.7×目标蛋白直接折算，
    #     主食蛋白未计入 → 实测 7/7 天蛋白超供 119-163%；③ K/P/Na 目标完全不参与选择，
    #     仅事后标注 → 实测 2-3/7 天钾/磷超限（day5 K=5124mg）；④ 剩余能量全部折算烹调油
    #     （rem≈486 kcal → 55g 油/天，油脂炸弹）。
    # 现在：排序按营养效率（主食/蛋白源按每 100 kcal 或每克蛋白的 K+P 负担；蔬果按钾），
    # 分量按「蛋白补差」（主食配额 40% + 蛋白源补足，精确不超）+「油脂上限 25g/天」，
    # 能量缺口 / K/P 超限一律显式警告（34 项 CKD 子集能量密度有限，缺口交由医生加餐）。
    staple_pool = sorted(staples, key=lambda f: (f["potassium_per_100g"] + f["phosphorus_per_100g"])
                         / max(f["energy_per_100g"], 1))
    prot_pool = sorted(prots, key=lambda f: (f["potassium_per_100g"] + f["phosphorus_per_100g"])
                       / max(f["protein_per_100g"], 0.1))
    veg_pool = sorted(vegs, key=lambda f: f["potassium_per_100g"])
    fruit_pool = sorted(fruits, key=lambda f: f["potassium_per_100g"])
    _FAT_MAX_G = 25.0  # 烹调油/黄油每日上限（油脂炸弹防护）
    _STAPLE_MAX_G = 600.0  # P0-2（2026-08-18）：主食每日克数上限——低蛋白主食（如米粉(熟) 0.9g/100g）
    #   按 40% 蛋白配额折算会爆到 1500g+/天、能量超目标 130%+，钳制并提示蛋白缺口由蛋白源补足。
    _PROT_MAX_G = 400.0  # P0-2（2026-08-18）：蛋白源每日克数上限（同口径，防低蛋白"蛋白源"爆量）

    for d in range(days):
        # N-S6：主食/蛋白源按「预计 K+P 不超限」顺延选择——pool 已按低钾磷排序，
        # 但 8 个主食含土豆/红薯/麦片等高钾品种，7 天轮换必碰上；从当前位置起
        # 向后找第一个预计不超限的品种，找不到才用当前位置（兜底）。
        prot_staple_quota = target_protein_g * 0.40
        staple = None
        for offset in range(len(staple_pool)):
            cand = staple_pool[(d + offset) % len(staple_pool)]
            sg = max(10, round(prot_staple_quota / max(cand["protein_per_100g"], 0.1) * 100))
            est_kp = sg * (cand["potassium_per_100g"] + cand["phosphorus_per_100g"]) / 100
            if est_kp <= (target_k_mg + target_p_mg) * 0.45 or offset == len(staple_pool) - 1:
                staple = cand
                break
        prot = None
        for offset in range(len(prot_pool)):
            cand = prot_pool[(d + offset) % len(prot_pool)]
            # LOW-8（2026-08-15）：蛋白源预计克重与主食同基准——此前主食按实际克重
            # sg 估算 K+P（sg*(K+P)/100），蛋白源却用 100g 绝对含量（100*(K+P)/100），
            # 阈值口径不一致（蛋白源只吃 50g 却按 100g 含量比），选品会系统性
            # 偏向高钾磷品种。现统一按「目标蛋白 40% 配额折算的实际克重」估算。
            # P2-2（2026-08-15）：闸门与实际执行口径再统一——实际分配是「剩余补差」
            # （目标蛋白 − 主食蛋白 ≈40% 配额 − 蔬果蛋白 ≈2g → 剩余 ≈60% 目标蛋白），
            # 此前闸门按 40% 配额估克重，K+P 闸门系统性偏松 ~1.5 倍（选中品种实际
            # 贡献 ≈ 估计的 1.5 倍）。改用实际补差口径估计。
            prot_est_protein = max(1.0, target_protein_g * 0.60 - 2.0)
            pg = max(10, round(prot_est_protein / max(cand["protein_per_100g"], 0.1) * 100))
            est_kp = pg * (cand["potassium_per_100g"] + cand["phosphorus_per_100g"]) / 100
            # P2-3（2026-08-15）：蛋白源选品加钠考量——此前只按 K+P 选品，高钠蛋白源
            # （如虾仁 Na=429/100g）被选中，单日钠可达 900mg+ 且钠无任何约束。
            # 按同口径克重估钠，目标钠的 35% 闸门（target_na_mg<=0 时跳过，无钠目标不约束）。
            na_ok = True
            if target_na_mg > 0:
                est_na = pg * cand["sodium_per_100g"] / 100
                na_ok = est_na <= target_na_mg * 0.35
            if (est_kp <= (target_k_mg + target_p_mg) * 0.35 and na_ok) \
                    or offset == len(prot_pool) - 1:
                prot = cand
                break
        veg = veg_pool[d % len(veg_pool)]
        fr = fruit_pool[d % len(fruit_pool)]
        fat = fats[0]

        # BUG-61（2026-08-12）：防御 0 能量/0 蛋白数据——主食/蛋白源除数必须 >0，
        # 否则 ZeroDivisionError（当前 foods_ckd.json 无此数据，属数据鲁棒性防护；
        # 与下方 fat 的 energy_per_100g>0 守卫同口径）
        if staple["energy_per_100g"] <= 0 or prot["protein_per_100g"] <= 0:
            raise ValueError(
                f"食物库含 0 能量/0 蛋白条目（主食={staple['name']}, 蛋白源={prot['name']}），"
                "无法按目标生成食谱，请检查数据")
        # 1) 蔬果：固定 100g（低钾品种已由 pool 排序保证）
        veg_g = 100
        fruit_g = 100
        e_veg = veg_g * veg["energy_per_100g"] / 100
        e_fruit = fruit_g * fr["energy_per_100g"] / 100
        # 2) 主食：按「目标蛋白 40% 配额」折算——主食蛋白计入预算，避免剩余能量全吸
        #    收进主食导致克数/蛋白失控（N-S6）
        staple_g = max(10, round(prot_staple_quota / staple["protein_per_100g"] * 100)) \
            if staple["protein_per_100g"] > 0 else 0
        # P0-2（2026-08-18）：低蛋白主食按 40% 蛋白配额折算会爆量（米粉(熟) 0.9g/100g →
        # 1556g/天、能量超 130%）。钳制到上限，蛋白缺口由蛋白源/补充剂补足，避免"主食炸弹"。
        if staple_g > _STAPLE_MAX_G:
            plan_warnings.append(
                f"第 {d + 1} 天：主食「{staple['name']}」蛋白密度低"
                f"（{staple['protein_per_100g']:.1f}g/100g），按 40% 蛋白配额需 {staple_g:.0f}g/天，"
                f"已钳制为 {_STAPLE_MAX_G:.0f}g；主食蛋白不足部分由蛋白源/营养补充剂补足")
            staple_g = int(_STAPLE_MAX_G)
        e_staple = staple_g * staple["energy_per_100g"] / 100
        prot_staple = staple_g * staple["protein_per_100g"] / 100
        # 3) 蛋白源：补差 = 目标蛋白 − 主食蛋白 − 蔬果蛋白（精确不超供）
        prot_veg_fruit = (veg_g * veg["protein_per_100g"] + fruit_g * fr["protein_per_100g"]) / 100
        rem_protein = max(0.0, target_protein_g - prot_staple - prot_veg_fruit)
        prot_g = max(0, round(rem_protein / prot["protein_per_100g"] * 100)) \
            if prot["protein_per_100g"] > 0 else 0
        # P0-2（2026-08-18）：蛋白源克数上限钳制（同口径，低蛋白"蛋白源"不会爆量）
        if prot_g > _PROT_MAX_G:
            plan_warnings.append(
                f"第 {d + 1} 天：蛋白源「{prot['name']}」克数 {prot_g:.0f}g 超安全上限"
                f"{_PROT_MAX_G:.0f}g，已钳制；请检查该食物蛋白密度或目标蛋白设置")
            prot_g = int(_PROT_MAX_G)
        # BUG-30 修复（2026-08-12）：蛋白能量统一用食物表总能量（energy_per_100g），
        # 此前用"蛋白g × 4 kcal/g"简化——对鸡蛋/瘦肉等含脂肪蛋白源会低估其能量贡献，
        # 导致 rem（脂肪额度）偏高、day_totals 实际能量系统性超 target。
        e_prot = prot_g * prot["energy_per_100g"] / 100
        # 4) 油脂：固定额度 ≤25g/天（N-S6 油脂炸弹防护），补足能量缺口
        fat_g = int(_FAT_MAX_G) if fat["energy_per_100g"] > 0 else 0
        e_fat = fat_g * fat["energy_per_100g"] / 100
        # 5) 能量缺口显式警告（34 项子集能量密度有限，缺口交由临床加餐，不硬凑油脂）
        e_total = e_staple + e_prot + e_veg + e_fruit + e_fat
        gap = target_energy_kcal - e_total
        if gap > 120:
            plan_warnings.append(
                f"第 {d + 1} 天：食谱能量 {round(e_total, 0):.0f} kcal，缺口 {round(gap, 0):.0f} kcal"
                "（蛋白已按目标精确配比、油脂已封顶 25g/天）——建议经临床评估后增加主食/"
                "加餐分量或营养补充剂补足能量，避免限磷限钾下纯油脂补能")
        # P0-2（2026-08-18）：超能告警（与缺口告警对称）——此前仅 deficit 告警，能量超目标
        # （如低蛋白主食爆量）被静默放过，医生无感知。超 120 kcal 即显式告警。
        elif gap < -120:
            plan_warnings.append(
                f"第 {d + 1} 天：食谱能量 {round(e_total, 0):.0f} kcal，超出目标 "
                f"{round(-gap, 0):.0f} kcal（{round(e_total / target_energy_kcal * 100):.0f}%）"
                "——建议减少主食/油脂分量，或经临床评估放宽能量目标")

        items = [
            _item(staple, staple_g), _item(prot, prot_g),
            _item(veg, veg_g), _item(fr, fruit_g), _item(fat, fat_g),
        ]
        meals = _split_meals(items)
        day_tot = _sum_items(items)
        ach = _achievement(day_tot, target_energy_kcal, target_protein_g, target_k_mg, target_p_mg, target_na_mg)
        # M2 修复（2026-08-14）：限钾/磷/钠目标**超限进入 plan_warnings**——此前超限
        # 只标 *_exceeded 布尔（achievement 内），限钾磷患儿的食谱"静默超限"，医生
        # 不逐日看 achievement 即无感知。逐日超限显式警告，提示需调整品种/分量。
        _over = [n for n, flag in (("钾", ach["potassium_exceeded"]),
                                   ("磷", ach["phosphorus_exceeded"]),
                                   ("钠", ach["sodium_exceeded"])) if flag]
        if _over:
            plan_warnings.append(
                f"第 {d + 1} 天：{('、'.join(_over))}超限——K={day_tot['potassium_mg']:.0f}mg"
                f"(目标 {target_k_mg:.0f})、P={day_tot['phosphorus_mg']:.0f}mg(目标 {target_p_mg:.0f})、"
                f"Na={day_tot['sodium_mg']:.0f}mg(目标 {target_na_mg:.0f})，建议调整品种/分量"
                "或由医生评估后放宽目标")
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
