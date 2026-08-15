# -*- coding: utf-8 -*-
"""饮食日记汇总与目标达成率评估。"""
from __future__ import annotations

from typing import Any

from a207_policy import enforce_read, get_caller

from .constants import FOOD_TABLE_REF, GUIDELINE, MCP_NAME
from .core import _normalize_date
from .fooddb import find_food, pnpr_grade, scale_nutrients
from .measures import parse_portion

SUM_KEYS = ("energy_kcal", "protein_g", "fat_g", "carb_g",
            "potassium_mg", "phosphorus_mg", "sodium_mg", "calcium_mg")

# 目标字段的兼容别名：既接受本包的输出，也接受 PCP 风格字段
# 五审（2026-08-13）：补 energy_target_kcal_per_day / protein_target_g_per_day——
# _normalize_target 把 PRNT 信封拍平后（energy.target_kcal_per_day →
# energy_target_kcal_per_day），这两键才会出现在目标 dict 顶层。
# S3 修复（2026-08-13）：移除 avg_* 别名——它们是**实际摄入均值**（日记汇总产出），
# 不是目标。若误把日记均值当 target 传入，assess_intake_vs_target 会把「摄入 vs 自身
# 均值」对照，产出 ~100% 达成率假象且无告警。目标键只接受真正的 target 命名空间。
TARGET_ALIAS = {
    "energy_kcal": ("energy_kcal_per_day", "energy_kcal", "target_kcal_per_day",
                    "energy_target_kcal_per_day"),
    "protein_g": ("protein_g_per_day", "protein_g", "target_g_per_day",
                  "protein_target_g_per_day"),
    "potassium_mg": ("potassium_mg_per_day", "potassium_mg", "k_mg_per_day"),
    "phosphorus_mg": ("phosphorus_mg_per_day", "phosphorus_mg", "p_mg_per_day"),
    "sodium_mg": ("sodium_mg_per_day", "sodium_mg", "na_mg_per_day"),
}
LIMIT_KEYS = ("potassium_mg", "phosphorus_mg", "sodium_mg")
FIELD_LABEL = {"energy_kcal": "能量", "protein_g": "蛋白质", "potassium_mg": "钾",
               "phosphorus_mg": "磷", "sodium_mg": "钠"}


def _blank_totals() -> dict[str, float]:
    return {key: 0.0 for key in SUM_KEYS}


def _normalize_target(target: dict[str, Any]) -> dict[str, Any]:
    """把目标参数归一化为顶层键可查的 dict（供 TARGET_ALIAS 命中）。

    五审（2026-08-13）修复 BUG：docstring 承诺"target 可传 calc_prnt_targets 的
    结果"，但 PRNT 返回 {ok, data: {energy: {target_kcal_per_day}, protein: {...}}}
    嵌套结构——TARGET_ALIAS 只查顶层键，全部取不到 → 达成率对照**静默为空**
    （achievement.items=[] 且无任何提示）。修复：解 {ok,data} 信封 + 把
    energy/protein 子块键拍平到顶层（target_kcal_per_day → energy_target_kcal_per_day）。
    """
    if not isinstance(target, dict) or not isinstance(target.get("data"), dict):
        return target
    data = target["data"]
    if isinstance(data.get("energy"), dict):
        data = {**data, **{f"energy_{k}": v for k, v in data["energy"].items()}}
    if isinstance(data.get("protein"), dict):
        data = {**data, **{f"protein_{k}": v for k, v in data["protein"].items()}}
    return data


