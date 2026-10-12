"""
完整的新闻处理流程
整合了所有步骤：获取 -> 去重 -> 内容提取 -> 报告生成 -> 数据库导入
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
import mysql.connector
from datetime import datetime, timedelta

# 导入各个模块
from news_combine import get_combined_news_with_metadata, _normalize_url_for_dedup
from news_analysis.backend.news.database import get_existing_url_date_pairs, NEWS_TABLE
from ai_deduplicate_engine import (
    ai_deduplicate_news_reports,
    _expand_news_reports,
)
from ai_news_core_content_extraction import process_news_with_category_prompts
from ai_news_report_creating import generate_news_report_from_existing_data
from ai_classify import re_classify
from confidence_config import calculate_confidence, is_absolute_truth, detect_platform_from_url


def connect_to_database(host_name, user_name, port, user_password, db_name):
    """创建数据库连接"""
    try:
        # 确保 port 为整数，避免 mysql-connector 格式化错误消息时崩溃
        port = int(port) if port else 3306
        
        connection = mysql.connector.connect(
            host=host_name,
            user=user_name,
            port=port,
            password=user_password,
            database=db_name,
            charset='utf8mb4',
            connect_timeout=30,  # 增加连接超时时间，网络慢时也能连上
            use_pure=True  # 使用纯 Python 驱动，避免 C 扩展的格式字符串 bug
        )
        print("数据库连接成功")
        return connection
    except mysql.connector.Error as err:
        err_no = err.errno if hasattr(err, 'errno') else 'N/A'
        err_msg = str(err)
        print(f"数据库连接失败 [错误码 {err_no}]: {err_msg}")
        if err_no == 2003:
            print("\n💡 排查建议：")
            print(f"   1. 检查网络是否能访问 {host_name}:{port}")
            print("   2. 如使用 VPN / 代理，尝试关闭后再连接")
            print("   3. 确认腾讯云的 IP 白名单是否包含当前公网 IP")
            print("   4. 确认数据库服务是否正常运行")
        return None
    except Exception as e:
        print(f"数据库连接失败: {e}")
        return None


async def ensure_table_schema(connection):
    """确保数据库表结构包含新字段"""
    try:
        cursor = connection.cursor()
        
        # 检查source_platform字段是否存在
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns 
            WHERE table_name = 'ship_news' AND column_name = 'source_platform'
        """)
        
        if cursor.fetchone()[0] == 0:
            print("添加 source_platform 字段...")
            cursor.execute("""
                ALTER TABLE ship_news 
                ADD COLUMN source_platform VARCHAR(100) DEFAULT 'unknown'
            """)
        
        # 检查confidence_score字段是否存在
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns 
            WHERE table_name = 'ship_news' AND column_name = 'confidence_score'
        """)
        
        if cursor.fetchone()[0] == 0:
            print("添加 confidence_score 字段...")
            cursor.execute("""
                ALTER TABLE ship_news 
                ADD COLUMN confidence_score INT DEFAULT 50
            """)
        
        # 检查tags字段是否存在（标签JSON）
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns 
            WHERE table_name = 'ship_news' AND column_name = 'tags'
        """)
        
        if cursor.fetchone()[0] == 0:
            print("添加 tags 字段...")
            cursor.execute("""
                ALTER TABLE ship_news 
                ADD COLUMN tags TEXT
            """)
        
        # 确保 summary 字段为 LONGTEXT，以支持存储完整清洗后的原文
        cursor.execute("""
            SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
            FROM information_schema.columns 
            WHERE table_name = 'ship_news' AND column_name = 'summary'
        """)
        row = cursor.fetchone()
        if row:
            data_type = row[0].upper() if row[0] else ""
            if data_type != "LONGTEXT":
                print("将 summary 字段升级为 LONGTEXT，以支持完整原文...")
                cursor.execute("ALTER TABLE ship_news MODIFY COLUMN summary LONGTEXT")
        else:
            print("添加 summary 字段（LONGTEXT）...")
            cursor.execute("ALTER TABLE ship_news ADD COLUMN summary LONGTEXT")
        
        # 废弃字段：subcategory、impact 不再使用，确保默认值为空串
        for obsolete_col in ('subcategory', 'impact'):
            cursor.execute("""
                SELECT COUNT(*) FROM information_schema.columns 
                WHERE table_name = 'ship_news' AND column_name = %s
            """, (obsolete_col,))
            if cursor.fetchone()[0] > 0:
                cursor.execute(f"""
                    ALTER TABLE ship_news 
                    ALTER COLUMN {obsolete_col} SET DEFAULT ''
                """)
        
        connection.commit()
        cursor.close()
        
    except Exception as e:
        print(f"检查/更新表结构失败: {e}")


