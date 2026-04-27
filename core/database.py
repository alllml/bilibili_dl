"""
下载历史记录数据库

【职责】SQLite 数据库封装，提供下载记录的 CRUD 操作。
数据库文件与 config.ini 同级目录，打包后在 exe 同级目录。
所有写操作应在主线程（信号槽回调）中执行，避免并发写入问题。
"""
import os
import sqlite3
import sys
import threading
from typing import Optional


class DownloadHistoryDB:
    """下载历史记录数据库管理器"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            if getattr(sys, 'frozen', False):
                base_dir = os.path.dirname(sys.executable)
            else:
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(base_dir, "download_history.db")

        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        """创建表结构（如果不存在）"""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS download_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                url           TEXT NOT NULL,
                title         TEXT NOT NULL DEFAULT '',
                platform      TEXT NOT NULL DEFAULT '',
                owner         TEXT DEFAULT '',
                duration      INTEGER DEFAULT 0,
                quality_name  TEXT DEFAULT '',
                quality_id    INTEGER DEFAULT 0,
                file_path     TEXT NOT NULL,
                file_size     INTEGER DEFAULT 0,
                cover_url     TEXT DEFAULT '',
                status        TEXT DEFAULT 'completed',
                error_msg     TEXT DEFAULT '',
                created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_platform
                ON download_history(platform);
            CREATE INDEX IF NOT EXISTS idx_created_at
                ON download_history(created_at DESC);
        """)
        self._conn.commit()

    def add_record(self, record: dict) -> int:
        """
        插入一条下载记录。
        record 字典应包含: url, title, platform, owner, duration,
        quality_name, quality_id, file_path, file_size, cover_url, status, error_msg
        """
        with self._lock:
            cursor = self._conn.execute("""
                INSERT INTO download_history
                    (url, title, platform, owner, duration,
                     quality_name, quality_id, file_path, file_size,
                     cover_url, status, error_msg)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.get("url", ""),
                record.get("title", ""),
                record.get("platform", ""),
                record.get("owner", ""),
                record.get("duration", 0),
                record.get("quality_name", ""),
                record.get("quality_id", 0),
                record.get("file_path", ""),
                record.get("file_size", 0),
                record.get("cover_url", ""),
                record.get("status", "completed"),
                record.get("error_msg", ""),
            ))
            self._conn.commit()
            return cursor.lastrowid

    def get_records(self, platform: str = None,
                    limit: int = 100, offset: int = 0) -> list[dict]:
        """查询记录列表，支持按平台筛选和分页"""
        with self._lock:
            if platform:
                cursor = self._conn.execute(
                    "SELECT * FROM download_history WHERE platform = ? "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (platform, limit, offset))
            else:
                cursor = self._conn.execute(
                    "SELECT * FROM download_history "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset))
            return [dict(row) for row in cursor.fetchall()]

    def get_total_count(self, platform: str = None) -> int:
        """获取记录总数"""
        with self._lock:
            if platform:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM download_history WHERE platform = ?",
                    (platform,))
            else:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM download_history")
            return cursor.fetchone()[0]

    def delete_record(self, record_id: int) -> None:
        """删除单条记录"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM download_history WHERE id = ?", (record_id,))
            self._conn.commit()

    def search(self, keyword: str, limit: int = 100) -> list[dict]:
        """按标题或 UP 主名搜索"""
        with self._lock:
            pattern = f"%{keyword}%"
            cursor = self._conn.execute(
                "SELECT * FROM download_history "
                "WHERE title LIKE ? OR owner LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (pattern, pattern, limit))
            return [dict(row) for row in cursor.fetchall()]

    def close(self):
        """关闭数据库连接"""
        self._conn.close()
