# -*- coding: utf-8 -*-
"""
数据库新闻重新处理模块

功能：
1. 从数据库获取已导入的新闻（支持按日期范围筛选）
2. 使用AI+RAG（ai_classify.re_classify）按照当前prompt重新分类
3. 根据新的分类，使用完整内容提取流程（ai_news_core_content_extraction.process_news_with_category_prompts）
   提取 data 维度（含 transactions 主体信息），summary 直接保留原文，并生成 tags
4. 使用完整报告生成流程（ai_news_report_creating.generate_news_report_from_existing_data）生成新闻报告
5. 替换数据库中原有的分类和内容

使用方法:
    # 方式1: 直接在代码中修改配置（推荐IDE使用）
    修改下面的 CONFIG 字典，然后直接运行文件
    
    # 方式2: 命令行参数
    python reprocess_database_news.py --days 7
    python reprocess_database_news.py --start-date 2024-01-01 --end-date 2024-03-31
    python reprocess_database_news.py --all
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

import asyncio
import argparse
import json
from datetime import datetime, timedelta


# 导入项目模块
from complete_news_pipeline import (
    connect_to_database,
    ensure_table_schema,
    fetch_existing_reports_by_main_tags,
)
from ai_classify import re_classify
from ai_news_core_content_extraction import process_news_with_category_prompts
from ai_news_report_creating import generate_news_report_from_existing_data
from ai_deduplicate_engine import ai_deduplicate_news_reports
from confidence_config import calculate_confidence
from prompt import VALID_CATEGORIES as PROMPT_VALID_CATEGORIES


# ============================================================
# 配置区域 - 直接在IDE中修改这些参数
# ============================================================

CONFIG = {
    # 日期范围配置（三种方式选一种）
    
    # 方式1: 处理最近N天
    # "days": 7,
    
    # 方式2: 指定日期范围
    # "start_date": "2024-03-01",
    # "end_date": "2024-03-31",
    
    # 方式3: 处理所有新闻
    # "process_all": True,
    
    # 默认: 处理最近7天
    "days": 7,
    "start_date": None,  # YYYY-MM-DD 或 None
    "end_date": None,    # YYYY-MM-DD 或 None
    "process_all": False,
    
    # 其他配置
    "limit": None,       # 限制处理数量（用于测试），None表示不限制
    "concurrent": 3,     # 最大并发数（已废弃：完整流程函数内部管理并发，此参数保留仅为兼容）
    "skip_reclassify": False,  # 是否跳过重新分类，仅重新提取内容（已废弃，请使用reprocess_steps）
    "force": False,      # 强制重新生成，不跳过 data 和 news_report 均已存在的已处理新闻（用于历史数据回填）
    
    # 重新处理步骤配置
    # 可选值: "reclassify"(重新分类), "core_content"(提取核心内容), "news_report"(生成新闻报告)
    # 可以是单个字符串或列表，处理顺序固定为: reclassify -> core_content -> news_report
    # "reprocess_steps": ["reclassify", "core_content", "news_report"],  # 全部处理
    # "reprocess_steps": ["core_content", "news_report"],  # 仅重新提取内容和生成报告
    # "reprocess_steps": ["news_report"],  # 仅重新生成新闻报告
    "reprocess_steps": ["reclassify", "core_content", "news_report"],
}

# 数据库配置
from db_config import DB_CONFIG as DEFAULT_DB_CONFIG

# 有效主维度列表（与 prompt.py 中的 MAIN_DIMENSIONS 保持一致）
VALID_CATEGORIES = PROMPT_VALID_CATEGORIES  # 现为旧 13 类兼容
MAIN_DIMENSIONS = getattr(__import__('prompt', fromlist=['MAIN_DIMENSIONS']), 'MAIN_DIMENSIONS', [])


# ============================================================
# 核心功能函数
# ============================================================

def _parse_json_safe(value):
    """安全解析 JSON 字符串。"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _is_field_empty(value):
    """判断字段是否为空（None、空字符串、空白字符串、空 JSON 对象/列表、JSON null 均视为空）。"""
    if value is None:
        return True
    if isinstance(value, (dict, list)):
        return len(value) == 0
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or text in ('null', 'None', '{}', '[]'):
        return True
    parsed = _parse_json_safe(text)
    if parsed is None:
        return not text  # 解析失败时只有非空字符串才视为非空
    if isinstance(parsed, (dict, list)):
        return len(parsed) == 0
    if isinstance(parsed, str):
        return not parsed.strip()
    return False


