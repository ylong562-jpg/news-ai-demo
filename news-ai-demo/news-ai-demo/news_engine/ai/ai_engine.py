"""
AI引擎 - 增强版
集成前置AI去重、置信度评分和新闻摘要生成
"""

import sys
import os
import asyncio

from openai import OpenAI
from news_combine import get_combined_news_with_metadata
from content_extractor import extract_article
from ai_deduplicate_engine import AIDeduplicateEngine
from confidence_config import calculate_confidence, is_absolute_truth


class BailianWebAnalyzer:
    def __init__(self, region="beijing"):
        """
        初始化阿里云百炼分析器
        """
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            print("❌ 错误: 请设置DASHSCOPE_API_KEY环境变量")
            sys.exit(1)

        base_urls = {
            "default": "<your-llm-endpoint>",  # 演示版已移除真实接口地址
        }

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_urls.get(region, base_urls["default"])
        )

        self.available_models = {
            "<your-model>": "（演示版已隐藏模型信息）"
        }
        
        # 初始化AI去重引擎
        self.dedup_engine = AIDeduplicateEngine(region=region)

    async def analyze(self, url=None, content=None, prompt="总结核心要点：", max_tokens=200, model="<your-model>", semaphore=None):
        """添加semaphore参数来控制并发"""
        try:
            if model not in self.available_models:
                print(f"⚠️  警告: 模型 {model} 可能不受支持")

            # 如果有semaphore，使用它来控制并发
            if semaphore:
                async with semaphore:
                    return await self._analyze_core(url, content, prompt, max_tokens, model)
            else:
                return await self._analyze_core(url, content, prompt, max_tokens, model)

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _analyze_core(self, url=None, content=None, prompt="总结核心要点：", max_tokens=200, model="<your-model>"):
        """将原有的分析逻辑提取出来"""
        # 模式判断：优先使用直接提供的 content
        if content is not None:
            print("🔍 进入直接分析模式...")

            # 直接构建一个包含提供的内容的"结果"对象，模拟爬取成功
            class SimpleResult:
                def __init__(self, text_content):
                    self.success = True
                    self.markdown = type('Markdown', (), {'raw_markdown': text_content})()

            simulated_result = SimpleResult(content)
            return await self._call_bailian(simulated_result, prompt, max_tokens, model)

        # 模式1：传统URL爬取模式
        elif url:
            print("🔍 进入网页爬取分析模式...")
            result = await extract_article(url=url, max_length=50000)

            if result.get('success') and result.get('content'):
                # 构造一个模拟的 crawl4ai 结果对象，复用 _call_bailian
                class SimpleResult:
                    def __init__(self, text_content):
                        self.success = True
                        self.markdown = type('Markdown', (), {'raw_markdown': text_content})()

                simulated_result = SimpleResult(result['content'])
                return await self._call_bailian(simulated_result, prompt, max_tokens, model)
            else:
                return {"success": False, "error": f"抓取失败: {result.get('error', '未知错误')}"}
        else:
            # 如果既没有提供 content 也没有提供有效的 url
            return {"success": False, "error": "必须提供有效的 URL 或直接的分析内容 (content)。"}

    async def _call_bailian(self, result, prompt, max_tokens, model):
        """调用阿里云百炼API - 精简版本"""
        try:
            content = ""

            # 提取内容：优先处理模拟结果，然后是真实爬虫结果
            if hasattr(result, 'markdown') and hasattr(result.markdown, 'raw_markdown'):
                content = result.markdown.raw_markdown
            elif hasattr(result, 'markdown') and result.markdown:
                if hasattr(result.markdown, 'cleaned_markdown') and result.markdown.cleaned_markdown:
                    content = result.markdown.cleaned_markdown
                elif hasattr(result.markdown, 'fit_markdown') and result.markdown.fit_markdown:
                    content = result.markdown.fit_markdown
                elif hasattr(result.markdown, 'raw_markdown') and result.markdown.raw_markdown:
                    content = result.markdown.raw_markdown

            # 备选内容源
            if not content:
                if hasattr(result, 'html') and result.html:
                    content = result.html
                elif hasattr(result, 'text') and result.text:
                    content = result.text
                else:
                    return {"success": False, "error": "无法提取有效内容"}

            # 验证内容有效性
            if not content or len(str(content).strip()) < 10:
                return {"success": False, "error": "提取的内容无效或过短"}

            # 编码处理
            if isinstance(content, bytes):
                content = content.decode('utf-8', errors='ignore')
            content = content.encode('utf-8', errors='ignore').decode('utf-8')

            # 内容长度限制（兜底截断，content_extractor 已做智能截断）
            if len(content) > 50000:
                content = content[:50000] + "...[内容已截断]"

            # 构建API请求消息
            messages = [
                {
                    "role": "system",
                    "content": "你是一个专业的文本分析助手"
                },
                {
                    "role": "user",
                    "content": f"{prompt}\n\n分析内容：\n{content}"
                }
            ]

            # 调用百炼API
            response = self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.3,
                max_tokens=max_tokens
            )

            analysis_result = response.choices[0].message.content

            return {
                "success": True,
                "analysis": analysis_result,
                "usage": response.usage,
                "model": model
            }

        except Exception as e:
            return {"success": False, "error": f"百炼API调用失败: {str(e)}"}


