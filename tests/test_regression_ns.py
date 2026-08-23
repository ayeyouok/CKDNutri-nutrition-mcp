"""N-S2/N-S3/N-S4/N-S5/N-S6/N-B8 回归测试（2026-08-14 修复后固化）。

python 直接运行 + pytest 双模式（对齐 P1 test_tools 风格）。
"""
import os

os.environ.setdefault("A207_ENV", "test")  # N-SEC-1（2026-08-14）：测试进程显式声明测试环境（守卫 fail-closed 默认拒绝）
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏（2026-08-15）：测试进程显式确认 json 后端为开发模式
import math
import sys
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
    for q, _want in expect.items():
        r = check_drug_nutrient_interaction(q)
        d = r.get("data", {})
        # 通过 drug_class 区分（钙型 vs 钠型；前药 vs 活性形式）
        cls = d.get("drug_class", "")
        if "聚苯乙烯磺酸" in q:
            assert ("钙型" in cls) == ("钙" in q), (q, cls)
        if q == "阿法骨化醇":
            assert "前药" in cls or "羟化" in cls, (q, cls)
        if q == "环孢素":
            assert "环孢素" in cls, (q, cls)


def test_med1_schofield_authoritative():
    """MED-1（2026-08-15）：Schofield 各段与权威 kcal 版换算一致（误差<1%）。

    权威：Schofield 1985（W kg、H cm，kcal/day）——
    男 0-3: 0.167W+15.174H-617.6；3-10: 19.59W+1.303H+414.9；
    10-18: 16.25W+1.372H+515.5；女 0-3: 16.252W+10.232H-413.5；
    3-10: 16.969W+1.618H+371.2；10-18: 8.365W+4.65H+200。
    代码以 MJ/米 存储（÷239.0064），逐段换算核对（10-18 男此前 (M,18) 段写错，
    BMR 低估 10-25%，已修正为 (0.068, 0.574, 2.157)）。
    """
    from CKDNutri_nutrition_mcp.core import schofield_bmr_kcal

    cases = [
        # (sex, age, W, H, kcal_权威)
        ("M", 1, 10, 80, 0.167 * 10 + 15.174 * 80 - 617.6),      # 598.0
        ("M", 5, 18, 110, 19.59 * 18 + 1.303 * 110 + 414.9),      # 910.9
        ("M", 10, 35, 140, 16.25 * 35 + 1.372 * 140 + 515.5),     # 1276.3
        ("M", 15, 50, 165, 16.25 * 50 + 1.372 * 165 + 515.5),     # 1554.4
        ("F", 1, 9, 75, 16.252 * 9 + 10.232 * 75 - 413.5),        # 508.9
        ("F", 5, 17, 108, 16.969 * 17 + 1.618 * 108 + 371.2),     # 843.7
        ("F", 12, 40, 150, 8.365 * 40 + 4.65 * 150 + 200.0),      # 1232.1
    ]
    for sex, age, w, h, expect in cases:
        got = schofield_bmr_kcal(sex, age, w, h)
        assert got is not None, (sex, age)
        rel = abs(got - expect) / expect
        assert rel < 0.01, f"Schofield {sex} {age}岁 {w}kg/{h}cm：got={got} 权威={expect}（偏差 {rel:.1%}）"


def test_low3_pew_score_finite():
    """LOW-3（2026-08-15）：record_pew_risk 的 score 有限性校验（NaN/Inf/非数值拒绝）。"""

    from a207_policy import as_caller

    from CKDNutri_nutrition_mcp import core

    with as_caller("doctor_assistant"):
        for bad in (float("nan"), float("inf"), float("-inf"), "abc", None):
            r = core.record_pew_risk("P0001", "2026-08-15", bad, "low")
            assert r["ok"] is False and r["error"] == "INVALID_INPUT", (bad, r)
        # 合法值放行
        r = core.record_pew_risk("P0001", "2026-08-15", 12.5, "low")
        assert r["ok"] is True, r
        assert math.isfinite(r["data"]["points"][0]["score"]), r


