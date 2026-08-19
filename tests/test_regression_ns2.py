"""十五审（2026-08-18）nutrition-mcp 三审回归：P0×3 / P1×7 / P2 关键项。

覆盖：单列 0 缺失判定、BAZ<-2 生长衰竭、height_cm 有限性、find_food 括号/基名/
单字/别名解析、中文数词、score 0-100、floor_protein_g、非 dict 日记聚合、PEW 缺
level、meal 枚举、未来日期排除、括号克重、cooking 组合、json 后端启动。
"""
import os

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
import sys
import tempfile
from pathlib import Path

# 存储隔离：PEW/日记写路径落到临时目录（避免跨运行污染仓库状态）
os.environ["A207_NUTRITION_ASSESSMENT_DATA_DIR"] = tempfile.mkdtemp(prefix="a207-nutrition-test-")

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from a207_policy import as_caller

from CKDNutri_nutrition_mcp import core, diary, fooddb, measures


def test_p01_single_column_zero_flagged_missing():
    """P0-1：单列字面 K=0/P=0 判缺失（荔枝干真实 K≈900、藜麦蛋白>0 必有磷）。"""
    rows = fooddb.load_foods()
    lizhi = fooddb.find_food("荔枝（干）")
    assert lizhi and "potassium_mg" in lizhi["missing_nutrients"], lizhi
    quinoa = fooddb.find_food("藜麦（散装）")
    assert quinoa and "phosphorus_mg" in quinoa["missing_nutrients"], quinoa
    # 单列 0 行被标记后，其缺失键值确为 0（与新断言契约一致）
    for r in rows:
        for k in r.get("missing_nutrients", []):
            assert r[k] in (0, 0.0), (r["name"], k, r[k])


def test_p02_baz_minus2_wasting_is_failure():
    """P0-2：BAZ<-2（消瘦）→ growth_status=failure（此前落 normal，急性营养不良
    能量按 SDI 中点而非上限）。3 岁男 95cm / 12kg → BAZ=-2.38 消瘦。"""
    r = core.calc_growth_zscore(age_years=3, sex="M", height_cm=95.0, weight_kg=12.0)
    assert r["ok"] is True, r
    assert r["data"]["baz"]["nutrition"] == "消瘦", r["data"]["baz"]
    assert r["data"]["growth_status_suggestion"] == "failure", r["data"]


def test_p03_height_nan_rejected():
    """P0-3：calc_prnt_targets height_cm=NaN/负 → ValueError（此前穿透产出 NaN
    + flag=consistent）；0=未提供哨兵放行。"""

    for bad in (float("nan"), -5.0, float("inf")):
        try:
            core.calc_prnt_targets(age_years=8, sex="M", weight_kg=25.0, height_cm=bad)
        except ValueError:
            continue
        raise AssertionError(f"height_cm={bad!r} 未拒绝")
    r = core.calc_prnt_targets(age_years=8, sex="M", weight_kg=25.0, height_cm=0.0)
    assert r["ok"] is True, r


def test_p11_find_food_display_names_all_resolve():
    """P1-1：34 个显示名全部可解析（半角括号归一 + csv 别名）——米粉(熟)→109 kcal
    不再错行 3.2 倍；米饭(熟)/面条(熟)/瘦猪肉 等不再 None。"""
    checks = {
        "米粉(熟)": ("米粉（熟）", 109.0),
        "米饭(熟)": ("粳米饭（蒸）", 118.0),
        "面条(熟)": ("面条（富强粉，煮）", 107.0),
        "瘦猪肉": ("猪肉（瘦）", 153.0),
        "梨": ("梨（代表值）", 51.0),
        "植物油": ("花生油", 899.0),
    }
    for q, (exp_name, exp_kcal) in checks.items():
        r = fooddb.find_food(q)
        assert r is not None and r["name"] == exp_name, (q, r)
        assert r["energy_kcal"] == exp_kcal, (q, r["energy_kcal"])


def test_p12_fuzzy_mismatches_fixed():
    """P1-2：模糊误配修复——大米→稻米 K=112（不再 淀粉 K=2 56 倍低估）、
    粳米→标一（不再 粳米粥）、猪蹄筋(泡发)→猪蹄筋、猪瘦肉/牛腩 命中。"""
    assert fooddb.find_food("大米")["potassium_mg"] == 112.0
    assert fooddb.find_food("白米")["name"] == "稻米（代表值）"
    assert fooddb.find_food("粳米")["name"] == "粳米（标一）"
    assert fooddb.find_food("猪蹄筋(泡发)")["name"] == "猪蹄筋"
    assert fooddb.find_food("猪瘦肉")["name"] == "猪肉（瘦）"
    assert fooddb.find_food("牛腩")["name"] == "牛肉（腹部肉）"


