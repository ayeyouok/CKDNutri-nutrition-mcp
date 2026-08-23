"""参考表与阈值常量。

单位口径（与 contracts/pcp-schema.json 一致）：
  能量 kcal / 蛋白 g / 体重 kg / 身高 cm / 电解质 mg（食物）或 mmol/L（血液）。

PRNT 2020 出处：Shaw V, Polderman N, Renken-Terhaerdt J, et al.
Energy and protein requirements for children with CKD stages 2-5 and on dialysis.
Pediatr Nephrol. 2020;35(3):519-531.
"""

# 本包在 a207_policy 权限矩阵中的登记名（方案 A：闸门按此查表，不在包内判角色）
MCP_NAME = "CKDNutri-nutrition-mcp"

GUIDELINE = "PRNT 2020"
GUIDELINE_REF = (
    "Shaw V, et al. Energy and protein requirements for children with CKD "
    "stages 2-5 and on dialysis - clinical practice recommendations from the "
    "Pediatric Renal Nutrition Taskforce. Pediatr Nephrol 2020;35:519-531."
)
FOOD_TABLE_REF = "中国食物成分表（第6版）代表值，每 100 g 可食部；数值经四舍五入"

# --- PRNT 能量/蛋白 SDI 锚点 -------------------------------------------
# BUG-32（2026-08-12）：原 SDI_ANCHORS 为死代码（全仓库无引用），且 3/12 月龄数值
# 与 core._PRNT_BANDS（实际计算引擎）不一致，删除以避免双轨漂移；计算以 core 为准。

# --- 分期/透析别名（单一事实源；透析蛋白额外补充在 core._DIALYSIS_EXTRA）-----
DIALYSIS_ALIAS = {
    "none": "none", "非透析": "none", "no": "none", "": "none", "nd": "none",
    "hd": "hemodialysis", "hemodialysis": "hemodialysis", "血透": "hemodialysis",
    "血液透析": "hemodialysis",
    "pd": "peritoneal", "peritoneal": "peritoneal", "腹透": "peritoneal",
    "腹膜透析": "peritoneal", "capd": "peritoneal", "apd": "peritoneal",
}
DIALYSIS_LABEL = {"none": "非透析", "hemodialysis": "血液透析", "peritoneal": "腹膜透析"}

# --- Schofield 静息能量消耗方程 -------------------------------------------
# #2 修复（2026-08-15）：删除残留的 SCHOFIELD 死代码——此表是 MED-1 修正前的
# 错误系数（(M,18) 段 0.071/2.132/-1.184 仍是错值），且全仓库零引用（实际计算
# 引擎在 core.py:171 的 SCHOFIELD，已修正为 0.068/0.574/2.157）。双源漂移隐患：
# 未来有人 import constants.SCHOFIELD 就会拿到错系数。删除防误用。
PAL_DEFAULT = 1.4          # CKD 患儿常见轻体力活动系数
KJ_PER_KCAL = 4.184

# --- BMI 第 50 百分位参考（用于水肿时估算干体重）---------------------------
BMI_P50 = {
    2: (16.4, 16.1), 3: (16.0, 15.7), 4: (15.8, 15.4), 5: (15.5, 15.3),
    6: (15.5, 15.3), 7: (15.7, 15.5), 8: (16.0, 15.9), 9: (16.4, 16.4),
    10: (16.9, 16.9), 11: (17.4, 17.5), 12: (18.0, 18.2), 13: (18.6, 18.9),
    14: (19.3, 19.6), 15: (19.9, 20.2), 16: (20.5, 20.7), 17: (21.1, 21.1),
    18: (21.7, 21.4),
}

# --- 腹膜透析葡萄糖吸收 -----------------------------------------------------
GLUCOSE_KCAL_PER_G = 3.4   # 葡萄糖一水合物（腹透液用糖）
# P2-8（2026-08-23 审查）：新增 (0.0, 0.0) 零点锚点——APD 短留腹（儿科 APEX
# 18~71min，典型 45min）葡萄糖吸收显著低于长留腹（"the shorter the exchange, the
# lesser the glucose absorption"）。原锚点起点 (1.0, 0.30) 使任何 <=1h 留腹硬返回
# 0.30，高估短留腹吸收（0.5h 实际远低于 30%）。加零点后改为从 0 起近线性插值，
# 与临床相符。其余锚点数值维持原 PRNT 设定不变。
PD_ABSORB_ANCHORS = ((0.0, 0.0), (1.0, 0.30), (2.0, 0.38), (4.0, 0.55),
                     (6.0, 0.65), (8.0, 0.72), (12.0, 0.80))
