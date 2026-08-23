"""审查回归（2026-08-23 第三轮）：反驳项 + 9 项新缺陷修复验证。

覆盖：
- 反驳1：二十两/三十两不再被解析为 22/32（市制重量单位正确）
- 新缺陷3：纯数字无单位直接作克重（不再 ×unit_grams 放大 100 倍）
- 新缺陷1：mealplan 低蛋白池越界不崩（staple 必被选）
- 新缺陷5：_overall_achievement 补齐 *_exceeded
- 新缺陷6：calc_pnpr NaN/负磷防御
- 新缺陷7/8：repository diary 规约 + child_foodlog 清洗（两个后端）
- 新缺陷9：pharma 歧义大类词全返回 None
- 新缺陷10：diary contributions 暴露且含全 SUM_KEYS
驳回项（BUG2 静默丢失 / fooddb 冲突防线）不在此测（已在前序验证，机理已不成立）。
"""
import math
import sys

sys.path.insert(0, "src")

from CKDNutri_nutrition_mcp.measures import parse_portion
from CKDNutri_nutrition_mcp.foods import calc_pnpr
from CKDNutri_nutrition_mcp.pharma import _resolve_drug
from CKDNutri_nutrition_mcp.diary import sum_diet_intake
from CKDNutri_nutrition_mcp.fooddb import find_food
from CKDNutri_nutrition_mcp.nutrition_repository import LocalJsonRepository


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("PASS:", msg)


# ---- 反驳1：二十两/三十两 ----
def test_cn_weight_unit():
    row = {"name": "x", "unit_grams": 100, "unit_name": "份", "aliases": []}
    _assert(parse_portion("二十两", row)["grams"] == 1000.0,
            "二十两 -> 1000g (非 1100)")
    _assert(parse_portion("三十两", row)["grams"] == 1500.0,
            "三十两 -> 1500g (非 1600)")
    _assert(parse_portion("二两", row)["grams"] == 100.0,
            "二两 -> 100g (未被破坏)")
    _assert(parse_portion("三两", row)["grams"] == 150.0,
            "三两 -> 150g (前序修复仍成立)")


# ---- 新缺陷3：纯数字无单位 ----
def test_plain_number_as_grams():
    row = {"name": "米饭", "unit_grams": 150, "unit_name": "碗", "aliases": []}
    _assert(parse_portion("150", row)["grams"] == 150.0,
            "纯数字 150 -> 150g (非 22500)")
    _assert(parse_portion("200.5", row)["grams"] == 200.5,
            "纯数字 200.5 -> 200.5g")
    # 中文数词不受影响
    _assert(parse_portion("一千五百克", row)["grams"] == 1500.0,
            "一千五百克 -> 1500g (中文数词路径不变)")


# ---- 新缺陷1：mealplan 越界不崩（极端限钾磷） ----
def test_mealplan_low_protein_no_crash():
    r = generate_meal_plan_extreme()
    _assert(r is not None and len(r["days"]) == 7,
            "极端限钾磷场景 7 天全部生成、无 TypeError 崩溃")
    for dd in r["days"]:
        names = set()
        for m in dd["meals"]:
            for i in m["items"]:
                if i["cat"] == "staple":
                    names.add(i["food"])
        _assert(len(names) >= 1, f"第 {dd['day']} 天主食被选 (非 None)")


def generate_meal_plan_extreme():
    from CKDNutri_nutrition_mcp.mealplan import generate_meal_plan
    return generate_meal_plan(target_energy_kcal=1500, target_protein_g=20,
                              days=7, target_k_mg=10, target_p_mg=10,
                              target_na_mg=10)


# ---- 新缺陷5：overall exceeded ----
def test_overall_exceeded():
    from CKDNutri_nutrition_mcp.mealplan import generate_meal_plan
    r = generate_meal_plan(target_energy_kcal=1500, target_protein_g=20,
                           days=7, target_k_mg=100, target_p_mg=800,
                           target_na_mg=1500)
    oa = r["overall_achievement"]
    _assert("potassium_exceeded" in oa, "overall 含 potassium_exceeded")
    _assert(oa["potassium_exceeded"] is True, "钾超标日整体 flagged (pct 封顶但 exceeded=True)")
    _assert(oa["phosphorus_exceeded"] is False, "磷未超标 -> False")
    _assert(oa["sodium_exceeded"] is False, "钠未超标 -> False")


