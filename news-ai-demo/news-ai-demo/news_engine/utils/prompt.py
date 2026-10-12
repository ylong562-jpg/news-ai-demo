
"""
prompt.py — Prompt 库（演示删减版）

【重要说明】
本文件为求职演示版：所有 prompt 的**指令正文**（维度定义、判定规则、
边界处理、负例约束、few-shot 示例、术语词表注入等经过多轮调优的内容）
均已删减为占位说明；保留的是：

- 全部对外名称与函数签名（其余模块 import 不受影响）
- 标签体系 / 分类映射 / 默认 JSON 结构等业务数据结构
- 输出格式契约（JSON 字段名、输出模板标记）

真实接入时，只需在本文件各占位处补回完整指令，流水线其余代码零改动。
"""

import sys
import os

# 确保当前目录在 Python 路径中（支持 PyInstaller 打包环境）
if getattr(sys, 'frozen', False):
    current_dir = os.path.dirname(sys.executable)
    internal_dir = os.path.join(current_dir, '_internal')
    if os.path.exists(internal_dir):
        sys.path.insert(0, internal_dir)
    sys.path.insert(0, current_dir)
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ============================================================
# 通用格式约束后缀（结构示意，细则已删减）
# ============================================================
STRICT_JSON_SUFFIX = (
    "【格式强制要求】你必须且只能返回一个合法的JSON对象，不要添加任何解释、前言、后缀、markdown代码块标记。"
    "键和字符串值必须使用英文双引号；无数据字段使用 null。严格遵守此格式要求。"
    "（细则已删减）"
)

SHIPYARD_NAME_TRANSLATION_HINT = (
    "【船厂名称处理】船厂字段优先输出中文标准名，其它主体名称按原文输出。（正文已删减）"
)

SHIP_TYPE_NO_HAIGONG_HINT = (
    "【船型名称强制规范】船型字段必须使用规范中文标准名，禁止已废弃的分类名。（正文已删减）"
)

ENTITY_VOCABULARY_HINT = (
    "【实体名称强制规范】输出实体名称时优先使用系统标准名称（canonical_name）。（正文已删减）"
)

ENTITY_SPECIFICITY_HINT = (
    "【实体宽泛/具体规则】区分具体实体与宽泛实体；主体不明确时使用宽泛占位，不要编造具体名称。（正文已删减）"
)

TRANSACTION_REASON_HINT = (
    "【交易划分说明】请在返回的JSON中增加 \"transaction_reason\" 字段，简要说明交易划分依据。（正文已删减）"
)

# ==================== 新主维度体系 ====================
# 主维度不作为 category 输入，而是作为 tag 维度 "主维度" 中的标签。
MAIN_DIMENSIONS = [
    "新造船订单",
    "船舶建造",
    "公司金融",
    "买卖船",
    "租船",
    "维修改装",
    "行业新闻",
]

# 只能出现在 main_tags 中的核心维度，绝不能降级到 secondary_tags
MAIN_ONLY_DIMENSIONS = {"新造船订单", "租船", "买卖船", "船舶建造", "公司金融"}

VALID_CATEGORIES = MAIN_DIMENSIONS


# 旧分类 -> 新主维度的兼容映射（用于历史数据迁移）
LEGACY_CATEGORY_TO_MAIN_DIMENSION = {
    "新造船订单": "新造船订单",
    "租船购船": "行业新闻",  # 旧分类无法区分买卖船/租船，保守兜底
    "船只交付": "船舶建造",
    "船舶建造": "船舶建造",
    "地缘政治与冲突": "行业新闻",
    "国际局势": "行业新闻",
    "维修改装": "维修改装",
    "船厂生产节点": "船舶建造",
    "生产节点": "船舶建造",
    "公司金融": "公司金融",
    "行业综合动态": "行业新闻",
    "行业新闻": "行业新闻",
    "其它": "行业新闻",
    # 以下旧主维度降级为次标签，历史数据映射到兜底主维度
    "企业合作与投资": "行业新闻",
    "运河航道运营": "行业新闻",
    "船舶规范与认证": "行业新闻",
    "航运市场分析": "行业新闻",
    "绿色航运": "行业新闻",
    "行业事故": "行业新闻",
    "人事调动": "行业新闻",
    "政策法规": "行业新闻",
    "企业日常动态": "行业新闻",
    "技术突破": "行业新闻",
    "行业会议/活动": "行业新闻",
}


