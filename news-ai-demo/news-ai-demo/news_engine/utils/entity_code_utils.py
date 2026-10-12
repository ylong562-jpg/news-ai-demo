# -*- coding: utf-8 -*-
"""
Poplars 编码工具函数。
为公司/船厂类 entity 自动生成 12 位编码：
  船厂  -> PMY + 8 位流水号 + X
  企业  -> PME + 8 位流水号 + X
"""
import os
import sqlite3

DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'data',
    'entity_registry.db'
)

YARD_TYPES = {'shipyard'}
ENTITY_TYPES = {'company', 'shipowner', 'seller', 'buyer', 'organization'}


def _get_next_seq(cursor, prefix):
    cursor.execute("SELECT MAX(CAST(SUBSTR(code, 4, 8) AS INTEGER)) FROM entities WHERE code LIKE ?", (prefix + '%',))
    row = cursor.fetchone()
    return (row[0] or 0) + 1


def generate_code_for_entity(entity_type, db_path=None):
    """
    根据 entity_type 生成新的 Poplars 编码。
    仅对 shipyard/company/shipowner/seller/buyer/organization 生成；其它返回 None。
    使用数据库事务安全地获取下一个流水号。
    """
    if entity_type in YARD_TYPES:
        prefix = 'PMY'
    elif entity_type in ENTITY_TYPES:
        prefix = 'PME'
    else:
        return None

    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    seq = _get_next_seq(cursor, prefix)
    code = f"{prefix}{seq:08d}X"
    conn.close()
    return code