def _extract_main_dimensions_from_tags(tags_text):
    """从 tags JSON 字符串中提取主维度列表。"""
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


async def fetch_existing_news_by_main_dimensions(connection, days_back, main_dimensions):
    """从数据库获取包含指定主维度标签的近期新闻数据（用于去重）"""
    existing_news_by_dimension = {dim: [] for dim in main_dimensions}

    try:
        cursor = connection.cursor(dictionary=True)
        start_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')

        for dim in main_dimensions:
            # 使用 LIKE 匹配 tags 中包含该主维度的记录
            like_pattern = f'%"{dim}"%'
            query = """
            SELECT id, title, link, date, news_report, summary, source_platform, confidence_score, tags
            FROM ship_news 
            WHERE date >= %s AND (tags LIKE %s OR news_report LIKE %s)
            ORDER BY date DESC
            """
            cursor.execute(query, [start_date, like_pattern, like_pattern])
            existing_news = cursor.fetchall()
            for news in existing_news:
                if news['news_report'] or news['summary']:
                    existing_news_by_dimension[dim].append(news)

        cursor.close()
        total = sum(len(items) for items in existing_news_by_dimension.values())
        print(f"从数据库获取到 {total} 条现有新闻（按主维度）")

    except Exception as e:
        print(f"获取数据库新闻失败: {e}")

    return existing_news_by_dimension


async def fetch_existing_reports_by_main_tags(connection, main_tags, days_back=7, exclude_ids=None):
    """
    从数据库获取近 N 天内指定主维度的报告条目（用于报告级去重）。
    exclude_ids: 需要排除的新闻 ID 列表（例如重新生成时排除原新闻自身）。
    """
    existing_reports = []
    if not main_tags or not connection:
        return existing_reports

    cursor = connection.cursor(dictionary=True)
    start_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    exclude_ids = set(exclude_ids or [])

    for tag in set(main_tags):
        like_pattern = f'%"{tag}"%'
        params = [start_date, like_pattern, like_pattern]
        id_filter = ""
        if exclude_ids:
            placeholders = ', '.join(['%s'] * len(exclude_ids))
            id_filter = f" AND id NOT IN ({placeholders})"
            params.extend(exclude_ids)
        query = f"""
        SELECT id, title, link, date, source_platform, confidence_score, news_report, data
        FROM ship_news
        WHERE date >= %s AND (tags LIKE %s OR news_report LIKE %s){id_filter}
        ORDER BY date DESC
        """
        cursor.execute(query, params)
        rows = cursor.fetchall()
        for row in rows:
            report_groups = _expand_news_reports(row)
            for g in report_groups:
                if g['main_tag'] == tag:
                    existing_reports.append({
                        'id': row.get('id'),
                        'main_tag': g['main_tag'],
                        'report_text': g['report'],
                        'title': row.get('title', ''),
                        'source_platform': row.get('source_platform', ''),
                        'confidence_score': row.get('confidence_score'),
                        'date': row.get('date', ''),
                        'link': row.get('link', ''),
                        'data': row.get('data', ''),
                    })

    cursor.close()
    total = len(existing_reports)
    excluded = len(exclude_ids)
    print(f"从数据库获取到 {total} 条历史报告（近 {days_back} 天，按主维度，排除 {excluded} 条自身）")
    return existing_reports


async def deduplicate_news_reports_with_database(
    new_news_data, connection, days_back=7, log_callback=None, exclude_ids=None
):
    """对新闻报告进行去重：同时处理批次内 + 近三天数据库历史。"""
    if not new_news_data or not connection:
        return new_news_data

    main_tags = set()
    for news in new_news_data:
        for g in _expand_news_reports(news):
            if g.get('main_tag'):
                main_tags.add(g['main_tag'])

    existing_reports = await fetch_existing_reports_by_main_tags(
        connection, main_tags, days_back, exclude_ids=exclude_ids
    )

    return await ai_deduplicate_news_reports(
        new_news_data,
        existing_reports=existing_reports,
        log_callback=log_callback,
        connection=connection,
    )