def normalize_category(raw_category):
    """规范化分类输出为 VALID_CATEGORIES 中的严格一个。
    只保留精确匹配和旧分类兼容映射；不再使用关键词兜底或模糊匹配。
    若仍无法识别，统一归入兜底分类"行业新闻"。
    """
    if not raw_category:
        return "行业新闻"
    text = str(raw_category).strip().strip("'\".。，,、")
    if text in VALID_CATEGORIES:
        return text
    # 兼容旧分类到新主维度
    if text in LEGACY_CATEGORY_TO_MAIN_DIMENSION:
        return LEGACY_CATEGORY_TO_MAIN_DIMENSION[text]
    # 不再使用子串/关键词兜底，统一归入兜底分类
    return "行业新闻"


# ==================== 分类提示词（正文已删减） ====================
category_prompt = (
    "【演示版：分类指令正文已删减】\n"
    "请将新闻严格分类到以下类别之一，输出必须是类别名称本身，不包含任何多余字符：\n"
    "【新造船订单，船舶建造，公司金融，买卖船，租船，维修改装，行业新闻】\n\n"
    "原指令包含：各类别的完整定义与决策流程、包含/排除规则、关键区分"
    "（SPV/公司股权、设备供应 vs 整船建造、意向 vs 签约等）、负例约束。\n\n"
    "请对提供的新闻内容进行分类。"
)


# ========== 候选分类精简规则（用于两步分类的第二步；正文已删减）==========
_CANDIDATE_RULES = {
    "新造船订单": "**新造船订单核心判断**：（正文已删减：交易标的判定、SPV/合同转让区分、订单汇总排除等）",
    "船舶建造": "**船舶建造核心判断**：（正文已删减：五类建造节点判定、交付语义、产能/数字化平台排除等）",
    "公司金融": "**公司金融核心判断**：（正文已删减：金融/资本事件范围、股权交易 vs 船舶交易区分等）",
    "买卖船": "**买卖船核心判断**：（正文已删减：商业所有权转移判定、非商业强制移交排除、主体/船型硬条件等）",
    "租船": "**租船核心判断**：（正文已删减：使用权租赁判定、主体/船型硬条件、非租赁事件排除等）",
    "维修改装": "**维修改装核心判断**：（正文已删减：工程改造/设备加装判定、对象为新造船或运营船等）",
    "行业新闻": "**行业新闻核心判断**：（正文已删减：兜底分类范围、订单汇总规则、地缘政治次标签规则等）",
}


# ==================== 实体提取提示词（每个分类；正文已删减） ====================
# 保留各 prompt 的字段结构契约（目标 JSON 的键名），指令正文删减。

# 1. 新造船订单
newbuilding_orders_prompt_entity = (
    "【任务：提取该主维度的核心交易主体（entity）】\n"
    "目标结构：单字典 {{\"shipowner\": ..., \"shipyard\": ...}}；多笔交易为字典数组。"
    "shipowner=订船方/船东，shipyard=造船方/船厂。\n"
    "（正文已删减：信息存在性检查、多交易拆分/合并规则、主体过滤、名称翻译规范、"
    "强制校验、few-shot 示例等）\n"
    + SHIPYARD_NAME_TRANSLATION_HINT + ENTITY_VOCABULARY_HINT + ENTITY_SPECIFICITY_HINT + TRANSACTION_REASON_HINT + STRICT_JSON_SUFFIX
)

# 2. 买卖船
ship_sale_prompt_entity = (
    "【任务：提取该主维度的核心交易主体（entity）】\n"
    "目标结构：{{\"buyer\": ..., \"seller\": ...}}，多笔交易为字典数组。\n"
    "（正文已删减：存在性检查、交易拆分/合并规则、名称规范等）\n"
    + ENTITY_VOCABULARY_HINT + ENTITY_SPECIFICITY_HINT + TRANSACTION_REASON_HINT + STRICT_JSON_SUFFIX
)

# 3. 租船
charter_sale_prompt_entity = (
    "【任务：提取该主维度的核心交易主体（entity）】\n"
    "目标结构：{{\"charterer\": ..., \"owner\": ...}}，多笔交易为字典数组。\n"
    "（正文已删减：存在性检查、交易拆分/合并规则、名称规范等）\n"
    + ENTITY_VOCABULARY_HINT + ENTITY_SPECIFICITY_HINT + TRANSACTION_REASON_HINT + STRICT_JSON_SUFFIX
)

# 4. 船舶建造（含船只交付与生产节点）
ship_building_prompt_entity = (
    "【任务：提取该主维度的核心交易主体（entity）】\n"
    "目标结构：{{\"shipowner\": ..., \"shipyard\": ...}}（shipowner 可为订造方/接收方/项目业主），多事件为数组。\n"
    "（正文已删减：节点事件存在性检查、意向/口号排除、多事件拆分/合并规则、海工项目处理等）\n"
    + SHIPYARD_NAME_TRANSLATION_HINT + ENTITY_VOCABULARY_HINT + ENTITY_SPECIFICITY_HINT + TRANSACTION_REASON_HINT + STRICT_JSON_SUFFIX
)

