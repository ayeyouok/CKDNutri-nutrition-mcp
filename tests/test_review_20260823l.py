# -*- coding: utf-8 -*-
"""七审（2026-08-23）回归：P0 学龄消瘦 + PEW high 低白蛋白遮蔽 + child 合并语义。

零 pytest 依赖，遵循项目约定顶部 setdefault 注入 env+caller。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
os.environ.setdefault("A207_CHILD_PATIENT_ID", "P0007")

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from a207_policy import as_caller  # noqa: E402
from CKDNutri_nutrition_mcp import core  # noqa: E402

_FAIL = []


def check(name, cond, extra=""):
    if cond:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name} {extra}")
        _FAIL.append(name)


# L-01：P0 学龄消瘦患儿（12 岁男，BMI=12.44）不得判 normal —— 应 failure（SDI 上限）
with as_caller("doctor_assistant"):
    r = core.calc_growth_zscore(age_years=12.0, sex="M", bmi=12.44)
gs = r["data"]["growth_status_suggestion"]
check("L-01 12岁男 BMI12.44 判 failure（非 normal）", gs == "failure", f"got={gs}")
check("L-01 warning 提及 WS/T 456 消瘦", any("WS/T 456" in w for w in r["data"]["warnings"]),
      f"warnings={r['data']['warnings']}")

# L-01b：正常 BMI 儿童不误伤（12 岁男 BMI=17.0 > 界值 15.4）
with as_caller("doctor_assistant"):
    r2 = core.calc_growth_zscore(age_years=12.0, sex="M", bmi=17.0)
gs2 = r2["data"]["growth_status_suggestion"]
check("L-01b 12岁男 BMI17.0 不误判 failure", gs2 != "failure", f"got={gs2}")

# L-02：PEW high 分支必须拼接低白蛋白说明（此前写死遮蔽）
r_pew = core._screen_pew(avg_p=20.0, avg_e=500.0, floor_p=35.0,
                         target_e=1400.0, albumin_g_L=20.0)
check("L-02 high 风险", r_pew["risk"] == "high", f"risk={r_pew['risk']}")
check("L-02 rationale 含低白蛋白", "白蛋白 20" in r_pew["rationale"],
      f"rationale={r_pew['rationale']}")
check("L-02 不误判正常患儿（alb=42）", "白蛋白" not in core._screen_pew(
    avg_p=40.0, avg_e=1200.0, floor_p=35.0, target_e=1400.0,
    albumin_g_L=42.0)["rationale"])

# L-03：child 同餐同食物同量两条收敛为 1 条（重复自报去重，区别于 food_diary 多份）
import datetime
from unittest import mock

_today = datetime.datetime.now(core._CN_TZ).strftime("%Y-%m-%d")
_entries = [
    {"date": _today, "meal": "早餐", "food": "鸡蛋", "amount": "1个"},
    {"date": _today, "meal": "早餐", "food": "鸡蛋", "amount": "1个"},
]
_saved = {}

def _fake_load(pid):
    return {"entries": [], "total_points": 0, "daily_points": 0,
            "last_points_date": ""}

def _fake_save(pid, row):
    _saved["row"] = row

with mock.patch.object(core, "_load_child_foodlog", _fake_load), \
     mock.patch.object(core, "_save_child_foodlog", _fake_save), \
     as_caller("child_assistant"):
    r_child = core.record_child_food("P0007", entries=_entries, write_mode=True)

check("L-03 child 同餐同食物同量收敛为 1 条", r_child["ok"] and
      r_child["data"]["entry_count"] == 1, f"data={r_child.get('data')}")

# L-03b：跨调用同键覆盖（重试幂等）——旧条目被本次值替换
_old_row = {"entries": [{"date": _today, "meal": "早餐", "food": "鸡蛋", "amount": "1个"}],
            "total_points": 0, "daily_points": 0, "last_points_date": ""}
def _fake_load2(pid):
    return dict(_old_row, entries=list(_old_row["entries"]))
_saved2 = {}
def _fake_save2(pid, row):
    _saved2["row"] = row

with mock.patch.object(core, "_load_child_foodlog", _fake_load2), \
     mock.patch.object(core, "_save_child_foodlog", _fake_save2), \
     as_caller("child_assistant"):
    r_child2 = core.record_child_food(
        "P0007", entries=[{"date": _today, "meal": "早餐", "food": "鸡蛋",
                           "amount": "2个"}], write_mode=True)

check("L-03b 跨调用同键覆盖（重试幂等）", r_child2["ok"] and
      r_child2["data"]["entry_count"] == 1 and
      _saved2["row"]["entries"][0]["amount"] == "2个",
      f"data={r_child2.get('data')} saved={_saved2.get('row')}")

print("\n==== RESULT ====")
if _FAIL:
    print(f"FAILED: {_FAIL}")
    sys.exit(1)
print("ALL PASS")
