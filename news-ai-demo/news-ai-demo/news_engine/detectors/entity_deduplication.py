# -*- coding: utf-8 -*-
"""
实体归一化/查重模块（大模型匹配版）

功能：
1. 为每种实体类型维护一个标准名称表（canonical names），词表来自
   /path/to/normalized_entity_vocabulary.json（只读，不再写入）。
2. 当新新闻提取到某个实体时：
   - 先进行精确/别名匹配；
   - 再进行子串包含匹配；
   - 仍无命中时，调用大模型判断是否与已有标准名指向同一主体；
   - 命中则替换为标准名，未命中则原样返回（词表为只读，不允许注册新实体）。
3. 不使用 difflib/编辑距离等模糊匹配作为最终决策依据，仅用于从大表中快速召回候选。

数据存储：/path/to/normalized_entity_vocabulary.json（只读 JSON 词表）
条目格式: {canonical_name, entity_types[], english_name, aliases[], frequency, code}
"""

import os
import json
import re
import difflib
import asyncio
from typing import Optional


# 实体词表：统一使用规范词表 JSON（只读，可用环境变量覆盖）。
ENTITY_VOCAB_PATH = os.getenv(
    "ENTITY_VOCABULARY_PATH",
    "/path/to/normalized_entity_vocabulary.json",
)


