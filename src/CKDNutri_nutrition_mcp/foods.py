# -*- coding: utf-8 -*-
"""食物查询、替换建议、量具换算与磷蛋白比。"""
from __future__ import annotations

from typing import Any

from a207_policy import enforce_read, get_caller

from .constants import DEKALIUM_TIPS, FOOD_TABLE_REF, MCP_NAME, PNPR_LEVELS
from .fooddb import (
    base_name,
    find_food,
    find_food_cluster,
    food_card,
    food_warnings,
    load_foods,
    pnpr_grade,
    scale_nutrients,
    search_food,
)
from .measures import nutrient_anchors, parse_portion, to_household

# 食物角色收敛表：把 CSV 的细粒度 category（猪/牛/植物油/稻米/鱼…）归并为粗粒度角色，
# 替换时只在同角色内推荐，杜绝“把油当肉替”这类荒谬结果。
_ROLE_MAP = {
    "猪": "肉类", "牛": "肉类", "羊": "肉类", "驴": "肉类", "马": "肉类",
    "蓄肉类其他": "肉类", "禽肉类其他": "肉类", "鸡": "肉类", "鸭": "肉类",
    "鹅": "肉类", "火鸡": "肉类",
    "鸡蛋": "蛋类", "鸭蛋": "蛋类", "鹅蛋": "蛋类", "鹌鹑蛋": "蛋类",
    "鱼": "水产", "虾": "水产", "蟹": "水产", "贝": "水产", "鱼虾蟹贝类其他": "水产",
    "大豆": "豆制品", "鲜豆类": "豆制品", "干豆类其他": "豆制品", "蚕豆": "豆制品",
    "绿豆": "豆制品", "赤豆": "豆制品", "腐乳": "豆制品",
    "液态乳": "奶", "奶酪": "奶", "酸奶": "奶", "奶粉": "奶", "乳类其他": "奶", "含乳饮料": "奶",
    "稻米": "主食", "小麦": "主食", "玉米": "主食", "小米、黄米": "主食", "大麦": "主食",
    "谷类其他": "主食", "薯类": "主食", "薯芋类": "主食", "淀粉类": "主食", "方便食品": "主食",
    "嫩茎、叶、花菜类": "蔬菜", "野生蔬菜类": "蔬菜", "茄果、瓜菜类": "蔬菜",
    "咸菜类": "蔬菜", "根菜类": "蔬菜", "葱蒜类": "蔬菜", "水生蔬菜类": "蔬菜",
    "青头菜": "蔬菜", "嫩姜": "蔬菜", "菌类": "蔬菜", "藻类": "蔬菜", "蔬菜汁饮料": "蔬菜",
    "仁果类": "水果", "核果类": "水果", "热带、亚热带水果": "水果", "浆果类": "水果",
    "柑橘类": "水果", "瓜果类": "水果", "果汁及果汁饮料": "水果",
    "植物油": "油脂", "动物油": "油脂", "奶油": "油脂",
    "树坚果": "坚果", "种子": "坚果",
}
_PROTEIN_ROLES = ("肉类", "蛋类", "水产", "豆制品", "奶")
_STAPLE_ROLES = ("主食",)

# 约束 -> (字段, 中文标签, 模式)。模式 lower=取更低者；iso_energy=等能量替换。
CONSTRAINT_MAP = {
    "低钾": ("potassium_mg", "钾", "lower"), "限钾": ("potassium_mg", "钾", "lower"),
    "low_k": ("potassium_mg", "钾", "lower"), "low_potassium": ("potassium_mg", "钾", "lower"),
    "低磷": ("phosphorus_mg", "磷", "lower"), "限磷": ("phosphorus_mg", "磷", "lower"),
    "low_p": ("phosphorus_mg", "磷", "lower"), "low_phosphorus": ("phosphorus_mg", "磷", "lower"),
    "低钠": ("sodium_mg", "钠", "lower"), "限钠": ("sodium_mg", "钠", "lower"),
    "low_na": ("sodium_mg", "钠", "lower"), "low_sodium": ("sodium_mg", "钠", "lower"),
    "低蛋白": ("protein_g", "蛋白质", "lower"), "限蛋白": ("protein_g", "蛋白质", "lower"),
    "low_protein": ("protein_g", "蛋白质", "lower"),
    "等能量": ("energy_kcal", "能量", "iso_energy"), "iso_energy": ("energy_kcal", "能量", "iso_energy"),
    "同热量": ("energy_kcal", "能量", "iso_energy"), "等热量": ("energy_kcal", "能量", "iso_energy"),
}