async def deduplicate_with_database(connection, new_news_data, days_back=3, similarity_threshold=0.8):
    """与数据库中的新闻进行去重（基于主维度）。

    新增置信度替换机制：
    - 当新新闻与数据库中某条（或多条）新闻判定为重复时，比较双方的 confidence_score。
    - 若新新闻置信度**严格高于**所有匹配到的现有新闻，则删除这些低置信度旧记录，保留新新闻。
    - 若存在任一现有新闻置信度大于或等于新新闻，则丢弃新新闻，保留现有记录。
    """
    from ai_engine import BailianWebAnalyzer

    if not new_news_data:
        return []

    try:
        cursor = connection.cursor()
    except Exception as e:
        print(f"创建游标失败: {e}")
        return new_news_data

    # 按主维度组织新新闻
    categorized_new_news = {}
    for news in new_news_data:
        dims = _extract_main_dimensions_from_tags(news.get('tags', ''))
        if not dims:
            dims = ['未分类']
        for dim in dims:
            if dim not in categorized_new_news:
                categorized_new_news[dim] = []
            categorized_new_news[dim].append(news)

    # 获取现有新闻
    existing_news_by_dimension = await fetch_existing_news_by_main_dimensions(
        connection, days_back, list(categorized_new_news.keys())
    )

    analyzer = BailianWebAnalyzer(region="beijing")
    deduplicated_news = []
    deleted_ids = []
    seen_titles = set()

    for dim, new_news_list in categorized_new_news.items():
        print(f"处理主维度: {dim} ({len(new_news_list)}条新新闻)")

        existing_news_in_dimension = existing_news_by_dimension.get(dim, [])

        for new_news in new_news_list:
            # 避免同一条新闻在不同主维度下重复加入结果
            title_key = new_news.get('title', '') + '|' + new_news.get('link', '')
            if title_key in seen_titles:
                continue

            new_text = new_news.get('news_report', '') or new_news.get('summary', '')
            matches = []

            if new_text:
                for existing_news in existing_news_in_dimension:
                    existing_text = existing_news.get('news_report', '') or existing_news.get('summary', '')

                    if existing_text:
                        similarity_result = await check_similarity(
                            analyzer, new_text, existing_text,
                            new_news.get('title', ''), existing_news.get('title', '')
                        )

                        if similarity_result >= similarity_threshold:
                            matches.append(existing_news)

            if matches:
                new_conf = int(new_news.get('confidence_score') or 50)
                higher_equal = [
                    e for e in matches
                    if int(e.get('confidence_score') or 50) >= new_conf
                ]

                if higher_equal:
                    print(f"发现重复且置信度不更高: {new_news.get('title', '')[:50]}...")
                    continue
                else:
                    for existing_news in matches:
                        existing_id = existing_news.get('id')
                        if existing_id:
                            try:
                                cursor.execute(
                                    "DELETE FROM ship_news WHERE id = %s",
                                    (existing_id,)
                                )
                                deleted_ids.append(existing_id)
                                print(
                                    f"删除低置信度重复新闻(id={existing_id}): "
                                    f"{existing_news.get('title', '')[:50]}..."
                                )
                            except Exception as e:
                                print(f"删除失败(id={existing_id}): {e}")
                        existing_news_in_dimension.remove(existing_news)

            seen_titles.add(title_key)
            deduplicated_news.append(new_news)

    if deleted_ids:
        try:
            connection.commit()
            print(f"已删除 {len(deleted_ids)} 条低置信度重复新闻")
        except Exception as e:
            print(f"提交删除失败: {e}")
            connection.rollback()

    cursor.close()
    return deduplicated_news


async def check_similarity(analyzer, text1, text2, title1="", title2=""):
    """检查两段文本的相似度"""
    try:
        import re
        
        prompt = f"""
        判断以下两段新闻是否描述同一事件，返回0-1之间的相似度分数。
        新闻1标题: {title1}
        新闻1内容: {text1[:500]}
        新闻2标题: {title2}
        新闻2内容: {text2[:500]}
        只返回数字，不要解释。
        """

        result = await analyzer.analyze(
            content=prompt,
            prompt="返回相似度分数(0-1之间)",
            max_tokens=50,
            model="<your-model>"
        )

        if result["success"]:
            response = result["analysis"].strip()
            numbers = re.findall(r"0\.\d+", response)
            if numbers:
                return float(numbers[0])
        return 0.0

    except Exception as e:
        print(f"相似度检查异常: {e}")
        return 0.0


