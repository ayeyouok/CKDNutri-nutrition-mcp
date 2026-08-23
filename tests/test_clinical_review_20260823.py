"""CKDNutri-nutrition-mcp core.py 临床复核回归测试（2026-08-23）。

覆盖本轮 9 条审查 claim 中「属实·已修」项的防回归（零 pytest 依赖，直接运行）：

- C-01：calc_prnt_targets PD 葡萄糖扣减按方案 dm——standard（未透析基准）不扣、
  adjusted（实际透析）扣；患者级数据错配校验（HD/未透析传 pd_glucose 拒绝）保留。
- C-02：calc_growth_zscore WHZ<-2 急性消瘦 → growth_status=failure（此前漏判 normal）。
- C-03：81-83.9 月龄（6.75-6.99 岁）超重盲区——WS/T 586 分支放宽至 age_years>=6.0。
- D-01：record_child_food write_mode=False 预演不落盘不加分（此前参数被忽略仍落盘）。
- D-02：upsert_food_diary S-3 幂等仅跨调用去重——单次调用同餐多份食物全保留。
- D-03：_aggregate 过滤空/脏日期，防止 num_days 虚增均值腰斩。
- R-01：schofield_bmr_kcal 超低体重负能耗 → None。
- R-02：assess_intake_vs_target 负数摄入 → INVALID_INPUT。
- R-04：get_food_diary_summary 空记录分支补 recent_entries（schema 一致）。

C-01 修复前的对比输入（PD 患儿 8 岁 M 25kg，pd_glucose=300）：
  修复前 standard 也扣 300（两方案能量相同、standard 名不副实）；
  修复后 standard 1712.5/扣 0、adjusted 1412.5/扣 300。
C-02 修复前对比输入（10 月男 h=75 w=8.1）：HAZ=0.27 WAZ=-1.67 均不触发，
  仅 WHZ=-2.17 触发 → 修复前判 normal、修复后 failure。
"""
from __future__ import annotations

import os

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
import sys
import tempfile
from pathlib import Path

os.environ["A207_NUTRITION_ASSESSMENT_DATA_DIR"] = tempfile.mkdtemp(prefix="a207-nutrition-review-")
os.environ.setdefault("A207_CHILD_PATIENT_ID", "P0020")

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from a207_policy import as_caller  # noqa: E402
from CKDNutri_nutrition_mcp import core  # noqa: E402
from CKDNutri_nutrition_mcp import nutrition_repository as repo_mod  # noqa: E402

CHILD = "child_assistant"
BOUND = "P0020"


def _reset_store() -> None:
    os.environ["A207_NUTRITION_ASSESSMENT_DATA_DIR"] = tempfile.mkdtemp(prefix="a207-nutrition-review-")
    repo_mod._REPO_CACHE.clear()


def test_c01_pd_deduction_only_adjusted():
    """C-01：PD 葡萄糖扣减仅 adjusted（实际处方）；standard=未透析基准不扣。"""
    _reset_store()
    with as_caller("doctor_assistant"):
        r = core.calc_prnt_targets(
            age_years=8, sex="M", weight_kg=25.0, height_cm=125.0,
            ckd_stage=4, dialysis_mode="peritoneal", pd_glucose_kcal_per_day=300.0)
    assert r["ok"] is True, r
    plans = {p["label"]: p for p in r["data"]["regimens"]}
    std, adj = plans["standard"], plans["adjusted"]
    assert std["energy"]["pd_glucose_deduction_kcal"] == 0, \
        "standard（未透析基准）不应扣 PD 葡萄糖"
    assert adj["energy"]["pd_glucose_deduction_kcal"] == 300.0, \
        "adjusted（实际处方）应扣 PD 葡萄糖"
    assert adj["energy"]["target_kcal_per_day"] < std["energy"]["target_kcal_per_day"], \
        "两方案能量应不同（standard>adjusted）——此前相同失去对照意义"
    # 患者级数据错配校验保留：HD 传 pd_glucose 仍拒绝
    with as_caller("doctor_assistant"):
        try:
            core.calc_prnt_targets(age_years=8, sex="M", weight_kg=25.0, height_cm=125.0,
                                   ckd_stage=4, dialysis_mode="hemodialysis",
                                   pd_glucose_kcal_per_day=300.0)
            raise AssertionError("HD 传 pd_glucose 应拒绝")
        except ValueError:
            pass
    print("  [ok] C-01 PD 扣减仅 adjusted（standard 基准不扣 + 患者级校验保留）")


