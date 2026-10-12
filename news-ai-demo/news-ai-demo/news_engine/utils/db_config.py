"""
news_ai 模块统一数据库配置
"""
import os
import sys

# 允许从项目根目录导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from config import DB_CONFIG as ROOT_DB_CONFIG
except ImportError:
    ROOT_DB_CONFIG = None

# 兼容 connect_to_database(host_name, user_name, port, user_password, db_name) 的参数格式
DB_CONFIG = {
    'host_name': os.getenv('DB_HOST', 'your-db-host.example.com'),
    'user_name': os.getenv('DB_USER', 'your-db-user'),
    'port': int(os.getenv('DB_PORT', '3306')),
    'user_password': os.getenv('DB_PASSWORD', 'your-db-password'),
    'db_name': os.getenv('DB_NAME', 'Time_Series')
}

# 兼容 mysql.connector.connect(**cfg) 的参数格式
DB_CONFIG_MYSQL = {
    'host': DB_CONFIG['host_name'],
    'user': DB_CONFIG['user_name'],
    'port': DB_CONFIG['port'],
    'password': DB_CONFIG['user_password'],
    'database': DB_CONFIG['db_name'],
    'charset': 'utf8mb4'
}
