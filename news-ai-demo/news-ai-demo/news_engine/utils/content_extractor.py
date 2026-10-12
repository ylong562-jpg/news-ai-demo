# -*- coding: utf-8 -*-
"""
新闻正文提取器（增强版）

针对 VPN 站点（Splash247 / TradeWinds / Seatrade）正文容易丢失的问题，
提供统一提取接口：优先 crawl4ai 浏览器渲染+整页滚动，按站点规则做 HTML 兜底抽取，
并支持更长、更完整的文章内容。
"""

import asyncio
import os
import re
import sys
from typing import Optional, Dict, Any
from urllib.parse import urlparse

# Windows 下避免 rich/crawl4ai 日志因 gbk 编码打印 Unicode 失败
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from bs4 import BeautifulSoup, Comment

# crawl4ai 相关导入
try:
    from crawl4ai import AsyncWebCrawler
    from crawl4ai.async_configs import CrawlerRunConfig, BrowserConfig
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    CRAWL4AI_AVAILABLE = True
except ImportError:
    CRAWL4AI_AVAILABLE = False


# 内容长度阈值配置
CONTENT_MIN_LENGTH = 600          # 小于此值认为正文提取不完整，触发 HTML 兜底
CONTENT_MAX_LENGTH = 50000        # 默认最大保留字符数


# 站点级正文提取规则
# - content_selectors: 正文容器选择器，按优先级尝试
# - remove_selectors: 需要移除的噪音元素
# - wait_for: 等待加载的选择器（传给 crawl4ai）
# - scroll_times: 页面滚动次数，用于懒加载
SITE_CONTENT_RULES: Dict[str, Dict[str, Any]] = {
    "splash247.com": {
        "content_selectors": [
            "div[class*='article-body']",
            "div[class*='entry-content']",
            "div.article-content",
            "article",
            "main article",
            "main",
        ],
        "remove_selectors": [
            "nav", "header", "footer", "aside",
            "div[class*='related']",
            "div[class*='newsletter']",
            "div[class*='subscribe']",
            "div[class*='share']",
            "div[class*='social']",
            "div[class*='comments']",
            "figure",
            "figcaption",
            "div[class*='image-caption']",
            "div[class*='photo-caption']",
            "div[class*='caption']",
            "div[class*='image-credit']",
            "img",
        ],
        "wait_for": "div[class*='article-body'], article",
        "wait_for_timeout": 20000,
        "scroll_times": 8,
        "scroll_delay_ms": 800,
    },
    "tradewindsnews.com": {
        "content_selectors": [
            # TradeWinds 主体正文通常在 story-content / article-body
            "div[class*='story-content']",
            "div[class*='story-body']",
            "div[class*='article-body']",
            "div[class*='article-content']",
            "div[class*='content-body']",
            "article",
            "main article",
            "main",
        ],
        "remove_selectors": [
            "nav", "header", "footer", "aside",
            "div[class*='related']",
            "div[class*='newsletter']",
            "div[class*='subscribe']",
            "div[class*='share']",
            "div[class*='social']",
            "div[class*='comments']",
            "div[class*='paywall']",
            # 移除图片、图表及其备注/说明文字，避免抓到无关数据
            "figure",
            "figcaption",
            "div[class*='image-caption']",
            "div[class*='photo-caption']",
            "div[class*='caption']",
            "div[class*='image-credit']",
            "div[class*='credit']",
            "img",
            "div[class*='gallery']",
            "div[class*='slideshow']",
        ],
        "wait_for": "div[class*='story-content'], div[class*='article-body'], article",
        "wait_for_timeout": 25000,
        "scroll_times": 10,
        "scroll_delay_ms": 800,
    },
    "seatrade-maritime.com": {
        "content_selectors": [
            "div[class*='article-body']",
            "div[class*='field-body']",
            "div[class*='content']",
            "article",
            "main article",
            "main",
        ],
        "remove_selectors": [
            "nav", "header", "footer", "aside",
            "div[class*='related']",
            "div[class*='newsletter']",
            "div[class*='subscribe']",
            "div[class*='share']",
            "div[class*='social']",
            "div[class*='comments']",
            "figure",
            "figcaption",
            "div[class*='image-caption']",
            "div[class*='photo-caption']",
            "div[class*='caption']",
            "div[class*='image-credit']",
            "img",
        ],
        "wait_for": "div[class*='article-body'], article",
        "wait_for_timeout": 20000,
        "scroll_times": 8,
        "scroll_delay_ms": 800,
    },
    "imarine.cn": {
        # iMarine 文章页为静态 HTML，crawl4ai 偶尔会把相邻文章/缓存页混在一起，
        # 因此直接使用 requests + BeautifulSoup 提取，避免浏览器状态干扰。
        "content_selectors": [
            "article div.entry-content",
            "div.entry-content",
            "article",
            "main article",
        ],
        "remove_selectors": [
            "nav", "header", "footer", "aside",
            "div[class*='related']",
            "div[class*='share']",
            "div[class*='social']",
            "div[class*='comments']",
            "figure",
            "figcaption",
            "div[class*='image-caption']",
            "div[class*='photo-caption']",
            "div[class*='caption']",
            "div[class*='image-credit']",
            "img",
            "script", "style", "noscript",
        ],
        "use_crawl4ai": False,
        "scroll_times": 0,
        "scroll_delay_ms": 0,
    },
    "eworldship.com": {
        # 国际船舶网使用 GBK 编码，且 HTTPS 证书较旧（DH_KEY_TOO_SMALL），
        # 因此禁用 crawl4ai，使用 requests + 降低 SSL 安全等级 + 强制 GBK 解码。
        "content_selectors": [
            "#nArticle .content",
            "#article_content",
            ".content-detail",
            ".article-content",
            ".news-content",
            "article",
        ],
        "remove_selectors": [
            "nav", "header", "footer", "aside",
            "div[class*='related']",
            "div[class*='share']",
            "div[class*='social']",
            "div[class*='comments']",
            "div[class*='sidebar']",
            "div[class*='header']",
            "div[class*='menu']",
            "div[class*='ad']",
            "figure",
            "figcaption",
            "div[class*='image-caption']",
            "div[class*='photo-caption']",
            "div[class*='caption']",
            "div[class*='image-credit']",
            "img",
            "script", "style", "noscript",
        ],
        "use_crawl4ai": False,
        "force_encoding": "gbk",
        "prefetch_url": "https://www.eworldship.com/",
        "scroll_times": 0,
        "scroll_delay_ms": 0,
    },
}


