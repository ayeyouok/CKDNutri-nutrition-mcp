# -*- coding: utf-8 -*-
"""M3 核心逻辑（纯函数，无 fastmcp 依赖，可单测）。

内容：
1. PRNT 2020 能量/蛋白质 SDI 目标引擎（按年龄×性别分段 + CKD 分期/透析/素食/生长状态调整）
2. 3 日饮食日记聚合（diet_diary_3d）
3. 摄入达成率与 PEW（蛋白质-能量消耗）风险筛查

数据来源（权威标尺，Wave 2 以本文件为准）：
  Shaw V, Polderman N, Renken-Terhaerdt J, et al. Energy and protein requirements for
  children with CKD stages 2-5 and on dialysis - clinical practice recommendations from
  the Pediatric Renal Nutrition Taskforce. Pediatr Nephrol. 2020;35:519-531.
"""
from __future__ import annotations

import json
import math
import threading
import os
import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from a207_policy import (
    enforce_nutrition_tool,
    PARENT_ROLE,
    get_caller,
    validate_patient_id,
    verify_guardian_token,
)
from .constants import DIALYSIS_ALIAS
from .nutrition_repository import DIARY_STORE_FILENAME, PEW_STORE_FILENAME

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
GUIDELINE = "PRNT 2020 (Shaw et al., Pediatr Nephrol 35:519-531)"
MCP_NAME = "CKDNutri-nutrition-mcp"

# P1-3：运行时写库不落安装目录；v0.5 起存储统一经 nutrition_repository DAO
# （默认 Tablestore；json 开发模式落 A207_NUTRITION_ASSESSMENT_DATA_DIR / A207_DATA_DIR）。

# BUG-53（2026-08-12）：日记/PEW 存储 read-modify-write 并发保护（与 P3 care 同口径）。
# v0.5（2026-08-13）：存储经 nutrition_repository DAO 访问（默认 Tablestore，json 开发模式）；
# _STORE_LOCK 保留为进程内优化（LocalJson 端 RMW 串行化；Tablestore 端减少版本冲突）。
_STORE_LOCK = threading.Lock()


def _require(value: Any, name: str) -> Any:
    """入口参数校验（F6 + P0-7 修复 2026-08-13）：必填数值/参数传 None、NaN、Inf 时
    显式抛出域错误，避免下游 TypeError 或 NaN 静默穿透。

    配合 server 层的 try/except → _invalid()，最终以 {ok:False, error:"INVALID_INPUT"}
    信封返回，而非把未捕获的 TypeError 暴露给调用方。

    P0-7：此前只拦 None——`NaN < 0` 恒为 False，calc_growth_zscore(NaN) 会静默产出
    "成人参照 Z 分"、assess_pew_risk(NaN) 会"PEW 恒判 low"，比报错危险得多。
    """
    if value is None:
        raise ValueError(f"{name} 不能为 None")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        import math

        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"{name} 必须为有效的有限数值，收到 {value!r}")
    return value


def _repo():
    """数据访问层入口（延迟导入避免循环引用）。"""
    from .nutrition_repository import get_repository

    return get_repository()


# BUG-18：DIARY_STORE/PEW_STORE 不再在模块加载时固化路径（env 变化需重启才能生效），
# 改为每次读写时解析，测试/部署中切换 A207_DATA_DIR 立即生效。
# 路径解析与 fail-closed 校验已下沉到 nutrition_repository（LocalJson 后端）。
# 常量保留导出（smoke 测试引用），实际存储经 _repo()。

# 写权判定经 enforce_nutrition_tool 工具级 ACL（P1-1：单一事实源在 a207_policy），
# 本包不再维护本地写白名单（2026-08-12 双轨制清理）。

# P2-1（2026-08-18）：日记餐次白名单——此前 meal 零校验任意值落库（幂等键混入
# dict/list 还会抛不可哈希 500）；儿科 CKD 日记餐次收敛为四档。
_MEAL_TYPES: frozenset[str] = frozenset({"早餐", "午餐", "晚餐", "加餐"})

# ---------------------------------------------------------------------------
# 家长-患儿绑定核验（与 P1 his.py 共享 guardian_tokens.json 状态库）
# 需求：家长受限视图必须经监护人令牌绑定核验（同 HIS get_labs 的 _guard_guardian）。
# 2026-08-12 修复：此前 M3 饮食日记读写均无绑定校验，家长可跨患者读写任意患儿日记。
# 令牌由 P1 his.issue_guardian_token（仅 doctor）签发并持久化到
# resolve_state_path("guardian_tokens.json") —— 本包经同一路径读取，零跨包 import。
# BUG-30/36（2026-08-12）：令牌校验统一收敛到 a207_policy.verify_guardian_token
# （含 expires_at 过期校验 + 恒定时间比对 + 旧令牌兼容），删除本地副本——
# 此前本地 _token_matches 无过期校验，令牌轮换后旧令牌在本包仍有效。
# ---------------------------------------------------------------------------


def _guard_guardian(caller: str, patient_id: str, guardian_token: str | None,
                    tool: str) -> dict[str, Any] | None:
    """家长每次读写日记前必须通过绑定核验，缺 token 即拒绝，不给降级视图。

    校验走 a207_policy.verify_guardian_token（统一实现，含过期校验，BUG-30/36）。
    """
    if caller != PARENT_ROLE:
        return None
    if not guardian_token:
        return {"ok": False, "error": "GUARDIAN_UNVERIFIED",
                "detail": f"caller=parent_assistant 调用 {tool} 必须携带 guardian_token"}
    if not verify_guardian_token(patient_id, guardian_token):
        return {"ok": False, "error": "FORBIDDEN",
                "detail": f"guardian_token 与 patient_id={patient_id} 不匹配或已过期"}
    return None

# 素食蛋白倍数：蛋奶素 1.2 / 纯素 1.3（植物蛋白生物利用度低）
# S2（2026-08-12 五包审查）：补 "lacto_ovo" 同义别名键——server 层 docstring 长期写
# "lacto_ovo" 而本表只有 "ovo_lacto"，调用方按文档传 lacto_ovo 会被旧逻辑静默降级
# mixed（蛋白需求低估 20%）。两键同值，均合法。
_VEG_MULT = {"mixed": 1.0, "ovo_lacto": 1.2, "lacto_ovo": 1.2, "vegan": 1.3}

# 透析蛋白额外补充（g/kg/day）：PD 0.15-0.3，HD 0.1
_DIALYSIS_EXTRA = {
    "none": (0.0, 0.0),
    "hemodialysis": (0.10, 0.10),
    "peritoneal": (0.15, 0.30),
}

# --- N-S1 临床要点（2026-08-14，权威：PRNT 2020 Shaw et al. / 2025 综述重印 Table 1）---
# PAL（体力活动水平）：SDI 能量基于较低 PAL——1-3 岁 1.4、4-9 岁 1.6、10-17 岁 1.8
# （CKD 儿童体力活动普遍偏低，取各指南 PAL 下限）。
_PAL_BY_AGE: tuple[tuple[float, float], ...] = (
    (0.0, 1.4), (3.0, 1.4), (4.0, 1.6), (9.0, 1.6), (10.0, 1.8), (18.0, 1.8),
)

# PD 透析液蛋白丢失参考（Quan & Baum 1996；婴儿高、青少年低）——信息性提示，
# 不改变默认补充量（KDOQI 2009：PD 0.15-0.30 / HD 0.10 g/kg/day 仍为权威）。
_PD_LOSS_REF = {"infant_g_per_kg": 0.28, "adolescent_g_per_kg": 0.10}


def _pal_for_age(age_years: float) -> float:
    """按年龄取 PAL（1-3 岁 1.4 / 4-9 岁 1.6 / 10-17 岁 1.8；<1 岁按 1.4、≥18 按 1.8）。

    M1 修复（2026-08-14）：此前 `if age_years < age_min: return pal` 返回"下一个边界
    的值"——(3.0, 1.4) 与 (9.0, 1.6) 两表项**永远不可达**，3.0-3.99 岁错得 1.6
    （应 1.4）、9.0-9.99 岁错得 1.8（应 1.6）。改为区间查找：age 落在
    [age_min_i, age_min_{i+1}) 返回该带值。
    """
    pal = _PAL_BY_AGE[0][1]
    for age_min, p in _PAL_BY_AGE:
        if age_years >= age_min:
            pal = p
        else:
            break
    return pal

# --- Schofield / 水肿 / 腹透葡萄糖（从 M5 移植，使 M3 成为唯一 PRNT 权威引擎）---
# 移植目的：去重后 M5 不再提供目标计算，但其 Schofield 交叉校验、水肿理想体重校正、
# 腹透葡萄糖吸收扣减等临床特性有价值，故并入 M3，保证单一权威引擎不丢能力。
# 权威来源：Schofield WN. Predicting basal metabolic rate: new standards and review of
# previous work. Hum Nutr Clin Nutr 1985; 39C Suppl 1: 5-41（FAO/WHO/UNU 1985 采用）。
# 系数为 kcal/day 版（W kg、H cm）换算到 MJ/day（H 米，÷239.0064，×100 对 H）：
#   男 0-3:  0.167W + 15.174H - 617.6   → (0.0007, 6.349, -2.584)
#   男 3-10: 19.59W +  1.303H + 414.9   → (0.082, 0.545, 1.736)
#   男 10-18:16.25W +  1.372H + 515.5   → (0.068, 0.574, 2.157)
#   女 0-3: 16.252W + 10.232H - 413.5   → (0.068, 4.281, -1.730)
#   女 3-10:16.969W +  1.618H + 371.2   → (0.071, 0.677, 1.553)
#   女 10-18:8.365W +  4.65H  + 200.0   → (0.035, 1.948, 0.837)
# MED-1 修正（2026-08-15）：男 10-18 段此前 (0.071, 2.132, -1.184) 换算错误（反推 kcal
# 版 ≈ 17.0W + 5.1H - 283，非权威 16.25W + 1.372H + 515.5）→ 10-18 岁男孩 BMR 低估
# 10-25%（35kg/140cm 十岁男孩 1024 vs 权威 1276 kcal/d），schofield_cross_check 偏差
# 偏正系统性误报 divergent。已按权威修正。0-3 男段经权威验证正确（0.167/15.174/-617.6
# 的精确 MJ 换算），未改动。
SCHOFIELD = {
    ("M", 3): (0.0007, 6.349, -2.584),
    ("M", 10): (0.082, 0.545, 1.736),
    ("M", 18): (0.068, 0.574, 2.157),
    ("F", 3): (0.068, 4.281, -1.730),
    ("F", 10): (0.071, 0.677, 1.553),
    ("F", 18): (0.035, 1.948, 0.837),
}
PAL_DEFAULT = 1.4          # CKD 患儿常见轻体力活动系数
KJ_PER_KCAL = 4.184
BMI_P50 = {
    2: (16.4, 16.1), 3: (16.0, 15.7), 4: (15.8, 15.4), 5: (15.5, 15.3),
    6: (15.5, 15.3), 7: (15.7, 15.5), 8: (16.0, 15.9), 9: (16.4, 16.4),
    10: (16.9, 16.9), 11: (17.4, 17.5), 12: (18.0, 18.2), 13: (18.6, 18.9),
    14: (19.3, 19.6), 15: (19.9, 20.2), 16: (20.5, 20.7), 17: (21.1, 21.1),
    18: (21.7, 21.4),
}
# 八审（2026-08-16）：GLUCOSE_KCAL_PER_G / PD_ABSORB_ANCHORS / PD_TRANSPORT_FACTOR /
# PD_GLUCOSE_KCAL_PER_KG_REF 曾在此重复定义——单一事实源在 constants.py（targets.py
# 已正确从 constants 导入），core 侧 4 个副本为**死常量**（全仓无引用，纯漂移风险：
# 改 constants 不动 core 即出现两份不同数值）。已删除，统一从 constants 引用。


# ---------------------------------------------------------------------------
# ---- WS/T 586-2018 学龄儿童青少年 BMI 超重界值（M-3，2026-08-15）-------------
# 表 1：6 岁~18 岁性别年龄别 BMI 筛查超重/肥胖界值（kg/m²），实足年龄半岁档。
# 此前代码用 BMI>24 统一粗判——7 岁男超重界值仅 17.0、12 岁男 20.7，BMI 17-24
# 区间大量漏判（7 岁男 BMI 18 已超重却不判）。现按官方年龄×性别别界值。
_WS586_OVERWEIGHT: dict[tuple[float, str], float] = {
    (6.0, "M"): 16.4, (6.5, "M"): 16.7, (7.0, "M"): 17.0, (7.5, "M"): 17.4,
    (8.0, "M"): 17.8, (8.5, "M"): 18.1, (9.0, "M"): 18.5, (9.5, "M"): 18.9,
    (10.0, "M"): 19.2, (10.5, "M"): 19.6, (11.0, "M"): 19.9, (11.5, "M"): 20.3,
    (12.0, "M"): 20.7, (12.5, "M"): 21.0, (13.0, "M"): 21.4, (13.5, "M"): 21.9,
    (14.0, "M"): 22.3, (14.5, "M"): 22.6, (15.0, "M"): 22.9, (15.5, "M"): 23.1,
    (16.0, "M"): 23.3, (16.5, "M"): 23.5, (17.0, "M"): 23.7, (17.5, "M"): 23.8,
    (18.0, "M"): 24.0,
    (6.0, "F"): 16.2, (6.5, "F"): 16.5, (7.0, "F"): 16.8, (7.5, "F"): 17.2,
    (8.0, "F"): 17.6, (8.5, "F"): 18.1, (9.0, "F"): 18.5, (9.5, "F"): 19.0,
    (10.0, "F"): 19.5, (10.5, "F"): 20.0, (11.0, "F"): 20.5, (11.5, "F"): 21.1,
    (12.0, "F"): 21.5, (12.5, "F"): 21.9, (13.0, "F"): 22.2, (13.5, "F"): 22.6,
    (14.0, "F"): 22.8, (14.5, "F"): 23.0, (15.0, "F"): 23.2, (15.5, "F"): 23.4,
    (16.0, "F"): 23.6, (16.5, "F"): 23.7, (17.0, "F"): 23.8, (17.5, "F"): 23.9,
    (18.0, "F"): 24.0,
}


