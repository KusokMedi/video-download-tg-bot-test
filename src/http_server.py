"""
HTTP-сервер для раздачи больших файлов (> 50 MB)
Простой встроенный сервер на Python
"""

import logging
from pathlib import Path
from threading import Thread
from http.server import HTTPServer, SimpleHTTPRequestHandler
import os
import mimetypes
from config import HTTP_SERVER_HOST, HTTP_SERVER_PORT, HTTP_SERVER_TIMEOUT
from typing import Optional

logger = logging.getLogger(__name__)


class FileDownloadHandler(SimpleHTTPRequestHandler):
    """Обработчик для скачивания файлов с таймаутом."""
    
    timeout = HTTP_SERVER_TIMEOUT
    
    def do_GET(self):
        """Обработать GET-запрос для скачивания файла."""
        try:
            file_path = Path(self.path.lstrip("/"))

            if not file_path.exists() or not file_path.is_file():
                self.send_error(404, "File not found")
                return

            # Отправить файл
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(str(file_path))[0] or "application/octet-stream")
            self.send_header("Content-Length", str(file_path.stat().st_size))
            self.send_header("Content-Disposition", f"attachment; filename={file_path.name}")
            self.end_headers()

            # Читать и отправлять файл по кускам для больших файлов
            with open(file_path, "rb") as f:
                while chunk := f.read(8192):  # 8KB chunks
                    self.wfile.write(chunk)

            logger.info(f"Sent file: {file_path.name}")

        except Exception as e:
            logger.error(f"Error serving file: {e}")
            self.send_error(500, "Internal server error")
    
    def log_message(self, format, *args):
        """Переопределить логирование."""
        logger.info(f"{self.client_address[0]} - {format % args}")


class HTTPFileServer:
    """Встроенный HTTP-сервер для раздачи файлов."""
    
    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
        self.server = None
        self.thread = None
    
    def start(self):
        """Запустить сервер."""
        try:
            os.chdir(self.storage_dir)
            self.server = HTTPServer((HTTP_SERVER_HOST, HTTP_SERVER_PORT), FileDownloadHandler)
            self.thread = Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            logger.info(f"HTTP server started on {HTTP_SERVER_HOST}:{HTTP_SERVER_PORT}")
        except Exception as e:
            logger.error(f"Failed to start HTTP server: {e}")
    
    def stop(self):
        """Остановить сервер."""
        if self.server:
            self.server.shutdown()
            logger.info("HTTP server stopped")
    
    def get_file_url(self, file_path: Path) -> str:
        """Получить URL для скачивания файла."""
        return f"http://localhost:{HTTP_SERVER_PORT}/{file_path.name}"


# Глобальный экземпляр сервера
http_server: Optional[HTTPFileServer] = None


def init_http_server(storage_dir: Path):
    """Инициализировать HTTP-сервер."""
    global http_server
    http_server = HTTPFileServer(storage_dir)
    http_server.start()


def get_download_url(file_path: Path) -> str:
    """Получить URL для скачивания файла."""
    if http_server:
        return http_server.get_file_url(file_path)
    return ""
