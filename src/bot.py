"""
Основной Telegram бот KusokMedi - версия 2
Обработка видео, управление очередью, приоритет пользователей
"""

import logging
import time
from pathlib import Path
from datetime import datetime, timedelta
from threading import Thread, Lock
import telebot
from telebot import types

from config import (
    TELEGRAM_TOKEN,
    ADMIN_ID,
    BOT_NAME,
    OWNER_USERNAME,
    STORAGE_DIR,
    MAX_FILE_SIZE_MB,
    PRIORITY_DAYS,
    MESSAGES,
    PROGRESS_UPDATE_INTERVAL,
)
from db import db
from utils import (
    is_youtube_url,
    get_video_info,
    format_duration,
    format_file_size,
    format_speed,
    format_eta,
    get_storage_size_mb,
)
from queue_worker import start_queue_worker, stop_queue_worker
from http_server import init_http_server, get_download_url

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent.parent / "logs" / "bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Кеш для ссылок (user_id -> {'url': url, 'timestamp': time.time()})
url_cache = {}
url_cache_lock = Lock()

# Кеш для сообщений с информацией о видео (user_id -> message_id)
video_info_messages = {}
video_info_lock = Lock()

# Отслеживание активных сообщений прогресса
progress_messages = {}  # download_id -> (chat_id, message_id)
progress_lock = Lock()

def cleanup_caches():
    """Очистить старые записи из кешей."""
    current_time = time.time()
    # Очистить url_cache старше 1 часа
    with url_cache_lock:
        to_remove = []
        for user_id, data in url_cache.items():
            if isinstance(data, dict) and 'timestamp' in data:
                if current_time - data['timestamp'] > 3600:
                    to_remove.append(user_id)
        for user_id in to_remove:
            del url_cache[user_id]

    # Очистить video_info_messages старше 30 минут
    with video_info_lock:
        to_remove = []
        for user_id, message_id in video_info_messages.items():
            # Простая очистка, можно улучшить с timestamp
            pass  # Пока оставим, так как удаляется при использовании

    # Очистить progress_messages для завершенных загрузок
    with progress_lock:
        active_downloads = db.count_active_downloads()
        if active_downloads == 0:
            progress_messages.clear()


# ==================== Команды ====================

@bot.message_handler(commands=["start"])
def handle_start(message: types.Message):
    """Обработка /start."""
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    first_name = message.from_user.first_name or "User"
    
    db.add_or_update_user(user_id, username, first_name)
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton("📹 YouTube ссылка"),
        types.KeyboardButton("📊 Статус")
    )
    markup.add(
        types.KeyboardButton("📚 Помощь"),
        types.KeyboardButton("💎 Приоритет")
    )
    if user_id == ADMIN_ID:
        markup.add(types.KeyboardButton("👑 Админ"))
    
    bot.send_message(message.chat.id, MESSAGES["start"], reply_markup=markup)
    logger.info(f"User {user_id} started bot")


@bot.message_handler(commands=["help"])
def handle_help(message: types.Message):
    """Обработка /help."""
    bot.send_message(message.chat.id, MESSAGES["help"])


@bot.message_handler(commands=["buy_priority"])
def handle_buy_priority(message: types.Message):
    """Обработка /buy_priority."""
    user_id = message.from_user.id
    bot.send_message(message.chat.id, MESSAGES["buy_priority_msg"])
    logger.info(f"User {user_id} requested priority info")


@bot.message_handler(commands=["status"])
def handle_status(message: types.Message):
    """Обработка /status."""
    user_id = message.from_user.id
    
    active = db.get_user_active_downloads(user_id)
    pending = db.get_all_pending_downloads()
    active_count = db.count_active_downloads()
    
    user = db.get_user(user_id)
    has_priority = db.has_priority(user_id)
    
    status_text = f"""
📊 Статус очереди:
- Активных загрузок: {active_count}
- В очереди: {len(pending)}
- Твои загрузки: {len(active)}
- Твой приоритет: {'✅ Активен' if has_priority else '❌ Нет'}

💾 Хранилище: {get_storage_size_mb(STORAGE_DIR):.1f} MB
"""
    
    bot.send_message(message.chat.id, status_text)


@bot.message_handler(commands=["admin"])
def handle_admin(message: types.Message):
    """Обработка /admin - панель администратора."""
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ Нет доступа (только для владельца)")
        return
    
    pending = db.get_pending_priority_purchases()
    
    admin_panel = f"""
👑 АДМИН ПАНЕЛЬ
    
📊 СТАТИСТИКА:
- Активных загрузок: {db.count_active_downloads()}
- В очереди: {len(db.get_all_pending_downloads())}
- Хранилище: {get_storage_size_mb(STORAGE_DIR):.1f} MB
    
💳 ПЛАТЕЖИ:
- Ожидают подтверждения: {len(pending)}

⚙️ УПРАВЛЕНИЕ ПРИОРИТЕТОМ:
📌 /give_priority - Выдать приоритет (формат: <ID> <дни>, отрицательное число = бесконечный)
❌ /remove_priority - Забрать приоритет
📋 /list_priority - Показать всех с приоритетом
"""
    
    markup = types.InlineKeyboardMarkup()
    
    if pending:
        markup.add(types.InlineKeyboardButton(
            f"💳 Платежи ({len(pending)})",
            callback_data="admin_view_payments"
        ))
    
    markup.add(types.InlineKeyboardButton(
        "🗑️ Очистить старые файлы",
        callback_data="admin_cleanup"
    ))
    
    bot.send_message(message.chat.id, admin_panel, reply_markup=markup)
    logger.info(f"Admin {message.from_user.id} opened admin panel")


