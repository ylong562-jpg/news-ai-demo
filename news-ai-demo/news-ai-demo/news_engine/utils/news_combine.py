"""
新闻合并模块 - 增强版
添加了source_platform和去重功能
只合并今天的新闻
支持自动扫描爬虫模块，区分VPN和非VPN爬虫
支持获取新闻页面markdown内容
"""

import sys
import os
import re

# 确保当前目录在 Python 路径中（支持 PyInstaller 打包环境）
if getattr(sys, 'frozen', False):
    # PyInstaller 环境：exe 所在目录
    exe_dir = os.path.dirname(sys.executable)
    # 添加 _internal（第三方库）
    internal_dir = os.path.join(exe_dir, '_internal')
    if os.path.exists(internal_dir):
        sys.path.insert(0, internal_dir)
    # 添加当前文件所在目录（news/）
    current_file_dir = os.path.dirname(os.path.abspath(__file__))
    if current_file_dir not in sys.path:
        sys.path.insert(0, current_file_dir)
else:
    # 正常 Python 环境
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
import asyncio
import importlib
import importlib.util
import inspect
import multiprocessing as mp
from pathlib import Path
from confidence_config import get_module_platform, calculate_confidence, is_absolute_truth, detect_platform_from_url
from title_utils import normalize_title, title_similarity, get_title_signature

# 单个爬虫来源最大执行时间（秒），超时后强制终止并跳过该来源
CRAWLER_SOURCE_TIMEOUT = 120
# 同时运行的爬虫来源数量上限，避免 chromedriver/Chrome 资源争用
MAX_CONCURRENT_SOURCES = 3

# 导入crawl4ai用于获取页面markdown
from content_extractor import extract_article

# 年份前缀检测正则：匹配 summary 开头是否已有 "【该新闻的日期是YYYY年"
_DATE_PREFIX_RE = re.compile(r'^【该新闻的日期是\d{4}年')


def _build_date_prefix(news_date):
    """根据新闻日期构建年份前缀说明。"""
    if not news_date:
        return ''
    pub_year_match = re.search(r'(\d{4})', str(news_date))
    if not pub_year_match:
        return ''
    pub_year = pub_year_match.group(1)
    return (
        f"【该新闻的日期是{pub_year}年，"
        f"如果每笔交易的订单信息没有明确的年份信息，默认为新闻日期的年份信息。】\n\n"
    )


def _add_date_prefix_to_summary(news_list):
    """在每条新闻的 summary 开头注入年份前缀（不修改正文其余部分）。"""
    for news in news_list:
        summary = news.get('summary', '')
        if not summary:
            continue
        prefix = _build_date_prefix(news.get('date', ''))
        if prefix and not _DATE_PREFIX_RE.match(str(summary)):
            news['summary'] = prefix + str(summary)
    return news_list


