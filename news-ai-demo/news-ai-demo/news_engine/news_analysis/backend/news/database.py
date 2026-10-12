# -*- coding: utf-8 -*-
"""
news_analysis 后端 - 新闻数据库操作
"""

import sys
import os

if __name__ == "__main__" and not getattr(sys, "frozen", False):
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

import mysql.connector

from news_analysis.backend.common.config import DEFAULT_DB_CONFIG, NEWS_TABLE


def connect_db(db_config: Optional[Dict[str, Any]] = None):
    """连接 MySQL 数据库。"""
    config = db_config or DEFAULT_DB_CONFIG
    return mysql.connector.connect(**config)


def get_table_columns(conn, table_name: str = NEWS_TABLE) -> List[str]:
    """获取数据表的所有列名。"""
    cursor = conn.cursor()
    cursor.execute(f"DESCRIBE {table_name}")
    columns = [row[0] for row in cursor.fetchall()]
    cursor.close()
    return columns


def fetch_source_platforms(conn, table_name: str = NEWS_TABLE) -> List[str]:
    """获取新闻表中所有非空的去重来源平台名称。"""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT DISTINCT source_platform FROM {table_name} "
            f"WHERE source_platform IS NOT NULL AND source_platform != '' "
            f"ORDER BY source_platform"
        )
        return [row[0] for row in cursor.fetchall()]
    finally:
        cursor.close()


def build_news_where_clause(
    categories: Optional[List[str]] = None,
    broad_categories: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    keyword: Optional[str] = None,
    days: Optional[int] = None,
    platforms: Optional[List[str]] = None,
    keywords: Optional[List[str]] = None,
) -> Tuple[str, List[Any]]:
    """构建新闻查询 WHERE 子句。

    参数：
        keyword: 单个关键字，模糊匹配 title/summary/news_report
        keywords: 多个关键字，任意一个匹配即可（OR 关系）
        broad_categories: 【已废弃】大类过滤已移除，保留参数仅作兼容
    """
    conditions = []
    params = []

    if categories:
        cat_conditions = []
        for cat in categories:
            cat_conditions.append(
                "JSON_CONTAINS(NULLIF(tags, ''), %s, '$.main_tags')"
            )
            params.append(json.dumps(cat))
        conditions.append(f"({' OR '.join(cat_conditions)})")

    # broad_categories 参数已废弃，不再生成 SQL 条件
    _ = broad_categories

    if days is not None:
        start = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
        conditions.append("date >= %s")
        params.append(start)
    else:
        if start_date:
            conditions.append("date >= %s")
            params.append(start_date)
        if end_date:
            conditions.append("date <= %s")
            params.append(end_date)

    if keyword:
        like = f"%{keyword}%"
        conditions.append("(title LIKE %s OR summary LIKE %s OR news_report LIKE %s)")
        params.extend([like, like, like])

    if keywords:
        keyword_conditions = []
        for kw in keywords:
            if not kw:
                continue
            like = f"%{kw}%"
            keyword_conditions.append("(title LIKE %s OR summary LIKE %s OR news_report LIKE %s)")
            params.extend([like, like, like])
        if keyword_conditions:
            conditions.append("(" + " OR ".join(keyword_conditions) + ")")

    if platforms:
        placeholders = ", ".join(["%s"] * len(platforms))
        conditions.append(f"source_platform IN ({placeholders})")
        params.extend(platforms)

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    return where, params


def fetch_news(
    conn,
    categories: Optional[List[str]] = None,
    broad_categories: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    keyword: Optional[str] = None,
    days: Optional[int] = None,
    platforms: Optional[List[str]] = None,
    keywords: Optional[List[str]] = None,
    limit: Optional[int] = None,
    order_by: str = "date DESC, id DESC",
) -> List[Dict[str, Any]]:
    """按条件查询 ship_news 表数据。"""
    columns = get_table_columns(conn, NEWS_TABLE)
    columns_str = ", ".join(columns)
    where, params = build_news_where_clause(
        categories, broad_categories, start_date, end_date, keyword, days, platforms, keywords
    )

    query = f"SELECT {columns_str} FROM {NEWS_TABLE}{where}"
    if order_by:
        query += f" ORDER BY {order_by}"
    if limit:
        query += " LIMIT %s"
        params.append(int(limit))

    cursor = conn.cursor(dictionary=True)
    cursor.execute(query, params)
    rows = cursor.fetchall()
    cursor.close()
    return rows