def test_med_gr1_piecewise_z():
    """MED-GR-1（2026-08-15）：国标附录 B 非均匀 SD——7 界值分段插值。

    旧实现 z=(x-m)/s 用单一 SD 外推，在 ±2SD/±3SD 系统性偏差可致营养等级错判：
    - 81 月男童 BMI 权威界值 12.1/-2=13.0/-1=14.1/中=15.4/+1=17.2/+2=19.7/+3=23.3，
      +2SD→+3SD 间距 3.6 vs −3SD→−2SD 间距 0.9（差 4 倍）；旧算法 BMI=19.7 会算
      z=(19.7-15.4)/1.55≈2.77 判"肥胖"，而 19.7 恰是 +2SD 应判"超重"。
    - 6 月男童体重 6.1/-2=6.8/-1=7.6/中=8.4/+1=9.4/+2=10.5/+3=11.7。
    修复后：界值点精确对应整数 Z，区间内线性插值。
    """
    from CKDNutri_nutrition_mcp import core

    def _near(a, b, tol=0.01):
        assert abs(a - b) <= tol, f"{a} vs {b}"

    # 81 月男童 BMI：7 界值点 → z 精确整数
    for bmi_val, exp_z in ((12.1, -3), (13.0, -2), (14.1, -1), (15.4, 0),
                           (17.2, 1), (19.7, 2), (23.3, 3)):
        r = core.calc_growth_zscore(age_years=81 / 12, sex="M", bmi=bmi_val)
        _near(r["data"]["baz"]["z"], exp_z, tol=0.005)

    # 关键错判场景：BMI=19.0（<+2SD=19.7 → 超重，旧算法 z≈2.32 误判肥胖）
    r = core.calc_growth_zscore(age_years=81 / 12, sex="M", bmi=19.0)
    assert r["data"]["baz"]["nutrition"] == "超重", r["data"]["baz"]
    # BMI=23.0（<+3SD=23.3 → 肥胖，旧算法 z≈4.9 误判重度肥胖）
    r = core.calc_growth_zscore(age_years=81 / 12, sex="M", bmi=23.0)
    assert r["data"]["baz"]["nutrition"] == "肥胖", r["data"]["baz"]

    # 6 月男童体重：-3SD=6.1 → z=-3.0 恰在界值 → "低体重"（国标表3：<−3SD 才重度）
    r = core.calc_growth_zscore(age_years=0.5, sex="M", weight_kg=6.1)
    _near(r["data"]["waz"]["z"], -3.0, tol=0.005)
    assert r["data"]["waz"]["nutrition"] == "低体重", r["data"]["waz"]
    # 6.0（<-3SD=6.1）→ 重度低体重（旧算法 z=(6.0-8.4)/0.9=-2.67 误判低体重）
    r = core.calc_growth_zscore(age_years=0.5, sex="M", weight_kg=6.0)
    assert r["data"]["waz"]["nutrition"] == "重度低体重", r["data"]["waz"]

    # 区间内线性：81 月 BMI 16.3（15.4↔17.2 中点）→ z≈0.5
    r = core.calc_growth_zscore(age_years=81 / 12, sex="M", bmi=16.3)
    _near(r["data"]["baz"]["z"], 0.5, tol=0.005)

    # 7-18 身高：WS/T 612 表 A.1 男 7 岁五界值 → z 精确整数（等距 s=5.99 构造）
    for h_cm, exp_z in ((113.51, -2), (119.49, -1), (125.48, 0),
                        (131.47, 1), (137.46, 2)):
        r = core.calc_growth_zscore(age_years=7, sex="M", height_cm=h_cm)
        _near(r["data"]["haz"]["z"], exp_z, tol=0.01)

    print("MED-GR-1 PIECEWISE Z OK")


def test_med1_missing_nutrients_flag():
    """MED-1（2026-08-15）：缺失营养素不得静默当 0——打 missing_nutrients 标记，
    food_warnings 与 sum_diet_intake 显式提示（CKD 患儿钾/磷低估风险）。"""
    from unittest import mock

    from CKDNutri_nutrition_mcp import diary, fooddb
    from CKDNutri_nutrition_mcp.foods import food_warnings

    rows = fooddb.load_foods()
    assert rows, "食物表为空"
    # H1（2026-08-15）：四电解质全 0 行（68 行）现被正确标记缺失——断言放宽为
    # "标记的行确实四电解质全 0/空"，而非"全部无缺失"
    # P0-1（2026-08-18）：缺失判定升级——单列 K/P 字面 0 也标记缺失（荔枝干 K=0 真实
    # ≈900、籼稻谷 P=0 真实≈110），断言改为"**每个被标记的键值确实为 0**"（旧断言
    # "被标记 ⇒ 四电解质全 0"编码的是 H1 旧语义，与 P0-1 单列缺失扩展矛盾）。
    missing_rows = [r for r in rows if r.get("missing_nutrients")]
    assert missing_rows, "应存在被标记的缺失行（H1 四电解质全 0 检测）"
    for r in missing_rows:
        for k in r["missing_nutrients"]:
            assert r[k] in (0, 0.0), (r["name"], k, r[k])

    # 构造缺失行验证提示路径
    fake = dict(rows[0])
    fake["missing_nutrients"] = ["potassium_mg", "sodium_mg"]
    fake["name"] = "测试食物X"
    w = food_warnings(fake)
    assert any("数据缺失" in x and "钾" in x for x in w), w

    with mock.patch.object(diary, "find_food", return_value=fake):
        res = diary.sum_diet_intake([{"food": "测试食物X", "grams": 100}])
    assert res["ok"] is True, res
    warns = res["data"].get("warnings") or []
    assert any("缺失" in x and "钾" in x for x in warns), warns


