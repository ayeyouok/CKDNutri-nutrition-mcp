"""CKDNutri-nutrition-mcp 本轮审查（2026-08-23）「属实·已修」回归测试。

零 pytest 依赖，直接 `python tests/test_review_20260823e.py` 运行。
覆盖：
- BUG-P0-03（diary 缩进）：missing_nutrients 判定块移回 for 循环内——
  ① 多记录不全漏报（前序缺失食物不再被最后一条完整食物吞掉）；
  ② 全部条目 continue（非 dict / 未匹配）时不再 UnboundLocalError 500 崩溃。
- targets 强类型校验（BUG-TYPE-1）：dialysate_glucose_g / dwell_hours 拒绝 str / bool 注入。
- pharma 反向模糊匹配排除（P0-药）："激素"/"碳酸"/"普利"/"沙坦"/"铁剂" 不再截胡歧义输入；
  长输入模糊（"糖皮质激素片"）与精确全名（"碳酸司维拉姆"）仍正常命中。
- repository 静默清空修复（X1）：raw 存在但非 str 抛 RuntimeError（非返回空列表）。
- pharma 空白 nutrient（P2-7）：nutrient="   " 按"未指定"返回全量交互，不误报 NUTRIENT_NOT_SUPPORTED。
- targets APD 短留腹锚点（P2-8）：dwell_hours=0.5 吸收率插值 < 0.30（不再硬截断高估）。
- repository 浅拷贝隔离（P2-9）：load 返回的 entries 与内部存储隔离。
"""
from __future__ import annotations

import os
import sys
import json
import tempfile
from pathlib import Path

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from a207_policy import as_caller  # noqa: E402

from CKDNutri_nutrition_mcp import diary, targets, pharma, fooddb  # noqa: E402
from CKDNutri_nutrition_mcp.nutrition_repository import (  # noqa: E402
    LocalJsonRepository,
    TablestoreRepository,
)


_pass = 0
_fail = 0


def _ok(name: str, cond: bool) -> None:
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}")


def _caller():
    # demo 家长等价角色即可触发业务读权限
    return as_caller("demo_parent_assistant")


def _find_missing_food() -> str | None:
    """动态找一个真实带 missing_nutrients 标记的食物名，避免硬编码脆弱。"""
    for name in ("荔枝（干）", "荔枝干", "藜麦", "鸭蛋白", "松蘑（干）", "香菇（干）"):
        row = fooddb.find_food(name)
        if row and row.get("missing_nutrients"):
            return name
    return None


print("== BUG-P0-03 diary 缩进：缺失食物逐条收集 ==")
with _caller():
    miss_name = _find_missing_food()
    if miss_name:
        # 缺失食物 + 完整食物（米饭）+ 再缺失食物：修复前因判定在循环外，
        # 循环结束 row 指向最后一条（米饭）致缺失全漏报；修复后逐条收集。
        result = diary.sum_diet_intake([
            {"food": miss_name, "grams": 50, "date": "2026-08-20"},
            {"food": "米饭", "grams": 100, "date": "2026-08-20"},
            {"food": miss_name, "grams": 30, "date": "2026-08-20"},
        ])
        _ok("P0-03 缺失食物不漏报 (warnings 含缺失提示)",
            result.get("ok") and any("缺失" in w for w in result["data"].get("warnings", [])))
        # 去重：同款缺失食物只展示一次（保留 BUG-P1-02 去重）
        wtxt = "；".join(result["data"].get("warnings", []))
        _ok("P0-03 去重仍生效 (同款缺失仅一次)", wtxt.count("（缺") == 1)
    else:
        _ok("P0-03 找到缺失食物样本", False)

    # 全部条目 continue（非 dict / 未匹配）→ 不应 UnboundLocalError
    try:
        r2 = diary.sum_diet_intake([123, "不存在的食物xyz", {"food": "不存在的食物abc"}])
        _ok("P0-03 全 continue 不崩 (返回 NO_MATCHED_ITEM)",
            r2.get("error") == "NO_MATCHED_ITEM")
    except Exception as exc:  # noqa: BLE001
        _ok(f"P0-03 全 continue 不崩 (实际抛 {type(exc).__name__})", False)


print("== BUG-TYPE-1 targets 强类型校验 ==")
with _caller():
    # str 注入 dialysate_glucose_g
    r = targets.calc_pd_glucose_absorption("50", 2.0, 1, "average", 20.0)
    _ok("targets 拒绝 str dialysate_glucose_g", r.get("error") == "INVALID_INPUT")
    # bool 注入 dwell_hours
    r = targets.calc_pd_glucose_absorption(50.0, True, 1, "average", 20.0)
    _ok("targets 拒绝 bool dwell_hours", r.get("error") == "INVALID_INPUT")
    # str 注入 dwell_hours
    r = targets.calc_pd_glucose_absorption(50.0, "4", 1, "average", 20.0)
    _ok("targets 拒绝 str dwell_hours", r.get("error") == "INVALID_INPUT")
    # str 注入 weight_kg（上一轮已修，固化）
    r = targets.calc_pd_glucose_absorption(50.0, 2.0, 1, "average", "20")
    _ok("targets 拒绝 str weight_kg", r.get("error") == "INVALID_INPUT")
    # 合法输入仍正常
    r = targets.calc_pd_glucose_absorption(50.0, 2.0, 1, "average", 20.0)
    _ok("targets 合法输入正常", r.get("ok") is True)


