# -*- coding: utf-8 -*-
"""
船型名称标准化模块

1. 从 ship_type_taxonomy.json 加载标准船型名称映射。
2. 仅对 data 字段中的 ship_type 做标准化。
3. 由新造船订单、买卖船、租船、船舶建造共用。
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

# 兼容旧版环境变量，但不再实际读取 Excel/JSON 缓存
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_EXCEL_PATH = os.getenv('SHIP_TYPE_EXCEL_PATH', str(DATA_DIR / "船型名称表_完整版.xlsx"))
FALLBACK_EXCEL_PATH = os.getenv('SHIP_TYPE_FALLBACK_EXCEL_PATH', str(DATA_DIR / "船型名称表.xlsx"))

# 常见船型兜底映射（优先级低于 Excel）
_DEFAULT_SHIP_TYPE_MAP = {
    # 通用/顶层
    "container ship": "集装箱船",
    "bulk carrier": "散货船",
    "tanker": "油轮",
    "oil tanker": "原油轮",
    "crude oil tanker": "原油轮",
    "product tanker": "成品油轮",
    "成品油轮": "成品油轮",
    "原油轮": "原油轮",
    "化学品船": "化学品船",
    "特种液货船": "特种液货船",
    "液化天然气运输船": "液化天然气运输船",
    "液化石油气运输船": "液化石油气运输船",
    "海工辅助船": "海工辅助船",
    "客船": "客船",
    "工程船": "工程船",
    "港作船": "港作船",
    "杂货船": "杂货船",
    # 具体类型
    "lng carrier": "液化天然气运输船",
    "lpg carrier": "液化石油气运输船",
    "car carrier": "汽车运输船",
    "vehicle carrier": "汽车/卡车运输船",
    "pctc": "汽车/卡车运输船",
    "pcc": "汽车运输船",
    "mpv": "多用途船",
    "multipurpose vessel": "多用途船",
    "chemical tanker": "化学品船",
    "offshore platform": "海工平台",
    "cable layer": "海缆敷设船",
    "fpso": "浮式生产储卸油装置",
    "flng": "浮式液化天然气装置",
    "ahts": "三用工作船",
    "psv": "平台供应船",
    "ctv": "风电运维船",
    "sovc": "风电运维船",
    "drillship": "钻井船",
    "semi-submersible": "半潜式钻井平台",
    "jack-up": "自升式钻井平台",
    "vlcc": "超大型原油船",
    "ulcc": "巨型原油船",
    "suezmax": "苏伊士型原油船",
    "aframax": "阿芙拉型原油船",
    "panamax": "巴拿马型散货船",
    "kamsarmax": "巴拿马型散货船",
    "capesize": "好望角型散货船",
    "newcastlemax": "纽卡斯尔型散货船",
    "handysize": "灵便型散货船",
    "handymax": "大灵便型散货船",
    "supramax": "超灵便型散货船",
    "ultramax": "极超灵便型散货船",
    "vloc": "超大型矿砂船",
    "vlgc": "超大型液化石油气船",
    "lgc": "大型液化石油气船",
    "mgc": "中型液化石油气船",
    "ulcv": "超大型集装箱船",
    "q-max": "Q-Max型LNG船",
    "q-flex": "Q-Flex型LNG船",
    "lr2": "远程2型成品油轮",
    "lr1": "远程1型成品油轮",
    "mr": "中程成品油轮",
    "general purpose": "灵便型成品油轮",
    "refrigerated cargo ship": "冷藏船",
    "ro-ro": "滚装船",
    "ferry": "渡轮",
    "cruise ship": "邮轮",
}

_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")

# 硬规则：当原文明确是化学品船相关表述，但标准化结果被误归为“特种船”时进行纠偏
_CHEMICAL_TANKER_KEYWORDS = [
    "化学品船",
    "化学品运输船",
    "chemical tanker",
    "chemical carrier",
    "不锈钢化学品船",
    "双相不锈钢化学品船",
    "duplex stainless steel chemical",
]


def _is_chinese(name: str) -> bool:
    return bool(_CHINESE_RE.search(name))


class ShipTypeNameNormalizer:
    def __init__(self, excel_path: str = DEFAULT_EXCEL_PATH):
        # excel_path 保留参数以兼容旧调用，但不再读取 Excel/JSON 缓存
        self.excel_path = excel_path
        self._map: Optional[Dict[str, str]] = None
        self._retriever = None

    def _get_retriever(self):
        if self._retriever is None:
            try:
                from ship_type_retriever import ShipTypeRetriever
                self._retriever = ShipTypeRetriever()
            except Exception as e:
                print(f"[ShipTypeNormalizer] 层级检索器加载失败: {e}，回退到 taxonomy/兜底映射")
                self._retriever = False
        return self._retriever

    def _load_map(self) -> Dict[str, str]:
        """从层级分类表构建别名->标准名映射，并叠加默认兜底映射。"""
        if self._map is not None:
            return self._map

        raw_map: Dict[str, str] = {}

        # 1) 优先从 ship_type_taxonomy.json 构建
        try:
            from ship_type_retriever import ShipTypeRetriever
            retriever = ShipTypeRetriever()
            for entry in retriever.entries:
                canonical = entry.get("canonical_name")
                if not canonical:
                    continue
                raw_map[canonical] = canonical
                for alias in entry.get("aliases", []):
                    if alias:
                        raw_map[alias] = canonical
        except Exception as e:
            print(f"[ShipTypeNormalizer] 从 taxonomy 构建映射失败: {e}")

        # 2) 叠加默认兜底映射（taxonomy 中不存在的才补充）
        for k, v in _DEFAULT_SHIP_TYPE_MAP.items():
            if k not in raw_map:
                raw_map[k] = v

        self._map = {}
        for k, v in raw_map.items():
            if not k:
                continue
            self._map[k.lower()] = v
        return self._map

    def normalize(self, raw_name: Optional[str]) -> Optional[str]:
        """标准化单个船型名称。优先使用层级检索器自底向上匹配，未命中再回退到 Excel/兜底映射。"""
        if not raw_name or not isinstance(raw_name, str):
            return raw_name

        raw_name = raw_name.strip()
        if not raw_name:
            return raw_name

        # 1) 优先使用层次化检索器（自底向上优先匹配最深层；未知船型自动归类并加入词表）
        retriever = self._get_retriever()
        if retriever:
            return retriever.normalize(raw_name, auto_extend=True)

        # 2) 回退到 Excel/兜底映射
        mapping = self._load_map()
        key = raw_name.lower()

        # 精确匹配
        if key in mapping:
            return mapping[key]

        # 去掉常见后缀/单位的模糊匹配
        cleaned_key = re.sub(r"\s*(ship|vessel|carrier|tanker|boats?)\s*$", "", key, flags=re.IGNORECASE).strip()
        if cleaned_key and cleaned_key in mapping:
            return mapping[cleaned_key]

        # 子串匹配：使用相似度评分，选择最佳匹配
        best_score = 0.0
        best_value = None
        for k, v in mapping.items():
            if not k:
                continue
            if k in key or key in k:
                common_len = len(k) if k in key else len(key)
                max_len = max(len(k), len(key))
                score = common_len / max_len if max_len > 0 else 0.0
                if k == key:
                    score += 1.0
                if (k in key or key in k) and len(k) > len(key):
                    score *= len(key) / len(k)
                if score > best_score:
                    best_score = score
                    best_value = v

        if best_value and best_score >= 0.5:
            return best_value

        return raw_name

    def normalize_with_power(self, raw_name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """标准化船型名称并拆分动力前缀。返回 (canonical_name, power)。"""
        if not raw_name or not isinstance(raw_name, str):
            return raw_name, None

        raw_name = raw_name.strip()
        if not raw_name:
            return raw_name, None

        # 优先使用层级检索器（支持 power + type 拆分）
        retriever = self._get_retriever()
        if retriever:
            result = retriever.normalize_and_split(raw_name, auto_extend=True)
            if result:
                return result["canonical_name"], result.get("power")

        # 回退到普通标准化
        return self.normalize(raw_name), None

    def normalize_json_data(self, data: Any) -> Any:
        """
        递归处理 data JSON，对 ship_type 字段做标准化，并拆分动力前缀到 power 字段。
        """
        if data is None:
            return data

        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                normalized = self.normalize_json_data(parsed)
                return json.dumps(normalized, ensure_ascii=False)
            except Exception:
                return data

        if isinstance(data, dict):
            result = {}
            extracted_power = None
            has_power_field = "power" in data

            for k, v in data.items():
                if str(k).lower() == "ship_type" and isinstance(v, str):
                    canonical, power = self.normalize_with_power(v)
                    # 硬规则纠偏：只要原文明确包含化学品船相关关键词，最终 ship_type 必须落到化学品船
                    v_lower = v.lower()
                    if any(kw in v_lower for kw in _CHEMICAL_TANKER_KEYWORDS):
                        canonical = "化学品船"
                    result[k] = canonical
                    if power:
                        extracted_power = power
                else:
                    result[k] = self.normalize_json_data(v)

            # 如果拆分出动力前缀且原字典没有 power 字段，自动补入
            if extracted_power and not has_power_field:
                result["power"] = extracted_power

            return result

        if isinstance(data, list):
            return [self.normalize_json_data(x) for x in data]

        return data


_default_normalizer: Optional[ShipTypeNameNormalizer] = None


def get_default_normalizer() -> ShipTypeNameNormalizer:
    global _default_normalizer
    if _default_normalizer is None:
        _default_normalizer = ShipTypeNameNormalizer()
    return _default_normalizer


def normalize_ship_type_name(raw_name: Optional[str]) -> Optional[str]:
    return get_default_normalizer().normalize(raw_name)


def normalize_ship_type_data(data: Any) -> Any:
    return get_default_normalizer().normalize_json_data(data)


def refresh_ship_type_map(excel_path: str = DEFAULT_EXCEL_PATH) -> Dict[str, str]:
    """强制重新从 taxonomy 构建映射（excel_path 仅保留兼容）。"""
    normalizer = ShipTypeNameNormalizer(excel_path=excel_path)
    return normalizer._load_map()


if __name__ == "__main__":
    n = ShipTypeNameNormalizer()
    samples = [
        "VLCC",
        "Suezmax",
        "Aframax",
        "LR2",
        "MR",
        "container ship",
        "LNG Carrier",
        "LPG Carrier",
        "成品油轮",
        "苏伊士型",
        "Capesize",
        "VLGC",
        "Unknown Ship Type",
    ]
    for s in samples:
        print(f"{s} -> {n.normalize(s)}")