# 旧别名兼容
ship_delivery_prompt_entity = ship_building_prompt_entity
shipyard_node_prompt_entity = ship_building_prompt_entity

# 5. 公司金融
company_finance_prompt_entity = (
    "【任务：提取该主维度的核心交易主体（entity）】\n"
    "目标结构：{{\"companies\": [...]}} 或 {{\"company\": ...}}。\n"
    "（正文已删减：主体范围、上市公司优先规则、名称规范等）\n"
    + ENTITY_VOCABULARY_HINT + ENTITY_SPECIFICITY_HINT + STRICT_JSON_SUFFIX
)

# 6. 维修改装
repair_conversion_prompt_entity = (
    "【任务：提取该主维度的核心交易主体（entity）】\n"
    "目标结构：{{\"shipowner\": ..., \"shipyard\": ...}}（shipyard 为承接工程的船厂/服务商）。\n"
    "（正文已删减：存在性检查、名称规范等）\n"
    + SHIPYARD_NAME_TRANSLATION_HINT + ENTITY_VOCABULARY_HINT + ENTITY_SPECIFICITY_HINT + TRANSACTION_REASON_HINT + STRICT_JSON_SUFFIX
)

# 7. 行业新闻（合并原行业综合动态与国际局势）
industry_news_prompt_entity = (
    "【任务：提取该主维度的核心交易主体（entity）】\n"
    "目标结构：{{\"entities\": [...], \"countries\": [...], \"organizations\": [...]}}。\n"
    "（正文已删减：实体范围、名称规范等）\n"
    + ENTITY_VOCABULARY_HINT + ENTITY_SPECIFICITY_HINT + STRICT_JSON_SUFFIX
)

# 8. 其它
others_prompt_entity = (
    "【任务：提取该主维度的核心交易主体（entity）】\n"
    "目标结构：{{\"entities\": [...]}}。\n（正文已删减）\n"
    + ENTITY_VOCABULARY_HINT + ENTITY_SPECIFICITY_HINT + STRICT_JSON_SUFFIX
)


# ==================== 数据提取提示词（每个分类；正文已删减） ====================
# 保留字段清单（下游解析契约），指令正文与示例删减。

# 1. 新造船订单
data_prompt_newbuilding_orders = (
    "【任务：提取该主维度的交易要素（data）】\n"
    "字段清单：trade_type（签署订单/备选生效/在手订单/预测订单）、date、shipowner、"
    "ship_type（规范中文标准名）、quantity、specs、price、delivery、power、shipyard、"
    "classification_society、flag、design_institute。\n"
    "单笔交易返回单字典；多笔交易返回 {{\"all_quantity\":..., \"all_price\":..., \"transactions\":[...]}}，"
    "transactions 数量与 entity 严格一一对应。\n"
    "（正文已删减：字段判定细则、日期格式规则、金额单位规范、多交易拆分/合并规则、正反示例等）\n"
    + SHIP_TYPE_NO_HAIGONG_HINT + TRANSACTION_REASON_HINT + STRICT_JSON_SUFFIX
)

# 1b. 新造船订单——周期性订单汇总/统计披露
data_prompt_newbuilding_order_summary = (
    "【任务：提取该主维度的交易要素（data）；订单汇总型报道专用】\n"
    "字段清单：summary_type（固定\"订单汇总\"）、source、period、all_quantity、all_price、"
    "region_summary、transactions（字段同上）。\n"
    "（正文已删减：汇总识别规则、明细提取原则、示例等）\n"
    + SHIP_TYPE_NO_HAIGONG_HINT + TRANSACTION_REASON_HINT + STRICT_JSON_SUFFIX
)

# 2. 买卖船
data_prompt_ship_sale = (
    "【任务：提取该主维度的交易要素（data）】\n"
    "字段清单：date、buyer、seller、ship_type、quantity、specs、money、delivery；"
    "多笔交易返回 transactions 数组。\n"
    "（正文已删减：字段判定细则、金额单位规范、拆分/合并规则等）\n"
    + SHIP_TYPE_NO_HAIGONG_HINT + TRANSACTION_REASON_HINT + STRICT_JSON_SUFFIX
)