def update_news(conn, news_id: int, updates: Dict[str, Any]) -> bool:
    """更新单条新闻记录。"""
    if not updates:
        return False

    allowed_columns = set(get_table_columns(conn, NEWS_TABLE))
    fields = []
    params = []
    for key, value in updates.items():
        if key not in allowed_columns or key == "id":
            continue
        fields.append(f"{key} = %s")
        params.append(value)

    if not fields:
        return False

    params.append(news_id)
    query = f"UPDATE {NEWS_TABLE} SET {', '.join(fields)} WHERE id = %s"

    cursor = conn.cursor()
    try:
        cursor.execute(query, params)
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()


def update_news_batch(conn, news_items: List[Dict[str, Any]]) -> int:
    """
    批量更新新闻记录。

    所有记录更新相同字段集，使用 executemany 减少网络往返。
    """
    if not news_items:
        return 0

    allowed_columns = set(get_table_columns(conn, NEWS_TABLE))
    fields = None
    params = []

    for item in news_items:
        original_id = item.get("_original_id")
        if not original_id:
            continue
        updates = {k: v for k, v in item.items() if k not in ("_original_id", "_original_title", "_original_link")}
        updates["title"] = item.get("_original_title", updates.get("title"))
        updates["link"] = item.get("_original_link", updates.get("link"))
        updates = {k: v for k, v in updates.items() if k in allowed_columns and k != "id"}
        if not updates:
            continue
        if fields is None:
            fields = sorted(updates.keys())
        row = [updates.get(f) for f in fields] + [original_id]
        params.append(row)

    if not params or not fields:
        return 0

    set_clause = ", ".join(f"{f} = %s" for f in fields)
    query = f"UPDATE {NEWS_TABLE} SET {set_clause} WHERE id = %s"

    cursor = conn.cursor()
    try:
        cursor.executemany(query, params)
        conn.commit()
        return cursor.rowcount
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()


def delete_news_by_ids(conn, news_ids: List[int]) -> int:
    """根据 ID 列表批量删除新闻记录。

    参数：
        conn: 数据库连接
        news_ids: 要删除的新闻 id 列表

    返回：
        实际删除的记录数
    """
    if not news_ids:
        return 0

    # 去重并确保为整数
    ids = sorted({int(nid) for nid in news_ids})
    placeholders = ", ".join(["%s"] * len(ids))
    query = f"DELETE FROM {NEWS_TABLE} WHERE id IN ({placeholders})"

    cursor = conn.cursor()
    try:
        cursor.execute(query, ids)
        conn.commit()
        return cursor.rowcount
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()


def get_existing_url_date_pairs(
    conn, dates: List[str], table_name: str = NEWS_TABLE
) -> set:
    """
    获取数据库中指定日期范围内已存在的 (date, link) 对集合。

    Parameters:
        conn: 数据库连接
        dates: 日期字符串列表，格式 YYYY-MM-DD
        table_name: 新闻表名

    Returns:
        set: {(date_str, link), ...}
    """
    if not conn or not dates:
        return set()

    valid_dates = [str(d).strip()[:10] for d in dates if d]
    if not valid_dates:
        return set()

    cursor = conn.cursor(dictionary=True)
    try:
        placeholders = ", ".join(["%s"] * len(valid_dates))
        query = f"SELECT link, date FROM {table_name} WHERE date IN ({placeholders})"
        cursor.execute(query, valid_dates)
        return {(str(row["date"]).strip()[:10], row["link"]) for row in cursor.fetchall()}
    finally:
        cursor.close()


def parse_json_safe(value: Any) -> Optional[Any]:
    """安全解析 JSON。"""
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