def _get_site_rule(url: str) -> Dict[str, Any]:
    """根据 URL 域名匹配站点规则，无匹配返回通用规则。"""
    hostname = urlparse(url).hostname or ""
    hostname = hostname.lower().lstrip("www.")
    for domain, rule in SITE_CONTENT_RULES.items():
        if domain in hostname:
            return rule
    return {
        "content_selectors": [
            "article",
            "main article",
            "div[class*='article-body']",
            "div[class*='article-content']",
            "div[class*='content-detail']",
            "div[class*='news-content']",
            "div[class*='post-content']",
            "div[class*='entry-content']",
            "div[class*='detail']",
            "div[class*='content']",
            "main",
            "body",
        ],
        "remove_selectors": [
            "nav", "header", "footer", "aside",
            "figure", "figcaption",
            "div[class*='image-caption']",
            "div[class*='photo-caption']",
            "div[class*='caption']",
            "div[class*='image-credit']",
            "img",
        ],
        # 默认禁用 crawl4ai：中文站点多为静态 HTML，requests + 文本密度提取更稳定、更快
        "wait_for": None,
        "wait_for_timeout": 5000,
        "scroll_times": 0,
        "scroll_delay_ms": 0,
        "use_crawl4ai": False,
    }


def _visible_text(element) -> str:
    """递归获取可见文本，跳过 display:none / script/style/comment。"""
    texts = []
    for child in element.descendants:
        if isinstance(child, Comment):
            continue
        if child.name in ("script", "style", "noscript"):
            continue
        if child.name:
            # 跳过明显隐藏的元素
            style = child.get("style", "")
            if "display:none" in style.replace(" ", "").lower():
                continue
        if child.string:
            texts.append(child.string)
    return " ".join(t for t in texts if t.strip())


