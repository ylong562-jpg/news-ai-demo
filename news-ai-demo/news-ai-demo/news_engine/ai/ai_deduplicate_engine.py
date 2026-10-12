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

AI去重引擎 - 前置去重模块
在生成report之前进行AI智能去重，通过分析title和report内容
"""

import asyncio
from openai import OpenAI
import os
import re
import sys
from typing import List, Dict, Tuple, Optional, Callable
import json

from title_utils import title_similarity, normalize_title
from ship_type_retriever import ShipTypeRetriever


# 船型分类检索器单例（用于按层级匹配船型）
_ship_type_retriever: Optional[ShipTypeRetriever] = None


def _get_ship_type_retriever() -> ShipTypeRetriever:
    """延迟初始化船型分类检索器。"""
    global _ship_type_retriever
    if _ship_type_retriever is None:
        _ship_type_retriever = ShipTypeRetriever()
    return _ship_type_retriever


# 实体宽泛/具体标记缓存
_specificity_map: Optional[Dict[str, str]] = None


def _load_specificity_map() -> Dict[str, str]:
    """从实体词表 JSON 计算所有实体的宽泛/具体标记（词表为只读，按规则计算）。"""
    vocab_path = os.getenv("ENTITY_VOCABULARY_PATH", "/path/to/normalized_entity_vocabulary.json")
    mapping: Dict[str, str] = {}
    if not os.path.exists(vocab_path):
        return mapping
    try:
        from entity_deduplication import classify_entity_name
        with open(vocab_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else data.get("entries", [])
        for item in items:
            name = str(item.get("canonical_name") or "").strip()
            if not name:
                continue
            entity_types = item.get("entity_types") or [item.get("entity_type")] or []
            specificity = "specific"
            for et in entity_types:
                if classify_entity_name(name, str(et)) == "broad":
                    specificity = "broad"
                    break
            mapping[name] = specificity
    except Exception:
        pass
    return mapping


def _get_entity_specificity(name: str) -> str:
    """返回实体名称的宽泛/具体标记；未知时使用本地规则兜底。"""
    global _specificity_map
    if _specificity_map is None:
        _specificity_map = _load_specificity_map()
    spec = _specificity_map.get(name)
    if spec:
        return spec
    try:
        from entity_deduplication import classify_entity_name
        for etype in ("shipowner", "shipyard", "company", "buyer", "seller", "charterer", "owner", "parties"):
            if classify_entity_name(name, etype) == "broad":
                _specificity_map[name] = "broad"
                return "broad"
        _specificity_map[name] = "specific"
        return "specific"
    except Exception:
        return "specific"


def _filter_broad_entities(names: set) -> set:
    """从主体集合中剔除宽泛实体，仅保留具体实体。"""
    return {n for n in names if _get_entity_specificity(n) == "specific"}


def _ship_types_overlap(st1: set, st2: set) -> bool:
    """
    判断两个船型集合是否存在重叠，支持船型分类架构的层级匹配：
    - 精确同名视为匹配；
    - 上层分类与下层具体船型视为匹配（如 集装箱船 与 支线集装箱船）；
    - 同一中间层分类下的具体船型视为匹配（如 大型支线集装箱船 与 小型支线集装箱船）；
    - 动力前缀拆分后的基础船型也按层级匹配。
    """
    if not st1 or not st2:
        return False

    # 1) 精确匹配兜底
    if st1 & st2:
        return True

    retriever = _get_ship_type_retriever()

    # 2) 层级匹配
    for a in st1:
        entry_a = retriever.retrieve(a)
        if not entry_a:
            continue
        canonical_a = entry_a.get("canonical_name")
        path_a = set(entry_a.get("path", []))
        level_2_a = entry_a.get("level_2")
        for b in st2:
            entry_b = retriever.retrieve(b)
            if not entry_b:
                continue
            canonical_b = entry_b.get("canonical_name")
            if canonical_a == canonical_b:
                return True
            path_b = set(entry_b.get("path", []))
            # 上层与下层：一个的 canonical_name 出现在另一个的 path 中
            if canonical_a in path_b or canonical_b in path_a:
                return True
            # 同一中间层分类下的兄弟具体船型（如 大型支线 vs 小型支线）
            level_2_b = entry_b.get("level_2")
            if level_2_a and level_2_b and level_2_a == level_2_b:
                return True

    return False


class AIDeduplicateEngine:
    """AI智能去重引擎"""
    
    def __init__(self, region="beijing"):
        """初始化AI分析器"""
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
        
        # 去重提示词 - 精准判断
        self.dedup_prompt = """【演示版：去重判定指令正文已删减】
请判断以下两条新闻是否报道了同一事件，返回 JSON：
{{"is_duplicate": true/false, "confidence": 0-100, "reason": "判断理由"}}

新闻1:
标题: {title1}
内容摘要: {content1}