def _ws586_overweight_threshold(age_years: float, sex: str) -> float | None:
    """WS/T 586-2018 表 1 BMI 超重界值（半岁档取档，6.0-18.0 岁；超出返回 None）。"""
    if age_years < 6.0 or age_years > 18.0:
        return None
    band = int(age_years * 2) / 2.0          # 实足年龄向下取半岁档
    if band < 6.0:
        band = 6.0
    if band > 18.0:
        band = 18.0
    return _WS586_OVERWEIGHT.get((band, sex))


# PRNT 2020 SDI 表
# 每条：(age_min, age_max, 标签, 能量_M(lo,hi), 能量_F(lo,hi), 蛋白(lo,hi), 每日蛋白总量)
#   婴儿段无性别拆分 → M/F 同值；蛋白总量对 15-17 岁按性别拆分（dict）。
# 单位为：能量 kcal/kg/day；蛋白 g/kg/day；每日总量 g。
#
# N-S1 修复（2026-08-14）：**按 PRNT 2020 Table 1 完整 9 带月龄 + 8 带年岁实现**——
# 此前把年岁段压缩为 4 段：「4-6岁」(3.0,7.0) 吞并 3 岁、「9-10岁」(7.0,12.0) 吞并
# 7-8/11-12 岁、「15-17岁」(12.0,18.01) 吞并 13-14 岁。实测 12 岁女孩落入 15-17 岁段
# （F 36-46，中位 41），权威应为 11-12 岁段（F 43-57，中位 50）→ 能量系统性低估 ~18%。
# 现按权威表精确分段（源：Shaw et al., Pediatr Nephrol 2020;35:519-531, Table 1）。
# M-7（2026-08-16，十一审）：月龄段边界用**精确分数**（1/12 为单位）——此前 4 位
# 截断常量（0.8333 等）比权威边界（10/12=0.83333...）略小，恰处边界附近的月龄
# （如 0.8333 岁 = 9.9996 月 < 10 月）被误分到下一带（10-11月）。实测复现后统一
# 用 N/12.0 精确边界，消除截断误差（0.8333 岁 → 6-9月带，与权威 [6/12,10/12) 一致）。
# ---------------------------------------------------------------------------
_PRNT_BANDS = [
    # 月龄段（0-12 月，BUG-64 逐月拆分保持；边界 = 月龄/12 精确分数）
    (0.0,    1 / 12.0, "0月",             (93, 107), (93, 107), (1.52, 2.50), (8, 12)),
    (1 / 12.0, 2 / 12.0, "1月",           (93, 120), (93, 120), (1.52, 1.80), (8, 12)),
    (2 / 12.0, 3 / 12.0, "2月",           (93, 120), (93, 120), (1.40, 1.52), (8, 12)),
    (3 / 12.0, 4 / 12.0, "3月",           (82, 98),  (82, 98),  (1.40, 1.52), (8, 12)),
    (4 / 12.0, 5 / 12.0, "4月",           (82, 98),  (82, 98),  (1.30, 1.52), (9, 13)),
    (5 / 12.0, 6 / 12.0, "5月",           (72, 82),  (72, 82),  (1.30, 1.52), (9, 13)),
    # N-S1 三审（2026-08-14）：6-9月段上界修正为 10/12 岁（0.8333...）——
    # 此前 (0.5, 0.75) 覆盖 6-9 月龄、10-11月段 (0.75, 1.0) 实际覆盖 9-12 月龄，
    # 把 9 月龄（0.75 岁）错归 10-11 段（蛋白总量 9-15 而非 9-14）。
    # 权威 Month 6-9 = 月龄 6-9（[6/12,10/12)），Month 10-11 = 月龄 10-11（[10/12,12/12)）。
    (6 / 12.0, 10 / 12.0, "6-9月",        (72, 82),  (72, 82),  (1.10, 1.30), (9, 14)),
    (10 / 12.0, 1.0,    "10-11月",        (72, 82),  (72, 82),  (1.10, 1.30), (9, 15)),
    (1.0,    2.0,    "12月龄",          (72, 120), (72, 120), (0.90, 1.14), (11, 14)),
    # 年岁段（N-S1：权威 8 带，整岁边界，逐段精确，不再吞并）
    # 边界口径：12月龄=12-23 月(1.0-2.0)、2岁=24-35 月(2.0-3.0)、3岁=36-47 月(3.0-4.0)、
    # 4-6岁=48-83 月(4.0-7.0)。此前 12月龄只到 1.5、2岁/3岁/4-6岁起点各提前 0.5 岁，
    # 导致 18-23/30-35/42-47 月龄落入错误的下一段（N-S1 二审，2026-08-14）。
    (2.0,    3.0,    "2岁",             (81, 95),  (79, 92),  (0.90, 1.05), (11, 15)),
    (3.0,    4.0,    "3岁",             (80, 82),  (76, 77),  (0.90, 1.05), (13, 15)),
    (4.0,    7.0,    "4-6岁",           (67, 93),  (64, 90),  (0.85, 0.95), (16, 22)),
    (7.0,    9.0,    "7-8岁",           (60, 77),  (56, 75),  (0.90, 0.95), (19, 28)),
    (9.0,    11.0,   "9-10岁",          (55, 69),  (49, 63),  (0.90, 0.95), (26, 40)),
    (11.0,   13.0,   "11-12岁",         (48, 63),  (43, 57),  (0.90, 0.95), (34, 42)),
    (13.0,   15.0,   "13-14岁",         (44, 63),  (39, 50),  (0.80, 0.90), (34, 50)),
    (15.0,   18.01,  "15-17岁",         (40, 55),  (36, 46),  (0.80, 0.90),
     {"M": (52, 65), "F": (45, 49)}),
]


def _round(x: float, n: int = 2) -> float:
    """四舍五入（ROUND_HALF_UP，非银行家舍入），并归一化 -0.0 → 0.0。

    P2（2026-08-18）：原 round() 为银行家舍入（33.25→33.2、2.5→2.0），且浮点表示会
    产出 -0.0 / 1.2300000001 等展示抖动；临床展示应"四舍五入"，避免等级/数值误读。
    """
    if x is None:
        return None
    try:
        d = Decimal(str(x)).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP)
    except Exception:
        return round(x, n)
    f = float(d)
    if f == 0.0:
        f = 0.0  # 归一化 -0.0
    return f


def _band_for_age(age: float, sex: str) -> dict[str, Any]:
    """按年龄选 PRNT 段；≥18 取最后一段。返回结构化字典。"""
    if age < 0:
        age = 0.0
    chosen = _PRNT_BANDS[0]
    for b in _PRNT_BANDS:
        if b[0] <= age < b[1]:
            chosen = b
            break
    else:
        chosen = _PRNT_BANDS[-1]
    age_min, age_max, label, e_m, e_f, prot, prot_total = chosen
    energy = e_m if sex == "M" else e_f
    if isinstance(prot_total, dict):
        prot_total_val = prot_total.get(sex, prot_total.get("M", (0, 0)))
    else:
        prot_total_val = prot_total
    return {
        "label": label,
        "energy_sdi": list(energy),         # [lo, hi] kcal/kg/day
        "protein_sdi": list(prot),          # [lo=floor, hi=target] g/kg/day
        "protein_total_daily": list(prot_total_val),
    }


def _interp(lo_anchor: float, hi_anchor: float, lo_value: float, hi_value: float,
            point: float) -> float:
    if hi_anchor == lo_anchor:
        return lo_value
    ratio = (point - lo_anchor) / (hi_anchor - lo_anchor)
    return lo_value + (hi_value - lo_value) * ratio


def schofield_bmr_kcal(sex: str, age_years: float, weight_kg: float,
                       height_cm: float) -> float | None:
    """Schofield 体重+身高方程，返回 kcal/d。"""
    if weight_kg <= 0 or height_cm <= 0:
        return None
    # bracket 语义（Schofield 年龄分段）：3=<3 岁、10=3-10 岁、18=>10 岁——键 (sex, 分段)
    # 中的数字是"分段标识"而非年龄/系数本身，避免维护者误读为"3 岁专属系数"。
    bracket = 3 if age_years < 3 else (10 if age_years < 10 else 18)
    coefficients = SCHOFIELD.get((sex, bracket))
    if not coefficients:
        return None
    a, b, c = coefficients
    megajoules = a * weight_kg + b * (height_cm / 100.0) + c
    return round(megajoules * 1000.0 / KJ_PER_KCAL, 1)


def ideal_body_weight_kg(age_years: float, sex: str, height_cm: float) -> float | None:
    """按 BMI 第 50 百分位反推参考体重，用于水肿时避免以水重开处方。
    BMI_P50 表仅覆盖 2–18 岁；age_years < 2 时自动夹取 2 岁基准，婴儿体成分与学龄儿童
    差异较大，推算值仅供参考。
    """
    if height_cm <= 0:
        return None
    years = sorted(BMI_P50)
    age = min(max(age_years, years[0]), years[-1])
    lower = max(y for y in years if y <= age)
    upper = min(y for y in years if y >= age)
    index = 0 if sex == "M" else 1
    bmi = BMI_P50[lower][index] if lower == upper else _interp(
        lower, upper, BMI_P50[lower][index], BMI_P50[upper][index], age)
    return round(bmi * (height_cm / 100.0) ** 2, 2)


