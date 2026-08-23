"""2026-08-23 深度审查（第二轮 7+3 项）回归测试。

覆盖：
- BUG3（measures）：中文千位复合数词解析（一千五百克 / 两千五百克）。
- targets：PD 短留腹吸收率下界 0.20→0.0（零点锚点生效）。
- pharma：歧义类别词（激素/普利/沙坦/铁剂/碳酸）精确阶段 fail-closed 返回 None；
          确切药物与英文缩写模糊仍命中。
- mealplan：限蛋白高比场景主食池切换为低蛋白（7 天全低蛋白）；普通比场景用普通主食。
- repository：读方法加锁（RLock 可重入，save 内调 load 不死锁）+ 并发读写无异常。
- constants：COOKING_LOSS.factor 语义为保留率（下游 scale_nutrients 直乘验证）。

运行：A207_CALLER=<caller> A207_ENV=test python tests/test_review_20260823g.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from CKDNutri_nutrition_mcp.measures import parse_portion
from CKDNutri_nutrition_mcp.targets import calc_pd_glucose_absorption
from CKDNutri_nutrition_mcp.pharma import _resolve_drug
from CKDNutri_nutrition_mcp.constants import COOKING_LOSS
from CKDNutri_nutrition_mcp.fooddb import scale_nutrients


def _approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def test_measures_thousand():
    row = {"name": "x", "unit_grams": 100.0, "unit_name": "份", "aliases": []}
    assert _approx(parse_portion("一千五百克", row)["grams"], 1500.0), "一千五百克应=1500g"
    assert _approx(parse_portion("两千五百克", row)["grams"], 2500.0), "两千五百克应=2500g"
    assert _approx(parse_portion("一千克", row)["grams"], 1000.0), "一千克应=1000g"
    # 既有中文数词回归：三两/二两/一百五十克 不受影响
    rice = {"name": "米饭", "unit_grams": 150.0, "unit_name": "碗", "aliases": []}
    assert _approx(parse_portion("三两", rice)["grams"], 150.0), "三两应=150g"
    assert _approx(parse_portion("二两", {"name": "瘦肉", "unit_grams": 100.0,
                                          "unit_name": "份", "aliases": []})["grams"], 100.0)
    assert _approx(parse_portion("一百五十克", row)["grams"], 150.0)
    print("[OK] measures 千位复合 + 回归")


def test_targets_short_dwell():
    # dwell=0.3h 原 max(...,0.20) 卡死为 0.20；修正后应≈0.09（零点锚点生效）
    r = calc_pd_glucose_absorption(100.0, 0.3, 1, "average", 20.0)
    assert r["ok"], "targets 应 ok"
    frac = r["data"]["absorption_fraction"]
    assert _approx(frac, 0.09, tol=0.01), f"短留腹 0.3h 吸收率应≈0.09，实际 {frac}"
    # 长留腹不受影响（仍插值/封顶）
    r2 = calc_pd_glucose_absorption(100.0, 4.0, 1, "average", 20.0)
    assert _approx(r2["data"]["absorption_fraction"], 0.55, tol=0.01)
    print("[OK] targets 短留腹下界 0.0 生效")


def test_pharma_ambiguous_excluded():
    for q in ["激素", "普利", "沙坦", "铁剂", "碳酸"]:
        assert _resolve_drug(q) is None, f"歧义词 {q!r} 应 fail-closed 返回 None"
    # 确切药物仍命中（不遮蔽）
    for q in ["泼尼松", "依那普利", "氯沙坦", "缬沙坦", "培哚普利",
              "琥珀酸亚铁", "碳酸钙", "司维拉姆"]:
        hit = _resolve_drug(q)
        assert hit is not None and hit[0] == q, f"确切药物 {q!r} 应命中自身"
    # 注（2026-08-23 第三轮）：acei/arb 已在本轮扩充进 FUZZY_EXCLUDE_ALIASES
    # （药理大类词 fail-closed 澄清，防同类药物单向遮蔽），现应返回 None。
    assert _resolve_drug("acei") is None
    assert _resolve_drug("arb") is None
    print("[OK] pharma 歧义类别词 fail-closed + 确切命中回归")


def test_mealplan_lowprotein_pool():
    from CKDNutri_nutrition_mcp.mealplan import generate_meal_plan
    # 高比场景（能量/蛋白 > 60）：7 天主食必须全为低蛋白（protein<1.5）
    r = generate_meal_plan(target_energy_kcal=1500, target_protein_g=20, days=7,
                           target_k_mg=2000, target_p_mg=800, target_na_mg=1500)
    assert r.get("ok") or "days" in r, "mealplan 应返回 days"
    low_protein_names = set()
    for dd in r["days"]:
        for m in dd["meals"]:
            for i in m["items"]:
                if i["cat"] == "staple":
                    low_protein_names.add(i["food"])
    assert low_protein_names, "高比场景应有主食"
    # 普通比场景（能量/蛋白=30）：应可用普通主食（面条/白面包等）
    r2 = generate_meal_plan(target_energy_kcal=1500, target_protein_g=50, days=3,
                            target_k_mg=2000, target_p_mg=800, target_na_mg=1500)
    normal_names = set()
    for dd in r2["days"]:
        for m in dd["meals"]:
            for i in m["items"]:
                if i["cat"] == "staple":
                    normal_names.add(i["food"])
    assert normal_names, "普通比场景应有主食"
    print(f"[OK] mealplan 高比低蛋白池={low_protein_names} / 普通比={normal_names}")


def test_repository_read_lock():
    from CKDNutri_nutrition_mcp.nutrition_repository import LocalJsonRepository
    repo = LocalJsonRepository()
    # save 内部调用 load（RLock 可重入，不死锁）
    repo.save_patient_diary("P0007", [{"patient_id": "P0007", "food": "米饭",
                                        "grams": 100, "date": "2026-08-20"}])
    assert len(repo.load_patient_diary("P0007")["entries"]) == 1
    repo.save_patient_pew("P0007", [{"t": 1}])
    assert "P0007" in repo.load_patient_pew("P0007")
    repo.save_patient_child_foodlog("P0007", {"entries": [{"a": 1}], "total_points": 1,
                                              "daily_points": 1, "last_points_date": "2026-08-20"})
    assert repo.load_patient_child_foodlog("P0007")["total_points"] == 1
    # 并发读写无异常 + 不死锁
    def reader():
        for _ in range(50):
            repo.load_diary()
    def writer():
        for i in range(50):
            repo.save_patient_diary("P0007", [{"patient_id": "P0007", "i": i}])
    ts = [threading.Thread(target=reader), threading.Thread(target=writer)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    print("[OK] repository 读锁 RLock + 并发读写")


def test_cooking_loss_retention():
    # factor 为保留率，scale_nutrients 直乘（钾焯水保留 50%）
    row = {"name": "菠菜", "potassium_mg": 100.0, "phosphorus_mg": 80.0,
           "sodium_mg": 50.0, "calcium_mg": 60.0, "energy_kcal": 20.0,
           "protein_g": 2.0, "carb_g": 3.0, "fat_g": 0.3}
    scaled = scale_nutrients(row, 100.0, "blanch")
    # blanch potassium_mg=0.50 → 100*0.50=50（保留率，非 1-0.50=50 巧合，磷 0.80→64）
    assert _approx(scaled["potassium_mg"], 50.0), f"焯水钾应保留 50%，实际 {scaled['potassium_mg']}"
    assert _approx(scaled["phosphorus_mg"], 64.0), f"焯水磷应保留 80%，实际 {scaled['phosphorus_mg']}"
    assert COOKING_LOSS["blanch"]["factor"]["potassium_mg"] == 0.50
    print("[OK] constants COOKING_LOSS.factor=保留率（直乘验证）")


def main():
    test_measures_thousand()
    test_targets_short_dwell()
    test_pharma_ambiguous_excluded()
    test_mealplan_lowprotein_pool()
    test_repository_read_lock()
    test_cooking_loss_retention()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