def _collect_text(obj, texts=None):
    """递归收集字符串内容。"""
    if texts is None:
        texts = []
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_text(v, texts)
    elif isinstance(obj, list):
        for item in obj:
            _collect_text(item, texts)
    elif isinstance(obj, str):
        texts.append(obj)
    return texts


def _row_matches_search_scope(news, keyword, search_scope):
    """判断单条新闻是否匹配关键字与检索范围。"""
    if not keyword:
        return True
    kw = keyword.lower()
    if search_scope == "title_summary_report":
        text = f"{news.get('title', '')} {news.get('summary', '')} {news.get('news_report', '')}".lower()
        return kw in text
    if search_scope == "link":
        return kw in str(news.get('link', '')).lower()
    if search_scope in ("entity", "data", "tags"):
        field = news.get(search_scope, "")
        obj = _parse_json_safe(field)
        if obj is None:
            return kw in str(field).lower()
        return kw in " ".join(_collect_text(obj)).lower()
    if search_scope == "all":
        text = " ".join(str(news.get(k, "")) for k in news)
        return kw in text.lower()
    return True


async def fetch_news_from_database(
    connection,
    start_date=None,
    end_date=None,
    limit=None,
    categories=None,
    keyword=None,
    search_scope="title_summary_report",
    news_id=None,
):
    """
    从数据库获取新闻数据
    
    Args:
        connection: 数据库连接
        start_date: 开始日期 (YYYY-MM-DD)
        end_date: 结束日期 (YYYY-MM-DD)
        limit: 限制返回的新闻数量
        categories: 筛选的新闻分类列表，None表示不筛选
        keyword: 关键字筛选
        search_scope: 关键字检索范围
        news_id: 指定单条新闻 ID，提供时忽略其他筛选条件
    
    Returns:
        list: 新闻数据列表
    """
    try:
        cursor = connection.cursor(dictionary=True)
        
        # 构建查询条件
        conditions = []
        params = []
        
        if news_id is not None:
            conditions.append("id = %s")
            params.append(news_id)
        else:
            if start_date:
                conditions.append("date >= %s")
                params.append(start_date)
            if end_date:
                conditions.append("date <= %s")
                params.append(end_date)
            if categories:
                # 按 tags.main_tags 中的主维度筛选（支持多个）
                cat_conditions = []
                for cat in categories:
                    cat_conditions.append("JSON_CONTAINS(NULLIF(tags, ''), %s, '$.main_tags')")
                    params.append(json.dumps(cat))
                conditions.append(f"({' OR '.join(cat_conditions)})")
            
            # 关键字在可SQL检索的范围内直接过滤
            sql_search_scopes = {"title_summary_report", "link"}
            if keyword and search_scope in sql_search_scopes:
                like = f"%{keyword}%"
                if search_scope == "title_summary_report":
                    conditions.append("(title LIKE %s OR summary LIKE %s OR news_report LIKE %s)")
                    params.extend([like, like, like])
                elif search_scope == "link":
                    conditions.append("link LIKE %s")
                    params.append(like)
        
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        
        query = f"""
        SELECT id, title, link, date, summary,
               data, news_report, tags,
               source_platform, confidence_score
        FROM ship_news
        {where_clause}
        ORDER BY date DESC
        """
        
        # JSON 范围需在内存中过滤，这里先不限制数量
        if limit and (not keyword or search_scope in sql_search_scopes) and news_id is None:
            query += f" LIMIT {limit}"
        
        cursor.execute(query, params)
        news_data = cursor.fetchall()
        cursor.close()
        
        # 将数据转换为标准格式
        formatted_data = []
        for news in news_data:
            formatted_data.append({
                'id': news['id'],
                'title': news['title'],
                'link': news['link'],
                'date': news['date'].strftime('%Y-%m-%d') if news['date'] else None,
                'summary': news['summary'] or '',
                'data': news['data'] or '',
                'news_report': news['news_report'] or '',
                'tags': news.get('tags') or '',
                'source_platform': news['source_platform'] or 'unknown',
                'confidence_score': news['confidence_score'] or 50
            })
        
        # JSON 范围关键字在内存过滤
        if keyword and search_scope not in sql_search_scopes:
            formatted_data = [
                n for n in formatted_data
                if _row_matches_search_scope(n, keyword, search_scope)
            ]
            if limit:
                formatted_data = formatted_data[:limit]
        
        print(f"✅ 从数据库获取到 {len(formatted_data)} 条新闻")
        return formatted_data
        
    except Exception as e:
        print(f"❌ 从数据库获取新闻失败: {e}")
        import traceback
        traceback.print_exc()
        return []


