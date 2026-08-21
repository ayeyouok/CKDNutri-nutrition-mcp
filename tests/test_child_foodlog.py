"""child_assistant 患儿身份 + child_foodlog 孩子自报饮食回归测试（2026-08-21）。

覆盖：
- child 绑定患儿（env A207_CHILD_PATIENT_ID）fail-closed：未设置拒绝 / 跨患儿 FORBIDDEN
- record_child_food：仅 child 可写（parent/doctor FORBIDDEN）；child 写 upsert_food_diary 拒绝
- 积分（小肾侠）：同一天 +1/条、最多 +5、跨天重置；段位映射
- **计算隔离（用户明确要求）**：get_food_diary_summary 双段输出——child_foodlog 有数据
  而 food_diary 为空 → diet_diary_3d 必须为空（孩子自报绝不进营养评估）

pytest + 直接运行双模式（CI 逐文件 `python tests/test_*.py`，不依赖 pytest）。
"""
import os

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
import sys
import tempfile
from pathlib import Path

os.environ["A207_NUTRITION_ASSESSMENT_DATA_DIR"] = tempfile.mkdtemp(prefix="a207-nutrition-child-")

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from a207_policy import (  # noqa: E402
    CallerUnknown,
    PermissionDenied,
    as_caller,
    get_child_patient_id,
)

from CKDNutri_nutrition_mcp import core  # noqa: E402
from CKDNutri_nutrition_mcp import nutrition_repository as repo_mod  # noqa: E402

CHILD = "child_assistant"
BOUND = "P0020"
OTHER = "P0001"


def _reset_store() -> None:
    """每个用例独立数据目录（防 child_foodlog/food_diary 跨用例累积污染断言）。"""
    os.environ["A207_NUTRITION_ASSESSMENT_DATA_DIR"] = tempfile.mkdtemp(prefix="a207-nutrition-child-")
    repo_mod._REPO_CACHE.clear()


def _set_child_env() -> None:
    _reset_store()
    os.environ["A207_CHILD_PATIENT_ID"] = BOUND


def _clear_child_env() -> None:
    os.environ.pop("A207_CHILD_PATIENT_ID", None)


# ---- 绑定 fail-closed ----

def test_child_binding_env_missing_rejected():
    """未设置 A207_CHILD_PATIENT_ID → get_child_patient_id 抛 CallerUnknown（fail-closed）。"""
    _clear_child_env()
    try:
        get_child_patient_id()
    except CallerUnknown:
        return
    raise AssertionError("未设置绑定 env 应抛 CallerUnknown")


def test_child_record_cross_patient_forbidden():
    """child 写非绑定患儿（P0001）→ FORBIDDEN（跨患儿越权）。"""
    _set_child_env()
    with as_caller(CHILD):
        r = core.record_child_food(OTHER, [{"date": "2026-08-21", "food": "苹果"}])
    assert r["ok"] is False and r["error"] == "FORBIDDEN", r


def test_child_record_bound_patient_ok():
    """child 写绑定患儿（P0020）→ 成功，返回积分/段位/来源标注。"""
    _set_child_env()
    with as_caller(CHILD):
        r = core.record_child_food(BOUND, [{"date": "2026-08-21", "food": "苹果", "amount": "半个"}])
    assert r["ok"] is True, r
    d = r["data"]
    assert d["total_points"] == 1 and d["awarded"] is True
    assert d["source"] == "child_self_report"
    assert "小肾侠" in d["band"]


# ---- 写权限收口 ----

def test_parent_cannot_write_child_foodlog():
    """家长/医生写 record_child_food → PermissionDenied（gate 工具级收口）。"""
    _set_child_env()
    for role in ("parent_assistant", "doctor_assistant"):
        with as_caller(role):
            try:
                core.record_child_food(BOUND, [{"date": "2026-08-21", "food": "苹果"}])
            except PermissionDenied:
                continue
            raise AssertionError(f"{role} 写 record_child_food 应被拒绝")


def test_child_cannot_write_food_diary():
    """child 写 upsert_food_diary（医疗记录）→ PermissionDenied（gate 显式拒绝）。"""
    _set_child_env()
    with as_caller(CHILD):
        try:
            core.upsert_food_diary(BOUND, [{"date": "2026-08-21", "food": "苹果",
                                            "energy_kcal": 50.0, "protein_g": 0.5,
                                            "potassium_mg": 100.0, "phosphorus_mg": 10.0,
                                            "sodium_mg": 5.0}])
        except PermissionDenied:
            return
    raise AssertionError("child 写 food_diary 应被工具级拒绝")


# ---- 计算隔离（关键）----

