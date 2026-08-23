"""六审（2026-08-23）nutrition-mcp 架构审查回归：临床计算 + 接口契约防回归。

零 pytest 依赖，直接 `python tests/test_review_20260823k.py` 运行；CI 由
`for f in tests/test_*.py; do python "$f" || exit 1; done` 统一调度。

覆盖本轮修复的两项（其余 ③④为删冗余/死代码，靠全量冒烟保证不回归）：
- K-01（claim 2）：get_pew_history 历史点 level 含大写/" Medium" 等非归一化值时，
  趋势计算不再 KeyError 500（此前 first["level"] 直索引 _PEW_LEVEL_ORDER["HIGH"] 崩）。
- K-02（claim 1）：_screen_pew 白蛋白单点单位防御——传入 4.2（g/dL 误传，=42 g/L 正常）
  不再误判 low_albumin=True（错扣 20 分把健康患儿误诊为低白蛋白/PEW 风险）；
  传入 3.5（g/dL 误传，=35 g/L 真实偏低）正确判 low_albumin 并标注换算。
"""
import os

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
import sys
import tempfile
from pathlib import Path
from unittest import mock

# 存储隔离：PEW/日记写路径落到临时目录（避免跨运行污染仓库状态）
os.environ["A207_NUTRITION_ASSESSMENT_DATA_DIR"] = tempfile.mkdtemp(prefix="a207-nutrition-k-")

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from a207_policy import as_caller  # noqa: E402

from CKDNutri_nutrition_mcp import core  # noqa: E402


def test_k01_pew_history_uppercase_level_no_keyerror():
    """K-01（claim 2）：历史 level 含大写/" Medium" 等非归一化值，趋势计算不 KeyError。

    模拟迁移/legacy 脏数据：首点 "HIGH"（大写）、末点 " Medium"（带前导空格）。
    修复后取权重统一 .strip().lower()，应正确判 worsening 且不再 500。
    """
    dirty = {"P0001": [
        {"date": "2026-08-01", "score": 95.0, "level": "HIGH"},
        {"date": "2026-08-02", "score": 50.0, "level": " Medium"},
    ]}
    with mock.patch.object(core, "_load_patient_pew_store", return_value=dirty):
        r = core.get_pew_history("P0001")
    assert r["ok"] is True, r  # 修复前此处 KeyError: 'HIGH' → 500
    # HIGH(2) → Medium(1) 风险下降 = improving（不 KeyError 即修复核心）
    assert r["data"]["trend"] == "improving", r["data"]
    assert r["data"]["count"] == 2, r["data"]


def test_k02_albumin_gdl_auto_convert_no_false_low():
    """K-02（claim 1）：白蛋白单位防御——g/dL 误传自动换算，不误判健康患儿。

    - 4.2 g/dL（=42 g/L 正常）→ 不应判 low_albumin；rationale 标注换算。
    - 3.5 g/dL（=35 g/L 真实偏低）→ 应判 low_albumin；rationale 标注换算。
    - 42 g/L（已正确单位）→ 不误判，无换算标注。
    """
    # 健康患儿误传 4.2（g/dL=42 g/L 正常）：蛋白/能量达标 → 不应因单位混淆误判 medium
    r_ok = core._screen_pew(avg_p=40.0, avg_e=1200.0, floor_p=35.0,
                            target_e=1400.0, albumin_g_L=4.2)
    assert r_ok["risk"] == "low", r_ok  # 核心：不误判为低白蛋白/PEW
    assert r_ok["score"] == 0.0, r_ok  # 未错扣 20 分

    # 真实偏低 3.5 g/dL（=35 g/L <38）：换算后仍判 low_albumin 并标注
    r_low = core._screen_pew(avg_p=40.0, avg_e=1200.0, floor_p=35.0,
                             target_e=1400.0, albumin_g_L=3.5)
    assert "白蛋白 35.0 g/L <38" in r_low["rationale"], r_low
    assert "（注：原输入 3.5 ≤10，已按 g/dL 自动换算为 35.0 g/L）" in r_low["rationale"], r_low

    # 已正确单位 42 g/L：不触发低白蛋白（无换算、无信号）
    r_norm = core._screen_pew(avg_p=40.0, avg_e=1200.0, floor_p=35.0,
                              target_e=1400.0, albumin_g_L=42.0)
    assert "白蛋白" not in r_norm["rationale"], r_norm  # 无低白蛋白信号


if __name__ == "__main__":
    test_k01_pew_history_uppercase_level_no_keyerror()
    test_k02_albumin_gdl_auto_convert_no_false_low()
    print("OK: test_review_20260823k 全部通过")
