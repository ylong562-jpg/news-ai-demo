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

日期处理工具函数
提供统一的日期解析和验证功能
"""

import json
import os
import re
from datetime import datetime, timedelta


_DATE_FORMAT_PATTERNS = None


def _load_date_format_patterns_from_vocab():
    """从 data/date_formats.json 加载允许的 date 格式正则列表。"""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "date_formats.json"
    )
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
            re.compile(r'^\d{4}年至今$'),
        ]


def load_date_format_patterns():
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


def is_allowed_date_format(date_str):
    """判断 date 字符串是否在 data/date_formats.json 词表范围内。"""
    if date_str is None:
        return False
    s = str(date_str).strip()
    if not s or s.lower() in ('null', 'none', 'nan'):
        return False
    for pat in load_date_format_patterns():
        if pat.match(s):
            return True
    return False


def get_today_str():
    """获取今日日期字符串 (YYYY-MM-DD)"""
    return datetime.now().strftime('%Y-%m-%d')


def get_yesterday_str():
    """获取昨日日期字符串 (YYYY-MM-DD)"""
    return (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')


def get_date_range(days=2):
    """
    获取日期范围（今天往前推n天）
    
    Parameters:
    days (int): 天数
    
    Returns:
    list: 日期字符串列表 [YYYY-MM-DD, ...]
    """
    dates = []
    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        dates.append(date)
    return dates


def parse_date(date_text, default=None):
    """
    解析各种格式的日期文本
    
    Parameters:
    date_text (str): 日期文本
    default (str): 默认值
    
    Returns:
    str: YYYY-MM-DD 格式日期，或默认值
    """
    if not date_text:
        return default
    
    date_text = str(date_text).strip()
    
    # 尝试多种日期格式
    patterns = [
        # 2024-01-15
        (r'(\d{4})-(\d{1,2})-(\d{1,2})', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"),
        # 2024/01/15
        (r'(\d{4})/(\d{1,2})/(\d{1,2})', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"),
        # 2024年01月15日
        (r'(\d{4})年(\d{1,2})月(\d{1,2})日', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"),
        # 01-15 (当前年份)
        (r'(\d{1,2})-(\d{1,2})', lambda m: f"{datetime.now().year}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"),
        # 01/15 (当前年份)
        (r'(\d{1,2})/(\d{1,2})', lambda m: f"{datetime.now().year}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"),
        # 15 Jan 2024
        (r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})', 
         lambda m: f"{m.group(3)}-{month_to_num(m.group(2))}-{m.group(1).zfill(2)}"),
        # Jan 15, 2024
        (r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})',
         lambda m: f"{m.group(3)}-{month_to_num(m.group(1))}-{m.group(2).zfill(2)}"),
    ]
    
    for pattern, formatter in patterns:
        match = re.search(pattern, date_text, re.IGNORECASE)
        if match:
            try:
                result = formatter(match)
                # 验证日期有效性
                datetime.strptime(result, '%Y-%m-%d')
                return result
            except:
                continue
    
    return default


def parse_event_date(date_text, default=None, reference_year=None):
    """
    解析业务事件日期，支持 YYYY-MM-DD、YYYY-MM 以及 YYYY年至今 等格式。
    优先返回 YYYY-MM-DD；若原文只明确到月份，则返回 YYYY-MM；
    若原文为"YYYY年至今"这类年份范围，则保留该范围表达。

    Parameters:
        date_text (str): 日期文本
        default: 默认值
        reference_year (int): 用于补全月份的参考年份；不传则使用当前年

    Returns:
        str: YYYY-MM-DD、YYYY-MM、YYYY年至今 格式日期，或默认值
    """
    if not date_text:
        return default

    date_text = str(date_text).strip()
    if not date_text or date_text.lower() in ('null', 'none'):
        return default

    # 已经是标准格式：YYYY-MM-DD 或 YYYY-MM
    if re.match(r'^\d{4}-\d{1,2}-\d{1,2}$', date_text):
        try:
            datetime.strptime(date_text, '%Y-%m-%d')
            return date_text
        except Exception:
            pass

    if re.match(r'^\d{4}-\d{1,2}$', date_text):
        try:
            datetime.strptime(date_text, '%Y-%m')
            return date_text
        except Exception:
            pass

    # 支持 "YYYY年至今" 这种年份范围表达，直接保留规范名称
    year_to_now_match = re.match(r'^(\d{4})年至今$', date_text)
    if year_to_now_match:
        return f"{year_to_now_match.group(1)}年至今"

    # 尝试解析带具体日的日期
    year = reference_year or datetime.now().year
    day_patterns = [
        (r'(\d{4})-(\d{1,2})-(\d{1,2})', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"),
        (r'(\d{4})/(\d{1,2})/(\d{1,2})', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"),
        (r'(\d{4})年(\d{1,2})月(\d{1,2})日', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"),
        # 中文月日（无年份），如 4月18日、4月18
        (r'(\d{1,2})月(\d{1,2})日?', lambda m: f"{year}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"),
        (r'(\d{1,2})-(\d{1,2})', lambda m: f"{year}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"),
        (r'(\d{1,2})/(\d{1,2})', lambda m: f"{year}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"),
        (r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})',
         lambda m: f"{m.group(3)}-{month_to_num(m.group(2))}-{m.group(1).zfill(2)}"),
        (r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})',
         lambda m: f"{m.group(3)}-{month_to_num(m.group(1))}-{m.group(2).zfill(2)}"),
    ]

    for pattern, formatter in day_patterns:
        match = re.search(pattern, date_text, re.IGNORECASE)
        if match:
            try:
                result = formatter(match)
                datetime.strptime(result, '%Y-%m-%d')
                return result
            except Exception:
                continue

    # 尝试只解析到月份：YYYY-MM / YYYY年M月 / YYYY/M
    month_patterns = [
        (r'(\d{4})-(\d{1,2})', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}"),
        (r'(\d{4})/(\d{1,2})', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}"),
        (r'(\d{4})年(\d{1,2})月', lambda m: f"{m.group(1)}-{m.group(2).zfill(2)}"),
    ]

    for pattern, formatter in month_patterns:
        match = re.search(pattern, date_text, re.IGNORECASE)
        if match:
            try:
                result = formatter(match)
                datetime.strptime(result, '%Y-%m')
                return result
            except Exception:
                continue

    # 当提供了参考年份时，尝试补全无年份的月份/季度/半年
    if reference_year:
        # 无年份的月份，如 "3月"
        month_no_year_patterns = [
            (r'^(\d{1,2})月$', lambda m: f"{year}-{m.group(1).zfill(2)}"),
        ]
        for pattern, formatter in month_no_year_patterns:
            match = re.search(pattern, date_text, re.IGNORECASE)
            if match:
                try:
                    result = formatter(match)
                    datetime.strptime(result, '%Y-%m')
                    return result
                except Exception:
                    continue

        # 无年份的季度，如 "Q1"、"第一季度"
        q_match = re.search(r'第([一二三四1234])季度', date_text, re.IGNORECASE)
        if q_match:
            return f"{year}年第{q_match.group(1)}季度"
        qn_match = re.search(r'Q([1-4])', date_text, re.IGNORECASE)
        if qn_match:
            return f"{year}年Q{qn_match.group(1)}"

        # 无年份的半年，如 "上半年"、"下半年"
        half_match = re.search(r'(上|下)半年', date_text)
        if half_match:
            return f"{year}年{half_match.group(1)}半年"

    return default


def is_ym_date(date_str):
    """判断是否为 YYYY-MM 格式的业务月份日期。"""
    if not date_str:
        return False
    return bool(re.match(r'^\d{4}-\d{1,2}$', str(date_str).strip()))


def month_to_num(month_name):
    """将月份英文名称转换为数字"""
    months = {
        'january': '01', 'february': '02', 'march': '03', 'april': '04',
        'may': '05', 'june': '06', 'july': '07', 'august': '08',
        'september': '09', 'october': '10', 'november': '11', 'december': '12'
    }
    return months.get(month_name.lower(), '01')


def is_today_or_recent(date_str, days=2):
    """
    检查日期是否是今天或最近几天
    
    Parameters:
    date_str (str): 日期字符串 YYYY-MM-DD
    days (int): 往前推几天
    
    Returns:
    bool: 是否是今天或最近几天
    """
    if not date_str:
        return False
    
    try:
        valid_dates = get_date_range(days)
        return date_str in valid_dates
    except:
        return False


def extract_date_from_relative(text):
    """
    从相对时间文本中提取日期
    如："2小时前", "昨天", "刚刚"
    
    Parameters:
    text (str): 相对时间文本
    
    Returns:
    str: YYYY-MM-DD 格式日期
    """
    text = str(text).lower()
    today = get_today_str()
    yesterday = get_yesterday_str()
    
    # 刚刚、几分钟前、几小时前 -> 今天
    if any(word in text for word in ['刚刚', '分钟前', '小时前', 'ago', 'today']):
        return today
    
    # 昨天
    if '昨天' in text or 'yesterday' in text:
        return yesterday
    
    # 前天
    if '前天' in text or 'day before yesterday' in text:
        return (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
    
    return None


def format_date_for_mysql(date_str):
    """
    确保日期格式符合MySQL要求
    
    Parameters:
    date_str (str): 日期字符串
    
    Returns:
    str: YYYY-MM-DD 格式
    """
    parsed = parse_date(date_str)
    if parsed:
        return parsed
    return get_today_str()  # 默认返回今天


# 测试
if __name__ == "__main__":
    print("日期工具测试:")
    print(f"今天: {get_today_str()}")
    print(f"昨天: {get_yesterday_str()}")
    print(f"最近3天: {get_date_range(3)}")
    
    test_dates = [
        "2024-03-15",
        "2024/03/15",
        "2024年03月15日",
        "03-15",
        "15 March 2024",
        "March 15, 2024",
        "2小时前",
        "昨天",
    ]
    
    print("\n日期解析测试:")
    for d in test_dates:
        print(f"  '{d}' -> {parse_date(d)}")

