#!/usr/bin/env python3
"""
演示入口：使用 news_engine 中的【真实 AI 处理代码】离线跑通全流程。

    python run_demo.py

流程（全部调用 news_engine 真实实现，LLM 由 MockAnalyzer/MockEngine 替代）：
  阶段1  AI 标签分类        ai/ai_classify.py              re_classify
  阶段2  核心内容结构化提取 ai/ai_news_core_content_extraction.py
  阶段3  AI 语义去重        ai/ai_deduplicate_engine.py    ai_deduplicate_news

离线原理：
  1. 向 sys.modules 注入 openai / bs4 桩模块（真实模式请安装依赖并配置环境变量）；
  2. 用 MockAnalyzer 替换各模块引用的 BailianWebAnalyzer（返回结构一致的模拟结果）；
  3. 用 MockEngine 替换 AIDeduplicateEngine（其内部去重规则逻辑仍是真实代码，
     仅"两两相似度 AI 判定"一步由模拟实现）。
"""

import asyncio
import json
import os
import re
import sys
import types

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NEWS_ENGINE = os.path.join(BASE_DIR, "news_engine")

# news_engine 内部使用扁平导入（ai/ utils/ detectors/ core/ 均在 sys.path 上）
for sub in ("ai", "utils", "detectors", "core", ""):
    p = os.path.join(NEWS_ENGINE, sub)
    if p not in sys.path:
        sys.path.insert(0, p)


def _install_stubs():
    """注入离线演示所需的桩模块（不发起任何真实网络请求）。"""
    openai_stub = types.ModuleType("openai")

    class _UnavailableOpenAI:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("演示模式不发起真实 LLM 调用；真实模式请安装 openai 并配置环境变量")

    openai_stub.OpenAI = _UnavailableOpenAI
    sys.modules.setdefault("openai", openai_stub)

    bs4_stub = types.ModuleType("bs4")
    bs4_stub.BeautifulSoup = type("BeautifulSoup", (), {})
    bs4_stub.Comment = type("Comment", (), {})
    sys.modules.setdefault("bs4", bs4_stub)


_install_stubs()

import ai_classify                      # noqa: E402
import ai_deduplicate_engine            # noqa: E402
import ai_engine                        # noqa: E402
import ai_news_core_content_extraction  # noqa: E402
import news_splitter                    # noqa: E402

# ---------------------------------------------------------------------------
# 离线 Mock：返回结构与真实 BailianWebAnalyzer.analyze / AIDeduplicateEngine 一致
# ---------------------------------------------------------------------------

_SHIPYARD_PROBES = [
    ("扬子江船业", "扬子江船业"), ("yangzijiang", "扬子江船业"),
    ("新时代造船", "新时代造船"), ("new times shipbuilding", "新时代造船"),
    ("外高桥造船", "外高桥造船"), ("waigaoqiao", "外高桥造船"),
    ("大连船舶重工", "大连船舶重工"), ("dalian shipbuilding", "大连船舶重工"),
]
_SHIP_TYPE_PROBES = [
    ("集装箱船", "集装箱船"), ("container ship", "集装箱船"), ("boxship", "集装箱船"),
    ("散货船", "散货船"), ("bulk carrier", "散货船"),
    ("lng", "LNG运输船"), ("LNG", "LNG运输船"),
    ("油轮", "油轮"), ("tanker", "油轮"), ("vlcc", "超大型原油船"),
]
_DEMO_OWNER = "环洋航运集团"  # 虚构的演示用订船方


def _probe(text, probes):
    lowered = (text or "").lower()
    for needle, canonical in probes:
        if needle.lower() in lowered:
            return canonical
    return None


def _pick_main_tag(content):
    c = content or ""
    if any(k in c for k in ("订单", "签约", "订购", "订造")) or any(
            k in c.lower() for k in ("order", "contract", "newbuilding", "clinches")):
        return "新造船订单"
    if any(k in c for k in ("交付", "交船", "命名", "下水", "开工", "试航")) or any(
            k in c.lower() for k in ("delivered", "delivery", "keel", "launch")):
        return "船舶建造"
    return "行业新闻"


