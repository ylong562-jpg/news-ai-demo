
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

import asyncio
import copy
import json
import re
from datetime import datetime
from typing import Optional
from ai_engine import BailianWebAnalyzer
from prompt import (
    category_prompt_groups, PROMPT_KEY_MAPPING, JSON_REQUIRED_INDICES,
    get_prompts_by_category, MAIN_DIMENSIONS,
    data_prompt_newbuilding_order_summary,
)
from ai_classify import re_classify, classify_tags_batch
from json_validator import validate_and_fix_json, extract_json_from_text
from entity_normalizer import normalize_shipyard_entity
from ship_type_normalizer import normalize_ship_type_data
from ship_type_retriever import ShipTypeRetriever
from entity_deduplication import deduplicate_entity_async
from news_splitter import split_news_by_main_dimensions, extract_main_dimensions_from_tags
from tag_quality_validator import (
    validate_main_tag_density,
    validate_tag_evidence,
)
from date_utils import parse_event_date, is_ym_date, parse_date

# 全局船型检索器（懒加载）
_ship_type_retriever: Optional[ShipTypeRetriever] = None

# 日期前缀检测正则：匹配 summary 开头是否已有 "【该新闻的发布日期是"
_DATE_PREFIX_RE = re.compile(r'^【该新闻的发布日期是')


def _get_ship_type_retriever() -> ShipTypeRetriever:
    global _ship_type_retriever
    if _ship_type_retriever is None:
        _ship_type_retriever = ShipTypeRetriever()
    return _ship_type_retriever


async def run_prompt_with_index(analyzer, news_item, index, prompt, semaphore, main_tag=""):
    """运行单个指定索引的prompt，返回(key_name, analysis)"""
    async with semaphore:
        try:
            pub_date_raw = (news_item.get('date') or news_item.get('publish_date') or
            news_item.get('published_at') or news_item.get('_original_date') or '')
            pub_date = parse_date(pub_date_raw, default='') if pub_date_raw else ''
            pub_year_match = re.search(r'(\d{4})', str(pub_date_raw)) if pub_date_raw else None
            pub_year = pub_year_match.group(1) if pub_year_match else ''
            current_date = datetime.now().strftime('%Y-%m-%d')

            # 将年份信息直接注入 summary 开头，作为 AI 提取的时间锚点
            date_prefix = ""
            if pub_year:
                date_prefix = (
                    f"【该新闻的发布日期是{pub_date or pub_year + '年'}，"
                    f"当前处理日期是{current_date}，"
                    f"如果每笔交易的订单信息没有明确的年份信息，默认为新闻发布日期的年份信息。】\n\n"
                )

            full_prompt = (
                f"【重要约束】回答中严格去除新闻发布方/媒体平台名称，不要出现任何可能的新闻媒体平台。\n\n"
                f"{prompt}\n\n"
                f"【再次强调】你必须且只能返回合法JSON对象/数组，不要输出任何解释、总结、前言或后缀文字。"
            )

            key_name = PROMPT_KEY_MAPPING.get(index, f"unknown_{index}")
            # data/entity 输出 JSON 较长，需要更大 token 预算
            if key_name == "data":
                max_tokens = 1000
            elif key_name == "entity":
                max_tokens = 600
            else:
                max_tokens = 400

            # 核心输入：同时提供聚焦片段、标题和原始完整正文，避免拆分导致公司名称丢失
            split_summary = news_item.get('summary', '')
            original_summary = news_item.get('original_summary', '') or split_summary
            title = news_item.get('title', '')

            source_text = ""
            if split_summary:
                source_text += f"【相关片段】\n{split_summary}\n\n"
            if title:
                source_text += f"【完整原文 - 标题】\n{title}\n\n"
            if original_summary and original_summary != split_summary:
                source_text += f"【完整原文 - 正文】\n{original_summary}"

            # 避免重复添加年份前缀
            source_with_prefix = source_text
            if date_prefix and source_text and not _DATE_PREFIX_RE.match(source_text):
                source_with_prefix = date_prefix + source_text
            if source_with_prefix:
                result = await analyzer.analyze(
                    content=source_with_prefix,
                    prompt=full_prompt,
                    max_tokens=max_tokens,
                    model="<your-model>"
                )
            else:
                result = await analyzer.analyze(
                    url=news_item['link'],
                    prompt=full_prompt,
                    max_tokens=max_tokens,
                    model="<your-model>"
                )

            if result["success"]:
                analysis = result["analysis"]

                # 对要求返回 JSON 的字段进行验证与修复
                if index in JSON_REQUIRED_INDICES:
                    analysis = await validate_and_fix_json(
                        analyzer=analyzer,
                        raw_result=analysis,
                        original_prompt=prompt,
                        key_name=key_name,
                        news_title=news_item.get("title", ""),
                        max_retries=10,
                        news_content=source_with_prefix,
                        category=main_tag,
                    )

                print(f"  ✅ {key_name} 处理成功")
                return key_name, analysis
            else:
                print(f"  ❌ {key_name} 处理失败: {result.get('error', '未知错误')}")
                return key_name, f"处理失败: {result.get('error', '未知错误')}"

        except Exception as e:
            key_name = PROMPT_KEY_MAPPING.get(index, f"unknown_{index}")
            print(f"  ❌ {key_name} 处理异常: {str(e)}")
            return key_name, f"处理异常: {str(e)}"


def _detect_order_summary(news_item: dict) -> bool:
    """判断当前新闻是否为'新造船订单'下的周期性订单汇总/统计报道。"""
    tags = news_item.get('tags', '')
    try:
        if isinstance(tags, str):
            tags = json.loads(tags) if tags else {}
    except Exception:
        tags = {}
    if isinstance(tags, dict):
        secondary = tags.get('secondary_tags', [])
        if isinstance(secondary, list) and "订单汇总" in secondary:
            return True

    # 若标签未命中，用 summary 关键词做兜底判断
    summary = (news_item.get('summary', '') or news_item.get('title', '')).lower()
    indicators = [
        '上周订单', '本周订单', '上月订单', '本月订单', '订单汇总', '订单统计',
        '新船订单汇总', '造船订单汇总', '公布的订单数量', '接单情况', '订单跟踪',
        '据统计', '上半年订单', '下半年订单', '一季度订单', '二季度订单',
    ]
    return any(kw in summary for kw in indicators)


def _is_valid_value(value):
    """判断一个值是否为有效（非 None、非空字符串、非空容器）。"""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() not in ("", "None", "null", "NULL", "nan", "NaN", "未获取到信息")
    if isinstance(value, (list, dict, set)):
        return len(value) > 0
    return True