def test_c02_whz_wasting_failure():
    """C-02：WHZ<-2 急性消瘦独立触发 failure（HAZ/WAZ 均正常时此前误判 normal）。"""
    _reset_store()
    with as_caller("doctor_assistant"):
        r = core.calc_growth_zscore(age_years=10 / 12, sex="M",
                                    height_cm=75.0, weight_kg=8.1)
    assert r["ok"] is True, r
    d = r["data"]
    assert d["haz"]["z"] >= -2 and d["waz"]["z"] >= -2, \
        "前置条件：HAZ/WAZ 不触发（否则非 WHZ 分支）"
    assert d["whz"]["z"] < -2, "前置条件：WHZ 必须 < -2"
    assert d["growth_status_suggestion"] == "failure", \
        f"WHZ={d['whz']['z']} < -2 急性消瘦应判 failure，实际 {d['growth_status_suggestion']}"
    assert any("WHZ" in w for w in d["warnings"]), "应含 WHZ 告警"
    print("  [ok] C-02 WHZ<-2 急性消瘦独立判 failure")


def test_c03_age82m_overweight_no_blank():
    """C-03：81-83.9 月龄（BAZ 盲区）启用 WS/T 586 判超重，不再漏判 normal。"""
    _reset_store()
    with as_caller("doctor_assistant"):
        r = core.calc_growth_zscore(age_years=82 / 12, sex="M",
                                    height_cm=125.0, weight_kg=32.0)
    assert r["ok"] is True, r
    d = r["data"]
    bmi = 32.0 / (1.25 ** 2)
    assert bmi >= core._ws586_overweight_threshold(82 / 12, "M"), \
        "前置条件：BMI 应达 WS/T 586 82 月龄超重界值"
    assert d["growth_status_suggestion"] == "overweight", \
        f"82 月龄 BMI {bmi:.1f} 应判 overweight（此前 age_months>=84 拦截漏判），实际 {d['growth_status_suggestion']}"
    print("  [ok] C-03 82 月龄超重盲区修复（WS/T 586 覆盖）")


def test_d01_child_foodlog_write_mode_dry_run():
    """D-01：record_child_food write_mode=False 预演——不落盘、不加分、persisted=False。"""
    _reset_store()
    with as_caller(CHILD):
        r = core.record_child_food(BOUND, [{"date": "2026-08-22", "meal": "早餐",
                                            "food": "鸡蛋", "amount": "2个"}],
                                   write_mode=False)
    assert r["ok"] is True, r
    assert r["data"]["persisted"] is False, "预演不应落盘"
    assert r["data"]["awarded"] is False, "预演不应加分"
    row = core._load_child_foodlog(BOUND)
    assert not row.get("entries"), "预演后存储应无条目"
    assert int(row.get("total_points", 0) or 0) == 0, "预演后积分应为 0"
    # 正式写入仍正常
    with as_caller(CHILD):
        r2 = core.record_child_food(BOUND, [{"date": "2026-08-22", "meal": "早餐",
                                             "food": "鸡蛋", "amount": "2个"}],
                                    write_mode=True)
    assert r2["data"]["persisted"] is True and r2["data"]["awarded"] is True, r2
    print("  [ok] D-01 record_child_food write_mode=False 预演不落盘不加分")


