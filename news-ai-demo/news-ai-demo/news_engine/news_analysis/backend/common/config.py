# -*- coding: utf-8 -*-
"""
news_analysis 后端配置模块
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# 默认数据库配置（优先从环境变量读取）
DEFAULT_DB_CONFIG = {
    "host": os.getenv('DB_HOST', 'your-db-host.example.com'),
    "user": os.getenv('DB_USER', 'your-db-user'),
    "password": os.getenv('DB_PASSWORD', 'your-db-password'),
    "database": os.getenv('DB_NAME', 'Time_Series'),
    "port": int(os.getenv('DB_PORT', '3306')),
    "charset": "utf8mb4",
    "connect_timeout": 30,
    "use_pure": True,
}

# 表名
NEWS_TABLE = "ship_news"
SHIPYARD_TABLE = "shipyard_data"

# 新闻管理相关（已同步为 7 个核心主维度体系）
TARGET_CATEGORIES = [
    "新造船订单", "买卖船", "租船", "船舶建造",
    "维修改装", "公司金融", "行业新闻",
]

# 需要 VPN 的新闻来源平台
VPN_NEEDED_PLATFORMS = {
    "Seatrade",
    "Splash247",
    "TradeWinds",
}

# 船厂官网平台（当前未纳入 VPN 判定，仅作登记）
SHIPYARD_WEBSITE_PLATFORMS = {
    "新时代造船",
    "扬子江船业",
    "北海造船",
    "大连船舶重工",
    "外高桥造船",
    "芜湖造船厂",
    "黄海造船",
    "蓬莱中柏京鲁船业",
    "苏美达船舶",
    "山海关船舶重工",
    "武昌船舶重工",
    "象屿海装",
    "扬子三井造船",
    "南通中远海运川崎",
    "大连中远海运川崎",
    "扬州中远海运重工",
    "厦门船舶重工",
    "常石集团",
    "舟山长宏",
    "招商局船舶",
    "中集来福士",
    "马尾造船",
    "江苏镇江",
    "浙江新乐造船",
    "泰州口岸船舶",
    "江苏海通",
}

# 默认输出目录
DEFAULT_OUTPUT_DIR = os.getenv('OUTPUT_DIR', os.path.join(BASE_DIR, 'output'))
os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)



def is_vpn_needed(platform: str) -> bool:
    """判断某个新闻来源平台是否需要 VPN。"""
    return platform in VPN_NEEDED_PLATFORMS