async def process_with_ai_deduplication(enable_ai_dedup=True, dedup_threshold=0.85):
    """
    处理新闻的主函数 - 集成前置AI去重
    
    Parameters:
    enable_ai_dedup (bool): 是否启用AI去重
    dedup_threshold (float): 去重相似度阈值
    
    Returns:
    list: 处理后的新闻列表
    """
    print("\n" + "="*70)
    print("🚀 启动AI新闻处理流程（集成前置去重）")
    print("="*70)
    
    # 1. 获取并合并新闻数据（带source_platform和confidence_score）
    print("\n📥 步骤1: 获取新闻数据...")
    news_data = await get_combined_news_with_metadata()
    if not news_data:
        print("❌ 没有获取到新闻数据")
        return None
    
    # 2. AI智能去重（前置去重，节省token）
    if enable_ai_dedup and len(news_data) > 1:
        print("\n🔍 步骤2: AI智能去重...")
        print(f"   去重前数量: {len(news_data)}")
        print(f"   相似度阈值: {dedup_threshold}")
        
        original_count = len(news_data)
        dedup_engine = AIDeduplicateEngine(region="beijing")
        news_data = await dedup_engine.deduplicate_by_groups(news_data, similarity_threshold=dedup_threshold)
        
        removed_count = original_count - len(news_data)
        save_ratio = (removed_count / original_count * 100) if original_count > 0 else 0
        print(f"   去重后数量: {len(news_data)}")
        print(f"   去除重复: {removed_count} 条 (节省 {save_ratio:.1f}%)")
    else:
        print("\n⏭️  步骤2: 跳过AI去重（数量不足或已禁用）")
    
    # 3. 更新置信度（基于重复次数）
    print("\n📊 步骤3: 更新置信度评分...")
    for news in news_data:
        platform = news.get('source_platform', '')
        dup_count = news.get('duplicate_count', 1)
        
        # 重新计算置信度
        if not is_absolute_truth(platform, url=news.get('link', '')):
            news['confidence_score'] = calculate_confidence(platform, dup_count, url=news.get('link', ''))
    
    # 统计置信度分布
    high_conf = len([n for n in news_data if n.get('confidence_score', 0) >= 80])
    med_conf = len([n for n in news_data if 50 <= n.get('confidence_score', 0) < 80])
    low_conf = len([n for n in news_data if n.get('confidence_score', 0) < 50])
    
    print(f"   高置信度(>=80%): {high_conf}条")
    print(f"   中置信度(50-79%): {med_conf}条")
    print(f"   低置信度(<50%): {low_conf}条")
    
    return news_data


