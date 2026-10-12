# -*- coding: utf-8 -*-
"""
置信度配置模块

用于管理不同新闻来源的置信度计算
"""

# 模块名称到平台名称的映射
MODULE_PLATFORM_MAP = {
    # 不需要VPN的爬虫
    "scrapping_chineseshipping_links": "中华航运网",
    "scrapping_cosco_shipping_links": "中远海运",
    "scrapping_coscon_links": "中远海运集运",
    "scrapping_csi_links": "中国船舶工业",
    "scrapping_cssc_links": "中国船舶集团",
    "scrapping_eworldship_links": "国际船舶网",
    "scrapping_imarine_links": "iMarine",
    "scrapping_shippingchina_links": "航运界",
    "scrapping_shippingonline_links": "中华航运网",
    "scrapping_clarksons_links": "克拉克森",
    "scrapping_maersk_links": "马士基",
    "scrapping_marinecircle_links": "海运圈聚焦",
    "scrapping_drewry_links": "德路里",
    # 需要VPN的爬虫
    "scrapping_seatrade_links": "Seatrade",
    "scrapping_splash247_links": "Splash247",
    "scrapping_tradewinds_links": "TradeWinds",
    # 船厂网站爬虫
    "新时代造船": "新时代造船",
    "扬子江船业": "扬子江船业",
    "北海造船": "北海造船",
    "大连船舶重工": "大连船舶重工",
    "外高桥造船": "外高桥造船",
    "芜湖造船厂": "芜湖造船厂",
    "黄海造船": "黄海造船",
    "蓬莱中柏京鲁船业": "蓬莱中柏京鲁船业",
    "苏美达船舶": "苏美达船舶",
    "山海关船舶重工": "山海关船舶重工",
    "武昌船舶重工": "武昌船舶重工",
    "象屿海装": "象屿海装",
    "扬子三井": "扬子三井造船",
    "南通中远海运川崎船舶": "南通中远海运川崎",
    "大连中远海运川崎船舶": "大连中远海运川崎",
    "扬州中远海运重工": "扬州中远海运重工",
    "厦门船舶重工": "厦门船舶重工",
    "常石集团": "常石集团",
    "舟山长宏": "舟山长宏",
    "招商局船舶": "招商局船舶",
    "中集来福士": "中集来福士",
    "马尾造船": "马尾造船",
    "江苏镇江": "江苏镇江",
    "浙江新乐造船": "浙江新乐造船",
    "泰州口岸船舶": "泰州口岸船舶",
    "江苏海通": "江苏海通",
}

# 公司官方渠道（官方机构/船厂/公司官网，置信度始终100%）
# 说明：非公司官方渠道的其他新闻来源统一评80分，TradeWinds 单独评60分。
ABSOLUTE_TRUTH_PLATFORMS = {
    "中国船舶集团",
    "中远海运",
    "新时代造船",  # 船厂官网视为绝对真实来源
    "扬子江船业",  # 船厂官网视为绝对真实来源
    "北海造船",  # 船厂官网视为绝对真实来源
    "大连船舶重工",  # 船厂官网视为绝对真实来源
    "外高桥造船",  # 船厂官网视为绝对真实来源
    "芜湖造船厂",  # 船厂官网视为绝对真实来源
    "黄海造船",  # 船厂官网视为绝对真实来源
    "蓬莱中柏京鲁船业",  # 船厂官网视为绝对真实来源
    "苏美达船舶",  # 船厂官网视为绝对真实来源
    "山海关船舶重工",  # 船厂官网视为绝对真实来源
    "武昌船舶重工",  # 船厂官网视为绝对真实来源
    "象屿海装",  # 船厂官网视为绝对真实来源
    "扬子三井造船",  # 船厂官网视为绝对真实来源
    "南通中远海运川崎",  # 船厂官网视为绝对真实来源
    "大连中远海运川崎",  # 船厂官网视为绝对真实来源
    "扬州中远海运重工",  # 船厂官网视为绝对真实来源
    "厦门船舶重工",  # 船厂官网视为绝对真实来源
    "常石集团",  # 船厂官网视为绝对真实来源
    "舟山长宏",  # 船厂官网视为绝对真实来源
    "招商局船舶",  # 船厂官网视为绝对真实来源
    "中集来福士",  # 船厂官网视为绝对真实来源
    "马尾造船",  # 船厂官网视为绝对真实来源
    "江苏镇江",  # 船厂官网视为绝对真实来源
    "浙江新乐造船",  # 船厂官网视为绝对真实来源
    "泰州口岸船舶",  # 船厂官网视为绝对真实来源
    "江苏海通",  # 船厂官网视为绝对真实来源
}

