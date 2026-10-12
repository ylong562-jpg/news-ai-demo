# -*- coding: utf-8 -*-
"""
主标签质量校验模块

对主维度标签进行密度校验、证据校验和报道后验校验，
防止低信息量内容被强行分类，避免主标签与报道内容不一致。
"""

import json
import re
from typing import Any, Dict, List, Tuple, Optional

# 主维度优先级（数字越小优先级越高）
# 顺序与 prompt.py 中 MAIN_DIMENSIONS 保持一致：
# 新造船订单 -> 船舶建造 -> 公司金融 -> 买卖船 -> 租船 -> 维修改装 -> 行业新闻
MAIN_TAG_PRIORITY = {
    "新造船订单": 1,
    "船舶建造": 2,
    "公司金融": 3,
    "买卖船": 4,
    "租船": 5,
    "维修改装": 6,
    "行业新闻": 7,
}


# 元话语/失败话语，报道中不应出现
META_PHRASES = [
    "无法生成", "无有效信息", "无有效交易", "无法提取", "没有有效",
    "不能生成", "未能生成", "不存在有效", "不具备有效", "无明显",
]


def _safe_json_loads(value: Any) -> Any:
    """安全解析 JSON 字符串/对象。"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value or value.lower() in ("null", "none", "未获取到信息"):
            return None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _is_valid(value: Any) -> bool:
    """检查值是否有效（非 None、非空字符串、非空列表、非空字典）。"""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() not in ("", "None", "null", "NULL", "nan", "NaN", "未获取到信息")
    if isinstance(value, (list, dict, set)):
        return len(value) > 0
    return True


def _unwrap_grouped_field(value: Any, field_name: str) -> Any:
    """
    如果 value 是新的分组格式 [{"main_tag": ..., field_name: ...}, ...]，
    则解包出所有内层对象/数组，返回一个合并后的列表或单个字典。
    否则返回原解析结果。
    """
    parsed = _safe_json_loads(value)
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "main_tag" in parsed[0]:
        inner_items = []
        for group in parsed:
            if not isinstance(group, dict):
                continue
            inner = group.get(field_name, "")
            inner_parsed = _safe_json_loads(inner)
            if isinstance(inner_parsed, list):
                inner_items.extend(inner_parsed)
            elif isinstance(inner_parsed, dict):
                inner_items.append(inner_parsed)
        return inner_items if inner_items else parsed
    return parsed


def _get_transaction_list(data: Any) -> List[Dict[str, Any]]:
    """从 data 中提取 transactions 列表，支持分组格式、平铺字典、transactions 数组、旧格式数组。"""
    parsed = _unwrap_grouped_field(data, "data")
    if parsed is None:
        return []

    def _extract(obj):
        """递归从 dict/list 中提取 transactions。"""
        result = []
        if isinstance(obj, dict):
            if "transactions" in obj and isinstance(obj["transactions"], list):
                for t in obj["transactions"]:
                    if isinstance(t, dict):
                        result.append(t)
            else:
                result.append(obj)
        elif isinstance(obj, list):
            for item in obj:
                result.extend(_extract(item))
        return result

    return _extract(parsed)


def _entity_has_any(entity: Any, keys: List[str]) -> bool:
    """检查 entity 中是否有任一 key 有效（支持分组格式）。"""
    parsed = _unwrap_grouped_field(entity, "entity")
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return False
    for item in parsed:
        if not isinstance(item, dict):
            continue
        for key in keys:
            if _is_valid(item.get(key)):
                return True
    return False


def _data_has_any(data: Any, keys: List[str]) -> bool:
    """检查 data（含 transactions）中是否有任一 key 有效（支持分组格式）。"""
    transactions = _get_transaction_list(data)
    if transactions:
        for t in transactions:
            for key in keys:
                if _is_valid(t.get(key)):
                    return True
    parsed = _unwrap_grouped_field(data, "data")
    if isinstance(parsed, dict):
        for key in keys:
            if _is_valid(parsed.get(key)):
                return True
    return False


def _count_distinct_entities(text: Any) -> int:
    """粗略计算字符串中独立公司/主体数量（用于判断是否过度合并）。
    仅按常见分隔符拆分，保留英文公司名内部空格，避免把一个公司拆成多个 token。
    """
    if not text:
        return 0
    text = str(text).strip()
    if not text:
        return 0

    import re
    # 用常见分隔符拆分；先统一替换为同一分隔符，再 split
    separators = r'[、，,和/；;及]'
    parts = [p.strip() for p in re.split(separators, text) if p.strip()]

    seen = set()
    unique = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return len(unique)


def _get_transaction_count(data: Any) -> int:
    """从 data 中提取交易笔数（支持分组格式）。"""
    return len(_get_transaction_list(data))


def _can_merge_entities(entity_list: list, max_party_count: int = 3) -> bool:
    """
    判断多个 entity 是否可以合理合并为一笔交易的订船方/造船方。
    只有当所有 entity 共享相同的 shipowner 或 shipyard，或合并后主体数量不超过阈值时，才允许合并。
    """
    if not entity_list:
        return False

    def _is_valid_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip() not in ("", "None", "null", "NULL", "nan", "NaN")
        if isinstance(value, (list, dict, set)):
            return len(value) > 0
        return True

    shipowners = []
    shipyards = []
    for item in entity_list:
        if not isinstance(item, dict):
            continue
        so = item.get('shipowner', '')
        sy = item.get('shipyard', '')
        if _is_valid_value(so):
            shipowners.append(str(so).strip())
        if _is_valid_value(sy):
            shipyards.append(str(sy).strip())

    # 去重
    unique_shipowners = list(dict.fromkeys(shipowners))
    unique_shipyards = list(dict.fromkeys(shipyards))

    # 如果只有唯一 shipowner 或唯一 shipyard，说明是同一笔交易的不同表述，可以合并
    if len(unique_shipowners) <= 1 and len(unique_shipyards) <= 1:
        return True

    # 否则，统计合并后的独立主体数量
    total_owner_count = sum(_count_distinct_entities(so) for so in unique_shipowners)
    total_yard_count = sum(_count_distinct_entities(sy) for sy in unique_shipyards)

    # 合并后订船方超过阈值，或造船方超过2个，认为关联性不足，不应合并
    if total_owner_count > max_party_count or total_yard_count > 2:
        return False

    return True


def validate_main_tag_density(
    news_item: Optional[Dict[str, Any]],
    main_tag: str,
    entity: Any,
    data: Any,
) -> Tuple[bool, str]:
    """
    校验指定主标签是否有足够的信息密度/有效事实支撑。

    Returns:
        (是否通过, 原因)
    """
    if not main_tag:
        return False, "主标签为空"

    # 交易类主标签：优先要求有具体交易方 + 具体交易信息
    if main_tag == "新造船订单":
        # 检测是否为订单汇总型报道（支持分组格式）
        parsed_data = _unwrap_grouped_field(data, "data")
        if isinstance(parsed_data, list) and parsed_data:
            # 如果是分组解包后的列表，取第一个字典用于判断 summary_type
            parsed_data = parsed_data[0] if isinstance(parsed_data[0], dict) else {}
        is_order_summary = (
            isinstance(parsed_data, dict)
            and parsed_data.get("summary_type") == "订单汇总"
        )

        has_party = _entity_has_any(entity, ["shipowner", "shipyard"]) or _data_has_any(data, ["shipowner", "shipyard"])
        has_ship_info = _data_has_any(data, ["ship_type", "quantity", "price", "specs"])
        # 如果 data 中船型、数量、规格齐全，即使 entity 暂时缺少订船方/造船方，
        # 也视为有足够事实支撑，允许生成报道（避免 entity 提取遗漏导致正确订单被降级）
        has_rich_data = _data_has_any(data, ["ship_type"]) and (
            _data_has_any(data, ["quantity"]) or _data_has_any(data, ["specs"])
        )

        # 订单汇总型报道：只要有统计周期或总数量或主要订单明细，即视为有事实支撑
        if is_order_summary:
            has_summary_info = _is_valid(parsed_data.get("period")) or _is_valid(parsed_data.get("all_quantity"))
            has_transactions = (
                isinstance(parsed_data, dict)
                and isinstance(parsed_data.get("transactions"), list)
                and len(parsed_data["transactions"]) > 0
            )
            if has_summary_info or has_transactions or has_ship_info:
                return True, "订单汇总型新造船订单密度校验通过"
            return False, "订单汇总型报道缺少统计周期、总数量及订单明细"

        if not has_party and not has_rich_data:
            return False, "新造船订单缺少订船方/造船方，且 data 中船型/数量/规格不足"
        if not has_ship_info:
            return False, "新造船订单缺少船型、数量、金额或规格信息"

        # 防止 entity 提取过宽：单 data 交易但 shipowner/shipyard 包含过多无关主体。
        # 当 data 已经能明确识别为具体订单（船型+数量/规格）时，主体数量即便较多
        #（如订单汇总/多国船厂对比报道）也应允许生成报道，避免把真实订单误降级为行业新闻。
        if not has_rich_data:
            entity_parsed = _safe_json_loads(entity)
            if isinstance(entity_parsed, dict):
                entity_parsed = [entity_parsed]
            tx_count = _get_transaction_count(data)
            if isinstance(entity_parsed, list) and tx_count <= 1:
                total_owner_count = 0
                total_yard_count = 0
                for item in entity_parsed:
                    if isinstance(item, dict):
                        total_owner_count += _count_distinct_entities(item.get("shipowner"))
                        total_yard_count += _count_distinct_entities(item.get("shipyard"))
                if total_owner_count > 3:
                    return False, f"新造船订单的订船方包含{total_owner_count}个独立主体，超过合理范围，疑似把多笔交易或背景公司合并为一笔"
                if total_yard_count > 2:
                    return False, f"新造船订单的造船方包含{total_yard_count}个独立主体，超过合理范围，疑似提取过宽"

        return True, "密度校验通过"

    if main_tag == "买卖船":
        has_party = _entity_has_any(entity, ["buyer", "seller"]) or _data_has_any(data, ["buyer", "seller"])
        has_ship_info = _data_has_any(data, ["ship_type", "quantity", "money", "specs"])
        has_rich_data = _data_has_any(data, ["ship_type"]) and (
            _data_has_any(data, ["quantity"]) or _data_has_any(data, ["specs"])
        )
        if not has_party and not has_rich_data:
            return False, "买卖船缺少买方/卖方，且 data 中船型/数量/规格不足"
        if not has_ship_info:
            return False, "买卖船缺少船型、数量、金额或规格信息"
        return True, "密度校验通过"

    if main_tag == "租船":
        has_party = _entity_has_any(entity, ["charterer", "owner"]) or _data_has_any(data, ["charterer", "owner"])
        has_ship_info = _data_has_any(data, ["ship_type", "quantity", "money", "specs"])
        has_rich_data = _data_has_any(data, ["ship_type"]) and (
            _data_has_any(data, ["quantity"]) or _data_has_any(data, ["specs"])
        )
        if not has_party and not has_rich_data:
            return False, "租船缺少承租方/出租方，且 data 中船型/数量/规格不足"
        if not has_ship_info:
            return False, "租船缺少船型、数量、租金或规格信息"
        return True, "密度校验通过"

    if main_tag == "船舶建造":
        has_party = _entity_has_any(entity, ["shipowner", "shipyard"]) or _data_has_any(data, ["shipowner", "shipyard"])
        # 必须明确包含节点/交付类型，否则只是订单中的承建信息
        has_event_type = _data_has_any(data, ["event_type", "node_type"])
        has_ship_type = _data_has_any(data, ["ship_type"])
        has_build_info = _data_has_any(data, ["ship_type", "event_type", "node_type", "specs", "vessel_name"])

        # 宽松条件1：如果 data 能明确识别为建造/交付节点（有 event_type 和 ship_type），
        # 说明节点事实明确，即使 entity 提取失败也允许生成报道
        has_strong_event = has_event_type and has_ship_type

        # 宽松条件2：多事件汇总报道，transactions 中至少有一条包含节点类型
        transactions = _get_transaction_list(data)
        has_event_transactions = any(
            _is_valid(t.get("event_type")) or _is_valid(t.get("node_type"))
            for t in transactions
        )

        # 宽松条件3：entity 中已明确船厂，且 data 中至少包含船型、船名或规格之一
        has_rich_data_with_party = has_party and _data_has_any(data, ["ship_type", "specs", "vessel_name"])

        if has_strong_event or has_event_transactions or has_rich_data_with_party:
            return True, "船舶建造节点事实明确，密度校验通过"

        if not has_party:
            return False, "船舶建造缺少船厂或订造方/接收方"
        if not has_build_info:
            return False, "船舶建造缺少船型、节点/交付类型、船名或规格"
        # 对纯生产节点保留更严格校验：必须包含节点类型
        if _data_has_any(data, ["node_type"]) and not has_event_type:
            return False, "船舶建造缺少明确的节点类型（开工/下水/试航/命名/出坞等），可能只是订单承建信息"
        return True, "密度校验通过"

    if main_tag == "维修改装":
        has_party = _entity_has_any(entity, ["shipowner", "shipyard"]) or _data_has_any(data, ["shipowner", "facility"])
        has_repair_info = _data_has_any(data, ["ship_type", "modify_type", "specs", "vessel_name", "facility"])
        if not has_party:
            return False, "维修改装缺少船东/运营方或船厂/服务商"
        if not has_repair_info:
            return False, "维修改装缺少船型、改装类型、船名、规格或服务商"
        return True, "密度校验通过"

    if main_tag == "公司金融":
        has_info = _data_has_any(data, ["event_type", "amount", "parties", "period", "metrics"])
        if not has_info:
            return False, "公司金融缺少事件类型、金额、主体、周期或财务指标"
        return True, "密度校验通过"

    if main_tag == "行业新闻":
        parsed_data = _unwrap_grouped_field(data, "data")
        if isinstance(parsed_data, list) and parsed_data:
            parsed_data = parsed_data[0] if isinstance(parsed_data[0], dict) else {}
        if isinstance(parsed_data, dict) and parsed_data.get("summary_type") == "订单汇总":
            has_summary_info = _is_valid(parsed_data.get("period")) or _is_valid(parsed_data.get("all_quantity"))
            has_transactions = (
                isinstance(parsed_data.get("transactions"), list)
                and len(parsed_data["transactions"]) > 0
            )
            if has_summary_info or has_transactions:
                return True, "行业新闻：订单汇总型报道密度校验通过"
            return False, "行业新闻：订单汇总型报道缺少统计周期、总数量及订单明细"

        has_info = _data_has_any(data, ["event_type", "subject", "shipowners", "shipyards", "key_info", "location", "affected_routes", "impact", "metrics"])
        if not has_info:
            return False, "行业新闻缺少事件类型、主体、关键信息、地点、影响或指标"
        return True, "密度校验通过"

    # 其他主标签默认放行，但要求 data 非空
    parsed_data = _safe_json_loads(data)
    if parsed_data is None:
        return False, f"{main_tag}未提取到有效数据"
    return True, "密度校验通过"


def validate_tag_evidence(
    news_content: str,
    main_tag: str,
    extracted_content: str = "",
) -> bool:
    """
    检查原文中是否存在支持该主维度作为核心主题的证据。

    不再使用关键词严格匹配，而是交给 AI 在分类阶段自行判断。
    本函数仅做最基础的非空检查，避免对空内容继续处理。
    """
    if not news_content:
        return False

    # 内容过短时认为证据不足（阈值 20 字，非常宽松）
    if len(news_content.strip()) < 20:
        return False

    return True


def validate_report_matches_tag(
    report: str,
    main_tag: str,
    entity: Any = None,
    data: Any = None,
) -> Tuple[bool, str]:
    """
    报道生成后的后验校验：仅用于识别 AI 生成失败或明显空内容。

    不再对报道内容做关键词严格匹配（如要求必须出现"市场""财报"等），
    具体内容是否符合主标签完全交给 AI 判断与生成。

    Returns:
        (是否通过, 原因)
    """
    if not report or not isinstance(report, str):
        return False, "报道为空"

    report = report.strip()
    if len(report) < 10:
        return False, "报道过短，信息不足"

    # 检查元话语 / AI 失败话语
    for phrase in META_PHRASES:
        if phrase in report:
            return False, f"报道包含元话语：{phrase}"

    # 其余情况均放行，由 AI 负责内容与主标签的一致性
    return True, "报道校验通过"


def get_main_tag_priority(main_tag: str) -> int:
    """获取主维度优先级，数字越小优先级越高。"""
    return MAIN_TAG_PRIORITY.get(main_tag, 99)


def sort_main_tags_by_priority(
    main_tags: List[str],
    information_density: Optional[Dict[str, str]] = None,
) -> List[str]:
    """
    按优先级和信息密度对主维度排序。
    优先级高者在前；同优先级时，信息密度 high > medium > low。
    """
    density_order = {"high": 0, "medium": 1, "low": 2}
    density_map = information_density or {}

    def sort_key(tag: str) -> Tuple[int, int]:
        priority = MAIN_TAG_PRIORITY.get(tag, 99)
        density = density_order.get(density_map.get(tag, "medium"), 1)
        return (priority, density)

    return sorted(main_tags, key=sort_key)


def downgrade_main_tag(main_tag: str) -> str:
    """将校验失败的主标签降级为行业新闻。"""
    return "行业新闻"


def is_transaction_main_tag(main_tag: str) -> bool:
    """判断是否为船舶交易类主标签。"""
    return main_tag in {"新造船订单", "买卖船", "租船"}


def extract_fact_fingerprint(
    main_tag: str,
    entity: Any,
    data: Any,
    report: Optional[str] = None,
) -> set:
    """
    提取报道的“事实指纹”，用于跨主维度去重。
    返回船名、船型、船东、船厂、交易动作等关键 token 集合。
    """
    fingerprint = set()

    # 从 entity 提取
    entity_parsed = _safe_json_loads(entity)
    if isinstance(entity_parsed, dict):
        for key, val in entity_parsed.items():
            if _is_valid(val):
                if isinstance(val, list):
                    for v in val:
                        fingerprint.add(str(v).strip())
                else:
                    fingerprint.add(str(val).strip())
    elif isinstance(entity_parsed, list):
        for item in entity_parsed:
            if isinstance(item, dict):
                for key, val in item.items():
                    if _is_valid(val):
                        fingerprint.add(str(val).strip())

    # 从 data 提取
    transactions = _get_transaction_list(data)
    key_fields = [
        "shipowner", "shipyard", "buyer", "seller", "charterer", "owner", "facility",
        "ship_type", "vessel_name", "specs", "quantity", "price", "money", "delivery", "node_type", "modify_type",
    ]
    for t in transactions:
        for key in key_fields:
            val = t.get(key)
            if _is_valid(val):
                fingerprint.add(str(val).strip())

    parsed_data = _safe_json_loads(data)
    if isinstance(parsed_data, dict):
        for key in key_fields:
            val = parsed_data.get(key)
            if _is_valid(val):
                fingerprint.add(str(val).strip())

    # 从 report 提取交易动作词（用于判断绿色航运等是否重复订单事实）
    if report:
        report_lower = report.lower()
        for kw in ["订造", "下单", "订购", "签约", "交付", "交船", "试航", "下水", "出坞"]:
            if kw in report_lower:
                fingerprint.add(kw)

    # 过滤空字符串
    return {t for t in fingerprint if t}


def calculate_fingerprint_overlap(fingerprint1: set, fingerprint2: set) -> float:
    """计算两个事实指纹集合的重叠率。"""
    if not fingerprint1 or not fingerprint2:
        return 0.0
    intersection = fingerprint1 & fingerprint2
    union = fingerprint1 | fingerprint2
    if not union:
        return 0.0
    return len(intersection) / len(union)


def calculate_report_text_similarity(report: str, existing_reports: list) -> float:
    """
    计算当前报道与已生成报道集合之间的最大文本相似度。
    使用简单分词后的 Jaccard 相似度，用于识别无增量信息的重复报道。
    """
    if not report or not existing_reports:
        return 0.0

    import re

    def tokenize(text):
        # 保留至少两个字符的词，过滤常见停用词和标点
        text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', ' ', str(text))
        tokens = [t.strip() for t in text.split() if len(t.strip()) >= 2]
        return set(tokens)

    report_tokens = tokenize(report)
    if not report_tokens:
        return 0.0

    max_sim = 0.0
    for existing in existing_reports:
        existing_tokens = tokenize(existing)
        if not existing_tokens:
            continue
        intersection = report_tokens & existing_tokens
        union = report_tokens | existing_tokens
        sim = len(intersection) / len(union) if union else 0.0
        if sim > max_sim:
            max_sim = sim

    return max_sim
