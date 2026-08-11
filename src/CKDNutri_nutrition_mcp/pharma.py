# -*- coding: utf-8 -*-
"""药物-营养素交互查询（儿童 CKD 常用药）。"""
from __future__ import annotations

from typing import Any

from a207_policy import enforce_read, get_caller

from .constants import MCP_NAME

NUTRIENT_ALIAS = {
    "磷": "phosphorus", "p": "phosphorus", "phosphorus": "phosphorus", "血磷": "phosphorus",
    "钾": "potassium", "k": "potassium", "potassium": "potassium", "血钾": "potassium",
    "钙": "calcium", "ca": "calcium", "calcium": "calcium", "血钙": "calcium",
    "钠": "sodium", "na": "sodium", "sodium": "sodium", "盐": "sodium",
    "蛋白": "protein", "蛋白质": "protein", "protein": "protein",
    "能量": "energy", "热量": "energy", "energy": "energy",
    "铁": "iron", "iron": "iron", "fe": "iron",
    "镁": "magnesium", "magnesium": "magnesium", "mg": "magnesium",
    "维生素d": "vitamin_d", "vitamin_d": "vitamin_d", "vd": "vitamin_d", "维d": "vitamin_d",
    "脂溶性维生素": "vitamin_d", "液体": "fluid", "水": "fluid", "fluid": "fluid",
}
NUTRIENT_LABEL = {"phosphorus": "磷", "potassium": "钾", "calcium": "钙", "sodium": "钠",
                  "protein": "蛋白质", "energy": "能量", "iron": "铁", "magnesium": "镁",
                  "vitamin_d": "维生素 D / 脂溶性维生素", "fluid": "液体"}

