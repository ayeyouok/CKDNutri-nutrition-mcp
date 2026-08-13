# -*- coding: utf-8 -*-
"""内置食物成分表的加载、检索与分级。

数据文件：data/food_data.csv，每 100 g 可食部。本模块只做数据访问与派生计算，
不含任何工具级业务编排（业务在 diet.py / targets.py）。
"""
from __future__ import annotations

import csv
import difflib
import os
import re
import threading
from typing import Any

from .constants import (
    COOKING_ALIAS,
    COOKING_LOSS,
    FOOD_TABLE_REF,
    K_LEVELS,
    NA_HIGH_MG_PER_100G,
    P_LEVELS,
    PNPR_LEVELS,
)

_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "data", "food_data.csv")

NUTRIENT_KEYS = ("energy_kcal", "protein_g", "fat_g", "carb_g",
                 "potassium_mg", "phosphorus_mg", "sodium_mg", "calcium_mg")

_CACHE: list[dict[str, Any]] | None = None
_CLUSTER: dict[str, list[dict[str, Any]]] = {}
# 五审（2026-08-13）：懒加载并发锁（double-checked locking）——此前无锁，多线程
# 首次调用时 _CLUSTER.clear() 重建与读取竞态（一个线程清空后另一线程读到半空
# 聚类表，find_food_cluster 漏命中）；refresh=True 同样在锁内重建。
_CACHE_LOCK = threading.Lock()


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def base_name(name: str) -> str:
    """剥离名称末尾的（…）/ (…)，得到聚类基名（早籼（标一）→ 早籼）。"""
    m = re.match(r"^(.*?)[（(][^（）()]*[)）]\s*$", (name or "").strip())
    return m.group(1) if m else (name or "").strip()