def test_low5_negative_grams_rejected():
    """LOW-5（2026-08-15）：scale_nutrients 负克重拒绝（此前 max(grams,0) 静默归 0
    产出假安全结果）；0 克合法。"""
    from CKDNutri_nutrition_mcp import fooddb

    row = fooddb.load_foods()[0]
    try:
        fooddb.scale_nutrients(row, -50)
    except ValueError:
        pass
    else:
        raise AssertionError("负克重应抛 ValueError（INVALID_INPUT 语义）")
    assert fooddb.scale_nutrients(row, 0)["grams"] == 0.0
    assert fooddb.scale_nutrients(row, 100)["potassium_mg"] >= 0





def test_s3_food_diary_content_idempotent():
    """S-3（2026-08-15）：upsert_food_diary (date+meal+food) **内容幂等**。

    此前无条件 `existing + stamped` 追加（每次新 uuid4 entry_id），家长弱网重试
    同一顿饭 → 两行，day_count/均值失真。现按内容键合并：同 date+meal+food
    已存在 → 本次值替换该条目（保留原 entry_id），否则新增。
    """
    from CKDNutri_nutrition_mcp import core

    pid = "P0999"  # 专用测试患者，避免污染既有数据
    meal = {"date": "2026-08-01", "meal": "午餐", "food": "米饭",
            "energy_kcal": 200, "protein_g": 4.0, "potassium_mg": 30,
            "phosphorus_mg": 20, "sodium_mg": 1}
    r1 = core.upsert_food_diary(pid, entries=[dict(meal)])
    assert r1["ok"] is True, r1
    # 弱网重试：同 (date+meal+food)，值微调（能量 200→250）
    retry = dict(meal)
    retry["energy_kcal"] = 250
    r2 = core.upsert_food_diary(pid, entries=[retry])
    assert r2["ok"] is True, r2
    # 存储仅 1 条、值取最后一次（幂等更新非追加）
    store = core._load_patient_store(pid)
    entries = store.get("entries", [])
    same = [e for e in entries
            if e.get("date") == "2026-08-01" and e.get("meal") == "午餐"
            and e.get("food") == "米饭"]
    assert len(same) == 1, f"同餐重试应幂等（只 1 条），实际 {len(same)} 条: {same}"
    assert same[0]["energy_kcal"] == 250, same
    # 不同日期/餐次 → 正常新增
    r3 = core.upsert_food_diary(pid, entries=[dict(meal, date="2026-08-02")])
    assert r3["ok"] is True and len(core._load_patient_store(pid).get("entries", [])) == 2


def test_m3_ws586_bmi_overweight():
    """M-3（2026-08-15）：≥7 岁无 BAZ 时按 WS/T 586-2018 年龄×性别别 BMI 超重界值
    判超重——此前 BMI>24 统一粗判漏判（7 岁男超重界值 17.0、12 岁男 20.7）。"""
    from CKDNutri_nutrition_mcp import core

    # 7 岁男 BMI 18（≥17.0 超重，旧 BMI>24 漏判）→ overweight + WS/T 586 提示
    r = core.calc_growth_zscore(age_years=7, sex="M", height_cm=125, weight_kg=28.1)
    assert r["data"]["growth_status_suggestion"] == "overweight", r["data"]["growth_status_suggestion"]
    assert any("WS/T 586" in w for w in r["data"]["warnings"])
    # 12 岁男 BMI 21（≥20.7 超重）
    r = core.calc_growth_zscore(age_years=12, sex="M", height_cm=150, weight_kg=47.25)
    assert r["data"]["growth_status_suggestion"] == "overweight"
    # 7 岁男 BMI 16（<17.0）→ normal
    r = core.calc_growth_zscore(age_years=7, sex="M", height_cm=125, weight_kg=25.0)
    assert r["data"]["growth_status_suggestion"] == "normal"

