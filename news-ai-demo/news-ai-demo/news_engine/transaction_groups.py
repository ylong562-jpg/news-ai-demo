# -*- coding: utf-8 -*-
"""
交易"相同主体"分组助手（纯函数，无重依赖）。

用于每日新闻 HTML 渲染阶段：同一新闻、同一主标签下的多篇报道，
只有在交易主体相同时才合并进同一新闻卡片展示（各篇报道原文换行拼接，
不再用 AI 重新生成）；其他交易完全不受影响。

分组规则（重要）：
- 每组有且仅有一个"组键"——建组那笔交易按主标签字段顺序取到的
  第一个具体主体值（如"新造船订单"取船东，船东缺失/宽泛时取船厂）；
- 后续交易只有包含某个已存在组的组键时才加入该组，
  否则以自身第一个具体主体值另建一组；
- "某船东"、"未披露"等宽泛占位值不作为组键；
- 不做传递式/连通分量式合并：A 与 B 同组、B 与 C 同组，
  但 A 与 C 主体完全不同的情况不会发生——因为组键不会扩散。
"""

# 宽泛占位类主体值：不能作为分组依据，否则会把互不相关的交易链到一起
BROAD_SUBJECT_MARKS = (
    "某船东", "某船厂", "某航运", "某海运", "某公司", "某企业", "某集团",
    "某知名", "某国内", "某国外", "某欧洲", "某希腊", "未披露", "未公开",
    "unknown", "undisclosed", "n/a",
)


def normalize_subject_value(value):
    """把主体字段归一化为字符串；列表取第一项；空值返回 __UNKNOWN__。"""
    if isinstance(value, list):
        value = value[0] if value else ''
    value = str(value).strip() if value is not None else ''
    return value if value else '__UNKNOWN__'


def is_broad_subject_value(value):
    """判断主体值是否为宽泛占位（某船东、未披露等），这类值不作为分组依据。"""
    if not value or value == '__UNKNOWN__':
        return True
    s = str(value).strip().lower()
    if not s:
        return True
    if s.startswith("某"):
        return True
    return any(mark in s for mark in BROAD_SUBJECT_MARKS)


def specific_subject_values(transaction, fields):
    """
    返回交易在指定主体字段上的具体（非宽泛占位）值列表，保持字段顺序。
    transaction 为交易字典；fields 如 ("shipowner", "shipyard")。
    """
    values = []
    for field in fields:
        raw = transaction.get(field)
        value = normalize_subject_value(raw if _is_valid(raw) else None)
        if not is_broad_subject_value(value) and value not in values:
            values.append(value)
    return values


def _is_valid(value):
    return value is not None and str(value).strip() not in ['', 'None', 'null', 'NULL', 'nan', 'NaN']


def group_transactions_by_shared_subject(transactions, fields):
    """
    按"相同主体"把交易分组：返回 [[transaction, ...], ...]，保持输入顺序。

    组键不扩散、不做传递式合并——保证同一组内每笔交易都确实包含
    该组的组键主体值。无具体主体值的交易各自单独成组。
    """
    groups = []
    key_to_index = {}
    for tx in transactions or []:
        if not isinstance(tx, dict):
            continue
        values = specific_subject_values(tx, fields)
        match_index = None
        for v in values:
            if v in key_to_index:
                match_index = key_to_index[v]
                break
        if match_index is not None:
            groups[match_index].append(tx)
        else:
            groups.append([tx])
            if values:
                # 只登记首个具体主体值为组键，组键不随新成员扩散
                key_to_index.setdefault(values[0], len(groups) - 1)
    return groups