class MockAnalyzer:
    """模拟 BailianWebAnalyzer：analyze() 返回结构一致的结果字典。"""

    def __init__(self, region="beijing"):
        self.region = region
        self.calls = 0

    async def analyze(self, url=None, content=None, prompt="", max_tokens=200,
                      model="<your-model>", semaphore=None):
        self.calls += 1

        async def _go():
            await asyncio.sleep(0.01)  # 模拟网络延迟
            return self._respond(content or url or "", prompt or "")

        if semaphore:
            async with semaphore:
                return await _go()
        return await _go()

    def _respond(self, content, prompt):
        try:
            analysis = self._route(content, prompt)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "analysis": analysis,
            "usage": {"prompt_tokens": len(content) // 2, "completion_tokens": len(analysis) // 2},
            "model": "mock-model",
        }

    def _route(self, content, prompt):
        # 多主维度拆分（本演示数据均为单主维度，不会走到；保留以兼容）
        if "拆分为若干段" in prompt:
            m = re.search(r"给定主维度（按相关性从高到低）：\n([^\n]+)", prompt)
            first = (m.group(1).split("、")[0].strip() if m else "行业新闻")
            return json.dumps([{"main_tag": first, "summary": content[:800]}], ensure_ascii=False)

        # 标签分类
        if "main_tags" in prompt:
            main_tag = _pick_main_tag(content)
            secondary = [t for t in (
                _probe(content, _SHIP_TYPE_PROBES), _probe(content, _SHIPYARD_PROBES)) if t]
            return json.dumps({
                "main_tags": [main_tag],
                "secondary_tags": secondary,
                "information_density": {main_tag: "medium"},
                "classification_reason": "（演示模拟输出）",
            }, ensure_ascii=False)

        # 核心内容提取：entity / data（按删减版 prompt 的任务标记路由）
        m = re.search(r"【当前聚焦主维度：(.+?)】", prompt)
        main_tag = m.group(1) if m else "行业新闻"
        if "交易要素（data）" in prompt:
            return json.dumps(self._data(content, main_tag), ensure_ascii=False)
        return json.dumps(self._entity(content, main_tag), ensure_ascii=False)

    def _entity(self, content, main_tag):
        shipyard = _probe(content, _SHIPYARD_PROBES)
        if main_tag == "行业新闻":
            return {"entities": [shipyard or "航运业"], "countries": [], "organizations": []}
        return {"shipowner": _DEMO_OWNER, "shipyard": shipyard}

    def _data(self, content, main_tag):
        shipyard = _probe(content, _SHIPYARD_PROBES)
        ship_type = _probe(content, _SHIP_TYPE_PROBES) or "集装箱船"
        if main_tag == "新造船订单":
            return {
                "trade_type": "签署订单", "date": "2026-10-10",
                "shipowner": _DEMO_OWNER, "ship_type": ship_type, "quantity": "6",
                "specs": "15000 TEU", "price": "7.8亿美元", "delivery": "2028年",
                "power": None, "shipyard": shipyard,
                "classification_society": None, "flag": None, "design_institute": None,
            }
        if main_tag == "船舶建造":
            event = "钢材切割" if ("开工" in content or "keel" in content.lower()) else "交付"
            return {
                "date": "2026-10-08", "event_type": event,
                "shipowner": _DEMO_OWNER, "shipyard": shipyard,
                "ship_type": ship_type, "specs": None, "vessel_name": None,
            }
        # 行业新闻等
        return {
            "event_type": "企业动态", "date": None,
            "shipowner_related": False, "shipyard_related": bool(shipyard),
            "shipowners": [], "shipyards": [shipyard] if shipyard else [],
            "key_info": None, "location": None, "affected_routes": None,
            "impact": None, "metrics": {},
        }


class MockEngine(ai_deduplicate_engine.AIDeduplicateEngine):
    """继承真实去重引擎，仅替换 API 客户端初始化与"两两 AI 判定"：

    - __init__：不再要求 DASHSCOPE_API_KEY / 真实接口（演示版已移除）；
    - check_duplicate_pair：模拟 LLM 判定（真实代码中的规则预筛、
      同日同维度同主体判重、信息丰富度合并等逻辑全部保留真实实现）。
    """

    def __init__(self, region="beijing"):
        self.region = region  # 跳过真实 API 客户端初始化

    async def check_duplicate_pair(self, news1, news2):
        c1 = f"{news1.get('title', '')} {news1.get('summary', '')}"
        c2 = f"{news2.get('title', '')} {news2.get('summary', '')}"
        same_yard = _probe(c1, _SHIPYARD_PROBES) and _probe(c1, _SHIPYARD_PROBES) == _probe(c2, _SHIPYARD_PROBES)
        same_type = _probe(c1, _SHIP_TYPE_PROBES) and _probe(c1, _SHIP_TYPE_PROBES) == _probe(c2, _SHIP_TYPE_PROBES)
        both_order = _pick_main_tag(c1) == "新造船订单" and _pick_main_tag(c2) == "新造船订单"
        if same_yard and same_type and both_order:
            return True, 0.95, "（演示）同一事件的中英文报道"
        return False, 0.1, ""


# 用 Mock 替换各模块中引用的真实客户端类（原文件零改动）
for _mod in (ai_engine, ai_classify, ai_news_core_content_extraction, news_splitter):
    _mod.BailianWebAnalyzer = MockAnalyzer
ai_deduplicate_engine.AIDeduplicateEngine = MockEngine


async def main():
    sample = os.path.join(BASE_DIR, "sample_data", "news_items.json")
    with open(sample, encoding="utf-8") as f:
        news_items = json.load(f)

    print("\n" + "=" * 70)
    print("阶段 1/3：AI 标签分类（ai_classify.re_classify）")
    print("=" * 70)
    classified = await ai_classify.re_classify(news_data=news_items)
    if not classified:
        print("❌ 标签分类无结果")
        return

    print("\n" + "=" * 70)
    print("阶段 2/3：核心内容结构化提取（ai_news_core_content_extraction）")
    print("=" * 70)
    extracted = await ai_news_core_content_extraction.process_news_with_category_prompts(classified)
    extracted = [e for e in extracted if e]

    print("\n" + "=" * 70)
    print("阶段 3/3：AI 语义去重（ai_deduplicate_engine.ai_deduplicate_news）")
    print("=" * 70)
    final = await ai_deduplicate_engine.ai_deduplicate_news(extracted, use_grouping=False)

    # 保存结构化结果摘要
    summary = []
    for item in final:
        summary.append({
            "id": item.get("id"),
            "title": item.get("title"),
            "date": item.get("date"),
            "source_platform": item.get("source_platform"),
            "duplicate_count": item.get("duplicate_count", 1),
            "tags": item.get("tags"),
            "entity": item.get("entity"),
            "data": item.get("data"),
        })
    out = os.path.join(BASE_DIR, "demo_output.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 70)
    print(f"✅ 流水线完成: {len(news_items)} -> {len(final)} 条（重复报道已合并）")
    print(f"   结构化结果已写入 {out}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
