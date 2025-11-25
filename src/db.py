"""
Database wrapper for SQLite
Управляет: users, downloads, priority_purchases
"""

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Tuple
import threading

from config import DB_PATH


class Database:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.init_db()

    def init_db(self):
        """Инициализация базы данных со всеми таблицами."""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Таблица пользователей
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    priority_until TIMESTAMP,
                    total_downloads INTEGER DEFAULT 0,
                    total_bytes_downloaded INTEGER DEFAULT 0
                )
            """)

            # Таблица загрузок
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS downloads (
                    download_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    video_url TEXT NOT NULL,
                    video_title TEXT,
                    file_path TEXT,
                    file_size_bytes INTEGER,
                    format TEXT,
                    status TEXT DEFAULT 'pending',
                    progress INTEGER DEFAULT 0,
                    speed_mbps REAL,
                    eta_seconds INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    error_message TEXT,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            """)

            # Таблица приоритетных покупок (для подтверждения админом)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS priority_purchases (
                    purchase_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount_usd REAL,
                    status TEXT DEFAULT 'pending',
                    confirmed_at TIMESTAMP,
                    priority_until TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            """)

            conn.commit()
            conn.close()

    def get_connection(self):
        """Получить подключение к БД."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # Возвращать результаты как dict-like объекты
        return conn

    # ==================== Пользователи ====================

    def add_or_update_user(self, user_id: int, username: str = None, first_name: str = None) -> None:
        """Добавить или обновить пользователя."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (user_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(?, username),
                    first_name = COALESCE(?, first_name)
            """, (user_id, username, first_name, username, first_name))
            conn.commit()
            conn.close()

    def get_user(self, user_id: int) -> Optional[Dict]:
        """Получить информацию о пользователе."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            conn.close()
            if result:
                return {
                    "user_id": result[0],
                    "username": result[1],
                    "first_name": result[2],
                    "joined_at": result[3],
                    "priority_until": result[4],
                    "total_downloads": result[5],
                    "total_bytes_downloaded": result[6],
                }
            return None

    def has_priority(self, user_id: int) -> bool:
        """Проверить, есть ли активный приоритет у пользователя."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT priority_until FROM users WHERE user_id = ?",
                (user_id,)
            )
            result = cursor.fetchone()
            conn.close()
            if result and result[0]:
                # INFINITE - всегда есть приоритет
                if result[0] == "INFINITE":
                    return True
                try:
                    return datetime.fromisoformat(result[0]) > datetime.now()
                except ValueError:
                    # Если не isoformat, считаем нет приоритета
                    return False
            return False

    def set_priority(self, user_id: int, days: int) -> None:
        """Установить приоритет на пользователя на N дней.
        Если days < 0 то приоритет бесконечный (INFINITE).
        """
        if days < 0:
            # Бесконечный приоритет
            priority_until = "INFINITE"
        else:
            priority_until = (datetime.now() + timedelta(days=days)).isoformat()
        
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET priority_until = ? WHERE user_id = ?",
                (priority_until, user_id)
            )
            conn.commit()
            conn.close()

    def admin_give_priority(self, user_id: int, days: int) -> bool:
        """Админ выдает приоритет на N дней. Возвращает True если успешно."""
        try:
            self.add_or_update_user(user_id)
            self.set_priority(user_id, days)
            return True
        except Exception as e:
            print(f"Error giving priority: {e}")
            return False

    def admin_remove_priority(self, user_id: int) -> bool:
        """Админ забирает приоритет. Возвращает True если успешно."""
        try:
            with self.lock:
                conn = self.get_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET priority_until = NULL WHERE user_id = ?",
                    (user_id,)
                )
                conn.commit()
                conn.close()
            return True
        except Exception as e:
            print(f"Error removing priority: {e}")
            return False

    def get_priority_duration(self, user_id: int) -> Optional[str]:
        """Получить, сколько дней осталось приоритета."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT priority_until FROM users WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            conn.close()
            if result and result[0]:
                # Спецсимвол INFINITE для бесконечного приоритета
                if result[0] == "INFINITE":
                    return "∞ Бесконечный"

                try:
                    priority_until = datetime.fromisoformat(result[0])
                    remaining = priority_until - datetime.now()
                    if remaining.total_seconds() > 0:
                        days = remaining.days
                        hours = remaining.seconds // 3600
                        return f"{days}д {hours}ч"
                    return "0д"
                except ValueError:
                    return None
            return None

    def get_users_with_priority(self) -> List[Dict]:
        """Получить всех пользователей с активным приоритетом."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id, username, first_name, priority_until, total_downloads
                FROM users
                WHERE priority_until IS NOT NULL
                ORDER BY 
                    CASE WHEN priority_until = 'INFINITE' THEN 0 ELSE 1 END,
                    priority_until DESC
            """)
            results = cursor.fetchall()
            conn.close()
            
            users_list = []
            for r in results:
                priority_display = "∞ Бесконечный" if r[3] == "INFINITE" else r[3]
                users_list.append({
                    "user_id": r[0],
                    "username": r[1],
                    "first_name": r[2],
                    "priority_until": priority_display,
                    "total_downloads": r[4],
                })
            return users_list

    # ==================== Загрузки ====================

    def add_download(self, user_id: int, video_url: str, video_title: str = None, format_type: str = None) -> int:
        """Добавить новую задачу загрузки. Возвращает download_id."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO downloads (user_id, video_url, video_title, format, status)
                VALUES (?, ?, ?, ?, 'pending')
            """, (user_id, video_url, video_title, format_type))
            conn.commit()
            download_id = cursor.lastrowid
            conn.close()
            return download_id

    def get_download(self, download_id: int) -> Optional[Dict]:
        """Получить информацию о загрузке."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM downloads WHERE download_id = ?", (download_id,))
            result = cursor.fetchone()
            conn.close()
            if result:
                return {
                    "download_id": result[0],
                    "user_id": result[1],
                    "video_url": result[2],
                    "video_title": result[3],
                    "file_path": result[4],
                    "file_size_bytes": result[5],
                    "format": result[6],
                    "status": result[7],
                    "progress": result[8],
                    "speed_mbps": result[9],
                    "eta_seconds": result[10],
                    "created_at": result[11],
                    "completed_at": result[12],
                    "error_message": result[13],
                }
            return None

    def update_download_progress(self, download_id: int, progress: int, speed_mbps: float = None, eta_seconds: int = None) -> None:
        """Обновить прогресс загрузки."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE downloads
                SET progress = ?, speed_mbps = ?, eta_seconds = ?
                WHERE download_id = ?
            """, (progress, speed_mbps, eta_seconds, download_id))
            conn.commit()
            conn.close()

    def update_download_status(self, download_id: int, status: str, file_path: str = None, 
                              file_size_bytes: int = None, error_message: str = None) -> None:
        """Обновить статус загрузки."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            completed_at = datetime.now().isoformat() if status == "completed" else None
            cursor.execute("""
                UPDATE downloads
                SET status = ?, file_path = ?, file_size_bytes = ?, error_message = ?, completed_at = ?
                WHERE download_id = ?
            """, (status, file_path, file_size_bytes, error_message, completed_at, download_id))
            conn.commit()
            conn.close()

    def get_user_active_downloads(self, user_id: int) -> List[Dict]:
        """Получить активные загрузки пользователя."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM downloads
                WHERE user_id = ? AND status IN ('pending', 'downloading', 'converting', 'sending')
                ORDER BY created_at
            """, (user_id,))
            results = cursor.fetchall()
            conn.close()
            return [
                {
                    "download_id": r[0],
                    "user_id": r[1],
                    "video_url": r[2],
                    "video_title": r[3],
                    "file_path": r[4],
                    "file_size_bytes": r[5],
                    "format": r[6],
                    "status": r[7],
                    "progress": r[8],
                }
                for r in results
            ]

    def get_all_pending_downloads(self) -> List[Dict]:
        """Получить все ожидающие загрузки, отсортированные по приоритету."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            # Приоритетные пользователи сортируются первыми
            cursor.execute("""
                SELECT d.* FROM downloads d
                JOIN users u ON d.user_id = u.user_id
                WHERE d.status = 'pending'
                ORDER BY
                    CASE WHEN u.priority_until = 'INFINITE' OR (u.priority_until IS NOT NULL AND u.priority_until > datetime('now')) THEN 0 ELSE 1 END,
                    d.created_at
            """)
            results = cursor.fetchall()
            conn.close()
            return [
                {
                    "download_id": r[0],
                    "user_id": r[1],
                    "video_url": r[2],
                    "video_title": r[3],
                    "file_path": r[4],
                    "file_size_bytes": r[5],
                    "format": r[6],
                    "status": r[7],
                    "progress": r[8],
                }
                for r in results
            ]

    def count_active_downloads(self) -> int:
        """Подсчитать активные загрузки (downloading, converting, sending)."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM downloads
                WHERE status IN ('downloading', 'converting', 'sending')
            """)
            count = cursor.fetchone()[0]
            conn.close()
            return count

    def get_completed_download_by_url_format(self, video_url: str, format_type: str) -> Optional[Dict]:
        """Найти завершенную загрузку по URL и формату."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM downloads
                WHERE video_url = ? AND format = ? AND status = 'completed' AND file_path IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT 1
            """, (video_url, format_type))
            result = cursor.fetchone()
            conn.close()
            if result:
                return {
                    "download_id": result[0],
                    "user_id": result[1],
                    "video_url": result[2],
                    "video_title": result[3],
                    "file_path": result[4],
                    "file_size_bytes": result[5],
                    "format": result[6],
                    "status": result[7],
                    "progress": result[8],
                    "speed_mbps": result[9],
                    "eta_seconds": result[10],
                    "created_at": result[11],
                    "completed_at": result[12],
                    "error_message": result[13],
                }
            return None

    # ==================== Приоритетные покупки ====================

    def add_priority_purchase(self, user_id: int, amount_usd: float) -> int:
        """Добавить запрос на покупку приоритета. Возвращает purchase_id."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO priority_purchases (user_id, amount_usd, status)
                VALUES (?, ?, 'pending')
            """, (user_id, amount_usd))
            conn.commit()
            purchase_id = cursor.lastrowid
            conn.close()
            return purchase_id

    def get_priority_purchase(self, purchase_id: int) -> Optional[Dict]:
        """Получить информацию о покупке приоритета."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM priority_purchases WHERE purchase_id = ?", (purchase_id,))
            result = cursor.fetchone()
            conn.close()
            if result:
                return {
                    "purchase_id": result[0],
                    "user_id": result[1],
                    "amount_usd": result[2],
                    "status": result[3],
                    "confirmed_at": result[4],
                    "priority_until": result[5],
                    "created_at": result[6],
                }
            return None

    def get_pending_priority_purchases(self) -> List[Dict]:
        """Получить все ожидающие покупки приоритета для админа."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM priority_purchases
                WHERE status = 'pending'
                ORDER BY created_at
            """)
            results = cursor.fetchall()
            conn.close()
            return [
                {
                    "purchase_id": r[0],
                    "user_id": r[1],
                    "amount_usd": r[2],
                    "status": r[3],
                    "confirmed_at": r[4],
                    "priority_until": r[5],
                    "created_at": r[6],
                }
                for r in results
            ]

    def confirm_priority_purchase(self, purchase_id: int, priority_days: int) -> None:
        """Подтвердить покупку приоритета и активировать его."""
        priority_until = (datetime.now() + timedelta(days=priority_days)).isoformat()
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Получить user_id из покупки
            cursor.execute("SELECT user_id FROM priority_purchases WHERE purchase_id = ?", (purchase_id,))
            user_id = cursor.fetchone()[0]
            
            # Обновить статус покупки
            cursor.execute("""
                UPDATE priority_purchases
                SET status = 'confirmed', confirmed_at = ?, priority_until = ?
                WHERE purchase_id = ?
            """, (datetime.now().isoformat(), priority_until, purchase_id))
            
            # Обновить приоритет пользователя
            cursor.execute("""
                UPDATE users
                SET priority_until = ?
                WHERE user_id = ?
            """, (priority_until, user_id))
            
            conn.commit()
            conn.close()

    def reject_priority_purchase(self, purchase_id: int) -> None:
        """Отклонить покупку приоритета."""
        with self.lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE priority_purchases
                SET status = 'rejected'
                WHERE purchase_id = ?
            """, (purchase_id,))
            conn.commit()
            conn.close()


# Глобальный экземпляр БД
db = Database()
