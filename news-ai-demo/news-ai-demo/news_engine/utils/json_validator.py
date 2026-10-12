"""
JSON 验证与自动修复模块
用于检测 AI 返回的特定字段是否为合法 JSON，如果不是则让 AI 重新生成直至正确。
如果所有修复都失败，返回默认合法 JSON，绝不返回纯文本，避免下游解析崩溃。
"""

import json
import re


def extract_json_from_text(text):
    """
    从文本中提取 JSON 对象或数组。

    尝试顺序：
    1. 直接解析整个文本（去除首尾空白后）
    2. 提取 markdown ```json ... ``` / ``` ... ``` 代码块
    3. 通过括号平衡算法提取最外层 {...}
    4. 通过括号平衡算法提取最外层 [...]
    """
    if not text or not isinstance(text, str):
        return None

    cleaned = text.strip()

    # 1. 直接解析
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. 提取 markdown 代码块
    json_block_pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
    matches = re.findall(json_block_pattern, cleaned, re.IGNORECASE)
    for match in matches:
        try:
            return json.loads(match.strip())
        except (json.JSONDecodeError, ValueError):
            continue

    # 3. 尝试通过平衡花括号提取 JSON 对象
    start = cleaned.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        break

    # 4. 尝试通过平衡方括号提取 JSON 数组
    start = cleaned.find("[")
    if start != -1:
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == "[":
                depth += 1
            elif cleaned[i] == "]":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        break

    return None


def is_valid_json(text):
    """检查文本是否包含可解析的 JSON。"""
    return extract_json_from_text(text) is not None


def normalize_json_string(text):
    """提取并标准化 JSON 字符串（确保可被解析）。"""
    extracted = extract_json_from_text(text)
    if extracted is not None:
        return json.dumps(extracted, ensure_ascii=False)
    return text


def get_default_json_for_field(category, key_name):
    """
    根据分类和字段名，返回一个默认的合法 JSON 结构。
    用于所有修复尝试都失败时的兜底，确保下游拿到的是 JSON 而不是纯文本。
    """
    from prompt import get_default_json_for_field as _prompt_default
    return _prompt_default(category, key_name)


async def validate_and_fix_json(
    analyzer, raw_result, original_prompt, key_name, news_title="", max_retries=10,
    news_content="", category=""
):
    """
    验证 AI 返回结果是否为合法 JSON，如果不是则要求 AI 重新生成。
    若最终仍无法修复，返回默认合法 JSON 结构，绝不返回纯文本。

    Parameters:
        analyzer: BailianWebAnalyzer 实例
        raw_result: AI 返回的原始字符串
        original_prompt: 原始的 prompt 文本
        key_name: 字段名（如 'entity', 'data'）
        news_title: 当前处理的新闻标题（用于日志）
        max_retries: 最大重试次数
        news_content: 原始新闻内容（修复时必须传入，否则AI看不到原文只能返回null）
        category: 新闻分类，用于兜底默认JSON

    Returns:
        str: 合法的 JSON 字符串；修复失败时返回默认 JSON 而不是原始字符串
    """
    # 先尝试提取
    extracted = extract_json_from_text(raw_result)
    if extracted is not None:
        normalized = json.dumps(extracted, ensure_ascii=False)
        return normalized

    print(
        f"  ⚠️ [{key_name}] 返回内容不是合法 JSON，准备修复... (新闻: {news_title[:40]}...)"
    )

    content_snippet = news_content[:3000] if news_content else ""

    fix_prompt = f"""你之前对以下要求的回答格式错误，输出不是合法JSON。请严格基于原始新闻内容，按以下要求重新回答，并确保输出为合法JSON：

{original_prompt}

【格式强制要求 - 最高优先级】
1. 你必须且只能返回一个合法的 JSON 对象或 JSON 数组
2. 不要添加任何解释、前言、后缀、markdown 代码块标记（如 ```json）
3. 确保返回的字符串可以被 Python 的 json.loads() 直接解析
4. 所有键和字符串值必须使用英文双引号，不要使用中文引号或单引号
5. 如果某个字段没有数据，使用 null（不带引号），不要用 "null" 字符串
6. 绝对不要输出自然语言描述，必须直接输出纯 JSON

错误示例（不要这样输出）：
```json
{{"key": "value"}}
```
正确示例（必须这样输出）：
{{"key": "value"}}
"""

    for attempt in range(1, max_retries + 1):
        try:
            result = await analyzer.analyze(
                content=content_snippet,
                prompt=fix_prompt,
                max_tokens=400,
                model="<your-model>",
            )

            if result["success"]:
                analysis = result["analysis"]
                extracted = extract_json_from_text(analysis)
                if extracted is not None:
                    normalized = json.dumps(extracted, ensure_ascii=False)
                    print(f"  ✅ [{key_name}] JSON 修复成功（第 {attempt} 次）")
                    return normalized
                else:
                    print(
                        f"  ⚠️ [{key_name}] 修复第 {attempt} 次仍不是合法 JSON，继续重试..."
                    )
            else:
                print(
                    f"  ❌ [{key_name}] 修复第 {attempt} 次 API 调用失败: {result.get('error', '未知错误')}"
                )

        except Exception as e:
            print(f"  ❌ [{key_name}] 修复第 {attempt} 次异常: {str(e)}")

    # 最终兜底：返回默认 JSON，绝不允许纯文本流向下游
    default_json = get_default_json_for_field(category, key_name)
    print(
        f"  ⚠️ [{key_name}] JSON 修复失败，已使用默认 JSON 兜底: {default_json}"
    )
    return default_json
