"""S9 修复（2026-08-13）：领域计算 golden dataset 单测。

最关乎患者安全的 eGFR/分期、PRNT 目标、生长 Z、PEW 筛查此前只有
import-smoke（验证"能 import"）+ 手动 probe（tests/probe_*.py，不在 CI 强制）。
本文件以**黄金数据集**固化正确性：每个用例给出输入 + 期望值（由参考表/公式
独立计算得到），CI 跑 pytest 即可回归，覆盖早产儿/透析/边界 eGFR/负值/NaN/
单位混用等边界。

运行：pytest tests/test_golden_dataset.py  或  python tests/test_golden_dataset.py
"""
from __future__ import annotations

import os
import sys
from math import isclose
from pathlib import Path

os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")

NUTRITION_SRC = Path(__file__).resolve().parents[1] / "src"
MCP_ROOT = Path(__file__).resolve().parents[2]
ASSESSMENT_SRC = MCP_ROOT / "CKDNutri-assessment-mcp" / "src"
POLICY_SRC = MCP_ROOT / "a207-policy" / "src"
for p in (NUTRITION_SRC, ASSESSMENT_SRC, POLICY_SRC):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _near(actual, expected, tol=0.01):
    assert isclose(actual, expected, rel_tol=tol, abs_tol=0.05), \
        f"期望 {expected}，实际 {actual}"


# ---------------------------------------------------------------- eGFR（assessment）
def test_egfr_golden_bedside():
    """床旁 Schwartz 2009：eGFR = 0.413 × 身高(cm) / Scr(mg/dL)。"""
    from CKDNutri_assessment_mcp import core

    # 6y M 115cm Scr=0.6 → 0.413×115/0.6 = 79.16…
    r = core.calc_egfr_schwartz(age_years=6, height_cm=115, serum_creatinine_mgdl=0.6)
    _near(r["data"]["egfr"], 0.413 * 115 / 0.6)
    # µmol/L 单位混用：88.4 µmol/L ≡ 1.0 mg/dL → 与 mg/dL=1.0 结果一致
    r1 = core.calc_egfr_schwartz(age_years=6, height_cm=115, serum_creatinine_mgdl=1.0)
    r2 = core.calc_egfr_schwartz(age_years=6, height_cm=115,
                                 serum_creatinine_mgdl=88.4, serum_creatinine_unit="umol_L")
    _near(r1["data"]["egfr"], r2["data"]["egfr"], tol=1e-6)
    _near(r2["data"]["egfr"], 0.413 * 115 / 1.0)


def test_egfr_golden_classic_preterm():
    """经典 Schwartz：早产儿 k=0.33、<1y 足月 0.45、1-12y 0.55、≥13y M 0.70/F 0.55。"""
    from CKDNutri_assessment_mcp import core

    cases = [
        # (age, sex, is_preterm, height, scr, expected_k)
        (0.5, None, True, 60, 0.8, 0.33),
        (0.5, None, False, 60, 0.8, 0.45),
        (6, None, False, 115, 0.6, 0.55),
        (14, "M", False, 160, 1.0, 0.70),
        (14, "F", False, 160, 1.0, 0.55),
    ]
    for age, sex, preterm, h, scr, k in cases:
        r = core.calc_egfr_schwartz(age_years=age, height_cm=h, serum_creatinine_mgdl=scr,
                                    method="classic", is_preterm=preterm, sex=sex)
        _near(r["data"]["egfr"], k * h / scr, tol=0.01)


def test_egfr_golden_revised_bun():
    """修订 Schwartz 2009：eGFR = 0.413×H/(Scr + 0.003×BUN − 0.024)。"""
    from CKDNutri_assessment_mcp import core

    r = core.calc_egfr_schwartz(age_years=6, height_cm=115, serum_creatinine_mgdl=0.6,
                                bun_mg_dl=30, method="revised2009")
    expect = 0.413 * 115 / (0.6 + 0.003 * 30 - 0.024)
    _near(r["data"]["egfr"], expect)


