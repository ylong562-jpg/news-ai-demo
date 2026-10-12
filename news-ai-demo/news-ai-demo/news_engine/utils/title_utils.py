# -*- coding: utf-8 -*-
"""
标题处理工具模块

提供标题标准化、中文友好的相似度计算等功能，
解决中文文本没有空格分隔导致的 Jaccard 相似度失效问题。
"""

import html
import re
import unicodedata


# 零宽字符集合
ZERO_WIDTH_CHARS = '\u200b\u200c\u200d\u2060\ufeff'


def normalize_title(title: str) -> str:
    """
    标准化标题，用于去重比较。

    处理内容：
    - HTML 实体解码
    - Unicode NFC 归一化
    - 移除零宽字符
    - 全角空格/不间断空格转普通空格
    - 折叠连续空白
    - 转小写
    - 移除首尾空白
    """
    if not title:
        return ""

    if not isinstance(title, str):
        title = str(title)

    # HTML 实体解码
    title = html.unescape(title)

    # Unicode NFC 归一化
    title = unicodedata.normalize('NFC', title)

    # 移除零宽字符
    for ch in ZERO_WIDTH_CHARS:
        title = title.replace(ch, '')

    # 转小写
    title = title.lower()

    # 全角空格、不间断空格等转普通空格
    title = title.replace('\u3000', ' ').replace('\xa0', ' ')

    # 折叠连续空白并去首尾
    title = ' '.join(title.split())

    return title.strip()


def is_chinese_dominant(text: str, threshold: float = 0.3) -> bool:
    """
    判断文本是否以中文为主。

    Parameters:
        text: 输入文本
        threshold: 中文字符占比阈值

    Returns:
        bool: 中文字符占比超过阈值返回 True
    """
    if not text:
        return False
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    return chinese_chars / len(text) > threshold


def _char_ngrams(text: str, n: int = 2):
    """生成字符级 n-gram 集合。"""
    return set(text[i:i + n] for i in range(len(text) - n + 1))


def title_similarity(title1: str, title2: str) -> float:
    """
    计算两个标题的相似度，自动适配中文和英文。

    规则：
    - 任一标题中文为主时，使用字符级 2-gram Jaccard 相似度
    - 否则使用词级 Jaccard 相似度
    - 标准化后完全相等返回 1.0

    Returns:
        float: 0.0 ~ 1.0 的相似度
    """
    t1 = normalize_title(title1)
    t2 = normalize_title(title2)

    if not t1 or not t2:
        return 0.0

    if t1 == t2:
        return 1.0

    # 中文为主时使用字符级 2-gram
    if is_chinese_dominant(t1) or is_chinese_dominant(t2):
        set1 = _char_ngrams(t1, n=2)
        set2 = _char_ngrams(t2, n=2)
    else:
        set1 = set(t1.split())
        set2 = set(t2.split())

    if not set1 or not set2:
        return 0.0

    intersection = len(set1 & set2)
    union = len(set1 | set2)

    return intersection / union if union > 0 else 0.0


def get_title_signature(title: str) -> str:
    """
    获取标题的签名，用于精确去重。

    返回标准化后的标题字符串。
    """
    return normalize_title(title)


if __name__ == "__main__":
    # 自测
    samples = [
        ("新船厂成立开工！央地强强联手打造海洋装备", "新船厂成立开工！央地强强联手打造海洋装备"),
        ("新船厂成立开工！央地强强联手打造海洋装备", "新船厂成立开工！央地强强联手打造海洋装备\"新"),
        ("Capesize rates surge on strong demand", "Capesize rates surge on cargo demand"),
        ("新船厂成立开工！ 央地强强联手打造海洋装备", "新船厂成立开工！央地强强联手打造海洋装备"),
    ]
    for t1, t2 in samples:
        print(f"'{t1[:30]}...' <-> '{t2[:30]}...': {title_similarity(t1, t2):.3f}")
