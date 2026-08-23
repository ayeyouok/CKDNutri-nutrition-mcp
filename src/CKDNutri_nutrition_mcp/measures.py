"""家庭量具与克重的双向换算。

设计目标：家长看到的是"半碗饭""掌心一块肉"，医护看到的是克重与营养素。
本模块把两种表达打通，并保证换算依据可解释（返回 basis 字段）。
"""
from __future__ import annotations

import math
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


def _cn_numeral(s: str) -> float | None:
    """把中文数字前缀（1-9999）解析为数值；不是数字前缀返回 None。

    P1-3（2026-08-18）：复合中文数词此前只取首字——"二十个"被解析成 2 个（10 倍
    低估）、"十二碗"解析成 1 碗。现支持 十/十一~十九/二十~九十九 组合。
    P1-7（2026-08-18 四审）：补"百"（一百/两百/一百零五/一百二十）——"一百克"
    此前解析成 1（"百"不识别），"两百克"静默回退 1 份（2 倍低估）。
    P3（2026-08-23 审查）：补"千"位复合——"一千"/"一千五百"/"两千"此前遇"千"
    提前退出返 1.0，导致 "一千五百克" 被断词为 quantity=1、rest="千五百克" 无法
    识别 → 回落 1 份（100g），实际 1500g 被低估 15 倍。现支持 千/几千几百几十。
    """
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if not s:
        return None
    if s[0] == "十":
        # 十 / 十一~十九
        if len(s) >= 2 and s[1] in digits:
            return float(10 + digits[s[1]])
        return 10.0
    if s[0] in digits:
        if len(s) == 1:
            return float(digits[s[0]])
        if s[1] == "千":
            # P3（2026-08-23 审查）：千复合——"一千"/"两千"/"一千五百"/"一千二百三十"。
            # 此前遇"千"提前退出返 1.0（如"一千五百克"→quantity=1、rest="千五百克"
            # 无法识别 → 回落 1 份 100g，实际 1500g 被低估 15 倍）。
            base = digits[s[0]] * 1000
            rest = s[2:]
            if not rest:
                return float(base)
            # 千后直接跟百部（"一千五百"）/ 十部（"一千二十"）/ 个位（口语"一千五"）
            rest_val = _cn_numeral(rest)
            if rest_val is not None:
                return float(base + rest_val)
            return float(base)
        if s[1] == "百":
            # 一百 / 两百 / 一百零五 / 一百二十 / 一百二十三
            base = digits[s[0]] * 100
            rest = s[2:]
            if not rest:
                return float(base)
            if rest[0] == "零" and len(rest) >= 2 and rest[1] in digits:
                return float(base + digits[rest[1]])
            if rest[0] == "十":
                if len(rest) >= 2 and rest[1] in digits:
                    return float(base + 10 + digits[rest[1]])
                return float(base + 10)
            if rest[0] in digits:
                # P2（2026-08-23）：余部为 "五十"/"二十三" 等复合数词——递归解析其数字
                # 前缀（自动忽略尾随单位"克"）。此前只取 rest[0] 单字，导致
                # "一百五十"→105、"一百二十三"→105 等系统性低估 ~30%。
                rest_val = _cn_numeral(rest)
                if rest_val is not None:
                    return float(base + rest_val)
            return float(base)
        if s[1] == "十":
            # 二十 / 二十三 / 九十
            # P3（2026-08-23 复审）：排除 "两" 作数词——"二十两"是"20 两"（重量单位
            # =1000g），不是 22；此前 s[2]=="两" 被 digits 判定为数字 2，致 二十两→22
            # 、三十两→32，系统性多算 100g（22×50=1100 vs 正确 20×50=1000）。
            # "两"作数词仅用于首字（"两碗"已在 len==1 分支）或复合（"二两"在下方
            # len(s)==2 且 s[1]=="两" 已被第 100 行排除逻辑覆盖）。
            base = digits[s[0]] * 10
            if len(s) >= 3 and s[2] in digits and s[2] != "两":
                return float(base + digits[s[2]])
            return float(base)
        if s[1] in digits and s[1] != "两":
            # 十一~九十九 的无"十"简写（如"三五"），按十位+个位。
            # P2（2026-08-23）：排除 "两"——"二两"是"2 两"（重量单位=100g），不是 22；
            # "两"作数词仅用于"两碗"等（首字情形已在上面 len==1 / 非复合分支处理）。
            return float(digits[s[0]] * 10 + digits[s[1]])
        return float(digits[s[0]])
    return None