def test_p13_chinese_numerals():
    """P1-3：中文数词组合——二分之一碗=0.5 碗、二十个=20、两个半=2.5。"""
    row = {"name": "稻米（代表值）", "unit_name": "碗", "unit_grams": 50.0,
           "unit_desc": "", "aliases": []}
    assert measures.parse_portion("二分之一碗", row)["grams"] == 25.0   # 0.5×50
    assert measures.parse_portion("二十个", row)["grams"] == 2000.0     # 20×100
    assert measures.parse_portion("两个半", row)["grams"] == 250.0      # 2.5×100
    assert measures.parse_portion("十二碗", row)["grams"] == 600.0


def test_p14_score_range_0_100():
    """P1-4：record_pew_risk score 契约域 0-100（500/-50/101/bool 拒绝）。"""
    from datetime import date

    today = date.today().isoformat()
    with as_caller("doctor_assistant"):
        for bad in (500.0, -50.0, 101.0, True):
            r = core.record_pew_risk("P0001", today, bad, "low")
            assert r["ok"] is False and r["error"] == "INVALID_INPUT", (bad, r)
        assert core.record_pew_risk("P0001", today, 30.0, "low")["ok"] is True


def test_p15_floor_protein_guard():
    """P1-5：assess_pew_risk floor_protein_g 校验（0/-5/nan/inf/bool 拒绝，
    此前 floor=0 → PEW 假阴性、inf → 恒 medium）。"""

    with as_caller("doctor_assistant"):
        for bad in (0.0, -5.0, float("nan"), float("inf"), True):
            r = core.assess_pew_risk(avg_protein_g=40.0, avg_energy_kcal=1000.0,
                                     target_protein_g=50.0, target_energy_kcal=1200.0,
                                     floor_protein_g=bad)
            assert r["ok"] is False and r["error"] == "INVALID_INPUT", (bad, r)


def test_p16_aggregate_skips_non_dict():
    """P1-6：_aggregate 对非 dict 条目 fail-soft 跳过（此前 None 条目 e.get →
    AttributeError 500，日记永久不可读）。"""
    agg = core._aggregate([{"date": "2026-08-18", "energy_kcal": 100.0},
                           None,
                           "corrupt",
                           {"date": "2026-08-18", "energy_kcal": 200.0}])
    assert agg["day_count"] == 1 and agg["entry_count"] == 2, agg
    # BUG-61 语义：均值按**天数**（同日两餐合计 300 / 1 天 = 300）
    assert agg["diet_diary_3d"]["avg_energy_kcal"] == 300.0, agg


def test_p17_pew_history_legacy_missing_level():
    """P1-7：legacy PEW 点缺 level 不再 KeyError 500——有效点参与趋势，不足 2 个
    有效点 → no_data（fail-closed）。"""
    # 直接构造：写入含缺 level 点的库（绕过入口校验）
    from unittest import mock

    with mock.patch.object(core, "_load_patient_pew_store",
                           return_value={"P0001": [
                               {"date": "2026-08-01", "score": 90.0},   # 缺 level
                               {"date": "2026-08-02", "score": 95.0, "level": "high"},
                               {"date": "2026-08-03", "score": 95.0, "level": "high"},
                           ]}):
        r = core.get_pew_history("P0001")
        assert r["ok"] is True, r  # 不再 KeyError 500
        assert r["data"]["trend"] == "stable", r["data"]


def test_p21_meal_enum_validated():
    """P2-1：meal 白名单（早餐/午餐/晚餐/加餐）——夜宵/第25餐/123/dict 拒绝。"""
    from datetime import date

    today = date.today().isoformat()
    base = {"date": today, "food": "苹果", "grams": 100}
    with as_caller("doctor_assistant"):
        for bad_meal in ("夜宵", "第25餐", 123, {"x": 1}, ["a"]):
            r = core.upsert_food_diary("P0001", [{**base, "meal": bad_meal}],
                                       write_mode=False)
            assert r["ok"] is False and r["error"] == "INVALID_INPUT", (bad_meal, r)
        assert core.upsert_food_diary("P0001", [{**base, "meal": "晚餐"}],
                                      write_mode=False)["ok"] is True


def test_p22_future_date_excluded_from_sum():
    """P2-2：sum_diet_intake 未来日期（2099-01-01）排除（写路径拒、读路径此前吃）。"""
    r = diary.sum_diet_intake([
        {"food": "苹果", "grams": 100, "date": "2099-01-01"},
        {"food": "苹果", "grams": 100, "date": "2026-08-18"},
    ])
    assert r["ok"] is True, r
    assert r["data"]["days"] == 1, r["data"]  # 未来日不参与分桶
    assert all(d["date"] != "2099-01-01" for d in r["data"]["per_day"]), r["data"]