新闻2:
标题: {title2}
内容摘要: {content2}
"""

    async def check_duplicate_pair(self, news1: Dict, news2: Dict) -> Tuple[bool, float, str]:
        """
        检查两条新闻是否重复
        
        Parameters:
        news1 (dict): 第一条新闻
        news2 (dict): 第二条新闻
        
        Returns:
        tuple: (是否重复, 置信度, 理由)
        """
        try:
            # 构建prompt
            prompt = self.dedup_prompt.format(
                title1=news1.get('title', ''),
                content1=news1.get('summary', '无内容')[:500],
                title2=news2.get('title', ''),
                content2=news2.get('summary', '无内容')[:500]
            )
            
            # 调用AI
            response = self.client.chat.completions.create(
                model="<your-model>",
                messages=[
                    {"role": "system", "content": "你是专业的新闻去重分析专家，只返回JSON格式结果。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,  # 低温度确保结果稳定
                max_tokens=150
            )
            
            result_text = response.choices[0].message.content.strip()
            
            # 解析JSON结果
            try:
                # 尝试直接解析
                result = json.loads(result_text)
            except json.JSONDecodeError:
                # 如果失败，尝试提取JSON部分
                import re
                json_match = re.search(r'\{[^}]+\}', result_text)
                if json_match:
                    result = json.loads(json_match.group())
                else:
                    raise ValueError(f"无法解析AI返回: {result_text}")
            
            is_duplicate = result.get('is_duplicate', False)
            confidence = float(result.get('confidence', 0))
            reason = result.get('reason', '无')
            
            return is_duplicate, confidence, reason
            
        except Exception as e:
            print(f"  AI去重检查失败: {str(e)[:50]}")
            # 失败时保守处理，假设不重复
            return False, 0.0, f"检查失败: {str(e)[:30]}"
    
    async def deduplicate_news_list(self, news_list: List[Dict], 
                                   similarity_threshold: float = 0.85) -> List[Dict]:
        """
        对新闻列表进行AI智能去重
        
        Parameters:
        news_list (list): 新闻列表
        similarity_threshold (float): AI判断重复的置信度阈值
        
        Returns:
        list: 去重后的新闻列表
        """
        if not news_list:
            return []
        
        print(f"\n🔍 开始AI智能去重，共 {len(news_list)} 条新闻...")
        
        unique_news = []
        duplicate_count = 0
        
        for i, news in enumerate(news_list):
            print(f"  检查 [{i+1}/{len(news_list)}]: {news['title'][:40]}...")
            
            if not unique_news:
                unique_news.append(news)
                continue
            
            is_duplicate = False
            
            # 与已保留的新闻进行比较
            for kept_news in unique_news:
                # 新增规则：同一天 + 有相同主维度 + 相同主体 视为重复
                entity_dup_reason = None
                news_dims = self._extract_main_dimensions(news.get('tags', ''))
                kept_dims = self._extract_main_dimensions(kept_news.get('tags', ''))
                has_common_dim = bool(set(news_dims) & set(kept_dims)) if (news_dims and kept_dims) else (
                    news.get('category') == kept_news.get('category') if news.get('category') else False
                )
                if self._is_same_day(news, kept_news) and has_common_dim:
                    # 优先从 data.transactions 提取主体；未命中时回退到旧 entity 字段
                    parties1 = _extract_parties_from_data(news)
                    parties2 = _extract_parties_from_data(kept_news)
                    if not parties1:
                        entity1 = news.get('entity')
                        if entity1:
                            parties1 = _collect_string_values(_safe_json_loads(entity1), _PARTY_A_KEYS + _PARTY_B_KEYS + _PARTY_OTHER_KEYS)
                    if not parties2:
                        entity2 = kept_news.get('entity')
                        if entity2:
                            parties2 = _collect_string_values(_safe_json_loads(entity2), _PARTY_A_KEYS + _PARTY_B_KEYS + _PARTY_OTHER_KEYS)
                    # 宽泛主体不参与去重比较，仅比较具体实体
                    parties1_specific = _filter_broad_entities(parties1)
                    parties2_specific = _filter_broad_entities(parties2)
                    if parties1_specific and parties2_specific and (parties1_specific & parties2_specific):
                        entity_dup_reason = "具体主体名称相同"
                
                if entity_dup_reason:
                    # 优先尝试 data 补充；补充失败再按信息丰富度决定是否替换
                    supplemented = self._try_supplement_data(kept_news, news)
                    if supplemented:
                        print(f"    ⚠️ 发现重复 ({entity_dup_reason})，新报道补充了额外信息，已合并到保留项")
                        print(f"    📰 保留: {kept_news.get('source_platform', 'unknown')}")
                        print(f"    🗑️  丢弃: {news.get('source_platform', 'unknown')}")
                        kept_news['duplicate_count'] = self._merge_duplicate_count(kept_news, news)
                    elif self._is_richer_news(news, kept_news):
                        print(f"    ⚠️ 发现重复 ({entity_dup_reason})，新报道信息更丰富，替换保留")
                        print(f"    📰 保留: {news.get('source_platform', 'unknown')}")
                        print(f"    🗑️  丢弃: {kept_news.get('source_platform', 'unknown')}")
                        news['duplicate_count'] = self._merge_duplicate_count(news, kept_news)
                        unique_news[unique_news.index(kept_news)] = news
                    else:
                        print(f"    ⚠️ 发现重复 ({entity_dup_reason})")
                        print(f"    📰 保留: {kept_news.get('source_platform', 'unknown')}")
                        print(f"    🗑️  丢弃: {news.get('source_platform', 'unknown')}")
                        kept_news['duplicate_count'] = self._merge_duplicate_count(kept_news, news)

                    is_duplicate = True
                    duplicate_count += 1
                    break
                
                # 首先进行快速标题相似度检查
                title_sim = self._quick_title_similarity(
                    news['title'], kept_news['title']
                )
                
                # 标题相似度低时，再检查内容摘要相似度，避免标题改写但内容重复的新闻漏过
                content_sim = 0.0
                if title_sim < 0.3:
                    content1 = news.get('summary', '')[:800]
                    content2 = kept_news.get('summary', '')[:800]
                    if content1 and content2:
                        content_sim = self._quick_title_similarity(content1, content2)
                
                # 标题和内容摘要都不相似时，检查是否同一天且核心量化指标高度重叠
                # （如同一天都报道"中远海发24艘、86亿元"订单），避免同事件不同标题漏过
                core_metrics_overlap = False
                if title_sim < 0.3 and content_sim < 0.5:
                    if self._is_same_day(news, kept_news):
                        metrics1 = self._extract_core_metrics(news.get('summary', ''))
                        metrics2 = self._extract_core_metrics(kept_news.get('summary', ''))
                        if metrics1 and metrics2:
                            overlap = len(metrics1 & metrics2)
                            if overlap >= 2:
                                core_metrics_overlap = True
                
                # 标题、内容摘要、核心指标都不相似，跳过AI检查
                if title_sim < 0.3 and content_sim < 0.5 and not core_metrics_overlap:
                    continue
                
                # 标题相似度高，进行AI精确判断
                ai_is_dup, ai_confidence, reason = await self.check_duplicate_pair(
                    news, kept_news
                )
                
                if ai_is_dup and ai_confidence >= similarity_threshold * 100:
                    # 优先尝试 data 补充；补充失败再按信息丰富度决定是否替换
                    supplemented = self._try_supplement_data(kept_news, news)
                    if supplemented:
                        print(f"    ⚠️ 发现重复 (置信度{ai_confidence:.0f}%): {reason}，新报道补充了额外信息，已合并到保留项")
                        print(f"    📰 保留: {kept_news.get('source_platform', 'unknown')}")
                        print(f"    🗑️  丢弃: {news.get('source_platform', 'unknown')}")
                        kept_news['duplicate_count'] = self._merge_duplicate_count(kept_news, news)
                    elif self._is_richer_news(news, kept_news):
                        print(f"    ⚠️ 发现重复 (置信度{ai_confidence:.0f}%): {reason}，新报道信息更丰富，替换保留")
                        print(f"    📰 保留: {news.get('source_platform', 'unknown')}")
                        print(f"    🗑️  丢弃: {kept_news.get('source_platform', 'unknown')}")
                        news['duplicate_count'] = self._merge_duplicate_count(news, kept_news)
                        unique_news[unique_news.index(kept_news)] = news
                    else:
                        print(f"    ⚠️ 发现重复 (置信度{ai_confidence:.0f}%): {reason}")
                        print(f"    📰 保留: {kept_news.get('source_platform', 'unknown')}")
                        print(f"    🗑️  丢弃: {news.get('source_platform', 'unknown')}")
                        kept_news['duplicate_count'] = self._merge_duplicate_count(kept_news, news)

                    is_duplicate = True
                    duplicate_count += 1
                    break
            
            if not is_duplicate:
                unique_news.append(news)
        
        print(f"\n✅ AI去重完成: {len(news_list)} -> {len(unique_news)} 条")
        print(f"   去除重复: {duplicate_count} 条")
        
        return unique_news
    
    def _quick_title_similarity(self, title1: str, title2: str) -> float:
        """
        快速计算标题相似度（中文友好的 Jaccard）
        用于预筛选，减少AI调用次数

        针对中文标题没有空格分隔的问题，使用字符级 2-gram，
        确保相近的中文标题能被正确识别，不会错过后续 AI 精确判断。
        """
        return title_similarity(title1, title2)
    
    def _is_richer_news(self, news1: Dict, news2: Dict) -> bool:
        """
        判断 news1 是否比 news2 信息更丰富，用于重复时选择保留哪一条。
        优先比较主维度数量；无 tags 时比较内容长度。
        """
        dims1 = self._extract_main_dimensions(news1.get('tags', ''))
        dims2 = self._extract_main_dimensions(news2.get('tags', ''))
        if len(dims1) != len(dims2):
            return len(dims1) > len(dims2)
        
        len1 = len(news1.get('summary', ''))
        len2 = len(news2.get('summary', ''))
        return len1 > len2
    
    def _merge_duplicate_count(self, kept_news: Dict, discarded_news: Dict) -> int:
        """合并两条重复新闻的重复计数，返回合并后的计数。"""
        return kept_news.get('duplicate_count', 1) + discarded_news.get('duplicate_count', 1)

    def _is_valid_value(self, value) -> bool:
        """判断一个值是否可作为有效补充数据（非空、非 null 字符串等）。"""
        if value is None:
            return False
        if isinstance(value, str) and value.strip() in ('', 'null', 'None', 'NULL'):
            return False
        if isinstance(value, (list, dict)) and not value:
            return False
        return True

    def _try_supplement_data(self, kept_news: Dict, new_news: Dict) -> bool:
        """
        尝试用新重复新闻的 data 补充旧新闻的 data。
        补充规则（仅针对新造船订单等交易类主维度）：
        1. 仅当旧新闻某字段为 null/空，且新新闻对应字段有值时才补充；
        2. 旧字段已有值时，无论新值是否相同都不覆盖；
        3. 仅对指定补充字段（classification_society、flag、design_institute）生效；
        4. 支持单条字典与 transactions 数组两种结构，也支持 data 字段为[{main_tag, data}]分组列表；
        5. 返回是否补充了任何字段。
        """
        SUPPLEMENT_FIELDS = {"classification_society", "flag", "design_institute"}

        # 仅对交易类主维度（新造船订单/买卖船/租船）做补充
        kept_dims = self._extract_main_dimensions(kept_news.get('tags', ''))
        new_dims = self._extract_main_dimensions(new_news.get('tags', ''))
        order_tags = {"新造船订单", "买卖船", "租船"}
        if not (set(kept_dims) & order_tags or set(new_dims) & order_tags):
            return False

        kept_data_raw = kept_news.get('data')
        new_data_raw = new_news.get('data')
        if not new_data_raw or not kept_data_raw:
            return False

        def _load(raw):
            """统一加载 data 字段：可能是 dict、list 或 JSON 字符串。"""
            if isinstance(raw, (dict, list)):
                return raw
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except Exception:
                    return None
            return None

        kept_loaded = _load(kept_data_raw)
        new_loaded = _load(new_data_raw)
        if not isinstance(kept_loaded, (dict, list)) or not isinstance(new_loaded, (dict, list)):
            return False

        # 统一转换为分组列表：[{main_tag?, data?}] 或直接视为单组
        def _to_groups(obj):
            if isinstance(obj, list):
                return obj
            return [obj]

        kept_groups = _to_groups(kept_loaded)
        new_groups = _to_groups(new_loaded)
        new_groups_by_tag = {}
        for g in new_groups:
            if isinstance(g, dict):
                tag = g.get("main_tag") or ""
                new_groups_by_tag.setdefault(tag, []).append(g)

        def _supplement_target(old_obj: Dict, new_obj: Dict) -> bool:
            """将 new_obj 中的补充字段按需写入 old_obj，返回是否有写入。"""
            changed = False
            if not isinstance(old_obj, dict) or not isinstance(new_obj, dict):
                return changed
            for field in SUPPLEMENT_FIELDS:
                new_value = new_obj.get(field)
                old_value = old_obj.get(field)
                if not self._is_valid_value(new_value):
                    continue
                if self._is_valid_value(old_value):
                    continue
                old_obj[field] = new_value
                changed = True
            return changed

        def _supplement_data_obj(old_data, new_data) -> bool:
            """对单个 data 对象（单条字典或含 transactions 的字典）执行补充。"""
            changed = False
            if not isinstance(old_data, dict) or not isinstance(new_data, dict):
                return changed
            old_tx = old_data.get("transactions")
            new_tx = new_data.get("transactions")
            if isinstance(old_tx, list) and isinstance(new_tx, list):
                for old_item, new_item in zip(old_tx, new_tx):
                    if _supplement_target(old_item, new_item):
                        changed = True
            else:
                if _supplement_target(old_data, new_data):
                    changed = True
            return changed

        supplemented = False
        for kept_group in kept_groups:
            if not isinstance(kept_group, dict):
                continue
            tag = kept_group.get("main_tag") or ""
            # 只补充交易类分组
            if tag not in order_tags:
                continue
            kept_group_data = kept_group.get("data")
            if isinstance(kept_group_data, str):
                try:
                    kept_group_data = json.loads(kept_group_data)
                except Exception:
                    continue
            if not isinstance(kept_group_data, dict):
                continue
            # 在新记录中找同主维度的分组进行补充
            candidates = new_groups_by_tag.get(tag, [])
            for new_group in candidates:
                if not isinstance(new_group, dict):
                    continue
                new_group_data = new_group.get("data")
                if isinstance(new_group_data, str):
                    try:
                        new_group_data = json.loads(new_group_data)
                    except Exception:
                        continue
                if not isinstance(new_group_data, dict):
                    continue
                if _supplement_data_obj(kept_group_data, new_group_data):
                    supplemented = True
                break  # 只取第一个匹配分组
            # 将修改后的数据写回分组，确保序列化时保留变更
            kept_group["data"] = kept_group_data

        if supplemented:
            if isinstance(kept_data_raw, str):
                kept_news['data'] = json.dumps(kept_loaded, ensure_ascii=False)
            else:
                kept_news['data'] = kept_loaded

        return supplemented

    def _is_same_day(self, news1: Dict, news2: Dict) -> bool:
        """
        判断两条新闻是否为同一天（基于日期字段前10位）
        """
        date_fields = ['publish_date', 'date', 'fetch_date', 'created_at', 'published_at']
        date1 = None
        date2 = None
        
        for field in date_fields:
            if field in news1 and news1[field]:
                date1 = str(news1[field])[:10]
                break
        for field in date_fields:
            if field in news2 and news2[field]:
                date2 = str(news2[field])[:10]
                break
        
        if not date1 or not date2:
            return False
        
        return date1 == date2
    
    def _extract_core_metrics(self, text: str) -> set:
        """
        提取新闻内容中的核心量化指标（数量、金额、吨位/箱位等），
        用于去重预筛选：同一天、多指标重叠的新闻高度可能是同事件的不同报道。
        """
        if not text:
            return set()
        text = str(text)
        metrics = set()
        # 数量：24艘、24条、34船、2+2艘
        for m in re.finditer(r'(\d+(?:\+\d+)?(?:\.\d+)?)\s*[艘条只船]', text):
            metrics.add(f"qty:{m.group(1)}")
        # 金额：86.56亿元、86亿、1.2亿美元、10亿元
        for m in re.finditer(r'(\d+(?:\.\d+)?)\s*[万亿]?(?:美|港)?元', text):
            metrics.add(f"amt:{m.group(1)}")
        # 吨位/箱位：8.7万吨、21万吨、24000TEU
        for m in re.finditer(r'(\d+(?:\.\d+)?)\s*[万吨TEUteu]', text):
            metrics.add(f"spec:{m.group(1)}")
        return metrics
    
    def _extract_main_dimensions(self, tags):
        """从 tags 字段中提取主维度标签列表。"""
        if not tags:
            return []
        try:
            if isinstance(tags, str):
                tags = json.loads(tags)
            if isinstance(tags, dict):
                return [t for t in tags.get('main_tags', []) if t]
        except Exception:
            pass
        return []
    
    async def check_entity_duplicate(self, entity1, entity2) -> Tuple[bool, str]:
        """
        使用AI判断两个entity是否指向同一主体
        
        Parameters:
            entity1: 第一个实体
            entity2: 第二个实体
            
        Returns:
            tuple: (是否相同, 理由)
        """
        try:
            # 快速精确匹配
            if str(entity1).strip().lower() == str(entity2).strip().lower():
                return True, "完全匹配"
            
            prompt = f"""判断以下两个实体名称是否指向同一主体（如同一家公司、同一艘船、同一组织、同一人物等）。