def _extract_main_tags(tags_text):
    """从 tags JSON 字符串中提取主维度标签列表。"""
    import json
    if not tags_text:
        return []
    try:
        tags = json.loads(tags_text)
        if isinstance(tags, dict):
            return [t for t in tags.get('main_tags', []) if t]
    except Exception:
        pass
    return []


async def reclassify_news(news_data):
    """
    使用AI重新对新闻进行标签分类（含主维度），不再生成 category/broad_category。
    
    Args:
        news_data: 新闻数据列表
    
    Returns:
        list: 重新分类后的新闻列表
    """
    print("\n" + "="*70)
    print("🔄 开始重新标签分类新闻")
    print("="*70)
    
    # 记录原始主维度，便于后续统计变化
    for news in news_data:
        news['_original_main_tags'] = _extract_main_tags(news.get('tags', ''))
    
    # 强制重新分类标签
    results = await re_classify(news_data=news_data, force_reclassify=True)
    
    # 打印分类统计
    print("\n" + "="*70)
    print("📊 重新标签分类统计:")
    print("="*70)
    
    old_stats = {}
    new_stats = {}
    changed_count = 0
    
    for news in results:
        old_tags = news.get('_original_main_tags', [])
        new_tags = _extract_main_tags(news.get('tags', ''))
        
        for tag in old_tags:
            old_stats[tag] = old_stats.get(tag, 0) + 1
        for tag in new_tags:
            new_stats[tag] = new_stats.get(tag, 0) + 1
        
        if set(old_tags) != set(new_tags):
            changed_count += 1
    
    print(f"\n原主维度分布:")
    for tag, count in sorted(old_stats.items(), key=lambda x: x[1], reverse=True):
        print(f"  {tag}: {count}条")
    
    print(f"\n新主维度分布:")
    for tag, count in sorted(new_stats.items(), key=lambda x: x[1], reverse=True):
        print(f"  {tag}: {count}条")
    
    print(f"\n主维度变化: {changed_count}/{len(results)} 条")
    print("="*70)
    
    return results


async def extract_core_content(news_data):
    """
    根据当前主维度 tags 使用完整流程提取核心内容（按主维度拆分生成多组 entity/data）
    
    Args:
        news_data: 新闻数据列表（需包含 tags 字段）
    
    Returns:
        list: 提取核心内容后的新闻列表
    """
    print("\n" + "="*70)
    print("📝 开始提取核心内容（使用完整提取流程+RAG）")
    print("="*70)
    
    # 使用 tags.main_tags 作为主维度选择 prompts；category 列已弃用。
    # process_news_with_category_prompts 根据 main_tags 自动选择 prompts，
    # 并对新造船订单/租船购船自动判断子类别，同时支持RAG反馈注入
    results = await process_news_with_category_prompts(news_data)
    
    # 标记为已重新处理
    for news in results:
        if news is not None:
            news['ai_reprocessed'] = True
            news['reprocessed_at'] = datetime.now().isoformat()
    
    # 过滤掉处理失败的新闻
    processed_news = [n for n in results if n is not None]
    
    print(f"\n✅ 核心内容提取完成: {len(processed_news)}/{len(news_data)} 条")
    return processed_news