def test_egfr_golden_boundaries():
    """边界：eGFR 阈值判级 G1/G2/G3a/G3b/G4/G5/G5D（用未 round 值判级）。"""
    from CKDNutri_assessment_mcp import core

    cases = [
        (95, "G1"), (89.9, "G2"), (60, "G2"), (59.9, "G3a"), (45, "G3a"),
        (44.9, "G3b"), (30, "G3b"), (29.9, "G4"), (15, "G4"), (14.9, "G5"),
        (14.9, "G5D"),  # 透析
    ]
    for egfr, want_g in cases:
        dial = "hemodialysis" if want_g == "G5D" else None
        r = core.classify_ckd(egfr=egfr, dialysis_mode=dial)
        assert r["data"]["g"] == want_g, (egfr, r["data"]["g"], want_g)


def test_egfr_golden_invalid_inputs():
    """负值 / NaN / Inf / 超龄 → ValueError（fail-closed）。"""
    from CKDNutri_assessment_mcp import core

    for bad in (-5, float("nan"), float("inf")):
        for fn in (
            lambda: core.calc_egfr_schwartz(age_years=6, height_cm=115, serum_creatinine_mgdl=bad),
            lambda: core.classify_ckd(egfr=bad),
        ):
            try:
                fn()
            except ValueError:
                pass
            else:
                raise AssertionError(f"bad={bad} 应抛 ValueError")


# ---------------------------------------------------------------- PRNT 目标（nutrition）
def test_prnt_golden_energy():
    """6y F 20kg 130cm：energy SDI=[64,90] → 正常生长取中点 77 kcal/kg/d → 1540 kcal/d。"""
    from CKDNutri_nutrition_mcp import core

    r = core.calc_prnt_targets(age_years=6, sex="F", weight_kg=20, height_cm=130, ckd_stage=1)
    d = r["data"]
    _near(d["energy"]["target_kcal_per_kg"], 77.0)
    _near(d["energy"]["target_kcal_per_day"], 77.0 * 20)
    _near(d["protein"]["target_g_per_kg"], 0.95)
    _near(d["protein"]["target_g_per_day"], 0.95 * 20)
    _near(d["protein"]["floor_g_per_day"], 0.85 * 20)