忽略大小写、缩写、中英文差异、以及常用修饰词（如"集团"、"公司"、"航运"等）的差异。

实体1: {entity1}
实体2: {entity2}

请返回JSON格式：
{{
    "is_same": true/false,
    "reason": "判断理由（15字以内）"
}}"""
            
            response = self.client.chat.completions.create(
                model="<your-model>",
                messages=[
                    {"role": "system", "content": "你是实体匹配专家，只返回JSON格式结果。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=100
            )
            
            result_text = response.choices[0].message.content.strip()
            try:
                result = json.loads(result_text)
            except json.JSONDecodeError:
                import re
                json_match = re.search(r'\{{[^}}]+\}}', result_text)
                if json_match:
                    result = json.loads(json_match.group())
                else:
                    raise ValueError(f"无法解析AI返回: {result_text}")
            
            is_same = result.get('is_same', False)
            reason = result.get('reason', '无')
            return is_same, reason
            
        except Exception as e:
            print(f"  AI entity检查失败: {str(e)[:50]}")
            return False, f"检查失败: {str(e)[:30]}"
    
    async def deduplicate_by_groups(self, news_list: List[Dict], similarity_threshold: float = 0.85) -> List[Dict]:
        """
        按分类分组进行去重，提高准确性
        
        Parameters:
        news_list (list): 新闻列表
        similarity_threshold (float): 去重相似度阈值，默认0.85
        
        Returns:
        list: 去重后的新闻列表
        """
        # 按主维度分组（取第一个主维度；无主维度则回退到旧 category）
        groups = {}
        for news in news_list:
            dims = self._extract_main_dimensions(news.get('tags', ''))
            group_key = dims[0] if dims else (news.get('category') or '未分类')
            if group_key not in groups:
                groups[group_key] = []
            groups[group_key].append(news)
        
        print(f"\n📊 按主维度分组去重，共 {len(groups)} 个主维度...")
        print(f"   相似度阈值: {similarity_threshold}")
        
        all_unique = []
        for group_key, news_in_group in groups.items():
            print(f"\n  处理主维度: {group_key} ({len(news_in_group)}条)")
            unique_in_group = await self.deduplicate_news_list(
                news_in_group, similarity_threshold=similarity_threshold
            )
            all_unique.extend(unique_in_group)
        
        print(f"\n✅ 分组去重完成，总计: {len(all_unique)} 条")
        return all_unique


async def ai_deduplicate_news(news_list: List[Dict], use_grouping: bool = True) -> List[Dict]:
    """
    使用AI进行新闻去重的便捷函数
    
    Parameters:
    news_list (list): 新闻列表
    use_grouping (bool): 是否按分类分组去重
    
    Returns:
    list: 去重后的新闻列表
    """
    if not news_list:
        return []
    
    engine = AIDeduplicateEngine(region="beijing")
    
    if use_grouping:
        return await engine.deduplicate_by_groups(news_list)
    else:
        return await engine.deduplicate_news_list(news_list)


def _expand_news_reports(news_item: Dict) -> List[Dict]:
    """
    将 news_report 字段解析为报告条目列表。

    兼容：
    - 新格式：JSON 数组 [{"main_tag": ..., "report": ...}, ...]
    - 旧格式：单条字符串报告

    返回列表元素包含：main_tag, report, entity, data
    """
    news_report = news_item.get('news_report', '')
    entity = news_item.get('entity', '')
    data = news_item.get('data', '')

    if not news_report:
        return []

    if isinstance(news_report, str):
        try:
            report_groups = json.loads(news_report)
        except Exception:
            # 旧格式：单条字符串报告
            return [{
                'main_tag': news_item.get('category') or '行业新闻',
                'report': news_report,
                'entity': entity,
                'data': data,
            }]
    else:
        report_groups = news_report

    if not isinstance(report_groups, list):
        return []

    expanded = []
    for group in report_groups:
        if not isinstance(group, dict):
            continue
        main_tag = group.get('main_tag') or news_item.get('category') or '行业新闻'
        report_text = group.get('report', '')
        if not report_text:
            continue
        expanded.append({
            'main_tag': main_tag,
            'report': report_text,
            'entity': entity,
            'data': data,
        })
    return expanded


def _safe_json_loads(value):
    """安全解析 JSON 字符串/对象。"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value or value.lower() in ("null", "none"):
            return None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _is_valid(value):
    """检查值是否有效。"""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() not in ("", "None", "null", "NULL", "nan", "NaN")
    if isinstance(value, (list, dict, set)):
        return len(value) > 0
    return True


