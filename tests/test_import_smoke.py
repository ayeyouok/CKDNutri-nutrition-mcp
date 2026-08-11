"""P2 冒烟自测：导入 server 不报错 + 代表性工具可调用。

运行：pytest tests/test_import_smoke.py  (或 python tests/test_import_smoke.py)
依赖：a207-policy 已随 pip install -e . 安装。
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

os.environ.setdefault("A207_CALLER", "doctor_assistant")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_server_imports():
    """导入 server 不可抛错（回归：合并时 calc_pnpr/calc_pd_glucose_absorption 等导入错位）。"""
    mod = importlib.import_module("CKDNutri_nutrition_mcp.server")
    assert mod.mcp is not None


def test_calc_prnt_targets():
    from CKDNutri_nutrition_mcp import core

    r = core.calc_prnt_targets(age_years=6, sex="F", weight_kg=20, height_cm=130, ckd_stage=1)
    assert "ok" in r and "data" in r


if __name__ == "__main__":
    test_server_imports()
    test_calc_prnt_targets()
    print("P2 SMOKE OK")