# 官方微信公众号账号（置信度 100）
# 用于 is_absolute_truth 的前缀匹配，兼容 BASE_CONFIDENCE 中的固定配置。
OFFICIAL_WECHAT_ACCOUNTS = {
    "大船集团",
    "北海造船",
    "广济船舶重工",
    "广船国际",
    "沪东中华造船",
    "沪东中华",  # 实际公众号订阅名
    "恒力集团",
    "江南造船",
    "马士基集团",
    "蓬莱中柏京鲁船业有限公司",
    "浦银金融租赁有限公司",
    "SWS外高桥造船",
    "sws外高桥造船",  # 实际公众号订阅名（大小写差异）
    "扬州国裕船舶制造有限公司",
    "中远海运散运",
    "中船澄西员工之家",
    "中谷物流",
    "中联航运",
    "中国船舶工业贸易有限公司",
}

# 船厂官网域名到平台名称的映射（用于 URL 兜底识别）
# 所有船厂官网的新闻网页来源均视为官方来源，置信度 100。
SHIPYARD_OFFICIAL_DOMAINS = {
    "zschship.com": "舟山长宏",
    "cmindustry.com.hk": "招商局船舶",
    "shgsic.com": "山海关船舶重工",
    "chinasws.com": "外高桥造船",
    "cnkasc.com": "泰州口岸船舶",
    "ncship.com.cn": "新时代造船",
    "yzjship.com": "扬子江船业",
    "tsuneishi.co.jp": "常石集团",
    "maweiship.com": "马尾造船",
    "zjshipyard.com": "江苏镇江",
    "nt-ship.com": "江苏海通",
    "huanghaiship.com": "黄海造船",
    "zh.huanghaiship.com": "黄海造船",
    "wuchuan.com.cn": "武昌船舶重工",
    "nacks.com": "南通中远海运川崎",
    "yamicship.com": "扬子三井造船",
    "dacks.com.cn": "大连中远海运川崎",
    "bhshipyard.com.cn": "北海造船",
    "xlshipbuilding.com": "浙江新乐造船",
    "xsi.com.cn": "厦门船舶重工",
    "xysoe.com": "象屿海装",
    "jinglushipyard.com": "蓬莱中柏京鲁船业",
    "yz.chi.coscoshipping.com": "扬州中远海运重工",
    "sumecmarine.com": "苏美达船舶",
    "cimc-raffles.com": "中集来福士",
    "wuhu.com.cn": "芜湖造船厂",
    "dsic.cn": "大连船舶重工",
}