def load_foods(refresh: bool = False) -> list[dict[str, Any]]:
    """读取并缓存食物表。返回的每行含数值化字段与派生分级。"""
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    with _CACHE_LOCK:
        # 五审：double-checked locking——首个线程释放锁后，等待线程直接命中缓存
        if _CACHE is not None and not refresh:
            return _CACHE
        rows: list[dict[str, Any]] = []
        with open(_DATA_FILE, encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                if not (raw.get("name") or "").strip():
                    continue
                row: dict[str, Any] = {
                    "name": raw["name"].strip(),
                    "aliases": [a for a in (raw.get("aliases") or "").split(";") if a],
                    "category": (raw.get("category") or "").strip(),
                    "subcategory": (raw.get("subcategory") or "").strip(),
                    "edible_pct": _to_float(raw.get("edible_pct"), 100.0),
                    "unit_name": (raw.get("unit_name") or "份").strip(),
                    "unit_grams": _to_float(raw.get("unit_grams"), 100.0),
                    "unit_desc": (raw.get("unit_desc") or "").strip(),
                    "note": (raw.get("note") or "").strip(),
                }
                for key in NUTRIENT_KEYS:
                    row[key] = _to_float(raw.get(key))
                row["potassium_level"], row["potassium_label"] = classify(row["potassium_mg"], K_LEVELS)
                row["phosphorus_level"], row["phosphorus_label"] = classify(row["phosphorus_mg"], P_LEVELS)
                row["sodium_high"] = row["sodium_mg"] >= NA_HIGH_MG_PER_100G
                row["pnpr_mg_per_g"] = round(row["phosphorus_mg"] / row["protein_g"], 1) \
                    if row["protein_g"] > 0 else None
                rows.append(row)
        _CLUSTER.clear()
        for row in rows:
            _CLUSTER.setdefault(base_name(row["name"]), []).append(row)
        _CACHE = rows
    return rows


def classify(value: float, table: tuple) -> tuple[str, str]:
    for threshold, code, label in table:
        if value < threshold:
            return code, label
    return table[-1][1], table[-1][2]


def pnpr_grade(ratio: float | None) -> tuple[str, str]:
    if ratio is None:
        return "unknown", "无法判定（蛋白为 0）"
    return classify(ratio, PNPR_LEVELS)


def _match_score(row: dict[str, Any], query: str) -> float:
    """名称/别名匹配打分（分越低越相似，99=未命中）。

    v2.4 修复（2026-08-13）：**别名只做精确匹配**，模糊分支（前缀/子串/相似度）
    仅对主名生效。原因：别名语义是「同一食物的另一种叫法」，不是「包含该词的食物」——
    此前「粟米」子串命中别名「粟米油」→ 误匹配「大麻油」；「西红柿」命中「奶柿子」
    同类误伤。别名一旦精确命中即 score=0（最高优先）。
    """
    best = 99.0
    # 1) 别名：只允许精确匹配（方言词入别名后即精确命中，杜绝子串误伤）
    for alias in row["aliases"]:
        if alias == query:
            return 0.0
    # 2) 主名：保留前缀/子串/相似度（名称是描述性短语，模糊匹配合理）
    name = row["name"]
    if name == query:
        best = 0.0
    elif name.startswith(query) or query.startswith(name):
        best = min(best, 1.0)
    elif query in name:
        best = min(best, 2.0)
    elif name in query:
        best = min(best, 3.0)
    else:
        ratio = difflib.SequenceMatcher(None, query, name).ratio()
        # 阈值收紧到 0.80：1752 食物下表，过低会张冠李戴（如“猪瘦肉”误匹配“猪肉脯”）。
        if ratio >= 0.80:
            best = min(best, 4.0 + (1.0 - ratio) * 10.0)
    return best


def search_food(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """按名称/别名检索，返回按匹配度升序的候选行。"""
    text = (query or "").strip()
    if not text:
        return []
    scored = [(row, _match_score(row, text)) for row in load_foods()]
    hits = sorted([item for item in scored if item[1] < 90.0], key=lambda x: (x[1], x[0]["name"]))
    return [row for row, _ in hits[:limit]]


def find_food(query: str) -> dict[str, Any] | None:
    hits = search_food(query, limit=1)
    return hits[0] if hits else None


def find_food_cluster(query: str) -> list[dict[str, Any]] | None:
    """若查询匹配某个含多规格的基名（如“早籼”“鸡蛋”），返回该基名下全部行；否则 None。

    用于 lookup 展示同基名所有规格（标一/标二/土鸡蛋…），由调用方决定返回单条还是整簇。
    """
    text = (query or "").strip()
    if not text:
        return None
    base = base_name(text)
    group = _CLUSTER.get(base)
    if group and len(group) > 1:
        return group
    return None


def scale_nutrients(row: dict[str, Any], grams: float,
                    cooking: str | None = None) -> dict[str, Any]:
    """按克重缩放营养素；cooking 指定时套用烹调保留系数。

    BUG-34 说明（2026-08-12）：数据表为「每 100 g **可食部**」，edible_pct（可食部比例）
    供展示/参考，不参与缩放——**调用方传入的 grams 一律按可食部克重理解**（家长量具
    换算 unit_grams 亦按可食部定义）。带皮带骨/带壳重量需调用方自行换算，避免高估。
    """
    ratio = max(grams, 0.0) / 100.0
    method = COOKING_ALIAS.get((cooking or "").strip(), (cooking or "raw").strip())
    if method not in COOKING_LOSS:
        method = "raw"
    factors = COOKING_LOSS[method]["factor"]
    out: dict[str, Any] = {"grams": round(grams, 1), "cooking": method,
                           "cooking_label": COOKING_LOSS[method]["label"]}
    for key in NUTRIENT_KEYS:
        out[key] = round(row[key] * ratio * factors.get(key, 1.0), 2)
    return out


def food_card(row: dict[str, Any]) -> dict[str, Any]:
    """食物基础卡片（每 100 g 可食部 + 分级 + 家庭量具锚点）。"""
    return {
        "name": row["name"],
        "aliases": row["aliases"],
        "category": row["category"],
        "subcategory": row["subcategory"],
        "edible_pct": row["edible_pct"],
        "per_100g": {key: row[key] for key in NUTRIENT_KEYS},
        "potassium_level": row["potassium_level"],
        "potassium_label": row["potassium_label"],
        "phosphorus_level": row["phosphorus_level"],
        "phosphorus_label": row["phosphorus_label"],
        "sodium_high": row["sodium_high"],
        "phosphorus_protein_ratio_mg_per_g": row["pnpr_mg_per_g"],
        "household_unit": {"unit": row["unit_name"], "grams": row["unit_grams"],
                           "desc": row["unit_desc"]},
        "note": row["note"],
        "source": FOOD_TABLE_REF,
    }


def food_warnings(row: dict[str, Any], scaled: dict[str, Any] | None = None) -> list[str]:
    """生成钾/磷/钠/磷蛋白比的可解释警示文案。"""
    notes: list[str] = []
    if row["potassium_level"] in ("high", "very_high"):
        text = (f"高钾食物：{row['name']} 每 100 g 含钾 {row['potassium_mg']:.0f} mg"
                f"（分级 {row['potassium_level']}）。")
        if scaled:
            text += f"本次 {scaled['grams']:.0f} g 约含钾 {scaled['potassium_mg']:.0f} mg。"
        text += "血钾偏高或 CKD 3 期以上限钾时须按量计入全天钾，并优先做去钾处理。"
        notes.append(text)
    if row["phosphorus_level"] in ("high", "very_high"):
        text = (f"高磷食物：每 100 g 含磷 {row['phosphorus_mg']:.0f} mg"
                f"（分级 {row['phosphorus_level']}）。")
        if scaled:
            text += f"本次 {scaled['grams']:.0f} g 约含磷 {scaled['phosphorus_mg']:.0f} mg。"
        text += "限磷者需与磷结合剂服用时机配合。"
        notes.append(text)
    if row["sodium_high"]:
        notes.append(f"高钠食物：每 100 g 含钠 {row['sodium_mg']:.0f} mg，限钠者按量控制。")
    ratio = row["pnpr_mg_per_g"]
    if ratio is not None and ratio > PNPR_LEVELS[1][0]:
        notes.append(f"磷蛋白比 {ratio:.1f} mg/g 偏高（>{PNPR_LEVELS[1][0]:.0f} 判为慎选），"
                     f"同等蛋白摄入下磷负荷更重。")
    return notes
