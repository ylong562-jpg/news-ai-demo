
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
import json
import re
from datetime import datetime, timedelta
from ai_engine import BailianWebAnalyzer
from ai_news_core_content_extraction import main_processing
from date_utils import parse_date, is_allowed_date_format
from tag_quality_validator import _can_merge_entities, extract_fact_fingerprint



# 清理新闻报告：去除句尾字数统计和开头错误时间
REPORT_POLISH_PROMPT = """【演示版：报告撰写/润色指令正文已删减】请基于给定的结构化新闻数据撰写/润色报道正文。"""


# 新造船订单专用润色提示：引导 AI 合并高度相似的多笔订单
SHIP_ORDER_POLISH_PROMPT = """【演示版：报告撰写/润色指令正文已删减】请基于给定的结构化新闻数据撰写/润色报道正文。"""


# 船舶建造专用润色提示：固定输出所有有效信息，严禁凭空添加船东
SHIP_BUILDING_POLISH_PROMPT = """【演示版：报告撰写/润色指令正文已删减】请基于给定的结构化新闻数据撰写/润色报道正文。"""


# 订单汇总型报道：直接把原文/摘要交给 AI 生成，避免模板化表述
ORDER_SUMMARY_REPORT_PROMPT = """【演示版：报告撰写/润色指令正文已删减】请基于给定的结构化新闻数据撰写/润色报道正文。"""


def clean_news_report(text):
    """
    清理新闻报告：去除句尾字数统计、开头错误时间以及结尾的"以上"收尾词
    
    Args:
        text: 需要清理的文本
        
    Returns:
        清理后的文本
    """
    if not text:
        return text
    # 去除句尾字数统计，如"(68字)"、"（68字）"
    text = re.sub(r'[（(]\d+字[）)]', '', text)
    # 去除开头的多余标点符号（如逗号、顿号）
    text = re.sub(r'^[,，、]+', '', text)
    # 去除结尾的"以上"收尾词及其前后附带标点、括号、空格
    text = re.sub(r'[。，、；：！？\.（）()\s]*以上[。，、；：！？\.（）()\s]*$', '', text)
    # 清理多余空格
    text = text.strip()
    return text


def _report_fact_coverage(report, main_tag, entity, data, min_coverage=0.5):
    """
    检查润色后的报告是否仍保留了 entity/data 中的关键事实。
    覆盖度低于阈值时认为润色丢失了关键信息，应回退到原报告。
    """
    if not report:
        return 0.0

    facts = extract_fact_fingerprint(main_tag, entity, data)
    if not facts:
        return 1.0

    report_lower = report.lower()
    covered = 0
    for fact in facts:
        fact_str = str(fact).strip().lower()
        if fact_str and fact_str in report_lower:
            covered += 1

    return covered / len(facts)


async def _generate_order_summary_report_with_ai(analyzer, summary, title=""):
    """
    订单汇总型报道直接把原文/摘要交给 AI 生成，避免模板化表述、来源前缀和具体日期。
    """
    if not summary:
        return ""

    content = f"标题：{title}\n\n原文/摘要：{summary[:3000]}"
    try:
        result = await analyzer.analyze(
            content=content,
            prompt=ORDER_SUMMARY_REPORT_PROMPT,
            max_tokens=200,
            model="<your-model>",
        )
        if result.get("success"):
            report = clean_news_report(result.get("analysis", "").strip())
            if report:
                print("   ✅ 订单汇总报道已由 AI 基于原文生成")
                return report
    except Exception as e:
        print(f"   ⚠️ 订单汇总 AI 报道生成失败: {e}")

    return ""


def remove_hallucinated_dates(report, news_item):
    """
    移除生成报告中原文不支持的日期，防止 AI 幻觉固定示例日期（如反复出现的4月15日）。
    若原文只给出日期而省略月份，可参考该新闻的发布日期补全月份；无明确日期则不生成日期。
    
    Args:
        report: AI生成的新闻报告
        news_item: 原始新闻数据字典，包含 title/summary/entity/data 以及可选的发布日期字段
        
    Returns:
        清理后的新闻报告
    """
    if not report:
        return report
    
    # 构建原文文本
    source_fields = ['title', 'summary', 'entity', 'data']
    source_text = ' '.join(str(news_item.get(field, '')) for field in source_fields)
    
    # 获取新闻发布日期的月份和日期，用于补全省略月份的日期
    pub_month = None
    pub_day = None
    publish_date = (
        news_item.get('publish_date')
        or news_item.get('published_at')
        or news_item.get('date')
        or news_item.get('fetch_date')
        or news_item.get('created_at')
    )
    if publish_date:
        m = re.search(r'(\d{4})[-/.年]?(\d{1,2})[-/.月]?(\d{1,2})', str(publish_date))
        if m:
            pub_month = int(m.group(2))
            pub_day = int(m.group(3))
    
    # 检查完整月日（X月X日/号）是否存在于原文
    def month_day_in_source(month, day):
        m = str(int(month))
        d = str(int(day))
        patterns = [
            f"{m}月{d}日",
            f"{m}月{d}号",
            f"{m}.{d}",
        ]
        return any(p in source_text for p in patterns)
    
    # 检查原文中是否存在独立的日期（只有日，没有完整月日前缀）
    def day_only_in_source(day):
        d = str(int(day))
        patterns = [f"{d}日", f"{d}号"]
        for p in patterns:
            # 排除 "X月X日/X号" 中的日期部分，且日期数字前不能是数字，只匹配独立的日期
            if re.search(rf'(?<!\d)(?<!\d月)(?<!\d\d月){re.escape(p)}', source_text):
                return True
        return False
    
    # 根据原文中的完整月日，补全只有日期的表达
    def find_month_for_day(day):
        d = str(int(day))
        pattern = re.compile(r'(\d{1,2})月' + re.escape(d) + r'(?:日|号)')
        match = pattern.search(source_text)
        if match:
            return int(match.group(1))
        return None
    
    # 第一步：处理带年份的完整日期 "YYYY年X月X日/号"
    full_year_date_pattern = re.compile(r'(\d{4})年(\d{1,2})月(\d{1,2})(?:日|号)')

    def replace_full_year_date(match):
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))
        full_expr = match.group(0)
        month_day_expr = f"{month}月{day}日"
        # 原文存在带年份的完整日期，保留
        if full_expr in source_text:
            return full_expr
        # 如果该日期明显早于新闻发布日期（旧信息），允许保留完整日期以提示时效性
        try:
            event_date_str = f"{year}-{month:02d}-{day:02d}"
            if _is_event_date_too_early({'date': event_date_str}, publish_date, threshold_days=15):
                return full_expr
        except Exception:
            pass
        # 原文有月日但无年份，只保留月日（不补全年份）
        if month_day_in_source(month, day):
            return month_day_expr
        # 与发布日期月日一致，保留月日
        if pub_month == month and pub_day == day:
            return month_day_expr
        # 无明确依据，移除整个日期
        return ''

    report = full_year_date_pattern.sub(replace_full_year_date, report)

    # 第二步：处理完整日期 "X月X日/号"
    full_date_pattern = re.compile(r'(\d{1,2})月(\d{1,2})(?:日|号)')
    
    def replace_full_date(match):
        month = int(match.group(1))
        day = int(match.group(2))
        # 原文有完整月日，保留
        if month_day_in_source(month, day):
            return match.group(0)
        # 原文没有完整月日，但与发布日期月日一致，保留（基于发布日期）
        if pub_month == month and pub_day == day:
            return match.group(0)
        # 无明确依据，视为幻觉日期并移除
        return ''
    
    report = full_date_pattern.sub(replace_full_date, report)
    
    # 第三步：处理只有日期、没有月份的情况 "X日/号"
    # 排除前面有 "X月" 的完整日期，并避免匹配多位日期中的后几位数字
    day_only_pattern = re.compile(r'(?<!\d)(?<!\d月)(?<!\d\d月)(\d{1,2})(?:日|号)')
    
    def replace_day_only(match):
        day = int(match.group(1))
        # 优先：原文有完整月日，使用该月份补全
        month = find_month_for_day(day)
        if month:
            return f"{month}月{day}日"
        # 其次：原文有独立日期，参考发布月份补全
        if day_only_in_source(day):
            if pub_month:
                return f"{pub_month}月{day}日"
        # 无明确日期依据，移除
        return ''
    
    report = day_only_pattern.sub(replace_day_only, report)
    
    # 清理因删除日期产生的多余标点和空格
    report = re.sub(r'[，,、]{2,}', '，', report)
    report = re.sub(r'[ \t]+', ' ', report)
    report = report.strip('，,、 ')
    
    return report


def _is_valid(value):
    """检查值是否有效（不为None且不为空字符串）。"""
    return value is not None and str(value).strip() not in ['', 'None', 'null', 'NULL', 'nan', 'NaN']


# 各主标签在 data transaction 中对应的主体字段
_CATEGORY_ENTITY_FIELDS = {
    "新造船订单": ("shipowner", "shipyard"),
    "船舶建造": ("shipowner", "shipyard"),
    "买卖船": ("buyer", "seller"),
    "租船": ("charterer", "owner"),
    "维修改装": ("shipowner", "facility"),
    "公司金融": ("parties", "company"),
}


def _get_entity_fields_for_category(category):
    """返回指定主标签在 data transaction 中的主体字段名元组。"""
    return _CATEGORY_ENTITY_FIELDS.get(category, ("shipowner", "shipyard"))


def _get_entity_from_transaction(entity_item, data_item, category):
    """
    从 transaction 中读取主体字段，优先使用 data_item 中的字段；
    若 data_item 中不存在，则回退到 entity_item（兼容旧格式）。
    返回包含对应字段的字典。
    """
    fields = _get_entity_fields_for_category(category)
    result = {}
    for field in fields:
        value = None
        if data_item and _is_valid(data_item.get(field)):
            value = data_item.get(field)
        elif entity_item and _is_valid(entity_item.get(field)):
            value = entity_item.get(field)
        if value is not None:
            result[field] = value
    return result


def _format_event_date_text(event_date_raw: str, include_year: bool = False, source_text: str = "", news_year: int = None) -> str:
    """把事件日期统一格式化为中文日期显示文本。
    默认只输出月日（如 '3月15日'）；当 include_year=True 时输出完整年份（如 '2025年3月15日'）。
    支持 YYYY-MM-DD、YYYY-MM、M月D日、MM-DD 等格式；YYYY-MM 只输出月份（如 '3月'）。
    对于词表格式（上半年/第N季度/QN等），若年份与 news_year 相同则省略年份。
    """
    if not _is_valid(event_date_raw):
        return ""
    s = str(event_date_raw).strip()
    source_text = str(source_text or "")

    year = None
    month = None
    day = None

    # 优先匹配 YYYY-MM-DD / YYYY/MM/DD / YYYY年M月D日
    m = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?', s)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        # 匹配 YYYY-MM / YYYY/MM / YYYY年M月
        m = re.search(r'(\d{4})[-/年](\d{1,2})(?:月|$)', s)
        if m:
            year, month = int(m.group(1)), int(m.group(2))
        else:
            # 匹配 M月D日
            m = re.search(r'(\d{1,2})月(\d{1,2})日?', s)
            if m:
                month, day = int(m.group(1)), int(m.group(2))
            else:
                # 匹配 MM-DD / MM/DD
                m = re.search(r'(\d{1,2})[-/](\d{1,2})', s)
                if m:
                    month, day = int(m.group(1)), int(m.group(2))

    # 词表格式（YYYY年/上半年/下半年/第N季度/QN/当日等）：年份与新闻年份相同则省略年份
    if year is None and month is None and is_allowed_date_format(s):
        if news_year is not None:
            m_year = re.match(r'(\d{4})年', s)
            if m_year:
                event_year = int(m_year.group(1))
                if event_year == news_year:
                    return re.sub(r'^\d{4}年', '', s)
        return s

    if month is None:
        return ""

    # 需要补全年份时：优先用日期字符串本身提取到的年份；否则从原文中查找对应月日前后的年份
    if include_year and year is None and source_text:
        # 在原文中查找 "YYYY年M月D日" 或 "YYYY年M月"
        patterns = [
            re.compile(r'(\d{4})年' + re.escape(f"{month}月{day}日" if day else f"{month}月")),
            re.compile(r'(\d{4})\s*年\s*' + re.escape(f"{month}") + r'\s*月'),
        ]
        for pat in patterns:
            mm = pat.search(source_text)
            if mm:
                year = int(mm.group(1))
                break
        # 兜底：搜索原文中所有 4 位年份，取与发布年份不同且最可能相关的一个
        if year is None:
            years_in_source = [int(y) for y in re.findall(r'(\d{4})年', source_text) if 1990 <= int(y) <= 2100]
            if years_in_source:
                year = years_in_source[0]

    if day is None:
        # YYYY-MM 格式只输出月份
        if year and include_year:
            return f"{year}年{month}月"
        return f"{month}月"

    if year and include_year:
        return f"{year}年{month}月{day}日"
    return f"{month}月{day}日"


