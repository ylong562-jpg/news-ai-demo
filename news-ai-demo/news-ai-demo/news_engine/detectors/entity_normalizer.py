# -*- coding: utf-8 -*-
r"""
船厂名称标准化模块

1. 从 /path/to/normalized_shipyard_vocabulary.json 读取船厂词表（canonical_name + aliases）构建 alias→canonical 映射（只读，不再写入）。
2. 优先使用 AI 在船厂规范名中匹配。
3. 词表未命中时，检查强制映射兜底；已是中文则保留原样；明显外国船厂保留英文。
4. 对疑似中国船厂但未命中的英文名称，可使用浏览器搜索兜底查询中文名，但结果不再录入词表。
5. 仅对 shipyard 字段做标准化，船东等其他主体保留原样。
6. 词表为只读：未命中的名称只做检测/原样返回，不允许注册进词表。
"""

import json
import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# 船厂词表：统一使用规范词表 JSON（只读，可用环境变量覆盖）。
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR.parent / "data"
SHIPYARD_VOCAB_PATH = Path(os.getenv("SHIPYARD_VOCABULARY_PATH", "/path/to/normalized_shipyard_vocabulary.json"))
SEARCH_CACHE_JSON = DATA_DIR / "shipyard_search_cache.json"
AI_MATCH_CACHE_JSON = DATA_DIR / "shipyard_ai_match_cache.json"

LLM_MODEL = "<your-model>"

FOREIGN_KEYWORDS = {
    "korea", "south korea", "japan", "norway", "germany", "italy", "france",
    "denmark", "greece", "netherlands", "finland", "spain", "brazil", "vietnam",
    "philippines", "india", "turkey", "singapore", "dubai", "uae",
    "hyundai", "samsung", "dsme", "daewoo", "mitsubishi", "kawasaki", "imabari",
    "namura", "sasebo", "fincantieri", "meyer werft", "damen", "stx", "hanjin",
    "sumitomo", "tacoma", "nassco", "bath iron works", "huntington", "general dynamics",
    "navantia", "dsic", "cssc", "csic", "cosco", "china merchants", "jiangnan",
}

MANUAL_SHIPYARD_OVERRIDES = {}

_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_json_map(path: Path) -> Dict[str, Optional[str]]:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_json_map(path: Path, data: Dict[str, Optional[str]]):
    _ensure_data_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _parse_aliases(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(a).strip() for a in value if str(a).strip()]
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(a).strip() for a in parsed if str(a).strip()]
    except Exception:
        pass
    return []


def _json_aliases(aliases: List[str]) -> str:
    return json.dumps(aliases, ensure_ascii=False)


_OVERLY_BROAD_NAME_PATTERNS = [
    r"旗下",
    r"有关",
    r"某",
    r"各",
    r"相关",
    r"等\s*$",
    r"^(中国|国内)\s*(船厂|公司|船东|港口)",
    r"^(船厂|公司|船东|港口)$",
    r"^(中国|国内)(船舶|造船)$",
    r"\b(a|the|some|certain|chinese|domestic)\s+(shipyard|company|shipowner|port)\b",
]


def _is_overly_broad_name(name: str) -> bool:
    if not name:
        return False
    text = name.strip().lower()
    if not text:
        return False
    for pat in _OVERLY_BROAD_NAME_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


# 船厂名中常见的通用后缀，本地模糊匹配时会先去除这些后缀再比较。
_COMMON_SUFFIXES = [
    "有限责任公司", "有限公司", "股份有限公司", "股份公司",
    "集团", "公司", "企业", "总厂",
    "造船厂", "船厂", "造船", "船舶", "重工", "船务",
    "海工", "海洋工程", "海洋石油工程",
    "Shipbuilding Co., Ltd.", "Shipbuilding Co Ltd", "Shipbuilding Company",
    "Shipyard Co., Ltd.", "Shipyard Co Ltd", "Shipyard Company",
    "Co., Ltd.", "Co Ltd", "Ltd.", "Ltd", "Inc.", "Inc", "Group",
]

# 用于识别纯英文名称（允许拉丁字母、数字、空格、标点，但不含 CJK 字符）。
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]")

# 用于粗略判断一个名称是否像船厂名，未命中词表时决定是否自动注册为新实体。
_SHIPYARD_NAME_KEYWORDS = [
    "船厂", "造船", "船舶", "重工", "船务", "海工", "海洋工程", "海洋石油工程",
    "shipyard", "shipbuilder", "shipbuilding", "heavy industry", "marine engineering",
    "marine", "offshore",
]