def test_m8_whz_weight_for_length():
    """M-8（2026-08-15）：身长/身高别体重 WHZ——0-2 岁身长别体重（表 B.5/B.6）、
    2-7 岁身高别体重（表 B.7/B.8）。此前 <2 岁关键指标缺失。"""
    from CKDNutri_nutrition_mcp import core

    # 1 岁男 身长 75 体重 9.9（中位）→ z≈0
    r = core.calc_growth_zscore(age_years=1, sex="M", height_cm=75, weight_kg=9.9)
    assert "whz" in r["data"] and abs(r["data"]["whz"]["z"]) < 0.5, r["data"].get("whz")
    # 3 岁女 → 身高别体重表
    r = core.calc_growth_zscore(age_years=3, sex="F", height_cm=95, weight_kg=15.5)
    assert "身高别" in r["data"]["whz"]["basis"]
    # 域外（2 岁男 74cm <75）→ 跳过 + 告警
    r = core.calc_growth_zscore(age_years=2, sex="M", height_cm=74, weight_kg=11)
    assert "whz" not in r["data"] and any("WHZ" in w for w in r["data"]["warnings"])

def test_m9_haz_82_83_skip():
    """M-9（2026-08-15）：82-83 月龄 HAZ 显式跳过（与 WAZ/BAZ 一致）——此前 HAZ
    在 81↔84 月插值（跨 WS/T 423/612 衔接），不对称。"""
    from CKDNutri_nutrition_mcp import core

    r = core.calc_growth_zscore(age_years=82 / 12, sex="M", height_cm=120)
    assert "haz" not in r["data"], r["data"].get("haz")
    assert any("82-83" in w for w in r["data"]["warnings"])
    # 边界：81 月（6岁9月）仍正常产出、84 月（7岁整）正常
    r = core.calc_growth_zscore(age_years=81 / 12, sex="M", height_cm=120)
    assert "haz" in r["data"]
    r = core.calc_growth_zscore(age_years=7, sex="M", height_cm=120)
    assert "haz" in r["data"]

def test_f4_diary_write_guards():
    """F-4（2026-08-15）：upsert_food_diary 未来日期/负营养值/非 dict 元素拒绝。"""
    import datetime

    from CKDNutri_nutrition_mcp import core

    # 守卫以北京业务日（UTC+8）判定"今天"，测试须同口径构造未来日，否则在 UTC
    # 时区的 CI 上 datetime.date.today() 落后 8 小时，构造的"明天"恰好等于北京
    # 今日 → 守卫不触发 → 误判（2026-08-24 CI 失败根因）。
    _cn_tz = datetime.timezone(datetime.timedelta(hours=8))
    tomorrow = (datetime.datetime.now(_cn_tz).date() + datetime.timedelta(days=1)).isoformat()
    base = {"meal": "午餐", "food": "米饭", "energy_kcal": 100, "protein_g": 2,
            "potassium_mg": 10, "phosphorus_mg": 5, "sodium_mg": 1}
    r = core.upsert_food_diary("P0001", entries=[dict(base, date=tomorrow)])
    assert r["ok"] is False and "未来" in r["detail"], r
    r = core.upsert_food_diary("P0001", entries=[dict(base, date="2026-08-01", energy_kcal=-50)])
    assert r["ok"] is False and "不能为负" in r["detail"], r
    r = core.upsert_food_diary("P0001", entries=["米饭"])
    assert r["ok"] is False and "必须为对象" in r["detail"], r


def test_m2_intake_threshold_shared():
    """M2（2026-08-16，第七轮审查）：能量达成率分级 core/diary 共用 _intake_pct_status
    单一阈值（此前 core <80=deficit/>120=excess vs diary 90-110=达标分裂，80-90 与
    110-120 区间同一份日记结论不同）。"""
    from CKDNutri_nutrition_mcp import core, diary

    assert core._intake_pct_status(79) == "deficit"
    assert core._intake_pct_status(85) == "low"
    assert core._intake_pct_status(95) == "ok"
    assert core._intake_pct_status(110) == "ok"
    assert core._intake_pct_status(115) == "high"
    assert core._intake_pct_status(131) == "excess"
    # diary 与 core 同口径
    avg = {"energy_kcal": 850, "protein_g": 20, "potassium_mg": 1000,
           "phosphorus_mg": 500, "sodium_mg": 1000}
    tgt = {"energy_kcal": 1000, "protein_g": 22, "potassium_mg": 2000,
           "phosphorus_mg": 1000, "sodium_mg": 1500}
    r = diary._achievement(avg, tgt)
    e = next(i for i in r["items"] if i["field"] == "energy_kcal")
    assert e["verdict"] == "不足", e["verdict"]  # 85% → 不足（与 core low 同源）



if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"NS2-NS6/NB8 REGRESSION OK（{len(fns)} 个用例）")












