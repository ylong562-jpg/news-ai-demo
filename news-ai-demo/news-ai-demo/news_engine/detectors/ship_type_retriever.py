# -*- coding: utf-8 -*-
"""
层次化船型检索器。

核心能力：
1. 从 /path/to/normalized_shiptype_vocabulary.json 加载多层次船型词表（规范词表，
   保留未知船型自动归类并持久化的能力）。
2. 自底向上优先匹配：level 3 -> level 2 -> level 1。
3. 未知船型自动归类：先匹配外层大类，再决定插入层级并持久化。
4. 为 AI prompt 生成候选列表 / 固定取值说明。

词表格式（扁平条目列表）：
{canonical_name, aliases[], category, parent, level, level_1, level_2, level_3,
 path[], size_info}
"""

import json
import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# 船型词表：统一使用规范词表 JSON（保留自动扩展写回，可用环境变量覆盖）。
TAXONOMY_PATH = Path(os.getenv("SHIPTYPE_VOCABULARY_PATH", "/path/to/normalized_shiptype_vocabulary.json"))

# 动力/燃料前缀：遇到 "前缀 + 基础船型" 的组合时，应拆分为 power + ship_type
POWER_PREFIXES = [
    "氢燃料电池",
    "氢燃料",
    "甲醇双燃料",
    "甲醇燃料",
    "LNG双燃料",
    "LNG燃料",
    "氨双燃料",
    "氨燃料",
    "电池动力",
    "核动力",
    "传统燃油",
    "柴油动力",
    "柴电混合",
    "风力辅助",
    "太阳能",
    "双燃料",
    "电动",
]

# 外层分类关键词映射（用于未知船型快速归类）
CATEGORY_KEYWORDS = {
    "集装箱船": ["container", "teu", "boxship", "feeder", "panamax", "post-panamax", "neo-panamax", "ulcv", "集装箱", "箱船"],
    "散货船": ["bulk", "bulker", "capesize", "panamax", "kamsarmax", "handysize", "handymax", "supramax", "ultramax", "vloc", "newcastlemax", "散货", "矿砂"],
    "油轮": ["tanker", "crude", "oil", "product", "vlcc", "ulcc", "suezmax", "aframax", "lr1", "lr2", "mr", "chemical", "bitumen", "asphalt", "vegitable", "edible", "油轮", "油船", "原油", "成品油", "化学品"],
    "液化气运输船": ["lng", "lpg", "gas carrier", "ethane", "ammonia", "co2", "q-max", "q-flex", "vlgc", "lgc", "mgc", "vlec", "液化天然气", "液化石油气", "乙烷", "氨", "二氧化碳"],
    "汽车运输船": ["car carrier", "pcc", "pctc", "vehicle carrier", "ro-ro", "汽车", "车辆"],
    "滚装船": ["ro-ro", "roro", "ropax", "ferry", "cruise", "passenger", "滚装", "客滚", "渡轮", "邮轮"],
    "多用途船": ["multi-purpose", "mpp", "mpv", "heavy lift", "general cargo", "多用途", "重吊", "杂货"],
    "海工平台": ["platform", "fpso", "flng", "fsru", "drillship", "jack-up", "semi-submersible", "tlp", "spar", "张力腿", "单柱式", "导管架", "重力式", "坐底式", "自升式", "半潜式", "钻井平台", "生产平台", "浮式生产", "浮式储存", "浮式液化", "海工平台", "海上平台", "海洋平台", "半潜式钻井平台", "自升式钻井平台", "钻井船"],
    "海工辅助船": ["offshore vessel", "offshore unit", "psv", "ahts", "ctv", "sovc", "海工辅助", "海工船", "海底电缆", "铺管", "风电运维", "平台供应", "三用工作船", "供应船", "守护船", "救助船", "起重船", "布缆船", "潜水支持船", "海缆敷设", "platform supply", "anchor handling", " offshore support"],
    "特种船": ["refrigerated", "livestock", "woodchip", "cement", "dredger", "crane", "tug", "salvage", "icebreaker", "research", "冷藏", "牲畜", "活鱼", "木屑", "水泥", "挖泥", "起重", "拖轮", "救助", "破冰", "科考"],
}