def test_diet_diary_3d_not_contaminated_by_child_foodlog():
    """**计算隔离**：child_foodlog 有数据、food_diary 为空 → diet_diary_3d 必须为空。

    用户明确要求 + 测试锁定：孩子自报数据绝不进营养评估。
    """
    _set_child_env()
    with as_caller(CHILD):
        # 先写大量孩子自报数据（数值巨大，若混入评估会暴露）
        core.record_child_food(BOUND, [{"date": "2026-08-21", "food": "炸鸡", "amount": "10份"}])
        core.record_child_food(BOUND, [{"date": "2026-08-21", "food": "糖果", "amount": "100颗"}])
    # 用医生身份读摘要（food_diary 空）
    with as_caller("doctor_assistant"):
        r = core.get_food_diary_summary(BOUND)
    assert r["ok"] is True, r
    d = r["data"]
    assert d["diet_diary_3d"] is None, \
        "food_diary 为空时 diet_diary_3d 必须为 None（child_foodlog 不得混入评估）"
    assert d["child_foodlog"]["entry_count"] == 2, d["child_foodlog"]
    assert d["child_foodlog"]["source"] == "child_self_report"


def test_diet_diary_3d_aggregates_food_diary_only():
    """**计算隔离**：food_diary 与 child_foodlog 同时有数据 → diet_diary_3d 只反映 food_diary。"""
    _set_child_env()
    with as_caller("doctor_assistant"):
        # food_diary：能量 100 kcal/日（真实医疗记录）
        core.upsert_food_diary(BOUND, [{"date": "2026-08-21", "meal": "早餐", "food": "粥",
                                        "energy_kcal": 100.0, "protein_g": 2.0,
                                        "potassium_mg": 50.0, "phosphorus_mg": 10.0,
                                        "sodium_mg": 5.0}])
    with as_caller(CHILD):
        # child_foodlog：能量巨大（孩子自报 9999 kcal，若混入会拉爆均值）
        core.record_child_food(BOUND, [{"date": "2026-08-21", "food": "乱报", "amount": "9999"}])
    with as_caller("doctor_assistant"):
        r = core.get_food_diary_summary(BOUND)
    d = r["data"]
    assert abs(d["diet_diary_3d"]["avg_energy_kcal"] - 100.0) < 0.01, \
        f"diet_diary_3d 应只来自 food_diary（100 kcal），收到 {d['diet_diary_3d']['avg_energy_kcal']}"
    assert d["child_foodlog"]["entry_count"] == 1


# ---- 积分（小肾侠）----

def test_daily_points_cap_at_5():
    """同一天最多 +5 分：第 6 笔记录照写、不加分。"""
    _set_child_env()
    with as_caller(CHILD):
        for i in range(6):
            r = core.record_child_food(BOUND, [{"date": "2026-08-21",
                                                "food": f"食物{i}"}])
            assert r["ok"] is True, r
    d = r["data"]
    assert d["total_points"] == 5, d
    assert d["daily_points"] == 5, d
    assert d["awarded"] is False, "第 6 笔不应再加分"
    assert d["entry_count"] == 6, "记录本身照写"


def test_points_reset_across_day():
    """跨天重置当日计数：last_points_date ≠ 今天 → daily_points 归零后重新累计。

    积分按"服务器 UTC 当天"重置（与条目日期无关）；模拟跨天=把存储行
    last_points_date 回拨到过去再记一笔。
    """
    _set_child_env()
    with as_caller(CHILD):
        core.record_child_food(BOUND, [{"date": "2026-08-21", "food": "第一天"}])
        # 模拟跨天：回拨 last_points_date
        row = core._load_child_foodlog(BOUND)
        row["last_points_date"] = "2000-01-01"
        core._save_child_foodlog(BOUND, row)
        r1 = core.record_child_food(BOUND, [{"date": "2026-08-21", "food": "新的一天"}])
    assert r1["data"]["daily_points"] == 1, "跨天后 daily_points 应从 1 重新开始"
    assert r1["data"]["total_points"] == 2, "累计分跨天保留"


def test_band_thresholds():
    """小肾侠段位阈值映射（用户指定）：0 青铜 / 10 白银 / 221 王者 / 366 全球精英。"""
    assert core._child_band(0)[0] == "小肾侠·青铜"
    assert core._child_band(9)[0] == "小肾侠·青铜"
    assert core._child_band(10)[0] == "小肾侠·白银"
    assert core._child_band(220)[0] == "小肾侠·大师"
    assert core._child_band(221)[0] == "小肾侠·王者"
    assert core._child_band(366)[0] == "小肾侠·全球精英"


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"CHILD FOODLOG OK（{len(fns)} 个用例）")


if __name__ == "__main__":
    _run_all()
