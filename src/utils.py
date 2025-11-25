"""
Утилиты для работы с видео, форматирования, валидации
"""

import re
import subprocess
import json
from pathlib import Path
from typing import Optional, Dict, List, Tuple
import time
import threading
import logging

from config import YTDLP_CONFIG, AUDIO_FORMAT, MAX_VIDEO_DURATION_MINUTES, FFMPEG_PATH, DOWNLOAD_TIMEOUT_SECONDS, CONVERSION_TIMEOUT_SECONDS, VIDEO_INFO_CACHE, VIDEO_INFO_CACHE_TIMEOUT


logger = logging.getLogger(__name__)


def is_youtube_url(url: str) -> bool:
    """Проверить, что это ссылка на YouTube."""
    youtube_regex = r"^(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/"
    return bool(re.match(youtube_regex, url))


def get_video_info(url: str) -> Optional[Dict]:
    """
    Получить информацию о видео с YouTube используя yt-dlp.
    Возвращает: {title, duration, thumbnail, ext, filesize, id, available_formats}
    available_formats - список доступных разрешений видео
    Использует кэширование для снижения запросов.
    """
    # Проверить кэш
    current_time = time.time()
    if url in VIDEO_INFO_CACHE:
        cache_entry = VIDEO_INFO_CACHE[url]
        if current_time - cache_entry['timestamp'] < VIDEO_INFO_CACHE_TIMEOUT:
            logger.info(f"Using cached video info for {url}")
            return cache_entry['info']
        else:
            # Удалить просроченный кэш
            del VIDEO_INFO_CACHE[url]

    try:
        cmd = [
            "yt-dlp",
            "-j",  # JSON output
            "--no-warnings",
            "--skip-download",
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        if result.returncode != 0:
            logger.error(f"yt-dlp error: {result.stderr}")
            return None
        
        data = json.loads(result.stdout)
        
        # Получить примерный размер и доступные форматы
        duration = data.get("duration", 0)
        filesize_approx = 0
        
        # Собрать доступные разрешения
        formats = data.get("formats", [])
        available_heights = set()
        format_sizes = {}  # height -> filesize
        best_audio_size = 0
        
        if formats:
            for fmt in formats:
                fmt_filesize = fmt.get("filesize") or fmt.get("filesize_approx") or 0
                height = fmt.get("height") or 0
                vcodec = fmt.get("vcodec", "none")
                acodec = fmt.get("acodec", "none")
                
                # Видео форматы
                if vcodec != "none" and vcodec != "none" and height > 0:
                    available_heights.add(height)
                    # Сохранить максимальный размер для каждого разрешения
                    if height not in format_sizes or fmt_filesize > format_sizes[height]:
                        format_sizes[height] = fmt_filesize
                
                # Аудио формат
                if acodec != "none" and vcodec == "none" and fmt_filesize > best_audio_size:
                    best_audio_size = fmt_filesize
        
        # Создать список доступных форматов (отсортировано по убыванию)
        available_formats = []
        sorted_heights = sorted(available_heights, reverse=True)
        
        for height in sorted_heights:
            # Стандартные названия для распространенных разрешений
            if height >= 2160:
                label = "4K"
            elif height >= 1440:
                label = "2K"
            elif height >= 1080:
                label = "1080p"
            elif height >= 720:
                label = "720p"
            elif height >= 480:
                label = "480p"
            elif height >= 360:
                label = "360p"
            elif height >= 240:
                label = "240p"
            elif height >= 144:
                label = "144p"
            else:
                label = f"{height}p"
            
            # Оценить размер (видео + аудио)
            video_size = format_sizes.get(height, 0)
            estimated_size = video_size + best_audio_size
            
            # Если нет размера, оценить по битрейту
            if estimated_size == 0 and duration > 0:
                # Примерные битрейты для разных разрешений
                if height >= 2160:
                    bitrate = 15.0  # Mbps
                elif height >= 1440:
                    bitrate = 10.0
                elif height >= 1080:
                    bitrate = 5.0
                elif height >= 720:
                    bitrate = 2.5
                elif height >= 480:
                    bitrate = 1.5
                else:
                    bitrate = 0.8
                estimated_size = int(duration * bitrate * 1024 * 1024 / 8)
            
            available_formats.append({
                "height": height,
                "label": label,
                "filesize": estimated_size
            })
        
        # Ограничить до разумного количества (убрать дубликаты по label)
        seen_labels = set()
        unique_formats = []
        for fmt in available_formats:
            if fmt["label"] not in seen_labels:
                seen_labels.add(fmt["label"])
                unique_formats.append(fmt)
        
        # Взять максимальный размер для основного отображения
        if unique_formats:
            filesize_approx = unique_formats[0]["filesize"]
        elif duration > 0:
            # Fallback оценка
            bitrate_mbps = 3.0
            filesize_approx = int(duration * bitrate_mbps * 1024 * 1024 / 8)
        
        video_info = {
            "title": data.get("title", "Unknown"),
            "duration": duration,  # в секундах
            "thumbnail": data.get("thumbnail", ""),
            "ext": data.get("ext", "mp4"),
            "filesize": filesize_approx,
            "id": data.get("id", ""),
            "available_formats": unique_formats[:8],  # Максимум 8 форматов
        }

        # Сохранить в кэш
        VIDEO_INFO_CACHE[url] = {
            'info': video_info,
            'timestamp': current_time
        }

        return video_info
    except subprocess.TimeoutExpired:
        logger.error(f"yt-dlp timeout for {url}")
        return None
    except Exception as e:
        logger.error(f"get_video_info error: {e}")
        return None


def format_duration(seconds: int) -> str:
    """Форматировать длительность в HH:MM:SS."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_file_size(bytes_size: int) -> str:
    """Форматировать размер файла в MB, GB."""
    if bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.1f} KB"
    elif bytes_size < 1024 * 1024 * 1024:
        return f"{bytes_size / (1024 * 1024):.1f} MB"
    else:
        return f"{bytes_size / (1024 * 1024 * 1024):.1f} GB"


def format_speed(bytes_per_sec: float) -> str:
    """Форматировать скорость загрузки."""
    mbps = bytes_per_sec / (1024 * 1024)
    return f"{mbps:.1f} MB/s"


def format_eta(seconds: int) -> str:
    """Форматировать оставшееся время в MM:SS или HH:MM:SS."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def download_video(url: str, output_path: Path, format_type: str = "1080p",
                   progress_callback=None) -> Tuple[bool, Optional[str], Dict]:
    """
    Загрузить видео с YouTube.
    format_type может быть: "mp3", "1080p", "720p", "480p", "360p", "4K", "2K", и т.д.
    progress_callback(stage, progress_pct, speed, eta, downloaded, total)
    Возвращает: (success, file_path, metadata)
    Использует прогресс-хуки yt-dlp для точного отслеживания.
    """
    try:
        import yt_dlp

        # Определить формат для yt-dlp
        if format_type == "mp3":
            ydl_opts = {
                'format': AUDIO_FORMAT,
                'outtmpl': str(output_path / "%(title)s.%(ext)s"),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            }
        else:
            # Определить высоту из format_type
            height = None
            if format_type == "4K":
                height = 2160
            elif format_type == "2K":
                height = 1440
            elif format_type.endswith("p"):
                try:
                    height = int(format_type[:-1])
                except ValueError:
                    height = 720  # fallback
            else:
                height = 720  # fallback

            ydl_opts = {
                'format': f'bestvideo[height<={height}][vcodec!=none]+bestaudio/best[height<={height}]',
                'outtmpl': str(output_path / "%(title)s.%(ext)s"),
                'merge_output_format': 'mp4',
            }

        # Добавить общие опции
        ydl_opts.update({
            'quiet': True,
            'no_warnings': True,
            'geo_bypass': True,
            'progress_hooks': [lambda d: self._progress_hook(d, progress_callback)],
        })

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                logger.info(f"Starting download: {url} ({format_type})")
                info = ydl.extract_info(url, download=True)

                # Найти скачанный файл
                downloaded_file = None
                newest_time = 0
                for file in output_path.glob("*"):
                    if file.is_file() and file.suffix.lower() in [".mp4", ".mkv", ".webm", ".mp3", ".m4a"]:
                        file_time = file.stat().st_mtime
                        if file_time > newest_time:
                            newest_time = file_time
                            downloaded_file = file

                if not downloaded_file:
                    logger.error("Downloaded file not found")
                    return False, None, {}

                if progress_callback:
                    progress_callback("completed", 100, 0, 0, 0, 0)

                metadata = {
                    "file_size": downloaded_file.stat().st_size,
                    "filename": downloaded_file.name,
                }

                logger.info(f"Download completed: {downloaded_file}")
                return True, str(downloaded_file), metadata

        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e)
            if "geo" in error_msg.lower() or "blocked" in error_msg.lower():
                logger.error(f"Geo-blocked video: {url}")
                return False, None, {"error": "video_geo_blocked"}
            elif "private" in error_msg.lower():
                logger.error(f"Private video: {url}")
                return False, None, {"error": "video_private"}
            elif "unavailable" in error_msg.lower():
                logger.error(f"Video unavailable: {url}")
                return False, None, {"error": "video_unavailable"}
            else:
                logger.error(f"Download error: {e}")
                return False, None, {"error": str(e)}
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return False, None, {"error": str(e)}

    except Exception as e:
        logger.error(f"Download error: {e}")
        return False, None, {"error": str(e)}

    def _progress_hook(self, d, progress_callback):
        """Обработчик прогресса yt-dlp."""
        if d['status'] == 'downloading':
            try:
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                speed = d.get('speed', 0)
                eta = d.get('eta', 0)

                if total > 0:
                    pct = int((downloaded / total) * 100)
                else:
                    pct = 0

                # Конвертировать скорость в MB/s
                speed_mbps = speed / (1024 * 1024) if speed else 0

                if progress_callback:
                    progress_callback("downloading", pct, speed_mbps, eta, downloaded, total)

            except Exception as e:
                logger.debug(f"Progress hook error: {e}")

        elif d['status'] == 'finished':
            if progress_callback:
                progress_callback("converting", 90, 0, 0, 0, 0)