def _parse_json_loose(value):
    """安全解析 JSON 字符串/对象/数组，失败返回 None。"""
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


def _validate_newbuilding_entity(entity, data=None):
    """
    校验新造船订单 entity 是否至少包含一个明确主体（shipowner 或 shipyard）。
    支持单字典、JSON 数组以及分组格式 [{"main_tag": ..., "entity": ...}]。
    若 entity 为空但 data.transactions 中已包含有效主体字段，同样视为通过。
    Returns: (bool, reason)
    """
    parsed = _parse_json_loose(entity)

    # 分组格式解包
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "main_tag" in parsed[0]:
        items = []
        for group in parsed:
            if not isinstance(group, dict):
                continue
            inner = _parse_json_loose(group.get("entity"))
            if isinstance(inner, list):
                items.extend(inner)
            elif isinstance(inner, dict):
                items.append(inner)
        parsed = items

    if isinstance(parsed, dict):
        parsed = [parsed]
    if isinstance(parsed, list) and parsed:
        for item in parsed:
            if not isinstance(item, dict):
                continue
            if _is_valid_value(item.get("shipowner")) or _is_valid_value(item.get("shipyard")):
                return True, "entity 主体校验通过"

    # entity 为空时，检查 data 中是否已有主体（支持分组格式）
    if data:
        data_parsed = _parse_json_loose(data)
        # 解包分组格式
        if isinstance(data_parsed, list) and data_parsed and isinstance(data_parsed[0], dict) and "main_tag" in data_parsed[0]:
            data_items = []
            for group in data_parsed:
                if not isinstance(group, dict):
                    continue
                inner = _parse_json_loose(group.get("data"))
                if isinstance(inner, dict):
                    data_items.append(inner)
            data_parsed = data_items

        def _extract_transactions(obj):
            result = []
            if isinstance(obj, dict):
                txs = obj.get("transactions")
                if isinstance(txs, list):
                    result.extend(txs)
                else:
                    result.append(obj)
            elif isinstance(obj, list):
                for item in obj:
                    result.extend(_extract_transactions(item))
            return result

        data_transactions = _extract_transactions(data_parsed)
        for tx in data_transactions:
            if isinstance(tx, dict) and (
                _is_valid_value(tx.get("shipowner")) or _is_valid_value(tx.get("shipyard"))
            ):
                return True, "data 中已包含主体字段，entity 可为空"

    if parsed is None or not parsed:
        return False, "entity 为空或不是有效的对象/数组"

    return False, "entity 与 data 中均未提取到任何明确主体（shipowner 或 shipyard）"


def _validate_newbuilding_data(data):
    """
    校验新造船订单 data 是否包含 ship_type 以及至少一个主体字段（shipowner/shipyard）。
    支持平铺字典、transactions 数组以及分组格式 [{"main_tag": ..., "data": ...}]。
    Returns: (bool, reason)
    """
    parsed = _parse_json_loose(data)
    if parsed is None:
        return False, "data 无法解析为有效 JSON"

    # 分组格式解包
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "main_tag" in parsed[0]:
        items = []
        for group in parsed:
            if not isinstance(group, dict):
                continue
            inner = _parse_json_loose(group.get("data"))
            if isinstance(inner, list):
                items.extend(inner)
            elif isinstance(inner, dict):
                items.append(inner)
        parsed = items

    candidates = []
    if isinstance(parsed, dict):
        candidates.append(parsed)
        txs = parsed.get("transactions")
        if isinstance(txs, list):
            candidates.extend(txs)
    elif isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                candidates.append(item)
                txs = item.get("transactions")
                if isinstance(txs, list):
                    candidates.extend(txs)

    has_ship_type = False
    has_party = False
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if _is_valid_value(item.get("ship_type")):
            has_ship_type = True
        if _is_valid_value(item.get("shipowner")) or _is_valid_value(item.get("shipyard")):
            has_party = True

    if not has_ship_type:
        return False, "data 中未提取到任何有效的 ship_type（船型）"
    if not has_party:
        return False, "data 中未提取到任何明确主体（shipowner 或 shipyard）"

    return True, "data 船型与主体校验通过"


def _check_shipyard_in_source(data, source_text):
    """检查 data 中的 shipyard 是否能在原文中找到依据；返回不在原文中的船厂名列表。"""
    if not data or not source_text:
        return []
    parsed = _parse_json_loose(data)
    if parsed is None:
        return []

    source = str(source_text)
    missing = []

    def _extract_shipyards(obj):
        result = []
        if isinstance(obj, dict):
            val = obj.get('shipyard')
            if isinstance(val, str) and _is_valid_value(val):
                result.append(val.strip())
            for v in obj.values():
                result.extend(_extract_shipyards(v))
        elif isinstance(obj, list):
            for item in obj:
                result.extend(_extract_shipyards(item))
        return result

    for name in _extract_shipyards(parsed):
        # 允许子串匹配：原文中至少包含船厂名的核心部分（>=4 字）
        if name in source:
            continue
        found = False
        for length in range(len(name), 3, -1):
            for start in range(0, len(name) - length + 1):
                segment = name[start:start + length]
                if len(segment) >= 4 and segment in source:
                    found = True
                    break
            if found:
                break
        if not found:
            missing.append(name)
    return missing


def _build_newbuilding_validation_feedback(entity, data, source_text=None):
    """根据 entity/data 校验结果生成给 AI 的反馈文本。"""
    parts = []
    entity_ok, entity_reason = _validate_newbuilding_entity(entity, data=data)
    data_ok, data_reason = _validate_newbuilding_data(data)
    if not entity_ok:
        parts.append(entity_reason)
    if not data_ok:
        parts.append(data_reason)

    if source_text:
        missing_shipyards = _check_shipyard_in_source(data, source_text)
        if missing_shipyards:
            parts.append(
                "以下船厂名在原文中找不到依据，请严格使用原文出现的船厂名称，不要根据集团/关联关系推断："
                + "、".join(missing_shipyards)
            )

    return "；".join(parts)


def _strip_transaction_reason(obj):
    """从解析后的 entity/data JSON 中剥离 transaction_reason 字段，返回 reason 与清洗后的对象。"""
    reason = None
    if isinstance(obj, dict):
        reason = obj.pop("transaction_reason", None)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                sub_reason, _ = _strip_transaction_reason(v)
                if sub_reason and not reason:
                    reason = sub_reason
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                item_reason = item.pop("transaction_reason", None)
                if item_reason and not reason:
                    reason = item_reason
                for v in item.values():
                    if isinstance(v, (dict, list)):
                        sub_reason, _ = _strip_transaction_reason(v)
                        if sub_reason and not reason:
                            reason = sub_reason
    return reason, obj