# 主体字段分组：用于「两个主体 + 一个船型 + 一个交易类型」的去重判断
_PARTY_A_KEYS = ("shipowner", "shipowners", "buyer", "buyers", "charterer", "charterers")
_PARTY_B_KEYS = ("shipyard", "shipyards", "seller", "sellers", "owner", "owners")
_PARTY_OTHER_KEYS = ("company", "companies", "entities", "entity", "parties", "party", "subject")
_SHIP_TYPE_KEYS = ("ship_type", "ship_types")
_TRADE_TYPE_KEY = "trade_type"

# 签署订单与备选生效即使主体/船型完全相同，也视为不同交易
_TRADE_TYPE_DEDUP_EXCEPTION_PAIRS = {
    frozenset({"签署订单", "备选生效"}),
}


def _collect_string_values(obj, keys):
    """从 dict/list 中递归收集指定 key 的非空字符串值。"""
    values = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                if isinstance(v, str) and _is_valid(v):
                    values.add(v.strip())
                elif isinstance(v, (list, tuple)):
                    for item in v:
                        if isinstance(item, str) and _is_valid(item):
                            values.add(item.strip())
            elif isinstance(v, (dict, list)):
                values.update(_collect_string_values(v, keys))
    elif isinstance(obj, list):
        for item in obj:
            values.update(_collect_string_values(item, keys))
    return values