# ---------------------------------------------------------------------------
# 1. PRNT 2020 目标引擎
# ---------------------------------------------------------------------------
def calc_prnt_targets(
    age_years: float,
    sex: str,
    weight_kg: float,
    height_cm: float = 0.0,
    ckd_stage: int = 1,
    dialysis_mode: str = "none",
    vegetarian_mode: str = "mixed",
    growth_status: str = "normal",
    is_edema: bool = False,
    pd_glucose_kcal_per_day: float | None = None,
    height_age_years: float | None = None,        # N-S1（2026-08-14）：身高年龄（严重生长迟缓按身高年龄查 SDI）
    high_urea_persistent: bool = False,           # N-S1：持续高尿素血症（排除可纠正因素后蛋白降至 SDI 下限）
) -> dict[str, Any]:
    """计算儿童 CKD 每日能量与蛋白质目标（PRNT 2020 权威口径，M3 唯一权威引擎）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。

    能量：初始=SDI 100%；growth_status=failure 取 SDI 上限，overweight 取下限，normal 取中点。
    蛋白质：目标=SDI 上限；绝对下限=SDI 下限（绝不可低于，防 PEW/生长迟缓）；透析额外补充
            叠加于上限与下限之上（补偿透析丢失，避免负氮平衡）；素食按倍数上调。

    N-S1 临床要点（2026-08-14，权威：PRNT 2020 Shaw et al. / theipna 2024 重印）：
      - 透析患者或生长不良/超重/身高年龄/高尿素血症患者，**同时输出两种方案**：
        data.regimens = [standard（标准推荐：正常生长、未透析、SDI 中点/蛋白上限）
                        , adjusted（当前临床调整）]——医生可对照处方。
      - height_age_years：严重生长迟缓者按身高年龄（身高在第 50 百分位对应的年龄）
        查 SDI 段（小年龄 per-kg 需求更高，促进追赶生长）。
      - high_urea_persistent：持续高尿素血症（已排除脱水/高分解代谢/激素治疗等）时，
        蛋白质目标降至 SDI 下限，同时保持能量摄入。
      - data.clinical_notes：权威要点提示（PD/HD 蛋白丢失、PAL 分龄、PEW 保护等）。

    移植自 M5 的临床特性（去重后 M3 成为唯一标尺，能力不丢）：
      - is_edema=True：以 BMI-P50 理想体重（干体重）替代实际体重开处方，避免以"水重"高估需求。
      - pd_glucose_kcal_per_day：腹透患者从透析液吸收葡萄糖供能，等量减少膳食能量目标，避免超额。
      - Schofield 交叉校验：独立估算 BMR（按分龄 PAL），信息性对照 SDI 目标，不覆盖权威数。
    """
    caller = get_caller()
    # BUG-01 修复：临床组工具必须工具级 ACL 收口（仅 doctor），矩阵读权不足以表达
    # "临床判读仅临床角色"（parent 对 P2 拥有 R/W）。
    enforce_nutrition_tool(caller, "calc_prnt_targets")
    _require(age_years, "age_years")
    _require(weight_kg, "weight_kg")
    # BUG-19：sex 非法值不再静默按男性处理，显式报错（calc_prnt_targets 与 calc_growth_zscore 同口径）
    if sex not in ("M", "F"):
        raise ValueError(f"sex 必须是 'M' 或 'F'，收到：{sex!r}")
    # S2（2026-08-12 五包审查）：fail-closed 补齐——
    # ① 负年龄显式拒绝（与 calc_growth_zscore 同口径，不再静默钳位 0.0）；
    # ② weight_kg 必须 > 0（0/负值此前会静默产出 0/负能量目标，无法检出配置错误）；
    # ③ growth_status 枚举校验（此前非法值静默落 else 取 SDI 中点，掩盖配置错误）；
    # ④ vegetarian_mode 枚举校验（此前非法值静默降级 mixed——如拼错的 "ovo-lacto"
    #    会被当杂食计算，蛋白需求低估 20%）。同义别名 lacto_ovo 已在 _VEG_MULT 收录。
    # S4（2026-08-13 六审后补）：age_years/weight_kg 增加**有限性**校验——此前
    # `age_years < 0` 与 `weight_kg <= 0` 对 NaN/Inf 恒 False（NaN 与任何数比较皆 False），
    # NaN 体重会静默产出 NaN 能量/蛋白目标（脏数据参与临床判定且无任何提示）。
    if not math.isfinite(age_years) or age_years < 0:
        raise ValueError("age_years 必须为不小于 0 的有限数值")
    # P0-2（2026-08-18 四审）：PRNT 2020 适用域上限 18——此前 age_years>18 仅附
    # Warning 仍按 15-17 岁段生成完整处方（上游 LLM/调用方极易直接误用成少年目标）；
    # 超域即拒绝（与下方 height_age_years>18 拒绝同口径 fail-closed）。
    if age_years > 18:
        raise ValueError(
            f"age_years={age_years} 超出 PRNT 2020 适用域（0-18 岁）：15-17 岁段参数"
            "不适用于成人，拒绝生成处方（成人营养目标请按成人标准另行计算）")
    if not math.isfinite(weight_kg) or weight_kg <= 0:
        raise ValueError("weight_kg 必须为 > 0 的有限数值")
    # P0-3（2026-08-18）：height_cm 有限性校验——此前仅校验 age/weight，height_cm=NaN
    # 穿透到 schofield_bmr_kcal（`NaN <= 0` 恒 False → 不返回 None）产出 NaN BMR，
    # schofield_cross 输出 NaN + flag="consistent"（非法 JSON 字面量 + 反方向误导）。
    # 0 保留为"未提供"哨兵（schofield 跳过交叉校验，既有语义）；NaN/Inf/负拒绝。
    if not math.isfinite(height_cm) or height_cm < 0:
        raise ValueError("height_cm 必须为不小于 0 的有限数值（0=未提供，跳过 Schofield 交叉校验）")
    if growth_status not in ("normal", "failure", "overweight"):
        raise ValueError(
            f"growth_status 必须是 normal / failure / overweight 之一，收到：{growth_status!r}")
    # P2 其余（2026-08-15）：ckd_stage 零校验（fail-open）——此前传 0/6/99 任意整数
    # 都静默计算（stage 1 警告兜底），超出 1-5 的录入错误被当作有效分期。补范围校验
    # （PRNT 2020 覆盖 CKD 2-5D；1 为沿用，<1/>5 拒绝）。
    if not isinstance(ckd_stage, int) or isinstance(ckd_stage, bool) or not (1 <= ckd_stage <= 5):
        raise ValueError(f"ckd_stage 必须是 1-5 的整数（PRNT 2020 覆盖 1-5），收到：{ckd_stage!r}")
    if vegetarian_mode not in _VEG_MULT:
        raise ValueError(
            f"vegetarian_mode 必须是 mixed / ovo_lacto / vegan 之一（lacto_ovo 与 ovo_lacto "
            f"同义），收到：{vegetarian_mode!r}")
    # N-S1（2026-08-14）：新参数校验——high_urea_persistent 严格 bool（防 "false" 字符串陷阱）；
    # height_age_years 有限性 + 适用域（0-18 岁）。
    if not isinstance(high_urea_persistent, bool):
        raise ValueError(f"high_urea_persistent 必须是 bool，收到：{high_urea_persistent!r}")
    if height_age_years is not None:
        if not math.isfinite(height_age_years) or height_age_years < 0:
            raise ValueError("height_age_years 必须为不小于 0 的有限数值")
        if height_age_years > 18:
            raise ValueError("height_age_years 超出 PRNT 2020 适用域（0-18 岁）")
    # F7：用 DIALYSIS_ALIAS 单一事实源归一化（兼容 pd/腹透/hemodialysis 等别名），
    # 避免裸 _DIALYSIS_EXTRA 成员判断把 "pd" 等别名静默降级为 "none"。
    # 边界（2026-08-15）：**未知非空值拒绝**——此前 DIALYSIS_ALIAS.get(未知)="none"
    # 静默降级：PD 患儿传错值按非透析目标（蛋白/能量低估），与 P4-3 _egfr_to_g
    # 白名单同口径（core 可被编排层直调绕过 server Literal）。
    if dialysis_mode:
        _dm = DIALYSIS_ALIAS.get(str(dialysis_mode).strip().lower())
        if _dm is None:
            raise ValueError(
                f"dialysis_mode 未知：{dialysis_mode!r}（合法值：none / hemodialysis"
                "（血透/hd）/ peritoneal（腹透/pd/capd/apd）），拒绝按非透析静默降级")
        dialysis_mode = _dm
    else:
        dialysis_mode = "none"

    # 是否存在临床调整 → 需要双方案（standard + adjusted）
    need_adjusted = (dialysis_mode != "none" or growth_status != "normal"
                     or height_age_years is not None or high_urea_persistent)

    def _plan(gs: str, dm: str, ha_years: float | None, hup: bool,
              label: str, name: str) -> dict[str, Any]:
        """单方案计算核心：standard（正常生长/未透析/实际年龄/蛋白上限）与 adjusted 共用。

        gs=growth_status；dm=dialysis_mode；ha_years=身高年龄（None=实际年龄）；
        hup=持续高尿素血症。水肿/素食/PD 葡萄糖扣减为患者属性，两方案一致保留。
        """
        band_age = ha_years if ha_years is not None else age_years
        band = _band_for_age(band_age, sex)
        e_lo, e_hi = band["energy_sdi"]
        p_lo, p_hi = band["protein_sdi"]
        veg = _VEG_MULT[vegetarian_mode]
        d_lo, d_hi = _DIALYSIS_EXTRA[dm]
        d_mid = (d_lo + d_hi) / 2.0 if d_hi > d_lo else d_lo

        # 能量取点
        if gs == "failure":
            e_pt = e_hi
            e_basis = "生长不良：向 SDI 上限调整"
        elif gs == "overweight":
            e_pt = e_lo
            e_basis = "超重/肥胖：向下调整以实现适宜体重增长（不损害营养状况）"
        else:
            e_pt = (e_lo + e_hi) / 2.0
            e_basis = "生长正常：取 SDI 中点（约 100% SDI）"
        if ha_years is not None:
            e_basis += f"；按身高年龄 {ha_years} 岁查 SDI（实际年龄 {age_years} 岁）"

        # 水肿校正：用 BMI-P50 理想体重替代实际体重开处方（dry weight 原则，理想体重用实际年龄）
        eff_weight = weight_kg
        weight_basis = "实际体重"
        if is_edema:
            ibw = ideal_body_weight_kg(age_years, sex, height_cm)
            if ibw and ibw > 0:
                eff_weight = ibw
                weight_basis = "水肿校正理想体重(BMI-P50)"
                e_basis += "；水肿：采用理想体重开处方（dry weight 原则）"
            else:
                # P1-7 修复（2026-08-13）：is_edema=True 但身高缺失 → ideal_body_weight_kg
                # 返回 None，此前**静默跳过校正**按水肿体重开处方（能量/蛋白系统性高估）且
                # warnings 为空。现在显式告警，提示补身高以启用 dry weight 校正。
                weight_basis = "实际体重（⚠ 水肿未校正：缺身高无法算理想体重）"
                e_basis += "；水肿但缺身高，未能按理想体重校正（dry weight 未生效），请补身高后复算"

        energy_day = e_pt * eff_weight

        # 腹透葡萄糖供能扣减：PD 患者从腹透液吸收葡萄糖，等量减少膳食能量目标
        pd_deduction = 0.0
        # P1（2026-08-18）：① Inf/NaN 拒绝——inf 会把能量扣成 0，静默低估膳食目标；
        # ② 非透析（dm="none"）不应扣 PD 葡萄糖：无腹透则无葡萄糖吸收，扣减属数据错配
        # （standard 方案标签"未透析"却仍扣 PD，能量目标被错误下压）。
        # P0-1（2026-08-18 四审）：扣减条件收紧为 **dm == "peritoneal"**——此前
        # `dm != "none"` 使 **HD（hemodialysis）患儿也扣 PD 葡萄糖**（血液透析不吸收
        # 透析液葡萄糖，膳食目标被错误下压）；且非 peritoneal 模式传入
        # pd_glucose_kcal_per_day 属数据错配，显式拒绝（严禁带 Warning 的错误计算结果）。
        if pd_glucose_kcal_per_day is not None:
            if not math.isfinite(pd_glucose_kcal_per_day) or pd_glucose_kcal_per_day < 0:
                raise ValueError(
                    f"pd_glucose_kcal_per_day 必须为不小于 0 的有限数值"
                    f"（收到 {pd_glucose_kcal_per_day!r}）")
            # 用**患者级 dialysis_mode**（外层归一化值）判定——_plan 内层 dm 是方案场景
            # 标签（standard 方案恒传 "none"=未透析场景，与患者实际透析状态无关），
            # 用 dm 会把腹膜透析患儿的两个方案都误拒。
            if pd_glucose_kcal_per_day > 0 and dialysis_mode != "peritoneal":
                raise ValueError(
                    f"pd_glucose_kcal_per_day 仅适用于腹膜透析（peritoneal），"
                    f"当前 dialysis_mode={dialysis_mode!r}——HD/未透析患儿不吸收透析液"
                    "葡萄糖，禁止扣减；请核对输入")
            if dialysis_mode == "peritoneal" and pd_glucose_kcal_per_day > 0:
                pd_deduction = float(pd_glucose_kcal_per_day)
                energy_day = max(energy_day - pd_deduction, 0.0)
                e_basis += f"；腹透葡萄糖供能扣减 {_round(pd_deduction, 1)} kcal/day"

        # 蛋白质：默认目标=SDI 上限（促生长）；高尿素血症 → 目标降至 SDI 下限（保能量）；
        # 下限为最低安全量（防 PEW）；透析额外补充叠加（补偿丢失）
        p_hi_eff = p_lo if hup else p_hi
        protein_target_per_kg = p_hi_eff * veg + d_mid
        protein_floor_per_kg = p_lo * veg + d_mid
        protein_target_g = protein_target_per_kg * eff_weight
        protein_floor_g = protein_floor_per_kg * eff_weight
        protein_note = ("目标取 SDI 下限（持续高尿素血症，已排除脱水/分解代谢/激素等可纠正因素）；"
                        "下限即最低安全摄入量，切勿再低以免 PEW/生长迟缓；透析叠加额外补充。"
                        if hup else
                        "目标取 SDI 上限（促生长）；绝对不可低于 SDI 下限(floor)以免 PEW/生长迟缓，"
                        "透析叠加额外补充。")

        # Schofield 交叉校验（信息性，不改变 SDI 权威数）：分龄 PAL×BMR 与 SDI 目标对照
        # BUG-49（2026-08-12）：用 eff_weight（水肿校正后理想体重）而非原始 weight_kg——
        # 水肿患儿的"水重"会让 BMR 虚高、deviation 偏负，误触发 divergent 提示。
        schofield_bmr = schofield_bmr_kcal(sex, age_years, eff_weight, height_cm)
        schofield_cross = None
        if schofield_bmr:
            pal = _pal_for_age(age_years)
            pal_adjusted = round(schofield_bmr * pal, 1)
            deviation = (energy_day - pal_adjusted) / pal_adjusted * 100.0 if pal_adjusted > 0 else 0.0
            schofield_cross = {
                "bmr_kcal_per_day": schofield_bmr,
                "pal": pal,
                "pal_adjusted_kcal_per_day": pal_adjusted,
                "deviation_pct_vs_sdi_target": _round(deviation, 1),
                "flag": "divergent" if abs(deviation) > 25 else "consistent",
                "note": "Schofield 为独立估算，SDI 目标为权威；偏差>25% 提示复核身高/体重/年龄。",
            }

        warnings: list[str] = []
        # 六审（2026-08-13）：超龄显式提示（对齐 assessment adult_caveat）——age>18 会
        # 静默套用 15-17 岁段参数，临床可能误当儿童处方执行。
        if age_years > 18:
            warnings.append(
                f"age_years={age_years} 超出 PRNT 2020 儿童适用域（0-18 岁），目标按 15-17 岁段"
                "参数计算，仅供参考，请改用成人营养指南。")
        if ckd_stage == 1:
            warnings.append("PRNT 2020 覆盖 CKD 2-5D；stage 1 暂沿用同表，请结合临床判断。")
        if dm != "none":
            warnings.append(
                f"透析额外补充蛋白 {_round(d_mid,2)} g/kg/day（PD 0.15-0.30 / HD 0.10），已叠加于目标与下限。"
            )
            if dm == "peritoneal":
                warnings.append(
                    f"PD 透析液蛋白丢失参考：婴儿约 {_PD_LOSS_REF['infant_g_per_kg']}、"
                    f"青少年约 {_PD_LOSS_REF['adolescent_g_per_kg']} g/kg/day（Quan & Baum 1996）。")
        if vegetarian_mode != "mixed":
            warnings.append(
                f"素食模式蛋白需求×{veg}（蛋奶素 1.2 / 纯素 1.3，因植物蛋白生物利用度低）。"
            )
        if is_edema:
            warnings.append(
                f"已启用水肿校正：以理想体重 {_round(eff_weight,1)}kg（BMI-P50）替代实际体重 "
                f"{_round(weight_kg,1)}kg 开处方。"
            )
        if pd_glucose_kcal_per_day is not None and pd_glucose_kcal_per_day > 0:
            warnings.append(
                f"已扣减腹透葡萄糖供能 {_round(pd_deduction,1)} kcal/day，避免能量超额。"
            )
            if dm != "peritoneal":
                warnings.append("提供了腹透葡萄糖供能，但当前非腹膜透析模式，请确认处方场景。")
        if hup:
            warnings.append(
                "持续高尿素血症：蛋白质目标已降至 SDI 下限（需先排除脱水/高分解代谢/激素治疗"
                "等可纠正因素，并保持能量摄入）。")
        if ha_years is not None:
            warnings.append(
                f"严重生长迟缓：按身高年龄（Height Age）{ha_years} 岁查 SDI，"
                f"而非实际年龄 {age_years} 岁——小年龄 per-kg 需求更高，促进追赶生长。")

        return {
            "label": label,
            "name": name,
            "age_band": band["label"],
            "age_band_basis": ("height_age" if ha_years is not None else "chronological"),
            "sex": sex,
            "ckd_stage": ckd_stage,
            "dialysis_mode": dm,
            "growth_status": gs,
            "is_edema": is_edema,
            "weight_used_kg": _round(eff_weight, 2),
            "weight_basis": weight_basis,
            "energy": {
                "sdi_kcal_per_kg": [e_lo, e_hi],
                "target_kcal_per_kg": _round(e_pt, 2),
                "target_kcal_per_day": _round(energy_day, 1),
                "pd_glucose_deduction_kcal": _round(pd_deduction, 1),
                "basis": e_basis,
            },
            "protein": {
                "sdi_g_per_kg": [p_lo, p_hi],
                # N-S1 三审（2026-08-14）：输出权威「每日蛋白总量」SDI 区间——
                # 15-17 岁按性别（M 52-65 / F 45-49）。_band_for_age 已按性别解析。
                "sdi_total_g_per_day": band["protein_total_daily"],
                "vegetarian_multiplier": veg,
                "dialysis_extra_g_per_kg": [d_lo, d_hi],
                "target_g_per_kg": _round(protein_target_per_kg, 3),
                "floor_g_per_kg": _round(protein_floor_per_kg, 3),
                "target_g_per_day": _round(protein_target_g, 1),
                "floor_g_per_day": _round(protein_floor_g, 1),
                "note": protein_note,
            },
            "pe_ratio": {
                "ideal_pct": [7, 12],
                "ckd_acceptable_pct": [5.3, 6.4],
                "requires_total_protein_ge_100pct": True,
            },
            "schofield_cross_check": schofield_cross,
            "warnings": warnings,
        }

    adjusted = _plan(growth_status, dialysis_mode, height_age_years,
                     high_urea_persistent, "adjusted", "调整推荐（当前临床参数）")
    if need_adjusted:
        standard = _plan("normal", "none", None, False, "standard", "标准推荐（正常生长、未透析）")
        plans = [standard, adjusted]
    else:
        # 无临床调整：唯一方案即标准推荐（label 语义对齐）
        adjusted["label"] = "standard"
        adjusted["name"] = "标准推荐（正常生长、未透析）"
        plans = [adjusted]

    # 临床要点提示（权威依据，PRNT 2020 / KDOQI 2009 / theipna 2024 重印）
    clinical_notes: list[str] = []
    if need_adjusted:
        clinical_notes.append(
            "本患者存在临床调整（生长/透析/身高年龄/高尿素血症），已同时输出两种方案："
            "regimens.standard=标准推荐（正常生长、未透析、SDI 中点、蛋白上限）；"
            "regimens.adjusted=当前调整推荐——可对照处方。")
    clinical_notes.append(
        "蛋白质目标建议取 SDI 上限以促进生长；SDI 下限为最低安全摄入量，切勿低于，"
        "以免蛋白质-能量消耗（PEW）与生长迟缓。")
    if dialysis_mode == "peritoneal":
        clinical_notes.append(
            f"PD 透析液蛋白丢失参考：婴儿约 {_PD_LOSS_REF['infant_g_per_kg']}、"
            f"青少年约 {_PD_LOSS_REF['adolescent_g_per_kg']} g/kg/day（Quan & Baum 1996）；"
            "KDOQI 建议额外补充 0.15-0.30 g/kg/day。")
    elif dialysis_mode == "hemodialysis":
        clinical_notes.append("HD 蛋白丢失补充：0.10 g/kg/day（KDOQI 2009）。")
    clinical_notes.append(
        "SDI 能量基于较低体力活动水平（PAL：1-3 岁 1.4 / 4-9 岁 1.6 / 10-17 岁 1.8），"
        "契合 CKD 儿童体力活动普遍偏低的实际情况。")
    if growth_status == "failure" or height_age_years is not None:
        clinical_notes.append(
            "生长不良：能量摄入应调整至 SDI 上限；严重生长迟缓者可参考身高年龄"
            "（Height Age=身高在生长曲线第 50 百分位对应的年龄）的 SDI 进行评估和补充。")
    if high_urea_persistent:
        clinical_notes.append(
            "高尿素血症：已将蛋白质目标调至 SDI 下限——务必先排除脱水/高分解代谢/激素治疗"
            "等可纠正因素，且不降低能量摄入。")

    return {
        "ok": True,
        "data": {
            # 顶层字段 = adjusted 方案（向后兼容既有调用方/编排层）
            "guideline": GUIDELINE,
            "age_band": adjusted["age_band"],
            "sex": sex,
            "ckd_stage": ckd_stage,
            "dialysis_mode": dialysis_mode,
            "vegetarian_mode": vegetarian_mode,
            "growth_status": growth_status,
            "is_edema": is_edema,
            "weight_used_kg": adjusted["weight_used_kg"],
            "weight_basis": adjusted["weight_basis"],
            "energy": adjusted["energy"],
            "protein": adjusted["protein"],
            "pe_ratio": adjusted["pe_ratio"],
            "schofield_cross_check": adjusted["schofield_cross_check"],
            "warnings": adjusted["warnings"],
            # N-S1（2026-08-14）：双方案 + 临床要点
            "regimens": plans,
            "clinical_notes": clinical_notes,
        },
    }