def _count_chinese_chars(text: str) -> int:
    """统计中文字符数量。"""
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def _extract_by_text_density(soup) -> str:
    """
    基于文本密度的正文提取兜底算法。
    当标准选择器无法命中正文容器时，通过评分找出最可能是正文的块级元素。
    """
    candidates = []
    # 候选标签：常见正文容器
    for el in soup.find_all(["article", "section", "div", "main"]):
        text = el.get_text(separator="\n", strip=True)
        if len(text) < 100:
            continue

        # 计算链接密度：链接文本占比过高的块通常是导航/列表，不是正文
        link_text = " ".join(a.get_text(strip=True) for a in el.find_all("a"))
        link_ratio = len(link_text) / len(text) if text else 0
        if link_ratio > 0.4:
            continue

        # 中文字符密度（中文站点）
        chinese_count = _count_chinese_chars(text)
        chinese_ratio = chinese_count / len(text) if text else 0

        # 评分：文本长度 + 中文密度奖励 - 链接密度惩罚
        score = len(text)
        if chinese_ratio > 0.3:
            score += chinese_count * 2
        score -= len(link_text) * 2

        candidates.append((score, len(text), text, el.name, el.get("class")))

    if not candidates:
        return ""

    # 按评分排序，取最高分的文本
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_text = candidates[0][2]
    best_text = re.sub(r"\n{3,}", "\n\n", best_text)
    return best_text.strip()


def _fix_common_mojibake(text: str) -> str:
    """修复 crawl4ai/requests 把 GBK 字节误按 Latin-1 解码导致的乱码。"""
    if not text:
        return text
    try:
        # Latin-1 是单字节编码，任何 str 都能 encode；再按 GBK 解码
        decoded = text.encode("latin-1").decode("gbk", errors="ignore")
        cjk_count = sum(1 for ch in decoded if "\u4e00" <= ch <= "\u9fff")
        # 修复后中文字符占比高才采用
        if cjk_count > len(decoded) * 0.3:
            return decoded
    except Exception:
        pass
    return text


def _clean_html_text(html: str, rule: Dict[str, Any]) -> str:
    """用 BeautifulSoup 按站点规则抽取并清理正文。"""
    if not html:
        return ""
    html = _fix_common_mojibake(html)
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""

    # 移除脚本、样式、注释、噪音元素
    for selector in ["script", "style", "noscript"] + rule.get("remove_selectors", []):
        try:
            for el in soup.select(selector):
                el.decompose()
        except Exception:
            continue

    # 移除注释节点
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # 尝试正文选择器，取最长且过短的候选
    best_content = ""
    content_selectors = rule.get("content_selectors", ["article", "main", "body"])
    for selector in content_selectors:
        try:
            el = soup.select_one(selector)
        except Exception:
            continue
        if not el:
            continue
        text = el.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        # 优先选足够长且更长的文本
        if len(text) > len(best_content):
            best_content = text

    # 若按选择器仍过短，尝试文本密度算法提取正文
    if len(best_content) < CONTENT_MIN_LENGTH:
        fallback = _extract_by_text_density(soup)
        if len(fallback) > len(best_content):
            best_content = fallback

    # 最后兜底：整页可见文本
    if len(best_content) < CONTENT_MIN_LENGTH:
        fallback = _visible_text(soup)
        if len(fallback) > len(best_content):
            best_content = fallback

    return best_content.strip()