def _get_report_date_text(data_item, news_date, source_text: str = "", include_date: bool = True) -> str:
    """
    获取报道开头应使用的日期文本。
    优先使用 data_item 中的交易事件日期；没有则使用新闻发布日期。
    事件日期年份与新闻发布年份相同时省略年份，只输出月日（或季度/半年等）；
    事件日期年份与新闻发布年份不同时保留年份。
    当 include_date=False 时（同主标签后续相似交易），不输出日期前缀。
    """
    if not include_date:
        return ""

    news_year = None
    if _is_valid(news_date):
        news_year_match = re.search(r'(\d{4})', str(news_date))
        if news_year_match:
            news_year = int(news_year_match.group(1))

    event_date_raw = data_item.get('date') if data_item else None
    if _is_valid(event_date_raw):
        event_date_str = str(event_date_raw).strip()
        event_year = None
        year_match = re.search(r'(\d{4})', event_date_str)
        if year_match:
            event_year = int(year_match.group(1))
        # 年份与新闻年份不同则保留年份；若事件日期明显早于新闻发布日期（旧信息），即使同年也保留年份
        is_past_event = _is_event_date_too_early(data_item, news_date, threshold_days=15)
        include_year = (
            (event_year is not None and news_year is not None and event_year != news_year)
            or is_past_event
        )
        date_text = _format_event_date_text(
            event_date_raw, include_year=include_year, source_text=source_text, news_year=news_year
        )
        if date_text:
            return date_text
    if _is_valid(news_date):
        return _format_event_date_text(news_date, include_year=False, news_year=news_year)
    return ""


def _is_event_date_too_early(data_item, news_date, threshold_days: int = 15) -> bool:
    """
    判断 data_item 中的 date 是否明显早于 news_date（旧信息）。
    支持 YYYY-MM-DD 和 YYYY-MM 两种格式：
      - 若事件日期带日，按天数差 > threshold_days 判断；
      - 若事件日期只到月份，当事件年月早于新闻年月时也视为旧信息。
    """
    if not data_item or not news_date:
        return False
    event_date_raw = data_item.get('date')
    if not _is_valid(event_date_raw):
        return False

    event_date = str(event_date_raw).strip()
    news_date_parsed = parse_date(str(news_date))
    if not news_date_parsed:
        return False

    # YYYY-MM 格式：比较年月
    ym_match = re.match(r'(\d{4})-(\d{1,2})$', event_date)
    if ym_match:
        try:
            event_ym = (int(ym_match.group(1)), int(ym_match.group(2)))
            news_ym = (int(news_date_parsed[:4]), int(news_date_parsed[5:7]))
            return event_ym < news_ym
        except Exception:
            return False

    # 词表格式（YYYY年/上半年/下半年/第N季度/QN/当日等）：比较年份
    if is_allowed_date_format(event_date):
        year_match = re.match(r'(\d{4})', event_date)
        if year_match:
            try:
                event_year = int(year_match.group(1))
                news_year = int(news_date_parsed[:4])
                return event_year < news_year
            except Exception:
                return False
        return False

    # YYYY-MM-DD 或其它可解析格式：按天数差判断
    from date_utils import parse_event_date
    event_date_full = parse_event_date(event_date)
    if not event_date_full:
        return False

    try:
        event_dt = datetime.strptime(event_date_full, '%Y-%m-%d')
        news_dt = datetime.strptime(news_date_parsed, '%Y-%m-%d')
    except Exception:
        return False

    return (news_dt - event_dt).days > threshold_days


def _is_order_summary_data(data):
    """判断 data 是否标记为订单汇总型报道。"""
    if not data:
        return False
    parsed = None
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except Exception:
            return False
    elif isinstance(data, dict):
        parsed = data
    if isinstance(parsed, dict):
        return parsed.get("summary_type") == "订单汇总"
    return False


def _is_vague_order_summary(data):
    """
    判断订单汇总数据是否为模糊/周期性汇总（应统一叙述），
    而非同一公告下的具体批量订单（应逐笔拆分）。
    """
    parsed = None
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except Exception:
            return False
    elif isinstance(data, dict):
        parsed = data
    if not isinstance(parsed, dict):
        return False
    if parsed.get("summary_type") != "订单汇总":
        return False

    # 只要有统计周期，即视为周期性汇总
    if _is_valid(parsed.get("period")):
        return True

    # 总数量达到阈值，视为大规模汇总
    all_quantity = str(parsed.get("all_quantity", ""))
    if all_quantity:
        try:
            num = int(re.search(r'\d+', all_quantity).group())
            if num >= 70:
                return True
        except Exception:
            pass

    # 多笔交易中过半缺乏具体订船方/造船方或签约日期，视为描述模糊的汇总
    transactions = parsed.get("transactions", [])
    if isinstance(transactions, list) and len(transactions) >= 4:
        vague_count = 0
        for tx in transactions:
            if not isinstance(tx, dict):
                continue
            has_party = _is_valid(tx.get("shipowner")) or _is_valid(tx.get("shipyard"))
            has_date = _is_valid(tx.get("date"))
            if not has_party or not has_date:
                vague_count += 1
        if vague_count >= len(transactions) * 0.5:
            return True

    return False


def _get_trade_type(data_item):
    """从单笔交易字典中提取 trade_type，兼容旧数据 is_forecast_order。"""
    if not isinstance(data_item, dict):
        return "签署订单"
    trade_type = str(data_item.get('trade_type', '')).strip()
    if trade_type in ("签署订单", "备选生效", "在手订单", "预测订单"):
        return trade_type
    # 旧数据兼容：is_forecast_order=true 视为预测订单
    if data_item.get("is_forecast_order") is True:
        return "预测订单"
    return "签署订单"


def _is_forecast_order_data(data):
    """判断 data 是否包含预测订单（尚未正式签约/生效的意向性订单）。兼容旧数据。"""
    if not data:
        return False
    parsed = None
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except Exception:
            return False
    elif isinstance(data, dict):
        parsed = data
    if not isinstance(parsed, dict):
        return False
    if parsed.get("is_forecast_order") is True:
        return True
    txs = parsed.get("transactions") if isinstance(parsed.get("transactions"), list) else []
    if not txs:
        txs = [parsed]
    return any(_get_trade_type(tx) == "预测订单" for tx in txs)


def _inject_trade_type_to_data(data_obj, trade_type):
    """为 data 对象（含 transactions 或平铺字典）的所有交易注入 trade_type。"""
    if not isinstance(data_obj, dict):
        return
    txs = data_obj.get("transactions")
    if isinstance(txs, list):
        for tx in txs:
            if isinstance(tx, dict) and not str(tx.get('trade_type', '')).strip():
                tx['trade_type'] = trade_type
    elif not str(data_obj.get('trade_type', '')).strip():
        data_obj['trade_type'] = trade_type


def _normalize_entity_to_list(value):
    """将 entity JSON 字符串/对象/数组统一为 Python 列表。兼容新的 [{main_tag, entity}] 分组格式。"""
    if not value:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
    else:
        parsed = value
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        # 新的 [{main_tag, entity}] 分组格式：展开内部 entity
        if parsed and isinstance(parsed[0], dict) and "main_tag" in parsed[0] and "entity" in parsed[0]:
            result = []
            for group in parsed:
                if not isinstance(group, dict):
                    continue
                inner = group.get("entity", "")
                if isinstance(inner, str):
                    try:
                        inner = json.loads(inner)
                    except Exception:
                        continue
                if isinstance(inner, dict):
                    result.append(inner)
                elif isinstance(inner, list):
                    result.extend([item for item in inner if isinstance(item, dict)])
            return result
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _report_contains_entities(report, entity, data=None, main_tag=None):
    """
    检查 report 是否包含交易主体（shipowner/buyer/seller/charterer/owner 等）。
    优先从 data.transactions 中提取主体；若 data 中无主体，则回退到 entity（兼容旧格式）。
    若明确存在主体但 report 中缺失，返回 False。
    """
    if not report:
        return False

    report_lower = str(report).lower()
    entity_keys = {'shipowner', 'buyer', 'seller', 'charterer', 'owner', 'operator'}

    # 优先从 data 中提取主体
    if data:
        data_transactions, _, _ = _get_data_transactions(data)
        for tx in data_transactions:
            if not isinstance(tx, dict):
                continue
            for key, val in tx.items():
                if key.lower() in entity_keys and _is_valid(val):
                    val_str = str(val).strip().lower()
                    if val_str and val_str not in report_lower:
                        return False

    # 回退到旧格式 entity
    entity_list = _normalize_entity_to_list(entity)
    for item in entity_list:
        if not isinstance(item, dict):
            continue
        for key, val in item.items():
            if key.lower() in entity_keys and _is_valid(val):
                val_str = str(val).strip().lower()
                if val_str and val_str not in report_lower:
                    return False
    return True


def _get_data_transactions(data):
    """
    解析 data JSON，返回 (transactions_list, all_quantity, all_price)。
    支持形式：
      1. 单笔交易：平铺字典 -> ([dict], None, None)
      2. 多笔交易（新格式）：{ "transactions": [...], "all_quantity": ..., "all_price": ... }
      3. 多笔交易（旧格式）：[dict, dict, ...] -> (list, None, None)
      4. 新的 [{main_tag, data}] 分组格式 -> 展开内部 data 再解析
      5. AI 偶发把 summary dict 包在单元素数组里：[{ "transactions": [...], ... }]
    """
    if not data:
        return [], None, None
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return [], None, None
    else:
        parsed = data

    # 新的 [{main_tag, data}] 分组格式：展开内部 data
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "main_tag" in parsed[0] and "data" in parsed[0]:
        group = parsed[0]
        inner = group.get("data", "")
        if isinstance(inner, str):
            try:
                parsed = json.loads(inner)
            except Exception:
                return [], None, None
        else:
            parsed = inner

    # 兼容 AI 偶发把 summary dict 包在单元素数组里的情况，如
    # [{"all_quantity": "3", "transactions": [...]}]
    # 否则该 dict 会被当作单笔交易，导致船型/数量/交付时间等全部丢失。
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict) and "transactions" in parsed[0]:
        parsed = parsed[0]

    if isinstance(parsed, dict):
        if "transactions" in parsed and isinstance(parsed["transactions"], list):
            transactions = [
                item for item in parsed["transactions"]
                if isinstance(item, dict) and item.get('is_duplicate') is not True
            ]
            all_quantity = str(parsed.get("all_quantity", "")) if parsed.get("all_quantity") is not None else ""
            all_price = str(parsed.get("all_price", "")) if parsed.get("all_price") is not None else ""
            return transactions, all_quantity, all_price
        # 单笔平铺字典：若本身被标记为重复则返回空
        if parsed.get('is_duplicate') is True:
            return [], None, None
        return [parsed], None, None

    if isinstance(parsed, list):
        transactions = [
            item for item in parsed
            if isinstance(item, dict) and item.get('is_duplicate') is not True
        ]
        return transactions, None, None

    return [], None, None


def _try_merge_order_transactions(data_transactions, all_price=None, all_quantity=None):
    """
    尝试合并高度相似的新造船订单交易。
    以 shipowner / shipyard / ship_type / date 四个维度为准：
      - date 维度必须相同；date 不同的交易即使其他维度相同也拆分为不同报道。
      - 在 date 相同的前提下，shipowner / shipyard / ship_type 中至少有一个维度相同即可合并。
      - 合并后相同维度只出现一次，不同维度按交易顺序用"、"和"和"组合。
    all_price/all_quantity 为外层统一总计，合并时优先使用。
    安全兜底：若任一交易的 shipowner 或 shipyard 为空，说明可能仍是旧格式或主体缺失，
    此时不合并，避免把不同主体的订单错误合并。
    """
    if len(data_transactions) <= 1:
        return data_transactions

    # 安全兜底：主体字段缺失时不合并
    for tx in data_transactions:
        if not _is_valid(tx.get('shipowner')) or not _is_valid(tx.get('shipyard')):
            return data_transactions

    dims = ['shipowner', 'shipyard', 'ship_type', 'date']

    def _normalize(val):
        return str(val).strip() if val is not None else ""

    def _tx_norm(tx):
        return {d: _normalize(tx.get(d)) for d in dims}

    # 聚类：每笔交易最多进入一个组；
    # 加入条件：与组内所有成员 date 相同，且四个维度中最多有 2 个不同（即 date + 至少一个其他维度相同）
    groups = []
    for tx in data_transactions:
        tx_n = _tx_norm(tx)
        placed = False
        for group in groups:
            can_join = True
            for member in group:
                mn = _tx_norm(member)
                diff_count = sum(
                    1 for d in dims
                    if tx_n[d] != mn[d] and (tx_n[d] or mn[d])
                )
                # date 不同直接拒绝；其他维度最多允许 2 个不同（date + 最多 1 个其他维度）
                if tx_n['date'] != mn['date'] or diff_count > 2:
                    can_join = False
                    break
            if can_join:
                group.append(tx)
                placed = True
                break
        if not placed:
            groups.append([tx])

    merged = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
            continue

        # 计算各维度取值
        values = {d: [] for d in dims}
        for tx in group:
            n = _tx_norm(tx)
            for d in dims:
                values[d].append(n[d])

        same_dims = {}
        diff_dims = []
        diff_values = {}
        for d in dims:
            unique_vals = list(dict.fromkeys([v for v in values[d] if v]))
            if len(unique_vals) == 1:
                same_dims[d] = unique_vals[0]
            elif len(unique_vals) > 1:
                diff_dims.append(d)
                diff_values[d] = unique_vals

        merged_tx = group[0].copy()
        for d, v in same_dims.items():
            merged_tx[d] = v
        for d in diff_dims:
            merged_tx[d] = diff_values[d]

        merged_tx['_merge_type'] = 'merged_orders'
        merged_tx['_merged_transactions'] = group
        merged_tx['_same_dims'] = same_dims
        merged_tx['_diff_dims'] = diff_dims
        merged_tx['_diff_values'] = diff_values

        merged_tx['_merged_quantities'] = [t.get('quantity') for t in group]
        merged_tx['_merged_prices'] = [t.get('price') for t in group]
        merged_tx['_merged_deliveries'] = [t.get('delivery') for t in group]
        merged_tx['_merged_specs'] = [t.get('specs') for t in group]

        # 尝试计算总数量
        try:
            total = 0
            for q in merged_tx['_merged_quantities']:
                if q is not None:
                    total += int(str(q).strip())
            merged_tx['quantity'] = str(total)
        except Exception:
            pass

        if _is_valid(all_price):
            merged_tx['_all_price'] = all_price
        if _is_valid(all_quantity):
            merged_tx['_all_quantity'] = all_quantity

        merged.append(merged_tx)

    return merged