@bot.callback_query_handler(func=lambda c: c.data == "admin_view_payments")
def handle_admin_payments(call: types.CallbackQuery):
    """Показать ожидающие платежи."""
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
        return
    
    pending = db.get_pending_priority_purchases()
    
    if not pending:
        bot.answer_callback_query(call.id, "Нет ожидающих платежей", show_alert=True)
        return
    
    for purchase in pending:
        purchase_id = purchase['purchase_id']
        user_id = purchase['user_id']
        amount = purchase['amount_usd']
        created = purchase['created_at']
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_priority_{purchase_id}"),
            types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_priority_{purchase_id}")
        )
        
        text = f"""
💳 ПЛАТЕЖ НА ПРОВЕРКЕ:
    
👤 User ID: {user_id}
💰 Сумма: ${amount}
📅 Дата: {created}
    
📝 Выбери действие:
"""
        
        bot.send_message(call.message.chat.id, text, reply_markup=markup)
    
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "admin_cleanup")
def handle_admin_cleanup(call: types.CallbackQuery):
    """Очистить старые файлы."""
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
        return
    
    from utils import cleanup_old_files
    cleanup_old_files(STORAGE_DIR, max_age_hours=72)
    
    bot.answer_callback_query(call.id, "✅ Файлы очищены", show_alert=True)
    logger.info("Admin performed cleanup")


# ==================== Управление приоритетом админом ====================

@bot.message_handler(commands=["give_priority"])
def handle_give_priority(message: types.Message):
    """Админ выдает приоритет по ID пользователя."""
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ Нет доступа (только для владельца)")
        return
    
    msg = bot.send_message(
        message.chat.id,
        """👑 ВЫДАТЬ ПРИОРИТЕТ

Отправь данные в формате:
<ID> <дни>

Примеры:
123456789 30
987654321 7
555555555 365"""
    )
    
    bot.register_next_step_handler(msg, process_give_priority)


def process_give_priority(message: types.Message):
    """Обработка выдачи приоритета."""
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        parts = message.text.strip().split()
        if len(parts) != 2:
            bot.send_message(message.chat.id, "❌ Неверный формат. Используй: <ID> <дни>\n\n💡 Совет: если дни отрицательные (например -1), то приоритет будет БЕСКОНЕЧНЫМ!")
            return
        
        user_id = int(parts[0])
        days = int(parts[1])
        
        if days == 0:
            bot.send_message(message.chat.id, "❌ Дни не могут быть 0. Используй положительное число или отрицательное для бесконечного приоритета")
            return
        
        # Определить тип приоритета ПЕРЕД передачей в БД
        if days < 0:
            priority_text = "∞ БЕСКОНЕЧНЫЙ"
            user_message_text = "∞ ВЕЧНЫЙ приоритет!"
        else:
            priority_text = f"{days} дней"
            user_message_text = f"{days} дней"
        
        # Если это ADMIN_ID и дни положительные, переделать на бесконечный
        if user_id == ADMIN_ID and days > 0:
            days = -1
            priority_text = "∞ БЕСКОНЕЧНЫЙ"
            user_message_text = "∞ ВЕЧНЫЙ приоритет!"
        
        if db.admin_give_priority(user_id, days):
            # Уведомить пользователя
            try:
                bot.send_message(
                    user_id,
                    f"""🎉 ПРИОРИТЕТ ВЫДАН АДМИНОМ!
    
👑 Ты получил VIP статус!
⚡ Твои видео теперь в начале очереди
📅 На {user_message_text}
    
Спасибо за использование KusokMedi! 🚀"""
                )
            except:
                pass
            
            bot.send_message(
                message.chat.id,
                f"""✅ ПРИОРИТЕТ ВЫДАН

👤 User ID: {user_id}
📅 Срок: {priority_text}
⚡ Статус: активирован"""
            )
            logger.info(f"Admin gave priority to {user_id} for {days} days")
        else:
            bot.send_message(message.chat.id, "❌ Ошибка при выдаче приоритета")
    
    except ValueError:
        bot.send_message(message.chat.id, "❌ ID и дни должны быть числами")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {str(e)[:100]}")
        logger.error(f"Error in process_give_priority: {e}")


@bot.message_handler(commands=["remove_priority"])
def handle_remove_priority(message: types.Message):
    """Админ забирает приоритет по ID пользователя."""
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ Нет доступа (только для владельца)")
        return
    
    msg = bot.send_message(
        message.chat.id,
        """🚫 ЗАБРАТЬ ПРИОРИТЕТ

Отправь ID пользователя:
123456789"""
    )
    
    bot.register_next_step_handler(msg, process_remove_priority)