def test_p24_bracket_grams():
    """P2-4：括号克重——"1碗(200g)" 按 200g 权威值；"30g(干)" 剥离规格后按 30g。"""
    row = {"name": "米饭", "unit_name": "碗", "unit_grams": 150.0,
           "unit_desc": "", "aliases": []}
    assert measures.parse_portion("1碗(200g)", row)["grams"] == 200.0
    assert measures.parse_portion("30g(干)", row)["grams"] == 30.0


def test_p25_cooking_combination():
    """P2-5："焯水+浸泡" 组合系数相乘（不再回落 raw 1.0）。"""
    out = fooddb.scale_nutrients(
        {"energy_kcal": 100.0, "protein_g": 10.0, "fat_g": 1.0, "carb_g": 20.0,
         "potassium_mg": 500.0, "phosphorus_mg": 100.0, "sodium_mg": 50.0,
         "calcium_mg": 30.0},
        100, "焯水+浸泡")
    assert out["cooking"] != "raw", out
    assert out["potassium_mg"] < 500.0, out  # 组合降钾系数 < 1


def test_p27_json_backend_main_ok():
    """P2-7：server.main json 后端不再崩（LocalJson 无 _get_client 不再误报 OTS）；
    未知后端 SystemExit(1)。"""
    from unittest import mock

    from CKDNutri_nutrition_mcp import server

    old = os.environ.get("A207_STORAGE_BACKEND")
    try:
        os.environ["A207_STORAGE_BACKEND"] = "json"
        with mock.patch.object(server.mcp, "run", lambda: None):
            server.main()  # 不应 SystemExit（修复前 LocalJson._get_client AttributeError）
        os.environ["A207_STORAGE_BACKEND"] = "jsno"
        try:
            server.main()
        except SystemExit as exc:
            assert exc.code == 1, exc
        else:
            raise AssertionError("未知后端应 SystemExit(1)")
    finally:
        if old is None:
            os.environ.pop("A207_STORAGE_BACKEND", None)
        else:
            os.environ["A207_STORAGE_BACKEND"] = old


def test_p01_hd_not_deduct_pd_glucose():
    """P0-1（四审）：HD 患儿不扣 PD 葡萄糖（此前 dm != "none" 令 HD 也扣）；
    非 peritoneal 传 pd_glucose 拒绝；peritoneal 正常扣减。"""
    from CKDNutri_nutrition_mcp import core

    # HD + pd_glucose → 拒绝（严禁带 Warning 的错误扣减）
    try:
        core.calc_prnt_targets(age_years=8, sex="M", weight_kg=25.0, height_cm=125.0,
                               ckd_stage=4, dialysis_mode="hemodialysis",
                               pd_glucose_kcal_per_day=300.0)
    except ValueError:
        pass
    else:
        raise AssertionError("HD 患儿传 pd_glucose 未被拒绝")
    # none + pd_glucose → 拒绝
    try:
        core.calc_prnt_targets(age_years=8, sex="M", weight_kg=25.0, height_cm=125.0,
                               ckd_stage=4, dialysis_mode="none",
                               pd_glucose_kcal_per_day=300.0)
    except ValueError:
        pass
    else:
        raise AssertionError("未透析传 pd_glucose 未被拒绝")
    # peritoneal → 正常扣减（能量目标低于未扣减版）
    base = core.calc_prnt_targets(age_years=8, sex="M", weight_kg=25.0, height_cm=125.0,
                                  ckd_stage=4, dialysis_mode="peritoneal")
    ded = core.calc_prnt_targets(age_years=8, sex="M", weight_kg=25.0, height_cm=125.0,
                                 ckd_stage=4, dialysis_mode="peritoneal",
                                 pd_glucose_kcal_per_day=300.0)
    assert base["ok"] is True and ded["ok"] is True
    assert ded["data"]["energy"]["target_kcal_per_day"] < base["data"]["energy"]["target_kcal_per_day"], \
        (base["data"]["energy"], ded["data"]["energy"])


def test_p02_prnt_age_gt18_rejected():
    """P0-2（四审）：age_years>18 拒绝生成处方（此前仅 Warning 仍按 15-17 段计算）。"""
    from CKDNutri_nutrition_mcp import core

    try:
        core.calc_prnt_targets(age_years=19, sex="M", weight_kg=60.0, height_cm=175.0)
    except ValueError:
        pass
    else:
        raise AssertionError("age_years=19 未被拒绝")
    assert core.calc_prnt_targets(age_years=18, sex="M", weight_kg=55.0,
                                  height_cm=170.0)["ok"] is True