def assess_intake_vs_target(
    diet: dict[str, Any],
    age_years: float,
    sex: str,
    weight_kg: float,
    ckd_stage: int = 1,
    dialysis_mode: str = "none",
    vegetarian_mode: str = "mixed",
    growth_status: str = "normal",
    height_cm: float = 0.0,
    is_edema: bool = False,
    pd_glucose_kcal_per_day: float | None = None,
    albumin_g_L: float | None = None,
    height_age_years: float | None = None,
    high_urea_persistent: bool = False,
) -> dict[str, Any]:
    """对照 PRNT 目标评估 3 日饮食日记均值，给出达成率、缺口/过量、PEW 风险。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    diet 需含：avg_energy_kcal / avg_protein_g / avg_potassium_mg / avg_phosphorus_mg /
              avg_sodium_mg（与 PCP nutrition_assessment.diet_diary_3d 对齐）。
    albumin_g_L（BUG-61，2026-08-12）：血清白蛋白 g/L，参与 PEW 筛查
    （<38 记低白蛋白预警——M-6 2026-08-15：CKiD 儿科 PEW 标准 <3.8 g/dL）；此前硬编码 None，白蛋白始终不参与本路径评估。
    """
    caller = get_caller()
    # BUG-01：临床判读工具级 ACL（仅 doctor）
    enforce_nutrition_tool(caller, "assess_intake_vs_target")
    _require(age_years, "age_years")
    _require(weight_kg, "weight_kg")
    if sex not in ("M", "F"):
        raise ValueError(f"sex 必须是 'M' 或 'F'，收到：{sex!r}")
    # BUG-61：diet 空值/非字典显式拒绝（此前 diet=None 会触发未捕获 TypeError）
    if not isinstance(diet, dict) or not diet:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "diet 需为字典且含 avg_energy_kcal / avg_protein_g"}
    required = ("avg_energy_kcal", "avg_protein_g")
    if not all(k in diet for k in required):
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "diet 需含 avg_energy_kcal 与 avg_protein_g"}
    # BUG-61 后补（2026-08-12）：键存在但值为 None（JSON null）时 .get(k, 0.0) 仍返回
    # None，float(None) 会抛未捕获 TypeError——统一 `or 0.0` 清洗，空值按 0 处理。
    # P1（2026-08-18）：NaN/Inf 显式拒绝——`nan or 0.0` 仍为 nan（nan 为真值），会穿透
    # 并在 _intake_pct_status(nan) 末支被静默判为"excess"（缺失摄入被误报为超标），临床误导。
    for _name, _raw in (("avg_energy_kcal", diet.get("avg_energy_kcal")),
                        ("avg_protein_g", diet.get("avg_protein_g")),
                        ("avg_potassium_mg", diet.get("avg_potassium_mg")),
                        ("avg_phosphorus_mg", diet.get("avg_phosphorus_mg")),
                        ("avg_sodium_mg", diet.get("avg_sodium_mg"))):
        _v = _raw if _raw is not None else 0.0
        if not isinstance(_v, (int, float)) or isinstance(_v, bool) or not math.isfinite(float(_v)):
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": f"{_name} 必须为有限数值（NaN/Inf 拒绝），收到 {_raw!r}"}
    avg_e = float(diet.get("avg_energy_kcal") or 0.0)
    avg_p = float(diet.get("avg_protein_g") or 0.0)
    avg_k = float(diet.get("avg_potassium_mg") or 0.0)
    avg_ph = float(diet.get("avg_phosphorus_mg") or 0.0)
    avg_na = float(diet.get("avg_sodium_mg") or 0.0)

    tgt = calc_prnt_targets(
        age_years=age_years, sex=sex, weight_kg=weight_kg, height_cm=height_cm,
        ckd_stage=ckd_stage, dialysis_mode=dialysis_mode, vegetarian_mode=vegetarian_mode,
        growth_status=growth_status, is_edema=is_edema,
        pd_glucose_kcal_per_day=pd_glucose_kcal_per_day,
        # M1（2026-08-16）：透传调整参数——与 DAG 内 prnt_targets 同口径，避免
        # 同一结果的"目标"在展示与摄入评估间矛盾。
        height_age_years=height_age_years, high_urea_persistent=high_urea_persistent,
    )
    if not tgt["ok"]:
        return tgt
    t = tgt["data"]
    target_e = t["energy"]["target_kcal_per_day"]
    target_p = t["protein"]["target_g_per_day"]
    floor_p = t["protein"]["floor_g_per_day"]

    e_pct = (avg_e / target_e * 100.0) if target_e > 0 else 0.0
    p_pct_vs_target = (avg_p / target_p * 100.0) if target_p > 0 else 0.0

    # 状态判定（M2：统一走 _intake_pct_status 单一阈值——与 diary 90-110 达标口径一致）
    e_status = _intake_pct_status(e_pct)

    if avg_p < floor_p:
        p_status = "below_floor"
    elif p_pct_vs_target > 130:
        p_status = "excess"
    else:
        p_status = "ok"

    # PE 比（蛋白 4 kcal/g）
    pe_ratio = (avg_p * 4.0 / avg_e * 100.0) if avg_e > 0 else 0.0

    # PEW 风险（简化筛查，非完整诊断）；BUG-61：白蛋白透传，不再硬编码 None
    pew = _screen_pew(avg_p, avg_e, floor_p, target_e, albumin_g_L)

    flags: list[str] = []
    if e_status == "deficit":
        flags.append(f"能量摄入仅达目标 {_round(e_pct,1)}%，低于 80% 建议线，存在生长/分解代谢风险。")
    if p_status == "below_floor":
        flags.append(f"蛋白质摄入 {_round(avg_p,1)}g 低于 PRNT 安全下限 {_round(floor_p,1)}g/day，严禁继续限制。")
    if p_status == "excess":
        flags.append(f"蛋白质摄入达目标 {_round(p_pct_vs_target,1)}%，超过 130%，需监测 BUN/血磷。")
    if pe_ratio and (pe_ratio < 5.3):
        flags.append(f"蛋白-能量比 {_round(pe_ratio,1)}% 偏低，提示蛋白被当作能量供能（PEW 倾向）。")

    return {
        "ok": True,
        "data": {
            "guideline": GUIDELINE,
            "energy": {
                "avg_kcal": _round(avg_e, 1),
                "target_kcal": _round(target_e, 1),
                "achievement_pct": _round(e_pct, 1),
                "status": e_status,
            },
            "protein": {
                "avg_g": _round(avg_p, 1),
                "target_g": _round(target_p, 1),
                "floor_g": _round(floor_p, 1),
                "achievement_pct_vs_target": _round(p_pct_vs_target, 1),
                "status": p_status,
            },
            "electrolytes_avg_mg": {
                "potassium": _round(avg_k, 1),
                "phosphorus": _round(avg_ph, 1),
                "sodium": _round(avg_na, 1),
            },
            "pe_ratio_pct": _round(pe_ratio, 2),
            "pew_risk": pew["risk"],
            "pew_rationale": pew["rationale"],
            "flags": flags,
            "note": "钾/磷/钠上限由临床医生在 clinician_limits 设定，此处仅展示 3 日实测均值。",
        },
    }