def _pick_target(target: dict[str, Any], field: str) -> float | None:
    # S3 修复（2026-08-13）：检测「摄入均值误当目标」——若 target 里出现 avg_* 键
    # （日记汇总产出，不是目标），显式报错而非静默对照自身（会产出 ~100% 达成率假象）。
    for key in ("avg_energy_kcal", "avg_protein_g", "avg_potassium_mg",
                "avg_phosphorus_mg", "avg_sodium_mg"):
        if key in target:
            raise ValueError(
                f"target 含摄入均值键 {key}——avg_* 是日记实际摄入均值，不是目标。"
                f"请改用真正的目标命名空间（如 energy_target_kcal_per_day / "
                f"protein_target_g_per_day / target_kcal_per_day），"
                f"或先调用 calc_prnt_targets 计算目标后再对照。")
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
    # BUG-64（2026-08-13）：记录无法归一化的日期——计算路径宽容分桶但必须显式警告，
    # 否则与写入路径（_normalize_date fail-closed 拒绝）行为割裂，用户算出一版
    # "能算但存不进"的结果，跨天统计被静默拆散。
    bad_dates: list[str] = []
    # MED-1（2026-08-15）：缺失营养素食物汇总提示（按 0 计会低估全天摄入）
    missing_foods: list[str] = []

    for index, entry in enumerate(diary):
        if not isinstance(entry, dict):
            unmatched.append({"index": index, "reason": "条目不是对象", "raw": str(entry)})
            continue
        name = str(entry.get("food") or entry.get("name") or "").strip()
        row = find_food(name) if name else None
        if row is None:
            # P0-6：None 可能是「未匹配」或「多规格歧义」（find_food 拒绝猜测）——
            # 区分提示，引导用户用完整名称/规格词（如"松蘑（干）"而非"松蘑"）。
            if name and len(name) >= 2:
                reason = ("内置食物表未唯一匹配该名称（可能多规格歧义或名称过简），"
                          "请用完整名称（如含规格词）重试")
            else:
                reason = "内置食物表中未匹配到该名称"
            unmatched.append({"index": index, "food": name, "reason": reason})
            continue
        grams = entry.get("grams") or entry.get("weight_g")
        # #3（2026-08-15）：数值字符串（如 "150"）不得静默按 1 份计——此前仅
        # isinstance(int/float) 走克重，str 数字掉进 parse_portion(None) 按"1 份"
        # 折算（如 150g 苹果算成 1 份≈100g，摄入量低估 33% 且无告警）。
        if isinstance(grams, str):
            grams = grams.strip()
            try:
                grams = float(grams)
            except ValueError:
                grams = None
        if isinstance(grams, (int, float)) and grams > 0:
            grams = float(grams)
            basis = f"按输入克重 {grams:.0f} g"
        else:
            resolved = parse_portion(entry.get("portion"), row)
            grams = resolved["grams"]
            basis = resolved["basis"]
        scaled = scale_nutrients(row, grams, entry.get("cooking"))

        # BUG-60：读取路径宽容归一化——可解析的变体日期统一为 ISO，无法解析的保留原样分桶
        raw_date = entry.get("date")
        try:
            date = _normalize_date(raw_date) if raw_date else "未标注日期"
        except ValueError:
            date = str(raw_date)
            bad_dates.append(str(raw_date))  # BUG-64：计数并随结果警告
        bucket = per_day.setdefault(date, {"date": date, "items": 0, "totals": _blank_totals()})
        bucket["items"] += 1
        for key in SUM_KEYS:
            bucket["totals"][key] += scaled[key]
        contributions.append({"food": row["name"], "date": date,
                              "meal": entry.get("meal"), "grams": scaled["grams"],
                              "basis": basis,
                              **{key: scaled[key] for key in
                                 ("energy_kcal", "protein_g", "potassium_mg", "phosphorus_mg")}})
        # MED-1（2026-08-15）：缺失营养素食物显式提示——汇总把缺失项按 0 计入
        # 会低估全天摄入（CKD 患儿钾/磷管控尤其危险），须在结果标注"按 0 计"。
        if row.get("missing_nutrients"):
            _labels = {"potassium_mg": "钾", "phosphorus_mg": "磷", "sodium_mg": "钠",
                       "calcium_mg": "钙", "energy_kcal": "能量", "protein_g": "蛋白质",
                       "fat_g": "脂肪", "carb_g": "碳水"}
            missing_foods.append(
                f"「{row['name']}」（缺 {'、'.join(_labels.get(k, k) for k in row['missing_nutrients'])}）")

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
    # P2 其余（2026-08-15）：不足 3 日日记显式警告——1-2 天样本的日均值/磷蛋白比
    # 代表性弱，此前静默输出误导临床判断（评估类工具可能直接消费日均值）。
    if day_count < 3:
        data["warnings"] = (data.get("warnings") or []) + [
            f"日记仅 {day_count} 天（不足 3 天），日均值与磷蛋白比参考性有限，"
            "建议补足 3 天以上再作临床判断。"]
    # v2.4 工具收敛：磷蛋白比（PNPR）作为汇总派生字段输出（原 calc_pnpr 独立工具下沉）。
    avg_protein = average.get("protein_g", 0.0)
    avg_phosphorus = average.get("phosphorus_mg", 0.0)
    if avg_protein and avg_protein > 0:
        ratio = avg_phosphorus / avg_protein
        code, label = pnpr_grade(ratio)
        data["pnpr"] = {
            "pnpr_mg_per_g": round(ratio, 1), "grade": code, "grade_label": label,
            "interpretation": f"日均每摄入 1 g 蛋白质同时带入 {ratio:.1f} mg 磷，"
                              f"该值基于每日平均摄入汇总。",
        }
    # BUG-64：非法日期显式警告（不再静默拆散跨天统计）
    if bad_dates:
        data["warnings"] = (data.get("warnings") or []) + [
            f"{len(bad_dates)} 条记录日期无法归一化为 YYYY-MM-DD（如 {bad_dates[0]!r}），"
            "已按原样分桶，跨天统计可能被拆散；请使用 YYYY-MM-DD 格式。"]
    # MED-1（2026-08-15）：缺失营养素食物汇总提示——缺项按 0 计会低估全天钾/磷，
    # CKD 患儿限钾限磷场景下该低估直接影响临床判断，必须显式警示。
    if missing_foods:
        data["warnings"] = (data.get("warnings") or []) + [
            f"{len(missing_foods)} 种食物存在营养数据缺失（按 0 计，可能低估贡献）："
            + "；".join(missing_foods[:5])
            + ("；等" if len(missing_foods) > 5 else "")
            + "。请谨慎解读汇总，或人工补充数据后重算。"]
    if target:
        # 五审（2026-08-13）：先归一化 PRNT 信封（{ok,data} 嵌套）——此前直接透传
        # 导致目标对照静默为空（achievement.items=[]）。兼容扁平简表不受影响。
        data["achievement"] = _achievement(average, _normalize_target(target))
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