def test_p03_albumin_strict_validation():
    """P0-3（四审）：assess_pew_risk albumin_g_L 严格校验（NaN/Inf/负/非数值/bool
    拒绝，不再静默转 None 漏扣 20 分）。"""

    from CKDNutri_nutrition_mcp import core

    with as_caller("doctor_assistant"):
        for bad in (float("nan"), float("inf"), -5.0, 0.0, "abc", True):
            r = core.assess_pew_risk(avg_protein_g=40.0, avg_energy_kcal=1000.0,
                                     target_protein_g=50.0, target_energy_kcal=1200.0,
                                     albumin_g_L=bad)
            assert r["ok"] is False and r["error"] == "INVALID_INPUT", (bad, r)
        # None=未提供 合法；正常值放行
        assert core.assess_pew_risk(avg_protein_g=40.0, avg_energy_kcal=1000.0,
                                    target_protein_g=50.0,
                                    target_energy_kcal=1200.0)["ok"] is True
        assert core.assess_pew_risk(avg_protein_g=40.0, avg_energy_kcal=1000.0,
                                    target_protein_g=50.0, target_energy_kcal=1200.0,
                                    albumin_g_L=25.0)["ok"] is True


def test_p04_pharma_arb_split():
    """P0-4（四审）：ARB/沙坦与 ACEI 拆分——氯沙坦/缬沙坦 不再解析到 依那普利。"""
    from CKDNutri_nutrition_mcp import pharma

    assert pharma._resolve_drug("氯沙坦")[0] == "氯沙坦"
    assert pharma._resolve_drug("缬沙坦")[0] == "缬沙坦"
    assert pharma._resolve_drug("arb")[0] == "氯沙坦"   # 类名归 ARB（氯沙坦）
    assert pharma._resolve_drug("沙坦")[0] in ("氯沙坦", "缬沙坦")
    assert pharma._resolve_drug("依那普利")[0] == "依那普利"
    assert pharma._resolve_drug("培哚普利")[0] == "培哚普利"
    # 依那普利 不再含 ARB 别名
    info = pharma.DRUGS["依那普利"]
    assert "氯沙坦" not in info["aliases"] and "沙坦" not in info["aliases"], info


def test_p16_nan_grams_blocked():
    """P1-6（四审）：scale_nutrients / to_household 阻断 NaN/Inf/bool 克重。"""

    from CKDNutri_nutrition_mcp import fooddb, measures

    row = {"name": "苹果", "unit_name": "个", "unit_grams": 100.0, "aliases": [],
           "energy_kcal": 50.0, "protein_g": 0.5, "fat_g": 0.2, "carb_g": 12.0,
           "potassium_mg": 100.0, "phosphorus_mg": 10.0, "sodium_mg": 1.0,
           "calcium_mg": 5.0}
    for bad in (float("nan"), float("inf"), True):
        try:
            fooddb.scale_nutrients(row, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"scale_nutrients grams={bad!r} 未拒绝")
        try:
            measures.to_household(row, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"to_household grams={bad!r} 未拒绝")
    assert fooddb.scale_nutrients(row, 100.0)["potassium_mg"] == 100.0


def test_p17_hundred_and_catty():
    """P1-7（四审）：中文数词"百"/"斤半"——一百克=100、两百克=200、一斤半=750。"""
    from CKDNutri_nutrition_mcp import measures

    row = {"name": "米", "unit_name": "碗", "unit_grams": 100.0, "unit_desc": "", "aliases": []}
    assert measures.parse_portion("一百克", row)["grams"] == 100.0
    assert measures.parse_portion("两百克", row)["grams"] == 200.0
    assert measures.parse_portion("一斤半", row)["grams"] == 750.0
    assert measures.parse_portion("十一碗", row)["grams"] == 1100.0


def test_p111_meal_plan_nutrients_guard():
    """P1-11（四审）：get_meal_plan_nutrients 输入防护（plan 非 dict/缺 days/非数值
    拒绝，不再 500）。"""
    from CKDNutri_nutrition_mcp import mealplan

    good = {"days": [{"day_totals": {"energy_kcal": 1200.0, "protein_g": 40.0,
                                     "potassium_mg": 1000.0, "phosphorus_mg": 300.0,
                                     "sodium_mg": 1500.0}}]}
    for bad in (None, "plan", {}, {"days": []}, {"days": [None]},
                {"days": [{"day_totals": {"energy_kcal": float("nan")}}]}):
        try:
            mealplan.get_meal_plan_nutrients(bad)
        except ValueError:
            continue
        raise AssertionError(f"plan={bad!r} 未拒绝")
    assert mealplan.get_meal_plan_nutrients(good)["energy_kcal"] == 1200.0