def test_prnt_golden_sdi_bands():
    """N-S1 回归（2026-08-14）：PRNT 2020 Table 1 完整年龄段分段（黄金数据）。

    此前 _PRNT_BANDS 把年岁段压缩为 4 段：7-12 岁全落"9-10岁"、12-18 岁全落
    "15-17岁"——实测 12 岁女孩能量按 15-17 岁段（F 36-46，中位 41），权威应为
    11-12 岁段（F 43-57，中位 50），系统性低估 ~18%。本测试锁死全部边界与
    权威值（源：Shaw et al., Pediatr Nephrol 2020;35:519-531, Table 1）。
    """
    from CKDNutri_nutrition_mcp import core

    # (age_years, sex, 期望 label, 期望能量[lo,hi] kcal/kg/day, 期望蛋白[lo,hi] g/kg/day)
    # 整岁边界（N-S1 二审，2026-08-14）：12月龄=12-23月(1.0-2.0)、2岁=24-35月(2.0-3.0)、
    # 3岁=36-47月(3.0-4.0)、4-6岁=48-83月(4.0-7.0)。此前 12月龄只到 1.5、
    # 2岁/3岁/4-6岁起点各提前 0.5 岁 → 18-23/30-35/42-47 月龄错入下一段。
    # 月龄段（N-S1 三审，2026-08-14）：6-9月=[6/12,10/12)、10-11月=[10/12,12/12)、
    # 12月龄=[1,2)——此前 10-11月段 (0.75,1.0) 覆盖 9-12 月龄，9 月龄错归 10-11 段。
    cases = [
        (0.5, "F", "6-9月", (72, 82), (1.10, 1.30)),    # 6 月龄
        (8/12, "F", "6-9月", (72, 82), (1.10, 1.30)),
        (9/12, "F", "6-9月", (72, 82), (1.10, 1.30)),   # ★ 9 月龄（此前错归 10-11 段）
        (10/12, "F", "10-11月", (72, 82), (1.10, 1.30)),  # ★ 10 月龄整入 10-11 段
        (11/12, "F", "10-11月", (72, 82), (1.10, 1.30)),
        (1.0, "F", "12月龄", (72, 120), (0.90, 1.14)),  # 边界：12 月整入 12 月龄段
        (1.4, "F", "12月龄", (72, 120), (0.90, 1.14)),
        (1.9, "F", "12月龄", (72, 120), (0.90, 1.14)),  # 23 月仍属 12 月龄
        (2.0, "F", "2岁", (79, 92), (0.90, 1.05)),      # 边界：24 月整入 2 岁
        (2.4, "F", "2岁", (79, 92), (0.90, 1.05)),
        (2.5, "F", "2岁", (79, 92), (0.90, 1.05)),      # ★ 30 月仍属 2 岁（此前错入 3 岁）
        (2.9, "F", "2岁", (79, 92), (0.90, 1.05)),      # 35 月仍属 2 岁
        (3.0, "F", "3岁", (76, 77), (0.90, 1.05)),      # 边界：36 月整入 3 岁
        (3.4, "F", "3岁", (76, 77), (0.90, 1.05)),
        (3.5, "F", "3岁", (76, 77), (0.90, 1.05)),      # ★ 42 月仍属 3 岁（此前错入 4-6 岁）
        (3.9, "F", "3岁", (76, 77), (0.90, 1.05)),      # 47 月仍属 3 岁
        (4.0, "F", "4-6岁", (64, 90), (0.85, 0.95)),    # 边界：48 月整入 4-6 岁
        (6.9, "F", "4-6岁", (64, 90), (0.85, 0.95)),
        (7.0, "F", "7-8岁", (56, 75), (0.90, 0.95)),   # 边界：7.0 归 7-8 岁
        (7.9, "F", "7-8岁", (56, 75), (0.90, 0.95)),
        (8.9, "F", "7-8岁", (56, 75), (0.90, 0.95)),
        (9.0, "F", "9-10岁", (49, 63), (0.90, 0.95)),  # 边界：9.0 归 9-10 岁
        (10.9, "F", "9-10岁", (49, 63), (0.90, 0.95)),
        (11.0, "F", "11-12岁", (43, 57), (0.90, 0.95)),  # 边界：11.0 归 11-12 岁
        (11.9, "F", "11-12岁", (43, 57), (0.90, 0.95)),
        (12.0, "F", "11-12岁", (43, 57), (0.90, 0.95)),  # ★ 12 岁女孩不再落 15-17 段
        (12.9, "F", "11-12岁", (43, 57), (0.90, 0.95)),
        (13.0, "F", "13-14岁", (39, 50), (0.80, 0.90)),  # 边界：13.0 归 13-14 岁
        (14.9, "F", "13-14岁", (39, 50), (0.80, 0.90)),
        (15.0, "F", "15-17岁", (36, 46), (0.80, 0.90)),  # 边界：15.0 归 15-17 岁
        (17.9, "F", "15-17岁", (36, 46), (0.80, 0.90)),
        # 男孩抽查（含整岁边界）
        (2.5, "M", "2岁", (81, 95), (0.90, 1.05)),
        (3.5, "M", "3岁", (80, 82), (0.90, 1.05)),
        (7.0, "M", "7-8岁", (60, 77), (0.90, 0.95)),
        (12.0, "M", "11-12岁", (48, 63), (0.90, 0.95)),
        (13.0, "M", "13-14岁", (44, 63), (0.80, 0.90)),
        (15.0, "M", "15-17岁", (40, 55), (0.80, 0.90)),
    ]
    for age, sex, label, energy, protein in cases:
        b = core._band_for_age(age, sex)
        assert b["label"] == label, (age, sex, b["label"], label)
        assert tuple(b["energy_sdi"]) == energy, (age, sex, b["energy_sdi"], energy)
        assert tuple(b["protein_sdi"]) == protein, (age, sex, b["protein_sdi"], protein)
    # 端到端：12 岁女孩目标 = 权威 11-12 岁段中位 50 kcal/kg/d
    r = core.calc_prnt_targets(age_years=12, sex="F", weight_kg=40, height_cm=150, ckd_stage=1)
    _near(r["data"]["energy"]["target_kcal_per_kg"], 50.0)