async def update_database(connection, news_data, update_fields=None):
    """
    更新数据库中的新闻数据
    
    Args:
        connection: 数据库连接
        news_data: 处理后的新闻数据列表
        update_fields: 需要更新的字段列表，None表示更新所有字段
    
    Returns:
        int: 成功更新的记录数
    """
    print("\n" + "="*70)
    print("💾 开始更新数据库")
    print("="*70)
    
    # 默认更新所有核心字段（category/entity 列已弃用，不再更新）
    if update_fields is None:
        update_fields = ['summary', 'data', 'news_report', 'tags']
    # 过滤已弃用字段，防止外部调用误传
    update_fields = [f for f in update_fields if f not in ('category', 'entity')]

    try:
        cursor = connection.cursor()

        # 根据要更新的字段构建SQL
        field_mapping = {
            'summary': '%s',
            'data': '%s',
            'news_report': '%s',
            'tags': '%s',
            'confidence_score': '%s',
            'ai_reprocessed': '%s'
        }
        
        # 构建SET子句
        set_clauses = []
        for field in update_fields:
            if field in field_mapping:
                set_clauses.append(f"{field} = {field_mapping[field]}")
        
        # 始终更新ai_reprocessed标记
        if 'ai_reprocessed' not in update_fields:
            set_clauses.append("ai_reprocessed = %s")
        
        update_sql = f"""
        UPDATE ship_news 
        SET {', '.join(set_clauses)}
        WHERE id = %s
        """
        
        success_count = 0
        failed_count = 0
        
        for news in news_data:
            try:
                # 构建数据元组
                data_tuple = []
                for field in update_fields:
                    if field in field_mapping and field != 'ai_reprocessed':
                        data_tuple.append(news.get(field, ''))
                
                # 添加ai_reprocessed和id
                if 'ai_reprocessed' not in update_fields:
                    data_tuple.append(1)  # ai_reprocessed = True
                data_tuple.append(news['id'])
                
                cursor.execute(update_sql, tuple(data_tuple))
                success_count += 1
                
                if success_count % 10 == 0:
                    print(f"   已更新 {success_count}/{len(news_data)} 条...")
                    
            except Exception as e:
                print(f"   ❌ 更新失败 ID={news.get('id')}: {str(e)[:100]}")
                failed_count += 1
                continue
        
        connection.commit()
        cursor.close()
        
        print(f"\n✅ 更新完成: 成功 {success_count} 条, 失败 {failed_count} 条")
        print(f"   更新字段: {', '.join(update_fields)}")
        return success_count
        
    except Exception as e:
        print(f"❌ 数据库更新失败: {e}")
        connection.rollback()
        return 0


async def _sync_confidence_score(connection, news_data):
    """
    按当前置信度配置同步 news_data 中每条新闻的 confidence_score。
    直接写回数据库，并更新内存中的 confidence_score。
    """
    if not news_data or not connection:
        return 0

    cursor = connection.cursor()
    updated = 0
    try:
        for news in news_data:
            new_score = _recalc_confidence(news)
            if news.get('confidence_score') == new_score:
                continue
            try:
                cursor.execute(
                    "UPDATE ship_news SET confidence_score = %s WHERE id = %s",
                    (new_score, news['id'])
                )
                news['confidence_score'] = new_score
                updated += 1
            except Exception as e:
                print(f"  ❌ 同步置信度失败 ID={news.get('id')}: {e}")
        connection.commit()
    except Exception as e:
        print(f"❌ 批量同步置信度失败: {e}")
        connection.rollback()
    finally:
        cursor.close()

    if updated:
        print(f"   已同步 {updated}/{len(news_data)} 条新闻的置信度")
    return updated


def _recalc_confidence(news: dict) -> int:
    """
    根据当前置信度配置重新计算单条新闻的 confidence_score。
    """
    platform = news.get('source_platform', '') or 'unknown'
    return calculate_confidence(
        platform,
        news.get('duplicate_count', 1),
        url=news.get('link', '')
    )


def normalize_reprocess_steps(steps):
    """
    规范化重新处理步骤配置
    
    Args:
        steps: 字符串或列表，表示要执行的步骤
    
    Returns:
        list: 规范化后的步骤列表
    """
    if steps is None:
        return ["reclassify", "core_content", "news_report"]
    
    if isinstance(steps, str):
        steps = [steps]
    
    # 有效的步骤
    valid_steps = {"reclassify", "core_content", "news_report"}
    
    # 过滤无效步骤并去重
    normalized = []
    seen = set()
    for step in steps:
        step = step.lower().strip()
        if step in valid_steps and step not in seen:
            normalized.append(step)
            seen.add(step)
    
    # 按固定顺序排序: reclassify -> core_content -> news_report
    order = {"reclassify": 0, "core_content": 1, "news_report": 2}
    normalized.sort(key=lambda x: order[x])
    
    return normalized if normalized else ["reclassify", "core_content", "news_report"]


async def _deduplicate_reports_against_history(news_data: list, connection) -> list:
    """
    将生成报道后的新闻与数据库历史报告进行最终交易查重。
    重复的报道和对应交易会被标记 is_duplicate=true，不删除原新闻。
    会自动排除当前正在处理的新闻自身，避免自匹配。
    """
    if not news_data or not connection:
        return news_data

    exclude_ids = {
        news.get("id") for news in news_data
        if news.get("id") is not None
    }

    main_tags = set()
    for news in news_data:
        try:
            report_groups = json.loads(news.get("news_report") or "[]") if isinstance(news.get("news_report"), str) else (news.get("news_report") or [])
        except Exception:
            report_groups = []
        if isinstance(report_groups, list):
            for g in report_groups:
                if isinstance(g, dict) and g.get("main_tag"):
                    main_tags.add(g["main_tag"])

    if not main_tags:
        return news_data

    print(f"\n🔍 开始与历史报告进行最终交易查重（排除自身 ID: {sorted(exclude_ids)}）...")
    existing_reports = await fetch_existing_reports_by_main_tags(
        connection, main_tags, days_back=7, exclude_ids=exclude_ids
    )
    news_data = await ai_deduplicate_news_reports(
        news_data,
        existing_reports=existing_reports,
        log_callback=print,
        connection=connection,
    )
    print("✅ 最终交易查重完成")
    return news_data


