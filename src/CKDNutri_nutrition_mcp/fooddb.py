"""内置食物成分表的加载、检索与分级。

数据文件：data/food_data.csv，每 100 g 可食部。本模块只做数据访问与派生计算，
不含任何工具级业务编排（业务在 diet.py / targets.py）。
"""
from __future__ import annotations

import csv
import difflib
import math
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
                    # 潜在 4（2026-08-14）：别名 strip 防御——此前 split(";") 未 strip，
                    # 含前导/尾随空格/全角空格的别名无法匹配（实测当前 CSV 无此脏数据，
                    # 属防未来数据回归的低成本防御）。
                    "aliases": [a.strip() for a in (raw.get("aliases") or "").split(";") if a.strip()],
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
                # MED-1（2026-08-15）：缺失营养素不得静默当 0——CKD 患者把"未知钾"
                # 误当"无钾"会低估全天钾摄入。缺失格仍按 0 参与数值计算（保持下游
                # 契约不变），但记录 missing_nutrients 标记，由 food_warnings /
                # sum_diet_intake 显式提示"数据缺失按 0 计，请谨慎"。
                # H1（2026-08-15）：缺失判定升级——字面 0.0 也是缺失信号（真实食物
                # 钾/磷/钠/钙不可能同时为 0，68 行四电解质全 0 是 CSV 数据缺失而非
                # 真实零值）。仅"四电解质全 0/空"判缺失；单列钠=0 不判（谷物/天然
                # 食材低钠是成分表正常标注，51% 钠 0 属正常分布）。
                _raw_k = (raw.get("potassium_mg") or "").strip()
                _raw_p = (raw.get("phosphorus_mg") or "").strip()
                _raw_na = (raw.get("sodium_mg") or "").strip()
                _raw_ca = (raw.get("calcium_mg") or "").strip()
                if all(v in ("", "0", "0.0")
                       for v in (_raw_k, _raw_p, _raw_na, _raw_ca)):
                    row["missing_nutrients"] = ["potassium_mg", "phosphorus_mg",
                                                "sodium_mg", "calcium_mg"]
                else:
                    row["missing_nutrients"] = [k for k in NUTRIENT_KEYS
                                                 if not (raw.get(k) or "").strip()]
                    # P0-1（2026-08-18）：**单列字面 0 判定**——钾/磷是真实食物必有成分
                    # （荔枝(干) 真实 K≈900、藜麦/鸭蛋白蛋白>0 必有磷），单列 K=0/P=0 是
                    # CSV 数据缺失而非真实零值；此前只判"四电解质全 0"（H1），单列 0 落空
                    # → 低钾 top1 荔枝(干) K=0 被当低钾推荐（真实高钾，56 倍级误导）、
                    # 低磷 top1 藜麦 P=0 同理，且 foods.py 的 missing_nutrients 防线不命中。
                    # 钠单列 0 **不判**（H1 保留：谷物/天然食材低钠是成分表正常标注，
                    # 51% 钠 0 属正常分布）。空串分支上面已覆盖，此处仅补字面 0。
                    for _k in ("potassium_mg", "phosphorus_mg"):
                        if (raw.get(_k) or "").strip() in ("0", "0.0") \
                                and _k not in row["missing_nutrients"]:
                            row["missing_nutrients"].append(_k)
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
    P1-1（2026-08-18）：**括号归一化**——foods_ckd.json 显示名/用户输入常用半角
    （"米粉(熟)"），主表全角（"米粉（熟）"），半角不归一导致"米粉(熟)"模糊命中
    "米粉"（干）349 kcal 错行 3.2 倍；比较前统一半角→全角。
    """
    q = query.replace("(", "（").replace(")", "）").strip()
    best = 99.0
    # 1) 别名：只允许精确匹配（方言词入别名后即精确命中，杜绝子串误伤）
    for alias in row["aliases"]:
        if alias == q or alias == query:
            return 0.0
    # 2) 主名：保留前缀/子串/相似度（名称是描述性短语，模糊匹配合理）
    name = row["name"]
    norm_name = name.replace("(", "（").replace(")", "）")
    if norm_name == q:
        best = 0.0
    elif norm_name.startswith(q) or q.startswith(norm_name):
        best = min(best, 1.0)
    elif q in norm_name:
        best = min(best, 2.0)
    elif norm_name in q:
        best = min(best, 3.0)
    else:
        ratio = difflib.SequenceMatcher(None, q, norm_name).ratio()
        # 阈值收紧到 0.80：1752 食物下表，过低会张冠李戴（如“猪瘦肉”误匹配“猪肉脯”）。
        if ratio >= 0.80:
            best = min(best, 4.0 + (1.0 - ratio) * 10.0)
    return best


def search_food(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """按名称/别名检索，返回按匹配度升序的候选行。

    P0-6 修复（2026-08-13）：**单字符查询拒绝**——中文 1 字（"鱼"/"蛋"/"肉"）在
    1752 行表里必然子串命中多个不相关食物（实测"鱼"→鱼腥草叶、"蛋"→蛋清肠、
    "肉"→肉桂），张冠李戴到错误营养值比报错更危险。要求 ≥2 字符；数字查询（如
    "100g"）同理拒绝。返回空列表由调用方转 INVALID_INPUT / 提示细化关键词。
    """
    text = (query or "").strip()
    if not text:
        return []
    # P1-2（2026-08-18）：单字符仅允许**精确命中**（别名/主名 == 查询，如显示名"梨"→
    # 梨（代表值）别名）——P0-6 的"单字符拒绝"防的是子串误伤（"鱼"→鱼腥草叶），
    # 精确命中是数据层已声明的别名关系，安全放行。
    if len(text) < 2:
        exact = [row for row in load_foods()
                 if row["name"] == text or text in row["aliases"]]
        return exact[:limit]
    # 数字串（如 "123"）不是食物名，拒绝
    if text.isdigit():
        return []
    scored = [(row, _match_score(row, text)) for row in load_foods()]
    # N-S3 修复（2026-08-14）：同分排序 = 代表值优先 → 名称短优先 → 名称升序。
    # 此前 (score, name) 按名称升序，全角括号码点（U+FF08）> 汉字码点，导致
    # "苹果梨"排在"苹果（代表值）"前（苹果→苹果梨 K=180 误匹配）；代表值行
    # 是中国食物成分表的通用主条目，必须优先于变体。
    # P1-2（2026-08-18）：排序键增 **base_name 精确** 优先级——此前"粳米"在
    # 粳米（标一）与粳米粥 之间同分（1.0 前缀），名称短者（粳米粥 3 字）胜出，
    # 干粮被替换成粥（K 13 vs 97 低估）；base_name == 查询 的规格行（粳米（标一）
    # base=粳米）优先。同理"猪蹄筋(泡发)" base=猪蹄筋 优先于 猪蹄（子串 3.0 同分）。
    hits = sorted([item for item in scored if item[1] < 90.0],
                  key=lambda x: (x[1],
                                 0 if base_name(x[0]["name"]) == base_name(text) else 1,
                                 0 if "代表值" in x[0]["name"] else 1,
                                 len(x[0]["name"]), x[0]["name"]))
    return [row for row, _ in hits[:limit]]


def find_food(query: str) -> dict[str, Any] | None:
    """单条精确检索：返回匹配度最高的一行。

    N-S2 修复（2026-08-14）：此前同基名多规格一律进"冲突检查"，把**生物变异**
    （红皮/白皮鸡蛋钾 98-244、香蕉/红皮 208-256）与**真数据冲突**（松蘑 93 vs 2402）
    同等对待 → 常见食物（鸡蛋/香蕉/豆腐/猪肉（瘦））全部 FOOD_NOT_FOUND，
    日记营养统计系统性低估。修复后按三级判定：
      1. **精确命中短路**：别名精确匹配或主名 == 查询词 → 直接返回该行，
         不参与基名分组（如"猪肉（瘦）"有精确行，此前被"猪肉"基名组拖入冲突拒绝）；
      2. **代表值优先**：组内含"代表值"行 → 返回代表值（中国食物成分表通用主条目）；
      3. **变异 vs 冲突**：组内关键电解质（K/P/Na）max/min ≤ 3 倍 → 生物变异，
         返回营养最全行；任一 > 3 倍 → 真数据冲突（松蘑 93 vs 2402），返回 None
         （调用方提示"名称有多个规格，请细化"，不猜——猜错营养值比报错更危险）。
    """
    text = (query or "").strip()
    if not text:
        return None
    hits = search_food(text, limit=10)
    if not hits:
        return None
    # 1) 精确命中短路（别名精确 0.0 或主名 == 查询）
    for r in hits:
        if _match_score(r, text) == 0.0:
            return r
    # 2) 以 Top1 候选基名建立同源食材组，代表值优先仅在**组内**生效
    #    （BUG-02，2026-08-23：原实现在全局 hits 上扫描「代表值」，可能跨食材劫持——
    #     返回非同源类的「X（代表值）」；与 N-S2 文档「组内代表值」契约一致，且对正常
    #     查询行为完全等价，仅消除跨组劫持的潜在路径）。
    target_base = base_name(hits[0]["name"])
    group = [r for r in hits if base_name(r["name"]) == target_base]
    for r in group:
        if "代表值" in r["name"]:
            return r
    # 3) 基名分组：生物变异 vs 真数据冲突（沿用上述 group）
    if len(group) > 1:
        # 3.5) B 方案（2026-08-14）：加工状态差异 ≠ 数据冲突——同基名但**名称不同**
        # （如"榛蘑（干）"vs"榛蘑（水发）"K 4629 vs 732）是干/水发/熟/生的正常
        # 数值差异（水分稀释），此前被 step 3 的倍差>3 判为"数据冲突"→ 返回 None，
        # 榛蘑/木耳等常见干制食材全部 FOOD_NOT_FOUND。按状态优先级返回：
        # 干 > 生/鲜 > 熟 > 水发（CKD 限钾磷场景以权威干品/生品值优先，水发值最低）。
        distinct_names = {r["name"] for r in group}
        if len(distinct_names) > 1:
            def _state_rank(r: dict[str, Any]) -> int:
                n = r["name"]
                if "干" in n:
                    return 0
                if "生" in n or "鲜" in n:
                    return 1
                if "熟" in n:
                    return 2
                if "水发" in n:
                    return 3
                return 4
            # P0-1（2026-08-18）：代表值优先选"缺失字段最少"的行——否则「全脂奶粉」会被
            # 解析成 P=0 的羊乳粉变体（缺失全四电解质），基准营养素按 0 算，派生计算全错。
            def _completeness(r: dict[str, Any]) -> int:
                return len(r.get("missing_nutrients", []))
            return min(group, key=lambda r: (_completeness(r), _state_rank(r)))
        # 4) 精确重名（名称完全相同）才做真数据冲突判定——CSV 已修正无精确重名，
        # 保留兜底防未来数据回归。
        _ELECTROLYTES = ("potassium_mg", "phosphorus_mg", "sodium_mg", "calcium_mg")
        complete = [r for r in group if any(r[k] > 0 for k in _ELECTROLYTES)]
        if len(complete) == 1:
            return complete[0]  # 唯一完整行（其余为电解质缺失脏行）
        if len(complete) > 1:
            conflict = False
            for key in ("potassium_mg", "phosphorus_mg"):
                vals = [r[key] for r in complete if r[key] > 0]
                if len(vals) >= 2 and max(vals) / min(vals) > 3.0:
                    conflict = True  # 如松蘑 93 vs 2402（26 倍）
                    break
            if conflict:
                return None  # 数据源冲突，拒绝猜测
            # 生物变异（同量级）：返回电解质最全行（K/P/Na/Ca 非零键最多）
            return max(complete, key=lambda r: sum(1 for k in _ELECTROLYTES if r[k] > 0))
    return hits[0]


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
    # LOW-5（2026-08-15）：负克重不得静默归 0——此前 max(grams, 0.0) 把录入错误
    # （如 -50g）当 0 克处理，产出"本次 0 营养"的假安全结果。显式拒绝（INVALID_INPUT
    # 语义），调用方（diary/foods）在入口已有克重校验，此处为第二道防线。
    # P1-6（2026-08-18 四审）：NaN/Inf 阻断——`isinstance(nan, float)` 与 `nan < 0`
    # 恒 False，NaN 克重此前穿透产出 NaN 营养素（污染下游聚合/PEW）；有限性校验。
    if not isinstance(grams, (int, float)) or isinstance(grams, bool):
        raise ValueError(f"grams 必须为数值，收到 {grams!r}")
    if not math.isfinite(grams):
        raise ValueError(f"grams 必须为有限数值（收到 {grams!r}），NaN/Inf 拒绝")
    if grams < 0:
        raise ValueError(f"grams 不能为负（收到 {grams}）——负克重通常是录入错误")
    ratio = grams / 100.0
    raw_method = (cooking or "").strip()
    # P2-5（2026-08-18）：**组合烹调**支持——"焯水+浸泡"等用 "+"（全角/半角）连接的
    # 组合此前未命中 COOKING_ALIAS 回落 raw（临床最常用的"先焯后泡"被当生食，钾保留
    # 系数 1.0 高估）；现拆分为多段分别取系数后**相乘**（顺序无关，各段系数独立）。
    segments = [s.strip() for s in re.split(r"[+＋]", raw_method) if s.strip()]
    method = None
    factors: dict[str, float] = {}
    cooking_note: str | None = None
    _labels: list[str] = []
    _is_combination = False
    if len(segments) > 1:
        all_known = True
        temp_factors: dict[str, float] = {}
        for seg in segments:
            m = COOKING_ALIAS.get(seg)
            if m is None or m not in COOKING_LOSS:
                all_known = False
                break
            f = COOKING_LOSS[m]["factor"]
            for k, v in f.items():
                temp_factors[k] = temp_factors.get(k, 1.0) * v
            _labels.append(COOKING_LOSS[m]["label"])
        # BUG-P0-02（2026-08-23）：此前 factors 在循环中原地累积，遇到未知段 break 后
        # factors 已被部分污染（非空），导致下面 `if not factors` 不触发、降级 raw 后
        # 仍套用污染的 0.7 折减系数（如「焯水+红烧」红烧未知 → 焯水系数残留套用到生食）。
        # 现用 temp_factors，仅当**全部段已知**才提交，否则保持 factors 为空 → 按 raw 回落。
        if all_known:
            factors = temp_factors
            method = "+".join(_labels)
            _is_combination = True
    if method is None:
        method = COOKING_ALIAS.get(raw_method, raw_method or "raw")
    # 组合 method 是 label 拼接串（非 COOKING_LOSS 键），跳过回落判定
    if not _is_combination and method not in COOKING_LOSS:
        # P2 其余（2026-08-15）：未知 cooking 静默回落 raw（系数 1.0，如"蒸"被当生食
        # 算）会高估钾磷实际摄入——回落保留（数值契约）但显式标记，供日记层提示。
        cooking_note = (f"烹调方式「{cooking}」不在受支持集合"
                        f"（{'/'.join(COOKING_LOSS)}），已按生食（raw，保留系数 1.0）计算，"
                        "请核实输入")
        method = "raw"
    if not factors:
        factors = COOKING_LOSS[method]["factor"]
    out: dict[str, Any] = {"grams": round(grams, 1), "cooking": method,
                           "cooking_label": method if "+" in method
                           else COOKING_LOSS[method]["label"]}
    if cooking_note:
        out["cooking_warning"] = cooking_note
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
    # MED-1（2026-08-15）：缺失营养素显式提示——CKD 患者把"未知钾"误当"无钾"
    # 会低估全天摄入，警示不触发。缺失格数值按 0 计，但必须告诉使用者谨慎。
    missing = row.get("missing_nutrients") or []
    if missing:
        labels = {"potassium_mg": "钾", "phosphorus_mg": "磷", "sodium_mg": "钠",
                  "calcium_mg": "钙", "energy_kcal": "能量", "protein_g": "蛋白质",
                  "fat_g": "脂肪", "carb_g": "碳水"}
        names = "、".join(labels[k] for k in missing if k in labels)
        notes.append(f"{row['name']} 的 {names} 数据缺失（当前按 0 计），"
                     "该食物此项摄入可能被低估，请谨慎使用/人工补充数据。")
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