def _format_order_transaction(entity_item, data_item, news_date=None, source_text="", include_date=True, is_forecast=False):
    """格式化单笔新造船订单交易描述。优先从 data_item 读取 shipowner/shipyard，
    缺失时回退到 entity_item（兼容旧格式）。同时支持同一船东多笔相似订单的合并输出。
    include_date=False 时省略开头日期前缀，用于同主标签后续相似交易。
    is_forecast=True 时表示预测订单，省略日期并使用不确定性措辞（已逐步由 trade_type 替代，保留参数仅作兼容）。
    """
    entity_fields = _get_entity_from_transaction(entity_item, data_item, "新造船订单")
    shipowner = entity_fields.get('shipowner', '')
    shipyard = entity_fields.get('shipyard', '')
    if isinstance(shipowner, list):
        shipowner = shipowner[0] if shipowner else ''
    if isinstance(shipyard, list):
        shipyard = shipyard[0] if shipyard else ''

    ship_type = data_item.get('ship_type', '') if data_item else ''
    quantity = str(data_item.get('quantity', '')) if data_item and data_item.get('quantity') is not None else ''
    price = data_item.get('price', '') if data_item else ''
    delivery = data_item.get('delivery', '') if data_item else ''
    specs = data_item.get('specs', '') if data_item else ''

    # 按交易类型确定时态与措辞
    trade_type = _get_trade_type(data_item)
    is_forecast = (trade_type == "预测订单") or (is_forecast is True)
    is_backlog_order = (trade_type == "在手订单")

    # 判断是否为过去订单（交易日期早于新闻发布日期15天以上）
    is_past_order = is_backlog_order or _is_event_date_too_early(data_item, news_date, threshold_days=15)

    merge_type = data_item.get('_merge_type') if data_item else None

    # ================= 合并模式：date 相同，允许最多两个非 date 维度不同 =================
    if merge_type == 'merged_orders':
        same_dims = data_item.get('_same_dims', {}) or {}
        diff_dims = data_item.get('_diff_dims', []) or []
        diff_values = data_item.get('_diff_values', {}) or {}
        merged_quantities = data_item.get('_merged_quantities', []) or []
        merged_prices = data_item.get('_merged_prices', []) or []
        merged_deliveries = data_item.get('_merged_deliveries', []) or []
        merged_specs = data_item.get('_merged_specs', []) or []
        merged_transactions = data_item.get('_merged_transactions', []) or []
        all_price_merged = data_item.get('_all_price')

        past_marker = "曾" if is_past_order else ""

        # 在手订单不输出日期前缀；预测订单若是已见诸报道的过去信息，仍保留年月前缀
        skip_date_prefix = is_backlog_order or (is_forecast and not is_past_order)

        # 无差异维度时的兜底
        if not diff_dims:
            fallback = merged_transactions[0] if merged_transactions else data_item
            return _format_order_transaction(entity_item, fallback, news_date, source_text)

        # 若 data 中未提取到船东/船厂，从 entity_item 兜底（避免合并后丢失主语）
        fallback_fields = _get_entity_from_transaction(entity_item, {}, "新造船订单")
        common_shipowner = same_dims.get('shipowner', '') or fallback_fields.get('shipowner', '')
        common_shipyard = same_dims.get('shipyard', '') or fallback_fields.get('shipyard', '')
        common_ship_type = same_dims.get('ship_type', '')

        def _flatten_for_join(items):
            """把列表中内部包含'、'或'和'的字符串拆平，再交给 _join_list 合并，
            保证最终格式只有最后一个用'和'，前面用'、'。"""
            flat = []
            for item in items:
                s = str(item).strip()
                if not s:
                    continue
                if '和' in s or '、' in s:
                    parts = [p.strip() for p in re.split(r'[、和]', s) if p.strip()]
                    flat.extend(parts)
                else:
                    flat.append(s)
            return flat

        def _join_list(items, sep1="、", sep2="、"):
            """合并列表项，并列项之间统一使用'、'连接。"""
            items = [str(i) for i in items if _is_valid(i)]
            if len(items) > 1:
                return sep1.join(items)
            elif items:
                return items[0]
            return ""

        def _type_full_text(q, spc, stype):
            parts = []
            if _is_valid(q):
                parts.append(f"{q}艘")
            if _is_valid(spc):
                parts.append(spc)
            if _is_valid(stype):
                parts.append(stype)
            return "".join(parts)

        def _values_all_same(values):
            if not values:
                return False
            normalized = [str(v).strip() for v in values if _is_valid(v)]
            return len(normalized) > 0 and len(set(normalized)) == 1

        parts = []

        # ---- 日期前缀 ----
        # 在手订单省略具体日期；预测订单若是已见诸报道的过去信息（is_past_order=True），仍保留年月前缀
        date_text = "" if skip_date_prefix else _get_report_date_text(data_item, news_date, source_text=source_text, include_date=include_date)
        if date_text:
            parts.append(f"{date_text}，")

        has_common_owner = _is_valid(common_shipowner)
        has_common_yard = _is_valid(common_shipyard)
        has_common_type = _is_valid(common_ship_type)

        if len(diff_dims) == 1:
            diff_dim = diff_dims[0]
            if diff_dim == 'shipowner':
                owners = diff_values.get('shipowner', [])
                qty_same = _values_all_same(merged_quantities)
                if qty_same and _is_valid(common_ship_type):
                    qty_text = _type_full_text(merged_quantities[0], "", "") if merged_quantities else ""
                    parts.append(f"{_join_list(owners)}{past_marker}分别向{common_shipyard}订购{qty_text}{common_ship_type}")
                elif qty_same:
                    qty_text = _type_full_text(merged_quantities[0], "", "") if merged_quantities else ""
                    parts.append(f"{_join_list(owners)}{past_marker}分别向{common_shipyard}订购{qty_text}")
                else:
                    type_text = _type_full_text("", "", common_ship_type) if _is_valid(common_ship_type) else ""
                    qty_items = [_type_full_text(q, "", type_text) for q in merged_quantities]
                    parts.append(f"{_join_list(owners)}{past_marker}分别向{common_shipyard}订购{_join_list(qty_items)}")

            elif diff_dim == 'shipyard':
                yards = diff_values.get('shipyard', [])
                qty_same = _values_all_same(merged_quantities)
                if qty_same and _is_valid(common_ship_type):
                    qty_text = _type_full_text(merged_quantities[0], "", "") if merged_quantities else ""
                    parts.append(f"{common_shipowner}{past_marker}分别向{_join_list(yards)}订购{qty_text}{common_ship_type}")
                elif qty_same:
                    qty_text = _type_full_text(merged_quantities[0], "", "") if merged_quantities else ""
                    parts.append(f"{common_shipowner}{past_marker}分别向{_join_list(yards)}订购{qty_text}")
                else:
                    type_text = _type_full_text("", "", common_ship_type) if _is_valid(common_ship_type) else ""
                    qty_items = [_type_full_text(q, "", type_text) for q in merged_quantities]
                    parts.append(f"{common_shipowner}{past_marker}分别向{_join_list(yards)}订购{_join_list(qty_items)}")

            elif diff_dim == 'ship_type':
                type_items = []
                for i, tx in enumerate(merged_transactions):
                    q = merged_quantities[i] if i < len(merged_quantities) else None
                    sp = merged_specs[i] if i < len(merged_specs) else None
                    st = tx.get('ship_type', '') if i < len(merged_transactions) else ''
                    txt = _type_full_text(q, sp, st)
                    if txt:
                        type_items.append(txt)
                parts.append(f"{common_shipowner}{past_marker}向{common_shipyard}分别订购{_join_list(type_items)}")

        else:
            # 两个不同维度：按交易顺序一一对应生成并列项
            if has_common_owner:
                # 船东相同，船厂和船型不同
                yard_items = []
                type_items = []
                for i, tx in enumerate(merged_transactions):
                    yard = tx.get('shipyard', '')
                    q = merged_quantities[i] if i < len(merged_quantities) else None
                    sp = merged_specs[i] if i < len(merged_specs) else None
                    st = tx.get('ship_type', '')
                    yard_items.append(yard)
                    type_items.append(_type_full_text(q, sp, st) or "船舶")
                parts.append(f"{common_shipowner}{past_marker}分别向{_join_list(yard_items)}订购{_join_list(type_items)}")

            elif has_common_yard:
                # 船厂相同，船东和船型不同
                owner_items = []
                type_items = []
                for i, tx in enumerate(merged_transactions):
                    owner = tx.get('shipowner', '')
                    q = merged_quantities[i] if i < len(merged_quantities) else None
                    sp = merged_specs[i] if i < len(merged_specs) else None
                    st = tx.get('ship_type', '')
                    owner_items.append(owner)
                    type_items.append(_type_full_text(q, sp, st) or "船舶")
                parts.append(f"{_join_list(owner_items)}{past_marker}分别向{common_shipyard}订购{_join_list(type_items)}")

            elif has_common_type:
                # 船型相同，船东和船厂不同
                owner_yard_items = []
                for i, tx in enumerate(merged_transactions):
                    owner = tx.get('shipowner', '')
                    yard = tx.get('shipyard', '')
                    item = ""
                    if _is_valid(owner):
                        item += owner
                    if _is_valid(yard):
                        item += f"向{yard}"
                    owner_yard_items.append(item)
                qty_same = _values_all_same(merged_quantities)
                if qty_same:
                    qty_text = _type_full_text(merged_quantities[0], "", "") if merged_quantities else ""
                    parts.append(f"{_join_list(owner_yard_items)}{past_marker}分别订购{qty_text}{common_ship_type}")
                else:
                    qty_items = [_type_full_text(q, "", "") for q in merged_quantities]
                    parts.append(f"{_join_list(owner_yard_items)}{past_marker}分别订购{_join_list(qty_items)}{common_ship_type}")

            else:
                # 理论上不会出现（date 相同且至少还有一个维度相同才会合并）
                item_texts = []
                for i, tx in enumerate(merged_transactions):
                    owner = tx.get('shipowner', '')
                    yard = tx.get('shipyard', '')
                    q = merged_quantities[i] if i < len(merged_quantities) else None
                    sp = merged_specs[i] if i < len(merged_specs) else None
                    st = tx.get('ship_type', '')
                    txt = ""
                    if _is_valid(owner):
                        txt += owner
                    if _is_valid(yard):
                        txt += f"向{yard}"
                    type_text = _type_full_text(q, sp, st)
                    if type_text:
                        txt += f"订购{type_text}"
                    elif _is_valid(q):
                        txt += f"订购{q}艘船"
                    else:
                        txt += "订购"
                    if txt:
                        item_texts.append(txt)
                parts.append(_join_list(item_texts))

        # ---- 价格 ----
        if _is_valid(all_price_merged):
            parts.append(f"，合同金额约{all_price_merged}")
        else:
            valid_prices = [p for p in merged_prices if _is_valid(p)]
            if valid_prices:
                parts.append(f"，合同金额分别为{_join_list(valid_prices)}")

        # ---- 交付时间 ----
        valid_deliveries = list(dict.fromkeys([d for d in merged_deliveries if _is_valid(d)]))
        # 若某个 delivery 字段内部已包含"和"/"、"（如"2028年11月和2029年1月"），先拆平再合并，
        # 确保最终格式为"A、B和C"，不会出现"A和B和C"。
        valid_deliveries = _flatten_for_join(valid_deliveries)
        if valid_deliveries:
            parts.append(f"，{_join_list(valid_deliveries)}交付")

        return "".join(parts)

    # ================= 默认：单笔交易格式化 =================
    parts = []
    # 日期放在开头：优先使用交易日期，否则使用新闻日期；在手订单省略日期；
    # 预测订单若是已见诸报道的过去信息（is_past_order=True），仍保留年月前缀以提示时效性
    if not is_backlog_order and (not is_forecast or is_past_order):
        date_text = _get_report_date_text(data_item, news_date, source_text=source_text, include_date=include_date)
        if date_text:
            parts.append(f"{date_text}，")

    # 主体部分：船东 + 船厂；过去订单在船东后加"曾"作为时间助词
    if _is_valid(shipowner) and _is_valid(shipyard):
        past_marker = "曾" if is_past_order else ""
        parts.append(f"{shipowner}{past_marker}向{shipyard}")
    elif _is_valid(shipowner):
        parts.append(f"{shipowner}")
    elif _is_valid(shipyard):
        parts.append(f"{shipyard}")

    # 动作：预测订单使用不确定性措辞
    order_verb = "拟订购" if is_forecast else "订购"
    if _is_valid(quantity) and _is_valid(ship_type):
        if _is_valid(specs):
            parts.append(f"{order_verb}{quantity}艘{specs}{ship_type}")
        else:
            parts.append(f"{order_verb}{quantity}艘{ship_type}")
    elif _is_valid(ship_type):
        if _is_valid(specs):
            parts.append(f"{order_verb}{specs}{ship_type}")
        else:
            parts.append(f"{order_verb}{ship_type}")
    elif _is_valid(quantity):
        parts.append(f"{order_verb}{quantity}艘船")

    # 金额（预测订单的不确定性仅体现在开头动词，金额/交付时间照常表述）
    if _is_valid(price):
        parts.append(f"，合同金额{price}")

    # 交付时间（不再硬编码“预计”，避免与 delivery 字段原文中的“预计”重复）
    if _is_valid(delivery):
        parts.append(f"，{delivery}交付")

    return "".join(parts)