def process_remove_priority(message: types.Message):
    """Обработка отзыва приоритета."""
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        user_id = int(message.text.strip())
        
        if db.admin_remove_priority(user_id):
            # Уведомить пользователя
            try:
                bot.send_message(
                    user_id,
                    """⚠️ ПРИОРИТЕТ ОТОЗВАН

😔 Твой VIP статус был отозван администратором.
⚡ Твои видео теперь обрабатываются в общей очереди.

Если это ошибка, свяжись с администратором."""
                )
            except:
                pass
            
            bot.send_message(
                message.chat.id,
                f"""✅ ПРИОРИТЕТ ОТОЗВАН

👤 User ID: {user_id}
🚫 Статус: удален"""
            )
            logger.info(f"Admin removed priority from {user_id}")
        else:
            bot.send_message(message.chat.id, "❌ Ошибка при отзыве приоритета")
    
    except ValueError:
        bot.send_message(message.chat.id, "❌ ID должен быть числом")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {str(e)[:100]}")
        logger.error(f"Error in process_remove_priority: {e}")


@bot.message_handler(commands=["list_priority"])
def handle_list_priority(message: types.Message):
    """Админ просматривает всех пользователей с приоритетом."""
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ Нет доступа (только для владельца)")
        return
    
    users_with_priority = db.get_users_with_priority()
    
    if not users_with_priority:
        bot.send_message(message.chat.id, "📋 Нет пользователей с активным приоритетом")
        return
    
    response = "📋 СПИСОК ПОЛЬЗОВАТЕЛЕЙ С ПРИОРИТЕТОМ\n\n"
    
    for idx, user in enumerate(users_with_priority, 1):
        username_display = f"@{user['username']}" if user['username'] else "N/A"
        response += f"{idx}. {user['first_name']} {username_display}\n"
        response += f"   ID: {user['user_id']}\n"
        response += f"   Приоритет: {user['priority_until']}\n"
        response += f"   Загрузок: {user['total_downloads']}\n\n"
    
    response += f"Всего: {len(users_with_priority)} пользователей"
    
    # Отправить по частям если сообщение слишком длинное
    if len(response) > 4000:
        chunks = response.split("\n\n")
        current_msg = ""
        for chunk in chunks:
            if len(current_msg) + len(chunk) > 4000:
                bot.send_message(message.chat.id, current_msg)
                current_msg = chunk + "\n\n"
            else:
                current_msg += chunk + "\n\n"
        if current_msg:
            bot.send_message(message.chat.id, current_msg)
    else:
        bot.send_message(message.chat.id, response)
    
    logger.info(f"Admin viewed priority list. Total: {len(users_with_priority)} users")


# ==================== Текстовые сообщения ====================

@bot.message_handler(func=lambda m: m.text and (is_youtube_url(m.text) or m.text.startswith("http")))
def handle_video_link(message: types.Message):
    """Обработка ссылки на видео."""
    user_id = message.from_user.id
    url = message.text.strip()

    if not url.startswith("http"):
        return

    # Сохранить URL
    with url_cache_lock:
        url_cache[user_id] = {'url': url, 'timestamp': time.time()}

    db.add_or_update_user(user_id, message.from_user.username, message.from_user.first_name)

    if is_youtube_url(url):
        # Обработка YouTube ссылки
        handle_youtube_link(message)
    else:
        # Обработка не-YouTube ссылки с предупреждением
        handle_non_youtube_link(message)


def handle_youtube_link(message: types.Message):
    """Обработка ссылки на YouTube."""
    user_id = message.from_user.id

    # Получить URL из кеша
    with url_cache_lock:
        url = url_cache.get(user_id)

    if not url:
        bot.send_message(message.chat.id, MESSAGES["invalid_link"])
        return

    wait_msg = bot.send_message(message.chat.id, "⏳ Получаю информацию о видео...")

    try:
        video_info = get_video_info(url)

        if not video_info:
            bot.edit_message_text(
                MESSAGES["video_not_found"],
                message.chat.id,
                wait_msg.message_id
            )
            return

        duration = video_info.get("duration", 0)
        if duration > 120 * 60:
            bot.edit_message_text(
                MESSAGES["video_too_long"],
                message.chat.id,
                wait_msg.message_id
            )
            return

        title = video_info['title']
        if len(title) > 100:
            title = title[:97] + "..."

        # Получить доступные форматы
        available_formats = video_info.get('available_formats', [])

        # Сформировать текст с размером лучшего качества
        best_size = video_info.get('filesize', 0)

        text = f"""
🎬 ВИДЕО

📝 Название: {title}
⏱️ Длительность: {format_duration(duration)}
📦 Примерный размер: ~{format_file_size(best_size)}

👇 Выбери качество:
"""

        markup = types.InlineKeyboardMarkup(row_width=2)

        # Динамически создать кнопки для доступных форматов
        quality_buttons = []
        emoji_map = {
            "4K": "📺", "2K": "🖥️", "1080p": "🎬", "720p": "🎥",
            "480p": "📹", "360p": "🎞️", "240p": "📱", "144p": "📟"
        }

        for fmt in available_formats[:6]:  # Максимум 6 форматов видео
            label = fmt["label"]
            emoji = emoji_map.get(label, "📹")
            size_text = format_file_size(fmt["filesize"]) if fmt["filesize"] > 0 else ""
            button_text = f"{emoji} {label}"
            if size_text:
                button_text += f" (~{size_text})"
            quality_buttons.append(
                types.InlineKeyboardButton(button_text, callback_data=f"download_{label}_{user_id}")
            )

        # Добавить кнопки парами
        for i in range(0, len(quality_buttons), 2):
            if i + 1 < len(quality_buttons):
                markup.add(quality_buttons[i], quality_buttons[i + 1])
            else:
                markup.add(quality_buttons[i])

        # Добавить аудио и отмену
        markup.add(types.InlineKeyboardButton("🎵 Аудио MP3", callback_data=f"download_mp3_{user_id}"))
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_{user_id}"))

        if video_info.get("thumbnail"):
            try:
                bot.delete_message(message.chat.id, wait_msg.message_id)
                sent_msg = bot.send_photo(
                    message.chat.id,
                    video_info["thumbnail"],
                    caption=text,
                    reply_markup=markup
                )
                # Сохранить message_id для удаления позже
                with video_info_lock:
                    video_info_messages[user_id] = sent_msg.message_id
            except:
                bot.edit_message_text(text, message.chat.id, wait_msg.message_id, reply_markup=markup)
                with video_info_lock:
                    video_info_messages[user_id] = wait_msg.message_id
        else:
            bot.edit_message_text(text, message.chat.id, wait_msg.message_id, reply_markup=markup)
            with video_info_lock:
                video_info_messages[user_id] = wait_msg.message_id

        logger.info(f"User {user_id} sent YouTube link: {url}")

    except Exception as e:
        logger.error(f"Error handling YouTube link: {e}")
        bot.edit_message_text(MESSAGES["error"].format(error=str(e)[:100]), message.chat.id, wait_msg.message_id)