def _not_found(food: str) -> dict[str, Any]:
    return {"ok": False, "error": "FOOD_NOT_FOUND",
            "detail": f"内置食物表中没有与「{food}」匹配的条目，"
                      f"可换更常见的名称（如「米饭」「猪肉（瘦）」「香蕉」）再查。"}


def _cluster_view(query: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """同基名多规格聚簇展示（早籼/鸡蛋…），供人工或 LLM 选具体规格后再精确计算。"""
    base = base_name(query)
    variants = [food_card(r) for r in rows]
    return {"ok": True, "data": {
        "query": query,
        "is_cluster": True,
        "base_name": base,
        "variant_count": len(variants),
        "variants": variants,
        "note": "该食物含多个规格/品种，请选择具体规格以获得精确计算。",
        "source": FOOD_TABLE_REF,
    }}


# BUG-33（2026-08-12）：DEKALIUM_TIPS 的键是口语分类（"叶菜""薯类"…），与 CSV 的实际
# category 值（"嫩茎、叶、花菜类""薯芋类"…）不对齐 → 叶菜/根茎/菌藻/畜肉提示零命中、
# 永远 fallback "吃菜不喝汤"。增加 CSV 分类 → 提示键 的映射，匹配走 category 子串。
_DEKALIUM_CATEGORY_KEYS: dict[str, tuple[str, ...]] = {
    "叶菜": ("嫩茎、叶、花菜类", "野生蔬菜类", "水生蔬菜类"),
    "薯类": ("薯类", "薯芋类", "淀粉类"),
    "根茎": ("根菜类",),
    "菌藻": ("菌类", "藻类"),
    "水果": ("仁果类", "核果类", "热带、亚热带水果", "浆果类", "柑橘类", "瓜果类"),
    "畜肉": ("猪", "牛", "羊", "马", "驴", "蓄肉类其他", "禽肉类其他",
             "鸡", "鸭", "鹅", "火鸡"),
}


def _dekalium_tip(row: dict[str, Any]) -> str:
    keys = {row.get("subcategory", ""), row.get("category", "")}
    for tip_key, tip in DEKALIUM_TIPS.items():
        if tip_key == "default":
            continue
        for token in _DEKALIUM_CATEGORY_KEYS.get(tip_key, (tip_key,)):
            if token and any(token in k for k in keys if k):
                return tip
    return DEKALIUM_TIPS["default"]


def lookup_food_nutrients(food: str, portion: str | None = None,
                          cooking: str | None = None) -> dict[str, Any]:
    """查询食物成分。portion 支持家庭量具（半碗/1个/两勺）或克重。

    基名查询（如“早籼”“鸡蛋”）若含多规格，返回整簇变体供选择；具体规格名（如
    “早籼（标一）”）仍返回单条并计算，保证计算路径精确不变。
    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    row = find_food(food)
    if row is None:
        return _not_found(food)
    # 精确同名优先：具体规格（如「早籼（标一）」「猪肉（瘦）」）直接走单条精确计算。
    # 别名层：当查询是“裸基名”（无规格后缀，如「早籼」「鸡蛋」）且存在 ≥3 个规格变体时，
    # 返回整簇供选择——满足“查早籼同时列出 标一/标二/特等”的需求；
    # 仅 2 个变体的常见食物（香蕉+红皮 等）与具体规格查询仍走精确单条，保证计算不出错。
    cluster = find_food_cluster(food)
    if cluster is not None and len(cluster) >= 3 and base_name(food) == food:
        return _cluster_view(food, cluster)
    resolved = parse_portion(portion, row)
    scaled = scale_nutrients(row, resolved["grams"], cooking)
    warnings = food_warnings(row, scaled)
    if not resolved["resolved"]:
        warnings.append(resolved["basis"])

    data = {
        "query": food,
        "food": food_card(row),
        "portion": {"input": portion, "grams": scaled["grams"], "basis": resolved["basis"]},
        "cooking": {"method": scaled["cooking"], "label": scaled["cooking_label"]},
        "intake": {key: scaled[key] for key in
                   ("energy_kcal", "protein_g", "fat_g", "carb_g",
                    "potassium_mg", "phosphorus_mg", "sodium_mg", "calcium_mg")},
        "household_translation": nutrient_anchors(scaled["protein_g"], scaled["energy_kcal"]),
        "warnings": warnings,
        "source": FOOD_TABLE_REF,
    }
    if row["potassium_level"] in ("high", "very_high"):
        data["dekalium_tip"] = _dekalium_tip(row)
    alternatives = [item["name"] for item in search_food(food, limit=4)
                    if item["name"] != row["name"]]
    if alternatives:
        data["other_matches"] = alternatives
    return {"ok": True, "data": data}


def substitute_food(food: str, constraint: str = "等能量", top_n: int = 4) -> dict[str, Any]:
    """按约束在“同食物角色”内推荐替换食物。

    constraint 可选：等能量(默认) / 低钾 / 低磷 / 低钠 / 低蛋白。
    等能量（默认，最贴合“家里没有某食物”的平替场景）：在同角色内找能量接近（±30%）
    且钾不更高的食物；低钾/低磷/低钠/低蛋白：在同角色内找该项更低的食物。
    候选池按食物角色收敛，杜绝把油脂当肉、把内脏当主食等荒谬替换。
    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    row = find_food(food)
    if row is None:
        return _not_found(food)
    key = str(constraint or "等能量").strip().lower()
    mapped = CONSTRAINT_MAP.get(key) or CONSTRAINT_MAP.get(str(constraint or "").strip())
    if mapped is None:
        return {"ok": False, "error": "UNSUPPORTED_CONSTRAINT",
                "detail": f"不支持的约束「{constraint}」，可用：等能量 / 低钾 / 低磷 / 低钠 / 低蛋白"}
    field, label, mode = mapped
    role, pool = _candidate_pool(row)
    if mode == "iso_energy":
        base = row["energy_kcal"] or 1.0
        candidates = [item for item in pool
                      if abs(item["energy_kcal"] - base) / base <= 0.30
                      and item["potassium_mg"] <= row["potassium_mg"]]
        candidates.sort(key=lambda item: (abs(item["energy_kcal"] - base), -item["protein_g"]))
        rationale = "同食物角色内能量接近（±30%）且钾不更高的食物，便于等能量平替"
    else:
        candidates = [item for item in pool if item[field] < row[field]]
        candidates.sort(key=lambda item: (item[field], -item["energy_kcal"]))
        rationale = f"同食物角色内{label}低于「{row['name']}」的食物，按{label}升序推荐"

    if not candidates:
        return {"ok": True, "data": {
            "base": food_card(row), "constraint": label, "role": role, "options": [],
            "message": f"在「{role}」角色内没有比「{row['name']}」{label}更低的常见食物，"
                       f"建议改为控制分量或更换食物大类。",
            "tip": _dekalium_tip(row), "source": FOOD_TABLE_REF}}

    options = []
    for item in candidates[:max(int(top_n or 4), 1)]:
        base_grams = row["unit_grams"]
        iso_grams = (base_grams * row["energy_kcal"] / item["energy_kcal"]) \
            if item["energy_kcal"] > 0 else None
        options.append({
            "name": item["name"],
            "role": _role_of(item),
            "per_100g": {k: item[k] for k in
                         ("energy_kcal", "protein_g", "potassium_mg",
                          "phosphorus_mg", "sodium_mg")},
            "potassium_label": item["potassium_label"],
            "phosphorus_label": item["phosphorus_label"],
            "delta_per_100g": round(item[field] - row[field], 2),
            "iso_energy_grams": round(iso_grams, 0) if iso_grams else None,
            "household_unit": f"{item['unit_name']}（{item['unit_desc']}）",
        })

    return {"ok": True, "data": {
        "base": food_card(row),
        "constraint": label,
        "role": role,
        "rationale": rationale,
        "pool": f"{role} 角色（蛋白类可跨肉/蛋/水产/豆制品/奶；主食类跨谷薯）",
        "options": options,
        "iso_energy_note": f"iso_energy_grams 表示替换后达到与 {row['unit_grams']:.0f} g "
                           f"{row['name']} 等能量所需的克重",
        "tip": _dekalium_tip(row),
        "source": FOOD_TABLE_REF,
    }}


def _role_of(row: dict[str, Any]) -> str:
    return _ROLE_MAP.get(row.get("category", ""), "其他")


def _candidate_pool(row: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """按食物角色收敛候选池：蛋白类食物之间、主食之间、同角色内部互替，油类永不混入肉类。"""
    role = _role_of(row)
    all_foods = load_foods()
    if role in _PROTEIN_ROLES:
        pool = [i for i in all_foods if _role_of(i) in _PROTEIN_ROLES and i["name"] != row["name"]]
    elif role in _STAPLE_ROLES:
        pool = [i for i in all_foods if _role_of(i) in _STAPLE_ROLES and i["name"] != row["name"]]
    else:
        pool = [i for i in all_foods if _role_of(i) == role and i["name"] != row["name"]]
    return role, pool


def convert_to_household_measure(food: str, grams: float) -> dict[str, Any]:
    """克重 -> 家庭量具表达，并同时给出该克重的营养素。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    row = find_food(food)
    if row is None:
        return _not_found(food)
    if grams is None or grams < 0:
        return {"ok": False, "error": "INVALID_INPUT", "detail": "grams 不能为负"}
    measure = to_household(row, float(grams))
    scaled = scale_nutrients(row, float(grams))
    return {"ok": True, "data": {
        "food": row["name"],
        "grams": measure["grams"],
        "measure": measure["primary"],
        "alternatives": measure["alternatives"],
        "intake": {key: scaled[key] for key in
                   ("energy_kcal", "protein_g", "potassium_mg", "phosphorus_mg", "sodium_mg")},
        "household_translation": nutrient_anchors(scaled["protein_g"], scaled["energy_kcal"]),
        "note": row["note"],
        "source": FOOD_TABLE_REF,
    }}


def calc_pnpr(food: str | None = None, protein_g: float | None = None,
              phosphorus_mg: float | None = None, grams: float = 100.0) -> dict[str, Any]:
    """磷蛋白比（mg 磷 / g 蛋白）：同等蛋白供给下的磷负荷指标。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    if food:
        row = find_food(food)
        if row is None:
            return _not_found(food)
        scaled = scale_nutrients(row, float(grams or 100.0))
        protein = scaled["protein_g"]
        phosphorus = scaled["phosphorus_mg"]
        origin = f"内置食物表：{row['name']} {scaled['grams']:.0f} g"
        name = row["name"]
    else:
        if protein_g is None or phosphorus_mg is None:
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": "未提供 food 时，protein_g 与 phosphorus_mg 均必填"}
        protein, phosphorus = float(protein_g), float(phosphorus_mg)
        origin, name = "调用方直接提供的数值", "自定义食物"
    if protein <= 0:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "蛋白质为 0 时磷蛋白比无临床意义，请改查磷绝对量"}
    ratio = phosphorus / protein
    code, label = pnpr_grade(ratio)
    return {"ok": True, "data": {
        "food": name, "basis": origin,
        "protein_g": round(protein, 2), "phosphorus_mg": round(phosphorus, 1),
        "pnpr_mg_per_g": round(ratio, 1), "grade": code, "grade_label": label,
        "thresholds": {"preferred": f"<{PNPR_LEVELS[0][0]:.0f}",
                       "acceptable": f"{PNPR_LEVELS[0][0]:.0f}-{PNPR_LEVELS[1][0]:.0f}",
                       "caution": f">{PNPR_LEVELS[1][0]:.0f}"},
        "interpretation": f"每摄入 1 g 蛋白质同时带入 {ratio:.1f} mg 磷。"
                          f"限磷患儿应优先选择磷蛋白比低的蛋白来源（如蛋清），"
                          f"并注意加工食品的磷添加剂吸收率接近 100%，不体现在成分表中。",
        "source": FOOD_TABLE_REF,
    }}