def _looks_like_shipyard_name(name: str) -> bool:
    if not name:
        return False
    lower = name.lower()
    return any(kw in lower for kw in _SHIPYARD_NAME_KEYWORDS)


def _looks_english(text: str) -> bool:
    """判断字符串是否主要由拉丁字符组成（不含中日韩文字）。"""
    if not text or not text.strip():
        return False
    return not bool(_CJK_RE.search(text))


def _strip_common_suffixes(name: str) -> str:
    """去除船厂名中的通用组织后缀，用于本地模糊匹配。"""
    if not name:
        return name
    s = name.strip()
    changed = True
    while changed and s:
        changed = False
        for suffix in _COMMON_SUFFIXES:
            if s.lower().endswith(suffix.lower()):
                s = s[: -len(suffix)].strip()
                # 同时去掉可能残留的括号说明
                s = re.sub(r"\s*[（(][^）)]+[）)]\s*$", "", s).strip()
                changed = True
                break
    return s


class ShipyardNameNormalizer:
    def __init__(
        self,
        enable_browser_search: bool = True,
        chrome_driver_path: Optional[str] = None,
    ):
        self.enable_browser_search = enable_browser_search
        self.chrome_driver_path = chrome_driver_path
        self._vocab_rows: Optional[List[Dict[str, Any]]] = None
        self._map: Optional[Dict[str, str]] = None
        self._canonicals: Optional[List[str]] = None
        self._search_cache: Optional[Dict[str, Optional[str]]] = None
        self._ai_match_cache: Optional[Dict[str, Optional[str]]] = None
        self._ai_client: Optional[Any] = None
        self._lock = threading.RLock()

    def _load_registry_rows(self) -> List[Dict[str, Any]]:
        """从船厂词表 JSON 加载记录（只读，带内存缓存）。

        兼容两种结构：扁平列表 [{canonical_name, aliases, ...}]
        或 {"entries": [...]}。新词表无专用 english_name 字段时，
        取第一个英文别名作为 english_name。
        """
        if self._vocab_rows is not None:
            return self._vocab_rows
        with self._lock:
            if self._vocab_rows is not None:
                return self._vocab_rows
            rows: List[Dict[str, Any]] = []
            if not SHIPYARD_VOCAB_PATH.exists():
                print(f"[ShipyardNormalizer] 船厂词表不存在: {SHIPYARD_VOCAB_PATH}")
                self._vocab_rows = rows
                return rows
            try:
                with open(SHIPYARD_VOCAB_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"[ShipyardNormalizer] 船厂词表加载失败: {e}")
                self._vocab_rows = rows
                return rows
            items = data if isinstance(data, list) else data.get("entries", [])
            for item in items:
                canonical = str(item.get("canonical_name") or "").strip()
                if not canonical:
                    continue
                aliases = _parse_aliases(item.get("aliases"))
                english = str(item.get("english_name") or "").strip()
                if not english:
                    for a in aliases:
                        if _looks_english(a):
                            english = a
                            break
                rows.append({
                    "canonical_name": canonical,
                    "english_name": english,
                    "aliases": aliases,
                    "frequency": item.get("frequency") or 1,
                })
            self._vocab_rows = rows
            return rows

    def _load_map(self) -> Dict[str, str]:
        if self._map is not None:
            return self._map

        with self._lock:
            if self._map is not None:
                return self._map

            rows = self._load_registry_rows()
            mapping: Dict[str, str] = {}
            canonicals: List[str] = []
            for row in rows:
                canonical = str(row.get("canonical_name") or "").strip()
                if not canonical:
                    continue
                canonicals.append(canonical)
                # 规范名自身映射到自身
                mapping[self._normalize_key(canonical)] = canonical
                # 去通用后缀的规范名也映射（如“中船黄埔文冲船舶”→“中船黄埔文冲船舶有限公司”）
                canonical_core = _strip_common_suffixes(canonical)
                if canonical_core and canonical_core != canonical:
                    mapping[self._normalize_key(canonical_core)] = canonical

                # 专用英文字段
                english = str(row.get("english_name") or "").strip()
                if english:
                    mapping[self._normalize_key(english)] = canonical
                    english_core = _strip_common_suffixes(english)
                    if english_core and english_core != english:
                        mapping[self._normalize_key(english_core)] = canonical

                for alias in _parse_aliases(row.get("aliases")):
                    alias = alias.strip()
                    if alias:
                        mapping[self._normalize_key(alias)] = canonical
                        alias_core = _strip_common_suffixes(alias)
                        if alias_core and alias_core != alias:
                            mapping[self._normalize_key(alias_core)] = canonical

            self._map = mapping
            self._canonicals = sorted(set(canonicals))
            return self._map

    def _load_search_cache(self) -> Dict[str, Optional[str]]:
        with self._lock:
            if self._search_cache is None:
                self._search_cache = _load_json_map(SEARCH_CACHE_JSON)
            return self._search_cache

    def _save_search_cache(self):
        with self._lock:
            if self._search_cache is not None:
                _save_json_map(SEARCH_CACHE_JSON, self._search_cache)

    def _load_ai_match_cache(self) -> Dict[str, Optional[str]]:
        with self._lock:
            if self._ai_match_cache is None:
                self._ai_match_cache = _load_json_map(AI_MATCH_CACHE_JSON)
            return self._ai_match_cache

    def _save_ai_match_cache(self):
        with self._lock:
            if self._ai_match_cache is not None:
                _save_json_map(AI_MATCH_CACHE_JSON, self._ai_match_cache)

    def _init_ai_client(self) -> bool:
        if self._ai_client is not None:
            return True
        if OpenAI is None:
            return False
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            return False
        try:
            self._ai_client = OpenAI(
                api_key=api_key,
                base_url="<your-llm-endpoint>",  # 演示版已移除真实接口地址
            )
            return True
        except Exception:
            return False

    def _is_foreign(self, name: str) -> bool:
        lower = name.lower()
        for kw in FOREIGN_KEYWORDS:
            if kw in lower:
                return True
        return False

    def _is_chinese(self, name: str) -> bool:
        return bool(_CHINESE_RE.search(name))

    def _normalize_key(self, name: str) -> str:
        name = name.strip()
        name = re.sub(r"\s+", " ", name)
        return name.lower()

    def _lookup_exact(self, name: str, mapping: Dict[str, str]) -> Optional[str]:
        key = self._normalize_key(name)
        return mapping.get(key)

    def _lookup_fuzzy(self, name: str, mapping: Dict[str, str]) -> Optional[str]:
        key = self._normalize_key(name)
        best_cn = None
        best_score = 0
        for k, cn in mapping.items():
            if not k:
                continue
            if key == k:
                return cn
            if key in k or k in key:
                score = min(len(key), len(k))
                if score > best_score and score >= 4:
                    best_score = score
                    best_cn = cn
        return best_cn

    def _local_recall(self, raw_name: str) -> List[str]:
        """
        本地词表召回：返回可能匹配的 canonical_name 候选列表。
        仅用于缩小 AI 匹配范围，不直接作为最终匹配结果，避免"中船集团船厂"
        被直接匹配到"中船集团青岛船厂"等以大配小的问题。
        """
        if not raw_name:
            return []
        mapping = self._load_map()
        if not mapping:
            return []

        key = self._normalize_key(raw_name)
        core = _strip_common_suffixes(raw_name)
        core_key = self._normalize_key(core)

        candidates: Dict[str, int] = {}

        def _add(cn: str, score: int):
            if cn:
                candidates[cn] = max(candidates.get(cn, 0), score)

        for k, cn in mapping.items():
            if not k:
                continue
            # 精确匹配（最高分）
            if key == k or core_key == k:
                _add(cn, 100)
                continue
            # 子串召回：避免过短匹配
            if key in k or k in key or core_key in k or k in core_key:
                overlap = max(
                    len(key) if key in k or k in key else 0,
                    len(core_key) if core_key in k or k in core_key else 0,
                )
                if overlap >= 4:
                    _add(cn, overlap)

        # 按召回分数降序，返回最多 20 个候选
        return [cn for cn, _ in sorted(candidates.items(), key=lambda x: -x[1])[:20]]

    def _ai_match(self, raw_name: str, candidates: Optional[List[str]] = None) -> Optional[str]:
        if not raw_name or not self._init_ai_client():
            return None

        # 仅对全量匹配使用缓存；候选匹配因候选集合可能变化，不缓存
        is_full_match = candidates is None
        if is_full_match:
            cache = self._load_ai_match_cache()
            if raw_name in cache:
                return cache[raw_name]

        with self._lock:
            if is_full_match:
                cache = self._load_ai_match_cache()
                if raw_name in cache:
                    return cache[raw_name]

            self._load_map()
            if candidates is None:
                candidates = self._canonicals or []
                if not candidates:
                    return None
                # 当前船厂词表约 900+ 条，全部放入单次 prompt 即可（生产 LLM 上下文充足），
                # 无需分 chunk 多次询问；这里仅保留一个宽松的安全上限。
                MAX_CANDIDATES = 2000
                if len(candidates) > MAX_CANDIDATES:
                    candidates = candidates[:MAX_CANDIDATES]
            else:
                candidates = list(dict.fromkeys(candidates))  # 去重保留顺序
                if not candidates:
                    return None

            candidates_text = "\n".join(f"- {c}" for c in candidates)
            if is_full_match:
                prompt = (
                    "【演示版：AI 匹配指令已删减】请判断输入名称是否对应候选标准船厂名，只输出标准名或\"无匹配\"。\n\n"
                    f"候选标准船厂名：\n{candidates_text}\n\n"
                    f"输入：{raw_name}\n\n"
                    "输出："
                )
            else:
                prompt = (
                    "【演示版：AI 匹配指令已删减】请判断输入名称是否与候选船厂名指向同一具体船厂，只输出标准名或\"无匹配\"。\n\n"
                    f"候选标准船厂名：\n{candidates_text}\n\n"
                    f"输入：{raw_name}\n\n"
                    "输出："
                )

            try:
                response = self._ai_client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[
                        {"role": "system", "content": "你是航运领域船厂名称标准化专家。"},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=100,
                )
                result = response.choices[0].message.content.strip()
            except Exception as e:
                print(f"[ShipyardNormalizer] AI 匹配失败 {raw_name}: {e}")
                if is_full_match:
                    cache[raw_name] = None
                    self._save_ai_match_cache()
                return None

            if not result or result in ("无匹配", "无", "None", "null"):
                matched = None
            elif result in candidates:
                matched = result
            else:
                matched = None
                for c in candidates:
                    if c in result or result in c:
                        matched = c
                        break

            if is_full_match:
                cache[raw_name] = matched
                self._save_ai_match_cache()
            if matched:
                print(f"[ShipyardNormalizer] AI 匹配: {raw_name} -> {matched}")
            return matched

    def _browser_search_chinese_name(self, english_name: str) -> Optional[str]:
        if not self.enable_browser_search:
            return None

        cache = self._load_search_cache()
        if english_name in cache:
            return cache[english_name]

        with self._lock:
            cache = self._load_search_cache()
            if english_name in cache:
                return cache[english_name]

            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.service import Service as ChromeService
                from selenium.webdriver.common.by import By
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC
                from webdriver_manager.chrome import ChromeDriverManager
            except ImportError:
                cache[english_name] = None
                return None

            driver = None
            try:
                query = f"{english_name} 船厂 中文"
                search_url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"

                chrome_options = Options()
                chrome_options.add_argument("--headless")
                chrome_options.add_argument("--disable-gpu")
                chrome_options.add_argument("--no-sandbox")
                chrome_options.add_argument("--disable-dev-shm-usage")
                chrome_options.add_argument(
                    "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                )

                # 使用 webdriver-manager 自动下载与当前 Chrome 版本匹配的驱动，
                # 避免系统 PATH 中的 chromedriver 版本不兼容。
                if self.chrome_driver_path and os.path.exists(self.chrome_driver_path):
                    service = ChromeService(executable_path=self.chrome_driver_path)
                else:
                    service = ChromeService(ChromeDriverManager().install())
                driver = webdriver.Chrome(service=service, options=chrome_options)

                driver.get(search_url)
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )

                text = driver.find_element(By.TAG_NAME, "body").text
                candidates = re.findall(r"[\u4e00-\u9fff]{2,}(?:船厂|造船|船舶|重工|船务|海工|海洋工程)", text)
                if candidates:
                    from collections import Counter
                    counter = Counter(candidates)
                    best = counter.most_common(1)[0][0]
                    cache[english_name] = best
                    self._save_search_cache()
                    # 词表为只读：搜索结果只作为本次标准化返回值，不再录入词表
                    return best

                cache[english_name] = None
                return None
            except Exception as e:
                print(f"[ShipyardNormalizer] 浏览器搜索失败 {english_name}: {e}")
                cache[english_name] = None
                return None
            finally:
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass

    def _ai_generate_english_name(self, canonical_name: str) -> Optional[str]:
        """调用 AI 为中文船厂名生成官方英文名称。"""
        if not canonical_name or not self._is_chinese(canonical_name):
            return None
        if not self._init_ai_client():
            return None

        prompt = (
            "【演示版：翻译指令已删减】请将以下中文船厂名翻译成权威英文名称，只输出英文名称。\n\n"
            f"中文船厂名：{canonical_name}\n\n"
            "英文名称："
        )
        try:
            response = self._ai_client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": "你是航运领域船厂英文名称专家。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=100,
            )
            result = response.choices[0].message.content.strip()
            # 清理可能包裹的引号或多余空格
            result = result.strip('"').strip("'")
            if result and _looks_english(result) and len(result) > 2:
                return result
        except Exception as e:
            print(f"[ShipyardNormalizer] AI 生成英文名称失败 {canonical_name}: {e}")
        return None

    def _ensure_english_name(self, canonical_name: str) -> Optional[str]:
        """
        返回 canonical_name 对应的英文名称（只读，不写入词表）。
        优先从词表的 english_name / aliases 中提取；缺失则调用 AI 生成，
        但不再持久化（词表为只读，未检测到的实体不允许录入）。
        """
        if not canonical_name:
            return None
        canonical = canonical_name.strip()

        # 若规范名本身就是英文，则规范名与英文名一致
        if _looks_english(canonical):
            return canonical

        # 从词表中查找 english_name / 英文别名
        for row in self._load_registry_rows():
            if str(row.get("canonical_name") or "").strip() != canonical:
                continue
            english = str(row.get("english_name") or "").strip()
            if english and _looks_english(english):
                return english
            for alias in _parse_aliases(row.get("aliases")):
                if _looks_english(alias):
                    return alias
            break

        # 词表中没有英文名称：AI 生成（不写入词表）
        return self._ai_generate_english_name(canonical)

    def normalize(self, raw_name: Optional[str]) -> Optional[str]:
        if not raw_name or not isinstance(raw_name, str):
            return raw_name

        raw_name = raw_name.strip()
        if not raw_name:
            return raw_name

        # 1. 本地词表召回候选，缩小 AI 匹配范围
        local_candidates = self._local_recall(raw_name)
        if local_candidates:
            matched = self._ai_match(raw_name, candidates=local_candidates)
            if matched:
                return matched

        # 2. 本地未召回到候选时，使用全量 AI 匹配兜底
        cn = self._ai_match(raw_name)
        if cn:
            return cn

        key = self._normalize_key(raw_name)
        override = MANUAL_SHIPYARD_OVERRIDES.get(key)
        if override:
            return override

        # 词表为只读：未命中的名称不再注册进词表，只做检测后原样返回。
        if _looks_like_shipyard_name(raw_name) and not _is_overly_broad_name(raw_name):
            return raw_name

        if self._is_chinese(raw_name):
            return raw_name

        if self._is_foreign(raw_name):
            return raw_name

        if self.enable_browser_search:
            searched = self._browser_search_chinese_name(raw_name)
            if searched:
                return searched

        return raw_name

    def normalize_json_entity(self, entity: Any) -> Any:
        if entity is None:
            return entity

        if isinstance(entity, str):
            try:
                data = json.loads(entity)
                normalized = self.normalize_json_entity(data)
                return json.dumps(normalized, ensure_ascii=False)
            except Exception:
                return entity

        if isinstance(entity, dict):
            result = {}
            for k, v in entity.items():
                key_lower = str(k).lower()
                if key_lower in ("shipyard", "shipyards", "facility", "facilities") and isinstance(v, str):
                    result[k] = self.normalize(v)
                elif key_lower in ("shipyard", "shipyards", "facility", "facilities") and isinstance(v, list):
                    result[k] = [self.normalize(x) for x in v]
                else:
                    result[k] = self.normalize_json_entity(v)
            return result

        if isinstance(entity, list):
            return [self.normalize_json_entity(x) for x in entity]

        return entity


_default_normalizer: Optional[ShipyardNameNormalizer] = None


def get_default_normalizer() -> ShipyardNameNormalizer:
    global _default_normalizer
    if _default_normalizer is None:
        _default_normalizer = ShipyardNameNormalizer()
    return _default_normalizer


def normalize_shipyard_name(raw_name: Optional[str]) -> Optional[str]:
    return get_default_normalizer().normalize(raw_name)


def normalize_shipyard_entity(entity: Any) -> Any:
    return get_default_normalizer().normalize_json_entity(entity)


if __name__ == "__main__":
    n = ShipyardNameNormalizer()
    samples = [
        "Shanghai Waigaoqiao Shipbuilding Co., Ltd.",
        "Yangzijiang Shipbuilding",
        "Meyer Werft",
        "上海外高桥造船有限公司",
        "Unknown Chinese Shipyard Ltd",
    ]
    for s in samples:
        print(f"{s} -> {n.normalize(s)}")