async def _extract_for_main_tag(analyzer, split_news, main_tag, semaphore, validation_feedback=None):
    """为指定主维度提取 entity 和 data 的辅助函数。"""
    print(f"  🔹 处理主维度: {main_tag}")
    split_news['category'] = main_tag

    # 获取该主维度对应的 prompts
    prompts = list(get_prompts_by_category(main_tag))

    # 注入当前主维度聚焦指令，确保多标签共存时只提取当前维度
    focus_prefix = (
        f"【当前聚焦主维度：{main_tag}】你当前只需提取与【{main_tag}】相关的信息，"
        f"不要返回其它主维度的内容。如果该维度在原文中没有独立事实，可返回 null。\n\n"
    )
    prompts = [focus_prefix + p for p in prompts]

    # 新造船订单/行业新闻 且为订单汇总型报道：使用专用 data prompt
    if main_tag in ("新造船订单", "行业新闻") and _detect_order_summary(split_news):
        print(f"   📊 检测到订单汇总型报道，使用专用 data prompt")
        if len(prompts) >= 2:
            prompts[1] = data_prompt_newbuilding_order_summary

    # trade_type 已由 data prompt 统一指导模型按每笔交易输出，无需在此处额外注入预测订单标记。

    # 船舶交易/建造相关主维度：注入船型候选
    if main_tag in ("新造船订单", "船舶建造", "买卖船", "租船"):
        try:
            retriever = _get_ship_type_retriever()
            summary_for_retrieval = split_news.get('summary', '')
            ship_type_snippet = retriever.get_prompt_snippet(summary_for_retrieval, top_k=5)
            if len(prompts) >= 2:
                prompts = list(prompts)
                prompts[1] = ship_type_snippet + "\n\n" + prompts[1]
        except Exception as e:
            print(f"   ⚠️ 注入船型候选失败: {e}")

    # 如果提供了校验反馈，追加到 prompts 中，引导 AI 修正
    if validation_feedback and main_tag == "新造船订单":
        feedback_suffix = (
            f"\n\n【严格校验反馈 - 必须修正】上次提取结果未通过系统校验：{validation_feedback}。"
            f"请重新阅读新闻原文，务必提取出至少一个订单主体（shipowner 订船方 或 shipyard 造船方），"
            f"并确保 data 中包含 ship_type（规范中文船型名）。如果原文确实没有这些信息，请返回 null，不要虚构。"
        )
        prompts = [p + feedback_suffix for p in prompts]

    tasks = []
    for i, prompt in enumerate(prompts):
        task = asyncio.create_task(
            run_prompt_with_index(analyzer, split_news, i, prompt, semaphore, main_tag=main_tag)
        )
        tasks.append(task)

    prompt_results = {}
    completed_tasks = await asyncio.gather(*tasks, return_exceptions=True)
    for task_result in completed_tasks:
        if isinstance(task_result, tuple) and len(task_result) == 2:
            key, value = task_result
            prompt_results[key] = value

    critical_fields = ["entity", "data"]
    failed_critical_count = sum(
        1 for key in critical_fields
        if prompt_results.get(key, "").startswith(("处理失败", "处理异常"))
    )

    if failed_critical_count >= 2:
        print(f"   ⚠️ 主维度 {main_tag} 两个核心字段均失败")
        return None, None, None

    for key in ["entity", "data"]:
        if key not in prompt_results:
            prompt_results[key] = "未获取到信息"

    entity_raw = prompt_results.get("entity", "")
    data_raw = prompt_results.get("data", "")

    # 提取并剥离 transaction_reason（中间变量，不写入数据库）
    transaction_reason = None
    try:
        entity_parsed = json.loads(entity_raw) if isinstance(entity_raw, str) and entity_raw.strip() else entity_raw
        data_parsed = json.loads(data_raw) if isinstance(data_raw, str) and data_raw.strip() else data_raw

        entity_reason, entity_clean = _strip_transaction_reason(copy.deepcopy(entity_parsed) if isinstance(entity_parsed, (dict, list)) else entity_parsed)
        data_reason, data_clean = _strip_transaction_reason(copy.deepcopy(data_parsed) if isinstance(data_parsed, (dict, list)) else data_parsed)

        transaction_reason = data_reason or entity_reason
        if entity_clean != entity_parsed:
            entity_raw = json.dumps(entity_clean, ensure_ascii=False)
        if data_clean != data_parsed:
            data_raw = json.dumps(data_clean, ensure_ascii=False)
    except Exception as e:
        print(f"   ⚠️ 提取 transaction_reason 失败: {e}")

    return entity_raw, data_raw, transaction_reason


def _ensure_newbuilding_supplement_fields(data):
    """
    确保新造船订单 data 中一定包含 classification_society、flag、design_institute
    三个字段；若缺失或为空，则默认填充 null。不触发 AI 提取。
    """
    if not data:
        return data
    supplement_fields = ["classification_society", "flag", "design_institute", "shipyard"]
    try:
        groups = json.loads(data) if isinstance(data, str) else data
    except Exception:
        return data

    if not isinstance(groups, list):
        groups = [groups]

    def _ensure(obj):
        for field in supplement_fields:
            if field not in obj or obj.get(field) in (None, "", "null", "None", "NULL"):
                obj[field] = None

    for group in groups:
        if not isinstance(group, dict) or group.get("main_tag") != "新造船订单":
            continue
        group_data = group.get("data", {})
        if isinstance(group_data, str):
            try:
                group_data = json.loads(group_data)
            except Exception:
                continue
        # 兼容 AI 把 summary dict 包在单元素数组里的情况
        if isinstance(group_data, list) and len(group_data) == 1 and isinstance(group_data[0], dict) and "transactions" in group_data[0]:
            group_data = group_data[0]
        if not isinstance(group_data, dict):
            continue

        transactions = group_data.get("transactions")
        if isinstance(transactions, list):
            for tx in transactions:
                if isinstance(tx, dict):
                    _ensure(tx)
        else:
            _ensure(group_data)

        # 将解析/修改后的 dict 写回分组，避免序列化时丢失变更
        group["data"] = group_data

    return json.dumps(groups, ensure_ascii=False) if isinstance(data, str) else groups