# 3. 租船
data_prompt_charter_sale = (
    "【任务：提取该主维度的交易要素（data）】\n"
    "字段清单：date、charterer、owner、ship_type、quantity、specs、money（租金）、delivery、charter_type。\n"
    "（正文已删减：字段判定细则、租金单位规范、拆分/合并规则等）\n"
    + SHIP_TYPE_NO_HAIGONG_HINT + STRICT_JSON_SUFFIX
)

# 4. 船舶建造（含船只交付与生产节点）
data_prompt_ship_building = (
    "【任务：提取该主维度的交易要素（data）】\n"
    "字段清单：date、event_type（钢材切割/铺设龙骨/下水/试航/交付 五选一）、"
    "shipowner、shipyard、ship_type、specs、vessel_name。\n"
    "（正文已删减：节点语义映射规则、多事件拆分等）\n"
    + SHIP_TYPE_NO_HAIGONG_HINT + TRANSACTION_REASON_HINT + STRICT_JSON_SUFFIX
)

# 旧别名兼容
data_prompt_ship_delivery = data_prompt_ship_building
data_prompt_shipyard_node = data_prompt_ship_building

# 5. 公司金融
data_prompt_company_finance = (
    "【任务：提取该主维度的交易要素（data）】\n"
    "字段清单：event_type、amount、parties、period、metrics。\n"
    "（正文已删减：事件类型枚举、金额规范、示例等）\n"
    + STRICT_JSON_SUFFIX
)

# 6. 维修改装
data_prompt_repair_conversion = (
    "【任务：提取该主维度的交易要素（data）】\n"
    "字段清单：date、shipowner、ship_type、specs、modify_type、facility、vessel_name。\n"
    "（正文已删减：改装类型枚举、示例等）\n"
    + SHIP_TYPE_NO_HAIGONG_HINT + TRANSACTION_REASON_HINT + STRICT_JSON_SUFFIX
)

# 7. 行业新闻（合并原行业综合动态与国际局势）
data_prompt_industry_news = (
    "【任务：提取该主维度的交易要素（data）】\n"
    "字段清单：event_type、date、shipowner_related、shipyard_related、shipowners、shipyards、"
    "key_info、location、affected_routes、impact、metrics。\n"
    "（正文已删减：事件类型枚举、related 强制判定规则与正反例、示例等）\n"
    + STRICT_JSON_SUFFIX
)

# 8. 其它
data_prompt_others = (
    "【任务：提取该主维度的交易要素（data）】\n"
    "字段清单：event_type、date、subject、key_info、metrics。\n（正文已删减）\n"
    + STRICT_JSON_SUFFIX
)




