"""P2 营养计算域 —— PRNT目标+食物DB+食谱+生长Z+PEW+日记。

合并自 M3 (a207-nutrition-assessment-mcp) + M5 (a207-nutrition-calc-mcp) + M7 (a207-meal-plan-mcp)。
"""
from __future__ import annotations

from importlib import metadata as _metadata


def _pkg_version() -> str:
    """从安装元数据读取版本（P2-6：与 pyproject.toml 单一事实源对齐）。未安装时回退 "0.0.0"。"""
    try:
        return _metadata.version("CKDNutri-nutrition-mcp")
    except _metadata.PackageNotFoundError:
        return "0.0.0"


__version__ = _pkg_version()

__all__ = ["__version__"]