def _extract_trade_types(data):
    """从 data 中提取所有 trade_type，兼容旧数据 is_forecast_order。"""
    trade_types = set()
    if not isinstance(data, dict):
        return trade_types
    txs = data.get("transactions")
    candidates = txs if isinstance(txs, list) else [data]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        tt = str(item.get(_TRADE_TYPE_KEY, "")).strip()
        if tt in ("签署订单", "备选生效", "在手订单", "预测订单"):
            trade_types.add(tt)
        elif item.get("is_forecast_order") is True:
            trade_types.add("预测订单")
    return trade_types


def _extract_report_keys(item: Dict) -> Dict[str, set]:
    """
    从报道条目（含 entity/data）中提取去重所需的维度：
    - party_a: 第一主体（订船方/买方/承租方等）
    - party_b: 第二主体（造船方/卖方/出租方等）
    - ship_types: 船型
    - trade_types: 交易类型（新增）

    兼容：
    - 旧格式：entity/data 为直接对象/数组
    - 新格式：entity/data 为分组数组 [{main_tag, entity/data}]
    """
    entity = _safe_json_loads(item.get("entity", ""))
    data = _safe_json_loads(item.get("data", ""))

    # 统一解包为可直接遍历的数据源列表
    sources = []
    for source in (entity, data):
        if source is None:
            continue
        if isinstance(source, list):
            # 分组格式 [{main_tag, entity/data}] 或旧数组格式
            for group in source:
                if isinstance(group, dict) and 'main_tag' in group:
                    inner = group.get('entity') or group.get('data')
                    if isinstance(inner, str):
                        inner = _safe_json_loads(inner)
                    if isinstance(inner, (dict, list)):
                        sources.append(inner)
                elif isinstance(group, (dict, list)):
                    sources.append(group)
        elif isinstance(source, (dict, list)):
            sources.append(source)

    party_a = set()
    party_b = set()
    ship_types = set()
    trade_types = set()

    for source in sources:
        party_a.update(_collect_string_values(source, _PARTY_A_KEYS))
        party_b.update(_collect_string_values(source, _PARTY_B_KEYS))
        ship_types.update(_collect_string_values(source, _SHIP_TYPE_KEYS))

    # 从 data 中提取 trade_type（兼容分组格式）
    if isinstance(data, dict):
        trade_types.update(_extract_trade_types(data))
        inner_data = data.get("data")
        if isinstance(inner_data, (dict, list)):
            for sub in (inner_data if isinstance(inner_data, list) else [inner_data]):
                if isinstance(sub, dict):
                    trade_types.update(_extract_trade_types(sub))
    elif isinstance(data, list):
        for group in data:
            if isinstance(group, dict):
                trade_types.update(_extract_trade_types(group))
                inner = group.get("data")
                if isinstance(inner, str):
                    inner = _safe_json_loads(inner)
                if isinstance(inner, (dict, list)):
                    for sub in (inner if isinstance(inner, list) else [inner]):
                        if isinstance(sub, dict):
                            trade_types.update(_extract_trade_types(sub))

    # 兜底：若第一/第二主体为空，但存在其它主体字段，放入对应桶以提升召回
    if not party_a:
        other = _collect_string_values(data, _PARTY_OTHER_KEYS) or _collect_string_values(entity, _PARTY_OTHER_KEYS)
        if other:
            party_a.update(other)

    return {
        "party_a": party_a,
        "party_b": party_b,
        "ship_types": ship_types,
        "trade_types": trade_types,
    }