DRUGS: dict[str, dict[str, Any]] = {
    "碳酸钙": {"aliases": ["钙尔奇", "calcium carbonate", "含钙磷结合剂", "醋酸钙"],
             "drug_class": "含钙磷结合剂",
             "effects": {
                 "phosphorus": ("随餐嚼服才能与食物中的磷结合；空腹或餐后补服基本无效。",
                                "monitor: 血磷", "high"),
                 "calcium": ("同时带来钙负荷，与活性维生素D合用时高钙血症与血管钙化风险上升。",
                             "monitor: 血钙、钙磷乘积、PTH", "high"),
                 "iron": ("与口服铁剂同服会互相干扰吸收，两者至少间隔 2 小时。",
                          "monitor: 血红蛋白、铁蛋白", "medium")}},
    "司维拉姆": {"aliases": ["sevelamer", "carbonate sevelamer", "碳酸司维拉姆", "非钙磷结合剂"],
              "drug_class": "非含钙磷结合剂",
              "effects": {
                  "phosphorus": ("须随每餐第一口食物服用，按餐中磷含量调整剂量。",
                                 "monitor: 血磷", "high"),
                  "vitamin_d": ("可轻度降低脂溶性维生素（A/D/E/K）吸收，长期使用需评估补充。",
                                "monitor: 25-OH-VD、凝血功能", "medium"),
                  "calcium": ("不增加钙负荷，是高钙血症患儿优于含钙结合剂的选择。",
                              "monitor: 血钙", "low")}},
    "碳酸镧": {"aliases": ["lanthanum", "福斯利诺"],
             "drug_class": "非含钙磷结合剂",
             "effects": {
                 "phosphorus": ("餐中或餐后即刻嚼碎服用，整片吞服显著降低结合效率。",
                                "monitor: 血磷", "high"),
                 "magnesium": ("长期用药需关注胃肠道反应与矿物质吸收变化。",
                               "monitor: 血镁、消化道症状", "low")}},
    "聚苯乙烯磺酸钙": {"aliases": ["降钾树脂", "聚苯乙烯磺酸钠", "kayexalate", "钾结合剂"],
                "drug_class": "阳离子交换降钾树脂",
                "effects": {
                    "potassium": ("在肠道交换钾离子，起效慢，不能替代急性高钾的急救处理。",
                                  "monitor: 血钾", "high"),
                    "calcium": ("钙型树脂释放钙离子，与活性维生素D合用注意高钙。",
                                "monitor: 血钙", "medium"),
                    "sodium": ("钠型树脂带来额外钠负荷，限钠与水肿患儿慎用。",
                               "monitor: 血钠、血压、水肿", "high")}},
    "环硅酸锆钠": {"aliases": ["szc", "锆硅酸钠", "新型钾结合剂"],
              "drug_class": "选择性钾结合剂",
              "effects": {
                  "potassium": ("与含钾食物同日使用时仍须限钾饮食，不可因用药放开高钾食物。",
                                "monitor: 血钾", "high"),
                  "sodium": ("制剂含钠，水肿、高血压或限钠患儿需计入全天钠。",
                             "monitor: 血压、体重、水肿", "medium"),
                  "fluid": ("与其他口服药间隔至少 2 小时，避免影响吸收。",
                            "monitor: 合并用药清单", "medium")}},
    "骨化三醇": {"aliases": ["calcitriol", "阿法骨化醇", "活性维生素d", "罗盖全"],
             "drug_class": "活性维生素 D",
             "effects": {
                 "calcium": ("显著增加肠道钙吸收，与含钙磷结合剂叠加时易高钙。",
                             "monitor: 血钙、PTH、钙磷乘积", "high"),
                 "phosphorus": ("同时增加磷吸收，血磷未控制前不宜加量。",
                                "monitor: 血磷", "high"),
                 "vitamin_d": ("与营养性维生素D补充是两回事，不可相互替代。",
                               "monitor: 25-OH-VD、1,25-(OH)2-VD", "medium")}},
    "呋塞米": {"aliases": ["速尿", "furosemide", "袢利尿剂", "托拉塞米"],
            "drug_class": "袢利尿剂",
            "effects": {
                "potassium": ("促进排钾，可致低钾；限钾饮食叠加利尿剂时需重新评估钾摄入。",
                              "monitor: 血钾", "high"),
                "sodium": ("排钠为治疗目的，但过度限钠加大剂量利尿易致低钠与低血容量。",
                           "monitor: 血钠、体重、血压", "high"),
                "calcium": ("增加尿钙排泄，长期使用关注骨代谢与肾钙质沉着。",
                            "monitor: 尿钙/肌酐比、超声", "medium"),
                "fluid": ("与限液方案需协同，不可只限液不复核尿量。",
                          "monitor: 出入量、体重", "high")}},
    "螺内酯": {"aliases": ["安体舒通", "spironolactone", "保钾利尿剂"],
            "drug_class": "保钾利尿剂",
            "effects": {
                "potassium": ("保钾作用叠加 CKD 排钾能力下降，高钾风险显著上升；"
                              "禁止同时使用钾盐替代盐（低钠盐多为氯化钾）。",
                              "monitor: 血钾、心电图", "high")}},
    "泼尼松": {"aliases": ["强的松", "prednisone", "甲泼尼龙", "糖皮质激素", "激素"],
            "drug_class": "糖皮质激素",
            "effects": {
                "sodium": ("水钠潴留致水肿与血压升高，用药期间须严格限钠。",
                           "monitor: 血压、体重、水肿", "high"),
                "potassium": ("促进排钾，长期大剂量可致低钾。", "monitor: 血钾", "medium"),
                "calcium": ("减少钙吸收并增加骨丢失，需保证钙与维生素D。",
                            "monitor: 血钙、骨密度", "high"),
                "protein": ("促进蛋白分解，蛋白需求上升，此时下调蛋白会加重负氮平衡。",
                            "monitor: 白蛋白、BUN、生长速率", "high"),
                "energy": ("食欲显著增加易致过量进食与肥胖，需主动管理能量与零食。",
                           "monitor: 体重、BMI、血糖", "high")}},
    "他克莫司": {"aliases": ["tacrolimus", "fk506", "环孢素", "普乐可复"],
             "drug_class": "钙调磷酸酶抑制剂",
             "effects": {
                 "potassium": ("常见药物性高钾，限钾饮食须同步收紧。",
                               "monitor: 血钾、药物浓度", "high"),
                 "magnesium": ("肾性失镁常见，低镁需膳食或药物补充。",
                               "monitor: 血镁", "medium"),
                 "fluid": ("柚子/西柚及其果汁抑制代谢酶，会显著抬高血药浓度，务必禁食。",
                           "monitor: 药物浓度", "high")}},
    "碳酸氢钠": {"aliases": ["小苏打", "sodium bicarbonate", "碱片"],
             "drug_class": "碱剂",
             "effects": {
                 "sodium": ("每片带来可观钠负荷，限钠患儿需把药源钠计入全天总钠。",
                            "monitor: 血压、水肿、血钠", "high"),
                 "protein": ("纠正代谢性酸中毒可减少蛋白分解，有利生长，不应因纠酸而减蛋白。",
                             "monitor: 血碳酸氢盐、身高速率", "medium")}},
    "琥珀酸亚铁": {"aliases": ["铁剂", "多糖铁复合物", "硫酸亚铁", "iron"],
              "drug_class": "口服铁剂",
              "effects": {
                  "iron": ("空腹或餐前 1 小时吸收最好；与维生素C同服可提高吸收。",
                           "monitor: 血红蛋白、铁蛋白、转铁蛋白饱和度", "high"),
                  "calcium": ("与牛奶、含钙磷结合剂同服显著降低铁吸收，间隔至少 2 小时。",
                              "monitor: 服药时间表", "high"),
                  "phosphorus": ("部分磷结合剂与铁剂互相干扰吸收，需错开服用时间。",
                                 "monitor: 血磷、铁指标", "medium")}},
    "重组人生长激素": {"aliases": ["生长激素", "rhgh", "growth hormone"],
                "drug_class": "生长激素",
                "effects": {
                    "protein": ("促进合成代谢，蛋白与能量供给不足时疗效打折，"
                                "用药期间须先确保达到 SDI 目标。",
                                "monitor: 身高速率、IGF-1、白蛋白", "high"),
                    "energy": ("能量摄入不足会抵消治疗效果，需同步评估摄入达成率。",
                               "monitor: 能量达成率、体重", "high")}},
    "依那普利": {"aliases": ["acei", "arb", "氯沙坦", "缬沙坦", "培哚普利", "普利", "沙坦"],
             "drug_class": "RAAS 抑制剂",
             "effects": {
                 "potassium": ("抑制醛固酮致血钾升高，与限钾饮食、保钾利尿剂叠加风险更高。",
                               "monitor: 血钾、肌酐", "high"),
                 "protein": ("用于降蛋白尿，与限蛋白饮食是两条独立路径，不可互相替代。",
                             "monitor: 尿蛋白/肌酐比", "medium")}},
}