# 基础置信度配置（百分比）
# 规则：公司官方渠道 100 分，TradeWinds 60 分，其他新闻来源统一 80 分。
BASE_CONFIDENCE = {
    # 公司官方/权威来源
    "中国船舶集团": 100,
    "中远海运": 100,
    "中远海运集运": 100,
    "中国船舶工业": 100,
    "马士基": 100,
    # 船厂官网（同 ABSOLUTE_TRUTH_PLATFORMS）
    "新时代造船": 100,
    "扬子江船业": 100,
    "北海造船": 100,
    "大连船舶重工": 100,
    "外高桥造船": 100,
    "芜湖造船厂": 100,
    "黄海造船": 100,
    "蓬莱中柏京鲁船业": 100,
    "苏美达船舶": 100,
    "山海关船舶重工": 100,
    "武昌船舶重工": 100,
    "象屿海装": 100,
    "扬子三井造船": 100,
    "南通中远海运川崎": 100,
    "大连中远海运川崎": 100,
    "扬州中远海运重工": 100,
    "厦门船舶重工": 100,
    "常石集团": 100,
    "舟山长宏": 100,
    "招商局船舶": 100,
    "中集来福士": 100,
    "马尾造船": 100,
    "江苏镇江": 100,
    "浙江新乐造船": 100,
    "泰州口岸船舶": 100,
    "江苏海通": 100,
    # 专业媒体
    "克拉克森": 80,
    "德路里": 80,
    "iMarine": 80,
    "TradeWinds": 60,
    "Splash247": 80,
    "Seatrade": 80,
    # 行业媒体
    "国际船舶网": 80,
    "中华航运网": 80,
    "航运界": 80,
    "海运圈聚焦": 80,
    # 微信公众号默认
    "微信公众号": 80,
    # 官方船厂/公司机构公众号（公司官方渠道，置信度 100）
    "微信公众号:大船集团": 100,
    "微信公众号:北海造船": 100,
    "微信公众号:广济船舶重工": 100,
    "微信公众号:广船国际": 100,
    "微信公众号:沪东中华造船": 100,
    "微信公众号:沪东中华": 100,  # 实际公众号订阅名
    "微信公众号:恒力集团": 100,
    "微信公众号:江南造船": 100,
    "微信公众号:马士基集团": 100,
    "微信公众号:蓬莱中柏京鲁船业有限公司": 100,
    "微信公众号:浦银金融租赁有限公司": 100,
    "微信公众号:SWS外高桥造船": 100,
    "微信公众号:sws外高桥造船": 100,  # 实际公众号订阅名（大小写差异）
    "微信公众号:扬州国裕船舶制造有限公司": 100,
    "微信公众号:中远海运散运": 100,
    "微信公众号:中船澄西员工之家": 100,
    "微信公众号:中谷物流": 100,
    "微信公众号:中联航运": 100,
    "微信公众号:中国船舶工业贸易有限公司": 100,
    # 行业媒体公众号（非官方渠道，置信度 80）
    "微信公众号:船舶海工圈": 80,
    "微信公众号:国际船舶海工网": 80,
    "微信公众号:国际船舶在线": 80,
    "微信公众号:海事早知道": 80,
    "微信公众号:海丰经纪": 80,
    "微信公众号:航运评论": 80,
    "微信公众号:老杨说船": 80,
    "微信公众号:《贸易风》海事周报": 80,
    "微信公众号:明辉说油": 80,
    "微信公众号:前海融资租赁俱乐部": 80,
    "微信公众号:人船情未了": 80,
    "微信公众号:SSY辛普森航运": 80,
    "微信公众号:信德海事": 80,
    "微信公众号:洲际船务": 80,
    "微信公众号:中国船舶工业行业协会": 80,
    "微信公众号:中国远洋海运e刊": 80,
    "微信公众号:中国船舶报": 80,
    # 默认
    "default": 80,
}

# 重复次数对置信度的增益配置（已停用，保留结构以兼容旧代码）
# 新规则下置信度仅按来源平台计算，不再随重复次数提升。
DUPLICATE_CONFIDENCE_BOOST = [
    (1, 1, 0),      # 1次: 无加成
    (2, 2, 0),      # 2次: 无加成
    (3, 3, 0),      # 3次: 无加成
    (4, 5, 0),      # 4-5次: 无加成
    (6, 10, 0),     # 6-10次: 无加成
    (11, float('inf'), 0),  # 11次以上: 无加成
]


def _normalize_platform_name(platform_name):
    """
    规范化平台名称，兼容全角/半角冒号等常见差异。
    """
    if not platform_name:
        return ""
    # 统一全角冒号为半角冒号，避免 "微信公众号：xxx" 无法匹配 "微信公众号:xxx"
    return platform_name.replace("：", ":").strip()


def get_module_platform(module_name):
    """
    根据模块名称获取平台名称
    
    Args:
        module_name: 爬虫模块名称
        
    Returns:
        str: 平台名称
    """
    return MODULE_PLATFORM_MAP.get(module_name, module_name)