async def reprocess_news_item(news_item: dict) -> dict:
    """
    对单条新闻执行完整重新处理（重新分类 → 提取核心内容 → 生成报告），
    返回处理后的新闻对象，不写入数据库。
    """
    original_id = news_item.get("id")
    news_data = [news_item]
    news_data = await reclassify_news(news_data)
    news_data = await extract_core_content(news_data)
    if not news_data:
        raise RuntimeError("核心内容提取失败")
    news_data = await generate_news_report_from_existing_data(news_data)
    processed = news_data[0]
    # 清理临时字段，避免污染返回数据
    processed.pop("_original_main_tags", None)
    # 保留原始元数据（id/link/date/source_platform/confidence_score 等）
    result = {**news_item, **processed}
    result["id"] = original_id
    # 重新计算置信度（配置规则变更后同步更新）
    result["confidence_score"] = _recalc_confidence(result)
    return result


async def reprocess_news_preview(
    news_ids: list[int],
    max_concurrent: int = 8,
    progress_callback=None,
) -> list[dict]:
    """
    根据新闻 ID 列表重新生成预览数据，不修改数据库。

    Args:
        news_ids: 需要重新处理的新闻 ID 列表
        max_concurrent: 最大并发数（默认 8）
        progress_callback: 每完成一条新闻时回调，签名为 callback(item: dict)

    Returns:
        处理成功的新闻对象列表（按传入顺序）
    """
    connection = connect_to_database(**DEFAULT_DB_CONFIG)
    if not connection:
        raise RuntimeError("数据库连接失败")

    try:
        # 先按顺序读取所有新闻（数据库连接非线程安全，必须串行读取）
        news_items = []
        for news_id in news_ids:
            news_data = await fetch_news_from_database(
                connection, news_id=news_id
            )
            if not news_data:
                print(f"⚠️ 未找到 ID={news_id} 的新闻")
                continue
            news_items.append((news_id, news_data[0]))

        semaphore = asyncio.Semaphore(max_concurrent)
        results_map = {}

        async def _process_one(news_id: int, news_item: dict):
            async with semaphore:
                try:
                    processed = await reprocess_news_item(news_item)
                    if progress_callback:
                        try:
                            progress_callback(processed)
                        except Exception as cb_err:
                            print(f"[WARN] 进度回调失败 ID={news_id}: {cb_err}")
                    return news_id, processed
                except Exception as e:
                    print(f"❌ 重新生成 ID={news_id} 失败: {e}")
                    return news_id, None

        tasks = [
            asyncio.create_task(_process_one(nid, item))
            for nid, item in news_items
        ]
        for coro in asyncio.as_completed(tasks):
            news_id, processed = await coro
            if processed is not None:
                results_map[news_id] = processed

        # 保持输出顺序与输入 news_ids 一致
        results = [results_map[nid] for nid in news_ids if nid in results_map]

        # 对重新生成的结果与历史报告进行最终交易查重
        if results:
            results = await _deduplicate_reports_against_history(results, connection)
            # 同步置信度（配置规则变更后，预览结果也应反映最新分值）
            for news in results:
                news["confidence_score"] = _recalc_confidence(news)
        return results
    finally:
        connection.close()


