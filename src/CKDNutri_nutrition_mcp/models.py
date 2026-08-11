# -*- coding: utf-8 -*-
"""pydantic 请求模型：约束 M7 工具入参形状。值域与 contracts/pcp-schema.json 口径对齐。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class MealPlanRequest(BaseModel):
    target_energy_kcal: float = Field(gt=0, description="每日能量目标 kcal（来自 M3 calc_prnt_targets）")
    target_protein_g: float = Field(gt=0, description="每日蛋白质目标 g（来自 M3）")
    target_k_mg: float = Field(ge=0, description="每日钾上限 mg")
    target_p_mg: float = Field(ge=0, description="每日磷上限 mg")
    target_na_mg: float = Field(ge=0, description="每日钠上限 mg")
    days: int = Field(default=7, ge=1, le=30, description="生成天数")
    vegetarian: bool = Field(default=False, description="是否仅用植物性蛋白源")
    exclude_foods: Optional[List[str]] = Field(default=None, description="排除的食物名（过敏/禁忌）")


class PlanNutrientsRequest(BaseModel):
    plan: dict = Field(description="generate_meal_plan 返回的 plan 对象")