def _reports_match_by_keys(keys1: Dict[str, set], keys2: Dict[str, set]) -> bool:
    """
    判断两篇报道是否重复：两个主体维度 + 一个船型维度 + 一个交易类型维度，
    现在要求四个维度全部相同才视为重复交易。
    例外：签署订单与备选生效即使其它维度完全相同，也视为不同交易。

    新增：主体比较时剔除宽泛实体（如“欧洲船东”、“中国船厂”），仅比较具体实体。
    """
    # 主体与船型必须全部匹配；比较主体时过滤宽泛实体
    a1 = _filter_broad_entities(keys1["party_a"])
    a2 = _filter_broad_entities(keys2["party_a"])
    b1 = _filter_broad_entities(keys1["party_b"])
    b2 = _filter_broad_entities(keys2["party_b"])
    if not (a1 and a2 and (a1 & a2)):
        return False
    if not (b1 and b2 and (b1 & b2)):
        return False
    if not (keys1["ship_types"] and keys2["ship_types"] and _ship_types_overlap(keys1["ship_types"], keys2["ship_types"])):
        return False

    # 交易类型必须匹配；若任一为空，视为不匹配，避免旧数据误杀新数据
    trade_types_1 = keys1.get("trade_types", set())
    trade_types_2 = keys2.get("trade_types", set())
    if not trade_types_1 or not trade_types_2:
        return False
    common_trade_types = trade_types_1 & trade_types_2
    if not common_trade_types:
        return False

    # 例外：若共同交易类型仅由签署订单与备选生效组成，视为不同交易
    if common_trade_types in _TRADE_TYPE_DEDUP_EXCEPTION_PAIRS:
        return False

    return True


def _extract_parties_from_data(news: Dict) -> set:
    """
    从新闻的 data 字段中提取所有主体名称（用于前置 AI 去重的快速通道）。
    """
    data = _safe_json_loads(news.get("data", ""))
    if data is None:
        return set()
    all_party_keys = _PARTY_A_KEYS + _PARTY_B_KEYS + _PARTY_OTHER_KEYS
    return _collect_string_values(data, all_party_keys)


def _extract_transaction_keys(item: Dict) -> Dict[str, set]:
    """
    从报道条目中提取交易类去重预过滤所需的关键要素：
    - parties: 船东/船厂/买方/卖方/承租方/出租方/公司等主体
    - ship_types: 船型
    - quantities: 数量

    优先解析 entity/data 字段；解析失败或为空时，fallback 到 report_text + title 的文本匹配。
    """
    parties = set()
    ship_types = set()
    quantities = set()

    # 1) 从 entity 提取主体
    entity = item.get('entity', '')
    entity_parsed = _safe_json_loads(entity)
    if isinstance(entity_parsed, dict):
        entity_parsed = [entity_parsed]
    if isinstance(entity_parsed, list):
        party_keys = ('shipowner', 'shipyard', 'buyer', 'seller', 'charterer', 'owner', 'company', 'companies')
        for e in entity_parsed:
            if not isinstance(e, dict):
                continue
            for key in party_keys:
                val = e.get(key)
                if not _is_valid(val):
                    continue
                if isinstance(val, list):
                    for v in val:
                        if _is_valid(v):
                            parties.add(str(v).strip())
                else:
                    parties.add(str(val).strip())

    # 2) 从 data 提取船型和数量
    data = item.get('data', '')
    data_parsed = _safe_json_loads(data)
    transactions = []
    if isinstance(data_parsed, dict):
        if "transactions" in data_parsed and isinstance(data_parsed["transactions"], list):
            transactions = [t for t in data_parsed["transactions"] if isinstance(t, dict)]
        else:
            transactions = [data_parsed]
    elif isinstance(data_parsed, list):
        transactions = [t for t in data_parsed if isinstance(t, dict)]

    for tx in transactions:
        st = tx.get('ship_type')
        if _is_valid(st):
            ship_types.add(str(st).strip())
        qty = tx.get('quantity')
        if _is_valid(qty):
            quantities.add(str(qty).strip())
        # 汇总型 all_quantity 也作为数量候选
        all_qty = tx.get('all_quantity') if isinstance(tx, dict) else None
        if _is_valid(all_qty):
            quantities.add(str(all_qty).strip())

    # 3) 兜底：从 report_text + title 中尝试提取
    text = str(item.get('report_text', '')) + " " + str(item.get('title', ''))
    if text.strip():
        # 主体兜底：提取动作动词前的名称作为候选主体
        if not parties:
            import re
            # 匹配：主体 + （可选副词） + 动作动词（获/接/订购/下单/交付/订造/承接）
            m = re.search(r'^([\u4e00-\u9fa5A-Za-z0-9（）()]{2,20}?)(?:再|又|将|已|均|共)?(?:获|接|订购|下单|交付|订造|承接|获签)', text)
            if m:
                candidate = m.group(1).strip("、，, ")
                if candidate and len(candidate) >= 2:
                    parties.add(candidate)
        # 常见船型关键词兜底
        if not ship_types:
            common_ship_types = [
                "集装箱船", "散货船", "油轮", "原油船", "成品油轮", "化学品船",
                "LNG运输船", "LPG运输船", "液化气运输船", "VLCC", "VLGC",
                "汽车运输船", "滚装船", "多用途船", "海工船", "FPSO", "海工平台"
            ]
            for st in common_ship_types:
                if st in text:
                    ship_types.add(st)
        # 数量兜底：X艘、X+Y艘
        if not quantities:
            import re
            qty_matches = re.findall(r'(\d+(?:\+\d+)?)\s*艘', text)
            for q in qty_matches:
                quantities.add(q)

    return {
        "parties": parties,
        "ship_types": ship_types,
        "quantities": quantities,
    }


def _has_transaction_overlap(item1: Dict, item2: Dict) -> bool:
    """
    判断两条交易类报道是否满足进入 AI 去重的最小重叠条件：
    - 主体（parties）至少有一个重叠，且
    - 船型（ship_types）或数量（quantities）至少有一个重叠

    该预过滤用于减少把“仅船型相同但主体事件不同”的新闻误判为重复。
    """
    keys1 = _extract_transaction_keys(item1)
    keys2 = _extract_transaction_keys(item2)

    party_overlap = bool(keys1["parties"] and keys2["parties"] and (keys1["parties"] & keys2["parties"]))
    ship_type_overlap = bool(keys1["ship_types"] and keys2["ship_types"] and _ship_types_overlap(keys1["ship_types"], keys2["ship_types"]))
    quantity_overlap = bool(keys1["quantities"] and keys2["quantities"] and (keys1["quantities"] & keys2["quantities"]))

    return party_overlap and (ship_type_overlap or quantity_overlap)