async def refresh_report_preview(
    news_ids: list[int],
    max_concurrent: int = 8,
    progress_callback=None,
) -> list[dict]:
    """
    根据新闻 ID 列表仅重新生成 news_report（报道正文）预览，不修改数据库。

    Args:
        news_ids: 需要刷新正文的新闻 ID 列表
        max_concurrent: 最大并发数（默认 8）
        progress_callback: 每完成一条新闻时回调，签名为 callback(item: dict)

    Returns:
        处理成功的新闻对象列表（按传入顺序）
    """
    connection = connect_to_database(**DEFAULT_DB_CONFIG)
    if not connection:
        raise RuntimeError("数据库连接失败")

    try:
        # 先按顺序读取所有新闻（数据库连接非线程安全，必须串行读取）
        news_items = []
        for news_id in news_ids:
            news_data = await fetch_news_from_database(
                connection, news_id=news_id
            )
            if not news_data:
                print(f"⚠️ 未找到 ID={news_id} 的新闻")
                continue
            news_items.append((news_id, news_data[0]))

        semaphore = asyncio.Semaphore(max_concurrent)
        results_map = {}

        async def _process_one(news_id: int, news_item: dict):
            async with semaphore:
                try:
                    report_data = await generate_news_report_from_existing_data([news_item])
                    if not report_data:
                        raise RuntimeError("生成报道正文失败")
                    refreshed = report_data[0]
                    # 保留原始元数据，仅更新 news_report
                    result = {**news_item, "news_report": refreshed.get("news_report", news_item.get("news_report", ""))}
                    if progress_callback:
                        try:
                            progress_callback(result)
                        except Exception as cb_err:
                            print(f"[WARN] 进度回调失败 ID={news_id}: {cb_err}")
                    return news_id, result
                except Exception as e:
                    print(f"❌ 刷新正文 ID={news_id} 失败: {e}")
                    return news_id, None

        tasks = [
            asyncio.create_task(_process_one(nid, item))
            for nid, item in news_items
        ]
        for coro in asyncio.as_completed(tasks):
            news_id, processed = await coro
            if processed is not None:
                results_map[news_id] = processed

        # 保持输出顺序与输入 news_ids 一致
        results = [results_map[nid] for nid in news_ids if nid in results_map]
        # 同步置信度（刷新正文后同样按当前配置重新计算）
        for news in results:
            news["confidence_score"] = _recalc_confidence(news)
        return results
    finally:
        connection.close()


async def reprocess_single_news_by_id(
    news_id: int,
    manual_summary: str | None = None,
) -> dict:
    """
    重新处理单条新闻并保存到数据库。

    Args:
        news_id: 新闻 ID
        manual_summary: 若提供，先更新 summary 字段再处理

    Returns:
        包含新闻标题、link、处理结果的字典
    """
    connection = connect_to_database(**DEFAULT_DB_CONFIG)
    if not connection:
        raise RuntimeError("数据库连接失败")

    try:
        # 确保表结构包含 ai_reprocessed 字段
        cursor = connection.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns 
            WHERE table_name = 'ship_news' AND column_name = 'ai_reprocessed'
        """)
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                ALTER TABLE ship_news 
                ADD COLUMN ai_reprocessed TINYINT DEFAULT 0
            """)
            connection.commit()
        cursor.close()

        # 读取单条新闻
        news_data = await fetch_news_from_database(
            connection, news_id=news_id
        )
        if not news_data:
            raise ValueError(f"未找到 ID={news_id} 的新闻")

        news_item = news_data[0]

        # 如果提供了手动正文，先更新数据库中的 summary
        if manual_summary is not None:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE ship_news SET summary = %s WHERE id = %s",
                (manual_summary, news_id),
            )
            connection.commit()
            cursor.close()
            news_item["summary"] = manual_summary

        processed = await reprocess_news_item(news_item)
        
        # 与历史报告进行最终交易查重
        deduped = await _deduplicate_reports_against_history([processed], connection)
        processed = deduped[0] if deduped else processed

        # 同步置信度（按当前配置重新计算）
        processed['confidence_score'] = _recalc_confidence(processed)

        updated_fields = ['tags', 'summary', 'data', 'news_report', 'confidence_score']
        updated_count = await update_database(
            connection, [processed], update_fields=updated_fields
        )

        return {
            "success": True,
            "news_id": news_id,
            "title": processed.get("title"),
            "link": processed.get("link"),
            "updated": updated_count,
        }
    finally:
        connection.close()