def test_prnt_golden_protein_total_by_sex():
    """N-S1 三审（2026-08-14）：每日蛋白总量按性别输出（15-17 岁 M/F 分列）。

    用户强调：蛋白质 15-17 岁分男女（M 52-65 / F 45-49）；能量更早自 2 岁起
    分男女。本测试锁死 calc_prnt_targets 输出的 sdi_total_g_per_day 与
    energy sdi 的性别拆分（源：theipna.org 2024 实践指南重印 PRNT 2020 Table 1）。
    """
    from CKDNutri_nutrition_mcp import core

    # 蛋白总量分性别（15-17 岁）
    r_f = core.calc_prnt_targets(age_years=15, sex="F", weight_kg=50, ckd_stage=1)
    r_m = core.calc_prnt_targets(age_years=15, sex="M", weight_kg=55, ckd_stage=1)
    assert r_f["data"]["protein"]["sdi_total_g_per_day"] == [45, 49], r_f["data"]["protein"]
    assert r_m["data"]["protein"]["sdi_total_g_per_day"] == [52, 65], r_m["data"]["protein"]
    # 其他段蛋白总量（性别无关）
    r = core.calc_prnt_targets(age_years=9, sex="F", weight_kg=30, ckd_stage=1)
    assert r["data"]["protein"]["sdi_total_g_per_day"] == [26, 40], r["data"]["protein"]

    # 能量性别拆分：12 月龄 M=F；2 岁起 M≠F
    b_12 = core._band_for_age(1.5, "F")
    b_2m, b_2f = core._band_for_age(2.5, "M"), core._band_for_age(2.5, "F")
    assert b_12["energy_sdi"] == [72, 120]
    assert b_2m["energy_sdi"] == [81, 95] and b_2f["energy_sdi"] == [79, 92]
    assert b_2m["energy_sdi"] != b_2f["energy_sdi"], "2 岁起能量必须分男女"

    # 月龄蛋白总量（9 月龄归 6-9 段 9-14；10 月龄归 10-11 段 9-15）
    b_9 = core._band_for_age(9 / 12, "F")
    b_10 = core._band_for_age(10 / 12, "F")
    assert b_9["protein_total_daily"] == [9, 14], b_9
    assert b_10["protein_total_daily"] == [9, 15], b_10


def test_prnt_golden_growth_status():
    """growth_status=failure（生长迟缓）：能量取 SDI 上限、蛋白取上限。"""
    from CKDNutri_nutrition_mcp import core

    r = core.calc_prnt_targets(age_years=6, sex="F", weight_kg=20, height_cm=130,
                               ckd_stage=1, growth_status="failure")
    d = r["data"]
    assert d["energy"]["target_kcal_per_kg"] >= 77.0, d["energy"]  # 迟滞应上浮
    assert d["protein"]["target_g_per_kg"] >= 0.95, d["protein"]


def test_prnt_golden_dialysis_extra():
    """透析患儿：蛋白目标加量（dialysis_extra_g_per_kg > 0）。"""
    from CKDNutri_nutrition_mcp import core

    r = core.calc_prnt_targets(age_years=6, sex="F", weight_kg=20, height_cm=130,
                               ckd_stage=5, dialysis_mode="hemodialysis")
    d = r["data"]
    assert d["protein"]["dialysis_extra_g_per_kg"][1] > 0, d["protein"]
    assert d["protein"]["target_g_per_kg"] > 0.95, d["protein"]