async def _batch_check_duplicate_pairs(
    engine: AIDeduplicateEngine,
    new_reports: List[Dict],
    existing_reports: List[Dict],
    similarity_threshold: float,
    direction: str,
) -> List[Tuple[int, int]]:
    """
    批量判断重复报告对。

    Parameters:
        engine: AIDeduplicateEngine 实例
        new_reports: 新报道列表，每个元素包含 report_text
        existing_reports: 历史报道列表（direction="external" 时使用）
        similarity_threshold: 置信度阈值（0-1）
        direction: "internal"（新报道之间）或 "external"（新报道 vs 历史报道）

    Returns:
        重复对列表。
        - internal: [(index1, index2), ...]，索引对应 new_reports 下标
        - external: [(new_index, existing_index), ...]，索引分别对应传入的两个列表下标
    """
    if not new_reports:
        return []
    if direction == "external" and not existing_reports:
        return []
    if direction == "internal" and len(new_reports) < 2:
        return []

    # 外部去重时，先用标题相似度快速筛选，只保留可能与任意新报道重复的历史报道
    if direction == "external":
        filtered_existing = []
        for ei, e in enumerate(existing_reports):
            if any(title_similarity(r['report_text'], e['report_text']) >= 0.3 for r in new_reports):
                filtered_existing.append(e)
        existing_reports = filtered_existing
        if not existing_reports:
            return []

    MAX_REPORT_LEN = 500
    lines = [
        "【演示版：批量去重判定指令已删减】请判断以下报道之间是否报道了同一事件。",
        "",
        "注意：只需返回你确信重复的配对，未列出的配对默认不重复。",
    ]

    lines.append("\n【新报道】")
    for i, r in enumerate(new_reports):
        text = str(r['report_text'])[:MAX_REPORT_LEN].replace('\n', ' ')
        lines.append(f"N{i}: {text}")

    if direction == "external":
        lines.append("\n【历史报道】")
        for i, e in enumerate(existing_reports):
            text = str(e['report_text'])[:MAX_REPORT_LEN].replace('\n', ' ')
            lines.append(f"H{i}: {text}")
        lines.append("\n请返回JSON格式，列出所有新报道与历史报道的重复对（new_index为新报道编号，existing_index为历史报道编号）：")
        lines.append('{\"duplicates\": [{\"new_index\": 0, \"existing_index\": 1, \"confidence\": 90, \"reason\": \"同一订单\"}, ...]}')
    else:
        lines.append("\n请返回JSON格式，列出所有新报道之间的重复对（index1/index2均为新报道编号）：")
        lines.append('{\"duplicates\": [{\"index1\": 0, \"index2\": 1, \"confidence\": 90, \"reason\": \"同一事件\"}, ...]}')

    prompt = "\n".join(lines)

    try:
        response = engine.client.chat.completions.create(
            model="<your-model>",
            messages=[
                {"role": "system", "content": "你是专业的新闻报告去重分析专家，只返回JSON格式结果。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=600,
        )
        result_text = response.choices[0].message.content.strip()
        try:
            result = json.loads(result_text)
        except json.JSONDecodeError:
            import re
            json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                raise ValueError(f"无法解析AI返回: {result_text}")

        duplicates = result.get('duplicates', [])
        threshold = similarity_threshold * 100
        pairs = []
        for dup in duplicates:
            conf = float(dup.get('confidence', 0))
            if conf < threshold:
                continue
            if direction == "external":
                ni = dup.get('new_index')
                ei = dup.get('existing_index')
                if isinstance(ni, int) and isinstance(ei, int):
                    pairs.append((ni, ei))
            else:
                i1 = dup.get('index1')
                i2 = dup.get('index2')
                if isinstance(i1, int) and isinstance(i2, int):
                    pairs.append((i1, i2))
        return pairs
    except Exception as e:
        print(f"  AI批量报告去重检查失败: {str(e)[:80]}")
        return []


def _mark_report_duplicate(news_item: Dict, group_index: int) -> None:
    """
    将 news_report 数组中指定索引的报道标记为重复（is_duplicate=true）。
    兼容数组格式与旧单条字符串格式。
    """
    news_report = news_item.get('news_report', '')
    if not news_report:
        return

    report_groups = _safe_json_loads(news_report)
    if isinstance(report_groups, list):
        if 0 <= group_index < len(report_groups):
            report_groups[group_index]['is_duplicate'] = True
            news_item['news_report'] = json.dumps(report_groups, ensure_ascii=False)
    elif isinstance(report_groups, str) or (report_groups is None and isinstance(news_report, str)):
        # 旧格式：单条字符串报告
        main_tag = news_item.get('category') or '行业新闻'
        news_item['news_report'] = json.dumps(
            [{'main_tag': main_tag, 'report': str(news_report), 'is_duplicate': True}],
            ensure_ascii=False
        )


def _mark_transactions_duplicate(news_item: Dict, main_tag: str) -> None:
    """
    将 data 字段中对应 main_tag 的交易分组下的每笔交易标记为重复（is_duplicate=true）。
    兼容 data 分组数组格式 [{main_tag, data}]、旧直接交易数组格式 [{...}] 与旧单对象格式。

    注意：is_duplicate 必须属于单笔交易/记录，不能写在包含 all_quantity/all_price 的汇总容器上。
    """
    data = news_item.get('data', '')
    if not data:
        return

    def _mark_transactions(obj):
        """把 is_duplicate 写到真正的交易/记录上，而不是汇总容器上。"""
        if isinstance(obj, dict):
            txs = obj.get('transactions')
            if isinstance(txs, list):
                # 多笔交易：只标记 transactions 里的每一条，不标记外层容器
                for tx in txs:
                    if isinstance(tx, dict):
                        tx['is_duplicate'] = True
                # 如果旧数据在外层容器遗留了 is_duplicate，清理掉
                obj.pop('is_duplicate', None)
                return
            # 单条记录/平铺字典：直接标记该记录
            obj['is_duplicate'] = True
        elif isinstance(obj, list):
            for item in obj:
                _mark_transactions(item)

    data_groups = _safe_json_loads(data)
    modified = False

    if isinstance(data_groups, list):
        # 判断是否为分组格式 [{main_tag, data}]
        is_group_format = any(
            isinstance(g, dict) and 'main_tag' in g for g in data_groups
        )

        if is_group_format:
            for group in data_groups:
                if not isinstance(group, dict):
                    continue
                if group.get('main_tag') != main_tag:
                    continue
                inner_data = group.get('data')
                if isinstance(inner_data, str):
                    try:
                        inner_data = json.loads(inner_data)
                    except (json.JSONDecodeError, TypeError):
                        continue
                _mark_transactions(inner_data)
                group['data'] = json.dumps(inner_data, ensure_ascii=False)
                modified = True
                break
        else:
            # 旧直接交易数组格式：无法精确映射 main_tag，标记全部交易
            _mark_transactions(data_groups)
            modified = True
    elif isinstance(data_groups, dict):
        if data_groups.get('main_tag') == main_tag:
            inner_data = data_groups.get('data')
            if isinstance(inner_data, str):
                try:
                    inner_data = json.loads(inner_data)
                except (json.JSONDecodeError, TypeError):
                    return
            _mark_transactions(inner_data)
            data_groups['data'] = json.dumps(inner_data, ensure_ascii=False)
            modified = True

    if modified:
        news_item['data'] = json.dumps(data_groups, ensure_ascii=False)


async def ai_deduplicate_news_reports(
    news_list: List[Dict],
    existing_reports: Optional[List[Dict]] = None,
    similarity_threshold: float = 0.85,
    log_callback: Optional[Callable[[str], None]] = None,
) -> List[Dict]:
    """
    基于生成的新闻报告（news_report）进行去重。

    去重维度：
    1. 同时处理的批次内报告（same batch）
    2. 数据库中近三天内相同主维度（main_tag）的历史报告

    优化：同一主维度下的所有新报道与历史报道一次性批量送入 AI 判断，
    不再逐条两两调用，减少 API 调用与 token 消耗；不同主维度之间不比较。

    当某个报告被判定为重复时，会在 news_report 的对应条目以及 data 中对应
    main_tag 的所有交易上设置 is_duplicate=true；原报道和交易不再被删除。

    Parameters:
        news_list: 待去重的新闻列表（要求已生成 news_report）
        existing_reports: 历史报告条目列表，每个条目至少包含 main_tag、report_text
        similarity_threshold: AI 判断重复的置信度阈值（0-1）
        log_callback: 日志回调

    Returns:
        List[Dict]: 标记后的新闻列表（条数与输入一致）
    """
    if not news_list:
        return []

    existing_reports = existing_reports or []

    def _log(msg: str):
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                print(msg)
        else:
            print(msg)

    # 展开所有新报道并分配稳定ID，同时保留 entity/data 用于预过滤
    new_reports = []
    for ni, news in enumerate(news_list):
        groups = _expand_news_reports(news)
        for gi, g in enumerate(groups):
            new_reports.append({
                'rid': len(new_reports),
                'news_index': ni,
                'group_index': gi,
                'main_tag': g['main_tag'],
                'report_text': g['report'],
                'entity': news.get('entity', ''),
                'data': news.get('data', ''),
            })

    # 按主维度分组
    new_by_tag: Dict[str, List[Dict]] = {}
    for r in new_reports:
        new_by_tag.setdefault(r['main_tag'], []).append(r)

    existing_by_tag: Dict[str, List[Dict]] = {}
    for e in existing_reports:
        existing_by_tag.setdefault(e['main_tag'], []).append(e)

    duplicate_rids = set()
    duplicate_count = 0

    _log(f"\n🔍 开始按主维度批量进行新闻报告去重，共 {len(news_list)} 条原新闻，"
         f"{len(new_reports)} 个报告，{len(set(existing_by_tag.keys()) & set(new_by_tag.keys()))} 个待比较主维度...")

    # 行业新闻不参与报道级去重
    SKIP_MAIN_TAGS = {"行业新闻"}

    for tag, tag_new_reports in new_by_tag.items():
        if tag in SKIP_MAIN_TAGS:
            _log(f"  主维度【{tag}】：跳过报道级去重")
            continue

        tag_existing = existing_by_tag.get(tag, [])
        _log(f"  主维度【{tag}】：{len(tag_new_reports)} 条新报告，{len(tag_existing)} 条历史报告")

        # 预计算新报道的去重键
        new_keys = [_extract_report_keys(r) for r in tag_new_reports]

        # 1) 新报道 vs 历史报道（字符串匹配，无需 AI）
        if tag_existing:
            for ei, e in enumerate(tag_existing):
                existing_keys = _extract_report_keys(e)
                for ni, r_keys in enumerate(new_keys):
                    if tag_new_reports[ni]['rid'] in duplicate_rids:
                        continue
                    if _reports_match_by_keys(r_keys, existing_keys):
                        dup = tag_new_reports[ni]
                        duplicate_rids.add(dup['rid'])
                        duplicate_count += 1
                        _log(f"    ⚠️ 与历史报告重复（{tag}）: N{ni} vs H{ei}")

        # 2) 新报道之间互相去重（字符串匹配，无需 AI）
        if len(tag_new_reports) >= 2:
            for i in range(len(tag_new_reports)):
                if tag_new_reports[i]['rid'] in duplicate_rids:
                    continue
                for j in range(i + 1, len(tag_new_reports)):
                    if tag_new_reports[j]['rid'] in duplicate_rids:
                        continue
                    if _reports_match_by_keys(new_keys[i], new_keys[j]):
                        # 保留先出现的，将后出现的标记为重复
                        drop_rid = tag_new_reports[j]['rid']
                        duplicate_rids.add(drop_rid)
                        duplicate_count += 1
                        _log(f"    ⚠️ 批次内重复（{tag}）: N{i} vs N{j}")

    # 标记重复：保留所有新闻和报道，仅设置 is_duplicate 字段
    for r in new_reports:
        if r['rid'] in duplicate_rids:
            news_item = news_list[r['news_index']]
            _mark_report_duplicate(news_item, r['group_index'])
            _mark_transactions_duplicate(news_item, r['main_tag'])

    _log(f"\n✅ 报告去重完成: {len(news_list)} 条原新闻保持不变，"
         f"标记 {duplicate_count} 个重复报告及对应交易")
    return news_list


# 测试
if __name__ == "__main__":
    # 测试数据
    test_news = [
        {
            "title": "Maersk orders 10 new container vessels",
            "summary": "Maersk has placed an order for 10 new container vessels with a total value of $1.2 billion.",
            "source_platform": "Splash247",
            "category": "新造船订单"
        },
        {
            "title": "Maersk places order for 10 container ships",
            "summary": "Danish shipping giant Maersk announced an order for 10 new container vessels worth $1.2B.",
            "source_platform": "TradeWinds",
            "category": "新造船订单"
        },
    ]
    
    print("="*60)
    print("AI去重引擎测试")
    print("="*60)
    
    result = asyncio.run(ai_deduplicate_news(test_news, use_grouping=False))
    
    print("\n去重结果:")
    for i, news in enumerate(result, 1):
        print(f"{i}. {news['title']}")
        print(f"   来源: {news.get('source_platform')}")
        print(f"   重复次数: {news.get('duplicate_count', 1)}")