def handle_non_youtube_link(message: types.Message):
        """Обработка ссылки не из YouTube с предупреждением."""
        user_id = message.from_user.id
        url = message.text.strip()
    
        # Получить URL из кеша
        with url_cache_lock:
            cache_entry = url_cache.get(user_id)
            url = cache_entry['url'] if cache_entry else url
    
        wait_msg = bot.send_message(message.chat.id, "⏳ Получаю информацию о видео...")
    
        try:
            video_info = get_video_info(url)
    
            if not video_info:
                bot.edit_message_text(
                    MESSAGES["video_not_found"],
                    message.chat.id,
                    wait_msg.message_id
                )
                return
    
            duration = video_info.get("duration", 0)
            if duration > 120 * 60:
                bot.edit_message_text(
                    MESSAGES["video_too_long"],
                    message.chat.id,
                    wait_msg.message_id
                )
                return
    
            title = video_info['title']
            if len(title) > 100:
                title = title[:97] + "..."
    
            # Получить доступные форматы
            available_formats = video_info.get('available_formats', [])
    
            # Сформировать текст с размером лучшего качества
            best_size = video_info.get('filesize', 0)
    
            text = f"""
    ⚠️ ВНИМАНИЕ: Это видео НЕ из YouTube!
    
    🎬 ВИДЕО
    📝 Название: {title}
    ⏱️ Длительность: {format_duration(duration)}
    📦 Примерный размер: ~{format_file_size(best_size)}
    
    🚨 ВОЗМОЖНЫЕ ПРОБЛЕМЫ:
    • Скачивание может не работать
    • Качество может быть хуже
    • Файл может быть поврежден
    • Сервис может блокировать загрузку
    
    ❓ Продолжить скачивание?
    """
    
            markup = types.InlineKeyboardMarkup(row_width=2)
    
            # Добавить кнопки для форматов (только если есть информация)
            if available_formats:
                quality_buttons = []
                emoji_map = {
                    "4K": "📺", "2K": "🖥️", "1080p": "🎬", "720p": "🎥",
                    "480p": "📹", "360p": "🎞️", "240p": "📱", "144p": "📟"
                }
    
                for fmt in available_formats[:4]:  # Максимум 4 формата для не-YouTube
                    label = fmt["label"]
                    emoji = emoji_map.get(label, "📹")
                    size_text = format_file_size(fmt["filesize"]) if fmt["filesize"] > 0 else ""
                    button_text = f"{emoji} {label}"
                    if size_text:
                        button_text += f" (~{size_text})"
                    quality_buttons.append(
                        types.InlineKeyboardButton(button_text, callback_data=f"confirm_download_{label}_{user_id}")
                    )
    
                # Добавить кнопки парами
                for i in range(0, len(quality_buttons), 2):
                    if i + 1 < len(quality_buttons):
                        markup.add(quality_buttons[i], quality_buttons[i + 1])
                    else:
                        markup.add(quality_buttons[i])
    
            # Добавить аудио и кнопки управления
            markup.add(types.InlineKeyboardButton("🎵 Аудио MP3", callback_data=f"confirm_download_mp3_{user_id}"))
            markup.add(types.InlineKeyboardButton("✅ Продолжить", callback_data=f"proceed_anyway_{user_id}"))
            markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_{user_id}"))
    
            if video_info.get("thumbnail"):
                try:
                    bot.delete_message(message.chat.id, wait_msg.message_id)
                    sent_msg = bot.send_photo(
                        message.chat.id,
                        video_info["thumbnail"],
                        caption=text,
                        reply_markup=markup
                    )
                    # Сохранить message_id для удаления позже
                    with video_info_lock:
                        video_info_messages[user_id] = sent_msg.message_id
                except:
                    bot.edit_message_text(text, message.chat.id, wait_msg.message_id, reply_markup=markup)
                    with video_info_lock:
                        video_info_messages[user_id] = wait_msg.message_id
            else:
                bot.edit_message_text(text, message.chat.id, wait_msg.message_id, reply_markup=markup)
                with video_info_lock:
                    video_info_messages[user_id] = wait_msg.message_id
    
            logger.info(f"User {user_id} sent non-YouTube link: {url}")
    
        except Exception as e:
            logger.error(f"Error handling non-YouTube link: {e}")
            bot.edit_message_text(MESSAGES["error"].format(error=str(e)[:100]), message.chat.id, wait_msg.message_id)


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_any_message(message: types.Message):
    """Обработка любых текстовых сообщений."""
    text = message.text
    user_id = message.from_user.id
    
    # Обработка кнопок из главного меню
    if text == "📹 YouTube ссылка":
        bot.send_message(message.chat.id, "Отправь мне ссылку на YouTube видео:")
        return
    
    elif text == "📚 Помощь":
        handle_help(message)
        return
    
    elif text == "📊 Статус":
        handle_status(message)
        return
    
    elif text == "💎 Приоритет":
        handle_buy_priority(message)
        return
    
    elif text == "👑 Админ":
        if user_id == ADMIN_ID:
            handle_admin(message)
        else:
            bot.send_message(message.chat.id, "❌ Нет доступа (только для владельца)")
        return
    
    # Обработка ссылок на видео
    elif is_youtube_url(text) or text.startswith("http"):
        handle_video_link(message)
        return
    
    # Неизвестная команда
    else:
        bot.send_message(
            message.chat.id,
            """❌ Команда не распознана

📝 Что я умею:
- 📹 Загружать видео с YouTube и других платформ
- 🎬 Конвертировать в разные качества
- 💎 Управлять приоритетом

Используй кнопки ниже или отправь ссылку на видео!"""
        )