def _intake_pct_status(pct: float) -> str:
    """能量/蛋白达成率统一分级（M2，2026-08-16，第七轮审查）——core 与 diary 共用
    单一阈值（此前分裂：core 能量 <80=deficit / >120=excess，diary 90-110=达标，
    80-90 与 110-120 区间同一份日记给不同结论）。
    deficit(<80，PRNT 分级干预线) / low(80-90) / ok(90-110 达标) / high(110-130) /
    excess(>130)。"""
    if pct < 80:
        return "deficit"
    if pct < 90:
        return "low"
    if pct <= 110:
        return "ok"
    if pct <= 130:
        return "high"
    return "excess"


def _screen_pew(avg_p: float, avg_e: float, floor_p: float, target_e: float,
                albumin_g_L: float | None) -> dict[str, Any]:
    """简化 PEW 风险筛查：蛋白低于安全下限 + 能量低于 80% 目标 → 高风险。

    S2 修复（2026-08-13）：返回数值 score（0-100，信号加权）——供
    record_pew_risk 落库（其 score 契约字段本应来自本函数，此前 assess 不返回
    score 导致编排层无值可传）。打分规则透明：蛋白缺乏 40 分 + 能量缺乏 40 分 +
    低白蛋白 20 分；high ≥80、medium 40-60、low 0。
    """
    # P0-3（2026-08-18 四审）：albumin 非法值防御性拒绝——入口 assess_pew_risk 已
    # INVALID_INPUT 拦截；此处兜底直调（_screen_pew 为模块内纯函数可被直调）——
    # 此前 `nan < 38` 恒 False 被静默当"不低白蛋白"（漏扣 20 分低估风险），且 NaN
    # 被静默转 None（fail-open）；一律显式拒绝。
    if albumin_g_L is not None and (
            isinstance(albumin_g_L, bool) or not isinstance(albumin_g_L, (int, float))
            or not math.isfinite(albumin_g_L) or albumin_g_L <= 0):
        raise ValueError(
            f"albumin_g_L 必须为 > 0 的有限数值或 None（收到 {albumin_g_L!r}），"
            "拒绝以脏值参与 PEW 判定")
    protein_deficit = avg_p < floor_p
    energy_deficit = (avg_e / target_e) < 0.8 if target_e > 0 else False
    low_albumin = (albumin_g_L is not None and albumin_g_L < 38)  # M-6：CKiD 儿科 PEW 标准 <3.8 g/dL

    # S2：信号加权分（透明、可解释；与等级判定共用信号，不另立口径）
    score = sum((
        40.0 if protein_deficit else 0.0,
        40.0 if energy_deficit else 0.0,
        20.0 if low_albumin else 0.0,
    ))

    if protein_deficit and energy_deficit:
        risk = "high"
        # BUG-63（2026-08-12）：明确标注"简化筛查"——避免把摄入不足直接读成 PEW 确诊，
        # 防止对短期厌食患儿过度干预（管饲等）；确诊需结合人体测量与生化指标。
        rationale = ("蛋白质低于安全下限且能量摄入 <80% 目标，提示 PEW 高风险。"
                     "（本结果为基于摄入/白蛋白的简化筛查；确诊需结合体重丢失、"
                     "中臂肌围等人体测量与生化指标）")
    elif protein_deficit or energy_deficit or low_albumin:
        risk = "medium"
        parts = []
        if protein_deficit:
            parts.append("蛋白质低于安全下限")
        if energy_deficit:
            parts.append("能量 <80% 目标")
        if low_albumin:
            parts.append(f"白蛋白 {albumin_g_L} g/L <38")
        rationale = "存在以下 PEW 预警信号：" + "；".join(parts) + "。"
    else:
        risk = "low"
        rationale = "蛋白质与能量摄入均达 PRNT 安全范围，PEW 风险低。"

    if albumin_g_L is None:
        rationale += "（未提供白蛋白，建议结合血清白蛋白 <38 g/L（CKiD 儿科 PEW 标准）与人体测量综合判定）"
    return {"risk": risk, "rationale": rationale, "score": score}