def discover_crawlers(enabled_modules=None):
    """
    自动扫描 web_links 目录下的所有爬虫模块
    
    Args:
        enabled_modules: 可选，指定要启用的模块名列表（如 ['scrapping_clarksons_links']）。
                        传入时只有列表中的模块会被标记为 enabled=True。
    
    Returns:
        tuple: (no_vpn_crawlers, vpn_crawlers, shipyard_crawlers)
            - no_vpn_crawlers: 不需要VPN的爬虫配置列表
            - vpn_crawlers: 需要VPN的爬虫配置列表
            - shipyard_crawlers: 船厂网站爬虫配置列表
    """
    # 检测是否在 PyInstaller 打包环境中
    if getattr(sys, 'frozen', False):
        # PyInstaller 环境：从 exe 所在目录找到 app/news/web_links
        exe_dir = Path(sys.executable).parent
        base_path = exe_dir / "app" / "news" / "web_links"
    else:
        # 正常 Python 环境
        base_path = Path(__file__).parent / "web_links"
    
    no_vpn_path = base_path / "no_vpn_needed"
    vpn_path = base_path / "vpn_needed"
    shipyard_path = base_path / "shipyard_website"
    
    # 调试信息
    print(f"   [调试] 查找 web_links 路径: {base_path}")
    print(f"   [调试] 路径存在: {base_path.exists()}")
    print(f"   [调试] no_vpn_needed 存在: {no_vpn_path.exists()}")
    print(f"   [调试] vpn_needed 存在: {vpn_path.exists()}")
    print(f"   [调试] shipyard_website 存在: {shipyard_path.exists()}")
    
    no_vpn_crawlers = []
    vpn_crawlers = []
    shipyard_crawlers = []
    
    # 扫描不需要VPN的爬虫
    if no_vpn_path.exists():
        for file in no_vpn_path.glob("scrapping_*.py"):
            module_name = file.stem  # 去掉.py后缀
            try:
                # 动态导入模块
                spec = importlib.util.spec_from_file_location(
                    f"web_links.no_vpn_needed.{module_name}", 
                    file
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                
                # 查找爬虫函数 (命名约定: scrape_<name>_news)
                func_name = module_name.replace("scrapping_", "scrape_").replace("_links", "_news")
                if hasattr(module, func_name):
                    crawl_func = getattr(module, func_name)
                    is_enabled = (enabled_modules is None) or (module_name in enabled_modules)
                    no_vpn_crawlers.append({
                        "module": module_name,
                        "func": crawl_func,
                        "enabled": is_enabled,
                        "needs_vpn": False
                    })
                    status = "启用" if is_enabled else "禁用"
                    print(f"   📡 发现爬虫 [无需VPN][{status}]: {module_name}")
            except Exception as e:
                print(f"   ⚠️ 加载失败 [{module_name}]: {e}")
    
    # 扫描需要VPN的爬虫
    if vpn_path.exists():
        for file in vpn_path.glob("scrapping_*.py"):
            module_name = file.stem
            try:
                spec = importlib.util.spec_from_file_location(
                    f"web_links.vpn_needed.{module_name}", 
                    file
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                
                func_name = module_name.replace("scrapping_", "scrape_").replace("_links", "_news")
                if hasattr(module, func_name):
                    crawl_func = getattr(module, func_name)
                    is_enabled = (enabled_modules is None) or (module_name in enabled_modules)
                    vpn_crawlers.append({
                        "module": module_name,
                        "func": crawl_func,
                        "enabled": is_enabled,
                        "needs_vpn": True
                    })
                    status = "启用" if is_enabled else "禁用"
                    print(f"   📡 发现爬虫 [需VPN][{status}]: {module_name}")
            except Exception as e:
                print(f"   ⚠️ 加载失败 [{module_name}]: {e}")
    
    # 扫描船厂网站爬虫（支持任意 .py 文件名，函数名约定: scrape_<name>_news）
    if shipyard_path.exists():
        for file in shipyard_path.glob("*.py"):
            # 跳过 __init__.py 和 __pycache__
            if file.name.startswith("_"):
                continue
            module_name = file.stem
            try:
                spec = importlib.util.spec_from_file_location(
                    f"web_links.shipyard_website.{module_name}", 
                    file
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                
                # 函数名约定: scrape_<module_name>_news
                func_name = f"scrape_{module_name}_news"
                if hasattr(module, func_name):
                    crawl_func = getattr(module, func_name)
                    is_enabled = (enabled_modules is None) or (module_name in enabled_modules)
                    shipyard_crawlers.append({
                        "module": module_name,
                        "func": crawl_func,
                        "enabled": is_enabled,
                        "needs_vpn": True  # 船厂网站归入VPN类
                    })
                    status = "启用" if is_enabled else "禁用"
                    print(f"   📡 发现爬虫 [船厂网站][{status}]: {module_name}")
            except Exception as e:
                print(f"   ⚠️ 加载失败 [{module_name}]: {e}")
    
    return no_vpn_crawlers, vpn_crawlers, shipyard_crawlers


async def fetch_news_from_source(config, driver_path):
    """
    从单个来源获取新闻，并添加source_platform标识
    
    Parameters:
        config (dict): 爬虫配置（可包含 target_date/start_date/end_date）
        driver_path (str): chromedriver路径
    
    Returns:
        list: 带有source_platform的新闻列表
    """
    module_name = config["module"]
    crawl_func = config["func"]
    platform_name = get_module_platform(module_name)
    target_date = config.get("target_date")
    start_date = config.get("start_date")
    end_date = config.get("end_date")
    
    try:
        date_hint = ""
        if start_date and end_date and start_date != end_date:
            date_hint = f" ({start_date} ~ {end_date})"
        elif target_date:
            date_hint = f" ({target_date})"
        print(f"📡 正在获取: {platform_name}{date_hint}...")

        # 根据爬虫函数签名智能决定传参：
        # 1. 支持 start_date/end_date 的爬虫：透传完整日期范围，由爬虫内部控制滚动/过滤
        # 2. 仅支持 target_date 的爬虫：透传目标日期并启用单日过滤
        # 3. 仅支持 filter_today 的爬虫：强制 filter_today=False，取消内置 today 限制，统一由外层按日期范围过滤
        sig = inspect.signature(crawl_func)
        params = sig.parameters

        has_start_date = "start_date" in params
        has_end_date = "end_date" in params
        has_target_date = "target_date" in params
        has_filter_today = "filter_today" in params

        call_kwargs = {"headless": True}

        if has_start_date and has_end_date:
            call_kwargs["start_date"] = start_date
            call_kwargs["end_date"] = end_date
        elif has_target_date:
            effective_target = target_date or end_date
            if effective_target:
                call_kwargs["target_date"] = effective_target
            if has_filter_today:
                call_kwargs["filter_today"] = True
        elif has_filter_today:
            call_kwargs["filter_today"] = False

        # 保留调用方自定义参数（如 max_news、scroll_times 等）
        for key in ("scroll_times", "pause_time", "max_news"):
            if key in config:
                call_kwargs[key] = config[key]

        try:
            news_list = await crawl_func(driver_path, **call_kwargs)
        except TypeError as te:
            # 若爬虫函数不支持 target_date，则移除后重试
            if target_date and "target_date" in str(te):
                print(f"   ⚠️ {platform_name}: 该爬虫不支持 target_date，回退到默认日期获取")
                fallback_kwargs = {k: v for k, v in call_kwargs.items() if k != "target_date"}
                news_list = await crawl_func(driver_path, **fallback_kwargs)
            else:
                raise

        if not news_list:
            print(f"   ⚠️ {platform_name}: 未获取到数据")
            return []

        # 添加source_platform标识
        for news in news_list:
            # 如果模块映射失败，尝试从 URL 兜底识别
            if not platform_name or platform_name == "unknown":
                platform_name = detect_platform_from_url(news.get('link', '')) or platform_name
            news['source_platform'] = platform_name
            # 初始化置信度（后续会根据重复次数调整）
            news['confidence_score'] = calculate_confidence(platform_name, 1, url=news.get('link', ''))
            news['duplicate_count'] = 1  # 初始重复次数为1

        print(f"   ✅ {platform_name}: 获取 {len(news_list)} 条新闻")
        return news_list

    except Exception as e:
        print(f"   [失败] {platform_name}: 获取失败 - {str(e)[:50]}")
        return []


def _source_worker(config, driver_path, queue):
    """在独立进程中运行单个来源的爬虫，并通过队列返回结果。"""
    # 子进程标准输出/错误设置为 UTF-8 + 替换模式，避免控制台编码不支持时崩溃
    try:
        import sys
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        result = asyncio.run(fetch_news_from_source(config, driver_path))
        queue.put(("ok", result))
    except Exception as e:
        queue.put(("error", e))


def _run_source_isolated(config, driver_path, timeout=CRAWLER_SOURCE_TIMEOUT):
    """
    在独立子进程中运行单个爬虫，超时后强制终止并返回 []。
    这样可以避免某个来源的同步阻塞代码卡住整个事件循环。
    """
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(target=_source_worker, args=(config, driver_path, queue))
    process.start()
    process.join(timeout=timeout)

    if process.is_alive():
        platform_name = get_module_platform(config["module"])
        print(f"   [超时] {platform_name}: 单源超时（{timeout}秒），强制终止并跳过")
        process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join()
        return []

    try:
        status, payload = queue.get(timeout=1)
        if status == "error":
            raise payload
        return payload
    except Exception as e:
        platform_name = get_module_platform(config["module"])
        print(f"   [失败] {platform_name}: 获取失败 - {str(e)[:50]}")
        return []


async def run_crawlers(crawler_configs, driver_path, stage_name=""):
    """
    运行一批爬虫

    Parameters:
        crawler_configs (list): 爬虫配置列表（可包含 target_date 字段）
        driver_path (str): chromedriver路径
        stage_name (str): 阶段名称（用于显示）

    Returns:
        list: 获取到的新闻列表
    """
    if not crawler_configs:
        return []

    if stage_name:
        print(f"\n{'='*60}")
        print(f"📦 {stage_name}")
        print(f"{'='*60}")

    # 限制并发数量，避免同时启动过多 Chrome/chromedriver 导致连接超时
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SOURCES)

    async def _run_one(config):
        async with semaphore:
            # 在独立进程中运行单个爬虫，子进程内的阻塞不会影响主事件循环
            return await asyncio.to_thread(_run_source_isolated, config, driver_path, CRAWLER_SOURCE_TIMEOUT)

    tasks = [_run_one(config) for config in crawler_configs if config["enabled"]]

    # 等待所有任务完成
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 合并所有结果
    all_news = []
    error_count = 0

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            platform_name = get_module_platform(crawler_configs[i]["module"])
            print(f"❌ {platform_name}: 发生异常 - {str(result)[:50]}")
            error_count += 1
        elif isinstance(result, list):
            all_news.extend(result)

    print(f"\n📊 {stage_name}完成: 共 {len(all_news)} 条新闻，{error_count} 个源失败")

    return all_news


def wait_for_vpn():
    """
    等待用户手动打开VPN并确认
    """
    print("\n" + "="*60)
    print("⏸️ 暂停执行 - 等待VPN连接")
    print("="*60)
    print("\n请执行以下操作:")
    print("  1. 手动打开VPN软件并连接")
    print("  2. 确认VPN连接成功后，按回车键继续...")
    print("\n提示: 如果不使用VPN，这些爬虫可能无法正常访问目标网站")
    print("="*60)
    
    input("\n👉 按回车键继续执行需要VPN的爬虫...")
    
    print("\n✅ 继续执行...")


def _normalize_date_range(start_date=None, end_date=None):
    """规范化日期范围，返回 (start_date, end_date) 字符串元组。"""
    from datetime import datetime as _dt
    today = _dt.now().strftime('%Y-%m-%d')
    if not start_date and not end_date:
        return today, today
    if not start_date:
        start_date = end_date
    if not end_date:
        end_date = start_date
    return start_date, end_date


def _iter_date_range(start_date, end_date):
    """迭代日期范围内的每一天（含起止），返回 YYYY-MM-DD 字符串。"""
    from datetime import datetime as _dt, timedelta as _td
    start = _dt.strptime(start_date, '%Y-%m-%d').date()
    end = _dt.strptime(end_date, '%Y-%m-%d').date()
    if start > end:
        start, end = end, start
    current = start
    while current <= end:
        yield current.strftime('%Y-%m-%d')
        current += _td(days=1)


def _expand_crawlers_by_date(crawler_configs, start_date, end_date):
    """将爬虫配置按日期展开，每个爬虫每天一个任务。
    支持 start_date/end_date 的爬虫不展开，由爬虫内部按范围处理。
    仅支持 target_date 的爬虫按日期范围展开。
    不支持任何日期参数的爬虫只执行一次，外层统一按日期范围过滤。"""
    expanded = []
    date_range = list(_iter_date_range(start_date, end_date))
    for config in crawler_configs:
        cfg = dict(config)
        cfg["start_date"] = start_date
        cfg["end_date"] = end_date

        if not config.get("enabled", True):
            expanded.append(cfg)
            continue

        crawl_func = config.get("func")
        supports_target_date = False
        supports_range = False
        if crawl_func and callable(crawl_func):
            try:
                params = inspect.signature(crawl_func).parameters
                supports_target_date = "target_date" in params
                supports_range = "start_date" in params and "end_date" in params
            except Exception:
                supports_target_date = False
                supports_range = False

        if supports_range:
            # 支持完整日期范围：只执行一次，由爬虫内部处理滚动和过滤
            expanded.append(cfg)
        elif supports_target_date:
            # 仅支持 target_date：按日期范围每天展开
            for date_str in date_range:
                day_cfg = dict(cfg)
                day_cfg["target_date"] = date_str
                expanded.append(day_cfg)
        else:
            # 不支持日期参数：只执行一次，使用 end_date 作为提示，外层统一过滤
            cfg["target_date"] = end_date
            expanded.append(cfg)
    return expanded


async def news_combine(vpn_mode='all', enabled_modules=None, start_date=None, end_date=None):
    """
    合并所有新闻源的数据，并添加source_platform标识
    支持按vpn_mode分别执行：all/no_vpn/vpn/shipyard/wechat_only
    支持指定日期范围（start_date/end_date），默认今天。
    
    Parameters:
        vpn_mode (str): 'all' 运行所有, 'no_vpn' 只运行不需要VPN的, 
                        'vpn' 只运行需要VPN的, 'shipyard' 只运行船厂网站的,
                        'wechat_only' 跳过网站爬虫（只由上层获取公众号）
        enabled_modules (list): 可选，指定要启用的爬虫模块名列表
        start_date (str): 开始日期，YYYY-MM-DD，默认今天
        end_date (str): 结束日期，YYYY-MM-DD，默认与 start_date 相同
    
    Returns:
        list: 合并后的新闻列表，每条新闻包含source_platform和confidence_score
    """
    # 微信公众号模式：不扫描也不执行任何网站爬虫
    if vpn_mode == 'wechat_only':
        print("\n" + "="*60)
        print("📱 微信公众号模式：跳过网站爬虫扫描")
        print("="*60)
        return []
    
    start_date, end_date = _normalize_date_range(start_date, end_date)
    single_day = start_date == end_date
    date_desc = start_date if single_day else f"{start_date} 至 {end_date}"
    
    driver_path = None  # 使用 webdriver-manager 自动下载驱动
    
    print("\n" + "="*60)
    print("🔍 扫描爬虫模块...")
    print(f"📅 目标日期: {date_desc}")
    print("="*60)
    
    # 自动发现所有爬虫模块
    no_vpn_crawlers, vpn_crawlers, shipyard_crawlers = discover_crawlers(enabled_modules=enabled_modules)
    
    # 按日期展开配置
    no_vpn_crawlers = _expand_crawlers_by_date(no_vpn_crawlers, start_date, end_date)
    vpn_crawlers = _expand_crawlers_by_date(vpn_crawlers, start_date, end_date)
    shipyard_crawlers = _expand_crawlers_by_date(shipyard_crawlers, start_date, end_date)
    
    print(f"\n📊 扫描结果:")
    print(f"   无需VPN: {len(no_vpn_crawlers)} 个任务")
    print(f"   需要VPN: {len(vpn_crawlers)} 个任务")
    print(f"   船厂网站: {len(shipyard_crawlers)} 个任务")
    print(f"   总计: {len(no_vpn_crawlers) + len(vpn_crawlers) + len(shipyard_crawlers)} 个任务")
    
    all_news = []
    
    # 第一阶段：执行不需要VPN的爬虫
    if vpn_mode in ('all', 'no_vpn') and no_vpn_crawlers:
        no_vpn_news = await run_crawlers(
            no_vpn_crawlers, 
            driver_path, 
            stage_name=f"获取无需VPN的新闻 ({date_desc})"
        )
        all_news.extend(no_vpn_news)
    elif vpn_mode in ('all', 'no_vpn'):
        print("\n⚠️ 没有发现无需VPN的爬虫")
    
    # 第二阶段：等待用户打开VPN，然后执行需要VPN的爬虫
    if vpn_mode in ('all', 'vpn') and vpn_crawlers:
        # 等待用户手动打开VPN
        wait_for_vpn()
        
        vpn_news = await run_crawlers(
            vpn_crawlers, 
            driver_path, 
            stage_name=f"获取需要VPN的新闻 ({date_desc})"
        )
        all_news.extend(vpn_news)
    elif vpn_mode in ('all', 'vpn'):
        print("\nℹ️ 没有发现需要VPN的爬虫")
    
    # 第三阶段：执行船厂网站爬虫
    # no_vpn 模式同样执行船厂网站爬虫（用户明确船厂网站无需 VPN）
    if vpn_mode in ('all', 'no_vpn', 'shipyard') and shipyard_crawlers:
        if vpn_mode == 'shipyard':
            wait_for_vpn()
        
        shipyard_news = await run_crawlers(
            shipyard_crawlers, 
            driver_path, 
            stage_name=f"获取船厂网站新闻 ({date_desc})"
        )
        all_news.extend(shipyard_news)
    elif vpn_mode in ('all', 'no_vpn', 'shipyard'):
        print("\nℹ️ 没有发现船厂网站爬虫")
    
    # 过滤到指定日期范围
    all_news = filter_news_by_date_range(all_news, start_date, end_date)
    
    print("\n" + "="*60)
    print(f"📊 最终获取完成: 共 {len(all_news)} 条新闻")
    print("="*60)
    
    return all_news


def _normalize_url_for_dedup(url: str) -> str:
    """
    为 URL 去重生成统一 key：小写、去首尾空格、移除末尾 '/' 与 'www.' 前缀。
    不调用 AI，仅做字符串归一化。
    """
    if not isinstance(url, str):
        return ""
    normalized = url.strip().lower()
    # 去掉 www. 前缀（保留域名其余部分）
    if normalized.startswith("www."):
        normalized = normalized[4:]
    # 去掉末尾斜杠
    normalized = normalized.rstrip("/")
    return normalized


def pre_deduplicate_by_url(news_list):
    """
    前置 URL 去重：在 news_combine 整合所有新闻后执行。
    同一 URL（忽略大小写）视为绝对重复，直接删除重复条目，仅保留第一条。
    不调用 AI。

    Parameters:
        news_list (list): 新闻列表

    Returns:
        list: 去重后的新闻列表
    """
    if not news_list:
        return []

    seen_urls = set()
    unique_news = []
    duplicate_count = 0

    for news in news_list:
        link = news.get('link', '')
        if not link:
            # 没有 URL 的新闻无法通过 URL 判重，保留
            unique_news.append(news)
            continue

        url_key = _normalize_url_for_dedup(link)
        if url_key in seen_urls:
            duplicate_count += 1
            print(f"URL 前置去重删除: {news.get('title', '')[:40]}... ({link[:60]})")
            continue

        seen_urls.add(url_key)
        unique_news.append(news)

    print(f"\n✅ URL 前置去重完成: {len(news_list)} -> {len(unique_news)} 条，删除 {duplicate_count} 条")
    return unique_news


def remove_duplicate_news(news_list, similarity_threshold=0.7):
    """
    根据标题相似度去除重复新闻（中文友好的 Jaccard 相似度版）
    同时更新重复次数和置信度

    针对中文标题没有空格分隔的问题，使用字符级 2-gram Jaccard 相似度，
    使相近的中文标题能被正确识别为重复。

    Parameters:
        news_list (list): 新闻列表
        similarity_threshold (float): 相似度阈值

    Returns:
        list: 去重后的新闻列表，包含更新后的duplicate_count和confidence_score
    """

    unique_news = []

    for news in news_list:
        is_duplicate = False
        current_title = news.get('title', '')
        current_platform = news.get('source_platform', 'unknown')

        # 与已保留的新闻进行相似度比较
        for kept_news in unique_news:
            kept_title = kept_news.get('title', '')

            # 使用中文友好的标题相似度
            similarity = title_similarity(current_title, kept_title)

            if similarity >= similarity_threshold:
                print(f"发现相似新闻: {news['title'][:40]}...")
                print(f"  与已保留新闻相似度: {similarity:.2f}")
                print(f"  来源1: {current_platform}")
                print(f"  来源2: {kept_news.get('source_platform', 'unknown')}")

                # 更新重复次数
                kept_news['duplicate_count'] = kept_news.get('duplicate_count', 1) + 1

                # 重新计算置信度（对于非绝对真实的新闻）
                kept_link = kept_news.get('link', '')
                if not is_absolute_truth(kept_news.get('source_platform', ''), url=kept_link):
                    new_confidence = calculate_confidence(
                        kept_news.get('source_platform', ''),
                        kept_news['duplicate_count'],
                        url=kept_link
                    )
                    kept_news['confidence_score'] = new_confidence
                    print(f"  更新置信度: {new_confidence}% (重复{kept_news['duplicate_count']}次)")

                is_duplicate = True
                break

        if not is_duplicate:
            unique_news.append(news)

    print(f"\n去重完成: {len(news_list)} -> {len(unique_news)} 条")
    return unique_news


def basic_deduplicate_by_title(news_list):
    """
    基于标题的基本去重（精确匹配）
    用于初步去重

    先对标题进行标准化（去空白、零宽字符、Unicode归一化等）再比较，
    避免因为不可见字符或空白差异导致相同标题重复通过。

    Parameters:
        news_list (list): 新闻列表

    Returns:
        list: 去重后的新闻列表
    """
    seen_signatures = set()
    unique_news = []

    for news in news_list:
        title = news.get('title', '')
        signature = get_title_signature(title)
        if not signature:
            # 没有有效标题的新闻直接保留，避免误删
            unique_news.append(news)
            continue

        if signature not in seen_signatures:
            seen_signatures.add(signature)
            unique_news.append(news)
        else:
            # 找到已存在的相同标题新闻，更新重复次数
            for existing in unique_news:
                if get_title_signature(existing.get('title', '')) == signature:
                    existing['duplicate_count'] = existing.get('duplicate_count', 1) + 1
                    # 更新置信度
                    existing_link = existing.get('link', '')
                    if not is_absolute_truth(existing.get('source_platform', ''), url=existing_link):
                        existing['confidence_score'] = calculate_confidence(
                            existing.get('source_platform', ''),
                            existing['duplicate_count'],
                            url=existing_link
                        )
                    break

    return unique_news


def is_today_news(news_date):
    """
    检查新闻日期是否为今天
    支持多种日期格式
    
    Parameters:
        news_date (str): 新闻日期字符串
    
    Returns:
        bool: 是否为今天
    """
    if not news_date:
        return False
    
    today = datetime.now().date()
    
    # 尝试多种日期格式
    date_formats = [
        "%Y-%m-%d",      # 2024-03-09
        "%Y/%m/%d",      # 2024/03/09
        "%Y.%m.%d",      # 2024.03.09
        "%Y年%m月%d日",   # 2024年03月09日
        "%m-%d-%Y",      # 03-09-2024
        "%d-%m-%Y",      # 09-03-2024
        "%m/%d/%Y",      # 03/09/2024
    ]
    
    for fmt in date_formats:
        try:
            parsed_date = datetime.strptime(news_date.strip(), fmt).date()
            return parsed_date == today
        except ValueError:
            continue
    
    # 如果都解析失败，检查是否包含今天的日期字符串
    today_str = today.strftime("%Y-%m-%d")
    today_str_alt = today.strftime("%Y/%m/%d")
    today_str_cn = today.strftime("%Y年%m月%d日")
    
    if news_date.strip() in [today_str, today_str_alt, today_str_cn]:
        return True
    
    return False


def filter_today_news(news_list):
    """
    过滤出今天的新闻，删除其他日期的
    
    Parameters:
        news_list (list): 原始新闻列表
    
    Returns:
        list: 今天的新闻列表
    """
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n🔍 过滤今天({today})的新闻...")
    
    today_news = []
    for news in news_list:
        news_date = news.get('date', '')
        if is_today_news(news_date):
            today_news.append(news)
        else:
            # 显示被过滤掉的新闻（调试用）
            pass  # 直接丢弃，不显示
    
    filtered_count = len(news_list) - len(today_news)
    print(f"   ✅ 保留今天新闻: {len(today_news)} 条")
    print(f"   🗑️  删除其他日期: {filtered_count} 条")
    
    return today_news


def filter_news_by_date_range(news_list, start_date=None, end_date=None):
    """
    过滤出指定日期范围内的新闻。
    若未指定日期范围，则回退到只保留今天的新闻。
    
    Parameters:
        news_list (list): 原始新闻列表
        start_date (str): 开始日期，YYYY-MM-DD
        end_date (str): 结束日期，YYYY-MM-DD
    
    Returns:
        list: 日期范围内的新闻列表
    """
    if not start_date and not end_date:
        return filter_today_news(news_list)
    
    start_date, end_date = _normalize_date_range(start_date, end_date)
    single_day = start_date == end_date
    date_desc = start_date if single_day else f"{start_date} 至 {end_date}"
    print(f"\n🔍 过滤日期范围({date_desc})的新闻...")
    
    start = datetime.strptime(start_date, '%Y-%m-%d').date()
    end = datetime.strptime(end_date, '%Y-%m-%d').date()
    if start > end:
        start, end = end, start
    
    filtered_news = []
    for news in news_list:
        news_date = news.get('date', '')
        if not news_date:
            continue
        try:
            parsed = datetime.strptime(str(news_date).strip()[:10], '%Y-%m-%d').date()
            if start <= parsed <= end:
                filtered_news.append(news)
        except ValueError:
            continue
    
    filtered_count = len(news_list) - len(filtered_news)
    print(f"   ✅ 保留范围内新闻: {len(filtered_news)} 条")
    print(f"   🗑️  删除范围外/无效日期: {filtered_count} 条")
    
    return filtered_news


def clean_news_markdown(text: str, title: str = "") -> str:
    """
    清洗新闻markdown内容，移除常见噪音。
    包括：相关阅读、版权声明、导航菜单、社交分享、推荐文章等。
    """
    if not text:
        return ""

    original_len = len(text)

    # 1. 按行处理，移除纯导航/菜单行（短且包含导航关键词）
    nav_keywords = ['首页', '关于我们', '联系我们', '登录', '注册', '搜索', '导航',
                    'Home', 'About', 'Contact', 'Login', 'Search']
    lines = []
    for line in text.split('\n'):
        stripped = line.strip()
        # 跳过过短且包含导航词的独立行
        if len(stripped) < 20 and any(kw in stripped for kw in nav_keywords):
            continue
        lines.append(line)
    text = '\n'.join(lines)

    # 2. 移除"相关阅读/推荐新闻"及之后所有内容（最常见的污染来源）
    # 【修复】删除了 pattern6（编辑/作者/来源截断）和 pattern7（查看更多截断），
    # 因为新闻网站的"来源"通常在正文开头，re.split 会从这里截断，导致正文全丢。
    cutoff_patterns = [
        r'##?\s*(?:相关阅读|推荐阅读|延伸阅读|热门推荐|猜你喜欢|相关文章|相关新闻)',
        r'##?\s*(?:版权声明|免责声明|隐私政策|关于我们|联系我们|广告合作)',
        r'##?\s*(?:上一篇[：:]?|下一篇[：:]?|推荐阅读[：:]?)',
        r'##?\s*(?:分享到|微信扫一扫|复制链接|点赞|收藏)',
        r'##?\s*(?:发表评论|登录后评论|暂无评论|评论列表)',
    ]
    for pattern in cutoff_patterns:
        parts = re.split(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if len(parts) > 1:
            text = parts[0]

    # 3. 移除 markdown 链接中的 URL 噪音（保留锚文本，移除网址）
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)

    # 4. 移除图片标记和单独的图片说明
    text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'', text)

    # 5. 移除 HTML 标签残留
    text = re.sub(r'<[^>]+>', '', text)

    # 6. 移除连续空行和重复分隔符
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[-=_]{3,}', '', text)

    # 7. 移除过短的段落（通常是广告或按钮文字）
    filtered_lines = []
    for line in text.split('\n'):
        stripped = line.strip()
        if len(stripped) > 0 and len(stripped) < 8:
            # 检查是否包含明显广告词
            if any(kw in stripped for kw in ['广告', '推广', ' sponsored ', 'AD', '戳我']):
                continue
        filtered_lines.append(line)
    text = '\n'.join(filtered_lines)

    cleaned = text.strip()
    return cleaned


async def fetch_markdown_for_news(news_list, max_concurrent=5):
    """
    为新闻列表获取页面markdown内容
    在VPN开启时调用此函数，避免VPN影响后续的AI API调用
    
    Parameters:
        news_list (list): 新闻列表
        max_concurrent (int): 最大并发数
    
    Returns:
        list: 添加了content字段（markdown内容）的新闻列表
    """
    if not news_list:
        return []
    
    print("\n" + "="*60)
    print("🌐 获取新闻页面内容 (Markdown)")
    print("="*60)
    print(f"📊 共 {len(news_list)} 条新闻需要获取内容")
    print(f"⚡ 最大并发数: {max_concurrent}")
    
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def fetch_single_news(news_item):
        """获取单条新闻的markdown内容"""
        url = news_item.get('link')
        # 核心有效字段为 summary；content 为历史冗余字段，保留但不再作为判断依据
        if news_item.get('summary'):
            print(f"   ⏭️ 跳过: 已有摘要 - {news_item.get('title', 'Unknown')[:50]}...")
            return news_item
        if not url:
            print(f"   ⚠️ 跳过: 无链接 - {news_item.get('title', 'Unknown')[:40]}...")
            news_item['summary'] = ''
            return news_item
        
        async with semaphore:
            try:
                print(f"   📄 获取: {news_item.get('title', 'Unknown')[:50]}...")
                result = await extract_article(
                    url=url,
                    title=news_item.get('title', ''),
                    source_platform=news_item.get('source_platform', ''),
                    max_length=50000,
                )
                
                if result.get('success') and result.get('content'):
                    content = result['content']
                    # 编码安全处理
                    content = content.encode('utf-8', errors='ignore').decode('utf-8')
                    # 核心有效字段为 summary（保留原文，年份前缀在 AI 提取阶段注入）
                    news_item['summary'] = content
                    print(f"   ✅ 成功 ({result.get('method', 'unknown')}): {len(content)} 字符")
                else:
                    error = result.get('error') or '无内容'
                    print(f"   ❌ 失败: {str(error)[:50]}...")
                    news_item['summary'] = ''
                    
            except Exception as e:
                print(f"   ❌ 异常: {str(e)[:50]}...")
                news_item['summary'] = ''
        
        return news_item
    
    # 并发获取所有新闻内容
    tasks = [fetch_single_news(news) for news in news_list]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # 处理结果
    final_results = []
    success_count = 0
    fail_count = 0
    
    for result in results:
        if isinstance(result, Exception):
            print(f"❌ 获取任务异常: {result}")
            fail_count += 1
        else:
            final_results.append(result)
            if result.get('summary'):
                success_count += 1
            else:
                fail_count += 1
    
    print("\n" + "="*60)
    print("📊 页面内容获取统计:")
    print(f"   ✅ 成功: {success_count} 条")
    print(f"   ❌ 失败: {fail_count} 条")
    print("="*60)
    
    return final_results


async def get_combined_news_with_metadata(vpn_mode='all', fetch_content=True, include_wechat_mp=True, enabled_modules=None, start_date=None, end_date=None):
    """
    获取合并后的新闻，包含完整的元数据（source_platform, confidence_score, duplicate_count）
    
    Parameters:
        vpn_mode (str): 'all' 运行所有, 'no_vpn' 只运行不需要VPN的, 'vpn' 只运行需要VPN的
        fetch_content (bool): 是否获取页面markdown内容，默认True
        include_wechat_mp (bool): 是否包含微信公众号文章，默认True
        enabled_modules (list): 可选，指定要启用的爬虫模块名列表
        start_date (str): 开始日期，YYYY-MM-DD，默认今天
        end_date (str): 结束日期，YYYY-MM-DD，默认与 start_date 相同
    
    Returns:
        list: 带完整元数据的新闻列表，包含content字段（markdown内容）
    """
    start_date, end_date = _normalize_date_range(start_date, end_date)
    single_day = start_date == end_date
    date_desc = start_date if single_day else f"{start_date} 至 {end_date}"
    
    # 1. 获取所有网站新闻
    all_news = await news_combine(vpn_mode=vpn_mode, enabled_modules=enabled_modules, start_date=start_date, end_date=end_date)
    
    if not all_news and vpn_mode != 'wechat_only':
        print("⚠️ 没有获取到网站新闻数据")
    
    # 2. 前置 URL 去重（绝对重复，直接删除；不调用 AI）
    if all_news:
        print("\n进行前置 URL 去重...")
        final_news = pre_deduplicate_by_url(all_news)
        
        # 3. 获取页面markdown内容（在VPN开启时完成）
        # 注意：已有content的新闻（如后续追加的公众号文章）会被跳过
        if fetch_content:
            final_news = await fetch_markdown_for_news(final_news, max_concurrent=5)
    else:
        final_news = []
    
    # 按日期范围过滤（兜底，防止部分爬虫返回范围外数据）
    final_news = filter_news_by_date_range(final_news, start_date, end_date)
    
    # 5. 获取微信公众号文章（在markdown获取后追加，避免覆盖已有content）
    if include_wechat_mp and vpn_mode in ('all', 'no_vpn', 'wechat_only'):
        try:
            from wechat_mp_fetcher import get_wechat_mp_news
            print("\n📱 开始获取微信公众号文章（WeRSS）...")
            # 同步函数放到独立线程执行，避免阻塞 asyncio 事件循环
            # 根据日期范围计算需要获取的最近天数
            from datetime import datetime as _dt
            try:
                days_back = (_dt.now().date() - _dt.strptime(start_date, '%Y-%m-%d').date()).days + 1
            except Exception:
                days_back = 1
            days_back = max(days_back, 1)
            wechat_news = await asyncio.to_thread(get_wechat_mp_news, days_back=days_back, limit_per_feed=20)
            if wechat_news:
                # 只保留日期范围内的公众号文章
                wechat_news = filter_news_by_date_range(wechat_news, start_date, end_date)
                print(f"\n✅ 获取到 {len(wechat_news)} 条公众号文章，追加到新闻列表...")
                final_news.extend(wechat_news)
                # 公众号追加后再次进行 URL 前置去重（绝对重复直接删除）
                print("\n公众号文章追加后进行 URL 前置去重...")
                final_news = pre_deduplicate_by_url(final_news)
                # 再次过滤，确保只保留指定日期范围内的新闻
                final_news = filter_news_by_date_range(final_news, start_date, end_date)
            else:
                print("\nℹ️  没有获取到公众号文章")
        except Exception as e:
            print(f"\n⚠️ 获取公众号文章失败: {e}")
            import traceback
            traceback.print_exc()
    
    # 6. 注入新闻年份前缀：在所有新闻源合并去重完成后，给 summary 加上时间锚点
    final_news = _add_date_prefix_to_summary(final_news)
    
    # 7. URL 兜底识别：对 source_platform 为空或 unknown 的新闻进行识别
    url_recognized_count = 0
    for news in final_news:
        current_platform = news.get('source_platform', '') or ''
        if not current_platform or current_platform.lower() == 'unknown':
            detected = detect_platform_from_url(news.get('link', ''))
            if detected:
                news['source_platform'] = detected
                news['confidence_score'] = calculate_confidence(
                    detected,
                    news.get('duplicate_count', 1),
                    url=news.get('link', '')
                )
                url_recognized_count += 1
    if url_recognized_count > 0:
        print(f"\n🔗 URL 兜底识别来源平台: {url_recognized_count} 条")

    # 7. 输出统计
    print("\n" + "="*60)
    print("最终数据统计:")
    print("="*60)
    
    # 按平台统计
    platform_stats = {}
    for news in final_news:
        platform = news.get('source_platform', 'unknown')
        platform_stats[platform] = platform_stats.get(platform, 0) + 1
    
    print("\n各平台新闻数量:")
    for platform, count in sorted(platform_stats.items(), key=lambda x: x[1], reverse=True):
        print(f"  {platform}: {count}条")
    
    # 按置信度统计
    high_confidence = len([n for n in final_news if n.get('confidence_score', 0) >= 80])
    medium_confidence = len([n for n in final_news if 50 <= n.get('confidence_score', 0) < 80])
    low_confidence = len([n for n in final_news if n.get('confidence_score', 0) < 50])
    
    print(f"\n置信度分布:")
    print(f"  高置信度(>=80%): {high_confidence}条")
    print(f"  中置信度(50-79%): {medium_confidence}条")
    print(f"  低置信度(<50%): {low_confidence}条")
    
    # 内容获取统计
    if fetch_content:
        with_summary = len([n for n in final_news if n.get('summary')])
        print(f"\n摘要获取:")
        print(f"  有摘要: {with_summary}条")
        print(f"  无摘要: {len(final_news) - with_summary}条")
    
    print(f"\n总计: {len(final_news)}条新闻")
    print("="*60)
    
    return final_news


if __name__ == "__main__":
    try:
        final_result = asyncio.run(get_combined_news_with_metadata())
        
        # 显示样例
        if final_result:
            print("\n样例数据:")
            for i, news in enumerate(final_result[:3], 1):
                print(f"\n{i}. {news['title'][:50]}...")
                print(f"   来源: {news.get('source_platform', 'N/A')}")
                print(f"   置信度: {news.get('confidence_score', 'N/A')}%")
                print(f"   重复次数: {news.get('duplicate_count', 1)}")
        
    except Exception as e:
        print(f"程序执行失败: {e}")
        import traceback
        traceback.print_exc()
