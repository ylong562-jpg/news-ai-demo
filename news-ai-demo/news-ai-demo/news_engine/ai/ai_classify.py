
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

from ai_engine import BailianWebAnalyzer, process_with_ai_deduplication
from prompt import category_prompt, tag_classification_prompt, VALID_CATEGORIES, validate_tags_output, filter_low_confidence_tags
import asyncio
from asyncio import Semaphore
import json


# 预定义的标准分类，已在这些分类中的新闻不再参与 AI 再分类
VALID_CATEGORIES_SET = set(VALID_CATEGORIES)



def _clean_category_response(raw: str, candidates: list) -> str:
    """
    清理 LLM 返回的分类结果，处理各种格式问题。
    """
    if not raw:
        return ""

    category = raw.strip()

    # 移除 markdown 代码块标记
    category = category.replace('```json', '').replace('```', '').strip()

    # 移除常见包装符号
    for wrapper in ['"', '"', '"', ''', ''', '「', '」', '【', '】', '《', '》', '(', ')', '[', ']']:
        category = category.replace(wrapper, '')

    # 移除标点符号
    for punct in ['，', ',', '。', '.', '、', '：', ':', ';', '；', '!', '！', '?', '？', '-', '—', '·']:
        category = category.replace(punct, '')

    # 移除序号前缀（如 "1. "、"A. "）
    category = category.lstrip('0123456789.abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ ').strip()

    # 移除常见前缀词
    for prefix in ['分类', '类别', '结果', '答案是', '答案是:', '答案是：', '输出', '属于', '归为']:
        if category.startswith(prefix):
            category = category[len(prefix):].strip()

    return category


def assign_broad_category(news_item: dict) -> dict:
    """
    【已废弃】原大类标签体系已移除，本函数不再执行任何操作，仅保留函数签名以兼容旧代码。
    """
    # 大类标签已废弃，不再赋值
    news_item.pop('broad_category', None)
    return news_item


async def classify_tags(news_item: dict, analyzer: BailianWebAnalyzer = None, semaphore: Semaphore = None) -> dict:
    """
    对单条新闻进行标签分类。
    标签分类独立于类别分类，根据新闻内容从预定义标签体系中选择相关标签。

    返回：
        在 news_item 中增加 'tags' 字段，值为 JSON 字符串：
        {"main_tags": [...], "secondary_tags": [...]}
    """
    if analyzer is None:
        analyzer = BailianWebAnalyzer(region="beijing")

    title = news_item.get('title', '')
    summary = news_item.get('summary', '')

    if not title and not summary:
        news_item['tags'] = json.dumps({"main_tags": [], "secondary_tags": []}, ensure_ascii=False)
        return news_item

    # 构造输入文本
    input_text = f"标题：{title}\n\n正文：{summary[:3000]}"

    async def _call_tag_api():
        result = await analyzer.analyze(
            content=input_text,
            prompt=tag_classification_prompt,
            max_tokens=300,
            model="<your-model>"
        )
        return result

    try:
        if semaphore:
            async with semaphore:
                result = await _call_tag_api()
        else:
            result = await _call_tag_api()

        if result.get("success"):
            raw_tags = result.get("analysis", "")
            validated_tags = validate_tags_output(raw_tags)
            # 标签分类结果完全由 AI 基于语义综合判断，不再进行关键词后处理修正。
            # 后置过滤低密度主维度，避免次要标签被生成独立报道
            validated_tags = filter_low_confidence_tags(validated_tags)
            # 提取分类依据作为中间变量，不存入 tags 字段（不写入数据库）
            try:
                tags_dict = json.loads(validated_tags)
                classification_reason = tags_dict.pop("classification_reason", None)
                if classification_reason:
                    news_item['_classification_reason'] = classification_reason
                validated_tags = json.dumps(tags_dict, ensure_ascii=False)
            except Exception:
                pass
            news_item['tags'] = validated_tags
            print(f"   ✅ 标签分类成功: {validated_tags[:100]}...")
        else:
            print(f"   ⚠️ 标签分类失败: {result.get('error', '未知错误')}")
            news_item['tags'] = json.dumps({"main_tags": [], "secondary_tags": []}, ensure_ascii=False)
    except Exception as e:
        print(f"   ❌ 标签分类异常: {str(e)}")
        news_item['tags'] = json.dumps({"main_tags": [], "secondary_tags": []}, ensure_ascii=False)

    return news_item