VALID_TAG_DIMENSIONS = {
    "主维度": MAIN_DIMENSIONS,
    "区域+主体": [
        "东南亚海事机构",
        "东南亚港口",
        "东南亚能源企业",
        "东南亚航运企业",
        "东南亚船东",
        "东南亚船厂",
        "东南亚船级社",
        "东南亚金融机构",
        "中东海事机构",
        "中东港口",
        "中东能源企业",
        "中东航运企业",
        "中东船东",
        "中东船厂",
        "中东船级社",
        "中东金融机构",
        "中国海事机构",
        "中国港口",
        "中国能源企业",
        "中国航运企业",
        "中国船东",
        "中国船厂",
        "中国船级社",
        "中国金融机构",
        "俄罗斯海事机构",
        "俄罗斯港口",
        "俄罗斯能源企业",
        "俄罗斯航运企业",
        "俄罗斯船东",
        "俄罗斯船厂",
        "俄罗斯船级社",
        "俄罗斯金融机构",
        "北欧海事机构",
        "北欧港口",
        "北欧能源企业",
        "北欧航运企业",
        "北欧船东",
        "北欧船厂",
        "北欧船级社",
        "北欧金融机构",
        "北美海事机构",
        "北美港口",
        "北美能源企业",
        "北美航运企业",
        "北美船东",
        "北美船厂",
        "北美船级社",
        "北美金融机构",
        "南美海事机构",
        "南美港口",
        "南美能源企业",
        "南美航运企业",
        "南美船东",
        "南美船厂",
        "南美船级社",
        "南美金融机构",
        "印度海事机构",
        "印度港口",
        "印度能源企业",
        "印度航运企业",
        "印度船东",
        "印度船厂",
        "印度船级社",
        "印度金融机构",
        "大洋洲海事机构",
        "大洋洲港口",
        "大洋洲能源企业",
        "大洋洲航运企业",
        "大洋洲船东",
        "大洋洲船厂",
        "大洋洲船级社",
        "大洋洲金融机构",
        "希腊海事机构",
        "希腊港口",
        "希腊能源企业",
        "希腊航运企业",
        "希腊船东",
        "希腊船厂",
        "希腊船级社",
        "希腊金融机构",
        "新加坡海事机构",
        "新加坡港口",
        "新加坡能源企业",
        "新加坡航运企业",
        "新加坡船东",
        "新加坡船厂",
        "新加坡船级社",
        "新加坡金融机构",
        "日本海事机构",
        "日本港口",
        "日本能源企业",
        "日本航运企业",
        "日本船东",
        "日本船厂",
        "日本船级社",
        "日本金融机构",
        "欧洲海事机构",
        "欧洲港口",
        "欧洲能源企业",
        "欧洲航运企业",
        "欧洲船东",
        "欧洲船厂",
        "欧洲船级社",
        "欧洲金融机构",
        "美国海事机构",
        "美国港口",
        "美国能源企业",
        "美国航运企业",
        "美国船东",
        "美国船厂",
        "美国船级社",
        "美国金融机构",
        "阿联酋海事机构",
        "阿联酋港口",
        "阿联酋能源企业",
        "阿联酋航运企业",
        "阿联酋船东",
        "阿联酋船厂",
        "阿联酋船级社",
        "阿联酋金融机构",
        "非洲海事机构",
        "非洲港口",
        "非洲能源企业",
        "非洲航运企业",
        "非洲船东",
        "非洲船厂",
        "非洲船级社",
        "非洲金融机构",
        "韩国海事机构",
        "韩国港口",
        "韩国能源企业",
        "韩国航运企业",
        "韩国船东",
        "韩国船厂",
        "韩国船级社",
        "韩国金融机构"
    ],
    "船型": [
        "集装箱船",
        "散货船",
        "油轮",
        "液化气运输船",
        "汽车运输船",
        "滚装船",
        "多用途船",
        "特种船",
        "海工辅助船",
        "海工平台"
    ],
    "燃料/动力技术": [
        "LNG双燃料", "LPG双燃料", "燃油动力", "双燃料主辅机",
        "太阳能光伏", "轴带发电机", "锂电池电动车专用系统"
    ],
    "环保/脱碳": [
        "碳排放", "绿色航运", "低碳转型", "节能设计",
        "生活污水控制", "网络安全与绿色融合"
    ],
    "政策法规": [
        "制裁", "出口管制", "海事法规", "入级规范",
        "法定检验", "附加标志管理"
    ],
    "市场/商业活动": [
        "运费波动", "订单增补", "订单汇总", "战略合作"
    ],
    "次标签": [
        "国际局势", "企业合作与投资", "运河航道运营", "船舶规范与认证",
        "航运市场分析", "绿色航运", "行业事故", "人事调动", "政策法规",
        "企业日常动态", "技术突破", "行业会议/活动"
    ],
}

VALID_TAGS = []
for tags in VALID_TAG_DIMENSIONS.values():
    VALID_TAGS.extend(tags)


# 区域+主体维度拆分为区域与主体类型，便于提示词结构化呈现，减少 token 消耗
_REGION_ENTITY_REGIONS = [
    "中国", "韩国", "日本", "新加坡", "东南亚", "印度",
    "中东", "阿联酋", "欧洲", "希腊", "北欧",
    "北美", "美国", "南美", "大洋洲", "俄罗斯", "非洲"
]
_REGION_ENTITY_ENTITIES = [
    "船东", "船厂", "港口", "航运企业", "能源企业", "船级社", "金融机构", "海事机构"
]


def build_tag_prompt():
    """构建标签分类提示词（密度感知版）。演示版：维度清单保留，指令正文已删减。"""
    dimension_texts = []
    for dim_name, tags in VALID_TAG_DIMENSIONS.items():
        if dim_name == "区域+主体":
            # 结构化呈现：先列可选区域，再列可选主体类型，由模型自行组合为"区域+主体类型"标签
            dimension_texts.append(
                f"【区域+主体】由\"区域\"与\"主体类型\"组合而成，标签格式为\"区域+主体类型\"。\n"
                f"可选区域：{', '.join(_REGION_ENTITY_REGIONS)}\n"
                f"可选主体类型：{', '.join(_REGION_ENTITY_ENTITIES)}\n"
                "示例：中国船厂、欧洲船东、中东能源企业、新加坡航运企业、希腊船级社"
            )
        else:
            dimension_texts.append(f"【{dim_name}】{', '.join(tags)}")

    tags_text = "\n".join(dimension_texts)

    prompt = f"""【演示版：标签分类指令正文已删减】
你是一名资深航运新闻编辑。请根据给定的标签体系，为该新闻分配最相关标签。

标签体系（7个维度）：
{tags_text}

【核心原则】（正文已删减：主维度独立事实判定、密度评估 high/medium/low、
交易类维度共存规则、订单汇总强制规则、维度判定优先级、关键区分正反例等）

【输出格式】
必须且只能返回以下JSON格式，不要添加任何解释、前言、后缀或markdown代码块标记：
{{"main_tags": ["主维度1", "主维度2", "主维度3"], "secondary_tags": ["次标签1", "次标签2"], "information_density": {{"主维度1": "high/medium/low", "主维度2": "high/medium/low", "主维度3": "high/medium/low"}}, "classification_reason": "分类依据（50字以内，说明为何选择这些主维度/次标签）"}}

【格式强制要求】
- 必须返回合法JSON对象
- 键必须使用英文双引号
- 字符串值必须使用英文双引号
- 空数组使用 []
- 不要输出任何JSON之外的内容"""

    return prompt