def _resolve_drug(drug: str) -> tuple[str, dict[str, Any]] | None:
    text = str(drug or "").strip().lower()
    if not text:
        return None
    for name, info in DRUGS.items():
        if text == name.lower():
            return name, info
    for name, info in DRUGS.items():
        keys = [name.lower()] + [alias.lower() for alias in info["aliases"]]
        if any(key in text or text in key for key in keys):
            return name, info
    return None


def check_drug_nutrient_interaction(drug: str, nutrient: str | None = None) -> dict[str, Any]:
    """查询某药物与某营养素的交互；nutrient 留空则返回该药全部交互。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    hit = _resolve_drug(drug)
    if hit is None:
        return {"ok": False, "error": "DRUG_NOT_FOUND",
                "detail": f"未收录药物「{drug}」，当前覆盖：{'、'.join(DRUGS)}",
                "supported_drugs": list(DRUGS)}
    name, info = hit

    key = NUTRIENT_ALIAS.get(str(nutrient or "").strip().lower()) if nutrient else None
    if nutrient and key is None:
        return {"ok": False, "error": "NUTRIENT_NOT_SUPPORTED",
                "detail": f"未识别营养素「{nutrient}」，可用：{'、'.join(NUTRIENT_LABEL.values())}",
                "supported_nutrients": list(NUTRIENT_LABEL.values())}

    selected = [key] if key else list(info["effects"])
    interactions = []
    for item in selected:
        effect = info["effects"].get(item)
        if not effect:
            continue
        advice, monitor, severity = effect
        interactions.append({"nutrient": item, "nutrient_label": NUTRIENT_LABEL[item],
                             "advice": advice, "monitoring": monitor.replace("monitor: ", ""),
                             "severity": severity})

    data = {"drug": name, "matched_input": drug, "drug_class": info["drug_class"],
            "interactions": interactions,
            "all_nutrients_covered": [NUTRIENT_LABEL[item] for item in info["effects"]],
            "note": "本表为膳食-用药协同提示，用于食谱与宣教；剂量与用法调整由主诊医师决定。"}
    if key and not interactions:
        data["message"] = (f"未收录「{name}」与「{NUTRIENT_LABEL[key]}」的直接交互，"
                           f"该药已收录的相关营养素为：{'、'.join(data['all_nutrients_covered'])}。")
    return {"ok": True, "data": data}