async def classify_tags_batch(news_data: list, max_concurrency: int = 5) -> list:
    """
    批量对新闻进行标签分类
    """
    if not news_data:
        return []

    analyzer = BailianWebAnalyzer(region="beijing")
    semaphore = Semaphore(max_concurrency)

    async def _process_single(news_item):
        try:
            return await classify_tags(news_item, analyzer=analyzer, semaphore=semaphore)
        except Exception as e:
            print(f"   ❌ 标签分类任务异常: {str(e)}")
            if isinstance(news_item, dict):
                news_item['tags'] = json.dumps({"main_tags": [], "secondary_tags": []}, ensure_ascii=False)
                return news_item
            return None

    tasks = [asyncio.create_task(_process_single(item)) for item in news_data if item is not None]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    final_results = []
    for result in results:
        if isinstance(result, Exception):
            print(f"❌ 标签分类任务异常: {result}")
        elif result is not None:
            final_results.append(result)

    print(f"\n📊 标签分类完成: {len(final_results)}/{len(news_data)} 条")
    return final_results


async def re_classify(news_data=None, prompt=category_prompt, force_reclassify=False):
    """
    【新流程】对新闻进行标签分类（不再生成 category/broad_category）。

    Parameters:
        news_data (list, optional): 已获取的新闻数据列表。如果为None，则会重新获取数据
        prompt (str): 保留参数兼容，但新流程中不再使用旧分类 prompt
        force_reclassify (bool): 是否强制重新打标签

    流程:
        1. 如果未提供 news_data，则获取新闻数据（带前置去重）
        2. 使用 classify_tags_batch 对所有新闻进行标签分类（含主维度）
        3. 返回带 tags 的新闻数据
    """
    print("\n" + "="*70)
    print("🚀 启动新闻标签分类流程")
    print("="*70)

    # 步骤1: 获取新闻数据（如果未提供）
    if news_data is None:
        print("\n📥 步骤1: 获取新闻数据...")
        news_data = await process_with_ai_deduplication(
            enable_ai_deduplication=True,
            dedup_threshold=0.85
        )

        if not news_data:
            print("❌ 没有获取到新闻数据")
            return []

        print(f"✅ 获取到 {len(news_data)} 条新闻")
    else:
        print(f"\n📥 步骤1: 使用传入的新闻数据 ({len(news_data)} 条)")
        print("   跳过数据获取和去重流程")

    # 步骤2: 标签分类
    print("\n🏷️ 步骤2: AI 标签分类...")
    final_results = await classify_tags_batch(news_data, max_concurrency=5)

    # 打印主维度统计
    print("\n" + "="*70)
    print("📊 标签分类完成统计:")
    print("="*70)

    main_dim_stats = {}
    for news in final_results:
        tags = news.get('tags', '')
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = {}
        if isinstance(tags, dict):
            for tag in tags.get('main_tags', []):
                main_dim_stats[tag] = main_dim_stats.get(tag, 0) + 1

    print("【主维度统计】")
    for tag, count in sorted(main_dim_stats.items(), key=lambda x: x[1], reverse=True):
        print(f"  {tag}: {count}条")

    print(f"\n总计: {len(final_results)}条新闻")
    print("="*70)

    return final_results


if __name__ == "__main__":
    result = asyncio.run(re_classify())

    if result:
        print(f"\n🎉 处理完成！共分类 {len(result)} 条新闻")
        # 显示前3条结果示例
        print("\n结果示例:")
        for i, news in enumerate(result[:3], 1):
            print(f"\n{i}. {news['title'][:50]}...")
            print(f"   分类: {news.get('category', 'N/A')}")
            print(f"   主标签: {news.get('main_tags', 'N/A')}")
            print(f"   来源: {news.get('source_platform', 'N/A')}")
    else:
        print("\n❌ 处理失败")