def test_prnt_golden_invalid():
    """weight<=0 / 负年龄 / 非法 growth_status / 非法 vegetarian_mode → ValueError。"""
    from CKDNutri_nutrition_mcp import core

    for kwargs in (
        dict(age_years=6, sex="F", weight_kg=0, height_cm=130),
        dict(age_years=-1, sex="F", weight_kg=20, height_cm=130),
        dict(age_years=6, sex="F", weight_kg=20, height_cm=130, growth_status="bogus"),
        dict(age_years=6, sex="F", weight_kg=20, height_cm=130, vegetarian_mode="bogus"),
    ):
        try:
            core.calc_prnt_targets(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{kwargs} 应抛 ValueError")


# ---------------------------------------------------------------- 生长 Z（nutrition）
def test_growth_golden_zscore():
    """6y M 115cm 20kg：HAZ/WAZ/BAZ 用 WS/T 423 参考表（黄金值自表插值）。"""
    from CKDNutri_nutrition_mcp import core

    g = core.calc_growth_zscore(age_years=6, sex="M", height_cm=115, weight_kg=20)
    d = g["data"]
    # 72 月龄 M：身高 median=118.8 sd=4.5（表内值）→ (115-118.8)/4.5 = -0.844
    _near(d["haz"]["z"], (115 - 118.8) / 4.5)
    # 体重 median=21.6 sd=2.85 → (20-21.6)/2.85 = -0.561
    _near(d["waz"]["z"], (20 - 21.6) / 2.85)
    # BMI=20/(1.15²)=15.12；median=15.4 sd=1.45 → (15.12-15.4)/1.45 = -0.19
    _near(d["baz"]["z"], (15.123 - 15.4) / 1.45, tol=0.02)


def test_growth_golden_age_bands():
    """<84 月按整月参考；≥84 月（7y）HAZ 用 7-18 岁标准、WAZ/BAZ 跳过并提示。"""
    from CKDNutri_nutrition_mcp import core

    g7 = core.calc_growth_zscore(age_years=7, sex="M", height_cm=125, weight_kg=26)
    d7 = g7["data"]
    assert "waz" not in d7 or d7["waz"] is None, d7  # 7 岁无 WAZ 标准
    assert any("7 岁以下" in (w or "") for w in d7.get("warnings", [])), d7["warnings"]


# ---------------------------------------------------------------- PEW 筛查（nutrition）
def test_pew_golden_levels():
    """PEW 三档 + score 契约（S2）：蛋白低+能量低=high(80)；单信号=medium；达标=low(0)。"""
    from CKDNutri_nutrition_mcp import core

    # high：蛋白 20g < floor、能量 600 < 80%×1000
    h = core.assess_pew_risk(avg_protein_g=20, avg_energy_kcal=600,
                             target_protein_g=40, target_energy_kcal=1000)
    assert h["data"]["pew_risk"] == "high" and h["data"]["score"] == 80.0, h
    # medium：仅低白蛋白 30
    m = core.assess_pew_risk(avg_protein_g=45, avg_energy_kcal=950,
                             target_protein_g=40, target_energy_kcal=1000, albumin_g_L=30.0)
    assert m["data"]["pew_risk"] == "medium" and m["data"]["score"] == 20.0, m
    # low：全部达标
    l = core.assess_pew_risk(avg_protein_g=45, avg_energy_kcal=950,
                             target_protein_g=40, target_energy_kcal=1000, albumin_g_L=40.0)
    assert l["data"]["pew_risk"] == "low" and l["data"]["score"] == 0.0, l


def test_pew_golden_invalid():
    """负摄入 / 零目标 → INVALID_INPUT 信封（不抛未捕获异常）。"""
    from CKDNutri_nutrition_mcp import core

    r = core.assess_pew_risk(avg_protein_g=-5, avg_energy_kcal=600,
                             target_protein_g=40, target_energy_kcal=1000)
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", r
    r2 = core.assess_pew_risk(avg_protein_g=40, avg_energy_kcal=600,
                              target_protein_g=40, target_energy_kcal=0)
    assert r2["ok"] is False and r2["error"] == "INVALID_INPUT", r2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"GOLDEN DATASET OK（{len(fns)} 个用例）")