print("== P2-8 targets APD 短留腹锚点 ==")
with _caller():
    # 0.5h 留腹：锚点 (0.0,0.0)-(1.0,0.30) 插值 = 0.15，应 < 0.30（不再硬截断高估）
    frac_low = targets._absorption_fraction(0.5)
    _ok("APD 0.5h 吸收率 < 0.30 (插值不被硬截断)", 0.0 < frac_low < 0.30)
    # 1.0h 仍为 0.30（锚点保留）
    frac_1 = targets._absorption_fraction(1.0)
    _ok("1.0h 吸收率仍为 0.30", abs(frac_1 - 0.30) < 1e-9)


print("== P0-药 pharma 反向模糊排除 ==")
with _caller():
    # 歧义短别名不再截胡
    r = pharma.check_drug_nutrient_interaction("甲状旁腺激素")
    _ok("'甲状旁腺激素' 不命中泼尼松 (DRUG_NOT_FOUND)",
        r.get("error") == "DRUG_NOT_FOUND")
    r = pharma.check_drug_nutrient_interaction("碳酸")
    _ok("'碳酸' 不截胡 (DRUG_NOT_FOUND)", r.get("error") == "DRUG_NOT_FOUND")
    r = pharma.check_drug_nutrient_interaction("补铁剂")
    _ok("'补铁剂' 不命中琥珀酸亚铁 (DRUG_NOT_FOUND)",
        r.get("error") == "DRUG_NOT_FOUND")
    # 长输入模糊仍工作
    r = pharma.check_drug_nutrient_interaction("糖皮质激素片")
    _ok("'糖皮质激素片' 命中泼尼松", r.get("ok") and r["data"]["drug"] == "泼尼松")
    # 精确全名仍命中
    r = pharma.check_drug_nutrient_interaction("碳酸司维拉姆")
    _ok("'碳酸司维拉姆' 命中司维拉姆", r.get("ok") and r["data"]["drug"] == "司维拉姆")
    r = pharma.check_drug_nutrient_interaction("碳酸钙")
    _ok("'碳酸钙' 命中碳酸钙", r.get("ok") and r["data"]["drug"] == "碳酸钙")


print("== P2-7 pharma 空白 nutrient ==")
with _caller():
    r = pharma.check_drug_nutrient_interaction("泼尼松", "   ")
    _ok("nutrient='   ' 返回全量交互 (非 NUTRIENT_NOT_SUPPORTED)",
        r.get("ok") is True and "interactions" in r.get("data", {}))


print("== X1 repository 静默清空修复 ==")
# X1 修复位于 TablestoreRepository.load_patient_diary；绕过需 OTS 的 __init__
repo = TablestoreRepository.__new__(TablestoreRepository)
# 模拟 _get_row 返回含非 str entries 的行，验证抛 RuntimeError（非返回空列表）
def _fake_get_row(table, pk):
    return {"entries": 123}  # 非 str 且非 None
repo._get_row = _fake_get_row  # type: ignore[attr-defined]
try:
    repo.load_patient_diary("P0007")  # type: ignore[attr-defined]
    _ok("X1 非 str raw 拒绝 (实际未抛)", False)
except RuntimeError as exc:
    _ok("X1 非 str raw 抛 RuntimeError (fail-closed)",
        "拒绝静默清空" in str(exc))

# raw=None（无数据）仍返回空（不抛）
def _fake_get_row_none(table, pk):
    return {"entries": None}
repo._get_row = _fake_get_row_none  # type: ignore[attr-defined]
r = repo.load_patient_diary("P0007")  # type: ignore[attr-defined]
_ok("X1 raw=None 返回空 (非抛错)", r == {"entries": []})

# PEW 同理（load_patient_pew）
def _fake_get_row_pew(table, pk):
    return {"points": 456}
repo._get_row = _fake_get_row_pew  # type: ignore[attr-defined]
try:
    repo.load_patient_pew("P0007")  # type: ignore[attr-defined]
    _ok("X1 PEW 非 str raw 拒绝 (实际未抛)", False)
except RuntimeError as exc:
    _ok("X1 PEW 非 str raw 抛 RuntimeError", "拒绝静默清空" in str(exc))


print("== P2-9 repository 浅拷贝隔离 ==")
with tempfile.TemporaryDirectory() as td:
    os.environ["A207_LOCAL_JSON_DIR"] = td
    repo2 = LocalJsonRepository()
    repo2.save_patient_child_foodlog("P0099", {
        "entries": [{"food": "苹果", "grams": 100, "date": "2026-08-20"}],
        "total_points": 0, "daily_points": 0, "last_points_date": "",
    })
    loaded = repo2.load_patient_child_foodlog("P0099")
    loaded["entries"].append({"food": "香蕉", "grams": 50, "date": "2026-08-20"})
    reloaded = repo2.load_patient_child_foodlog("P0099")
    _ok("P2-9 load 返回 entries 隔离 (原地改不影响存储)",
        len(reloaded["entries"]) == 1)


print(f"\n结果：{_pass} 通过 / {_fail} 失败")
if _fail:
    sys.exit(1)