async def run_with_config(config=None):
    """
    使用配置运行处理流程（供IDE直接调用）
    
    Args:
        config: 配置字典，如果为None则使用默认CONFIG
    """
    if config is None:
        config = CONFIG
    
    # 解析配置
    if config.get("process_all"):
        start_date = None
        end_date = None
        print("📅 处理所有新闻")
    elif config.get("days"):
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=config["days"])).strftime('%Y-%m-%d')
        print(f"📅 处理最近{config['days']}天的新闻 ({start_date} 至 {end_date})")
    else:
        start_date = config.get("start_date")
        end_date = config.get("end_date")
        print(f"📅 处理日期范围: {start_date or '不限'} 至 {end_date or '不限'}")
    
    limit = config.get("limit")
    concurrent = config.get("concurrent", 3)
    categories = config.get("categories", None)
    keyword = config.get("keyword")
    search_scope = config.get("search_scope") or "title_summary_report"
    
    # 解析重新处理步骤
    reprocess_steps = normalize_reprocess_steps(config.get("reprocess_steps"))
    
    # 兼容旧配置
    if config.get("skip_reclassify") and "reclassify" in reprocess_steps:
        reprocess_steps.remove("reclassify")
        print("\n⚠️ 注意: skip_reclassify=True 已从reprocess_steps中移除reclassify")
    
    print("\n" + "="*70)
    print("🔄 重新处理步骤配置")
    print("="*70)
    print(f"   将要执行的步骤: {' -> '.join(reprocess_steps)}")
    if categories:
        print(f"   筛选分类: {', '.join(categories)}")
    else:
        print("   筛选分类: 全部")
    print("="*70)
    
    if not reprocess_steps:
        print("❌ 没有配置任何处理步骤")
        return
    
    # 连接数据库
    print("\n🔌 连接数据库...")
    connection = connect_to_database(**DEFAULT_DB_CONFIG)
    if not connection:
        print("❌ 数据库连接失败")
        return
    
    try:
        # 确保表结构包含ai_reprocessed字段
        cursor = connection.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns 
            WHERE table_name = 'ship_news' AND column_name = 'ai_reprocessed'
        """)
        if cursor.fetchone()[0] == 0:
            print("添加 ai_reprocessed 字段...")
            cursor.execute("""
                ALTER TABLE ship_news 
                ADD COLUMN ai_reprocessed TINYINT DEFAULT 0
            """)
            connection.commit()
        cursor.close()
        
        # 获取新闻数据
        news_data = await fetch_news_from_database(
            connection, start_date, end_date, limit, categories, keyword, search_scope
        )

        if not news_data:
            print("❌ 没有获取到新闻数据")
            return

        # 同步置信度：即使后续跳过 AI 处理，也要保证配置变更后分数一致
        print("\n📝 同步置信度评分...")
        await _sync_confidence_score(connection, news_data)

        # 过滤已处理过的新闻：data 和 news_report 均非空时跳过，避免重复处理
        # 手动强制重新生成时可通过 config['force']=True 跳过此过滤
        force_reprocess = config.get('force', False)
        original_count = len(news_data)
        if not force_reprocess:
            news_data = [
                news for news in news_data
                if _is_field_empty(news.get('data')) or _is_field_empty(news.get('news_report'))
            ]
            skipped_count = original_count - len(news_data)
            print(f"   获取新闻: {original_count} 条，跳过已处理: {skipped_count} 条，实际待处理: {len(news_data)} 条")
        else:
            skipped_count = 0
            print(f"   强制重新生成: {original_count} 条（不跳过已处理）")
        
        if not news_data:
            print("✅ 没有需要处理的新闻（data 和 news_report 均已存在）")
            return
        
        # 根据配置的步骤执行处理
        updated_fields = []  # 记录需要更新的字段
        
        # 步骤1: 重新标签分类
        if "reclassify" in reprocess_steps:
            news_data = await reclassify_news(news_data)
            updated_fields.append('tags')
        else:
            print("\n⏭️ 跳过重新标签分类")
        
        # 步骤2: 提取核心内容
        if "core_content" in reprocess_steps:
            news_data = await extract_core_content(news_data)
            updated_fields.extend(['summary', 'data', 'tags'])
        else:
            print("\n⏭️ 跳过核心内容提取")
        
        if not news_data:
            print("❌ 没有成功处理的新闻")
            return
        
        # 步骤3: 生成新闻报告
        if "news_report" in reprocess_steps:
            print("\n" + "="*70)
            print("📰 开始生成新闻报告")
            print("="*70)
            news_data = await generate_news_report_from_existing_data(news_data)
            updated_fields.append('news_report')
        else:
            print("\n⏭️ 跳过新闻报告生成")
        
        # 步骤4: 与历史报告进行最终交易查重（标记重复，不删除）
        if news_data:
            news_data = await _deduplicate_reports_against_history(news_data, connection)

        # 步骤4.5: 重新计算置信度（配置规则变更后，重新处理时应同步更新）
        for news in news_data:
            news['confidence_score'] = _recalc_confidence(news)
        updated_fields.append('confidence_score')

        # 去重更新字段列表
        updated_fields = list(dict.fromkeys(updated_fields))
        
        # 步骤5: 更新数据库
        updated_count = await update_database(connection, news_data, update_fields=updated_fields)
        
        # 显示样例
        print("\n" + "="*70)
        print("📋 处理结果样例:")
        print("="*70)
        for i, news in enumerate(news_data[:3], 1):
            print(f"\n{i}. {news['title'][:50]}...")
            print(f"   标签: {news.get('tags', 'N/A')[:80]}...")
            if 'data' in updated_fields or 'core_content' in reprocess_steps:
                print(f"   数据: {str(news.get('data', 'N/A'))[:50]}...")
            if 'news_report' in updated_fields or 'news_report' in reprocess_steps:
                print(f"   新闻报告: {news.get('news_report', 'N/A')[:60]}...")
        
        print("\n" + "="*70)
        print(f"✅ 流程完成！共更新 {updated_count} 条新闻")
        print(f"   更新字段: {', '.join(updated_fields)}")
        print("="*70)
        
        return updated_count
        
    except Exception as e:
        print(f"❌ 处理失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        connection.close()
        print("🔌 数据库连接已关闭")


async def main():
    """主函数 - 支持命令行参数和IDE直接运行"""
    parser = argparse.ArgumentParser(
        description='重新处理数据库中的新闻数据',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 方式1: 直接修改代码中的 CONFIG 字典，然后运行
  python reprocess_database_news.py
  
  # 方式2: 使用命令行参数
  python reprocess_database_news.py --days 7
  python reprocess_database_news.py --start-date 2024-01-01 --end-date 2024-03-31
  python reprocess_database_news.py --all
  python reprocess_database_news.py --days 7 --limit 10
  
  # 方式3: 指定处理步骤（可多选，用逗号分隔）
  python reprocess_database_news.py --days 7 --steps reclassify
  python reprocess_database_news.py --days 7 --steps core_content,news_report
  python reprocess_database_news.py --days 7 --steps reclassify,core_content,news_report
  
  # 方式4: 指定分类（可多选，用逗号分隔）
  python reprocess_database_news.py --days 7 --categories 行业新闻
  python reprocess_database_news.py --days 7 --categories 买卖船 --steps core_content,news_report

  # 方式5: 强制重新生成（不跳过已处理的新闻），用于历史数据回填
  python reprocess_database_news.py --all --categories 行业新闻 --force --steps core_content,news_report

  注意：--categories 按 tags.main_tags 中的主维度筛选，category 列已弃用。
        """
    )
    
    parser.add_argument('--start-date', type=str, help='开始日期 (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, help='结束日期 (YYYY-MM-DD)')
    parser.add_argument('--days', type=int, help='处理最近N天的新闻')
    parser.add_argument('--all', action='store_true', help='处理所有新闻')
    parser.add_argument('--limit', type=int, help='限制处理的新闻数量（用于测试）')
    parser.add_argument('--concurrent', type=int, default=3, help='最大并发数（已废弃，完整流程函数内部管理并发，默认3）')
    parser.add_argument('--skip-reclassify', action='store_true', help='跳过重新分类（已废弃，请使用--steps）')
    parser.add_argument('--steps', type=str, default=None, 
                        help='重新处理步骤，可选: reclassify,core_content,news_report（可多选，逗号分隔，默认全部）')
    parser.add_argument('--categories', type=str, default=None,
                        help='按 tags.main_tags 中的主维度筛选，可选: 新造船订单,买卖船,租船,船舶建造,维修改装,公司金融,行业新闻（多选用逗号分隔，默认全部）')
    parser.add_argument('--force', action='store_true',
                        help='强制重新生成，不跳过 data 和 news_report 均已存在的已处理新闻（用于历史数据回填）')

    args = parser.parse_args()
    
    # 解析 steps 参数
    reprocess_steps = None
    if args.steps:
        reprocess_steps = [s.strip() for s in args.steps.split(',')]
    
    # 解析 categories 参数
    categories = None
    if args.categories:
        categories = [c.strip() for c in args.categories.split(',')]
    
    # 如果有命令行参数，使用命令行参数；否则使用CONFIG
    if any([args.start_date, args.end_date, args.days, args.all, args.limit, args.steps, args.skip_reclassify, args.categories, args.force]):
        # 使用命令行参数
        config = {
            "days": args.days,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "process_all": args.all,
            "limit": args.limit,
            "concurrent": args.concurrent,
            "skip_reclassify": args.skip_reclassify,
            "reprocess_steps": reprocess_steps,
            "categories": categories,
            "force": args.force,
        }
        await run_with_config(config)
    else:
        # 使用代码中的 CONFIG
        print("📝 使用代码中的 CONFIG 配置")
        await run_with_config(CONFIG)


if __name__ == "__main__":
    asyncio.run(main())