# ==================== Callback кнопки ====================

@bot.callback_query_handler(func=lambda c: c.data.startswith("download_"))
def handle_download_callback(call: types.CallbackQuery):
    """Обработка выбора качества."""
    parts = call.data.split("_")
    format_type = parts[1]
    user_id = int(parts[2])
    
    if call.from_user.id != user_id:
        bot.answer_callback_query(call.id, "❌ Это не твоя ссылка", show_alert=True)
        return
    
    try:
        emoji_map = {
            "4K": "📺", "2K": "🖥️", "1080p": "🎬", "720p": "🎥",
            "480p": "📹", "360p": "🎞️", "240p": "📱", "144p": "📟", "mp3": "🎵"
        }
        emoji = emoji_map.get(format_type, "📥")
        bot.edit_message_text(f"⏳ Подготавливаю загрузку в качестве {emoji} {format_type}...", call.message.chat.id, call.message.message_id)
    except:
        pass
    
    # Получить URL из кеша
    with url_cache_lock:
        cache_entry = url_cache.get(user_id)
        url = cache_entry['url'] if cache_entry else None
    
    if not url:
        bot.answer_callback_query(call.id, "❌ Ошибка: ссылка потеряна", show_alert=True)
        return
    
    logger.info(f"User {user_id} selected format: {format_type}")

    # Проверить кеш готовых файлов
    cached_download = db.get_completed_download_by_url_format(url, format_type)
    if cached_download and cached_download["file_path"] and Path(cached_download["file_path"]).exists():
        # Файл уже есть, отправить из кеша
        logger.info(f"Using cached file for {url} {format_type}: {cached_download['file_path']}")
        _send_cached_file(user_id, cached_download, call.message.chat.id)
        bot.answer_callback_query(call.id, "✅ Файл из кеша отправлен", show_alert=False)
        return

    # Добавить в БД
    download_id = db.add_download(user_id, url, format_type=format_type)

    emoji_map = {
        "4K": "📺", "2K": "🖥️", "1080p": "🎬", "720p": "🎥",
        "480p": "📹", "360p": "🎞️", "240p": "📱", "144p": "📟", "mp3": "🎵"
    }
    emoji = emoji_map.get(format_type, "📥")
    progress_msg = bot.send_message(
        call.message.chat.id,
        f"📥 Стартую загрузку в качестве {emoji} {format_type}...\n0%"
    )

    with progress_lock:
        progress_messages[download_id] = (call.message.chat.id, progress_msg.message_id)

    update_thread = Thread(target=_update_progress_loop, args=(download_id, user_id), daemon=True)
    update_thread.start()

    bot.answer_callback_query(call.id, "✅ Загрузка запущена", show_alert=False)