def calculate_confidence(platform_name, duplicate_count=1, url=None):
    """
    计算新闻的置信度

    新规则：
    - 公司官方渠道（ABSOLUTE_TRUTH_PLATFORMS、官方公众号、船厂官网域名）: 100
    - TradeWinds: 60
    - 其他新闻来源: 80

    Args:
        platform_name: 平台名称
        duplicate_count: 重复次数（已停用，保留参数以兼容旧调用）
        url: 新闻链接（可选），用于兜底识别船厂官网域名

    Returns:
        int: 置信度百分比 (0-100)
    """
    platform_name = _normalize_platform_name(platform_name)

    # 公司官方渠道始终 100
    if is_absolute_truth(platform_name, url=url):
        return 100

    # 按平台返回固定分值
    return BASE_CONFIDENCE.get(platform_name, BASE_CONFIDENCE["default"])


def is_absolute_truth(platform_name, url=None):
    """
    判断平台是否为绝对真实（官方权威来源）

    支持三种识别方式：
    1. platform_name 精确匹配 ABSOLUTE_TRUTH_PLATFORMS
    2. platform_name 为"微信公众号:{官方账号}"时，账号名在 OFFICIAL_WECHAT_ACCOUNTS 中
    3. url 域名匹配船厂官网域名 SHIPYARD_OFFICIAL_DOMAINS

    Args:
        platform_name: 平台名称
        url: 新闻链接（可选）

    Returns:
        bool: 是否为绝对真实
    """
    platform_name = _normalize_platform_name(platform_name)

    # 1. 精确匹配官方平台
    if platform_name in ABSOLUTE_TRUTH_PLATFORMS:
        return True

    # 2. 官方微信公众号前缀匹配
    if platform_name.startswith("微信公众号:"):
        account = platform_name[len("微信公众号:"):].strip()
        if account in OFFICIAL_WECHAT_ACCOUNTS:
            return True

    # 3. 通过 URL 域名识别船厂官网
    if url:
        detected = detect_platform_from_url(url)
        if detected and detected in ABSOLUTE_TRUTH_PLATFORMS:
            return True

    return False


# URL 域名到平台名称的映射（用于 source_platform 缺失/unknown 时的兜底识别）
URL_PLATFORM_MAP = {
    "eworldship.com": "国际船舶网",
    "imarine.cn": "iMarine",
    "chineseshipping.com.cn": "中华航运网",
    "shippingonline.cn": "中华航运网",
    "shippingchina.com": "航运界",
    "cssc.net.cn": "中国船舶集团",
    "csi.com.cn": "中国船舶工业",
    "coscoshipping.com": "中远海运",
    "coscon.com": "中远海运集运",
    "maersk.com": "马士基",
    "clarksons.net": "克拉克森",
    "drewry.com": "德路里",
    "seatrade-maritime.com": "Seatrade",
    "splash247.com": "Splash247",
    "tradewindsnews.com": "TradeWinds",
    "marinecircle.com": "海运圈聚焦",
    # 船厂官网域名（官方来源）
    **SHIPYARD_OFFICIAL_DOMAINS,
}


def detect_platform_from_url(url):
    """
    根据 URL 域名识别新闻来源平台。
    返回识别到的平台名称，无法识别时返回空字符串。
    """
    if not url:
        return ""
    url_lower = str(url).lower()
    # 去掉协议头、www. 等前缀，提取域名主体
    import re
    match = re.search(r"https?://(?:www\.)?([^/]+)", url_lower)
    if not match:
        return ""
    domain = match.group(1)
    # 先尝试完整域名匹配
    if domain in URL_PLATFORM_MAP:
        return URL_PLATFORM_MAP[domain]
    # 再尝试子串匹配（如 xxx.eworldship.com）
    for key_domain, platform in URL_PLATFORM_MAP.items():
        if key_domain in domain:
            return platform
    return ""


# 获取平台可信度等级
def get_platform_tier(platform_name):
    """
    获取平台的可信度等级

    Args:
        platform_name: 平台名称

    Returns:
        int: 等级 (1-5, 5为最高)
    """
    confidence = calculate_confidence(platform_name)

    if confidence >= 95:
        return 5
    elif confidence >= 80:
        return 4
    elif confidence >= 60:
        return 3
    elif confidence >= 40:
        return 2
    else:
        return 1