def _normalize(text: str) -> str:
    return re.sub(r"[\s\-_/]+", "", text.strip().lower())


def _split_power_and_type(name: str, retriever: "ShipTypeRetriever") -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    把 "动力前缀 + 基础船型" 的组合拆分开。
    例如 "氢燃料电池集装箱船" -> ("氢燃料电池", 集装箱船entry)
    如果无法拆分则返回 None。
    """
    n = _normalize(name)
    for prefix in sorted(POWER_PREFIXES, key=lambda x: -len(x)):
        np = _normalize(prefix)
        if not np:
            continue
        if n.startswith(np):
            remainder = name[len(prefix):].strip()
            if not remainder:
                continue
            # 如果 remainder 只是 "运输船"、"船" 等过于宽泛的词，跳过该前缀长度再试
            if _normalize(remainder) in ("运输船", "船", "船舶"):
                continue
            entry = retriever.retrieve(remainder)
            if not entry:
                # remainder 本身未命中，尝试在 remainder 中找最深的基础船型
                entry = retriever._find_deepest_contained_match(remainder)
            if entry and entry.get("level") in (1, 2, 3):
                return prefix, entry
    return None


def _keyword_classify(name: str) -> Optional[str]:
    """基于关键词快速判断未知船型属于哪个顶层分类。"""
    n = _normalize(name)
    scores = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if _normalize(kw) in n)
        if score:
            scores[category] = score
    if not scores:
        return None
    return max(scores, key=scores.get)


class ShipTypeRetriever:
    def __init__(self, taxonomy_path: Optional[Path] = None):
        self.taxonomy_path = taxonomy_path or TAXONOMY_PATH
        self.entries: List[Dict[str, Any]] = []
        self.by_canonical: Dict[str, Dict[str, Any]] = {}
        self.by_alias: Dict[str, Dict[str, Any]] = {}
        self._save_lock = threading.Lock()
        self._load()

    def _load(self):
        if not self.taxonomy_path.exists():
            raise FileNotFoundError(f"船型分类表不存在：{self.taxonomy_path}")
        with open(self.taxonomy_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 兼容两种结构：扁平条目列表（/opt 规范词表）或 {"entries": [...]}（旧 taxonomy）
        self.entries = data if isinstance(data, list) else data.get("entries", [])
        self._normalize_entry_fields(self.entries)

        # 自底向上优先级：level 深的先注册，保证同别名取最精确
        for entry in sorted(self.entries, key=lambda x: -x["level"]):
            canonical = entry["canonical_name"]
            norm_canonical = _normalize(canonical)
            if norm_canonical not in self.by_canonical:
                self.by_canonical[norm_canonical] = entry
            for alias in entry.get("aliases", []):
                norm_alias = _normalize(alias)
                if not norm_alias:
                    continue
                if norm_alias not in self.by_alias:
                    self.by_alias[norm_alias] = entry

    @staticmethod
    def _normalize_entry_fields(entries: List[Dict[str, Any]]) -> None:
        """统一条目字段：旧文件使用 "size"，/opt 规范词表使用 "size_info"。"""
        for entry in entries:
            if "size" in entry and "size_info" not in entry:
                entry["size_info"] = entry.pop("size")
            entry.setdefault("size_info", "")

    def _save(self):
        """持久化词表（按 level/path 排序后写入），带锁防止并发写冲突。
        写入格式与 /opt 规范词表保持一致：扁平条目列表，使用 size_info 字段。"""
        with self._save_lock:
            # 重新加载最新文件，合并可能由其他进程/线程写入的变更
            self._merge_latest_from_disk()
            self.entries.sort(key=lambda x: (x["level"], x["path"]))
            with open(self.taxonomy_path, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, ensure_ascii=False, indent=2)

    def _merge_latest_from_disk(self):
        """从磁盘重新加载 taxonomy，并把内存中新条目合并进去。"""
        if not self.taxonomy_path.exists():
            return
        try:
            with open(self.taxonomy_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 兼容两种结构：扁平条目列表或 {"entries": [...]}
            disk_entries = data if isinstance(data, list) else data.get("entries", [])
            self._normalize_entry_fields(disk_entries)
            existing_norms = {_normalize(e["canonical_name"]) for e in self.entries}
            for e in disk_entries:
                norm = _normalize(e["canonical_name"])
                if norm not in existing_norms:
                    self.entries.append(e)
                    existing_norms.add(norm)
                    self.by_canonical[norm] = e
                    for a in e.get("aliases", []):
                        na = _normalize(a)
                        if na and na not in self.by_alias:
                            self.by_alias[na] = e
        except Exception:
            pass

    def _find_deepest_contained_match(self, query: str) -> Optional[Dict[str, Any]]:
        """
        检查 query 中是否包含某个已有具体船型的 canonical_name 或 alias。
        例如 "氨燃料VLCC" 包含 "VLCC"，应匹配到 超大型原油船。
        返回 level 最深的匹配条目。
        """
        key = _normalize(query)
        if not key:
            return None

        best_entry = None
        best_level = 0
        best_len = 0

        for k, entry in list(self.by_canonical.items()) + list(self.by_alias.items()):
            if not k or len(k) < 2:
                continue
            if k in key:
                # 优先 level 深，其次匹配长度长
                if entry["level"] > best_level or (entry["level"] == best_level and len(k) > best_len):
                    best_level = entry["level"]
                    best_len = len(k)
                    best_entry = entry

        return best_entry

    def retrieve(self, query: str) -> Optional[Dict[str, Any]]:
        """
        检索最匹配的船型条目，优先返回 level 最深的条目。
        返回完整 entry 字典，便于后续取 level_1/level_2/level_3/path。
        """
        if not query or not isinstance(query, str):
            return None
        key = _normalize(query)
        if not key:
            return None

        # 1. 精确匹配 canonical_name（最深优先）
        if key in self.by_canonical:
            return self.by_canonical[key]

        # 2. 精确匹配 alias
        if key in self.by_alias:
            return self.by_alias[key]

        # 3. query 中包含已有具体船型名称（如 "氨燃料VLCC"）
        contained = self._find_deepest_contained_match(query)
        if contained:
            return contained

        # 4. 子串相似度匹配，按 (level 深优先, score 高优先) 排序
        candidates = []
        for k, entry in list(self.by_canonical.items()) + list(self.by_alias.items()):
            if not k:
                continue
            if k in key or key in k:
                common = len(k) if k in key else len(key)
                max_len = max(len(k), len(key))
                score = common / max_len if max_len else 0.0
                if k == key:
                    score += 1.0
                if key in k and len(k) > len(key):
                    score *= len(key) / len(k)
                if score >= 0.5:
                    candidates.append((entry, score))

        if candidates:
            candidates.sort(key=lambda x: (-x[0]["level"], -x[1]))
            return candidates[0][0]

        return None

    def classify(self, query: str) -> Optional[Dict[str, Any]]:
        """
        对未知船型进行分类，返回应归属的父级条目（level 1 或 level 2）。
        先关键词匹配，未命中则调用 LLM。
        """
        top_category = _keyword_classify(query)
        if not top_category:
            return None

        # 尝试找到该顶层分类下最适合的 level 2 父级
        level2_candidates = [
            e for e in self.entries
            if e["level"] == 2 and e["level_1"] == top_category
        ]
        if not level2_candidates:
            # 没有 level 2，直接归属顶层
            return self.by_canonical.get(_normalize(top_category))

        # 关键词匹配 level 2
        n = _normalize(query)
        best_l2 = None
        best_score = 0
        for l2 in level2_candidates:
            score = 0
            # canonical_name 匹配
            if _normalize(l2["canonical_name"]) in n:
                score += 5
            # aliases 匹配
            for alias in l2.get("aliases", []):
                if _normalize(alias) in n:
                    score += 3
            if score > best_score:
                best_score = score
                best_l2 = l2

        if best_l2:
            return best_l2

        # 默认返回顶层分类条目
        return self.by_canonical.get(_normalize(top_category))

    def add_entry(self, canonical_name: str, parent_entry: Dict[str, Any], aliases: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        在指定父级下新增一个具体船型（level 3），并持久化。
        parent_entry 可以是 level 1 顶层分类 或 level 2 中间层分类。
        """
        if not canonical_name or not parent_entry:
            raise ValueError("canonical_name 和 parent_entry 不能为空")

        norm_name = _normalize(canonical_name)
        if norm_name in self.by_canonical or norm_name in self.by_alias:
            return self.by_canonical.get(norm_name) or self.by_alias.get(norm_name)

        parent_level = parent_entry.get("level", 1)
        level_1 = parent_entry["level_1"] if parent_level >= 1 else parent_entry["canonical_name"]
        level_2 = parent_entry["canonical_name"] if parent_level >= 2 else None
        level_3 = canonical_name
        path = [p for p in [level_1, level_2, level_3] if p]
        parent_name = level_2 if level_2 else level_1

        entry = {
            "canonical_name": canonical_name,
            "aliases": list(aliases or []),
            "category": level_1,
            "parent": parent_name,
            "level": 3,
            "level_1": level_1,
            "level_2": level_2,
            "level_3": level_3,
            "path": path,
            "size_info": "",
            "source": "auto_extended"
        }

        self.entries.append(entry)
        self.by_canonical[norm_name] = entry
        for a in entry.get("aliases", []):
            na = _normalize(a)
            if na and na not in self.by_alias:
                self.by_alias[na] = entry

        self._save()
        print(f"[ShipTypeRetriever] 自动扩展词表: {canonical_name} -> {'/'.join(path)}")
        return entry

    def add_alias(self, query: str, target_entry: Dict[str, Any]) -> Dict[str, Any]:
        """把 query 作为别名添加到已有条目中。"""
        norm_query = _normalize(query)
        if norm_query in self.by_canonical or norm_query in self.by_alias:
            return self.by_canonical.get(norm_query) or self.by_alias.get(norm_query)

        if query not in target_entry.get("aliases", []):
            target_entry.setdefault("aliases", []).append(query)
            self.by_alias[norm_query] = target_entry
            self._save()
            print(f"[ShipTypeRetriever] 自动添加别名: {query} -> {target_entry['canonical_name']}")
        return target_entry

    def classify_and_add(self, query: str) -> Optional[Dict[str, Any]]:
        """
        未知船型的主入口：
        1. 先尝试各种匹配；
        2. 若精确匹配到 level 3 具体船型，作为别名；
        3. 若是 "动力前缀 + 基础船型" 组合（如 "氢燃料电池集装箱船"），直接返回基础船型，不新增复合条目；
        4. 若 query 包含已有具体船型名称（如 "氨燃料VLCC"），作为该具体船型的别名；
        5. 若只匹配到 level 1/2 外层分类，则把 query 作为该分类下新的 level 3 条目；
        6. 否则通过关键词归类到外层大类并新增 level 3 条目。
        返回最终命中的 entry。
        """
        if not query or not isinstance(query, str):
            return None

        norm_query = _normalize(query)

        # Guard: 防止把“特种船 + 化学品船”的误识别结果混入词表
        # 例如历史上出现的“特种船（不锈钢化学品船）”“双相不锈钢化学品船特种船”等，
        # 本质都是化学品船，不应作为新词条扩展到特种船下。
        if "特种船" in query and any(kw in query for kw in ("化学品船", "不锈钢化学品船", "双相不锈钢化学品船", "chemical tanker", "chemical")):
            chemical_entry = self.by_canonical.get(_normalize("化学品船"))
            if chemical_entry:
                return chemical_entry

        # 1. 已有精确匹配（canonical/alias）
        if norm_query in self.by_canonical or norm_query in self.by_alias:
            return self.by_canonical.get(norm_query) or self.by_alias.get(norm_query)

        # 2. 动力前缀 + 基础船型：直接返回基础船型，不加入词表
        split = _split_power_and_type(query, self)
        if split:
            return split[1]

        # 3. query 中包含已有具体船型名称，作为别名
        contained = self._find_deepest_contained_match(query)
        if contained and contained.get("level") == 3:
            return self.add_alias(query, contained)

        # 4. query 中只包含 level 1/2 分类名称，作为该分类下新的 level 3
        if contained and contained.get("level") in (1, 2):
            if norm_query == _normalize(contained["canonical_name"]):
                return contained  # query 就是分类名本身
            return self.add_entry(query, contained)

        # 5. 通过关键词归类并新增
        parent = self.classify(query)
        if parent:
            return self.add_entry(query, parent)

        return None

    def normalize(self, query: str, auto_extend: bool = True) -> str:
        """
        标准化船型名称。
        auto_extend=True 时，未知船型会自动归类并加入词表；
        auto_extend=False 时，未知船型原样返回。
        """
        norm_query = _normalize(query)

        # 1. 精确匹配（canonical 或 alias）直接返回
        exact = self.by_canonical.get(norm_query) or self.by_alias.get(norm_query)
        if exact:
            return exact["canonical_name"]

        # 2. 非精确匹配：尝试归类并加入词表
        if auto_extend:
            new_entry = self.classify_and_add(query)
            if new_entry:
                return new_entry["canonical_name"]

        # 3. 仍无法归类则原样返回
        return query

    def normalize_with_path(self, query: str, auto_extend: bool = True) -> Optional[Dict[str, Any]]:
        norm_query = _normalize(query)
        entry = self.by_canonical.get(norm_query) or self.by_alias.get(norm_query)

        if not entry and auto_extend:
            entry = self.classify_and_add(query)

        if not entry:
            return None

        return {
            "canonical_name": entry["canonical_name"],
            "level": entry["level"],
            "level_1": entry["level_1"],
            "level_2": entry["level_2"],
            "level_3": entry["level_3"],
            "path": entry["path"],
            "size_info": entry.get("size_info", ""),
        }

    def normalize_and_split(self, query: str, auto_extend: bool = True) -> Optional[Dict[str, Any]]:
        """
        标准化并拆分 "动力前缀 + 基础船型"。
        返回值包含 canonical_name、path 以及提取出的 power（若无动力前缀则为 null）。
        """
        if not query or not isinstance(query, str):
            return None

        # 先看是否是 power + type 组合
        split = _split_power_and_type(query, self)
        if split:
            power, entry = split
            return {
                "canonical_name": entry["canonical_name"],
                "level": entry["level"],
                "level_1": entry["level_1"],
                "level_2": entry["level_2"],
                "level_3": entry["level_3"],
                "path": entry["path"],
                "size_info": entry.get("size_info", ""),
                "power": power,
            }

        # 否则走普通标准化
        res = self.normalize_with_path(query, auto_extend=auto_extend)
        if res:
            res["power"] = None
        return res

    def candidates_from_text(self, text: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        从文本中召回候选船型。对每个 canonical/alias 检查是否在文本中出现，
        按 level 深优先、匹配长度长优先返回。
        """
        if not text:
            return []
        text_normalized = _normalize(text)
        hits = []
        seen = set()
        for entry in self.entries:
            canonical = entry["canonical_name"]
            names = [canonical] + entry.get("aliases", [])
            best_score = 0
            for name in names:
                n = _normalize(name)
                if not n or len(n) < 2:
                    continue
                if n in text_normalized:
                    score = len(n)
                    if score > best_score:
                        best_score = score
            if best_score > 0 and canonical not in seen:
                seen.add(canonical)
                hits.append((entry, best_score))
        hits.sort(key=lambda x: (-x[0]["level"], -x[1]))
        return [h[0] for h in hits[:top_k]]

    def format_allowed_list(self) -> str:
        """为 prompt 生成固定取值清单（精简版）。"""
        lines = []
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for e in self.entries:
            if e["level"] == 1:
                continue
            groups.setdefault(e["level_1"], []).append(e)

        for top in sorted(groups.keys()):
            lines.append(f"【{top}】")
            level2_items = [e for e in groups[top] if e["level"] == 2]
            level3_items = [e for e in groups[top] if e["level"] == 3]
            for e in level2_items:
                alias_str = ", ".join(e.get("aliases", []))
                lines.append(f"  - {e['canonical_name']}" + (f"（{alias_str}）" if alias_str else ""))
            for e in level3_items:
                alias_str = ", ".join(e.get("aliases", []))
                indent = "    - " if e.get("level_2") else "  - "
                lines.append(f"{indent}{e['canonical_name']}" + (f"（{alias_str}）" if alias_str else ""))
        return "\n".join(lines)

    def format_compact_prompt(self) -> str:
        """生成一段可直接插入 AI prompt 的固定名称说明。"""
        return (
            "ship_type 必须且只能从以下规范词表中选取最精确的一个。"
            "若原文使用英文或别名（如 VLCC、Capesize、Q-Max），请转换为对应的标准中文名。\n"
            + self.format_allowed_list()
        )

    def get_prompt_snippet(self, text: str = "", top_k: int = 5) -> str:
        """
        基于文本召回候选船型，并生成 prompt 片段。
        如果 text 为空，则只返回固定取值说明。
        """
        candidates = self.candidates_from_text(text, top_k=top_k)
        lines = [
            "【船型固定取值规则】",
            "ship_type 字段必须返回规范中文船型名，优先从以下候选中选取最精确的一个：",
        ]
        if candidates:
            for c in candidates:
                alias_str = ", ".join(c.get("aliases", [])[:3])
                path_str = " / ".join(c["path"])
                lines.append(f"  - {c['canonical_name']}" + (f"（{alias_str}）" if alias_str else "") + f"  ← {path_str}")
        else:
            lines.append("  （未从文中召回候选，请从词表中选取最精确类型）")
        lines.append("若候选均不准确，可向上选择父级（如 Q-Max → 液化天然气运输船），但禁止编造未在词表中的名称。")
        return "\n".join(lines)


# 默认实例
_default_retriever: Optional[ShipTypeRetriever] = None


def get_default_retriever() -> ShipTypeRetriever:
    global _default_retriever
    if _default_retriever is None:
        _default_retriever = ShipTypeRetriever()
    return _default_retriever


def retrieve_ship_type(query: str) -> Optional[Dict[str, Any]]:
    return get_default_retriever().retrieve(query)


def normalize_ship_type(query: str, auto_extend: bool = True) -> str:
    return get_default_retriever().normalize(query, auto_extend=auto_extend)


if __name__ == "__main__":
    r = ShipTypeRetriever()
    samples = [
        "VLCC", "Suezmax", "Aframax", "LR2", "MR", "成品油轮", "原油轮",
        "container ship", "LNG Carrier", "LPG Carrier", "VLGC", "MGC",
        "Q-Max", "Q-Flex", "Small-scale LNG Carrier",
        "苏伊士型", "Capesize", "Panamax", "Handysize", "VLOC",
        "汽车运输船", "PCTC", "滚装船", "邮轮",
        "FPSO", "AHTS", "风电运维船",
        # 未知船型，需要自动归类
        "氨燃料VLCC",
        "氢燃料电池集装箱船",
        "新型海底电缆船",
        "Unknown Ship Type",
    ]
    lines = []
    for s in samples:
        res = r.normalize_with_path(s, auto_extend=True)
        if res:
            lines.append(f"{s} -> {res['canonical_name']} (level={res['level']}, path={'/'.join(res['path'])})")
        else:
            lines.append(f"{s} -> (no match)")
    Path("ship_type_retriever_test.txt").write_text("\n".join(lines), encoding="utf-8")
    print("done")