def _combine_entity_items(entity_list):
    """把多个 entity 字典中的 shipowner/shipyard 合并成一个字典（用于多主体单交易场景），并去重。"""
    shipowners = []
    shipyards = []
    for item in entity_list:
        if not isinstance(item, dict):
            continue
        so = item.get('shipowner', '')
        sy = item.get('shipyard', '')
        if _is_valid(so) and str(so) not in shipowners:
            shipowners.append(str(so))
        if _is_valid(sy) and str(sy) not in shipyards:
            shipyards.append(str(sy))
    return {
        "shipowner": "、".join(shipowners) if shipowners else "",
        "shipyard": "、".join(shipyards) if shipyards else "",
    }


def parse_ship_order_report(entity, data, news_date=None, source_text=""):
    """
    直接解析entity和data字段，生成新造船订单的新闻报告。
    支持单笔交易（平铺字典）或多笔交易（transactions数组 + all_quantity/all_price）。
    特别处理：多个entity但data只有一笔交易时，把所有主体合并为一条描述。
    """
    entity_list = _normalize_entity_to_list(entity)
    data_transactions, all_quantity, all_price = _get_data_transactions(data)

    # 如果 entity 或 data 存在但无法解析，回退到正则/文本提取
    if (entity and not entity_list and not _is_valid_json(entity)) or (data and not data_transactions and not _is_valid_json(data)):
        return _parse_ship_order_report_fallback(entity, data)

    # 取消合并机制：每条订单交易独立生成报道
    # data_transactions = _try_merge_order_transactions(data_transactions, all_price=all_price, all_quantity=all_quantity)

    transaction_parts = []

    # 每笔交易按自身的 trade_type 决定时态与日期前缀；
    # _format_order_transaction 内部已根据 trade_type 处理预测订单/在手订单的日期省略。
    def _fmt_order(entity_item, data_item, idx):
        return _format_order_transaction(
            entity_item, data_item, news_date=news_date, source_text=source_text,
            include_date=True
        )

    # 常见异常情况：多 entity 但单 data transaction -> 合并 entity 后用同一 data 生成一条报告
    if len(entity_list) > 1 and len(data_transactions) == 1:
        if _can_merge_entities(entity_list):
            combined_entity = _combine_entity_items(entity_list)
            desc = _fmt_order(combined_entity, data_transactions[0] if data_transactions else {}, 0)
            if desc:
                transaction_parts.append(desc)
        else:
            # 主体关联性不足，避免把整篇新闻的公司都列为订船方/船厂
            # 仅使用第一个 entity 生成报告，其余视为可能的多笔交易但被 data 遗漏
            print(f"   ⚠️ 多entity单data且主体关联性不足（可能提取过宽），仅使用第一个entity生成报告")
            desc = _fmt_order(entity_list[0], data_transactions[0] if data_transactions else {}, 0)
            if desc:
                transaction_parts.append(desc)
    elif len(entity_list) == len(data_transactions):
        for idx, (entity_item, data_item) in enumerate(zip(entity_list, data_transactions)):
            desc = _fmt_order(entity_item, data_item, idx)
            if desc:
                transaction_parts.append(desc)
    else:
        # 其它长度不一致情况：按 data_transactions 循环，缺失的 entity 用所有已知主体合并填充，
        # 避免合并后 entity 数量多于 data 数量而产生残句（如"7月9日，东太平洋航运向招商威海。"）
        combined_entity = _combine_entity_items(entity_list) if entity_list else {}
        for idx, data_item in enumerate(data_transactions):
            if idx < len(entity_list):
                entity_item = entity_list[idx]
            elif combined_entity:
                entity_item = combined_entity
            else:
                entity_item = {}
            desc = _fmt_order(entity_item, data_item, idx)
            if desc:
                transaction_parts.append(desc)

    if not transaction_parts:
        return clean_news_report(f"{entity}；{data}")

    # 组装报告
    if len(transaction_parts) == 1:
        report = transaction_parts[0] + "。"
    else:
        report = "\n\n".join([p + "。" for p in transaction_parts])
        # 如果有统一总计数量/金额，追加到末尾
        summary_parts = []
        if _is_valid(all_quantity):
            summary_parts.append(f"共{all_quantity}艘")
        if _is_valid(all_price):
            summary_parts.append(f"总金额{all_price}")
        if summary_parts:
            report += "\n\n（" + "，".join(summary_parts) + ")。"

    report = report.replace("。。", "。")
    report = clean_news_report(report)
    report = re.sub(r'[ \t]+', ' ', report)
    return report


def parse_order_summary_report(entity, data, news_date=None, source_text=""):
    """
    解析订单汇总型 data，生成汇总式报道（统一叙述，不拆分单笔订单）。
    适用于周期性订单统计/汇总报道（如"上周订单"、"X月新船订单汇总"、"克拉克森订单周报"），
    以及作为行业新闻呈现的订单统计。

    输出规则：
    - 日期强制放在开头，无新闻日期时用当前日期兜底；
    - 来源与周期自然拼接；
    - 不再逐条列出单笔订单，而是将船型/数量/地区分布合并为一段统一叙述。
    """
    data_transactions, all_quantity, all_price = _get_data_transactions(data)

    parsed = {}
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except Exception:
            parsed = {}
    elif isinstance(data, dict):
        parsed = data

    source = parsed.get('source', '') if isinstance(parsed, dict) else ''
    period = parsed.get('period', '') if isinstance(parsed, dict) else ''

    # 日期放在开头：订单汇总使用新闻发布日期；无新闻日期则用当前日期兜底
    date_text = _get_report_date_text(None, news_date, source_text=source_text)
    if not date_text:
        now = datetime.now()
        date_text = f"{now.month}月{now.day}日"

    # 按地区/船厂汇总
    region_summary = parsed.get('region_summary') if isinstance(parsed, dict) else None
    region_parts = []
    if isinstance(region_summary, list):
        for r in region_summary:
            if isinstance(r, dict):
                region = r.get('region', '')
                qty = r.get('quantity', '')
                if _is_valid(region) and _is_valid(qty):
                    region_parts.append(f"{region}{qty}艘")

    # 统一叙述：按船型+数量聚合，不再逐条列明细
    type_parts = []
    if data_transactions:
        for tx in data_transactions:
            if not isinstance(tx, dict):
                continue
            quantity = tx.get('quantity', '')
            ship_type = tx.get('ship_type', '')
            specs = tx.get('specs', '')
            if _is_valid(quantity) and _is_valid(ship_type):
                desc = f"{quantity}艘{ship_type}"
                if _is_valid(specs):
                    desc += f"（{specs}）"
                type_parts.append(desc)
            elif _is_valid(ship_type):
                type_parts.append(ship_type)

    parts = []

    # 开头：日期 + 来源 + 周期
    opener = date_text
    source_period_parts = []
    if _is_valid(source):
        source_period_parts.append(f"据{source}")
    if _is_valid(period):
        source_period_parts.append(period)
    if source_period_parts:
        opener += "，" + "，".join(source_period_parts)

    # 总数量
    if _is_valid(all_quantity):
        parts.append(f"{opener}，全球船厂共公布新船订单{all_quantity}艘")
    else:
        parts.append(f"{opener}，全球船厂公布多艘新船订单")

    # 地区分布
    if region_parts:
        parts.append(f"订单分布为：{'、'.join(region_parts)}")

    # 船型构成（统一叙述）
    if type_parts:
        parts.append(f"主要包括{'、'.join(type_parts)}")

    # 总金额
    if _is_valid(all_price):
        parts.append(f"订单总金额约{all_price}")

    report = "。".join(parts) + "。"
    report = report.replace("。。", "。")
    report = clean_news_report(report)
    report = re.sub(r'[ \t]+', ' ', report)
    return report


def _is_valid_json(text):
    if not text or not isinstance(text, str):
        return False
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, TypeError):
        return False