def _update_progress_loop(download_id: int, user_id: int):
    """Цикл обновления прогресса."""
    start_time = time.time()
    last_update = 0
    last_progress = -1
    last_status = ""
    
    while True:
        download = db.get_download(download_id)
        
        if not download:
            break
        
        status = download["status"]
        
        if status in ["completed", "failed"]:
            break
        
        progress = download["progress"] or 0
        speed = download["speed_mbps"] or 0
        eta = download["eta_seconds"] or 0
        
        elapsed = time.time() - start_time
        
        # Обновлять только если прошло достаточно времени ИЛИ изменился прогресс/статус
        should_update = (
            elapsed - last_update > PROGRESS_UPDATE_INTERVAL or
            progress != last_progress or
            status != last_status
        )
        
        if should_update:
            with progress_lock:
                if download_id in progress_messages:
                    chat_id, message_id = progress_messages[download_id]
                    
                    # Создать progress bar (20 символов)
                    filled = int(progress / 5)
                    bar = "█" * filled + "░" * (20 - filled)
                    
                    text = None
                    if status == "downloading":
                        text = f"📥 ЗАГРУЖАЮ ВИДЕО\n\n{bar} {progress}%"
                        if speed and speed > 0:
                            text += f"\n⚡ Скорость: {speed:.1f} MB/s"
                        if eta and eta > 0:
                            text += f"\n⏱️ Осталось: {format_eta(int(eta))}"
                    elif status == "converting":
                        text = f"⚙️ КОНВЕРТИРУЮ ВИДЕО\n\n{bar}"
                    elif status == "sending":
                        text = f"📤 ОТПРАВЛЯЮ ФАЙЛ\n\n{bar}"
                    else:
                        text = f"⏳ ОБРАБОТКА\n\n{bar} {progress}%"
                    
                    # Только редактировать если текст изменился
                    if text:
                        try:
                            bot.edit_message_text(text, chat_id, message_id)
                            last_progress = progress
                            last_status = status
                        except Exception as e:
                            logger.debug(f"Failed to update progress: {e}")
            
            last_update = elapsed
        
        time.sleep(0.5)  # Проверять чаще
    
    # Завершить
    download = db.get_download(download_id)
    if download and download["status"] == "completed":
        _send_completed_download(user_id, download)
    elif download and download["status"] == "failed":
        with progress_lock:
            if download_id in progress_messages:
                chat_id, message_id = progress_messages[download_id]
                error_msg = download.get("error_message", "Неизвестная ошибка")

                # Дружелюбные сообщения об ошибках
                friendly_error = "❌ ОШИБКА ЗАГРУЗКИ\n\n"
                if "geo_blocked" in error_msg or "geo" in error_msg.lower():
                    friendly_error += "🌍 Видео заблокировано в вашем регионе\n\n💡 Попробуйте VPN или другое видео"
                elif "private" in error_msg or "private" in error_msg.lower():
                    friendly_error += "🔒 Это приватное видео\n\n💡 Автор сделал его недоступным для просмотра"
                elif "unavailable" in error_msg or "unavailable" in error_msg.lower():
                    friendly_error += "🚫 Видео недоступно\n\n💡 Возможно, оно было удалено или скрыто"
                elif "timeout" in error_msg.lower():
                    friendly_error += "⏰ Превышено время ожидания\n\n💡 Попробуйте позже или выберите меньшее качество"
                else:
                    friendly_error += f"⚠️ {error_msg}\n\n💡 Попробуйте еще раз или обратитесь в поддержку"

                try:
                    bot.edit_message_text(friendly_error, chat_id, message_id)
                except:
                    pass


def _send_cached_file(user_id: int, download: dict, chat_id: int):
    """Отправить файл из кеша."""
    file_path = download.get("file_path")
    file_size = download.get("file_size_bytes", 0)

    if not file_path or not Path(file_path).exists():
        logger.error(f"Cached file not found: {file_path}")
        return

    # Проверить размер файла
    if file_size == 0:
        logger.error(f"Cached file is empty: {file_path}")
        return

    # Удалить сообщение с информацией о видео и кнопками
    with video_info_lock:
        if user_id in video_info_messages:
            try:
                bot.delete_message(chat_id, video_info_messages[user_id])
                logger.info(f"Deleted video info message for user {user_id}")
            except Exception as e:
                logger.debug(f"Could not delete video info message: {e}")
            del video_info_messages[user_id]

    if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        url = get_download_url(Path(file_path))
        filename = Path(file_path).name
        text = f"""📦 ФАЙЛ СЛИШКОМ БОЛЬШОЙ

📊 Размер: {format_file_size(file_size)} ({file_size / (1024*1024):.1f} MB)
⚠️ Лимит Telegram: {MAX_FILE_SIZE_MB} MB

📥 СКАЧАТЬ ПО ССЫЛКЕ:
{url}

⏱️ Ссылка действует 1 час
📝 Имя файла: {filename}"""
        bot.send_message(chat_id, text)
        logger.info(f"Sent cached download link for {file_path}")
    else:
        try:
            file_extension = Path(file_path).suffix.lower()

            with open(file_path, "rb") as f:
                if file_extension == ".mp3":
                    # Отправить как аудио
                    bot.send_audio(
                        chat_id,
                        f,
                        caption="✅ ГОТОВО! (из кеша)\n\n🎵 Вот твое аудио!\n\n💬 Хочешь еще? Отправь новую ссылку!"
                    )
                elif file_extension in [".mp4", ".webm", ".mkv", ".avi", ".mov"]:
                    # Отправить как видео с поддержкой стриминга
                    bot.send_video(
                        chat_id,
                        f,
                        caption="✅ ГОТОВО! (из кеша)\n\n🎬 Вот твое видео!\n\n💬 Хочешь еще? Отправь новую ссылку!",
                        supports_streaming=True,
                        width=1280,
                        height=720
                    )
                else:
                    # Отправить как документ (для других расширений)
                    bot.send_document(
                        chat_id,
                        f,
                        caption="✅ ГОТОВО! (из кеша)\n\n🎬 Вот твой файл!\n\n💬 Хочешь еще? Отправь новую ссылку!"
                    )

        except Exception as e:
            logger.error(f"Error sending cached file: {e}")
            bot.send_message(chat_id, f"❌ Ошибка при отправке файла из кеша\n\n⚠️ {str(e)[:100]}")


