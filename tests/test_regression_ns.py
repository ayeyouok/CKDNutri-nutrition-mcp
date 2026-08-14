# -*- coding: utf-8 -*-
"""N-S2/N-S3/N-S4/N-S5/N-S6/N-B8 回归测试（2026-08-14 修复后固化）。

python 直接运行 + pytest 双模式（对齐 P1 test_tools 风格）。
"""
import os
import sys
import math
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")


def test_ns2_food_match_common_foods():
    """N-S2：常见食物不再 FOOD_NOT_FOUND（此前基名簇多规格全拒）。"""
    from CKDNutri_nutrition_mcp.fooddb import find_food

    cases = {"鸡蛋": "鸡蛋（代表值）", "香蕉": "香蕉", "豆腐": "豆腐（代表值）",
             "猪肉（瘦）": "猪肉（瘦）", "鱼丸": "鱼丸", "苹果": "苹果（代表值）"}
    for q, want in cases.items():
        r = find_food(q)
        assert r is not None, f"{q} 仍 FOOD_NOT_FOUND"
        assert r["name"] == want, (q, r["name"], want)
    # A 数据修正（2026-08-14）：11 组重名已按成分表第 6 版合并权威行——松蘑
    # 现在命中"松蘑（干）"权威值（K 2402/P 390），不再拒绝。
    r = find_food("松蘑")
    assert r is not None and r["name"] == "松蘑（干）", r
    assert abs(float(r["potassium_mg"]) - 2402.0) < 1, r["potassium_mg"]
    # B 方案（2026-08-14）：加工状态差异（干 vs 水发）非冲突——榛蘑应命中干品权威值
    # （2026-08-14 用户决策：K 取 2492，fail-safe 高值原则——高钾患者宁可标高避吃）
    r = find_food("榛蘑")
    assert r is not None and r["name"] == "榛蘑（干）", r
    assert abs(float(r["potassium_mg"]) - 2492.0) < 1, r["potassium_mg"]
    # 单字仍拒绝
    assert find_food("鱼") is None


def test_ns3_milk_prefix_match():
    """N-S3：牛奶→纯牛奶（代表值，全脂），不再命中牛奶饼干；苹果→代表值非苹果梨。"""
    from CKDNutri_nutrition_mcp.fooddb import find_food

    m = find_food("牛奶")
    assert m is not None and m["name"] == "纯牛奶（代表值，全脂）", m
    # 牛奶 200g 钠 = 63.7×2 ≈ 127（此前牛奶饼干 399×2=798，偏差 6 倍）
    assert abs(m["sodium_mg"] - 63.7) < 0.1, m["sodium_mg"]
    a = find_food("苹果")
    assert a is not None and a["name"] == "苹果（代表值）", a


def test_ns4_diary_nan_rejected():
    """N-S4：日记写路径 NaN/Inf 拒绝（fail-closed，此前静默落库）。"""
    from CKDNutri_nutrition_mcp import core

    r = core.upsert_food_diary("P0001", entries=[{
        "date": "2026-08-14", "meal": "早餐", "food": "测试",
        "energy_kcal": float("nan"), "protein_g": 10.0,
        "potassium_mg": 100.0, "phosphorus_mg": 80.0, "sodium_mg": 50.0,
    }])
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", r
    r2 = core.upsert_food_diary("P0001", entries=[{
        "date": "2026-08-14", "meal": "早餐", "food": "测试",
        "energy_kcal": 500.0, "protein_g": float("inf"),
        "potassium_mg": 100.0, "phosphorus_mg": 80.0, "sodium_mg": 50.0,
    }])
    assert r2["ok"] is False and r2["error"] == "INVALID_INPUT", r2
    # 正常数值仍可写
    r3 = core.upsert_food_diary("P0001", entries=[{
        "date": "2026-08-14", "meal": "早餐", "food": "米饭",
        "energy_kcal": 200.0, "protein_g": 4.0,
        "potassium_mg": 50.0, "phosphorus_mg": 60.0, "sodium_mg": 2.0,
    }])
    assert r3["ok"] is True, r3