def _collapse_repeated_phrases(text: str) -> str:
    """
    折叠连续重复的多词短语（抓取产物常把标题/副标题/作者重复多份）。
    按词扫描：发现首个连续重复块后只保留一份并跳过后续所有重复，
    对单行 blob（词间只有空格、没有换行）同样有效。
    """
    words = text.split()
    out = []
    i, n = 0, len(words)
    while i < n:
        k = 0
        for cand in range(2, min(20, (n - i) // 2) + 1):
            if words[i:i + cand] == words[i + cand:i + 2 * cand]:
                k = cand
                break
        if k:
            out.extend(words[i:i + k])
            i += 2 * k
            while words[i:i + k] == out[-k:]:
                i += k
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


def _clean_tradewinds_text(text: str) -> str:
    """
    TradeWinds 站点正文专用清洗。

    TradeWinds 文章页 DOM 因响应式/ hydrate 结构会把标题、作者、日期等输出多份，
    正文之后还会带上话题标签、"Copy link / Be notified / Read more" 以及整版相关推荐文章，
    直接提取会得到大量重复与无关内容。本函数只保留正文主数据：
    0. 去掉 tag-soup 文本首词残留的裸 "html"，并截断行内出现的页脚功能链接
       （"Copy link / Be notified / Read more" 之后的区域单行 blob 中不以整行出现）；
    1. 丢弃第一处 "Published ..." 之前的 byline 区域（标题/副标题/作者/地点/栏目，均重复多份）；
    2. 丢弃 "Copy link / Be notified / Read more" 之后的相关推荐区域；
    3. 删除 Published/Updated 元信息行；
    4. 合并行内连续重复单词（如 "Bangkok Bangkok"）与连续重复的多词短语（标题被重复多份）；
    5. 全局去除完全重复的行（标题/导语被重复多份时只保留首次）。
    """
    if not text:
        return text

    # 0) 去掉 tag-soup 文本首词残留的裸 "html"，并截断行内页脚功能链接
    text = re.sub(r"^html\s+", "", text.strip(), flags=re.IGNORECASE)
    for marker in ("Copy link", "Be notified", "Read more"):
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx].rstrip()

    raw_lines = [l.strip() for l in text.split("\n")]

    # 1) 定位正文起点：第一处 Published 之前的区域是重复多份的 byline 噪音
    start_idx = 0
    for i, line in enumerate(raw_lines):
        if re.match(r"^Published\s+\d{1,2}\s+\w+\s+\d{4}", line):
            start_idx = i
            break

    # 2) 定位正文终点：相关推荐/功能链接区域起点
    end_idx = len(raw_lines)
    for i in range(start_idx, len(raw_lines)):
        if raw_lines[i] in ("Copy link", "Be notified", "Read more"):
            end_idx = i
            break

    lines = raw_lines[start_idx:end_idx]

    # 3) 去掉 Published/Updated 元信息行
    lines = [l for l in lines if not re.match(r"^(Published|Updated)\s+\d{1,2}\s+\w+\s+\d{4}", l)]

    # 4) 合并行内连续重复：单词级（"Bangkok Bangkok"）+ 多词短语级（标题/作者重复多份）
    cleaned_lines = []
    for line in lines:
        line = re.sub(r"\b([A-Za-z]+)(?:\s+\1\b)+", r"\1", line)
        line = _collapse_repeated_phrases(line)
        cleaned_lines.append(line)

    # 5) 全局去除完全重复的非短行，保留首次出现
    seen = set()
    deduped = []
    for line in cleaned_lines:
        if len(line) >= 10:
            key = re.sub(r"\s+", " ", line).lower()
            if key in seen:
                continue
            seen.add(key)
        deduped.append(line)

    text = "\n".join(l for l in deduped if l)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _clean_markdown(text: str) -> str:
    """清理 crawl4ai 返回的 markdown：去掉图片/链接 URL、截断页脚/相关文章等噪音。"""
    if not text:
        return text

    # 图片 markdown：保留 alt 文本，去掉 URL
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    # 链接 markdown：保留链接文本，去掉 URL
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # 去掉转义字符
    text = text.replace("\\(", "").replace("\\)", "")

    # 截断常见页脚/相关文章/导航标题
    footer_patterns = [
        r"##\s*Read more",
        r"##\s*Follow us",
        r"##\s*About us",
        r"##\s*Privacy",
        r"##\s*Cookies",
        r"###\s*About us",
        r"###\s*Contact us",
        r"###\s*Terms",
        r"###\s*Privacy",
        r"####\s*Privacy",
        r"####\s*Follow us",
        r"####\s*About us",
    ]
    cut = len(text)
    for pat in footer_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m and m.start() < cut:
            cut = m.start()
    if cut < len(text):
        text = text[:cut].strip()

    # 清理多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _smart_truncate(text: str, max_length: int = CONTENT_MAX_LENGTH) -> str:
    """按段落智能截断，避免断在句子中间。"""
    if len(text) <= max_length:
        return text
    cut = text.rfind("\n\n", 0, max_length)
    if cut < max_length * 0.8:
        for char in "。.?!？！":
            cut = text.rfind(char, 0, max_length)
            if cut >= max_length * 0.8:
                break
    if cut <= 0:
        cut = max_length
    return text[:cut + 1] + "\n\n...[内容已截断]"


def _is_content_too_short(content: str, title: str = "") -> bool:
    """判断提取到的内容是否明显过短。"""
    if not content:
        return True
    if len(content) < CONTENT_MIN_LENGTH:
        return True
    if title and len(content) < len(title) * 2:
        return True
    return False


def _extract_best_markdown(crawl_result) -> str:
    """从 crawl4ai 结果中选取最长的 markdown 文本。"""
    content = ""
    if not hasattr(crawl_result, "markdown") or not crawl_result.markdown:
        return content
    md = crawl_result.markdown
    for attr in ("fit_markdown", "cleaned_markdown", "raw_markdown"):
        candidate = getattr(md, attr, None) or ""
        if len(candidate) > len(content):
            content = candidate
    return content.strip()


async def _run_crawl4ai(url: str, rule: Dict[str, Any]) -> Optional[Any]:
    """使用 crawl4ai 抓取并返回 CrawlResult。"""
    if not CRAWL4AI_AVAILABLE:
        return None

    scroll_steps = rule.get("scroll_times", 5)
    scroll_delay = rule.get("scroll_delay_ms", 600) / 1000.0
    wait_for = rule.get("wait_for")
    wait_for_timeout = rule.get("wait_for_timeout", 20000)

    # 构建运行配置：浏览器渲染、整页滚动、等待正文容器、移除覆盖层
    run_config_kwargs = {
        "markdown_generator": DefaultMarkdownGenerator(),
        "page_timeout": 90000,
        "delay_before_return_html": 4.0,
        "scan_full_page": True,
        "max_scroll_steps": scroll_steps,
        "scroll_delay": scroll_delay,
        "remove_overlay_elements": True,
        "simulate_user": True,
        "ignore_body_visibility": True,
        "verbose": False,
    }
    # 仅当规则明确指定 wait_for 时才等待，避免默认规则因站点结构不匹配而超时
    if wait_for:
        run_config_kwargs["wait_for"] = wait_for
        run_config_kwargs["wait_for_timeout"] = wait_for_timeout
    else:
        # 不等待特定选择器时，给一个较短的页面基本加载时间即可
        run_config_kwargs["wait_for_timeout"] = 1000

    run_config = CrawlerRunConfig(**run_config_kwargs)

    browser_config = BrowserConfig(
        headless=True,
        verbose=False,
        ignore_https_errors=True,
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        # 额外 asyncio.wait_for 硬超时保险，避免 crawler 内部忽略 timeout 导致挂死
        crawl_result = await asyncio.wait_for(
            crawler.arun(url=url, config=run_config),
            timeout=110,
        )
        return crawl_result


async def extract_article(
    url: str,
    title: str = "",
    source_platform: str = "",
    max_length: int = CONTENT_MAX_LENGTH,
    use_crawl4ai: bool = True,
) -> Dict[str, Any]:
    """
    统一文章正文提取接口。

    Args:
        url: 文章链接
        title: 文章标题（用于判断内容是否过短）
        source_platform: 来源平台名称
        max_length: 最大保留字符数
        use_crawl4ai: 是否使用 crawl4ai

    Returns:
        {
            "success": bool,
            "content": str,          # 提取到的正文
            "title": str,            # 页面标题（如可获取）
            "url": str,
            "method": str,           # crawl4ai / html_fallback / requests_fallback / failed
            "error": str or None,
        }
    """
    result = {
        "success": False,
        "content": "",
        "title": title,
        "url": url,
        "method": "failed",
        "error": None,
    }

    if not url:
        result["error"] = "URL 为空"
        return result

    rule = _get_site_rule(url)
    hostname = (urlparse(url).hostname or "").lower()

    # ========== 第一步：crawl4ai 浏览器渲染抓取 ==========
    # 部分静态站点明确禁用 crawl4ai，直接用 requests 兜底，避免浏览器状态/缓存导致内容错乱
    if use_crawl4ai and rule.get("use_crawl4ai", True) and CRAWL4AI_AVAILABLE:
        try:
            crawl_result = await _run_crawl4ai(url, rule)
        except Exception as e:
            crawl_result = None
            result["error"] = f"crawl4ai 异常: {str(e)[:200]}"

        if crawl_result is not None:
            # 尝试 markdown
            content = ""
            method = "crawl4ai"
            if getattr(crawl_result, "success", False):
                md_content = _extract_best_markdown(crawl_result)
                md_content = _clean_markdown(md_content)

                html = getattr(crawl_result, "html", "") or ""
                html_content = _clean_html_text(html, rule) if html else ""

                # TradeWinds 页面结构特殊：byline/标签/相关推荐会重复混入，做站点级清洗
                if "tradewindsnews.com" in hostname:
                    md_content = _clean_tradewinds_text(md_content)
                    html_content = _clean_tradewinds_text(html_content)

                # 选择最佳候选：
                # 1. HTML 清理结果若足够长且包含标题，优先使用（可去除图片 caption、footer 等噪音）
                # 2. 否则使用 markdown 结果
                candidates = []
                if not _is_content_too_short(md_content, title):
                    candidates.append((md_content, "crawl4ai"))
                html_valid = (
                    html_content
                    and not _is_content_too_short(html_content, title)
                    and (not title or title[:15].lower() in html_content.lower())
                )
                if html_valid:
                    candidates.append((html_content, "html_fallback"))

                content = ""
                method = "crawl4ai"
                if candidates:
                    # HTML 候选足够完整时优先，避免图片说明、footer 混入正文
                    if len(candidates) == 2 and len(html_content) >= max(
                        CONTENT_MIN_LENGTH, len(md_content) * 0.25
                    ):
                        content, method = html_content, "html_fallback"
                    else:
                        content, method = candidates[0]

                # 提取页面标题
                page_title = ""
                if html:
                    try:
                        soup = BeautifulSoup(html, "html.parser")
                        title_tag = soup.find("title")
                        if title_tag:
                            page_title = title_tag.get_text(strip=True)
                    except Exception:
                        pass
                result["title"] = page_title or title

                if content:
                    result["success"] = True
                    result["content"] = _smart_truncate(content, max_length)
                    result["method"] = method
                    return result
            else:
                err_msg = getattr(crawl_result, "error_message", None) or "crawl4ai 返回失败"
                result["error"] = err_msg

    # ========== 第二步：requests 同步兜底（crawl4ai 失败/不可用/内容为空） ==========
    def _requests_fetch(weak_ssl: bool = False, prefetch_url: Optional[str] = None):
        """weak_ssl=True 时同时使用 verify=False + 降低 OpenSSL 安全等级以兼容旧证书。"""
        import requests
        from requests.adapters import HTTPAdapter
        from requests.packages.urllib3.util.ssl_ import create_urllib3_context
        from requests.packages.urllib3.exceptions import InsecureRequestWarning

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }

        if weak_ssl:
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

            class WeakSSLAdapter(HTTPAdapter):
                def init_poolmanager(self, *args, **kwargs):
                    ctx = create_urllib3_context(ciphers="DEFAULT:@SECLEVEL=1")
                    ctx.set_ciphers("DEFAULT:!DH")
                    ctx.check_hostname = False
                    kwargs["ssl_context"] = ctx
                    return super().init_poolmanager(*args, **kwargs)

            session = requests.Session()
            session.mount("https://", WeakSSLAdapter())
            verify = False
        else:
            session = requests.Session()
            verify = True

        # 部分站点需要先访问首页建立会话，否则文章页会返回 404
        if prefetch_url:
            try:
                session.get(prefetch_url, timeout=30, verify=verify, headers=headers)
            except Exception:
                pass

        return session.get(url, timeout=30, verify=verify, headers=headers)

    prefetch_url = rule.get("prefetch_url")
    try:
        response = _requests_fetch(weak_ssl=False, prefetch_url=prefetch_url)
    except Exception as ssl_err:
        err_str = str(ssl_err).lower()
        if "ssl" in err_str or "certificate" in err_str or "dh_key" in err_str:
            try:
                response = _requests_fetch(weak_ssl=True, prefetch_url=prefetch_url)
            except Exception as e:
                result["error"] = f"crawl4ai 与 requests 兜底均失败: {str(e)[:200]}"
                return result
        else:
            result["error"] = f"crawl4ai 与 requests 兜底均失败: {str(ssl_err)[:200]}"
            return result

    if response.status_code == 200:
        forced_encoding = rule.get("force_encoding")
        if forced_encoding:
            try:
                html = response.content.decode(forced_encoding, errors="ignore")
            except Exception:
                html = response.text
        else:
            html = response.text
        if html:
            fallback = _clean_html_text(html, rule)
            if "tradewindsnews.com" in hostname:
                fallback = _clean_tradewinds_text(fallback)
            if len(fallback) > 100:
                result["success"] = True
                result["content"] = _smart_truncate(fallback, max_length)
                result["method"] = "requests_fallback"
                result["error"] = None
                try:
                    soup = BeautifulSoup(html, "html.parser")
                    title_tag = soup.find("title")
                    if title_tag:
                        result["title"] = title_tag.get_text(strip=True) or title
                except Exception:
                    pass
                return result

    result["error"] = result["error"] or f"crawl4ai 与 requests 兜底均失败: HTTP {response.status_code}"
    return result
