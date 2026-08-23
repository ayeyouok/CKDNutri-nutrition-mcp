# 十二审（2026-08-24）回归测试：core.py 4 项 P2 边界修复防回归
# 约定：顶部 setdefault 注入测试 env+caller；零 pytest 依赖；不跨包 import。
import os
import sys
import math

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_NUTRITION_ASSESSMENT_DATA_DIR",
                      "C:/tmp/a207-ci-check-p")
os.environ.setdefault("A207_CHILD_PATIENT_ID", "P0007")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from CKDNutri_nutrition_mcp import core  # noqa: E402


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(f"{name} FAILED :: {detail}")


# P-01：schofield_bmr_kcal 对 NaN 输入返回 None（不穿透返回 NaN）
def test_p01_schofield_nan():
    r1 = core.schofield_bmr_kcal("M", 10.0, float("nan"), 140.0)
    check("P-01 weight NaN -> None", r1 is None, repr(r1))
    r2 = core.schofield_bmr_kcal("M", 10.0, 30.0, float("nan"))
    check("P-01 height NaN -> None", r2 is None, repr(r2))
    r3 = core.schofield_bmr_kcal("M", float("nan"), 30.0, 140.0)
    check("P-01 age NaN -> None", r3 is None, repr(r3))
    # 对照：正常输入仍算得合理值
    r4 = core.schofield_bmr_kcal("M", 10.0, 30.0, 140.0)
    check("P-01 正常输入得正值", r4 is not None and r4 > 0, repr(r4))


# P-02：_aggregate 对历史脏数据数字字符串正确累加（不被归零）
def test_p02_aggregate_numstr():
    entries = [
        {"date": "2026-08-20", "meal": "午餐", "food": "米饭",
         "energy_kcal": "350.5", "protein_g": "8.0",
         "potassium_mg": 100, "phosphorus_mg": 50, "sodium_mg": 10},
        {"date": "2026-08-20", "meal": "晚餐", "food": "面条",
         "energy_kcal": 400, "protein_g": 12,
         "potassium_mg": 120, "phosphorus_mg": 60, "sodium_mg": 20},
    ]
    res = core._aggregate(entries)
    avg_e = res["diet_diary_3d"]["avg_energy_kcal"]
    # 350.5 + 400 = 750.5，两天同日期 → num_days=1 → 750.5
    check("P-02 数字字符串正确累加", abs(avg_e - 750.5) < 1e-6, f"avg_e={avg_e}")
    avg_p = res["diet_diary_3d"]["avg_protein_g"]
    check("P-02 蛋白 8+12=20", abs(avg_p - 20.0) < 1e-6, f"avg_p={avg_p}")
    # 对照：非数字字符串仍归零
    bad = [{"date": "2026-08-20", "meal": "x", "food": "y",
            "energy_kcal": "abc", "protein_g": 5,
            "potassium_mg": 0, "phosphorus_mg": 0, "sodium_mg": 0}]
    res_bad = core._aggregate(bad)
    check("P-02 非法字符串归零", res_bad["diet_diary_3d"]["avg_energy_kcal"] == 0.0,
          str(res_bad["diet_diary_3d"]))


# P-03：get_food_diary_summary 含 int date 脏数据不崩溃且返回
def test_p03_food_diary_int_date():
    pid = "P0099"
    # 直写 child foodlog 脏数据：date 为 int（20260824）混入
    core._save_child_foodlog(pid, {
        "entries": [
            {"date": 20260824, "meal": "早餐", "food": "面包",
             "energy_kcal": 200, "protein_g": 6,
             "potassium_mg": 80, "phosphorus_mg": 40, "sodium_mg": 15},
            {"date": "2026-08-23", "meal": "午餐", "food": "米饭",
             "energy_kcal": 350, "protein_g": 8,
             "potassium_mg": 100, "phosphorus_mg": 50, "sodium_mg": 10},
        ],
        "total_points": 10,
    })
    r = core.get_food_diary_summary(pid, guardian_token=None)
    check("P-03 ok 返回", r.get("ok") is True, str(r)[:200])
    child = r["data"]["child_foodlog"]
    dd = child["recent_days"]
    # int date 归一为 "20260824" 字符串键，最近 3 天应包含它
    check("P-03 recent_days 含归一键", "20260824" in dd, str(list(dd.keys())))
    check("P-03 不跨类型崩溃", True)  # 走到这里即未抛 TypeError


# P-04：get_pew_history 返回 points 已按 date 升序
def test_p04_pew_history_sorted_points():
    pid = "P0088"
    # 乱序写入历史点（date 不单调）
    core._save_patient_pew_store(pid, [
        {"date": "2026-08-10", "level": "low", "score": 10},
        {"date": "2026-08-01", "level": "high", "score": 70},
        {"date": "2026-08-05", "level": "medium", "score": 40},
    ])
    r = core.get_pew_history(pid)
    check("P-04 ok 返回", r.get("ok") is True, str(r)[:200])
    pts = r["data"]["points"]
    dates = [p.get("date") for p in pts if isinstance(p, dict)]
    check("P-04 points 按 date 升序", dates == sorted(dates), str(dates))
    # 前端展示用，第一条应为最早（08-01），最后一条为最晚（08-10）
    check("P-04 首=最早", dates[0] == "2026-08-01", dates[0])
    check("P-04 尾=最晚", dates[-1] == "2026-08-10", dates[-1])


if __name__ == "__main__":
    test_p01_schofield_nan()
    test_p02_aggregate_numstr()
    test_p03_food_diary_int_date()
    test_p04_pew_history_sorted_points()
    print("\nALL TWELFTH-REVIEW TESTS PASS")