def _send_completed_download(user_id: int, download: dict):
    """Отправить завершенную загрузку."""
    from db import db  # Импорт здесь чтобы избежать циклического импорта

    file_path = download.get("file_path")
    file_size = download.get("file_size_bytes", 0)
    download_id = download["download_id"]

    if not file_path or not Path(file_path).exists():
        logger.error(f"File not found: {file_path}")
        return

    # Проверить размер файла
    if file_size == 0:
        logger.error(f"Downloaded file is empty: {file_path}")
        with progress_lock:
            if download_id in progress_messages:
                chat_id, message_id = progress_messages[download_id]
                try:
                    bot.edit_message_text("❌ Ошибка: файл пустой", chat_id, message_id)
                except:
                    pass
        return

    # Обновить статус на "sending"
    db.update_download_status(download_id, "sending")

    with progress_lock:
        if download_id in progress_messages:
            chat_id, message_id = progress_messages[download_id]

            # Удалить сообщение с информацией о видео и кнопками
            with video_info_lock:
                if user_id in video_info_messages:
                    try:
                        bot.delete_message(chat_id, video_info_messages[user_id])
                        logger.info(f"Deleted video info message for user {user_id}")
                    except Exception as e:
                        logger.debug(f"Could not delete video info message: {e}")
                    del video_info_messages[user_id]

            if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                url = get_download_url(Path(file_path))
                filename = Path(file_path).name
                text = f"""📦 ФАЙЛ СЛИШКОМ БОЛЬШОЙ

📊 Размер: {format_file_size(file_size)} ({file_size / (1024*1024):.1f} MB)
⚠️ Лимит Telegram: {MAX_FILE_SIZE_MB} MB

📥 СКАЧАТЬ ПО ССЫЛКЕ:
{url}

⏱️ Ссылка действует 1 час
📝 Имя файла: {filename}"""
                try:
                    bot.edit_message_text(text, chat_id, message_id)
                except:
                    pass
                logger.info(f"Sent download link for {file_path}")
            else:
                try:
                    bot.edit_message_text("📤 Отправляю файл...", chat_id, message_id)

                    file_extension = Path(file_path).suffix.lower()

                    with open(file_path, "rb") as f:
                        if file_extension == ".mp3":
                            # Отправить как аудио
                            bot.send_audio(
                                chat_id,
                                f,
                                caption="✅ ГОТОВО!\n\n🎵 Вот твое аудио!\n\n💬 Хочешь еще? Отправь новую ссылку!"
                            )
                        elif file_extension in [".mp4", ".webm", ".mkv", ".avi", ".mov"]:
                            # Отправить как видео с поддержкой стриминга
                            bot.send_video(
                                chat_id,
                                f,
                                caption="✅ ГОТОВО!\n\n🎬 Вот твое видео!\n\n💬 Хочешь еще? Отправь новую ссылку!",
                                supports_streaming=True,
                                width=1280,
                                height=720
                            )
                        else:
                            # Отправить как документ (для других расширений)
                            bot.send_document(
                                chat_id,
                                f,
                                caption="✅ ГОТОВО!\n\n🎬 Вот твой файл!\n\n💬 Хочешь еще? Отправь новую ссылку!"
                            )

                    # Удалить сообщение о прогрессе
                    try:
                        bot.delete_message(chat_id, message_id)
                    except:
                        pass

                    # Удалить файл после отправки
                    try:
                        Path(file_path).unlink()
                        logger.info(f"Deleted file after sending: {file_path}")
                    except Exception as e:
                        logger.warning(f"Could not delete file {file_path}: {e}")

                except Exception as e:
                    logger.error(f"Error sending file: {e}")
                    try:
                        bot.edit_message_text(f"❌ Ошибка при отправке файла\n\n⚠️ {str(e)[:100]}", chat_id, message_id)
                    except:
                        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_download_"))
def handle_confirm_download_callback(call: types.CallbackQuery):
    """Обработка подтверждения скачивания не-YouTube видео."""
    parts = call.data.split("_")
    format_type = parts[2]
    user_id = int(parts[3])

    if call.from_user.id != user_id:
        bot.answer_callback_query(call.id, "❌ Это не твоя ссылка", show_alert=True)
        return

    try:
        emoji_map = {
            "4K": "📺", "2K": "🖥️", "1080p": "🎬", "720p": "🎥",
            "480p": "📹", "360p": "🎞️", "240p": "📱", "144p": "📟", "mp3": "🎵"
        }
        emoji = emoji_map.get(format_type, "📥")
        bot.edit_message_text(f"⚠️ ВНИМАНИЕ: Скачивание не-YouTube видео!\n\n⏳ Подготавливаю загрузку в качестве {emoji} {format_type}...", call.message.chat.id, call.message.message_id)
    except:
        pass

    # Получить URL из кеша
    with url_cache_lock:
        url = url_cache.get(user_id)

    if not url:
        bot.answer_callback_query(call.id, "❌ Ошибка: ссылка потеряна", show_alert=True)
        return

    logger.info(f"User {user_id} confirmed download for non-YouTube: {format_type}")

    # Проверить кеш готовых файлов
    cached_download = db.get_completed_download_by_url_format(url, format_type)
    if cached_download and cached_download["file_path"] and Path(cached_download["file_path"]).exists():
        # Файл уже есть, отправить из кеша
        logger.info(f"Using cached file for {url} {format_type}: {cached_download['file_path']}")
        _send_cached_file(user_id, cached_download, call.message.chat.id)
        bot.answer_callback_query(call.id, "✅ Файл из кеша отправлен", show_alert=False)
        return

    # Добавить в БД
    download_id = db.add_download(user_id, url, format_type=format_type)

    emoji_map = {
        "4K": "📺", "2K": "🖥️", "1080p": "🎬", "720p": "🎥",
        "480p": "📹", "360p": "🎞️", "240p": "📱", "144p": "📟", "mp3": "🎵"
    }
    emoji = emoji_map.get(format_type, "📥")
    progress_msg = bot.send_message(
        call.message.chat.id,
        f"⚠️ НЕ-YOUTUBE ВИДЕО\n📥 Стартую загрузку в качестве {emoji} {format_type}...\n0%"
    )

    with progress_lock:
        progress_messages[download_id] = (call.message.chat.id, progress_msg.message_id)

    update_thread = Thread(target=_update_progress_loop, args=(download_id, user_id), daemon=True)
    update_thread.start()

    bot.answer_callback_query(call.id, "✅ Загрузка запущена", show_alert=False)