class EntityRegistry:
    """实体注册表，支持按类型归一化和查重"""

    def __init__(self, db_path=None):
        # db_path 参数仅保留兼容（词表已改为只读 JSON，不再使用 SQLite）。
        self.db_path = db_path or ENTITY_VOCAB_PATH
        self._analyzer = None
        self._llm_semaphore = asyncio.Semaphore(5)
        self._vocab_loaded = False
        self._entities_by_type = {}

    def _analyzer_instance(self):
        if self._analyzer is None:
            from ai_engine import BailianWebAnalyzer
            self._analyzer = BailianWebAnalyzer(region="beijing")
        return self._analyzer

    def _load_vocab(self):
        """从实体词表 JSON 加载全部条目，并按 entity_type 展开索引（只读，只做一次）。"""
        if self._vocab_loaded:
            return
        try:
            with open(ENTITY_VOCAB_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"   ⚠️ 实体词表加载失败 [{ENTITY_VOCAB_PATH}]: {e}")
            self._vocab_loaded = True
            return
        items = data if isinstance(data, list) else data.get("entries", [])
        for item in items:
            canonical = str(item.get("canonical_name") or "").strip()
            if not canonical:
                continue
            aliases = item.get("aliases") or []
            if isinstance(aliases, str):
                try:
                    aliases = json.loads(aliases)
                except (json.JSONDecodeError, TypeError):
                    aliases = []
            aliases = [str(a).strip() for a in aliases if str(a).strip()]
            # 新词表一个条目可属于多个类型（entity_types 列表）
            entity_types = item.get("entity_types") or [item.get("entity_type")] or []
            for et in entity_types:
                et = str(et or "").strip()
                if not et:
                    continue
                self._entities_by_type.setdefault(et, []).append({
                    "canonical_name": canonical,
                    "english_name": item.get("english_name") or "",
                    "aliases": aliases,
                    "frequency": item.get("frequency") or 1,
                })
        self._vocab_loaded = True

    def _load_entities_by_type(self, entity_type):
        """加载某类型的所有标准名称（含专用英文字段），来自只读 JSON 词表。"""
        self._load_vocab()
        return self._entities_by_type.get(entity_type, [])

    def _exact_or_alias_match(self, raw, entities):
        """精确匹配 canonical 或 alias"""
        for ent in entities:
            if raw == ent["canonical_name"]:
                return ent["canonical_name"]
            for alias in ent["aliases"]:
                if raw == alias:
                    return ent["canonical_name"]
        return None

    def _substring_match(self, raw, entities):
        """强子串匹配：raw 被 canonical 包含，或 canonical 被 raw 包含"""
        best = None
        best_score = 0
        for ent in entities:
            canonical = ent["canonical_name"]
            # raw 被 canonical 包含（如 "外高桥" 在 "外高桥造船" 中）
            if raw in canonical and len(raw) >= 3:
                score = 90 + len(raw) / len(canonical) * 10
                if score > best_score:
                    best_score = score
                    best = canonical
            # canonical 被 raw 包含
            elif canonical in raw and len(canonical) >= 3:
                score = 80 + len(canonical) / len(raw) * 10
                if score > best_score:
                    best_score = score
                    best = canonical
        return best

    def _recall_candidates(self, raw, entities, top_k=5, min_ratio=0.35):
        """
        使用 difflib 仅做候选召回，不做最终决策。
        返回按相似度排序的候选标准名列表。
        """
        raw_lower = raw.lower()
        scored = []
        for ent in entities:
            canonical = ent["canonical_name"]
            ratio = difflib.SequenceMatcher(None, raw_lower, canonical.lower()).ratio()
            if ratio >= min_ratio:
                scored.append((ratio, canonical))
            for alias in ent["aliases"]:
                ratio = difflib.SequenceMatcher(None, raw_lower, alias.lower()).ratio()
                if ratio >= min_ratio:
                    scored.append((ratio, canonical))

        scored.sort(key=lambda x: x[0], reverse=True)
        seen = set()
        candidates = []
        for _, name in scored:
            if name not in seen:
                seen.add(name)
                candidates.append(name)
            if len(candidates) >= top_k:
                break
        return candidates

    async def _llm_match(self, entity_type, raw_name, candidates):
        """
        调用大模型判断 raw_name 是否与候选标准名中的某一个指向同一主体。
        返回命中的 canonical_name，未命中返回 None。
        """
        if not candidates:
            return None

        analyzer = self._analyzer_instance()
        candidates_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(candidates))

        prompt = f"""你是一名航运实体标准化专家。请判断以下“新名称”是否与某个“已有标准名”指向同一主体（同一公司、船厂、船东、组织、港口、船级社等）。

判断规则：
- 忽略大小写、中英文差异、缩写、以及“集团/公司/航运/造船/重工/船业/企业/能源/运输/港口/管理局/船级社”等通用修饰词差异。
- 只有确实指向同一主体时才返回对应标准名；如果都不匹配，必须返回“无”。
- 不要返回相似但不相同的主体名称。

实体类型：{entity_type}
新名称：{raw_name}

候选标准名：
{candidates_text}

请只返回以下 JSON 格式，不要任何解释、前言或 markdown 代码块：
{{"match": "命中标准名 或 无", "reason": "判断理由（15字以内）"}}"""

        async with self._llm_semaphore:
            try:
                result = await analyzer.analyze(
                    content=prompt,
                    prompt="判断新名称与候选标准名是否指向同一主体，返回JSON",
                    max_tokens=100,
                    model="<your-model>"
                )

                if not result.get("success"):
                    print(f"   ⚠️ 实体LLM匹配失败: {result.get('error', '未知错误')}")
                    return None

                text = result.get("analysis", "").strip()
                # 去除可能的 markdown 代码块
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)

                try:
                    parsed = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    # 尝试用正则提取 JSON
                    match = re.search(r"\{[^}]+\}", text)
                    if not match:
                        return None
                    parsed = json.loads(match.group(0))

                match_name = str(parsed.get("match", "无")).strip()
                if match_name and match_name != "无" and match_name in candidates:
                    print(f"   🤖 LLM实体匹配: [{entity_type}] {raw_name} -> {match_name}")
                    return match_name
                return None

            except Exception as e:
                print(f"   ⚠️ 实体LLM匹配异常: {str(e)[:80]}")
                return None

    # 类型回退：当特定类型未命中时，在保留的词表类型中继续查找
    # 词表仅保留 5 大主标签实体字段：shipowner, shipyard, buyer, seller,
    # charterer, owner, company, parties
    _FALLBACK_TYPES = {
        "shipowner": ["company"],
        "shipyard": ["company"],
        "buyer": ["company", "shipowner"],
        "seller": ["company", "shipowner"],
        "charterer": ["company", "shipowner"],
        "owner": ["company", "shipowner"],
        "company": ["shipowner"],
        "parties": ["company"],
    }

    async def _find_in_type(self, entity_type, raw):
        """在单一类型中查找标准名"""
        entities = self._load_entities_by_type(entity_type)
        if not entities:
            return None

        exact = self._exact_or_alias_match(raw, entities)
        if exact:
            return exact

        sub = self._substring_match(raw, entities)
        if sub:
            return sub

        candidates = self._recall_candidates(raw, entities)
        if candidates:
            llm_match = await self._llm_match(entity_type, raw, candidates)
            if llm_match:
                return llm_match
        return None

    async def find_canonical_async(self, entity_type, raw_name):
        """
        查找 raw_name 对应的标准名称。
        1. 在指定类型中精确/别名/子串/LLM 匹配
        2. 未命中则在相关回退类型中继续查找
        返回 canonical_name 或 None
        """
        if not raw_name or not isinstance(raw_name, str):
            return None

        raw = raw_name.strip()
        if not raw or len(raw) < 2:
            return None

        # 1. 指定类型
        result = await self._find_in_type(entity_type, raw)
        if result:
            return result

        # 2. 回退到相关类型（解决 vocabulary 类型与 prompt key 不完全对齐的问题）
        for fallback_type in self._FALLBACK_TYPES.get(entity_type, []):
            result = await self._find_in_type(fallback_type, raw)
            if result:
                return result

        return None

    async def normalize_name_async(self, entity_type, raw_name):
        """
        将 raw_name 归一化为标准名称。
        命中则返回标准名；未命中则原样返回（词表为只读，不允许注册/更新）。
        """
        if not raw_name or not isinstance(raw_name, str):
            return raw_name

        raw = raw_name.strip()
        if not raw or len(raw) < 2:
            return raw

        canonical = await self.find_canonical_async(entity_type, raw)
        if canonical:
            # 词表为只读：只做检测归一化，不更新频率、不添加别名。
            return canonical

        # 词表为只读：未命中的实体原样返回，不再注册进词表。
        return raw

    async def normalize_entity_json_async(self, entity_json):
        """
        递归处理 entity JSON（字符串或dict/list），将其中的字符串值归一化。
        """
        # 词表仅保留 5 大主标签对应的实体字段：
        # 新造船订单/船舶建造：shipowner, shipyard
        # 买卖船：buyer, seller
        # 租船：charterer, owner
        # 公司金融：company, parties
        KEY_TO_TYPE = {
            "shipowner": "shipowner",
            "shipowners": "shipowner",
            "shipyard": "shipyard",
            "shipyards": "shipyard",
            "buyer": "buyer",
            "buyers": "buyer",
            "seller": "seller",
            "sellers": "seller",
            "charterer": "charterer",
            "charterers": "charterer",
            "owner": "owner",
            "owners": "owner",
            "company": "company",
            "companies": "company",
            "parties": "parties",
            "party": "parties",
        }

        if isinstance(entity_json, str):
            try:
                parsed = json.loads(entity_json)
                normalized = await self.normalize_entity_json_async(parsed)
                return json.dumps(normalized, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                return entity_json

        if isinstance(entity_json, dict):
            result = {}
            for key, value in entity_json.items():
                key_lower = str(key).lower()
                # 若是分组格式中的 entity 字段（字符串 JSON），递归处理其内部结构
                if key_lower in ("entity", "entities") and isinstance(value, str):
                    try:
                        inner = json.loads(value)
                        result[key] = json.dumps(
                            await self.normalize_entity_json_async(inner),
                            ensure_ascii=False,
                        )
                    except (json.JSONDecodeError, TypeError):
                        result[key] = value
                    continue

                entity_type = KEY_TO_TYPE.get(key)
                if entity_type is None:
                    # 非词表字段：保留原值，仅递归处理嵌套结构
                    if isinstance(value, dict):
                        result[key] = await self.normalize_entity_json_async(value)
                    elif isinstance(value, list):
                        result[key] = [await self.normalize_entity_json_async(item) for item in value]
                    else:
                        result[key] = value
                    continue
                if isinstance(value, list):
                    result[key] = [
                        await self.normalize_name_async(entity_type, v)
                        if isinstance(v, str) else v
                        for v in value
                    ]
                elif isinstance(value, str):
                    result[key] = await self.normalize_name_async(entity_type, value)
                elif isinstance(value, dict):
                    result[key] = await self.normalize_entity_json_async(value)
                else:
                    result[key] = value
            return result

        if isinstance(entity_json, list):
            return [await self.normalize_entity_json_async(item) for item in entity_json]

        return entity_json


    async def normalize_data_json_async(self, data_json):
        """
        递归处理 data JSON（字符串或 dict/list），对 transactions 中的主体字段做查重归一化。
        支持分组格式 [{"main_tag": ..., "data": ...}] 以及 transactions 数组/平铺字典。
        """
        # 词表仅保留 5 大主标签对应的实体字段：
        # 新造船订单/船舶建造/维修改装：shipowner, shipyard（facility 视为 shipyard）
        # 买卖船：buyer, seller
        # 租船：charterer, owner
        # 公司金融：company, parties
        KEY_TO_TYPE = {
            "shipowner": "shipowner",
            "shipowners": "shipowner",
            "shipyard": "shipyard",
            "shipyards": "shipyard",
            "buyer": "buyer",
            "buyers": "buyer",
            "seller": "seller",
            "sellers": "seller",
            "charterer": "charterer",
            "charterers": "charterer",
            "owner": "owner",
            "owners": "owner",
            "facility": "shipyard",
            "facilities": "shipyard",
            "company": "company",
            "companies": "company",
            "parties": "parties",
            "party": "parties",
        }

        if isinstance(data_json, str):
            try:
                parsed = json.loads(data_json)
                normalized = await self.normalize_data_json_async(parsed)
                return json.dumps(normalized, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                return data_json

        if isinstance(data_json, dict):
            result = {}
            for key, value in data_json.items():
                key_lower = str(key).lower()
                # 若是分组格式中的 data 字段（字符串 JSON），递归处理
                if key_lower == "data" and isinstance(value, str):
                    try:
                        inner = json.loads(value)
                        result[key] = json.dumps(
                            await self.normalize_data_json_async(inner),
                            ensure_ascii=False,
                        )
                    except (json.JSONDecodeError, TypeError):
                        result[key] = value
                # 对 transactions 数组递归处理
                elif key_lower == "transactions" and isinstance(value, list):
                    result[key] = [await self.normalize_data_json_async(item) for item in value]
                elif key_lower in KEY_TO_TYPE and isinstance(value, str):
                    entity_type = KEY_TO_TYPE[key_lower]
                    result[key] = await self.normalize_name_async(entity_type, value)
                elif key_lower in KEY_TO_TYPE and isinstance(value, list):
                    entity_type = KEY_TO_TYPE[key_lower]
                    result[key] = [
                        await self.normalize_name_async(entity_type, v)
                        if isinstance(v, str) else v
                        for v in value
                    ]
                elif isinstance(value, dict):
                    result[key] = await self.normalize_data_json_async(value)
                elif isinstance(value, list):
                    result[key] = [await self.normalize_data_json_async(item) for item in value]
                else:
                    result[key] = value
            return result

        if isinstance(data_json, list):
            return [await self.normalize_data_json_async(item) for item in data_json]

        return data_json


# 全局单例
_registry_instance = None


def get_entity_registry():
    """获取全局实体注册表实例"""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = EntityRegistry()
    return _registry_instance


async def deduplicate_entity_async(entity_json):
    """
    对外异步接口：对 entity JSON 进行实体查重/归一化（大模型匹配）。
    """
    registry = get_entity_registry()
    return await registry.normalize_entity_json_async(entity_json)


async def deduplicate_data_async(data_json):
    """
    对外异步接口：对 data JSON 中的 transactions 主体字段进行查重/归一化。
    """
    registry = get_entity_registry()
    return await registry.normalize_data_json_async(data_json)


# 保留旧同步接口（仅推荐在同步独立脚本中使用；异步流程请使用 deduplicate_entity_async）
def deduplicate_entity(entity_json, min_ratio=0.65):
    """
    对外同步接口：对 entity JSON 进行实体查重/归一化。
    注意：在已有事件循环的异步上下文中调用会引发嵌套循环错误，请改用 deduplicate_entity_async。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        raise RuntimeError(
            "deduplicate_entity() 不能在异步上下文中使用，请改用 deduplicate_entity_async()。"
        )

    registry = get_entity_registry()
    return asyncio.run(registry.normalize_entity_json_async(entity_json))


if __name__ == "__main__":
    # 简单测试
    async def _test():
        registry = get_entity_registry()
        await registry.normalize_name_async("shipyard", "外高桥造船")
        await registry.normalize_name_async("shipyard", "外高桥")
        await registry.normalize_name_async("shipowner", "中远海运能源")

        test_entity = {
            "shipowner": "中远海能",
            "shipyard": "外高桥",
            "companies": ["中远海控", "某新公司"]
        }
        print(json.dumps(await deduplicate_entity_async(test_entity), ensure_ascii=False, indent=2))

    asyncio.run(_test())