tag_classification_prompt = build_tag_prompt()


# ==================== 分类 → Prompt 映射 ====================
# 核心内容提取只有2个prompt：entity(0), data(1)
category_prompt_groups = {
    "新造船订单": [
        newbuilding_orders_prompt_entity,
        data_prompt_newbuilding_orders,
    ],
    "船舶建造": [
        ship_building_prompt_entity,
        data_prompt_ship_building,
    ],
    "买卖船": [
        ship_sale_prompt_entity,
        data_prompt_ship_sale,
    ],
    "租船": [
        charter_sale_prompt_entity,
        data_prompt_charter_sale,
    ],
    "维修改装": [
        repair_conversion_prompt_entity,
        data_prompt_repair_conversion,
    ],
    "公司金融": [
        company_finance_prompt_entity,
        data_prompt_company_finance,
    ],
    "行业新闻": [
        industry_news_prompt_entity,
        data_prompt_industry_news,
    ],
}

# prompt索引映射：只有 entity 和 data
PROMPT_KEY_MAPPING = {
    0: "entity",
    1: "data",
}

# 哪些prompt索引要求必须返回JSON
JSON_REQUIRED_INDICES = {0, 1}


def get_prompts_by_category(category):
    """根据分类获取对应的prompt列表"""
    return category_prompt_groups.get(category, category_prompt_groups["行业新闻"])


# ==================== 默认JSON兜底结构 ====================
# 用于json_validator等模块在所有修复尝试失败时的安全兜底

def get_default_entity_for_category(category):
    """根据分类返回默认的entity JSON结构"""
    defaults = {
        "新造船订单": {"shipowner": None, "shipyard": None},
        "船舶建造": {"shipowner": None, "shipyard": None},
        "买卖船": {"buyer": None, "seller": None},
        "租船": {"charterer": None, "owner": None},
        "维修改装": {"shipowner": None, "shipyard": None},
        "公司金融": {"companies": []},
        "行业新闻": {"entities": [], "countries": [], "organizations": []},
    }
    import json
    return json.dumps(defaults.get(category, {"entities": []}), ensure_ascii=False)


def get_default_data_for_category(category):
    """根据分类返回默认的data JSON结构"""
    import json
    defaults = {
        "新造船订单": {
            "trade_type": None, "date": None, "shipowner": None, "ship_type": None, "quantity": None,
            "specs": None, "price": None, "delivery": None, "power": None,
            "shipyard": None, "classification_society": None, "flag": None, "design_institute": None
        },
        "船舶建造": {
            "date": None, "event_type": None, "shipowner": None, "shipyard": None,
            "ship_type": None, "specs": None, "vessel_name": None
        },
        "买卖船": {
            "date": None, "buyer": None, "seller": None, "ship_type": None,
            "quantity": None, "specs": None, "money": None, "delivery": None
        },
        "租船": {
            "date": None, "charterer": None, "owner": None, "ship_type": None,
            "quantity": None, "specs": None, "money": None, "delivery": None,
            "charter_type": None
        },
        "维修改装": {
            "date": None, "shipowner": None, "ship_type": None, "specs": None,
            "modify_type": None, "facility": None, "vessel_name": None
        },
        "公司金融": {
            "event_type": None, "amount": None, "parties": [],
            "period": None, "metrics": {}
        },
        "行业新闻": {
            "event_type": None, "date": None,
            "shipowner_related": False, "shipyard_related": False,
            "shipowners": [], "shipyards": [],
            "key_info": None, "location": None, "affected_routes": None,
            "impact": None, "metrics": {}
        },
    }
    return json.dumps(defaults.get(category, {}), ensure_ascii=False)