@bot.callback_query_handler(func=lambda c: c.data.startswith("proceed_anyway_"))
def handle_proceed_anyway_callback(call: types.CallbackQuery):
    """Обработка кнопки 'Продолжить' для не-YouTube видео без выбора качества."""
    user_id = int(call.data.split("_")[2])

    if call.from_user.id != user_id:
        bot.answer_callback_query(call.id, "❌ Это не твоя ссылка", show_alert=True)
        return

    # Получить URL из кеша
    with url_cache_lock:
        url = url_cache.get(user_id)

    if not url:
        bot.answer_callback_query(call.id, "❌ Ошибка: ссылка потеряна", show_alert=True)
        return

    # Попытаться скачать в лучшем доступном качестве
    try:
        video_info = get_video_info(url)
        if video_info and video_info.get('available_formats'):
            # Взять первый (лучший) формат
            best_format = video_info['available_formats'][0]['label']
            # Имитировать callback для этого формата
            fake_call = types.CallbackQuery()
            fake_call.data = f"confirm_download_{best_format}_{user_id}"
            fake_call.from_user = call.from_user
            fake_call.message = call.message
            fake_call.id = call.id
            handle_confirm_download_callback(fake_call)
        else:
            # Если не удалось получить информацию, попробовать 720p
            fake_call = types.CallbackQuery()
            fake_call.data = f"confirm_download_720p_{user_id}"
            fake_call.from_user = call.from_user
            fake_call.message = call.message
            fake_call.id = call.id
            handle_confirm_download_callback(fake_call)
    except:
        bot.answer_callback_query(call.id, "❌ Не удалось определить формат, попробуйте выбрать качество вручную", show_alert=True)


@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel_"))
def handle_cancel_callback(call: types.CallbackQuery):
    """Обработка отмены."""
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass  # Если не удается удалить, игнорируем
    bot.answer_callback_query(call.id, "❌ Отменено", show_alert=False)


@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_priority_"))
def handle_confirm_priority_callback(call: types.CallbackQuery):
    """Подтверждение приоритета."""
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
        return
    
    purchase_id = int(call.data.split("_")[2])
    
    db.confirm_priority_purchase(purchase_id, PRIORITY_DAYS)
    
    purchase = db.get_priority_purchase(purchase_id)
    user_id = purchase["user_id"]
    
    try:
        bot.send_message(
            user_id,
            MESSAGES["priority_activated"]
        )
    except:
        pass
    
    bot.edit_message_text(f"✅ ПОДТВЕРЖДЕНО\n\n👤 User ID: {user_id}\n📅 Приоритет активирован на {PRIORITY_DAYS} дней", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id, "✅ Подтверждено", show_alert=False)
    
    logger.info(f"Admin confirmed priority for user {user_id}")


@bot.callback_query_handler(func=lambda c: c.data.startswith("reject_priority_"))
def handle_reject_priority_callback(call: types.CallbackQuery):
    """Отклонение приоритета."""
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
        return
    
    purchase_id = int(call.data.split("_")[2])
    
    db.reject_priority_purchase(purchase_id)
    
    purchase = db.get_priority_purchase(purchase_id)
    user_id = purchase["user_id"]
    
    try:
        bot.send_message(user_id, "❌ К сожалению, ваша заявка на приоритет отклонена 😔\n\nПопробуйте еще раз позже")
    except:
        pass
    
    bot.edit_message_text(f"❌ ОТКЛОНЕНО\n\n👤 User ID: {user_id}", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id, "✅ Отклонено", show_alert=False)


# ==================== Точка входа ====================

def run_bot():
    """Запустить бота."""
    logger.info("Starting KusokMedi bot...")

    # Инициализировать HTTP-сервер
    init_http_server(STORAGE_DIR)

    # Запустить worker очереди
    start_queue_worker()

    # Запустить очистку кешей каждые 10 минут
    def cleanup_task():
        while True:
            time.sleep(600)  # 10 минут
            cleanup_caches()

    import threading
    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()

    try:
        logger.info("Bot polling started")
        bot.infinity_polling(timeout=30, long_polling_timeout=30)
    except KeyboardInterrupt:
        logger.info("Bot interrupted")
    finally:
        stop_queue_worker()
        logger.info("Bot stopped")


if __name__ == "__main__":
    run_bot()