def _parse_ship_order_report_fallback(entity, data):
    """当 JSON 解析失败时的回退解析逻辑。"""
    shipowner = ""
    shipyard = ""
    if entity and isinstance(entity, str):
        if "shipowner" in entity.lower() or "shipyard" in entity.lower():
            match = re.search(r"shipowner['\"\s:]+([^'\"}]+)", entity, re.IGNORECASE)
            if match:
                shipowner = match.group(1).strip()
            match = re.search(r"shipyard['\"\s:]+([^'\"}]+)", entity, re.IGNORECASE)
            if match:
                shipyard = match.group(1).strip()
        if not shipowner and not shipyard:
            clean_entity = entity.strip("{}'\"")
            lines = clean_entity.split(',')
            for line in lines:
                if '船东' in line or '航运' in line or '海运' in line or 'shipping' in line.lower():
                    shipowner = line.split(':')[-1].strip("'\"")
                if '船厂' in line or '造船' in line or 'yard' in line.lower() or '重工' in line:
                    shipyard = line.split(':')[-1].strip("'\"")

    ship_type = ""
    quantity = ""
    price = ""
    delivery = ""
    specs = ""
    if data and isinstance(data, str):
        patterns = {
            'ship_type': r"(?:ship_type|船型|船舶类型|type)['\"\s:]+([^'\"}]+)",
            'quantity': r"(?:quantity|数量|艘数|艘)['\"\s:]+([^'\"}]+)",
            'price': r"(?:price|金额|价格|合同金额|总价)['\"\s:]+([^'\"}]+)",
            'delivery': r"(?:delivery|交付|交船|delivery_date|交付时间)['\"\s:]+([^'\"}]+)",
            'specs': r"(?:specs|dwt|吨位|箱位|载重吨|规格)['\"\s:]+([^'\"}]+)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, data, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if key == 'ship_type':
                    ship_type = value
                elif key == 'quantity':
                    quantity = value
                elif key == 'price':
                    price = value
                elif key == 'delivery':
                    delivery = value
                elif key == 'specs':
                    specs = value

    parts = []
    if _is_valid(shipowner) and _is_valid(shipyard):
        parts.append(f"{shipowner}向{shipyard}")
    elif _is_valid(shipowner):
        parts.append(f"{shipowner}")
    elif _is_valid(shipyard):
        parts.append(f"{shipyard}")

    if _is_valid(quantity) and _is_valid(ship_type):
        if _is_valid(specs):
            parts.append(f"订购{quantity}艘{specs}{ship_type}")
        else:
            parts.append(f"订购{quantity}艘{ship_type}")
    elif _is_valid(ship_type):
        if _is_valid(specs):
            parts.append(f"订购{specs}{ship_type}")
        else:
            parts.append(f"订购{ship_type}")
    elif _is_valid(quantity):
        parts.append(f"订购{quantity}艘船")

    if _is_valid(price):
        parts.append(f"，合同金额{price}")
    if _is_valid(delivery):
        parts.append(f"，预计{delivery}交付")

    if parts:
        report = "".join(parts) + "。"
        report = clean_news_report(report)
        report = re.sub(r'[ \t]+', ' ', report)
        return report
    return clean_news_report(f"{entity}；{data}")


def _format_ship_sale_transaction(entity_item, data_item, news_date=None, source_text="", include_date=True):
    """格式化单笔买卖船交易描述。优先从 data_item 读取 buyer/seller，缺失时回退 entity_item。
    include_date=False 时省略开头日期前缀，用于同主标签后续相似交易。
    """
    entity_fields = _get_entity_from_transaction(entity_item, data_item, "买卖船")
    buyer = entity_fields.get('buyer', '')
    seller = entity_fields.get('seller', '')
    if isinstance(buyer, list):
        buyer = buyer[0] if buyer else ''
    if isinstance(seller, list):
        seller = seller[0] if seller else ''

    ship_type = data_item.get('ship_type', '') if data_item else ''
    quantity = str(data_item.get('quantity', '')) if data_item and data_item.get('quantity') is not None else ''
    money = data_item.get('money', '') if data_item else ''
    delivery = data_item.get('delivery', '') if data_item else ''
    specs = data_item.get('specs', '') if data_item else ''

    action = "交易"
    parts = []
    # 日期放在开头：优先使用交易日期，否则使用新闻日期
    date_text = _get_report_date_text(data_item, news_date, source_text=source_text, include_date=include_date)
    if date_text:
        parts.append(f"{date_text}，")

    if _is_valid(buyer) and _is_valid(seller):
        if buyer == seller:
            parts.append(f"{buyer}")
            action = "完成"
        else:
            parts.append(f"{buyer}向{seller}")
    elif _is_valid(buyer):
        parts.append(f"{buyer}")
    elif _is_valid(seller):
        parts.append(f"{seller}")

    if _is_valid(quantity) and _is_valid(ship_type):
        if _is_valid(specs):
            parts.append(f"{action}{quantity}艘{specs}{ship_type}")
        else:
            parts.append(f"{action}{quantity}艘{ship_type}")
    elif _is_valid(ship_type):
        if _is_valid(specs):
            parts.append(f"{action}{specs}{ship_type}")
        else:
            parts.append(f"{action}{ship_type}")
    elif _is_valid(quantity):
        parts.append(f"{action}{quantity}艘船")
    elif _is_valid(action):
        parts.append(f"进行船舶{action}")

    if _is_valid(money):
        parts.append(f"，金额{money}")
    if _is_valid(delivery):
        parts.append(f"，预计{delivery}交付")

    return "".join(parts)


def parse_ship_sale_report(entity, data, news_date=None, source_text=""):
    """
    直接解析买卖船的entity和data字段，生成新闻报告。
    支持单笔交易（平铺字典）或多笔交易（transactions数组 + all_quantity/all_price）。
    """
    entity_list = _normalize_entity_to_list(entity)
    data_transactions, all_quantity, all_price = _get_data_transactions(data)

    if (entity and not entity_list and not _is_valid_json(entity)) or (data and not data_transactions and not _is_valid_json(data)):
        return _parse_ship_sale_report_fallback(entity, data, source_text=source_text)

    transaction_parts = []
    # 单一实体对应多笔交易时，复用该实体，避免后续交易丢失主体
    single_entity = entity_list[0] if len(entity_list) == 1 else None
    max_transactions = max(len(entity_list), len(data_transactions))
    for i in range(max_transactions):
        entity_item = entity_list[i] if i < len(entity_list) else (single_entity if single_entity else {})
        data_item = data_transactions[i] if i < len(data_transactions) else {}
        # 取消合并机制：每笔交易均生成日期前缀
        desc = _format_ship_sale_transaction(
            entity_item, data_item, news_date=news_date, source_text=source_text,
            include_date=True
        )
        if desc:
            transaction_parts.append(desc)

    if not transaction_parts:
        return clean_news_report(f"{entity}；{data}")

    if max_transactions == 1:
        report = transaction_parts[0] + "。"
    else:
        report = "\n\n".join([p + "。" for p in transaction_parts])
        summary_parts = []
        if _is_valid(all_quantity):
            summary_parts.append(f"共{all_quantity}艘")
        if _is_valid(all_price):
            summary_parts.append(f"总金额{all_price}")
        if summary_parts:
            report += "\n（" + "，".join(summary_parts) + "）。"

    report = report.replace("。。", "。")
    report = clean_news_report(report)
    report = re.sub(r'[ \t]+', ' ', report)
    return report


def _parse_ship_sale_report_fallback(entity, data, source_text=""):
    """当 JSON 解析失败时的回退解析逻辑（买卖船）。"""
    buyer = ""
    seller = ""
    if entity and isinstance(entity, str):
        if "buyer" in entity.lower() or "seller" in entity.lower():
            match = re.search(r"buyer['\"\s:]+([^'\"}]+)", entity, re.IGNORECASE)
            if match:
                buyer = match.group(1).strip()
            match = re.search(r"seller['\"\s:]+([^'\"}]+)", entity, re.IGNORECASE)
            if match:
                seller = match.group(1).strip()

    ship_type = ""
    quantity = ""
    money = ""
    delivery = ""
    specs = ""
    if data and isinstance(data, str):
        patterns = {
            'ship_type': r"(?:ship_type|船型|船舶类型|type)['\"\s:]+([^'\"}]+)",
            'quantity': r"(?:quantity|数量|艘数|艘)['\"\s:]+([^'\"}]+)",
            'money': r"(?:money|金额|价格|合同金额|总价|租金)['\"\s:]+([^'\"}]+)",
            'delivery': r"(?:delivery|交付|起租|delivery_date|交付时间|起租时间)['\"\s:]+([^'\"}]+)",
            'specs': r"(?:specs|dwt|吨位|箱位|载重吨|规格)['\"\s:]+([^'\"}]+)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, data, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if key == 'ship_type':
                    ship_type = value
                elif key == 'quantity':
                    quantity = value
                elif key == 'money':
                    money = value
                elif key == 'delivery':
                    delivery = value
                elif key == 'specs':
                    specs = value

    return _format_ship_sale_transaction(
        {"buyer": buyer, "seller": seller},
        {"ship_type": ship_type, "quantity": quantity, "money": money, "delivery": delivery, "specs": specs},
        source_text=source_text,
    ) or clean_news_report(f"{entity}；{data}")


def _format_charter_transaction(entity_item, data_item, news_date=None, source_text="", include_date=True):
    """格式化单笔租船交易描述。优先从 data_item 读取 charterer/owner，缺失时回退 entity_item。
    include_date=False 时省略开头日期前缀，用于同主标签后续相似交易。
    """
    entity_fields = _get_entity_from_transaction(entity_item, data_item, "租船")
    charterer = entity_fields.get('charterer', '')
    owner = entity_fields.get('owner', '')
    if isinstance(charterer, list):
        charterer = charterer[0] if charterer else ''
    if isinstance(owner, list):
        owner = owner[0] if owner else ''

    ship_type = data_item.get('ship_type', '') if data_item else ''
    quantity = str(data_item.get('quantity', '')) if data_item and data_item.get('quantity') is not None else ''
    money = data_item.get('money', '') if data_item else ''
    delivery = data_item.get('delivery', '') if data_item else ''
    specs = data_item.get('specs', '') if data_item else ''
    charter_type = data_item.get('charter_type', '') if data_item else ''

    action = "租用" if not _is_valid(charter_type) else charter_type
    parts = []
    # 日期放在开头：优先使用交易日期，否则使用新闻日期
    date_text = _get_report_date_text(data_item, news_date, source_text=source_text, include_date=include_date)
    if date_text:
        parts.append(f"{date_text}，")

    if _is_valid(charterer) and _is_valid(owner):
        if charterer == owner:
            parts.append(f"{charterer}")
            action = "完成"
        else:
            parts.append(f"{charterer}向{owner}")
    elif _is_valid(charterer):
        parts.append(f"{charterer}")
    elif _is_valid(owner):
        parts.append(f"{owner}")

    if _is_valid(quantity) and _is_valid(ship_type):
        if _is_valid(specs):
            parts.append(f"{action}{quantity}艘{specs}{ship_type}")
        else:
            parts.append(f"{action}{quantity}艘{ship_type}")
    elif _is_valid(ship_type):
        if _is_valid(specs):
            parts.append(f"{action}{specs}{ship_type}")
        else:
            parts.append(f"{action}{ship_type}")
    elif _is_valid(charterer) or _is_valid(owner):
        parts.append(f"进行船舶{action}")

    if _is_valid(money):
        parts.append(f"，租金{money}")
    if _is_valid(delivery):
        parts.append(f"，预计{delivery}起租/交船")

    return "".join(parts)


def parse_charter_report(entity, data, news_date=None, source_text=""):
    """
    直接解析租船的entity和data字段，生成新闻报告。
    支持单笔交易（平铺字典）或多笔交易（transactions数组 + all_quantity/all_price）。
    """
    entity_list = _normalize_entity_to_list(entity)
    data_transactions, all_quantity, all_price = _get_data_transactions(data)

    if (entity and not entity_list and not _is_valid_json(entity)) or (data and not data_transactions and not _is_valid_json(data)):
        return _parse_charter_report_fallback(entity, data, source_text=source_text)

    transaction_parts = []
    single_entity = entity_list[0] if len(entity_list) == 1 else None
    max_transactions = max(len(entity_list), len(data_transactions))
    for i in range(max_transactions):
        entity_item = entity_list[i] if i < len(entity_list) else (single_entity if single_entity else {})
        data_item = data_transactions[i] if i < len(data_transactions) else {}
        # 取消合并机制：每笔交易均生成日期前缀
        desc = _format_charter_transaction(
            entity_item, data_item, news_date=news_date, source_text=source_text,
            include_date=True
        )
        if desc:
            transaction_parts.append(desc)

    if not transaction_parts:
        return clean_news_report(f"{entity}；{data}")

    if max_transactions == 1:
        report = transaction_parts[0] + "。"
    else:
        report = "\n\n".join([p + "。" for p in transaction_parts])
        summary_parts = []
        if _is_valid(all_quantity):
            summary_parts.append(f"共{all_quantity}艘")
        if _is_valid(all_price):
            summary_parts.append(f"总租金{all_price}")
        if summary_parts:
            report += "\n（" + "，".join(summary_parts) + "）。"

    report = report.replace("。。", "。")
    report = clean_news_report(report)
    report = re.sub(r'[ \t]+', ' ', report)
    return report


def _parse_charter_report_fallback(entity, data, source_text=""):
    """当 JSON 解析失败时的回退解析逻辑（租船）。"""
    charterer = ""
    owner = ""
    if entity and isinstance(entity, str):
        if "charterer" in entity.lower() or "owner" in entity.lower():
            match = re.search(r"charterer['\"\s:]+([^'\"}]+)", entity, re.IGNORECASE)
            if match:
                charterer = match.group(1).strip()
            match = re.search(r"owner['\"\s:]+([^'\"}]+)", entity, re.IGNORECASE)
            if match:
                owner = match.group(1).strip()

    ship_type = ""
    quantity = ""
    money = ""
    delivery = ""
    specs = ""
    charter_type = ""
    if data and isinstance(data, str):
        patterns = {
            'ship_type': r"(?:ship_type|船型|船舶类型|type)['\"\s:]+([^'\"}]+)",
            'quantity': r"(?:quantity|数量|艘数|艘)['\"\s:]+([^'\"}]+)",
            'money': r"(?:money|金额|租金|日租金)['\"\s:]+([^'\"}]+)",
            'delivery': r"(?:delivery|起租|交船|delivery_date|起租时间|交船时间)['\"\s:]+([^'\"}]+)",
            'specs': r"(?:specs|dwt|吨位|箱位|载重吨|规格)['\"\s:]+([^'\"}]+)",
            'charter_type': r"(?:charter_type|租船类型|期租|光租|融资租赁)['\"\s:]+([^'\"}]+)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, data, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if key == 'ship_type':
                    ship_type = value
                elif key == 'quantity':
                    quantity = value
                elif key == 'money':
                    money = value
                elif key == 'delivery':
                    delivery = value
                elif key == 'specs':
                    specs = value
                elif key == 'charter_type':
                    charter_type = value

    return _format_charter_transaction(
        {"charterer": charterer, "owner": owner},
        {"ship_type": ship_type, "quantity": quantity, "money": money, "delivery": delivery, "specs": specs, "charter_type": charter_type},
        source_text=source_text,
    ) or clean_news_report(f"{entity}；{data}")


def _format_ship_building_transaction(entity_item, data_item, news_date=None, source_text="", include_date=True):
    """格式化单笔船舶建造（交付/节点）描述。优先从 data_item 读取 shipowner/shipyard，缺失时回退 entity_item。
    include_date=False 时省略开头日期前缀，用于同主标签后续相似交易。
    输出固定包含所有有效信息：日期、船东（如有）、船厂（如有）、event_type、规格、船型、船名。
    """
    entity_fields = _get_entity_from_transaction(entity_item, data_item, "船舶建造")
    shipowner = entity_fields.get('shipowner', '')
    shipyard = entity_fields.get('shipyard', '')
    if isinstance(shipowner, list):
        shipowner = shipowner[0] if shipowner else ''
    if isinstance(shipyard, list):
        shipyard = shipyard[0] if shipyard else ''

    ship_type = data_item.get('ship_type', '') if data_item else ''
    specs = data_item.get('specs', '') if data_item else ''
    event_type = str(data_item.get('event_type') or '').strip() if data_item else ''
    vessel_name = data_item.get('vessel_name', '') if data_item else ''

    parts = []
    # 日期放在开头：优先使用交易日期，否则使用新闻日期
    date_text = _get_report_date_text(data_item, news_date, source_text=source_text, include_date=include_date)
    if date_text:
        parts.append(f"{date_text}，")

    # 统一事件动词，默认“交付”保证向前兼容
    et = event_type or "交付"
    # "其他"是不可读的事件占位值（data 提取阶段无法归类时的选项），
    # 不代表真实事件类型，输出句子时必须省略，避免"建造的液化天然气运输船其他"
    if et == "其他":
        et = ""

    # 组合船型/规格/船名描述
    type_desc = ""
    if _is_valid(specs):
        type_desc += specs
    if _is_valid(ship_type):
        type_desc += ship_type
    if _is_valid(vessel_name):
        type_desc += f"“{vessel_name}”"

    # 主体 + 动作：固定输出所有有效主体，并把 event_type 作为事件动词
    if _is_valid(shipowner) and _is_valid(shipyard):
        if shipowner == shipyard:
            # 自主建造/集团内部交付，避免"X从X接收"
            parts.append(f"{shipowner}自主建造的{type_desc}{et}")
        else:
            # 保留“交付”事件的常用表达，其他事件使用"为...建造的...event_type"
            if et == "交付":
                parts.append(f"{shipowner}从{shipyard}接收{type_desc}")
            else:
                parts.append(f"{shipyard}为{shipowner}建造的{type_desc}{et}")
    elif _is_valid(shipowner):
        parts.append(f"{shipowner}的{type_desc}{et}")
    elif _is_valid(shipyard):
        parts.append(f"{shipyard}建造的{type_desc}{et}")
    elif type_desc:
        parts.append(f"{type_desc}{et}")
    else:
        # 没有任何有效信息时的兜底
        parts.append(f"{et}")

    return "".join(parts)


def parse_ship_building_report(entity, data, news_date=None, source_text=""):
    """
    直接解析船舶建造的entity和data字段，生成新闻报告。
    支持单笔事件（平铺字典）或多笔事件（transactions数组）。
    """
    entity_list = _normalize_entity_to_list(entity)
    data_transactions, _, _ = _get_data_transactions(data)

    transaction_parts = []
    max_transactions = max(len(entity_list), len(data_transactions))
    for i in range(max_transactions):
        entity_item = entity_list[i] if i < len(entity_list) else {}
        data_item = data_transactions[i] if i < len(data_transactions) else {}
        # 取消合并机制：每笔交易均生成日期前缀
        desc = _format_ship_building_transaction(
            entity_item, data_item, news_date=news_date, source_text=source_text,
            include_date=True
        )
        if desc:
            transaction_parts.append(desc)

    if not transaction_parts:
        return clean_news_report(f"{entity}；{data}")

    if max_transactions == 1:
        report = transaction_parts[0] + "。"
    else:
        report = "\n\n".join([p + "。" for p in transaction_parts])

    report = report.replace("。。", "。")
    report = clean_news_report(report)
    report = re.sub(r'[ \t]+', ' ', report)
    return report


async def _polish_report_with_ai(analyzer, report, news_item, main_tag="", custom_prompt=None):
    """使用 AI 对生成的新闻报告进行语序、语法润色，保持信息不丢失。"""
    if not report or not str(report).strip():
        return report

    content = (
        f"标题：{news_item.get('title', '')}\n"
        f"主维度：{main_tag}\n"
        f"实体：{news_item.get('entity', '')}\n"
        f"数据：{news_item.get('data', '')}\n"
        f"原报告：{report}"
    )

    prompt = custom_prompt if custom_prompt else REPORT_POLISH_PROMPT

    try:
        result = await analyzer.analyze(
            content=content,
            prompt=prompt,
            max_tokens=250,
            model="<your-model>",
        )
        if result.get("success"):
            polished = str(result.get("analysis", "")).strip()
            polished = clean_news_report(polished)
            if polished:
                return polished
    except Exception as e:
        print(f"   [WARN] 报告润色失败: {e}")

    return report


def _extract_entity_data_group(news_item, main_tag):
    """
    从新的数组格式 entity/data 中提取指定主维度对应的分组。
    兼容旧格式（单对象/字符串）。
    返回 (entity, data) 字符串或对象。
    """
    import json

    def _find_group(field):
        value = news_item.get(field, '')
        if not value:
            return ''
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value
        else:
            parsed = value

        if isinstance(parsed, list):
            for group in parsed:
                if isinstance(group, dict) and group.get('main_tag') == main_tag:
                    return group.get(field, '')
            # 如果找不到对应主维度，返回第一个分组
            if parsed and isinstance(parsed[0], dict):
                return parsed[0].get(field, '')
            return ''
        return value

    return _find_group('entity'), _find_group('data')


async def _generate_single_report(
    analyzer,
    news_item,
    main_tag,
    entity,
    data,
    summary,
    title,
    report_vdb,
    embedder,
    prompts,
    strict=False,
    generated_reports=None,
):
    """为单个主维度生成一次报告，返回 report 字符串或空字符串。

    Args:
        generated_reports: 已生成的报道列表，用于提醒 AI 避免重复提取相同事实。
    """
    
    temp_item = news_item.copy()
    temp_item['entity'] = entity
    temp_item['data'] = data
    temp_item['category'] = main_tag

    # 新闻发布日期，用于判断 data 中的事件时间是否明显过早
    news_date = news_item.get('date', '') if news_item else ''

    # 原文文本，用于在事件日期明显早于发布日期时补全年份
    source_text = ' '.join(str(news_item.get(field, '')) for field in ['title', 'summary', 'entity', 'data'])

    news_report_prompt, ship_order_prompt, charter_sale_prompt = prompts

    # 严格模式：追加约束
    strict_suffix = "\n【严格约束】只输出事实，不要解释、不要评价、不要出现'无法生成''无有效信息''无有效交易'等元话语，不要重复罗列。"

    # 构建已生成报道上下文，提醒 AI 避免重复
    context_suffix = ""
    if generated_reports:
        context_lines = []
        for idx, prev_report in enumerate(generated_reports, 1):
            context_lines.append(f"{idx}. {prev_report}")
        context_suffix = (
            "\n\n【已生成报道上下文】以下报道已从同一篇新闻中生成，"
            "请在生成当前主维度报道时避免重复提取相同事实，只补充当前维度特有的增量信息：\n"
            + "\n".join(context_lines)
        )

    # 构建 AI 前序步骤的 reason 上下文（中间变量，不写入数据库）
    reason_suffix = ""
    classification_reason = news_item.get("_classification_reason")
    if classification_reason:
        reason_suffix += f"\n\n【分类依据】该新闻被AI判定为当前主维度，依据是：{classification_reason}"
    transaction_reasons = news_item.get("_transaction_reasons", {})
    if isinstance(transaction_reasons, dict) and main_tag in transaction_reasons:
        reasons = transaction_reasons[main_tag]
        if isinstance(reasons, list) and reasons:
            reason_suffix += f"\n【交易划分依据】AI对当前主维度的交易划分说明：{'；'.join(str(r) for r in reasons if r)}"
    if reason_suffix:
        reason_suffix += "\n请在生成报道时参考以上依据，确保报道内容与分类、交易划分保持一致。"

    if main_tag == "新造船订单":
        # 订单汇总型报道：直接把原文/摘要交给 AI 生成，避免模板化表述。
        # 同一公告下的具体批量订单仍走单笔拆分逻辑。
        if _is_order_summary_data(data) and _is_vague_order_summary(data):
            report = await _generate_order_summary_report_with_ai(analyzer, summary, title)
        else:
            # 历史兼容：若旧数据次标签含"预测订单"但 data 未标记 trade_type，
            # 为所有 transactions 注入 trade_type="预测订单"，供后续按笔交易处理。
            data_for_report = data
            tags = news_item.get('tags', '')
            try:
                tags_parsed = json.loads(tags) if isinstance(tags, str) and tags else tags
                if isinstance(tags_parsed, dict):
                    secondary = tags_parsed.get('secondary_tags', [])
                    if isinstance(secondary, list) and "预测订单" in secondary and not _is_forecast_order_data(data):
                        if isinstance(data, str):
                            try:
                                data_obj = json.loads(data)
                                if isinstance(data_obj, dict):
                                    _inject_trade_type_to_data(data_obj, "预测订单")
                                    data_for_report = json.dumps(data_obj, ensure_ascii=False)
                            except Exception:
                                pass
                        elif isinstance(data, dict):
                            data_obj = dict(data)
                            _inject_trade_type_to_data(data_obj, "预测订单")
                            data_for_report = data_obj
            except Exception:
                pass

            original_report = parse_ship_order_report(entity, data_for_report, news_date=news_date, source_text=source_text)
            news_report = clean_news_report(original_report)
            # 新造船订单使用专用润色提示，引导 AI 合并高度相似的多笔订单
            ship_order_prompt = SHIP_ORDER_POLISH_PROMPT + (strict_suffix if strict else "")
            news_report = await _polish_report_with_ai(
                analyzer, news_report, temp_item,
                main_tag=main_tag,
                custom_prompt=ship_order_prompt
            )

            # 润色后事实覆盖度检查：若关键信息丢失，回退到原始解析报告
            coverage = _report_fact_coverage(news_report, main_tag, entity, data)
            if coverage < 0.5:
                print(f"   ⚠️ 润色后关键事实覆盖度不足（{coverage:.0%}），回退到原始解析报告")
                news_report = clean_news_report(original_report)

            # 润色后主体存在性校验：若 data/entity 中的交易主体丢失，回退到原始解析报告
            if not _report_contains_entities(news_report, entity, data=data, main_tag=main_tag):
                print("   ⚠️ 润色后丢失交易主体，回退到原始解析报告")
                news_report = clean_news_report(original_report)

            report = news_report

    elif main_tag == "买卖船":
        structured_content = f"""基于以下信息生成买卖船新闻报告：

实体：{entity}
摘要：{summary}
数据：{data}{context_suffix}{reason_suffix}"""
        combined_prompt = charter_sale_prompt + (strict_suffix if strict else "")
        if report_vdb and embedder:
            try:
                query_text = f"新闻标题: {title}\n主维度: {main_tag}\n实体: {entity}\n摘要: {summary}\n数据: {data}"
                query_vector = embedder.embed(query_text)
                from rag_feedback.config import REPORT_RAG_TOP_K, REPORT_RAG_SIMILARITY_THRESHOLD
                results = report_vdb.search(
                    query_vector,
                    top_k=REPORT_RAG_TOP_K,
                    threshold=REPORT_RAG_SIMILARITY_THRESHOLD,
                )
                if results:
                    from rag_feedback.config import REPORT_RAG_MAX_CONTEXT_CHARS, REPORT_RAG_MAX_CASE_CHARS
                    rag_context = report_vdb.format_report_rag_context(
                        results,
                        max_total_chars=REPORT_RAG_MAX_CONTEXT_CHARS,
                        max_case_chars=REPORT_RAG_MAX_CASE_CHARS,
                    )
                    combined_prompt = f"{combined_prompt}\n\n{rag_context}"
            except Exception as e:
                print(f"   [WARN] 报告RAG检索失败: {e}")

        ai_result = await analyzer.analyze(
            content=structured_content,
            prompt=combined_prompt,
            max_tokens=150,
            model="<your-model>"
        )
        if ai_result["success"]:
            report = clean_news_report(ai_result["analysis"].strip())
            report = remove_hallucinated_dates(report, temp_item)
        else:
            original_report = parse_ship_sale_report(entity, data, news_date=news_date, source_text=source_text)
            report = clean_news_report(original_report)

    elif main_tag == "租船":
        structured_content = f"""基于以下信息生成租船新闻报告：

实体：{entity}
摘要：{summary}
数据：{data}{context_suffix}{reason_suffix}"""
        combined_prompt = charter_sale_prompt + (strict_suffix if strict else "")
        if report_vdb and embedder:
            try:
                query_text = f"新闻标题: {title}\n主维度: {main_tag}\n实体: {entity}\n摘要: {summary}\n数据: {data}"
                query_vector = embedder.embed(query_text)
                from rag_feedback.config import REPORT_RAG_TOP_K, REPORT_RAG_SIMILARITY_THRESHOLD
                results = report_vdb.search(
                    query_vector,
                    top_k=REPORT_RAG_TOP_K,
                    threshold=REPORT_RAG_SIMILARITY_THRESHOLD,
                )
                if results:
                    from rag_feedback.config import REPORT_RAG_MAX_CONTEXT_CHARS, REPORT_RAG_MAX_CASE_CHARS
                    rag_context = report_vdb.format_report_rag_context(
                        results,
                        max_total_chars=REPORT_RAG_MAX_CONTEXT_CHARS,
                        max_case_chars=REPORT_RAG_MAX_CASE_CHARS,
                    )
                    combined_prompt = f"{combined_prompt}\n\n{rag_context}"
            except Exception as e:
                print(f"   [WARN] 报告RAG检索失败: {e}")

        ai_result = await analyzer.analyze(
            content=structured_content,
            prompt=combined_prompt,
            max_tokens=150,
            model="<your-model>"
        )
        if ai_result["success"]:
            report = clean_news_report(ai_result["analysis"].strip())
            report = remove_hallucinated_dates(report, temp_item)
        else:
            original_report = parse_charter_report(entity, data, news_date=news_date, source_text=source_text)
            report = clean_news_report(original_report)

    elif main_tag == "船舶建造":
        original_report = parse_ship_building_report(entity, data, news_date=news_date, source_text=source_text)
        news_report = clean_news_report(original_report)
        # 船舶建造使用专用润色提示，固定保留所有有效信息并防止 AI 凭空添加船东
        ship_building_prompt = SHIP_BUILDING_POLISH_PROMPT + (strict_suffix if strict else "")
        news_report = await _polish_report_with_ai(
            analyzer, news_report, temp_item,
            main_tag=main_tag,
            custom_prompt=ship_building_prompt
        )

        # 润色后事实覆盖度检查
        coverage = _report_fact_coverage(news_report, main_tag, entity, data)
        if coverage < 0.5:
            print(f"   ⚠️ 润色后关键事实覆盖度不足（{coverage:.0%}），回退到原始解析报告")
            news_report = clean_news_report(original_report)

        # 润色后主体存在性校验：若 data/entity 中的交易主体丢失，回退到原始解析报告
        if not _report_contains_entities(news_report, entity, data=data, main_tag=main_tag):
            print("   ⚠️ 润色后丢失交易主体，回退到原始解析报告")
            news_report = clean_news_report(original_report)

        report = news_report

    else:
        # 订单汇总型报道（已归至行业新闻等）：直接把原文/摘要交给 AI 生成，避免模板化表述。
        if _is_order_summary_data(data):
            report = await _generate_order_summary_report_with_ai(analyzer, summary, title)
        else:
            structured_content = f"""基于以下信息生成新闻报告：

主维度：{main_tag}
实体：{entity}
摘要：{summary}
数据：{data}{context_suffix}{reason_suffix}"""
            combined_prompt = news_report_prompt + (strict_suffix if strict else "")
            if report_vdb and embedder:
                try:
                    query_text = f"新闻标题: {title}\n主维度: {main_tag}\n实体: {entity}\n摘要: {summary}\n数据: {data}"
                    query_vector = embedder.embed(query_text)
                    from rag_feedback.config import REPORT_RAG_TOP_K, REPORT_RAG_SIMILARITY_THRESHOLD
                    results = report_vdb.search(
                        query_vector,
                        top_k=REPORT_RAG_TOP_K,
                        threshold=REPORT_RAG_SIMILARITY_THRESHOLD,
                    )
                    if results:
                        from rag_feedback.config import REPORT_RAG_MAX_CONTEXT_CHARS, REPORT_RAG_MAX_CASE_CHARS
                        rag_context = report_vdb.format_report_rag_context(
                            results,
                            max_total_chars=REPORT_RAG_MAX_CONTEXT_CHARS,
                            max_case_chars=REPORT_RAG_MAX_CASE_CHARS,
                        )
                        combined_prompt = f"{combined_prompt}\n\n{rag_context}"
                except Exception as e:
                    print(f"   [WARN] 报告RAG检索失败: {e}")

            ai_result = await analyzer.analyze(
                content=structured_content,
                prompt=combined_prompt,
                max_tokens=150,
                model="<your-model>"
            )
            if ai_result["success"]:
                report = clean_news_report(ai_result["analysis"].strip())
                report = remove_hallucinated_dates(report, temp_item)
            else:
                report = ""

    return report


async def _generate_single_report_with_retry(
    analyzer,
    news_item,
    main_tag,
    entity,
    data,
    summary,
    title,
    report_vdb,
    embedder,
    prompts,
    generated_reports=None,
):
    """
    生成单个主维度报道，带最多 2 次重试。
    返回 report 字符串；若最终校验失败返回空字符串。
    """
    from tag_quality_validator import validate_report_matches_tag

    for attempt in range(3):  # 初始 1 次 + 2 次重试
        strict = attempt > 0
        report = await _generate_single_report(
            analyzer, news_item, main_tag, entity, data, summary, title,
            report_vdb, embedder, prompts, strict=strict,
            generated_reports=generated_reports
        )
        ok, reason = validate_report_matches_tag(report, main_tag, entity, data)
        if ok:
            return report
        print(f"   ⚠️ 主维度【{main_tag}】报道校验失败（第{attempt + 1}次）：{reason}")

    return ""


async def _generate_fallback_daily_report(analyzer, news_item):
    """当唯一主维度校验全部失败时，降级为行业新闻并生成通用报道。"""
    title = news_item.get('title', '')
    summary = news_item.get('summary', '')
    source_text = summary or title

    prompt = """请基于以下新闻标题和摘要，生成一段30-80字的行业新闻报告。
要求：
1. 字数严格控制在30-80字之间
2. 包含主体和核心事件
3. 不要出现"无法生成""无有效信息"等元话语
4. 句尾不要出现（××字）"""

    try:
        result = await analyzer.analyze(
            content=f"标题：{title}\n摘要：{source_text}",
            prompt=prompt,
            max_tokens=150,
            model="<your-model>"
        )
        if result.get("success"):
            report = clean_news_report(result["analysis"].strip())
            if report and len(report) >= 10:
                return [{
                    "main_tag": "行业新闻",
                    "report": report,
                    "is_duplicate": False
                }]
    except Exception as e:
        print(f"   [WARN] 降级生成失败: {e}")

    return [{
        "main_tag": "行业新闻",
        "report": f"{title}" if title else "该新闻暂无有效报道",
        "is_duplicate": False
    }]


def _extract_data_groups(news_item):
    """
    从 news_item 的 data 字段中提取 [{main_tag, data}] 分组列表。
    兼容旧格式：单对象、数组、字符串等。
    """
    data = news_item.get('data', '')
    if not data:
        return []

    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return []
    else:
        parsed = data

    # 新分组格式
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and 'main_tag' in parsed[0]:
        return parsed

    # 旧格式：统一包装成单组，主标签由后续展开逻辑推断
    return [{'main_tag': '', 'data': parsed}]


def _get_main_tag_transactions(news_item, main_tag):
    """
    提取同一条新闻、指定主标签下的单笔交易字典列表。

    数据库字段中的报道按"一笔交易一篇"生成，不在此做任何跨交易合并；
    相同主体交易的重组只发生在 HTML 渲染阶段，不影响这里的结果。
    已标记 is_duplicate=True 的交易不生成报道。
    非交易型主标签或无法解析时返回空列表（调用方回退到整组生成逻辑）。
    """
    if main_tag not in _CATEGORY_ENTITY_FIELDS:
        return []

    transactions = []
    for group in _extract_data_groups(news_item):
        if not isinstance(group, dict):
            continue
        group_main_tag = str(group.get('main_tag', '') or '').strip()
        group_data = group.get('data', '')

        data_transactions, _, _ = _get_data_transactions(group_data)
        if not data_transactions:
            # 兼容旧格式：整个 data 作为单笔交易
            if isinstance(group_data, dict):
                data_transactions = [group_data]
            elif isinstance(group_data, str):
                try:
                    parsed = json.loads(group_data)
                    if isinstance(parsed, dict):
                        data_transactions = [parsed]
                    elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                        data_transactions = parsed
                except Exception:
                    pass

        for tx in data_transactions or []:
            if not isinstance(tx, dict):
                continue
            if tx.get('is_duplicate') is True:
                continue
            # 交易自带主标签优先，否则继承分组主标签
            tx_main_tag = str(tx.get('main_tag', '') or '').strip() or group_main_tag
            if tx_main_tag != main_tag:
                continue
            transactions.append(tx)

    return transactions




async def generate_news_report_from_existing_data(news_data, enable_global_transaction_merge=True):
    """
    基于字典中已有的结构化信息生成新闻报告（主维度版本）。

    entity/data 字段支持新数组格式：
        [{"main_tag": str, "entity"/"data": str}, ...]
    也兼容旧单对象格式。

    生成的 news_report 为数组格式：
        [{"main_tag": str, "report": str}, ...]

    同一主标签下有多笔交易时，数据库字段中的报道按"一笔交易一篇"生成，
    不做任何跨交易/跨新闻合并（enable_global_transaction_merge 参数已废弃，
    仅保留以兼容旧调用方）；相同主体交易的重组只发生在 HTML 渲染阶段。
    """
    from tag_quality_validator import (
        sort_main_tags_by_priority,
        extract_fact_fingerprint,
        calculate_fingerprint_overlap,
        is_transaction_main_tag,
        validate_main_tag_density,
        calculate_report_text_similarity,
        _count_distinct_entities,
        _can_merge_entities,
    )

    # 首先过滤掉None值
    valid_news_data = [item for item in news_data if item is not None]

    if not valid_news_data:
        print("⚠️ 警告：没有有效的新闻数据可供处理")
        return []

    print(f"📊 开始处理 {len(valid_news_data)} 条有效新闻数据")

    analyzer = BailianWebAnalyzer(region="beijing")
    updated_news = []

    # ========== 初始化报告向量RAG检索器 ==========
    report_vdb = None
    embedder = None
    try:
        from rag_feedback.embedder import get_embedder
        from rag_feedback.vector_db import get_vector_db
        from rag_feedback.config import (
            RAG_ENABLED, REPORT_VECTOR_COLLECTION_NAME,
            REPORT_RAG_TOP_K, REPORT_RAG_SIMILARITY_THRESHOLD
        )
        if RAG_ENABLED:
            embedder = get_embedder()
            report_vdb = get_vector_db(collection_name=REPORT_VECTOR_COLLECTION_NAME)
            print(f"[RAG-REPORT] 报告向量RAG已启用 | 向量库记录数: {report_vdb.count()}")
        else:
            print("[RAG-REPORT] RAG已禁用")
    except Exception as e:
        print(f"[WARN] 报告向量RAG初始化失败: {e}")

    # 通用新闻报告生成提示词
    news_report_prompt = """请基于以下结构化信息，生成一段30-80字的简洁严谨新闻报告,要求：
1. 字数严格控制在30-80字之间
2. 突出核心事实，包含时间、主体、关键成果
3. 语言简洁专业，避免主观评价
4. 采用标准新闻报告文体
5. 日期必须放在报道开头。优先使用 data/entity 中交易事件明确给出的日期；若交易信息没有日期，则使用新闻发布日期；只写原文明确给出的月日（如"6月29日"），严禁补全年份；但如果 data/entity 中的事件日期明显早于新闻发布日期（超过两周或跨年度），应判断为之前的信息，此时可从原文中获取对应年份，并在报道中明确写出完整日期（如"2025年6月29日"），以提示信息时效性。若原文没有给出具体日期，则不要在报道中写日期。
6. 写日期时应优先使用原文中明确指代该新闻事件发生或发布时间的真实月日，避免将原文中仅作为背景、引述或辅助信息出现的不相关日期写入报告；若日期信息不完整（如只有"X日"而省略月份），可参考该新闻的发布日期补全月份；若无法判断具体时间或原文未提及相关日期，则不要写日期。严禁编造日期、严禁使用任何固定示例日期（如4月15日、4月15号、4.15等）、严禁补全年份；但事件日期明显早于发布日期时，可从原文获取年份并写出完整日期。
7. 翻译规范：对英文内容，除国外公司名称可保留英文外，中国的船厂、船东等主体名称必须翻译为对应的中文名称；船舶相关内容中，除船型（shiptype）可保留英文外，其余术语均需翻译为中文并保持专业。
8. 句尾不要出现（××字）这样的多余文本。
9. 检查生成的语句标点使用逻辑是否合规，句首不得出现多余的逗号、顿号或其他无关标点。"""

    # 新造船订单专用提示词
    ship_order_prompt = """请基于以下结构化信息，生成一段30-80字的简洁新闻报告,要求：
1. 字数严格控制在30-80字之间
2. 必须包含船厂和船东（航运公司/客户）的信息
3. 将数据字段中的所有已知交易信息都包含在内（如船型、数量、金额、交付时间等）
4. 只陈述交易事实，绝对不要包含任何影响分析、市场评价或预测性文字
5. 语言简洁专业，采用标准新闻报告文体
6. 日期必须放在报道开头。优先使用 data/entity 中交易事件明确给出的日期；若交易信息没有日期，则使用新闻发布日期；只写原文明确给出的月日（如"6月29日"），不要写年份，严禁补全年份；但如果 data 中的 date 字段明显早于新闻发布日期（超过两周或跨年度），应判断为之前的信息，此时可从原文中获取对应年份，并在报道中明确写出完整日期（如"2025年6月29日"），以提示信息时效性。
7. 注意 data 中的 date 字段：如果该日期明显早于新闻发布日期（超过两周或跨年度），应判断为之前的信息，此时可从原文中获取对应年份，并在报道中明确写出完整日期（X年X月X日），以提示信息时效性。
8. 翻译规范：对英文内容，除国外公司名称可保留英文外，中国的船厂、船东等主体名称必须翻译为对应的中文名称；船舶相关内容中，除船型（shiptype）可保留英文外，其余术语均需翻译为中文并保持专业。
9. 句尾不要出现（××字）这样的多余文本。
10. 检查生成的语句标点使用逻辑是否合规，句首不得出现多余的逗号、顿号或其他无关标点。"""

    # 租船/买卖船专用提示词
    charter_sale_prompt = """请基于以下结构化信息，生成一段30-80字的简洁新闻报告，要求：
1. 字数严格控制在30-80字之间。
2. 必须包含交易主体（买方/租家、卖方/船东）和交易标的（船型、数量、金额等核心信息）。
3. 交易主体相同时需重构句式：如果买方与卖方相同，不要输出"X向X交易"这类不合语法的表述，应改为"X完成/达成Y艘Z交易"等通顺表达；如果船舶建造/交付报道中船东/接收方与船厂/建造方为同一主体（如"山东海洋集团自主建造并在本集团交付"），不要输出"X从X接收"这类不合语法的表述，应改为"X完成交付/下水/建造Y艘Z"或"X自主建造的Y艘Z完成交付/下水"等通顺表达。
4. 若存在多笔同类型交易，应合并为简洁的总述，不要重复罗列每笔交易。
5. 只陈述交易事实，绝对不要包含任何影响分析、市场评价或预测性文字。
6. 语言简洁专业，采用标准新闻报告文体。
7. 日期必须放在报道开头。优先使用 data/entity 中交易事件明确给出的日期；若交易信息没有日期，则使用新闻发布日期；只写原文明确给出的月日（如"6月29日"），不要写年份，严禁补全年份；但如果 data 中的 date 字段明显早于新闻发布日期（超过两周或跨年度），应判断为之前的信息，此时可从原文中获取对应年份，并在报道中明确写出完整日期（如"2025年6月29日"），以提示信息时效性。
8. 注意 data 中的 date 字段：如果该日期明显早于新闻发布日期（超过两周或跨年度），应判断为之前的信息，此时可从原文中获取对应年份，并在报道中明确写出完整日期（X年X月X日），以提示信息时效性。
9. 翻译规范：对英文内容，除国外公司名称可保留英文外，中国的船厂、船东等主体名称必须翻译为对应的中文名称；船舶相关内容中，除船型（shiptype）可保留英文外，其余术语均需翻译为中文并保持专业。
10. 句尾不要出现（××字）这样的多余文本。
11. 检查生成的语句标点使用逻辑是否合规，句首不得出现多余的逗号、顿号或其他无关标点。"""

    prompts = (news_report_prompt, ship_order_prompt, charter_sale_prompt)

    for index, news_item in enumerate(valid_news_data, 1):
        try:
            # 检查news_item是否为有效字典
            if not isinstance(news_item, dict):
                print(f"❌ 跳过第{index}条数据：非字典类型")
                continue

            # 提取现有结构化信息（使用安全获取方式）
            summary = news_item.get('summary', '')
            title = news_item.get('title', f'未知标题_{index}')

            print(f"🔍 处理第{index}条新闻: {title}")

            # 确定需要生成报告的主维度列表
            from prompt import MAIN_DIMENSIONS
            import json

            tags = news_item.get('tags', '')
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = {}
            if not isinstance(tags, dict):
                tags = {}

            # 1. 从 tags 获取主维度
            main_tags = [tag for tag in tags.get('main_tags', []) if tag in MAIN_DIMENSIONS]
            information_density = tags.get('information_density', {}) if isinstance(tags.get('information_density'), dict) else {}

            # 2. 从 entity/data 数组中推断主维度（data/entity 是实际提取的结构化证据，通常比 tags 更可靠）
            inferred_tags = []
            for field in ['entity', 'data']:
                value = news_item.get(field, '')
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except Exception:
                        continue
                if isinstance(value, list):
                    for group in value:
                        if isinstance(group, dict) and group.get('main_tag') in MAIN_DIMENSIONS:
                            if group['main_tag'] not in inferred_tags:
                                inferred_tags.append(group['main_tag'])

            # 3. 合并策略：
            #    - 如果 tags 中没有有效主维度，直接使用 inferred_tags
            #    - 如果 tags 中只有【行业新闻】这种兜底标签，但 inferred_tags 中有更具体的交易/建造标签，
            #      则用 inferred_tags 替换，避免有具体订单/交易数据时仍生成泛泛的行业新闻
            #    - 否则，合并 tags 与 inferred_tags，去重
            if not main_tags:
                main_tags = inferred_tags
            elif main_tags == ["行业新闻"] and inferred_tags:
                main_tags = inferred_tags
                # density 也同步重置为 medium，因为结构化数据已证明该维度有事实支撑
                information_density = {tag: information_density.get(tag, "medium") for tag in main_tags}
            else:
                for tag in inferred_tags:
                    if tag not in main_tags:
                        main_tags.append(tag)
                        information_density.setdefault(tag, "medium")

            if not main_tags:
                # 兼容旧 category 字段
                category = news_item.get('category', '')
                if category:
                    main_tags = [category]
                else:
                    main_tags = ["行业新闻"]

            # 按优先级排序主维度
            main_tags = sort_main_tags_by_priority(main_tags, information_density)
            print(f"   主维度优先级排序: {', '.join(main_tags)}")

            report_groups = []
            used_fact_fingerprints = []  # 已使用的事实指纹列表（仅含此前其他主维度的报道）
            used_reports = []            # 已生成的报道文本列表（作为上下文）

            for idx, main_tag in enumerate(main_tags):
                entity, data = _extract_entity_data_group(news_item, main_tag)

                if not any([entity, summary, data]):
                    # 无有效信息时跳过，不生成占位报道
                    print(f"   ⚠️ 主维度【{main_tag}】无有效信息，跳过")
                    continue

                # 密度校验：低密度/无独立事实的主标签不应生成独立报道
                density_ok, density_reason = validate_main_tag_density(news_item, main_tag, entity, data)
                if not density_ok:
                    if len(main_tags) > 1:
                        print(f"   🗑️ 主维度【{main_tag}】密度校验失败：{density_reason}，跳过")
                        continue
                    else:
                        print(f"   ⚠️ 主维度【{main_tag}】为唯一主维度且密度校验失败，降级为行业新闻")
                        report_groups = await _generate_fallback_daily_report(analyzer, news_item)
                        break

                # 数据库字段中的报道按"一笔交易一篇"生成：
                # 相同主体交易的语序重组只发生在 HTML 渲染阶段（临时处理），
                # 这里不做任何跨交易/跨新闻合并，各笔交易互不覆盖、互不影响
                tag_reports = []
                transactions = _get_main_tag_transactions(news_item, main_tag)
                if transactions:
                    for tx in transactions:
                        # 线性流程：将已生成的报道作为上下文传入，提醒 AI 避免重复提取
                        tx_report = await _generate_single_report_with_retry(
                            analyzer, news_item, main_tag, tx, tx, summary, title,
                            report_vdb, embedder, prompts,
                            generated_reports=used_reports
                        )
                        if not tx_report:
                            print(f"   ⚠️ 主维度【{main_tag}】单笔交易报道生成失败，跳过该笔交易")
                            continue
                        tag_reports.append(tx_report)
                else:
                    # 非交易型数据（旧格式、公司金融、行业新闻等）：整组生成一篇报道
                    whole_report = await _generate_single_report_with_retry(
                        analyzer, news_item, main_tag, entity, data, summary, title,
                        report_vdb, embedder, prompts,
                        generated_reports=used_reports
                    )
                    if whole_report:
                        tag_reports.append(whole_report)

                if not tag_reports:
                    # 校验失败，根据是否唯一主维度处理
                    if len(main_tags) > 1:
                        print(f"   🗑️ 主维度【{main_tag}】报道生成失败且非唯一主维度，删除该报道")
                        continue
                    else:
                        print(f"   ⚠️ 主维度【{main_tag}】为唯一主维度且生成失败，降级为行业新闻")
                        report_groups = await _generate_fallback_daily_report(analyzer, news_item)
                        break

                # 本主维度内的各笔交易是相互独立的事实，只做跨主维度去重，不做相互去重
                tag_fingerprints = []
                for report in tag_reports:
                    # 事实指纹去重（仅与此前其他主维度的报道比对）
                    current_fingerprint = extract_fact_fingerprint(main_tag, entity, data, report)
                    max_overlap = 0.0
                    for fp in used_fact_fingerprints:
                        overlap = calculate_fingerprint_overlap(current_fingerprint, fp)
                        if overlap > max_overlap:
                            max_overlap = overlap

                    # 交易类主标签若与已生成报道事实高度重叠，直接跳过
                    if is_transaction_main_tag(main_tag) and max_overlap >= 0.5:
                        print(f"   🗑️ 主维度【{main_tag}】与已生成报道事实重叠（{max_overlap:.0%}），跳过")
                        continue

                    # 对非交易类次要维度（如公司金融、行业新闻等），
                    # 若生成的报道与已生成报道文本高度相似，说明没有增量信息，应跳过
                    if not is_transaction_main_tag(main_tag):
                        max_text_sim = calculate_report_text_similarity(report, used_reports)
                        if max_text_sim >= 0.75:
                            print(f"   🗑️ 主维度【{main_tag}】与已生成报道文本相似度过高（{max_text_sim:.0%}），跳过")
                            continue

                    tag_fingerprints.append(current_fingerprint)
                    used_reports.append(report)

                    report_groups.append({
                        "main_tag": main_tag,
                        "report": report,
                        "is_duplicate": False
                    })
                    print(f"   ✅ 主维度【{main_tag}】报道生成完成")

                used_fact_fingerprints.extend(tag_fingerprints)

            if not report_groups:
                # 全部失败，生成兜底报道
                report_groups = await _generate_fallback_daily_report(analyzer, news_item)

            # 确保每条报道都携带 is_duplicate 字段（默认 false）
            for g in report_groups:
                g.setdefault("is_duplicate", False)

            processed_item = news_item.copy()
            processed_item['news_report'] = json.dumps(report_groups, ensure_ascii=False)
            updated_news.append(processed_item)

        except Exception as e:
            print(f"❌ 处理第{index}条新闻时出现异常: {str(e)}")
            import traceback
            traceback.print_exc()
            if isinstance(news_item, dict):
                safe_item = news_item.copy()
                safe_item['news_report'] = json.dumps([{
                    "main_tag": "行业新闻",
                    "report": f"处理异常: {str(e)}",
                    "is_duplicate": False
                }], ensure_ascii=False)
                updated_news.append(safe_item)
            else:
                safe_item = {
                    'title': f'异常数据_{index}',
                    'news_report': json.dumps([{
                        "main_tag": "行业新闻",
                        "report": f"数据格式异常: {str(e)}",
                        "is_duplicate": False
                    }], ensure_ascii=False),
                }
                updated_news.append(safe_item)

    return updated_news


async def main():
    try:
        # 获取数据
        print("🔄 开始获取新闻数据...")
        data = await main_processing()

        # 数据验证和统计
        total_count = len(data) if data else 0
        valid_count = len([item for item in data if item is not None]) if data else 0
        none_count = total_count - valid_count

        print(f"📈 数据统计: 总数={total_count}, 有效数据={valid_count}, None值={none_count}")

        if valid_count == 0:
            print("⚠️ 没有有效数据可供处理，程序结束")
            return []

        print("🔧 开始生成新闻报告...")
        updated_data = await generate_news_report_from_existing_data(data)

        # 显示结果
        print("\n" + "=" * 60)
        print(f"🎉 生成结果汇总: 共处理{len(updated_data)}条新闻")
        print("=" * 60)

        success_count = 0
        for i, item in enumerate(updated_data, 1):
            if isinstance(item, dict) and item.get('news_report'):
                success_count += 1
                print(f"\n{i}. 标题: {item.get('title', 'N/A')}")
                print(f"   分类: {item.get('category', 'N/A')}")
                print(f"   新闻报告: {item.get('news_report', 'N/A')}")
                print(f"   字数: {len(item.get('news_report', ''))}")
                print("-" * 50)

        print(f"\n📊 最终统计: 成功生成{success_count}条，失败{len(updated_data) - success_count}条")
        return updated_data

    except Exception as e:
        print(f"💥 主程序执行失败: {str(e)}")
        return []


# 运行
if __name__ == "__main__":
    try:
        result = asyncio.run(main())
        print(f"🏁 程序执行完成，返回{len(result) if result else 0}条结果")
    except Exception as e:
        print(f"💥 程序运行异常: {str(e)}")