def get_default_json_for_field(category, key_name):
    """
    根据分类和字段名，返回默认的合法JSON结构。
    保持与旧接口兼容（移除subcategory参数）。
    """
    import json
    if key_name == "entity":
        return get_default_entity_for_category(category)
    if key_name == "data":
        return get_default_data_for_category(category)
    return json.dumps({}, ensure_ascii=False)


# ==================== 格式校验辅助函数（完整保留） ====================

def validate_category_output(raw_output):
    """
    校验分类输出是否为合法分类名称
    返回规范化后的分类名称
    """
    return normalize_category(raw_output)


def filter_low_confidence_tags(validated_tags_json: str) -> str:
    """
    后置过滤：将 information_density 为 low 的主维度移出 main_tags，
    避免低密度、被高优先级维度覆盖的次要主维度生成独立报道。
    核心维度（新造船订单、租船、买卖船、船舶建造、公司金融）只能属于 main_tags，
    若被移出则直接删除，不得降级到 secondary_tags。
    如果过滤后没有主维度，则降级为行业新闻。

    特别保护：交易类主维度（新造船订单、买卖船、租船）代表具体交易事件，
    同类交易主维度之间不互相覆盖；只要没有被更高优先级的主维度覆盖，
    即使 density 被评为 low，也应保留为 main_tag 并提升密度为 medium。
    """
    import json
    try:
        tags = json.loads(validated_tags_json)
    except Exception:
        return validated_tags_json

    if not isinstance(tags, dict):
        return validated_tags_json

    main_tags = tags.get("main_tags", [])
    secondary_tags = tags.get("secondary_tags", [])
    density = tags.get("information_density", {})

    if not isinstance(main_tags, list):
        main_tags = []
    if not isinstance(secondary_tags, list):
        secondary_tags = []
    if not isinstance(density, dict):
        density = {}

    # 主维度优先级：排在 MAIN_DIMENSIONS 中越靠前优先级越高
    priority = {tag: idx for idx, tag in enumerate(MAIN_DIMENSIONS)}
    TRANSACTION_MAIN_TAGS = {"新造船订单", "买卖船", "租船"}

    kept_main = []
    removed = []
    for tag in main_tags:
        tag_density = str(density.get(tag, "medium")).lower().strip()
        if tag_density == "low":
            tag_priority = priority.get(tag, 999)
            # 检查是否已被更高优先级的主维度覆盖
            # 特别规则：同类交易主维度之间不视为互相覆盖
            covered = any(
                priority.get(kept_tag, 999) < tag_priority
                and not (tag in TRANSACTION_MAIN_TAGS and kept_tag in TRANSACTION_MAIN_TAGS)
                for kept_tag in kept_main
            )
            # 交易类主维度：未被更高优先级主维度覆盖时，保留为 main_tag
            if tag in TRANSACTION_MAIN_TAGS and not covered:
                density[tag] = "medium"
                kept_main.append(tag)
            else:
                removed.append(tag)
        else:
            kept_main.append(tag)

    # 核心维度若被移出 main_tags，直接删除，不得加入 secondary_tags
    for tag in removed:
        if tag in MAIN_ONLY_DIMENSIONS:
            continue
        if tag not in secondary_tags:
            secondary_tags.append(tag)

    # 清理 secondary_tags 中可能出现的核心维度（兜底）
    secondary_tags = [tag for tag in secondary_tags if tag not in MAIN_ONLY_DIMENSIONS]

    # 强制规则：次标签含"订单汇总"时，说明该报道是周期性/统计性订单披露，
    # 主维度必须归【行业新闻】，不得在 main_tags 中保留【新造船订单】。
    if "订单汇总" in secondary_tags and "新造船订单" in kept_main:
        kept_main.remove("新造船订单")
        # 新造船订单属于核心维度，不得加入 secondary_tags
        if "行业新闻" not in kept_main:
            kept_main.append("行业新闻")
            density["行业新闻"] = density.get("行业新闻", "medium")

    # 历史兼容：若旧模型仍输出次标签"预测订单"，且主维度不含【新造船订单】或与【订单汇总】冲突，则移除。
    if "预测订单" in secondary_tags:
        if "新造船订单" not in kept_main or "订单汇总" in secondary_tags:
            secondary_tags.remove("预测订单")

    # 如果过滤后没有主维度，降级为行业新闻
    if not kept_main:
        kept_main = ["行业新闻"]
        density = {"行业新闻": "medium"}
    else:
        # 清理 density，只保留保留的主维度
        density = {tag: density.get(tag, "medium") for tag in kept_main}

    tags["main_tags"] = kept_main
    tags["secondary_tags"] = secondary_tags
    tags["information_density"] = density
    return json.dumps(tags, ensure_ascii=False)