def assess_pew_risk(
    avg_protein_g: float,
    avg_energy_kcal: float,
    target_protein_g: float,
    target_energy_kcal: float,
    albumin_g_L: float | None = None,
    floor_protein_g: float | None = None,
) -> dict[str, Any]:
    """独立 PEW 风险筛查接口（供编排层直接传入已算好的均值与目标）。

    floor_protein_g（BUG-42 修复，2026-08-12）：PRNT 官方蛋白安全下限（=SDI 下限×体重），
    应传入 calc_prnt_targets 返回的 protein.floor_g_per_day。缺省时退化为
    "目标×85%"近似（婴儿段会偏高：0 月段官方下限 1.52 远低于 85% 目标 2.13，
    会把合规摄入误判为低于安全下限）——独立调用方建议显式传官方下限。
    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    # BUG-01：临床判读工具级 ACL（仅 doctor）
    enforce_nutrition_tool(caller, "assess_pew_risk")
    _require(avg_protein_g, "avg_protein_g")
    _require(avg_energy_kcal, "avg_energy_kcal")
    _require(target_protein_g, "target_protein_g")
    _require(target_energy_kcal, "target_energy_kcal")
    # 六审（2026-08-13）：负值/零目标显式拒绝（fail-closed）——此前负蛋白/负能量
    # 会被 _screen_pew 当作"极低摄入"判高风险（方向碰巧对但数据本身非法），零目标
    # 会除零/退化为恒真；脏数据应 INVALID_INPUT，不得静默参与临床判定。
    for _name, _val in (("avg_protein_g", avg_protein_g), ("avg_energy_kcal", avg_energy_kcal),
                        ("target_protein_g", target_protein_g),
                        ("target_energy_kcal", target_energy_kcal)):
        if _val < 0:
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": f"{_name} 不能为负，收到 {_val}"}
    if target_protein_g <= 0 or target_energy_kcal <= 0:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "target_protein_g 与 target_energy_kcal 必须 > 0"}
    # P0-3（2026-08-18 四审）：albumin_g_L 严格校验——此前 NaN/Inf 在 _screen_pew
    # 内被静默转 None（"按未提供"继续计算，漏扣 20 分低估风险、违反 fail-closed），
    # 非数字字符串抛裸 TypeError 500。None=未提供合法（PEW 可不含白蛋白）；
    # 显式传入的非法值（非数值/bool/NaN/Inf/<=0）一律 INVALID_INPUT，禁止隐式转 None。
    if albumin_g_L is not None:
        if isinstance(albumin_g_L, bool) or not isinstance(albumin_g_L, (int, float)) \
                or not math.isfinite(albumin_g_L) or albumin_g_L <= 0:
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": f"albumin_g_L 必须为 > 0 的有限数值或 None（未提供），"
                              f"收到 {albumin_g_L!r}"}
    # P1-5（2026-08-18）：floor_protein_g 校验——此前零校验直通 _screen_pew：
    # floor=0/-5 使"蛋白低于下限"恒不成立 → PEW 假阴性（risk=low, score=0）；
    # floor=inf 使"蛋白低于下限"恒成立 → 恒判 medium/40；bool/NaN 同理穿透。
    if floor_protein_g is not None and (
            isinstance(floor_protein_g, bool)
            or not math.isfinite(floor_protein_g) or floor_protein_g <= 0):
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"floor_protein_g 必须为 > 0 的有限数值（收到 {floor_protein_g!r}）"}
    floor_p = floor_protein_g if floor_protein_g is not None else target_protein_g * 0.85
    pew = _screen_pew(avg_protein_g, avg_energy_kcal, floor_p, target_energy_kcal, albumin_g_L)
    # S2 修复（2026-08-13）：透出数值 score——record_pew_risk.score 契约字段
    # 的合法来源（此前 assess 不返回 score，编排层无值可传：传 0.0 误导 / 调用失败）。
    return {"ok": True, "data": {
        "pew_risk": pew["risk"],
        "rationale": pew["rationale"],
        "score": round(float(pew["score"]), 1),
    }}


# ---------------------------------------------------------------------------
# 3. 3 日饮食日记：写入(store) + 聚合(diet_diary_3d) + 读取
# ---------------------------------------------------------------------------
def _load_patient_store(patient_id: str) -> dict[str, Any]:
    """患者级读（N-MEM-2）：行级 GetRow(pk=patient_id)，不扫全表。

    医院级（数千患儿 × 多年日记）下，单次"记一顿饭/读一家日记"不再拉全库，
    消除 OOM 与写放大。
    """
    return _repo().load_patient_diary(patient_id)


def _save_patient_store(patient_id: str, entries: list[dict[str, Any]]) -> None:
    """患者级写（N-MEM-2）：只写该患者行（行级 _rev 乐观锁）。"""
    _repo().save_patient_diary(patient_id, entries)


def _aggregate(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """取最近 3 个不重复日期的条目，按**天数**求每日均值，返回 diet_diary_3d 形状。

    BUG-61（2026-08-12）：分母必须是天数而非餐次条目数——原除以 len(used) 得到的是
    "平均每餐"，患者一天记 3 餐时日均值被缩 3 倍，与日目标（target_kcal_per_day）
    对照会触发能量不足误报与 PEW 假阳性。
    """
    by_day: dict[str, list[dict[str, Any]]] = {}
    # P1-6（2026-08-18）：非 dict 条目跳过——此前 store 混入 None/str 条目（旧版本
    # 写入或外部污染）时 `e.get` 抛 AttributeError → 500，且每次读都崩（日记永久
    # 不可读）。写路径 F-4 已防新脏数据，读路径对历史脏条目 fail-soft 跳过（脏条目
    # 无营养值可贡献，剔除不改变均值语义）。
    for e in entries:
        if not isinstance(e, dict):
            continue
        by_day.setdefault(e.get("date", ""), []).append(e)
    days = sorted(by_day.keys(), reverse=True)[:3]
    used = [e for d in days for e in by_day[d]]
    num_days = max(len(days), 1)
    # P3（2026-08-18）：NaN 清洗——`or 0.0` 拦不住 NaN（NaN 是 truthy），写路径
    # N-S4 已拒 NaN，但历史脏数据仍会经 sum() 产出 NaN 键；非有限值按 0 计（fail-soft，
    # 与 `or 0.0` 的 None 口径对齐）。
    def _num(v: Any) -> float:
        if isinstance(v, bool):
            return 0.0
        if isinstance(v, (int, float)) and math.isfinite(v):
            return float(v)
        return 0.0
    avg = {
        "avg_energy_kcal": sum(_num(e.get("energy_kcal")) for e in used) / num_days,
        "avg_protein_g": sum(_num(e.get("protein_g")) for e in used) / num_days,
        "avg_potassium_mg": sum(_num(e.get("potassium_mg")) for e in used) / num_days,
        "avg_phosphorus_mg": sum(_num(e.get("phosphorus_mg")) for e in used) / num_days,
        "avg_sodium_mg": sum(_num(e.get("sodium_mg")) for e in used) / num_days,
    }
    return {"day_count": len(days), "entry_count": len(used), "diet_diary_3d": avg}


def _normalize_date(value: Any, field: str = "date") -> str:
    """把日期串归一化为 ISO YYYY-MM-DD（BUG-60，2026-08-12）。

    接受 YYYY-MM-DD / YYYY/M/D / YYYY.M.D（容忍前后空白）；解析失败显式抛错
    （fail-closed）。此前日期作为不透明字符串直接落库，"2026-3-1" 与 "2026-03-01"
    会被 _aggregate 当作不同日期分桶，跨天统计被拆散、平均值失真。
    """
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"{field} 必须为有效日期（YYYY-MM-DD），收到：{value!r}")


def upsert_food_diary(
    patient_id: str,
    entries: list[dict[str, Any]] | None = None,
    write_mode: bool = True,
    guardian_token: str | None = None,
) -> dict[str, Any]:
    """写入/追加饮食条目（MX-3 收口：家长/医生，2026-08-12 需求对齐临床=✔）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    条目由调用方经 M5 计算养分后填入（能量/蛋白/钾/磷/钠），M3 仅做存储与聚合，
    不反向调用 M5，保证各包自包含。
    家长写入必须携带 guardian_token 完成患儿绑定核验（2026-08-12 修复：
    此前无绑定校验，家长可向任意患儿写入日记污染数据）。
    """
    caller = get_caller()
    enforce_nutrition_tool(caller, "upsert_food_diary")
    # 一般 1（2026-08-14）：统一 patient_id 契约校验（对齐 P1/P3 各入口）——此前
    # 缺此校验，医生身份可写入畸形/脏 id（如 "P123" 或空格）污染 diary_store.json；
    # 家长路径虽经 _guard_guardian fail-closed 拒绝（畸形 id 无令牌条目），但医生侧
    # 数据污染与读侧不匹配。畸形 id 显式 INVALID_INPUT，不静默落库。
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}
    denied = _guard_guardian(caller, patient_id, guardian_token, "upsert_food_diary")
    if denied:
        return denied
    if not entries:
        return {"ok": False, "error": "INVALID_INPUT", "detail": "entries 不能为空"}
    # F-4（2026-08-15）：entries 元素类型校验——此前非 dict 元素直接 e.get 抛
    # AttributeError 冒泡成 500；显式 INVALID_INPUT（与 P3 通知 payload 同模式）。
    for _i, _e in enumerate(entries):
        if not isinstance(_e, dict):
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": f"entries[{_i}] 必须为对象（dict），收到 {type(_e).__name__}"}
    # P2-1（2026-08-18）：meal 枚举校验——此前零校验，"夜宵"/123/None/"第25餐" 全部
    # 落库（幂等键 content_key=(date,meal,food) 混入 dict/list 时抛不可哈希 TypeError
    # 500）；显式白名单 + 拒绝不可哈希类型（dict/list/set → INVALID_INPUT）。
    for _i, _e in enumerate(entries):
        _meal = _e.get("meal")
        if _meal is None or _meal == "":
            continue
        if isinstance(_meal, (dict, list, set, tuple)) or not isinstance(_meal, str) \
                or _meal.strip() not in _MEAL_TYPES:
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": f"entries[{_i}].meal 必须为 {_MEAL_TYPES} 之一"
                              f"（收到 {_meal!r}）"}

    with _STORE_LOCK:
        # N-MEM-2（2026-08-14）：患者级读——此前 _load_store() 全表 GetRange +
        # save 回写每个患者行（单次记餐 = 全表扫描 + O(患者数) 写放大，数千患儿
        # 直接 OOM）。现只读/只写该患者行。
        store = _load_patient_store(patient_id)
        existing = store.get("entries", [])
        stamped = []
        for e in entries:
            # BUG-60：写入前归一化日期（容忍 YYYY-M-D / YYYY/M/D 等变体，非法日期显式拒绝）
            raw_date = e.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")  # C1（2026-08-15）：日记业务日期统一 UTC——此前 datetime.now() 本地 naive，与同库 recorded_at(UTC) 不一致，跨时区部署（UTC+8 等）行为漂移
            # F-4（2026-08-15）：未来日期拒绝——未来条目会成为 3 日锚点污染摄入评估
            # （sum_diet_intake 取最近 3 日窗口，未来条目把窗口拉向未来导致今日摄入
            # 缺失）。与 P1 report_date 未来拒绝同口径（UTC 业务日）。
            _norm_date = _normalize_date(raw_date, "date")
            if _norm_date > datetime.now(timezone.utc).date().isoformat():
                return {"ok": False, "error": "INVALID_INPUT",
                        "detail": f"条目日期 {_norm_date} 晚于今天（未来日期），拒绝写入"}
            # N-S4 修复（2026-08-14）：写路径营养键有限性校验（fail-closed）——
            # 此前 float() 直接转换，NaN/Inf 可静默落库（读路径比较恒 False →
            # assess_intake_vs_target 误判 e_status="ok"），与 P0-7 读路径同口径。
            numeric = {}
            for key in ("energy_kcal", "protein_g", "potassium_mg",
                        "phosphorus_mg", "sodium_mg"):
                val = e.get(key, 0.0)
                try:
                    fv = float(val)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "INVALID_INPUT",
                            "detail": f"{key} 无法解析为数值：{val!r}"}
                if not math.isfinite(fv):
                    return {"ok": False, "error": "INVALID_INPUT",
                            "detail": f"{key} 必须为有限数值（NaN/Inf 拒绝）：{val!r}"}
                # F-4（2026-08-15）：负营养值拒绝——负能量/负钾等物理不可能，静默落库
                # 会拉低均值并制造假"达标"；0 合法（该餐未检出）。
                if fv < 0:
                    return {"ok": False, "error": "INVALID_INPUT",
                            "detail": f"{key} 不能为负（收到 {fv}），请核查录入"}
                numeric[key] = fv
            stamped.append({
                "patient_id": patient_id,
                # C1 修复（2026-08-14）：条目补服务端 entry_id——此前条目无唯一 id，
                # _item_key 回退用 date 键 → 同日早/午/晚多餐 key 相同，并发合并时
                # 一餐被另一餐覆盖（nutrition_repository._merge_lists）。服务端
                # uuid 碰撞免疫（与 P1 sample_id 同模式），保证并发追加不互替。
                "entry_id": f"{patient_id}-D{uuid.uuid4().hex[:8]}",
                "date": _norm_date,
                "meal": e.get("meal", ""),
                "food": e.get("food", ""),
                "energy_kcal": numeric["energy_kcal"],
                "protein_g": numeric["protein_g"],
                "potassium_mg": numeric["potassium_mg"],
                "phosphorus_mg": numeric["phosphorus_mg"],
                "sodium_mg": numeric["sodium_mg"],
            })
        # S-3（2026-08-15）：(date+meal+food) 内容幂等——此前无条件 `existing + stamped`
        # 追加（每次新 uuid4 entry_id），家长弱网重试同一顿饭 → 两行，day_count/均值
        # 失真。现按内容键合并：同 date+meal+food 已存在 → 用本次值**替换**该条目
        # （幂等更新，保留原 entry_id），否则新增。多条目同键取最后一条（后写者意图）。
        content_key = lambda e: (e.get("date"), e.get("meal") or "", e.get("food") or "")
        newest = {}
        for e in stamped:
            newest[content_key(e)] = e
        merged: list[dict] = []
        seen: set = set()
        for e in existing:
            k = content_key(e)
            if k in newest:
                if k not in seen:           # 同键旧条目被本次值替换（保留原 entry_id）
                    repl = dict(newest[k])
                    repl["entry_id"] = e.get("entry_id") or newest[k]["entry_id"]
                    merged.append(repl)
                    seen.add(k)
            else:
                merged.append(e)
        for k, e in newest.items():         # 新键条目追加
            if k not in seen:
                merged.append(e)
                seen.add(k)
        all_entries = merged

        if write_mode:
            # N-MEM-2：患者级写（只写该患者行，行级 _rev 乐观锁）
            _save_patient_store(patient_id, all_entries)

    # 行级读写已按患者隔离（N-MEM-2），all_entries 即该患者全部条目，无需再 filter
    agg = _aggregate(all_entries)
    return {
        "ok": True,
        "data": {
            "patient_id": patient_id,
            # S5（2026-08-12 五包审查）：write_mode=False（预演）时 stored_count 归 0，
            # 并补 persisted 显式标注（对齐 clinical-data upsert_lab_result 的 persisted
            # 字段）——此前预演也返回 stored_count=len(stamped)，"存了 N 条"与实际未写矛盾。
            "stored_count": len(stamped) if write_mode else 0,
            "persisted": bool(write_mode),
            "write_mode": write_mode,
            "day_count": agg["day_count"],
            "entry_count": agg["entry_count"],
            "diet_diary_3d": {k: _round(v, 1) for k, v in agg["diet_diary_3d"].items()},
            "note": ("聚合最近 3 个不重复日期；diet_diary_3d 已对齐 PCP nutrition_assessment "
                     "形状。write_mode=False 时仅预演不落盘（stored_count=0）。"),
        },
    }


def get_food_diary_summary(patient_id: str, guardian_token: str | None = None) -> dict[str, Any]:
    """读取并聚合某患者的饮食日记（只读，家长/医生/临床角色可读）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    家长读取必须携带 guardian_token 完成患儿绑定核验（2026-08-12 修复：
    此前无绑定校验，家长可跨患者读取任意患儿日记原始条目）。
    """
    caller = get_caller()
    enforce_nutrition_tool(caller, "get_food_diary_summary")
    # N1 修复（2026-08-13）：统一 patient_id 契约校验（与 P1 his 同口径，
    # a207_policy.validate_patient_id ^P[0-9]{4,}$）——畸形 id 不进存储层。
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_food_diary_summary")
    if denied:
        return denied
    # N-MEM-2（2026-08-14）：患者级读（行级 GetRow），行内 entries 即该患者全部
    # 条目——此前 _load_store() 全表拉取后再 filter，医院级全库扫描。
    store = _load_patient_store(patient_id)
    entries = store.get("entries", [])
    if not entries:
        return {
            "ok": True,
            "data": {
                "patient_id": patient_id,
                "day_count": 0,
                "entry_count": 0,
                "diet_diary_3d": None,
                "note": "暂无该患者的饮食日记记录。",
            },
        }
    agg = _aggregate(entries)
    return {
        "ok": True,
        "data": {
            "patient_id": patient_id,
            "day_count": agg["day_count"],
            "entry_count": agg["entry_count"],
            "diet_diary_3d": {k: _round(v, 1) for k, v in agg["diet_diary_3d"].items()},
            "recent_entries": entries[-10:],
        },
    }


# ---------------------------------------------------------------------------
# 中国卫健委行业标准：儿童生长 Z 评分（身高/体重/BMI 年龄别）
# 标准：WS/T 423-2022（7岁以下，Appendix B SD 值）+ WS/T 612-2018（7-18 身高，Appendix A）
# 注：不采用 WHO 2007 LMS —— 用户判定 WHO 已 20 年未更新、偏旧，改用中国官方标准。
# 数据：data/growth_ref_cn.json（<84月按整月，7-18岁按整岁；m=中位数, s=标准差）。
# ---------------------------------------------------------------------------
_GROWTH_REF_PATH = os.path.join(os.path.dirname(__file__), "data", "growth_ref_cn.json")
_GROWTH_REF = None
# S3（2026-08-12 五包审查）：懒加载并发锁（double-checked locking，对齐 assessment）
_GROWTH_REF_LOCK = threading.Lock()


def _load_growth_ref() -> dict:
    global _GROWTH_REF
    if _GROWTH_REF is None:
        with _GROWTH_REF_LOCK:
            if _GROWTH_REF is None:  # S3：防多线程首调重复 I/O
                with open(_GROWTH_REF_PATH, "r", encoding="utf-8") as f:
                    _GROWTH_REF = json.load(f)
    return _GROWTH_REF


def _interp_sd(table: list, age_key: float):
    """兼容包装：按 age_key 线性插值返回 (中位数 m, ±1SD 平均 s)。越界取端点。

    table 行支持两种格式：
    - 旧 3 列 [age, m, s]（height_7_18 沿用，WS/T 612 5 界值等距）
    - 新 8 列 [age, n3, n2, n1, m, p1, p2, p3]（WS/T 423 附录 B 7 界值，
      MED-GR-1（2026-08-15）：国标 SD 分布非均匀，单一 s 外推 ±2SD/±3SD 会
      系统性错判营养等级，必须存全 7 界值）
    s 取 ±1SD 平均（=(p1−n1)/2），仅用于展示字段，判定一律走 _z_from_bands。
    """
    bands = _interp_bands(table, age_key)
    if bands is None:
        return None, None
    n1, m, p1 = bands[2], bands[3], bands[4]
    return m, round((p1 - n1) / 2, 2)


def _interp_bands(table: list, age_key: float):
    """按 age_key 线性插值返回 (n3, n2, n1, m, p1, p2, p3) 七界值；越界取端点。

    - 8 列行：各界值列分别插值（保留非均匀分布信息）；
    - 3 列行（7-18 身高，WS/T 612 等距 5 界值）：用 s 构造 ±3SD（m∓3s），
      保持原有外推口径。
    返回 None 表示数据缺失（调用方应 fail-closed 或告警）。
    """
    if not table:
        return None
    is_8 = len(table[0]) >= 8
    if age_key <= table[0][0]:
        row = table[0]
    elif age_key >= table[-1][0]:
        row = table[-1]
    else:
        row = None
        for i in range(len(table) - 1):
            a0 = table[i][0]
            a1 = table[i + 1][0]
            if a0 <= age_key <= a1:
                if a1 == a0:
                    row = table[i + 1]
                else:
                    t = (age_key - a0) / (a1 - a0)
                    r0, r1 = table[i], table[i + 1]
                    row = [r0[0] + (r1[0] - r0[0]) * t] + [
                        r0[j] + (r1[j] - r0[j]) * t for j in range(1, len(r0))]
                break
        if row is None:
            row = table[-1]
    if is_8:
        return (row[1], row[2], row[3], row[4], row[5], row[6], row[7])
    # 3 列行：m=s=row[1], row[2]；构造等距 7 界值
    m, s = row[1], row[2]
    return (m - 3 * s, m - 2 * s, m - s, m, m + s, m + 2 * s, m + 3 * s)


def _z_from_bands(x: float, bands: tuple) -> float:
    """按国标 7 界值分段线性插值计算 Z 分（MED-GR-1，2026-08-15）。

    国标 WS/T 423 附录 B 的 SD 分布非均匀（BMI 高尾最甚：81 月男童 −3→−2SD 间距
    0.9、+2→+3SD 间距 3.6，差 4 倍）——旧实现 z=(x−m)/s 用单一 s 线性外推，
    在 ±2SD/±3SD 处系统性偏差（如 81 月男童 BMI 19.7=+2SD 界值却算 z≈2.77），
    可致超重/肥胖/重度肥胖、低体重/重度低体重相邻等级错判。
    分段插值保证：x 恰为任一 SD 界值时 z 精确等于对应整数，区间内线性。
    超出 ±3SD 用最外侧斜率外推（与原口径一致，仅 7-18 身高等距表触达）。
    """
    n3, n2, n1, m, p1, p2, p3 = bands
    if x <= n3:
        return -3.0 + (x - n3) / (n2 - n3)          # 低于 -3SD：最外侧斜率外推
    if x <= n2:
        return -3.0 + (x - n3) / (n2 - n3)
    if x <= n1:
        return -2.0 + (x - n2) / (n1 - n2)
    if x <= m:
        return -1.0 + (x - n1) / (m - n1)
    if x <= p1:
        return (x - m) / (p1 - m)
    if x <= p2:
        return 1.0 + (x - p1) / (p2 - p1)
    if x <= p3:
        return 2.0 + (x - p2) / (p3 - p2)
    return 3.0 + (x - p3) / (p3 - p2)               # 高于 +3SD：最外侧斜率外推


def _valid_sd(s: Any) -> bool:
    """SD 必须为正的有限数值（Z=(X-m)/s；s<=0 或 NaN 会产生无意义/反向 Z 分）。

    BUG-59（2026-08-12）：原判 `s in (None, 0)` 仅拦零值，负数与 NaN（NaN<=0 为 False）
    仍会穿透；统一收紧为"正有限数"。
    """
    return isinstance(s, (int, float)) and s == s and s > 0


# BUG-59（2026-08-12）：身高参照表静态化缓存（数据只随 growth_ref_cn.json 变化，
# 与 _GROWTH_REF 同生命周期）——此前 calc_growth_zscore 每次评估都重新合并+排序。
_HEIGHT_TABLE_CACHE: dict[str, list] = {}


def _height_table(sex: str) -> list:
    """合并 height_under7(月) 与 height_7_18(岁→月)，统一 8 列 [age, n3..p3]。

    MED-GR-1（2026-08-15）：WS/T 423 附录 B 与 WS/T 612 表 A 均已存 8 列全界值
    （7-18 的 ±3SD 用外侧斜率外推），保证 _interp_bands 插值时行宽一致。
    """
    cached = _HEIGHT_TABLE_CACHE.get(sex)
    if cached is not None:
        return cached
    ref = _load_growth_ref()
    merged = [list(r) for r in ref["height_under7"][sex]]  # 8 列
    for row in ref["height_7_18"][sex]:
        if len(row) >= 8:
            merged.append([row[0] * 12] + list(row[1:8]))  # 岁→月，8 列
        else:
            # 旧 3 列兼容（不应触达）：用 s 构造 ±3SD
            age_years, m, s = row
            merged.append([age_years * 12, m - 3 * s, m - 2 * s, m - s, m,
                           m + s, m + 2 * s, m + 3 * s])
    merged.sort(key=lambda r: r[0])
    _HEIGHT_TABLE_CACHE[sex] = merged
    return merged


def _grade_5(z: float) -> str:
    """生长水平 5 等级（标准差法：表2 / WS/T 612 表3.3 同口径）。

    区间：[−∞,−2) 下；[−2,−1) 中下；[−1,1] 中；[1,2] 中上；(2,∞) 上。
    P2 核实（2026-08-13）：z==2.0 归"中上"符合 WS/T 5 等级惯例（+2SD 含于中上
    区间，>+2SD 才判"上"），非缺陷，保留。
    """
    if z < -2:
        return "下"
    if z < -1:
        return "中下"
    if z <= 1:
        return "中"
    if z <= 2:
        return "中上"
    return "上"


def _haz_nutrition(z: float) -> str:
    """身高别年龄 营养状况（生长迟缓）。WS/T 423 表3 年龄别身长/身高行。"""
    if z < -3:
        return "重度生长迟缓"
    if z < -2:
        return "生长迟缓"
    return "正常"


def _waz_nutrition(z: float) -> str:
    """体重别年龄 营养状况（低体重）。WS/T 423 表3 年龄别体重行。"""
    if z < -3:
        return "重度低体重"
    if z < -2:
        return "低体重"
    return "正常"


def _baz_nutrition(z: float) -> str:
    """BMI 别年龄 营养状况。WS/T 423 表3 年龄别BMI行。"""
    if z >= 3:
        return "重度肥胖"
    if z >= 2:
        return "肥胖"
    if z >= 1:
        return "超重"
    if z < -3:
        return "重度消瘦"
    if z < -2:
        return "消瘦"
    return "正常"


def calc_growth_zscore(age_years: float, sex: str,
                       height_cm: float | None = None,
                       weight_kg: float | None = None,
                       bmi: float | None = None) -> dict[str, Any]:
    """按中国卫健委行业标准计算儿童生长 Z 评分（HAZ / WAZ / BAZ）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。

    标准：WS/T 423-2022（7岁以下 体重/身高/BMI 年龄别 SD 值）+ WS/T 612-2018（7-18 身高年龄别）。
    不使用 WHO 2007（用户判定偏旧）。Z = (实测 - 中位数) / SD，SD 由标准附录给出。

    - HAZ（身高别年龄）：0-18 岁全覆盖（<84月用 WS/T 423，≥7岁用 WS/T 612）。
    - WAZ（体重别年龄）/ BAZ（BMI别年龄）：仅 7 岁以下（WS/T 423 随附；7-18 体重/BMI 标准未提供，跳过）。
    - 返回各指标 z、5 等级、营养状况分类，并给出 PRNT `growth_status` 建议（failure/overweight/normal）。
    """
    caller = get_caller()
    # BUG-01：临床判读工具级 ACL（仅 doctor）
    enforce_nutrition_tool(caller, "calc_growth_zscore")
    _require(age_years, "age_years")
    # BUG-19：sex 非法值显式报错，不再静默按男性计算
    if sex not in ("M", "F"):
        raise ValueError(f"sex 必须是 'M' 或 'F'，收到：{sex!r}")
    # BUG-60：负年龄显式拒绝——此前会按 0 月边界钳位算出无临床意义的 Z 分
    if age_years < 0:
        raise ValueError("age_years 不能为负")
    # #4（2026-08-15，G-6 残留）：可选参数 NaN/Inf 有限性校验——此前仅
    # `<=0`/`>250` 比较，NaN 不满足任何比较直接穿透（height_cm=nan → z=nan
    # 静默返回"normal"生长判定，且 NaN 随信封输出非法 JSON）。P4 有 _require_finite
    # 全拦，P2 是唯一 fail-open 点，此处补齐（对齐 calc_prnt_targets 的 _require）。
    for _name, _val in (("height_cm", height_cm), ("weight_kg", weight_kg),
                        ("bmi", bmi)):
        if _val is not None and (isinstance(_val, bool)
                                 or not isinstance(_val, (int, float))):
            raise ValueError(f"{_name} 必须为数值，收到 {_val!r}")
        if isinstance(_val, float) and (_val != _val or _val in (float("inf"), float("-inf"))):
            raise ValueError(f"{_name} 必须为有效的有限数值，收到 {_val!r}")
    # Code Smells-15（2026-08-12）：身高/体重必须为正——此前 height_cm=0 会算出
    # (0-中位数)/SD 的 -26 级荒谬 Z 分，静默返回"生长迟缓"误导临床。
    if height_cm is not None and height_cm <= 0:
        raise ValueError("height_cm 必须 > 0")
    if weight_kg is not None and weight_kg <= 0:
        raise ValueError("weight_kg 必须 > 0")
    if bmi is not None and bmi <= 0:
        raise ValueError("bmi 必须 > 0")
    # BUG-63（2026-08-12）：生理学合理上界——过高/过重（录入错误）会算出荒谬 Z 分
    # （如 300cm → Z≈+26 判"上"）；**不做下限收紧**（早产儿 40-45cm 合法，下限保持 >0）。
    if height_cm is not None and height_cm > 250:
        raise ValueError(f"height_cm {height_cm} 超出生理学合理范围（≤250 cm），请核查数据")
    if weight_kg is not None and weight_kg > 200:
        raise ValueError(f"weight_kg {weight_kg} 超出生理学合理范围（≤200 kg），请核查数据")
    ref = _load_growth_ref()
    age_months = age_years * 12.0
    results: dict[str, Any] = {"ok": True, "data": {}}
    d = results["data"]
    d["age_years"] = _round(age_years, 3)
    d["age_months"] = _round(age_months, 1)
    d["sex"] = sex
    d["standards"] = ref["meta"]["standards"]
    warnings: list[str] = []
    # BUG-63（2026-08-12）：原始 Z 分（未 round）用于 growth_status 临床判定——
    # d["haz"]["z"] 等是 round(…,2) 后的展示值，直接用它判定会在 -2 边界引入 ±0.005
    # 抖动（如 -2.004 round 成 -2.00 → `-2.0 < -2` 为 False 漏判生长迟缓）。
    haz_z: float | None = None
    waz_z: float | None = None
    baz_z: float | None = None

    # BUG-60：>18 岁（>216 月）超出 WS/T 423/612 适用域——不再静默按 216 月边界钳位，
    # 显式标注越界并提示结果仅供参考（负年龄已在入口拒绝）。
    if age_months > 216:
        d["age_out_of_bounds"] = True
        warnings.append(
            "age_years 超出中国卫健委标准适用上限（18 岁），Z 评分按 18 岁边界参考值计算，"
            "仅供参考，不作临床判定依据。")

    # HAZ（身高别年龄）—— 合并 0-18
    if height_cm is not None:
        # M-9（2026-08-15）：82-83 月龄（6岁10-11月）显式跳过——WS/T 423 表止于
        # 81 月（6岁9月）、WS/T 612 起于 7 岁整（84 月），两表均不覆盖该窗口。此前
        # 用 81↔84 月线性插值（跨两套标准衔接混用），而 WAZ/BAZ 在同一窗口显式跳过
        # ——不对称。统一为显式跳过 + 告警（与 WAZ/BAZ 同口径）。
        if 81 < age_months < 84:
            warnings.append(
                "年龄处于 6岁10-11月（82-83 月龄）：WS/T 423 生长表止于 81 月（6岁9月）、"
                "WS/T 612 起于 7 岁整（84 月），两标准均不覆盖该窗口，HAZ 跳过"
                "（与 WAZ/BAZ 一致）；请按相邻标准表人工评估。")
        else:
            m, s = _interp_sd(_height_table(sex), age_months)
            if m is None or not _valid_sd(s):
                warnings.append("身高参考数据缺失，无法计算 HAZ。")
            else:
                # MED-GR-1（2026-08-15）：Z 用 7 界值分段插值（国标附录 B 非均匀 SD），
                # 不再 (x-m)/s 单 s 外推（±2SD/±3SD 系统性偏差 → 营养等级错判）。
                haz = _z_from_bands(height_cm, _interp_bands(_height_table(sex), age_months))
                haz_z = haz  # BUG-63：原始值用于判定
                d["haz"] = {
                    "z": _round(haz, 2),
                    "median_cm": _round(m, 1),
                    "sd_cm": _round(s, 2),
                    "height_cm": _round(height_cm, 1),
                    "grade": _grade_5(haz),
                    "nutrition": _haz_nutrition(haz),
                }
    else:
        warnings.append("未提供 height_cm，跳过 HAZ。")

    # WHZ（身长/身高别体重）—— M-8（2026-08-15）：此前 <2 岁关键指标（身长别体重）
    # 缺失。0-2 岁用身长别体重（WS/T 423 表 B.5/B.6，行键=身长 cm 45-100）；2-7 岁
    # 用身高别体重（表 B.7/B.8，行键=身高 cm 75-130）。Z 同用 7 界值分段插值。
    if height_cm is not None and weight_kg is not None and age_months < 84:
        if age_months < 24:
            _wf = ref["weight_for_length"][sex]
            _basis = "身长别体重（WS/T 423-2022 表 B.5/B.6，0-2 岁）"
            _wf_dom = (45.0, 100.0)
        else:
            _wf = ref["weight_for_height"][sex]
            _basis = "身高别体重（WS/T 423-2022 表 B.7/B.8，2-7 岁）"
            _wf_dom = (75.0, 130.0)
        if _wf_dom[0] <= height_cm <= _wf_dom[1]:
            _bands = _interp_bands(_wf, height_cm)
            if _bands is not None:
                whz = _z_from_bands(weight_kg, _bands)
                d["whz"] = {
                    "z": _round(whz, 2),
                    "median_kg": _round(_bands[3], 2),
                    "sd_kg": round((_bands[3] - _bands[2] + _bands[4] - _bands[3]) / 2, 2),
                    "weight_kg": _round(weight_kg, 2),
                    "length_cm": _round(height_cm, 1),
                    "grade": _grade_5(whz),
                    "wasting": "消瘦" if whz < -2 else ("超重" if whz > 2 else "正常"),
                    "basis": _basis,
                }
        else:
            warnings.append(
                f"WHZ：身高 {height_cm:.0f} cm 超出"
                f"{'身长别体重' if age_months < 24 else '身高别体重'}表覆盖域"
                f"（{_wf_dom[0]:.0f}-{_wf_dom[1]:.0f} cm），跳过。")

    # BUG-61（2026-08-12）：BMI 自动推算提前——原写在 <84 月分块内，≥7 岁（age_months>=84）
    # 且未显式传 bmi 时恒为 None，下方"BMI>24 学龄超重粗判"分支永不触发，超重漏诊并误判 normal。
    if bmi is None and height_cm and weight_kg:
        h_m = height_cm / 100.0
        bmi = weight_kg / (h_m * h_m)

    # WAZ / BAZ（仅 ≤81 月，WS/T 423 附录 B —— 参考表最大 81 月龄）
    # N2 修复（2026-08-13）：上限从 <84 收紧到 <=81——此前 age 81-83 月龄也进
    # _interp_sd，越界取端点（81 月参考值）被"压平"，产出假 Z 分且无告警。
    # 81-83 月龄显式跳过 + 告警，不静默使用边界参考值。
    if age_months <= 81:
        if weight_kg is not None:
            m, s = _interp_sd(ref["weight"][sex], age_months)
            if m is None or not _valid_sd(s):
                warnings.append("体重参考数据缺失，无法计算 WAZ。")
            else:
                # MED-GR-1：7 界值分段插值（非均匀 SD），界值点精确对应整数 Z
                waz = _z_from_bands(weight_kg, _interp_bands(ref["weight"][sex], age_months))
                waz_z = waz  # BUG-63：原始值用于判定
                d["waz"] = {
                    "z": _round(waz, 2),
                    "median_kg": _round(m, 2),
                    "sd_kg": _round(s, 2),
                    "weight_kg": _round(weight_kg, 1),
                    "grade": _grade_5(waz),
                    "nutrition": _waz_nutrition(waz),
                }
        else:
            warnings.append("未提供 weight_kg，跳过 WAZ。")
        if bmi is not None:
            m, s = _interp_sd(ref["bmi"][sex], age_months)
            if m is None or not _valid_sd(s):
                warnings.append("BMI 参考数据缺失，无法计算 BAZ。")
            else:
                # MED-GR-1：BMI 高尾非均匀最严重（81 月男童 +2SD→+3SD 间距 3.6 vs
                # −3SD→−2SD 0.9），单 s 外推可致超重/肥胖/重度肥胖错判，必须分段插值。
                baz = _z_from_bands(bmi, _interp_bands(ref["bmi"][sex], age_months))
                baz_z = baz  # BUG-63：原始值用于判定
                d["baz"] = {
                    "z": _round(baz, 2),
                    "median": _round(m, 2),
                    "sd": _round(s, 2),
                    "bmi": _round(bmi, 1),
                    "grade": _grade_5(baz),
                    "nutrition": _baz_nutrition(baz),
                }
        else:
            warnings.append("未提供 bmi/身高体重，跳过 BAZ。")
    elif age_months < 84:
        # N2 修复：81<age<84 月龄——WS/T 423 附录 B 参考表最大 81 月，无 82-83 月
        # 参考值；此前静默用 81 月端点值（压平），现在显式告警不产出假 Z 分。
        warnings.append(
            f"月龄 {age_months:.0f} 超出 WAZ/BAZ 参考表上限（81 月，WS/T 423 附录 B），"
            "无法计算 WAZ/BAZ，请结合 BMI 绝对值与生长曲线人工评估。")
    else:
        warnings.append(
            "WAZ/BAZ 仅 7 岁以下可用（WS/T 423 随附）；7-18 体重/BMI 标准未提供，跳过。")

    # PRNT growth_status 建议（供 calc_prnt_targets 输入）
    # 优先级：HAZ 生长迟缓 > WAZ 低体重/消瘦 > BAZ/BMI 超重
    # BUG-63：haz_z/waz_z/baz_z 均为未 round 的原始值（grade/nutrition 一直用原始值，
    # 此前 growth_status 误用 d[*]["z"] 的 round 后值，-2 边界 ±0.005 抖动）
    if haz_z is not None and haz_z < -2:
        growth_status = "failure"          # 生长迟缓 → 能量取 SDI 上限
    elif waz_z is not None and waz_z < -2:
        growth_status = "failure"          # 低体重/消瘦 → 能量取 SDI 上限
    elif baz_z is not None and baz_z < -2:
        # P0-2（2026-08-18）：**BAZ<-2 消瘦 → failure**——此前决策链只认 HAZ/WAZ 负向
        # 与 BAZ 正向（≥1 超重），BAZ=-2.43 时 _baz_nutrition 判"消瘦"但 growth_status
        # 落 else 判 normal（同函数自相矛盾），急性营养不良患儿能量按 SDI 中点而非上限
        # （促生长）。消瘦属生长衰竭（与 HAZ/WAZ<-2 同口径），取 SDI 上限。
        growth_status = "failure"
        warnings.append(
            f"BAZ {baz_z:.2f} < -2（消瘦，WS/T 423 年龄别 BMI），判生长衰竭，"
            "能量按 SDI 上限（促生长）；请结合临床评估。")
    elif baz_z is not None and baz_z >= 1:
        growth_status = "overweight"        # BAZ≥1 → 超重/肥胖，能量向下调整
    elif baz_z is None and bmi is not None and age_months >= 84:
        # M-3（2026-08-15）：≥7 岁无 BAZ 标准时用 WS/T 586-2018 年龄×性别别 BMI 超重
        # 界值判超重——此前 BMI>24 统一粗判漏判大量患儿（7 岁男超重界值 17.0、12 岁男
        # 20.7，BMI 17-24 区间本应超重却不判）。
        _ov_thr = _ws586_overweight_threshold(age_years, sex)
        if _ov_thr is not None and bmi >= _ov_thr:
            growth_status = "overweight"
            warnings.append(
                f"≥7 岁 BAZ 标准不可用，BMI {bmi:.1f} ≥ WS/T 586-2018 {age_years:.0f} 岁"
                f"{'男' if sex == 'M' else '女'}超重界值 {_ov_thr:.1f}，判超重；"
                "建议结合腰围/体脂综合评估。")
        elif _ov_thr is not None:
            # BUG-63（2026-08-12）：≥7 岁 BAZ 缺失且 BMI 未达超重界值——补消瘦/极低 BMI
            # 粗筛提示，杜绝"严重消瘦被判 normal 且无警告"的漏诊。**不改 growth_status**
            # （7-8 岁 BMI 14 属正常范围，扁平阈值直接调能量会误伤；仅提示人工评估）。
            if bmi < 14:
                warnings.append(
                    f"≥7 岁无 BAZ 标准，BMI {bmi:.1f} 极低（<14），提示消瘦/营养不良可能，"
                    "请立即结合中臂肌围等人体测量人工评估。")
            else:
                _thr_txt = (f"（WS/T 586-2018 {age_years:.0f} 岁"
                            f"{'男' if sex == 'M' else '女'}超重界值 {_ov_thr:.1f}）"
                            if _ov_thr is not None else "（≥7 岁无界值）")
                warnings.append(
                    f"≥7 岁无 BAZ 标准，BMI {bmi:.1f} 未达超重界值{_thr_txt}；"
                    "消瘦/营养不足判定请结合中臂肌围等人体测量人工评估。")
            growth_status = "normal"
        else:
            # P2-6（2026-08-18）：≥18 岁 WS/T 586 超窗（_ws586 上限 18.0）——此前
            # _ov_thr=None 静默落 normal，18.5 岁 BMI 33.8 判 normal（超窗静默）。
            # 成人超重界值按中国标准 BMI≥24（28 及以上为肥胖），能量同样向下调整。
            if age_years > 18.0 and bmi >= 24.0:
                growth_status = "overweight"
                warnings.append(
                    f"≥18 岁无 WS/T 儿童界值，BMI {bmi:.1f} ≥ 24（中国成人超重界值），"
                    "判超重；能量向下调整。")
            else:
                growth_status = "normal"
    else:
        growth_status = "normal"
    d["growth_status_suggestion"] = growth_status
    d["warnings"] = warnings
    return results


# ---------------------------------------------------------------------------
# PEW 历史存储（ADR-007：PEW 历史归属 M3）
# ---------------------------------------------------------------------------
# 依据 ADR-007：PEW 是 M3 assess_pew_risk 的产出，其历史时间线由 M3 拥有并落库；
# M4 仅作为聚合 facade 从 M3 读取（零跨包 import，M4 不自己存 PEW）。
# 存储为 <state>/pew_history_store.json，按 patient_id 追加历史点（路径延迟解析，见 BUG-18）。
_PEW_LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}


def _load_patient_pew_store(patient_id: str) -> dict:
    """患者级读 PEW 历史（N-MEM-3）：行级 GetRow(pk=patient_id)，不扫全表。

    返回 {patient_id: [points]}；B1 fail-closed（损坏/类型错误禁止静默清空）
    由 nutrition_repository 实现层保证。
    """
    return _repo().load_patient_pew(patient_id)


def _save_patient_pew_store(patient_id: str, points: list[dict[str, Any]]) -> None:
    """患者级写 PEW 历史（N-MEM-3）：只写该患者行（行级 _rev 乐观锁）。"""
    _repo().save_patient_pew(patient_id, points)


def record_pew_risk(patient_id: str, date: str, score: float, level: str) -> dict[str, Any]:
    """按 ADR-007，PEW 历史由 M3 拥有并落库。

    每次 assess_pew_risk 评估后，由编排层（router/PCP）调用本函数持久化一个历史点。
    :param patient_id: 患者标识（与 PCP 一致，^P[0-9]{4,}$）
    :param date: 评估日期 YYYY-MM-DD
    :param score: PEW 数值分（S2 修复 2026-08-13：来自 assess_pew_risk 返回的
        data.score 字段——信号加权 0-100；此前 assess 不返回 score，本参数无合法来源）
    :param level: PEW 风险等级 low / medium / high
    :return: 落库后该患者的完整历史点列表（身份由部署环境注入，P0-1）
    """
    caller = get_caller()
    enforce_nutrition_tool(caller, "record_pew_risk")  # 仅临床角色可落 PEW 历史
    # N1 修复（2026-08-13）：统一 patient_id 契约校验（畸形 id 不进写库）
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}
    if level not in _PEW_LEVEL_ORDER:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "level 必须是 low / medium / high"}
    # LOW-3 修复（2026-08-15）：score 有限性校验（对齐 N-S4 写路径口径）——此前
    # score 直接落库，NaN/Inf 可静默入库（读路径比较恒 False → 趋势判定失真、无告警），
    # 与 diary 写路径同口径 fail-closed。
    # P1-4（2026-08-18）：bool 在 float() 转换**之前**拒绝（float(True)=1.0 此前
    # 静默入库）；随后校验有限性与契约域 0-100（score=500/-50/101 拒绝）。
    if isinstance(score, bool):
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"score 不能为 bool（收到 {score!r}）"}
    try:
        score = float(score)
    except (TypeError, ValueError):
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"score 无法解析为数值：{score!r}"}
    if not math.isfinite(score):
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"score 必须为有限数值（NaN/Inf 拒绝）：{score!r}"}
    if not (0.0 <= score <= 100.0):
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"score 必须为 0-100 的数值（收到 {score!r}）"}
    # BUG-60：PEW 历史按日期排序去重，日期必须归一化，否则异形日期破坏时间线
    date = _normalize_date(date, "date")
    # 边界（2026-08-15）：未来日期拒绝——未来 PEW 点会成为趋势窗口的"未来锚点"
    # 导致趋势反转（record_pew_risk 写入未来 → 最新点在未来 → 趋势误判恶化/好转）。
    if date > datetime.now(timezone.utc).date().isoformat():
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"PEW 记录日期 {date} 晚于今天（未来日期），拒绝写入"}
    with _STORE_LOCK:
        # N-MEM-3（2026-08-14）：患者级读/写——此前 _load_pew_store() 全表 GetRange +
        # _save_pew_store() 回写每个患者行（record_pew_risk 每次评估后必调，数千患儿
        # 单次落库 = 全表扫描 + O(患者数) 写放大，与 N-MEM-2 修复前日记路径同构）。
        store = _load_patient_pew_store(patient_id)
        pts = store.get(patient_id, [])
        pts.append({
            "date": date,
            "score": score,
            "level": level,
            # C2 修复（2026-08-14）：aware UTC——此前 naive datetime.now() 与 care/P1
            # 的 UTC 口径混存（同进程多包写入 recorded_at 两种口径，审计/时间线错位）。
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "caller": caller,
        })
        # 同日只保留最新一次（覆盖更新）
        by_date: dict = {}
        for p in pts:
            by_date[p["date"]] = p
        ordered = [by_date[k] for k in sorted(by_date.keys())]
        _save_patient_pew_store(patient_id, ordered)
    # N4 修复（2026-08-13）：返回信封与其余工具统一 {ok, data}——此前扁平
    # {ok, patient_id, points} 与 get_pew_history 等不一致，编排层无法统一解包。
    return {"ok": True, "data": {"patient_id": patient_id, "points": ordered}}


def get_pew_history(patient_id: str) -> dict[str, Any]:
    """读取某患者的 PEW 历史（ADR-007：存储归属 M3）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    :param patient_id: 患者标识
    :return: 按日期升序的历史点 + 趋势判断（improving / worsening / stable / no_data）
    """
    caller = get_caller()
    # BUG-01 修复：PEW 历史属临床判读（NUTRITION_ASSESSMENT_CLINICAL_TOOLS 已登记），
    # 原实现用 enforce_read 导致家长（矩阵 R/W）可读 PEW 临床历史。
    enforce_nutrition_tool(caller, "get_pew_history")
    # N1 修复（2026-08-13）：统一 patient_id 契约校验
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}
    # N-MEM-3（2026-08-14）：患者级读（行级 GetRow）——此前 _load_pew_store() 全表
    # 拉取后再取键，医院级全库扫描。
    store = _load_patient_pew_store(patient_id)
    pts = store.get(patient_id, [])
    trend = "no_data"
    if len(pts) >= 2:
        # P1-7（2026-08-18）：legacy/脏点缺 level 不再 KeyError 500——此前
        # `first["level"]` 直索引（.get 只护外层字典），旧数据缺 level 时整接口崩。
        # 过滤出 level 合法的点参与趋势；不足 2 个有效点 → no_data（fail-closed，
        # 不把缺 level 的点当 low 静默参与趋势——可能掩盖历史高风险）。
        valid = [p for p in pts
                 if isinstance(p, dict)
                 and str(p.get("level", "")).strip().lower() in _PEW_LEVEL_ORDER]
        if len(valid) >= 2:
            first, last = valid[0], valid[-1]
            fo, lo = _PEW_LEVEL_ORDER[first["level"]], _PEW_LEVEL_ORDER[last["level"]]
            if lo > fo:
                trend = "worsening"
            elif lo < fo:
                trend = "improving"
            else:
                trend = "stable"
    # 一般 2（2026-08-14）：信封统一 {ok, data}——此前扁平 {ok, patient_id, count,
    # points, trend} 与 record_pew_risk 的 {ok, data} 不一致（core.py:1346 注释自称
    # "统一"但并未统一），编排层需双形态兼容。消费方（care get_pew_timeline /
    # content 报告）均为参数注入不解析返回，改信封无破坏。
    return {
        "ok": True,
        "data": {
            "patient_id": patient_id,
            "count": len(pts),
            "points": pts,
            "trend": trend,
        },
    }