async def import_news_to_database(connection, news_data):
    """将新闻数据导入数据库"""
    try:
        cursor = connection.cursor()

        # 入库前再次兜底识别 source_platform
        for news in news_data:
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

        insert_sql = """
        INSERT INTO ship_news 
        (title, link, date, summary,
         data, news_report, tags,
         source_platform, confidence_score) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        success_count = 0
        for news in news_data:
            try:
                data_tuple = (
                    news['title'],
                    news['link'],
                    news.get('date'),
                    news.get('summary', ''),
                    news.get('data', ''),
                    news.get('news_report', ''),
                    news.get('tags', ''),
                    news.get('source_platform', 'unknown'),
                    news.get('confidence_score', 50)
                )
                cursor.execute(insert_sql, data_tuple)
                success_count += 1
            except Exception as e:
                print(f"插入失败: {news.get('title', 'unknown')[:50]}... - {str(e)[:100]}")
                continue

        connection.commit()
        cursor.close()
        print(f"成功导入 {success_count}/{len(news_data)} 条新闻")

    except Exception as e:
        print(f"数据导入失败: {e}")
        connection.rollback()
        raise


def pre_deduplicate_by_url_against_database(news_data, db_config):
    """
    与数据库中相同日期的新闻按 URL 进行预查重。
    若 (date, link) 已存在，则直接视为重复并丢弃。
    URL 比较忽略大小写（调用 _normalize_url_for_dedup 归一化），不调用 AI。
    """
    if not news_data or not db_config:
        return news_data

    print("\n🔍 预查重：与数据库相同日期新闻按 URL 去重...")
    connection = connect_to_database(**db_config)
    if not connection:
        print("⚠️ 数据库连接失败，跳过 URL 预查重")
        return news_data

    try:
        dates = {str(news.get("date", "")).strip()[:10] for news in news_data if news.get("date")}
        if not dates:
            print("⚠️ 待查重新闻均无有效日期，跳过 URL 预查重")
            return news_data

        raw_existing_pairs = get_existing_url_date_pairs(connection, dates, NEWS_TABLE)
        # URL 归一化：忽略大小写、末尾斜杠与 www 前缀
        existing_pairs = {
            (date_str, _normalize_url_for_dedup(link))
            for date_str, link in raw_existing_pairs
        }

        unique_news = []
        duplicate_count = 0
        seen_local_pairs = set()
        for news in news_data:
            date_str = str(news.get("date", "")).strip()[:10]
            link = news.get("link", "")

            if not link:
                unique_news.append(news)
                continue

            pair = (date_str, _normalize_url_for_dedup(link))

            if pair in existing_pairs:
                duplicate_count += 1
                print(f"  与数据库 URL 重复（忽略大小写）: {link[:80]}")
                continue

            if pair in seen_local_pairs:
                duplicate_count += 1
                print(f"  批次内 URL 重复（忽略大小写）: {link[:80]}")
                continue
            seen_local_pairs.add(pair)
            unique_news.append(news)

        print(f"✅ URL 预查重完成：去除 {duplicate_count} 条重复新闻，剩余 {len(unique_news)} 条")
        return unique_news
    except Exception as e:
        print(f"⚠️ URL 预查重失败: {e}")
        return news_data
    finally:
        connection.close()


async def run_complete_pipeline(
    enable_db_dedup=True,
    db_config=None,
    vpn_mode='all',
    include_wechat_mp=True,
    start_date=None,
    end_date=None
):
    """
    运行完整的新闻处理流程
    
    Parameters:
    enable_db_dedup: 是否启用报告级去重
    db_config: 数据库配置
    vpn_mode: 'all' 运行所有, 'no_vpn' 只运行不需要VPN的, 'vpn' 只运行需要VPN的
    start_date: 开始日期 YYYY-MM-DD，默认昨天
    end_date: 结束日期 YYYY-MM-DD，默认今天
    
    Returns:
    list: 处理完成的新闻列表
    """
    
    # 默认抓取昨天和今天两天
    if not start_date or not end_date:
        today = datetime.now().strftime('%Y-%m-%d')
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        start_date = yesterday
        end_date = today
    
    print("\n" + "="*70)
    print("🚀 启动完整新闻处理流程")
    print(f"📅 抓取日期范围: {start_date} 至 {end_date}")
    print("="*70)
    
    # 步骤1: 获取新闻数据
    print("\n📥 步骤1: 获取新闻数据...")
    news_data = await get_combined_news_with_metadata(
        vpn_mode=vpn_mode,
        include_wechat_mp=include_wechat_mp,
        start_date=start_date,
        end_date=end_date
    )
    if not news_data:
        print("❌ 没有获取到新闻数据")
        return []
    print(f"✅ 获取到 {len(news_data)} 条新闻")

    # 步骤1b: 与数据库相同日期新闻按 URL 预查重
    if db_config:
        news_data = pre_deduplicate_by_url_against_database(news_data, db_config)
        if not news_data:
            print("ℹ️ 所有新闻均与数据库中相同日期 URL 重复")
            return []
    
    # 如果是VPN模式，提示用户关闭VPN再继续AI操作
    if vpn_mode == 'vpn':
        print("\n" + "="*70)
        print("⚠️  VPN模式检测到 - 新闻爬取已完成")
        print("="*70)
        print("\n🌐 获取需要VPN的新闻已完成，接下来将调用AI API进行：")
        print("   - AI重新分类")
        print("   - AI内容提取")
        print("   - AI报告生成")
        print("   - 报告级去重")
        print("\n💡 提示：建议在继续前关闭VPN，以防止AI API连接失败")
        print("="*70)
        input("\n按回车键继续（请确保已关闭VPN）...")
    
    # 步骤2: AI标签分类（含主维度，不再生成 category/broad_category）
    print("\n🏷️  步骤2: AI标签分类...")
    news_data = await re_classify(news_data=news_data)
    if not news_data:
        print("❌ 标签分类失败")
        return []
    print(f"✅ 标签分类完成，共 {len(news_data)} 条")
    
    # 步骤3: 内容提取（按主维度拆分，生成多组 entity, data, tags）
    print("\n📝 步骤3: 提取核心内容...")
    news_data = await process_news_with_category_prompts(news_data)
    print(f"✅ 内容提取完成，共 {len(news_data)} 条")
    
    # 过滤掉处理失败的新闻（None值）
    news_data = [n for n in news_data if n is not None]
    print(f"过滤后剩余 {len(news_data)} 条有效新闻")
    
    # 步骤4: 生成新闻报告（按主维度生成多组 report）
    print("\n📰 步骤4: 生成新闻报告...")
    news_data = await generate_news_report_from_existing_data(news_data)
    print(f"✅ 报告生成完成")
    
    # 步骤5: 连接数据库
    if db_config:
        print("\n💾 步骤5: 连接数据库...")
        connection = connect_to_database(**db_config)
        if not connection:
            print("❌ 数据库连接失败")
            return news_data
        
        # 确保表结构
        await ensure_table_schema(connection)
        
        # 步骤6: 报告级去重（同批次 + 近一周历史；不调用 AI，按主体/船型）
        if enable_db_dedup:
            print("\n🗄️  步骤6: 按主维度进行报告级去重（同批次 + 近一周历史）...")
            news_data = await deduplicate_news_reports_with_database(
                news_data, connection, days_back=7
            )
            print(f"✅ 报告去重完成，共 {len(news_data)} 条（重复项已标记）")
        
        # 步骤7: 导入数据库
        if news_data:
            print("\n📤 步骤7: 导入数据库...")
            await import_news_to_database(connection, news_data)
        
        connection.close()
    
    print("\n" + "="*70)
    print(f"✅ 流程完成！共处理 {len(news_data)} 条新闻")
    print("="*70)
    
    return news_data


async def main():
    """主函数示例"""
    
    # 数据库配置
    from db_config import DB_CONFIG as db_config
    
    try:
        result = await run_complete_pipeline(
            enable_db_dedup=True,
            db_config=db_config
        )
        
        # 显示结果样例
        if result:
            print("\n结果样例:")
            for i, news in enumerate(result[:3], 1):
                print(f"\n{i}. {news.get('title', 'N/A')[:50]}...")
                print(f"   来源: {news.get('source_platform', 'N/A')}")
                print(f"   标签: {news.get('tags', 'N/A')}")
                print(f"   置信度: {news.get('confidence_score', 'N/A')}%")
                print(f"   实体: {news.get('entity', 'N/A')}")
                print(f"   数据: {news.get('data', 'N/A')}")
                print(f"   报告: {news.get('news_report', 'N/A')[:60]}...")
        
        return result
        
    except Exception as e:
        print(f"❌ 流程执行失败: {e}")
        import traceback
        traceback.print_exc()
        return []


if __name__ == "__main__":
    asyncio.run(main())