_CN_NUMERAL_CHARS = frozenset("零一二两三四五六七八九十百千")


def _parse_quantity(text: str) -> tuple[float, str]:
    """从量具串首部剥离数量，返回 (数量, 剩余量词串)。"""
    for word, value in sorted(CN_FRACTION.items(), key=lambda kv: -len(kv[0])):
        if text.startswith(word):
            return value, text[len(word):]
    # P1-3（2026-08-18）：通用分数词 "X分之一"（二分之一/五分之一/十分之一…）——
    # 此前仅枚举了 三分之一/四分之一/四分之三，"二分之一碗"被解析成 2 碗（4 倍高估）。
    _frac = re.match(r"^([零一二两三四五六七八九十]{1,2})分之一", text)
    if _frac:
        n = _cn_numeral(_frac.group(1))
        if n and n > 0:
            return 1.0 / n, text[_frac.end():]
    match = _NUM_RE.match(text)
    if match:
        return float(match.group(1)), text[match.end():]
    if text and text[0] in CN_NUMBER:
        # P1-3（2026-08-18）：复合中文数词（二十/十二/二十三）——此前只取首字
        # （"二十个"→2 个，10 倍低估）。_cn_numeral 解析前缀后按实际字符数剥离。
        # P1-7（2026-08-18 四审）：剥离长度覆盖 百 组合（一百=2 字、一百零五=4 字）。
        cn_val = _cn_numeral(text)
        if cn_val is not None:
            head = cn_val
            head_len = 0
            for _ch in text:
                if _ch in _CN_NUMERAL_CHARS:
                    head_len += 1
                else:
                    break
        else:
            head = CN_NUMBER[text[0]]
            head_len = 1
        rest = text[head_len:]
        if rest.startswith("点") and len(rest) > 1 and rest[1] in CN_NUMBER:
            return head + CN_NUMBER[rest[1]] / 10.0, rest[2:]
        if rest.startswith("半"):
            return head + 0.5, rest[1:]
        # P1-3（2026-08-18）："X个半"（2.5 份）——数量后跟量词再跟"半"，如"两个半"。
        # 此时 rest="个半"，半份附在量词后：0.5 并入数量、量词保留。
        if len(rest) >= 2 and rest.endswith("半"):
            return head + 0.5, rest[:-1]
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
        if not token:
            continue
        # 同时剥离前缀与后缀食物名——上游大模型/意图识别常传递「食物名前置」份量
        # （如「米饭2碗」「苹果3个」），此前只剥后缀，前缀残留导致 _parse_quantity
        # 首字符非数字、数量截断为 1.0（2 碗米饭被算 1 碗，摄入腰斩 / 低估 50%+）。
        # BUG-P0-01（2026-08-23）：仅 endswith 剥离时「米饭2碗」→ text 仍含「米饭」，
        # 数量被静默回落 1 份，能量/钾磷系统性低估。
        if text.endswith(token) and text != token:
            text = text[: -len(token)]
        elif text.startswith(token) and text != token:
            text = text[len(token):]
    text = text or "1份"

    # P3 复审 LOW-加固（2026-08-23 夜审）：负数份量（如 "-10" / "-10g"）此前绕过纯数字
    # 分支（带负号 isdigit()=False）落入回退 100g，被当正向摄入计入（与 BUG11 负数克重
    # 同口径拒绝）。统一在入口拦截前导负号。
    if text.startswith("-"):
        return {"grams": 0.0, "resolved": False,
                "basis": f"份量「{portion}」含前导负号，已按 0 处理（录入错误）"}

    # P3（2026-08-23 复审）：纯数字无单位（如 "150"）直接作为克重——此前落入
    # unit is None 分支按 "份"×unit_grams（150×150=22500g），单次进食能量飙至数万
    # 千卡。纯阿拉伯数字串无量词语义，明确按克重直出（与 "150g" 行为一致）。
    # 注意 diary 入口已把字符串数字转 float 走克重分支，此处兜底 measures 被直接
    # 调用（mealplan/量具工具）时的边界；中文数词（"一千五百"）不在此列——它们
    # 无阿拉伯数字，交给下方 _parse_quantity 按量词/单位解析。
    if text.replace(".", "", 1).isdigit():
        val = float(text)
        if val < 0:
            return {"grams": 0.0, "resolved": False,
                    "basis": f"纯数字份量 {val:.0f} 为负，已按 0 处理（录入错误）"}
        return {"grams": val, "resolved": True,
                "basis": f"按输入数值直接作为克重 {val:.0f} g 计"}

    # P2-4（2026-08-18）：尾部括号解析——
    # ① "1碗(200g)"：括号内克重为**权威值**（此前被无视，按碗 150g 计，摄入低估 25%）；
    # ② "30g(干)"：括号内无克重（规格说明）→ 剥离括号后按常规解析（此前整体回落 1 份，
    #    30g 被记成 100g，3 倍高估）。
    _trail = re.search(r"[（(]([^）)]*)[)）]$", text)
    if _trail:
        _inner = _trail.group(1).strip()
        _gm = re.match(r"^(\d+(?:\.\d+)?)\s*(g|克|ml|毫升)$", _inner, re.IGNORECASE)
        if _gm:
            _v = float(_gm.group(1))
            return {"grams": _v, "resolved": True,
                    "basis": f"按括号标注克重 {_v:.0f} g 计（{text}）"}
        text = (text[:_trail.start()] or text).strip()

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
    # P1-7（2026-08-18 四审）：中文数量 + 克（"一百克"→100g、"两百克"→200g）——
    # 克 不在通用量具表（GENERIC_UNIT_GRAMS），此前静默回落 1 份（"两百克"被记成
    # 100g，2 倍低估）；数量即克重直出。
    if rest in ("克", "g", "G"):
        return {"grams": quantity, "resolved": True,
                "basis": f"按中文数量「{text}」计 {quantity:.0f} g"}
    # BUG10 修复（2026-08-23）：中文数词 + 重量单位"千克/公斤"（如"一千克""两公斤"）
    # 此前因"千/公斤"不在 GENERIC_UNIT_GRAMS 且 _GRAM_RE 要求阿拉伯数字开头，
    # 中文数词前缀解析后 rest="千克" 无法识别 → 回落 1 份（100g），1000g 被低估 10 倍。
    # 此处将"千克/公斤"识别为 ×1000 重量后缀（与 _GRAM_RE 对阿拉伯数字"1千克"的处理一致）。
    if rest in ("千克", "公斤"):
        grams = quantity * 1000.0
        return {"grams": grams, "resolved": True,
                "basis": f"按中文数量「{text}」计 {grams:.0f} g（{quantity:.0f} {rest}）"}
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
    # P1-6（2026-08-18 四审）：NaN/Inf/bool 阻断——NaN 克重产出 NaN count 与 NaN
    # 替代项（下游展示/换算污染）；显式拒绝（调用方 diary 入口已有克重校验）。
    if not isinstance(grams, (int, float)) or isinstance(grams, bool):
        raise ValueError(f"grams 必须为数值（收到 {grams!r}）")
    if not math.isfinite(grams):
        raise ValueError(f"grams 必须为有限数值（收到 {grams!r}），NaN/Inf 拒绝")
    # BUG11 修复（2026-08-23）：负数克重此前漏校验，输出"约 -0.5份"异常文本。
    # 与 scale_nutrients 同口径（grams < 0 显式拒绝）。
    if grams < 0:
        raise ValueError(f"grams 不能为负（收到 {grams!r}）——负克重通常是录入错误")
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
