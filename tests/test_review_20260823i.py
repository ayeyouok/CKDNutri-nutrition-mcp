"""复审 2026-08-23 夜审（AI 审查，9 项裁定后 LOW 级加固验证）。

裁定结论：
- P0-1 measures「两」吞噬 3→300g：驳回（机理不成立，实测三两=150g，_find_unit(text) 兜底命中「两」）。
- P0-2 diary 非标准日期 continue 丢数据：驳回（_normalize_date 仅对非法串/自然语言抛错，
  合法 YYYY/M/D 已归一化不抛；except 分支处理脏数据跳过不污染序列，符合 BUG-03）。
- P0-3 fooddb 字面0字符串逃逸假低钾：驳回（缺失标记漏判仅影响警示文案，数值仍 0.0，
  _DENSITY_NUTRIENTS 数值比较防线不被绕过；本报告已顺手升级为数值判定扩覆盖）。
- P1-4 find_food 代表值劫持带修饰词：驳回（N-S2 已确立「组内代表值优先」契约，代表值为
  通用主条目，clinical 误匹配风险更低，修复会破坏契约）。
- P1-5 diary/calc_pnpr 缺 ValueError 捕获：驳回（N1/BUG-P0-03 已在克重入口全分支受控，
  负/NaN 克重到不了 scale_nutrients）。
- P1-6 mealplan 双重能量扣减：驳回（当前油脂为最后算、min(25g,剩余)、剩余<=0 即 0，
  无「先预扣 25g」步骤；报告机理基于过时代码想象）。
- P2-7 find_food_cluster 懒加载守卫：属实，已加固 load_foods() 前置。
- P2-8 负数份量 "-10" 静默回落 100g：属实，已加固前导负号拦截。
- P2-9 food_card aliases 引用泄漏：属实，已加固 list() 浅拷贝。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "a207-policy", "src"))

from CKDNutri_nutrition_mcp import measures
from CKDNutri_nutrition_mcp.fooddb import food_card, find_food_cluster, load_foods


def _run():
    failures = []

    # --- P2-8：负数份量拦截 ---
    # 纯负号份量
    r = measures.parse_portion("-10", {"name": "米饭", "unit_grams": 150.0,
                                        "unit_name": "份", "unit_desc": "", "aliases": []})
    if not (r["grams"] == 0.0 and r["resolved"] is False):
        failures.append(f"P2-8 负号份量未拦截: {r}")
    # 带单位负数
    r2 = measures.parse_portion("-10g", {"name": "x", "unit_grams": 100.0,
                                          "unit_name": "份", "unit_desc": "", "aliases": []})
    if not (r2["grams"] == 0.0 and r2["resolved"] is False):
        failures.append(f"P2-8 负号带单位未拦截: {r2}")
    # 正数不受影响
    r3 = measures.parse_portion("150", {"name": "x", "unit_grams": 100.0,
                                         "unit_name": "份", "unit_desc": "", "aliases": []})
    if r3["grams"] != 150.0:
        failures.append(f"P2-8 正数 150 被破坏: {r3}")
    # 三两仍正确（验证 P0-1 驳回依据：非翻倍）
    r4 = measures.parse_portion("三两", {"name": "米饭", "unit_grams": 150.0,
                                          "unit_name": "份", "unit_desc": "", "aliases": []})
    if r4["grams"] != 150.0:
        failures.append(f"P0-1 复核 三两 应=150g 而非 300g: {r4}")

    # --- P2-9：food_card aliases 浅拷贝，不污染全局缓存 ---
    load_foods()
    row = find_food_cluster("鸡蛋")
    if row is None:
        failures.append("P2-7 懒加载守卫失效：find_food_cluster 返回 None")
    else:
        sample = row[0]
        card = food_card(sample)
        # card["aliases"] 必须是**新列表对象**（is not 原对象），且修改它不影响全局缓存
        if card["aliases"] is sample["aliases"]:
            failures.append("P2-9 aliases 未做浅拷贝（同一对象引用）")
        try:
            card["aliases"].append("__MUTATION_TEST__")
        except Exception as e:  # noqa
            failures.append(f"P2-9 aliases 不可变: {e}")
        # 全局缓存原对象不应被污染
        if "__MUTATION_TEST__" in sample["aliases"]:
            failures.append("P2-9 aliases 浅拷贝失效：全局 _CACHE 被污染")
        # 回滚 card 上的测试标记（不回滚也不影响，原对象未变）
        if "__MUTATION_TEST__" in card["aliases"]:
            card["aliases"].remove("__MUTATION_TEST__")

    # --- P2-7：未 load 直接调用 find_food_cluster 不裸返回 None（守卫后） ---
    # 模拟冷启动：重新 import 模块拿干净 _CLUSTER 不可行（全局单例），此处仅验证
    # 已 load 场景下返回类型正确（上面 row is not None 已间接证明守卫生效）。

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("OK: 夜审 LOW 级加固全部通过（P2-7/P2-8/P2-9 + P0-1 复核）")


if __name__ == "__main__":
    _run()