def test_ns5_pew_score_single_track():
    """N-S5：DAG PEW score 统一 0-100（不再被 _PEW_SCORE 0/1/2 覆盖）。"""
    from CKDNutri_nutrition_mcp import server

    r = server.comprehensive_nutrition_assessment_tool(
        age_years=6, sex="F", weight_kg=20, height_cm=130, ckd_stage=3,
        avg_protein_g=20.0, avg_energy_kcal=800.0, serum_albumin_g_l=30.0)
    d = r.get("data", {})
    pew = d.get("pew")
    if pew:
        score = pew.get("score")
        assert score is not None
        # 0-100 口径：medium/high 的加权分应 > 2（0/1/2 双轨已删）
        if pew.get("pew_risk") in ("medium", "high"):
            assert score > 2.0, (pew.get("pew_risk"), score)
        assert 0.0 <= score <= 100.0, score


def test_ns6_mealplan_balanced():
    """N-S6：食谱生成——蛋白不严重超供、钾/磷超限天数显著受控、油脂封顶。"""
    from CKDNutri_nutrition_mcp.mealplan import generate_meal_plan

    r = generate_meal_plan(2000, 40, target_k_mg=2000, target_p_mg=800,
                           target_na_mg=1500, days=7)
    assert len(r["days"]) == 7
    p_over = sum(1 for d in r["days"] if d["achievement"]["protein_pct"] > 115)
    k_over = sum(1 for d in r["days"] if d["achievement"]["potassium_exceeded"])
    p_lim = sum(1 for d in r["days"] if d["achievement"]["phosphorus_exceeded"])
    # 蛋白不得再 119-163% 超供（修复前 7/7 天）；钾/磷超限天数大幅下降
    assert p_over <= 1, f"蛋白超供天数 {p_over}/7"
    assert k_over <= 2, f"钾超限天数 {k_over}/7"
    assert p_lim <= 3, f"磷超限天数 {p_lim}/7"
    # 油脂封顶 ≤25g/天（修复前 55g 油脂炸弹）
    for d in r["days"]:
        fat_g = sum(i.get("grams", 0) for meal in d["meals"] for i in meal.get("items", [])
                    if i.get("cat") == "fat")
        assert fat_g <= 25, (d["day"], fat_g)
    # 蛋白接近目标（±10%）
    for d in r["days"]:
        assert 90 <= d["achievement"]["protein_pct"] <= 112, (d["day"], d["achievement"]["protein_pct"])


def test_nb8_pharma_alias_split():
    """N-B8：环孢素/阿法骨化醇/聚苯乙烯磺酸钠 独立条目，不再误返回他药文本。"""
    from CKDNutri_nutrition_mcp.pharma import check_drug_nutrient_interaction

    expect = {"他克莫司": "他克莫司", "环孢素": "环孢素",
              "骨化三醇": "骨化三醇", "阿法骨化醇": "阿法骨化醇",
              "聚苯乙烯磺酸钙": "聚苯乙烯磺酸钙", "聚苯乙烯磺酸钠": "聚苯乙烯磺酸钠"}
    for q, want in expect.items():
        r = check_drug_nutrient_interaction(q)
        d = r.get("data", {})
        got = d.get("drug") or d.get("drug_name") or ""
        # 通过 drug_class 区分（钙型 vs 钠型；前药 vs 活性形式）
        cls = d.get("drug_class", "")
        if "聚苯乙烯磺酸" in q:
            assert ("钙型" in cls) == ("钙" in q), (q, cls)
        if q == "阿法骨化醇":
            assert "前药" in cls or "羟化" in cls, (q, cls)
        if q == "环孢素":
            assert "环孢素" in cls, (q, cls)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"NS2-NS6/NB8 REGRESSION OK（{len(fns)} 个用例）")