_SHIP_BUILDING_EVENT_TYPE_MAP = {
    "开工": "钢材切割",
    "钢材切割": "钢材切割",
    "铺龙骨": "铺设龙骨",
    "铺设龙骨": "铺设龙骨",
    "上船台": "铺设龙骨",
    "下水": "下水",
    "出坞": "下水",
    "命名": "下水",
    "吉水": "下水",
    "顺利入水": "下水",
    "试航": "试航",
    "海试": "试航",
    "调试": "试航",
    "交付": "交付",
    "交船": "交付",
    "交接船": "交付",
    "启航": "交付",
}


def _normalize_ship_building_event_type(data):
    """
    规范化船舶建造 data 中的 event_type 到五种标准值：
    钢材切割、铺设龙骨、下水、试航、交付（启航等同于交付）。
    无法识别的值一律置为 null，不再保留"其他"。
    """
    if not data:
        return data
    try:
        groups = json.loads(data) if isinstance(data, str) else data
    except Exception:
        return data

    if not isinstance(groups, list):
        groups = [groups]

    def _normalize_obj(obj):
        if not isinstance(obj, dict):
            return
        event_type = obj.get("event_type")
        if event_type:
            normalized = _SHIP_BUILDING_EVENT_TYPE_MAP.get(str(event_type).strip())
            obj["event_type"] = normalized

    for group in groups:
        if not isinstance(group, dict) or group.get("main_tag") != "船舶建造":
            continue
        group_data = group.get("data", {})
        if isinstance(group_data, str):
            try:
                group_data = json.loads(group_data)
            except Exception:
                continue
        if isinstance(group_data, list) and len(group_data) == 1 and isinstance(group_data[0], dict) and "transactions" in group_data[0]:
            group_data = group_data[0]
        if not isinstance(group_data, dict):
            continue

        transactions = group_data.get("transactions")
        if isinstance(transactions, list):
            for tx in transactions:
                _normalize_obj(tx)
        else:
            _normalize_obj(group_data)

        group["data"] = group_data

    return json.dumps(groups, ensure_ascii=False) if isinstance(data, str) else groups


# 需要对齐 entity/data 交易数量的主维度（这些主维度的 data 支持 transactions 数组）
_TRANSACTION_CATEGORIES = {
    "新造船订单",
    "船舶建造",
    "买卖船",
    "租船",
}

_ENTITY_FIELDS_DESC = {
    "新造船订单": "shipowner（订船方/船东）, shipyard（造船方/船厂）",
    "船舶建造": "shipowner（船东/订造方/接收方）, shipyard（造船方/承建方）",
    "买卖船": "buyer（买方）, seller（卖方）",
    "租船": "charterer（承租方/租船人）, owner（出租方/船东）",
}


def _parse_entity_to_list(entity):
    """将 entity JSON 字符串/对象/数组统一为 Python 字典列表。"""
    if not entity:
        return []
    try:
        parsed = json.loads(entity) if isinstance(entity, str) else entity
    except Exception:
        return []
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _parse_data_transactions(data):
    """解析 data JSON，返回 transactions 字典列表（支持 transactions 数组、平铺字典/列表，
    以及 summary dict 被 AI 包在单元素数组里的情况）。"""
    if not data:
        return []
    try:
        parsed = json.loads(data) if isinstance(data, str) else data
    except Exception:
        return []
    if isinstance(parsed, dict):
        txs = parsed.get("transactions")
        if isinstance(txs, list):
            return [item for item in txs if isinstance(item, dict)]
        return [parsed]
    if isinstance(parsed, list):
        # 兼容 summary dict 被包在单元素数组里的情况
        if len(parsed) == 1 and isinstance(parsed[0], dict) and "transactions" in parsed[0]:
            txs = parsed[0].get("transactions")
            if isinstance(txs, list):
                return [item for item in txs if isinstance(item, dict)]
            return [parsed[0]]
        return [item for item in parsed if isinstance(item, dict)]
    return []


async def _align_entity_to_transactions(analyzer, news_item, main_tag, entity, data):
    """
    当 entity 条目数量与 data 中的 transactions 数量不一致时，
    基于原文和已提取的交易数据重新对齐 entity，确保一一对应。
    若 entity 为空但 data.transactions 已包含主体字段，则无需对齐，直接返回原 entity。
    """
    if main_tag not in _TRANSACTION_CATEGORIES:
        return entity

    entity_list = _parse_entity_to_list(entity)
    transactions = _parse_data_transactions(data)
    if not transactions:
        return entity

    # 新格式：data transactions 已内嵌主体，entity 可为空，无需对齐
    if not entity_list:
        party_fields = {"shipowner", "shipyard", "buyer", "seller", "charterer", "owner", "facility"}
        has_party_in_data = any(
            isinstance(tx, dict) and any(_is_valid_value(tx.get(k)) for k in party_fields)
            for tx in transactions
        )
        if has_party_in_data:
            return entity

    if len(entity_list) == len(transactions):
        return entity

    summary = news_item.get("summary", "") or news_item.get("title", "")
    title = news_item.get("title", "")
    if not summary:
        return entity

    fields_desc = _ENTITY_FIELDS_DESC.get(main_tag, "交易双方主体")

    summary_snippet = summary[:5000]
    alignment_prompt = f"""你是一名航运信息提取专家。请根据新闻原文和已经提取到的交易数据，重新提取并返回恰好 {len(transactions)} 个主体条目，使每个主体条目与对应交易一一对应。

新闻标题：{title}

已提取到的交易数据（按顺序，共 {len(transactions)} 条）：
{json.dumps(transactions, ensure_ascii=False, indent=2)}

当前主体提取结果（可能遗漏或合并了多笔交易）：
{json.dumps(entity_list, ensure_ascii=False, indent=2)}

要求：
1. 必须返回一个合法的 JSON 数组，数组长度必须严格等于 {len(transactions)}。
2. 每个元素是一个字典，字段为：{fields_desc}。
3. 必须严格按交易顺序对应：第 i 个主体对应第 i 条交易。
4. 同一公告下不同船厂、不同船型、不同数量、不同子公司的订单/交易，必须拆分为独立主体条目，不要合并。
5. 如果某条交易的对应主体在原文中没有明确名称，使用 null 或"某船东"/"某船厂"占位，但仍必须占一个数组位置。
6. 绝对不要输出自然语言描述，只输出 JSON 数组。
"""

    try:
        result = await analyzer.analyze(
            content=summary_snippet,
            prompt=alignment_prompt,
            max_tokens=1000,
            model="<your-model>",
        )
        if result.get("success"):
            raw = result.get("analysis", "")
            parsed = extract_json_from_text(raw)
            if isinstance(parsed, list):
                aligned = [item for item in parsed if isinstance(item, dict)]
                if len(aligned) == len(transactions):
                    print(
                        f"   🔗 {main_tag} entity 与 data 交易数量不一致，已对齐："
                        f"{len(entity_list)} -> {len(aligned)}"
                    )
                    return json.dumps(aligned, ensure_ascii=False)
                else:
                    print(
                        f"   ⚠️ {main_tag} entity 对齐后数量仍不一致："
                        f"期望 {len(transactions)}，实际 {len(aligned)}"
                    )
    except Exception as e:
        print(f"   ⚠️ {main_tag} entity-data 对齐失败: {e}")

    return entity