PD_TRANSPORT_FACTOR = {"high": 1.15, "high_average": 1.05, "average": 1.0,
                       "low_average": 0.92, "low": 0.85}
PD_GLUCOSE_KCAL_PER_KG_REF = (7.5, 9.08)  # PRNT 引用的日吸收参考区间

# --- 食物电解质分级阈值（mg / 100 g 可食部）--------------------------------
K_LEVELS = ((150.0, "low", "低钾"), (250.0, "medium", "中等钾"),
            (400.0, "high", "高钾"), (float("inf"), "very_high", "极高钾"))
P_LEVELS = ((80.0, "low", "低磷"), (160.0, "medium", "中等磷"),
            (300.0, "high", "高磷"), (float("inf"), "very_high", "极高磷"))
NA_HIGH_MG_PER_100G = 400.0
# 磷蛋白比 mg P / g 蛋白：肾病选食核心指标
PNPR_LEVELS = ((12.0, "preferred", "优选"), (16.0, "acceptable", "可接受"),
               (float("inf"), "caution", "慎选"))

# --- 烹调处理对电解质的保留系数 ---------------------------------------------
# P3（2026-08-23 审查）：COOKING_LOSS 的 factor 实为**保留率（retention rate）**而非
# 损失率——如 blanch 的 potassium_mg=0.50 表示"焯水后钾保留 50%"（丢 50%），下游
# scale_nutrients 用 raw * factor 折算。命名易望文生义写成 raw * (1 - factor) 导致逻辑
# 颠倒，特显式标注：factor = 保留率，直接使用（勿 1−factor）。
COOKING_LOSS = {
    "raw": {"factor": {"potassium_mg": 1.0, "phosphorus_mg": 1.0, "sodium_mg": 1.0},
            "label": "不做处理（生重/原值）"},
    # factor = retention_rate（保留率，直接使用，勿写成 1−factor）
    "blanch": {"factor": {"potassium_mg": 0.50, "phosphorus_mg": 0.80, "sodium_mg": 0.90},
               "label": "焯水后弃汤（适用叶菜）"},
    "boil_discard": {"factor": {"potassium_mg": 0.40, "phosphorus_mg": 0.75, "sodium_mg": 0.85},
                     "label": "切小块水煮后弃汤（适用薯类/根茎）"},
    "soak": {"factor": {"potassium_mg": 0.70, "phosphorus_mg": 0.90, "sodium_mg": 0.95},
             "label": "切块冷水浸泡 2 小时并换水"},
    "boil_meat": {"factor": {"potassium_mg": 0.70, "phosphorus_mg": 0.90, "sodium_mg": 0.90},
                  "label": "肉类水煮后弃汤"},
}
COOKING_ALIAS = {"生": "raw", "不处理": "raw", "焯水": "blanch", "焯": "blanch",
                 "水煮弃汤": "boil_discard", "水煮": "boil_discard",
                 "浸泡": "soak", "泡水": "soak", "煮肉弃汤": "boil_meat"}

DEKALIUM_TIPS = {
    "叶菜": "叶菜先焯水 3-5 分钟并弃掉焯水，再下锅炒，钾可去掉约一半。",
    "薯类": "薯类去皮切小块，冷水浸泡 2 小时换水，再水煮弃汤，钾可去掉约六成。",
    "根茎": "根茎类切薄片浸泡后水煮弃汤，避免连汤食用。",
    "菌藻": "干菌藻类泡发后弃掉泡发水，不要用泡发水煮汤。",
    "水果": "高钾水果控制单次分量，不喝果汁与果泥浓缩制品（同样重量钾更集中）。",
    "畜肉": "肉类先水煮弃汤再烹调，可去掉部分钾；不喝浓汤与肉汁。",
    "default": "汤汁中钾磷钠含量高，吃菜不喝汤是最简单有效的一步。",
}
