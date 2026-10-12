# news-ai-demo — 船舶新闻 AI 处理引擎（演示版）

船舶/航运新闻的 AI 处理管线，从船舶航运新闻聚合分析系统中剥离的核心模块，全部业务代码原样保留。流水线分三个阶段：

```
sample_data/news_items.json
        │
  阶段1  AI 标签分类          ai/ai_classify.py
        │    （批量并发 + 输出校验 + 低密度主维度过滤）
  阶段2  核心内容结构化提取    ai/ai_news_core_content_extraction.py
        │    （按主维度选 prompt 并发提取 → 校验失败自动反馈重试
        │      → 交易对齐 → 日期 ISO 化 / 船厂船型名归一化）
  阶段3  AI 语义去重            ai/ai_deduplicate_engine.py
             （规则预筛：同日+同主维度+同主体直接判重；
               否则标题相似度预筛 → 两两 LLM 语义判定 → 合并择优）
        ▼
demo_output.json（结构化 entity/data 提取结果）
```

演示模式完全离线，无需 API Key、数据库或第三方依赖（仅需 Python 3.9+ 标准库）：

```bash
python run_demo.py
```

示例数据中包含一对中英文不同来源报道的同一事件，去重阶段会识别合并（`duplicate_count=2`），全程输出真实判定日志。离线实现方式为 `run_demo.py` 向 `sys.modules` 注入 `openai`/`bs4` 桩模块并以 `MockAnalyzer` 替换 LLM 分析器。

## 目录结构

```
news-ai-demo/
├── run_demo.py                     # 演示入口（桩注入 + Mock + 三阶段驱动）
├── sample_data/news_items.json     # 示例新闻
├── demo_output.json                # 运行后生成
└── news_engine/                    # 真实业务代码
    ├── ai/
    │   ├── ai_engine.py            # LLM 客户端封装（并发/内容抽取/置信度）
    │   ├── ai_classify.py          # AI 标签分类
    │   ├── ai_news_core_content_extraction.py  # 核心内容结构化提取（1435 行）
    │   └── ai_deduplicate_engine.py            # AI 语义去重（1465 行，规则+LLM 两级）
    ├── ai_news_report_creating.py  # 新闻报告生成
    ├── transaction_groups.py       # 交易分组
    ├── utils/
    │   ├── prompt.py               # Prompt 库 + 标签体系 + 输出校验
    │   ├── json_validator.py       # JSON 多层兜底提取修复
    │   ├── news_splitter.py        # 多主维度新闻 AI 拆分
    │   ├── content_extractor.py    # 网页正文抽取
    │   ├── news_combine.py         # 多源新闻合并去重（含历史库 URL 比对）
    │   ├── title_utils.py          # 中文友好标题相似度
    │   ├── date_utils.py           # 日期解析
    │   └── db_config.py            # 数据库配置（环境变量 + 占位符）
    ├── detectors/                  # 规范化与质检
    │   ├── entity_normalizer.py    # 船厂名归一化（词表 + AI 兜底）
    │   ├── ship_type_normalizer.py / ship_type_retriever.py  # 船型名归一化/词表检索
    │   ├── entity_deduplication.py # 实体级去重
    │   ├── tag_quality_validator.py# 标签密度/原文证据校验
    │   └── confidence_config.py    # 置信度评分配
    ├── core/                       # 编排层（支持 progress_callback，供 Web 层 SSE 推送）
    │   ├── reprocess_database_news.py
    │   ├── complete_news_pipeline.py
    │   ├── daily_news_generation.py
    │   └── main.py                 # 定时任务入口
    └── news_analysis/              # 新闻库表结构（连接信息为占位符）
```

## 接入真实环境需配置的位置

| 配置项 | 位置 | 说明 |
|--------|------|------|
| LLM 接口与模型 | `news_engine/ai/ai_engine.py`、`ai/ai_deduplicate_engine.py`、`detectors/entity_normalizer.py` 等全部调用点 | 占位符 `<your-llm-endpoint>` / `<your-model>`（`LLM_MODEL`），替换为真实 base_url、api_key、模型名 |
| 数据库连接 | `news_engine/utils/db_config.py`、`news_engine/news_analysis/backend/common/config.py` | 环境变量 `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` |
| Python 依赖 | 安装 `openai`、`trafilatura`、`beautifulsoup4` 等 | 安装后桩模块自动失效，无需改动代码 |

> 演示说明：Prompt 正文的调优内容（判定规则/负例/示例/词表注入）以 `【演示版：…已删减】` 占位，函数签名、字段清单、标签体系与默认 JSON 结构均完整保留；仓库中不含任何真实凭据。