async def _normalize_extracted(entity, data, main_tag):
    """对提取结果进行船厂名、船型名标准化和实体查重。"""
    from entity_normalizer import normalize_shipyard_entity
    from ship_type_normalizer import normalize_ship_type_data
    from entity_deduplication import deduplicate_entity_async

    try:
        if entity:
            entity = normalize_shipyard_entity(entity)
        if data:
            data = normalize_shipyard_entity(data)
    except Exception as e:
        print(f"   ⚠️ 船厂名标准化失败: {e}")

    if main_tag in ("新造船订单", "船舶建造", "买卖船", "租船"):
        try:
            if data:
                data = normalize_ship_type_data(data)
        except Exception as e:
            print(f"   ⚠️ 船型名标准化失败: {e}")

    # 新造船订单补充字段默认填充 null
    if main_tag == "新造船订单" and data:
        data = _ensure_newbuilding_supplement_fields(data)

    # 船舶建造 event_type 规范化
    if main_tag == "船舶建造" and data:
        data = _normalize_ship_building_event_type(data)

    # 实体查重归一化：仅当 entity 为非空字符串（LLM 原始输出）时执行；
    # 行业新闻等 dict 默认结构（如 {"entities": [], "countries": [], "organizations": []}）原样保留
    if entity and isinstance(entity, str):
        try:
            original_entity = entity
            entity = await deduplicate_entity_async(original_entity)
            if entity != original_entity:
                print(f"   🔗 实体查重归一化: entity 已更新")
        except Exception as e:
            print(f"   ⚠️ 实体查重归一化失败: {e}")

    # 对 data 中的主体字段做查重归一化：所有主标签统一处理
    # （行业新闻的 shipowners/shipyards 复数主体字段词表已支持）
    if data:
        try:
            from entity_deduplication import deduplicate_data_async
            original_data = data
            data = await deduplicate_data_async(original_data)
            if data != original_data:
                print(f"   🔗 data 主体查重归一化: data 已更新")
        except Exception as e:
            print(f"   ⚠️ data 主体查重归一化失败: {e}")

    return entity, data


def _is_vague_date(raw_date):
    """判断日期描述是否为模糊词（需要回退到新闻日期）。"""
    if raw_date is None:
        return True
    s = str(raw_date).strip().lower()
    if not s or s in ('', 'null', 'none', 'nan'):
        return True
    # 常见的模糊时间表达
    vague_patterns = [
        r'^日前$', r'^近日$', r'^最近$', r'^不久前$', r'^前些时日?$', r'^前些时候$',
        r'^近日来?$', r'^日前来?$', r'^最近来?$', r'^近段时间$', r'^一段时间以来$',
        r'^日前宣布$', r'^近日宣布$', r'^最近宣布$',
    ]
    for pat in vague_patterns:
        if re.match(pat, s, re.IGNORECASE):
            return True
    return False


_DATE_FORMAT_PATTERNS = None