def cleanup_old_files(storage_path: Path, max_age_hours: int = 72) -> int:
    """Удалить старые файлы старше max_age_hours. Возвращает количество удаленных."""
    import time
    current_time = time.time()
    removed_count = 0
    
    for file in storage_path.glob("*"):
        if file.is_file():
            file_age_hours = (current_time - file.stat().st_mtime) / 3600
            if file_age_hours > max_age_hours:
                try:
                    file.unlink()
                    removed_count += 1
                    logger.info(f"Removed old file: {file.name}")
                except Exception as e:
                    logger.error(f"Failed to remove {file.name}: {e}")
    
    return removed_count


def get_storage_size_mb(storage_path: Path) -> float:
    """Получить размер папки storage в MB."""
    total_size = sum(f.stat().st_size for f in storage_path.rglob("*") if f.is_file())
    return total_size / (1024 * 1024)


class ProgressTracker:
    """Трекер прогресса загрузки с расчетом скорости и ETA."""
    
    def __init__(self):
        self.start_time = time.time()
        self.last_bytes = 0
        self.last_time = self.start_time
        self.total_bytes = 0
    
    def update(self, bytes_downloaded: int, total_bytes: int) -> Tuple[int, float, int]:
        """
        Обновить прогресс.
        Возвращает: (progress_pct, speed_mbps, eta_seconds)
        """
        current_time = time.time()
        time_delta = current_time - self.last_time
        
        if time_delta < 0.5:
            return 0, 0, 0
        
        bytes_delta = bytes_downloaded - self.last_bytes
        speed_mbps = (bytes_delta / time_delta) / (1024 * 1024) if time_delta > 0 else 0
        
        pct = int((bytes_downloaded / total_bytes) * 100) if total_bytes > 0 else 0
        
        remaining_bytes = total_bytes - bytes_downloaded
        eta_seconds = int(remaining_bytes / (bytes_delta / time_delta)) if bytes_delta > 0 else 0
        
        self.last_bytes = bytes_downloaded
        self.last_time = current_time
        self.total_bytes = total_bytes
        
        return pct, speed_mbps, eta_seconds
