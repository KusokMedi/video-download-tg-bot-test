"""
Background worker для обработки приоритетной очереди загрузок
Обрабатывает pending -> downloading -> converting -> completed
"""

import logging
import time
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional
import subprocess
import os

from config import (
    MAX_CONCURRENT_DOWNLOADS,
    PROGRESS_UPDATE_INTERVAL,
    DOWNLOAD_TIMEOUT_SECONDS,
    STORAGE_DIR,
)
from db import db
from utils import download_video, get_video_info, format_file_size, format_speed, format_eta

logger = logging.getLogger(__name__)


class QueueWorker:
    """Рабочий для обработки очереди загрузок."""
    
    def __init__(self):
        self.is_running = False
        self.thread = None
        self.active_downloads = {}  # download_id -> (start_time, total_bytes)
    
    def start(self):
        """Запустить worker."""
        if self.is_running:
            logger.warning("Worker already running")
            return
        
        self.is_running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.info("Queue worker started")
    
    def stop(self):
        """Остановить worker."""
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Queue worker stopped")
    
    def _run_loop(self):
        """Основной цикл обработки очереди."""
        while self.is_running:
            try:
                active_count = db.count_active_downloads()
                
                # Если есть место в очереди, запустить следующую задачу
                if active_count < MAX_CONCURRENT_DOWNLOADS:
                    pending = db.get_all_pending_downloads()
                    if pending:
                        download = pending[0]
                        self._process_download(download)
                
                time.sleep(2)  # Проверка каждые 2 секунды
            
            except Exception as e:
                logger.error(f"Worker error: {e}")
                time.sleep(5)
    
    def _process_download(self, download: dict):
        """Обработать одну загрузку."""
        download_id = download["download_id"]
        user_id = download["user_id"]
        video_url = download["video_url"]
        format_type = download["format"]
        
        logger.info(f"Processing download {download_id}: {video_url} ({format_type})")
        
        try:
            # Создать директорию пользователя
            user_dir = STORAGE_DIR / str(user_id)
            user_dir.mkdir(exist_ok=True)
            
            # Обновить статус на "downloading"
            db.update_download_status(download_id, "downloading")
            
            # Функция для обновления прогресса
            progress_data = {"last_update": time.time()}
            
            def progress_callback(stage, pct, speed, eta, downloaded, total):
                now = time.time()
                if now - progress_data["last_update"] > PROGRESS_UPDATE_INTERVAL:
                    db.update_download_progress(download_id, pct, speed, eta)
                    progress_data["last_update"] = now
            
            # Загрузить видео
            success, file_path, metadata = download_video(
                video_url,
                user_dir,
                format_type=format_type,
                progress_callback=progress_callback
            )

            if not success:
                error_msg = metadata.get("error", "Download failed")
                db.update_download_status(download_id, "failed", error_message=error_msg)
                logger.error(f"Download failed for {download_id}: {error_msg}")
                return

            # Конвертация обрабатывается внутри download_video, статус остается downloading до completed

            # Обновить статус на "completed"
            file_size = Path(file_path).stat().st_size if file_path else 0
            db.update_download_status(
                download_id,
                "completed",
                file_path=file_path,
                file_size_bytes=file_size
            )
            
            logger.info(f"Download completed {download_id}: {file_size} bytes")
        
        except subprocess.TimeoutExpired:
            db.update_download_status(download_id, "failed", error_message="Download timeout")
            logger.error(f"Download timeout for {download_id}")
        except Exception as e:
            db.update_download_status(download_id, "failed", error_message=str(e))
            logger.error(f"Error processing download {download_id}: {e}")


# Глобальный worker
queue_worker = QueueWorker()


def start_queue_worker():
    """Запустить глобальный worker."""
    queue_worker.start()


def stop_queue_worker():
    """Остановить глобальный worker."""
    queue_worker.stop()
