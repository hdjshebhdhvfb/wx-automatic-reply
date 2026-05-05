"""
SQLite 对话历史持久化模块
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chat_history.db')


def create_db():
    """创建数据库表 (首次运行时自动创建)"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS history (
                user_id   TEXT,
                role      TEXT,
                content   TEXT,
                timestamp TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')
        # 索引加速查询
        c.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_id
            ON history(user_id)
        ''')
        conn.commit()
        conn.close()
        print(f"✅ 数据库就绪: {DB_PATH}")
    except Exception as e:
        print(f"⚠️  数据库创建警告: {e}")


def add_history(user_id: str, role: str, content: str):
    """写入一条对话记录"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            'INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)',
            (user_id, role, content)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️  历史保存失败: {e}")


def get_history(user_id: str, limit: int = 20) -> list:
    """
    获取指定用户的最近 N 条历史消息。
    返回格式: [{"role": "user", "content": "你好"}, ...]
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            SELECT role, content FROM history
            WHERE user_id = ?
            ORDER BY rowid DESC
            LIMIT ?
        ''', (user_id, limit))
        rows = c.fetchall()
        rows.reverse()  # 恢复时间正序
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in rows]
    except Exception as e:
        print(f"⚠️  历史读取失败: {e}")
        return []


def clear_history(user_id: str):
    """清除指定用户的历史"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM history WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️  历史清除失败: {e}")