def test_d02_food_diary_multi_servings_kept():
    """D-02：单次调用同餐多份同名食物全保留（不再被 S-3 键覆盖吞并）。"""
    _reset_store()
    with as_caller("doctor_assistant"):
        r = core.upsert_food_diary("P0001", entries=[
            {"date": "2026-08-22", "meal": "午餐", "food": "米饭",
             "energy_kcal": 100.0, "protein_g": 2.0,
             "potassium_mg": 10.0, "phosphorus_mg": 5.0, "sodium_mg": 1.0},
            {"date": "2026-08-22", "meal": "午餐", "food": "米饭",
             "energy_kcal": 100.0, "protein_g": 2.0,
             "potassium_mg": 10.0, "phosphorus_mg": 5.0, "sodium_mg": 1.0},
        ])
    assert r["ok"] is True, r
    assert r["data"]["entry_count"] == 2, \
        f"同餐 2 份米饭应全保留（摄入 200g 不得缩水为 100g），实际 {r['data']['entry_count']}"
    # 跨调用重试仍幂等（S-3 保留）：重发同键 1 份 → 覆盖旧残留 → 1 条
    with as_caller("doctor_assistant"):
        r2 = core.upsert_food_diary("P0001", entries=[
            {"date": "2026-08-22", "meal": "午餐", "food": "米饭",
             "energy_kcal": 100.0, "protein_g": 2.0,
             "potassium_mg": 10.0, "phosphorus_mg": 5.0, "sodium_mg": 1.0},
        ])
    assert r2["data"]["entry_count"] == 1, \
        "跨调用同键重试应覆盖旧残留（S-3 幂等保留，弱网重试不产生重复行）：实际" \
        f" {r2['data']['entry_count']}"
    print("  [ok] D-02 单次同餐多份保留 + 跨调用重试幂等（S-3）")


def test_d03_aggregate_empty_date_filtered():
    """D-03：_aggregate 空/脏日期跳过，不虚增天数分母。"""
    agg = core._aggregate([
        {"date": "2026-08-22", "energy_kcal": 100.0, "protein_g": 5.0},
        {"date": "", "energy_kcal": 999.0, "protein_g": 50.0},   # 脏条目（缺日期）
        {"date": "   ", "energy_kcal": 999.0, "protein_g": 50.0},  # 纯空白
    ])
    assert agg["day_count"] == 1, \
        f"空日期不得虚增天数分母，实际 day_count={agg['day_count']}"
    assert abs(agg["diet_diary_3d"]["avg_energy_kcal"] - 100.0) < 0.01, \
        f"均值应只来自有效日期（100），实际 {agg['diet_diary_3d']['avg_energy_kcal']}"
    print("  [ok] D-03 空日期过滤，防分母虚增均值腰斩")


def test_r01_schofield_extreme_low_weight_none():
    """R-01：超低体重早产儿 Schofield 负能耗 → None（不产出负数 BMR）。"""
    from CKDNutri_nutrition_mcp.core import schofield_bmr_kcal
    got = schofield_bmr_kcal("M", 1.0, 1.0, 35.0)
    assert got is None, f"1.0kg/35cm 早产儿 BMR 应为 None（线性外推负能耗），实际 {got}"
    # 正常值不回归
    ok = schofield_bmr_kcal("M", 8.0, 25.0, 125.0)
    assert ok is not None and ok > 0, f"正常输入应返回正值 BMR，实际 {ok}"
    print("  [ok] R-01 Schofield 负能耗保护")


def test_r02_intake_negative_rejected():
    """R-02：assess_intake_vs_target 负数摄入 → INVALID_INPUT。"""
    _reset_store()
    with as_caller("doctor_assistant"):
        r = core.assess_intake_vs_target(
            {"avg_energy_kcal": -500.0, "avg_protein_g": 20.0,
             "avg_potassium_mg": 500.0, "avg_phosphorus_mg": 100.0, "avg_sodium_mg": 50.0},
            age_years=8, sex="M", weight_kg=25.0, height_cm=125.0, ckd_stage=3)
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", \
        f"负数摄入应 INVALID_INPUT，实际 {r}"
    print("  [ok] R-02 负数摄入拦截")


def test_r04_summary_empty_has_recent_entries():
    """R-04：get_food_diary_summary 空记录分支含 recent_entries（schema 一致）。"""
    _reset_store()
    with as_caller("doctor_assistant"):
        r = core.get_food_diary_summary("P0001")
    assert r["ok"] is True, r
    d = r["data"]
    assert "recent_entries" in d and d["recent_entries"] == [], \
        f"空记录分支应含 recent_entries=[]，实际 keys={list(d.keys())}"
    print("  [ok] R-04 空记录分支 recent_entries schema 一致")


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\nCLINICAL REVIEW (2026-08-23) OK（{len(fns)} 个用例）")


if __name__ == "__main__":
    _run_all()