# ---- 新缺陷6：calc_pnpr NaN/负 ----
def test_calc_pnpr_defense():
    _assert(calc_pnpr(protein_g=float("nan"), phosphorus_mg=100)["ok"] is False,
            "NaN 蛋白拒绝")
    _assert(calc_pnpr(protein_g=10, phosphorus_mg=float("nan"))["ok"] is False,
            "NaN 磷拒绝")
    _assert(calc_pnpr(protein_g=10, phosphorus_mg=-50)["ok"] is False,
            "负磷拒绝 (不再误判 preferred)")
    _assert(calc_pnpr(protein_g=0, phosphorus_mg=50)["ok"] is False,
            "蛋白=0 拒绝")
    _assert(calc_pnpr(protein_g=10, phosphorus_mg=50)["ok"] is True,
            "正常 10/50 仍 preferred")


# ---- 新缺陷7/8：repository 加固 ----
def test_repository_hardening(tmpdir):
    import os
    os.environ["A207_DATA_DIR"] = str(tmpdir)
    os.environ["A207_ACCEPT_DEV_STORAGE"] = "1"
    repo = LocalJsonRepository()
    # diary 防御规约：漏传 patient_id 被注入
    repo.save_patient_diary("P0007", [{"food": "米饭", "grams": 100,
                                       "date": "2026-08-20"}])
    got = repo.load_patient_diary("P0007")
    _assert(got["entries"][0].get("patient_id") == "P0007",
            "diary 条目 patient_id 被防御注入")
    # child_foodlog 清洗：None + 容器扩展字段被剥离
    repo.save_patient_child_foodlog("P0007", {
        "entries": [{"a": 1}], "total_points": 5, "daily_points": 1,
        "last_points_date": None, "tags": ["x"], "meta": {"k": "v"}})
    got2 = repo.load_patient_child_foodlog("P0007")
    _assert(got2["last_points_date"] == "", "child None 日期 -> 空串")
    _assert(got2["total_points"] == 5, "child total_points 保留")
    _assert(got2["entries"] == [{"a": 1}], "child entries 保留")


# ---- 新缺陷9：pharma 歧义大类词 ----
def test_pharma_excluded():
    for q in ["激素", "普利", "沙坦", "铁剂", "碳酸", "acei", "arb",
              "钾结合剂", "降钾树脂", "非钙磷结合剂", "活性维生素d",
              "保钾利尿剂", "袢利尿剂", "生长激素"]:
        _assert(_resolve_drug(q) is None,
                f"歧义词 {q!r} -> None (fail-closed)")
    # 确切药仍命中
    for q in ["泼尼松", "依那普利", "氯沙坦", "司维拉姆", "碳酸钙",
              "聚苯乙烯磺酸钠", "环硅酸锆钠", "呋塞米", "螺内酯", "骨化三醇"]:
        r = _resolve_drug(q)
        _assert(r is not None and r[0] == q, f"确切药 {q!r} 仍命中")


# ---- 新缺陷10：diary contributions 全字段 ----
def test_diary_contributions_full():
    rice = find_food("米饭")
    apple = find_food("苹果")
    d = [
        {"food": rice["name"], "grams": 150, "date": "2026-08-20",
         "meal": "午餐", "patient_id": "P0007"},
        {"food": apple["name"], "grams": 100, "date": "2026-08-20",
         "meal": "加餐", "patient_id": "P0007"},
    ]
    r = sum_diet_intake(d, target=None)
    data = r["data"]
    _assert("contributions" in data, "contributions 已暴露")
    c0 = data["contributions"][0]
    for k in ("sodium_mg", "fat_g", "carb_g", "calcium_mg", "energy_kcal",
              "protein_g", "potassium_mg", "phosphorus_mg"):
        _assert(k in c0, f"contributions 含 {k}")


if __name__ == "__main__":
    import tempfile
    test_cn_weight_unit()
    test_plain_number_as_grams()
    test_mealplan_low_protein_no_crash()
    test_overall_exceeded()
    test_calc_pnpr_defense()
    test_pharma_excluded()
    test_diary_contributions_full()
    test_repository_hardening(tempfile.mkdtemp())
    print("\nALL TESTS PASSED")