def validate_tags_output(raw_output):
    """
    校验标签输出是否为合法JSON且包含main_tags和secondary_tags
    返回修正后的JSON字符串，失败返回默认空标签
    """
    import json
    try:
        # 尝试解析JSON
        data = None
        if isinstance(raw_output, str):
            # 尝试直接解析
            try:
                data = json.loads(raw_output.strip())
            except (json.JSONDecodeError, ValueError):
                pass

            # 尝试从文本中提取JSON
            if data is None:
                from json_validator import extract_json_from_text
                data = extract_json_from_text(raw_output)
        elif isinstance(raw_output, dict):
            data = raw_output

        if data is None:
            return json.dumps({"main_tags": ["行业新闻"], "secondary_tags": [], "information_density": {}}, ensure_ascii=False)

        # 确保有main_tags和secondary_tags
        main_tags = data.get("main_tags", []) if isinstance(data.get("main_tags"), list) else []
        secondary_tags = data.get("secondary_tags", []) if isinstance(data.get("secondary_tags"), list) else []
        information_density = data.get("information_density", {}) if isinstance(data.get("information_density"), dict) else {}
        # 分类依据为中间变量，不写入数据库字段，但保留在返回 JSON 中供后续解析
        classification_reason = data.get("classification_reason", "") if isinstance(data.get("classification_reason"), str) else ""

        # 过滤无效标签：主标签必须来自主维度列表
        # 被移到次标签的合法非主维度标签保留到 secondary_tags
        moved_to_secondary = [t for t in main_tags if t in VALID_TAGS and t not in MAIN_DIMENSIONS]
        main_tags = [t for t in main_tags if t in MAIN_DIMENSIONS]
        secondary_tags = [t for t in secondary_tags if t in VALID_TAGS and t not in main_tags]
        for tag in moved_to_secondary:
            if tag not in secondary_tags:
                secondary_tags.append(tag)

        # 不再限制主标签数量
        if not main_tags:
            main_tags = ["行业新闻"]

        # 清理 information_density，只保留 main_tags 中的键
        valid_density_values = {"high", "medium", "low"}
        cleaned_density = {}
        for tag in main_tags:
            density = str(information_density.get(tag, "medium")).lower().strip()
            if density not in valid_density_values:
                density = "medium"
            cleaned_density[tag] = density

        result = {
            "main_tags": main_tags,
            "secondary_tags": secondary_tags,
            "information_density": cleaned_density,
        }
        if classification_reason:
            result["classification_reason"] = classification_reason

        return json.dumps(result, ensure_ascii=False)
    except Exception:
        return json.dumps({"main_tags": ["行业新闻"], "secondary_tags": [], "information_density": {}}, ensure_ascii=False)


# ==================== 每日航运要闻 HTML 生成 Prompt（正文已删减） ====================
daily_news_generation_prompt = """【演示版：公众号 HTML 生成指令正文已删减】
请根据给定的新闻数据生成《每日航运要闻》HTML 文档。
（正文已删减：分类与优先级规则、HTML 设计规范、输入输出格式示例等）"""


# ==================== 向后兼容别名 ====================
# 以下别名用于兼容旧代码中使用的 prompt 变量名，避免修改所有引用文件。

newbuilding_orders_prompt_ordering_party = newbuilding_orders_prompt_entity
newbuilding_orders_prompt_specifications = data_prompt_newbuilding_orders
repair_conversion_prompt_contractor = repair_conversion_prompt_entity
repair_conversion_prompt_project_metrics = data_prompt_repair_conversion
ship_delivery_prompt_quantitative_data = data_prompt_ship_delivery
charter_sale_prompt_specifications = data_prompt_charter_sale
company_finance_prompt_quantitative_data = data_prompt_company_finance
industry_news_prompt_comprehensive_summary = ""
others_prompt_general_metrics = ""

# 旧分类名称兼容
CATEGORY_ALIAS = {
    "国际局势": "行业新闻",
    "行业综合动态": "行业新闻",
    "行业新闻": "行业新闻",
}


# ==================== 热点聚焦生成 Prompt（正文已删减） ====================
hot_focus_prompt = """【演示版：热点聚焦生成指令正文已删减】
请从给定的一周海事新闻列表中，从"船舶市场"、"国际局势"、"政策风向"三个维度各提炼 1 个最核心热点。
输出 JSON 结构：{{"market": {{"topic": ..., "summary": ..., "keywords": [...], "references": [...]}}, "international": ..., "policy": ...}}
（正文已删减：维度视角说明、提炼原则、字段细则等）"""