def _load_date_format_patterns_from_vocab():
    """从 data/date_formats.json 加载允许的 date 格式正则列表。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "date_formats.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
        patterns = [fmt["pattern"] for fmt in config.get("formats", []) if fmt.get("pattern")]
        return [re.compile(p) for p in patterns]
    except Exception as e:
        print(f"   ⚠️ 加载日期格式词表失败: {e}，使用基础格式兜底")
        return [
            re.compile(r'^\d{4}-\d{2}-\d{2}$'),
            re.compile(r'^\d{4}-\d{2}$'),
            re.compile(r'^\d{4}年(?:上|下)半年$'),
            re.compile(r'^\d{4}年第[一二三四1234]季度$'),
            re.compile(r'^\d{4}年Q[1-4]$'),
        ]


def _load_date_format_patterns():
    """返回允许的 date 格式正则列表。

    所有格式均从 data/date_formats.json 词表加载。
    单年份（如 2026、2026年）不在允许范围内，应回退为该条新闻的发布日期。
    为避免信息重复，"YYYY年M月D日"与"YYYY年M月"等格式不在词表中，统一规范为 YYYY-MM-DD / YYYY-MM。
    """
    global _DATE_FORMAT_PATTERNS
    if _DATE_FORMAT_PATTERNS is not None:
        return _DATE_FORMAT_PATTERNS

    _DATE_FORMAT_PATTERNS = _load_date_format_patterns_from_vocab()
    return _DATE_FORMAT_PATTERNS


def _is_allowed_date_format(date_str):
    """判断 date 字符串是否在允许的格式词表范围内。"""
    if date_str is None:
        return False
    s = str(date_str).strip()
    if not s or s.lower() in ('null', 'none', 'nan'):
        return False
    if _is_vague_date(s):
        return False
    for pat in _load_date_format_patterns():
        if pat.match(s):
            return True
    return False


def _parse_news_date_to_iso(pub_date):
    """把新闻发布日期统一解析为 YYYY-MM-DD，失败返回 None。"""
    if not pub_date:
        return None
    s = str(pub_date).strip()
    # 优先匹配 YYYY-MM-DD / YYYY/MM/DD / YYYY年M月D日
    m = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?', s)
    if m:
        try:
            return f"{int(m.group(1))}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except Exception:
            pass
    # 匹配 YYYY-MM / YYYY年M月，取该月1日
    m = re.search(r'(\d{4})[-/年](\d{1,2})(?:月|$)', s)
    if m:
        try:
            return f"{int(m.group(1))}-{int(m.group(2)):02d}-01"
        except Exception:
            pass
    return None


def _normalize_dates_in_data(data, reference_year=None, news_date=None):
    """
    递归规范化 data JSON 中的 date 字段：
    - 优先统一为 YYYY-MM-DD
    - 若原文只明确到月份，则统一为 YYYY-MM
    - 若日期为模糊词（'日前''近日'等）或无效，回退到新闻发布日期
    - 保持其它字段不变
    """
    if not data:
        return data

    parsed = _parse_json_loose(data)
    if parsed is None:
        return data

    fallback_date = _parse_news_date_to_iso(news_date)

    def _norm_obj(obj):
        if not isinstance(obj, dict):
            return
        trade_type = str(obj.get('trade_type', '')).strip()
        raw_date = obj.get('date')
        raw_str = str(raw_date).strip() if raw_date is not None else ""

        # 在手订单：没有明确时间则保持 null，禁止使用新闻日期或补充年份信息
        if trade_type == "在手订单":
            if not raw_str or raw_str in ("当日",) or _is_vague_date(raw_date):
                obj['date'] = None
                return
            # 若模型已返回允许格式，保持原值，不归为新闻日期
            if _is_allowed_date_format(raw_str):
                return
            # 若原文给出单独年份（如"2024年"），视为明确时间，保持原值
            if re.match(r'^\d{4}年?$', raw_str):
                return

        # 特殊值"当日"直接替换为新闻发布日期
        if raw_str == "当日" and fallback_date:
            obj['date'] = fallback_date
            return
        # 单独的年份（如 2026、2026年）直接归为该新闻的发布日期
        if re.match(r'^\d{4}年?$', raw_str):
            if fallback_date:
                print(f"   📅 单年份 {raw_str} 归为该新闻日期：{fallback_date}")
                obj['date'] = fallback_date
            return
        if _is_vague_date(raw_date):
            if fallback_date:
                obj['date'] = fallback_date
            return
        normalized = parse_event_date(raw_date, default=raw_date, reference_year=reference_year)
        if normalized:
            # parse_event_date 成功规范化（结果与原值不同），或原值本身已在允许格式词表中
            if normalized != raw_date and _is_allowed_date_format(normalized):
                obj['date'] = normalized
            elif normalized == raw_date and _is_allowed_date_format(raw_date):
                obj['date'] = raw_date
            elif fallback_date:
                print(f"   ⚠️ date 格式不合法：{raw_date}，回退为新闻日期{fallback_date}")
                obj['date'] = fallback_date

    if isinstance(parsed, dict):
        txs = parsed.get('transactions')
        if isinstance(txs, list):
            for tx in txs:
                _norm_obj(tx)
        _norm_obj(parsed)
    elif isinstance(parsed, list):
        # 兼容多种分组/数组格式：
        # 1. [{"main_tag": ..., "data": ...}]：需要解包 data 再处理 transactions
        # 2. [{"transactions": [...]}] 或 [{"all_quantity": ..., "transactions": [...]}]：直接处理 transactions
        for group in parsed:
            if not isinstance(group, dict):
                _norm_obj(group)
                continue

            if 'data' in group:
                inner = _parse_json_loose(group.get('data'))
                if isinstance(inner, dict):
                    txs = inner.get('transactions')
                    if isinstance(txs, list):
                        for tx in txs:
                            _norm_obj(tx)
                    _norm_obj(inner)
                    group['data'] = json.dumps(inner, ensure_ascii=False)
                _norm_obj(group)
            elif 'transactions' in group:
                txs = group.get('transactions')
                if isinstance(txs, list):
                    for tx in txs:
                        _norm_obj(tx)
                _norm_obj(group)
            else:
                _norm_obj(group)

    return json.dumps(parsed, ensure_ascii=False) if isinstance(data, str) else parsed


def _normalize_shipyard_against_source(data, source_text):
    """
    根据原文对 shipyard 字段进行校验清洗：
    如果 AI 在原文船厂名后补充了原文未出现的后缀（如"（宁波）有限公司"），
    且去掉后缀后的短名称能在原文中找到，则回退到短名称。

    注意：当原文中已经存在完整规范名时，应优先保留完整名，避免把
    "中船黄埔文冲船舶有限公司"误截断为"中船黄埔文冲船舶"。
    """
    if not data or not source_text:
        return data

    parsed = _parse_json_loose(data)
    if parsed is None:
        return data

    source = str(source_text)

    def _clean_name(name):
        if not name:
            return name
        s = str(name).strip()

        # 如果完整名称本身已在原文中出现，直接保留完整规范名，
        # 防止后续按"最短匹配"把完整名截断。
        if s in source:
            return s

        # 候选名称：原名称、去掉括号内容、再去掉有限公司
        candidates = [s]
        no_paren = re.sub(r'[（(][^）)]+[）)]', '', s).strip()
        no_paren = re.sub(r'\s+', '', no_paren)
        if no_paren and no_paren != s:
            candidates.append(no_paren)
        no_ltd = re.sub(r'有限公司$', '', no_paren).strip()
        if no_ltd and no_ltd != no_paren:
            candidates.append(no_ltd)

        # 完整名未在原文中出现时，选择原文中出现的最长候选，
        # 以保留更多信息（如"有限公司"后缀）。
        best = s
        best_len = 0
        for c in candidates:
            if c and c in source and len(c) > best_len:
                best = c
                best_len = len(c)
        return best

    def _norm_obj(obj):
        if isinstance(obj, dict) and 'shipyard' in obj:
            obj['shipyard'] = _clean_name(obj.get('shipyard'))

    if isinstance(parsed, dict):
        txs = parsed.get('transactions')
        if isinstance(txs, list):
            for tx in txs:
                _norm_obj(tx)
        _norm_obj(parsed)
    elif isinstance(parsed, list):
        for group in parsed:
            if not isinstance(group, dict):
                _norm_obj(group)
                continue
            if 'data' in group:
                inner = _parse_json_loose(group.get('data'))
                if isinstance(inner, dict):
                    txs = inner.get('transactions')
                    if isinstance(txs, list):
                        for tx in txs:
                            _norm_obj(tx)
                    _norm_obj(inner)
                    group['data'] = json.dumps(inner, ensure_ascii=False)
                _norm_obj(group)
            elif 'transactions' in group:
                txs = group.get('transactions')
                if isinstance(txs, list):
                    for tx in txs:
                        _norm_obj(tx)
                _norm_obj(group)
            else:
                _norm_obj(group)

    return json.dumps(parsed, ensure_ascii=False) if isinstance(data, str) else parsed


async def process_single_split(analyzer, news_item, split_item, semaphore):
    """
    处理单条拆分后的新闻片段，提取 entity 和 data，并进行密度/证据校验。

    Parameters:
        analyzer: BailianWebAnalyzer 实例
        news_item: 原始新闻字典
        split_item: {"main_tag": str, "summary": str}
        semaphore: 并发信号量

    Returns:
        {"main_tag": str, "entity": str, "data": str} 或 None
    """
    main_tag = split_item.get("main_tag", "")
    if not main_tag:
        main_tag = "行业新闻"

    split_news = news_item.copy()
    # 保留原始完整正文，避免拆分后丢失公司名称等关键信息
    split_news['original_summary'] = news_item.get('summary', '')
    split_summary = split_item.get('summary', news_item.get('summary', ''))

    # 拆分后的片段会丢失原文开头注入的年份前缀，这里重新补回
    pub_date = (news_item.get('date') or news_item.get('publish_date') or
            news_item.get('published_at') or news_item.get('_original_date') or '')
    pub_year_match = re.search(r'(\d{4})', str(pub_date)) if pub_date else None
    pub_year = pub_year_match.group(1) if pub_year_match else ''
    if pub_year and split_summary:
        date_prefix = (
            f"【该新闻的日期是{pub_year}年，"
            f"如果每笔交易的订单信息没有明确的年份信息，默认为新闻日期的年份信息。】\n\n"
        )
        # 避免重复添加（使用正则匹配固定格式）
        if not _DATE_PREFIX_RE.match(split_summary):
            split_summary = date_prefix + split_summary

    split_news['summary'] = split_summary

    # 新造船订单增加强制校验与 AI 重试：entity 必须含主体，data 必须含 ship_type
    max_validation_retries = 2
    validation_feedback = None
    entity, data, transaction_reason = None, None, None
    for attempt in range(max_validation_retries + 1):
        entity, data, transaction_reason = await _extract_for_main_tag(
            analyzer, split_news, main_tag, semaphore,
            validation_feedback=validation_feedback
        )
        if entity is None or data is None:
            return None

        if main_tag != "新造船订单":
            break

        entity_ok, _ = _validate_newbuilding_entity(entity, data=data)
        data_ok, _ = _validate_newbuilding_data(data)
        if entity_ok and data_ok:
            if attempt > 0:
                print(f"   ✅ 新造船订单第 {attempt} 次重提后通过校验")
            break

        feedback = _build_newbuilding_validation_feedback(entity, data, source_text=split_summary)
        if attempt < max_validation_retries:
            print(f"   ⚠️ 新造船订单校验未通过：{feedback}，正在进行第 {attempt + 1} 次 AI 重新提取...")
            validation_feedback = feedback
        else:
            print(f"   ⚠️ 新造船订单经过 {max_validation_retries + 1} 次提取仍未通过校验：{feedback}，使用最后一次结果继续处理")
            validation_feedback = None

    # entity 与 data 的交易数量对齐：修复多 batch 订单主体遗漏/合并问题
    entity = await _align_entity_to_transactions(analyzer, split_news, main_tag, entity, data)

    entity, data = await _normalize_extracted(entity, data, main_tag)

    # 规范化 data 中的 date 字段，支持 YYYY-MM-DD / YYYY-MM
    try:
        pub_year = None
        pub_date = news_item.get('date') or news_item.get('publish_date') or news_item.get('published_at')
        if pub_date:
            m = __import__('re').search(r'(\d{4})', str(pub_date))
            if m:
                pub_year = int(m.group(1))
        # 构造原文文本，用于船厂名称校验
        source_text_for_shipyard = ' '.join(str(split_news.get(field, '')) for field in ['title', 'summary'])
        data = _normalize_dates_in_data(data, reference_year=pub_year, news_date=pub_date)
        # 根据原文校验船厂名称，去除 AI 添加的原文未出现后缀
        data = _normalize_shipyard_against_source(data, source_text_for_shipyard)
        # source 校验可能把规范名回退为原文短名，再次标准化确保最终使用词表完整名
        data = normalize_shipyard_entity(data)
    except Exception as e:
        print(f"   ⚠️ date/shipyard 字段规范化失败: {e}")

    # 密度校验 + 原文证据校验（仅作为提示，不再自动降级主标签）
    density_ok, density_reason = validate_main_tag_density(news_item, main_tag, entity, data)
    evidence_ok = validate_tag_evidence(split_news.get('summary', ''), main_tag)

    if not density_ok:
        print(f"   ⚠️ 主维度 {main_tag} 信息密度偏低：{density_reason}，保留原标签，后续报道生成时校验")
    if not evidence_ok:
        print(f"   ⚠️ 主维度 {main_tag} 原文证据偏弱，保留原标签，后续报道生成时校验")

    return {
        "main_tag": main_tag,
        "entity": entity,
        "data": data,
        "transaction_reason": transaction_reason,
    }


async def process_single_news(analyzer, news_item, semaphore):
    """处理单条新闻：拆分主维度 -> 分别提取 entity/data -> 合并为数组"""
    title = news_item.get('title', '')
    print(f"\n处理新闻: {title}")

    # 确保已有 tags（没有则临时补打）
    tags = news_item.get('tags', '')
    main_dimensions = extract_main_dimensions_from_tags(tags)
    if not main_dimensions:
        print(f"   ⚠️ 未识别主维度，尝试重新打标签...")
        tagged = await classify_tags_batch([news_item], max_concurrency=1)
        if tagged:
            news_item = tagged[0]
            main_dimensions = extract_main_dimensions_from_tags(news_item.get('tags'))

    if not main_dimensions:
        print(f"   ⚠️ 仍无有效主维度，默认归入【行业新闻】")
        # 给新闻补一个默认标签
        default_tags = {"main_tags": ["行业新闻"], "secondary_tags": []}
        news_item['tags'] = json.dumps(default_tags, ensure_ascii=False)
        main_dimensions = ["行业新闻"]

    print(f"   识别到主维度: {', '.join(main_dimensions)}")

    # 按主维度拆分内容
    split_items = await split_news_by_main_dimensions(news_item, analyzer=analyzer)

    # 并发处理每个拆分片段
    prompt_semaphore = asyncio.Semaphore(2)  # 每个片段内部 2 个 prompt 并发
    tasks = [
        asyncio.create_task(process_single_split(analyzer, news_item, split_item, prompt_semaphore))
        for split_item in split_items
    ]
    split_results = await asyncio.gather(*tasks, return_exceptions=True)

    # 合并结果：相同 main_tag 的 entity/data 合并到一组
    entity_groups_map = {}
    data_groups_map = {}
    transaction_reason_map = {}
    final_main_tags = set()

    for result in split_results:
        if isinstance(result, Exception):
            print(f"   ❌ 拆分处理异常: {result}")
            continue
        if result is None:
            continue

        tag = result["main_tag"]
        final_main_tags.add(tag)

        if tag not in entity_groups_map:
            entity_groups_map[tag] = {
                "main_tag": tag,
                "entity": result["entity"],
            }
        else:
            # 合并 entity：简单拼接字符串，保留第一个
            existing = entity_groups_map[tag]["entity"]
            if existing and result["entity"]:
                entity_groups_map[tag]["entity"] = existing + "\n" + str(result["entity"])
            elif result["entity"]:
                entity_groups_map[tag]["entity"] = result["entity"]

        if tag not in data_groups_map:
            data_groups_map[tag] = {
                "main_tag": tag,
                "data": result["data"],
            }
        else:
            existing = data_groups_map[tag]["data"]
            if existing and result["data"]:
                data_groups_map[tag]["data"] = existing + "\n" + str(result["data"])
            elif result["data"]:
                data_groups_map[tag]["data"] = result["data"]

        # 收集 transaction_reason（中间变量，不写入 entity/data）
        reason = result.get("transaction_reason")
        if reason:
            if tag not in transaction_reason_map:
                transaction_reason_map[tag] = []
            transaction_reason_map[tag].append(reason)

    if not entity_groups_map:
        print(f"🗑️ 删除处理失败的新闻: {title}")
        return None

    entity_groups = list(entity_groups_map.values())
    data_groups = [data_groups_map[g["main_tag"]] for g in entity_groups]

    # 同步更新 tags 中的 main_tags 和 information_density，确保与提取结果一致
    try:
        tags = news_item.get('tags', '')
        if isinstance(tags, str):
            tags = json.loads(tags) if tags else {}
        if isinstance(tags, dict):
            final_tags = list(final_main_tags) or ["行业新闻"]
            tags['main_tags'] = final_tags
            # 同步 information_density：保留已有密度，新增主维度默认 medium，删除已不在的键
            density = tags.get('information_density', {}) if isinstance(tags.get('information_density'), dict) else {}
            tags['information_density'] = {
                tag: density.get(tag, "medium") for tag in final_tags
            }
            news_item['tags'] = json.dumps(tags, ensure_ascii=False)
    except Exception as e:
        print(f"   ⚠️ 更新 tags 失败: {e}")

    updated_news = news_item.copy()
    # 保留已有的 summary，仅在缺失时用 title 兜底（content 为历史冗余字段，不再使用）
    if not updated_news.get("summary"):
        updated_news["summary"] = title

    # 将年份信息注入 summary 开头，确保下游报告生成/展示也能看到明确时间锚点
    pub_date = (news_item.get('date') or news_item.get('publish_date') or
            news_item.get('published_at') or news_item.get('_original_date') or '')
    pub_year_match = re.search(r'(\d{4})', str(pub_date)) if pub_date else None
    pub_year = pub_year_match.group(1) if pub_year_match else ''
    if pub_year and updated_news.get("summary"):
        date_prefix = (
            f"【该新闻的日期是{pub_year}年，"
            f"如果每笔交易的订单信息没有明确的年份信息，默认为新闻日期的年份信息。】\n\n"
        )
        # 避免重复添加（使用正则匹配固定格式）
        if not _DATE_PREFIX_RE.match(str(updated_news["summary"])):
            updated_news["summary"] = date_prefix + updated_news["summary"]

    updated_news["entity"] = json.dumps(entity_groups, ensure_ascii=False)
    updated_news["data"] = json.dumps(data_groups, ensure_ascii=False)
    # transaction_reason 作为中间变量辅助后续 AI 步骤，不写入数据库字段
    if transaction_reason_map:
        updated_news["_transaction_reasons"] = transaction_reason_map
    updated_news["ai_processed"] = True
    updated_news["model_used"] = "<your-model>"
    # 新流程中不再写入 category
    updated_news["category"] = ""

    print(f"✅ 新闻处理完成: {title}（生成 {len(entity_groups)} 组数据）")
    return updated_news


async def process_news_with_category_prompts(news_data):
    """处理新闻数据，增加全局并发控制"""
    processed_news = []
    analyzer = BailianWebAnalyzer(region="beijing")

    # 创建全局信号量，限制总并发数
    news_semaphore = asyncio.Semaphore(3)

    async def process_with_semaphore(news_item):
        async with news_semaphore:
            try:
                return await process_single_news(analyzer, news_item, news_semaphore)
            except Exception as e:
                print(f"❌ 处理失败 {news_item.get('title', '')}: {str(e)}")
                return news_item

    # 创建所有新闻的处理任务
    tasks = []
    for news_item in news_data:
        task = asyncio.create_task(process_with_semaphore(news_item))
        tasks.append(task)

    # 分批处理，避免一次性创建过多任务
    batch_size = 5
    for i in range(0, len(tasks), batch_size):
        batch_tasks = tasks[i:i + batch_size]
        batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

        for result in batch_results:
            if not isinstance(result, Exception):
                processed_news.append(result)

        if i + batch_size < len(tasks):
            print(f"批次完成，等待3秒后继续下一批...")
            await asyncio.sleep(3)

    return processed_news


async def process_existing_news(news_data):
    """处理已有的新闻数据（不重新获取）"""
    if not news_data:
        print("❌ 没有新闻数据需要处理")
        return []

    print(f"开始处理 {len(news_data)} 条新闻...")

    try:
        result = await process_news_with_category_prompts(news_data)
        print(f"\n核心内容提取完成，共处理 {len(result)} 条新闻")
        return result
    except Exception as e:
        print(f"❌ 处理失败: {str(e)}")
        return []


async def main_processing():
    """主处理函数"""
    try:
        news_data = await re_classify()
        if not news_data:
            print("❌ 没有获取到新闻数据")
            return []

        print(f"开始根据主维度重新处理 {len(news_data)} 条新闻...")

        max_retries = 3
        for attempt in range(max_retries):
            try:
                result = await process_news_with_category_prompts(news_data)
                print(f"\n核心内容提取完成，共处理 {len(result)} 条新闻")
                return result
            except Exception as e:
                print(f"❌ 第{attempt + 1}次处理失败: {str(e)}")
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 5
                    print(f"等待{wait_time}秒后重试...")
                    await asyncio.sleep(wait_time)
                else:
                    raise e
        return []

    except Exception as e:
        print(f"❌ 主处理函数执行失败: {str(e)}")
        return []


if __name__ == "__main__":
    asyncio.run(main_processing())
