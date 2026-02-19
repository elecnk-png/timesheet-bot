import os
import logging
import sqlite3
import csv
import io
import asyncio
from datetime import datetime, timedelta, date
from functools import wraps
from typing import Dict, List, Tuple, Optional, Any
import pytz
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ConversationHandler, ContextTypes
)

# КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: добавляем nest_asyncio для работы на хостинге
import nest_asyncio
nest_asyncio.apply()

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')

# Настройка часового пояса UTC+8
TIMEZONE = pytz.timezone('Asia/Singapore')
# Альтернативный вариант с zoneinfo:
# TIMEZONE = ZoneInfo("Asia/Singapore")

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
(
    SELECT_POSITION, SELECT_STORE, CREATE_POSITION_NAME,
    CREATE_STORE_NAME, CREATE_STORE_ADDRESS, CUSTOM_PERIOD_START,
    CUSTOM_PERIOD_END, DELETE_EMPLOYEE_REQUEST, DELETE_STORE_REQUEST,
    ASSIGN_SUPER_ADMIN_SELECT
) = range(10)

# Константы
MAX_MESSAGE_LENGTH = 4000

# Вспомогательные функции для работы с временем UTC+8
def get_now_utc8() -> datetime:
    """Получить текущее время в UTC+8"""
    return datetime.now(TIMEZONE)

def get_today_date_utc8() -> str:
    """Получить сегодняшнюю дату в UTC+8 в формате ISO"""
    return get_now_utc8().date().isoformat()

def get_current_time_utc8() -> str:
    """Получить текущее время в UTC+8 в формате ЧЧ:ММ"""
    return get_now_utc8().strftime('%H:%M')

def parse_datetime_utc8(date_str: str, time_str: str) -> datetime:
    """Создать datetime из даты и времени в UTC+8"""
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return TIMEZONE.localize(dt)

def format_datetime_utc8(dt: datetime) -> str:
    """Форматировать datetime в строку с временем UTC+8"""
    if dt.tzinfo is None:
        dt = TIMEZONE.localize(dt)
    else:
        dt = dt.astimezone(TIMEZONE)
    return dt.strftime('%d.%m.%Y %H:%M')

def format_time_utc8(dt: datetime) -> str:
    """Форматировать время в ЧЧ:ММ UTC+8"""
    if dt.tzinfo is None:
        dt = TIMEZONE.localize(dt)
    else:
        dt = dt.astimezone(TIMEZONE)
    return dt.strftime('%H:%M')

# Инициализация базы данных
def init_database():
    """Создание всех необходимых таблиц в базе данных"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Таблица сотрудников
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employees (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL,
            position TEXT NOT NULL,
            store TEXT NOT NULL,
            reg_date TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            is_super_admin INTEGER DEFAULT 0
        )
    ''')
    
    # Таблица табеля
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS timesheet (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT DEFAULT 'working',
            check_in TEXT,
            check_out TEXT,
            hours REAL DEFAULT 0,
            notes TEXT,
            confirmed INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES employees (user_id)
        )
    ''')
    
    # Таблица должностей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_by INTEGER NOT NULL,
            created_date TEXT NOT NULL
        )
    ''')
    
    # Таблица магазинов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            address TEXT,
            created_by INTEGER NOT NULL,
            created_date TEXT NOT NULL
        )
    ''')
    
    # Таблица запросов на удаление
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS delete_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_date TEXT NOT NULL,
            requester_id INTEGER NOT NULL,
            requester_name TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            target_name TEXT NOT NULL,
            status TEXT DEFAULT 'pending'
        )
    ''')
    
    # Таблица запросов на админа
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_date TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            user_position TEXT,
            user_store TEXT,
            status TEXT DEFAULT 'pending'
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

# Декоратор для проверки прав
def require_auth(admin_only=False, super_admin_only=False):
    """Декоратор для проверки авторизации и прав доступа"""
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id
            
            # Проверка зарегистрирован ли пользователь
            conn = sqlite3.connect('timesheet.db')
            cursor = conn.cursor()
            cursor.execute(
                "SELECT is_admin, is_super_admin FROM employees WHERE user_id = ?",
                (user_id,)
            )
            result = cursor.fetchone()
            conn.close()
            
            if not result:
                await update.effective_message.reply_text(
                    "❌ Вы не зарегистрированы. Используйте /start для регистрации."
                )
                return
            
            is_admin, is_super_admin = result
            
            # Проверка прав
            if super_admin_only and not is_super_admin:
                await update.effective_message.reply_text(
                    "❌ Эта функция доступна только супер-администраторам."
                )
                return
            
            if admin_only and not (is_admin or is_super_admin):
                await update.effective_message.reply_text(
                    "❌ Эта функция доступна только администраторам."
                )
                return
            
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator

# Функции для работы с БД
def get_user(user_id: int) -> Optional[Tuple]:
    """Получить информацию о пользователе"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT full_name, position, store, is_admin, is_super_admin FROM employees WHERE user_id = ?",
        (user_id,)
    )
    result = cursor.fetchone()
    conn.close()
    return result

def get_active_shift(user_id: int) -> Optional[Tuple]:
    """Получить активную смену пользователя"""
    today = get_today_date_utc8()
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, check_in FROM timesheet WHERE user_id = ? AND date = ? AND status = 'working'",
        (user_id, today)
    )
    result = cursor.fetchone()
    conn.close()
    return result

def get_positions() -> List[str]:
    """Получить список всех должностей"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM positions ORDER BY name")
    result = [row[0] for row in cursor.fetchall()]
    conn.close()
    return result

def get_stores() -> List[Tuple[str, str]]:
    """Получить список всех магазинов (название, адрес)"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute("SELECT name, address FROM stores ORDER BY name")
    result = cursor.fetchall()
    conn.close()
    return result

def get_super_admins() -> List[Tuple[int, str]]:
    """Получить список супер-администраторов"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, full_name FROM employees WHERE is_super_admin = 1 ORDER BY full_name"
    )
    result = cursor.fetchall()
    conn.close()
    return result

# ИСПРАВЛЕНИЕ 1: Функция для отмены регистрации
async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена регистрации"""
    await update.message.reply_text(
        "❌ Регистрация отменена. Если захотите зарегистрироваться, используйте /start"
    )
    return ConversationHandler.END

# Функции для удаления webhook
async def delete_webhook():
    """Удаление webhook перед запуском polling"""
    try:
        # Создаем временное приложение только для удаления webhook
        async with Application.builder().token(BOT_TOKEN).build() as app:
            # Удаляем webhook и параметр drop_pending_updates=True говорит Telegram 
            # не присылать обновления, которые были получены, пока бот был офлайн
            result = await app.bot.delete_webhook(drop_pending_updates=True)
            if result:
                logger.info("✅ Webhook успешно удален, ожидающие обновления сброшены.")
            else:
                logger.warning("⚠️ Не удалось удалить webhook (возможно, его и не было).")
    except Exception as e:
        logger.error(f"❌ Ошибка при удалении webhook: {e}")

# ИСПРАВЛЕНИЕ 2: Обновленная функция start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start - регистрация или главное меню"""
    user = update.effective_user
    user_id = user.id
    full_name = user.full_name
    
    logger.info(f"Команда /start от пользователя {user_id} ({full_name})")
    
    # Проверяем, зарегистрирован ли пользователь
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT is_admin, is_super_admin FROM employees WHERE user_id = ?",
        (user_id,)
    )
    employee = cursor.fetchone()
    
    if employee:
        # Пользователь уже зарегистрирован
        is_admin, is_super_admin = employee
        conn.close()
        
        if is_super_admin:
            await update.message.reply_text(
                f"👋 С возвращением, {full_name}!\n"
                f"Ваш статус: ⭐ Супер-администратор\n"
                f"Используйте /admin для входа в панель управления."
            )
        elif is_admin:
            await update.message.reply_text(
                f"👋 С возвращением, {full_name}!\n"
                f"Ваш статус: 👑 Администратор\n"
                f"Используйте /admin для входа в панель управления."
            )
        else:
            await update.message.reply_text(
                f"👋 С возвращением, {full_name}!\n"
                f"Используйте /checkin для начала смены или /timesheet для просмотра табеля."
            )
        return ConversationHandler.END
    
    # Проверяем, есть ли в системе супер-администраторы
    cursor.execute("SELECT COUNT(*) FROM employees WHERE is_super_admin = 1")
    super_admin_count = cursor.fetchone()[0]
    
    if super_admin_count == 0:
        # Первый пользователь становится супер-администратором
        cursor.execute('''
            INSERT INTO employees (user_id, full_name, position, store, reg_date, is_admin, is_super_admin)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, full_name, "Администратор", "Главный офис", 
              get_today_date_utc8(), 1, 1))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(
            "🎉 Вы зарегистрированы как первый супер-администратор!\n\n"
            "⚠️ Важно: Сейчас в системе нет должностей и магазинов.\n"
            "1️⃣ Используйте /admin для входа в панель администратора\n"
            "2️⃣ Создайте должности в разделе 'Управление должностями'\n"
            "3️⃣ Создайте магазины в разделе 'Управление магазинами'\n\n"
            "Только после этого другие сотрудники смогут регистрироваться."
        )
        return ConversationHandler.END
    else:
        # Проверяем наличие должностей и магазинов
        cursor.execute("SELECT COUNT(*) FROM positions")
        positions_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM stores")
        stores_count = cursor.fetchone()[0]
        conn.close()
        
        if positions_count == 0 or stores_count == 0:
            # Нет должностей или магазинов - предлагаем стать администратором
            keyboard = [
                [InlineKeyboardButton("👑 Стать администратором", callback_data="request_admin")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "👋 Добро пожаловать!\n\n"
                "⚠️ В системе пока нет должностей или магазинов.\n"
                "Вы можете подать заявку на становление администратором:",
                reply_markup=reply_markup
            )
            return ConversationHandler.END
        else:
            # Есть должности и магазины - начинаем регистрацию
            positions = get_positions()
            if not positions:
                await update.message.reply_text(
                    "❌ В системе нет должностей. Обратитесь к администратору."
                )
                return ConversationHandler.END
            
            # Создаем клавиатуру с должностями
            keyboard = []
            for pos in positions:
                keyboard.append([InlineKeyboardButton(pos, callback_data=f"reg_pos_{pos}")])
            
            # Добавляем кнопку отмены
            keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_registration")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "📝 Для регистрации выберите вашу должность:",
                reply_markup=reply_markup
            )
            return SELECT_POSITION

async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отметка начала рабочего дня"""
    user_id = update.effective_user.id
    
    # Проверка регистрации
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
    # Проверка, нет ли уже активной смены
    active_shift = get_active_shift(user_id)
    if active_shift:
        await update.message.reply_text(
            f"❌ У вас уже есть активная смена, начатая в {format_time_utc8(datetime.fromisoformat(active_shift[1]))}"
        )
        return
    
    # Создаем новую смену
    now = get_now_utc8()
    today = now.date().isoformat()
    checkin_time = now.isoformat()
    
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO timesheet (user_id, date, status, check_in)
        VALUES (?, ?, ?, ?)
    ''', (user_id, today, 'working', checkin_time))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        f"✅ Начало смены отмечено в {format_time_utc8(now)}\n"
        f"📅 Дата: {today}\n"
        f"Не забудьте отметить конец смены командой /checkout"
    )

async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отметка конца рабочего дня"""
    user_id = update.effective_user.id
    
    # Проверка регистрации
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
    # Проверка наличия активной смены
    active_shift = get_active_shift(user_id)
    if not active_shift:
        await update.message.reply_text(
            "❌ У вас нет активной смены. Используйте /checkin для начала смены"
        )
        return
    
    shift_id, checkin_time_str = active_shift
    checkin_time = datetime.fromisoformat(checkin_time_str)
    checkout_time = get_now_utc8()
    
    # Расчет отработанных часов
    hours_worked = (checkout_time - checkin_time).total_seconds() / 3600
    
    # Обновляем запись
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE timesheet 
        SET status = 'completed', check_out = ?, hours = ?
        WHERE id = ?
    ''', (checkout_time.isoformat(), round(hours_worked, 2), shift_id))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        f"✅ Конец смены отмечен в {format_time_utc8(checkout_time)}\n"
        f"⏱ Отработано часов: {hours_worked:.2f}"
    )

async def timesheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр табеля за указанный период"""
    user_id = update.effective_user.id
    
    # Проверка регистрации
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
    # Парсим аргументы
    args = context.args
    days = 7  # по умолчанию
    
    if args and args[0].isdigit():
        days = int(args[0])
    
    # Получаем записи за период
    end_date = get_today_date_utc8()
    start_date = (datetime.now(TIMEZONE) - timedelta(days=days-1)).date().isoformat()
    
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT date, check_in, check_out, hours, confirmed, notes
        FROM timesheet
        WHERE user_id = ? AND date BETWEEN ? AND ? AND status = 'completed'
        ORDER BY date DESC
    ''', (user_id, start_date, end_date))
    records = cursor.fetchall()
    conn.close()
    
    if not records:
        await update.message.reply_text(f"📊 Нет записей за последние {days} дней")
        return
    
    # Формируем отчет
    report = f"📋 ТАБЕЛЬ ЗА {days} ДНЕЙ\n\n"
    total_hours = 0
    
    for record in records:
        date_str, checkin, checkout, hours, confirmed, notes = record
        
        checkin_time = format_time_utc8(datetime.fromisoformat(checkin)) if checkin else "-"
        checkout_time = format_time_utc8(datetime.fromisoformat(checkout)) if checkout else "-"
        confirmed_mark = "✅" if confirmed else "❌"
        
        report += f"📅 {date_str}\n"
        report += f"   Начало: {checkin_time}\n"
        report += f"   Конец: {checkout_time}\n"
        report += f"   Часов: {hours}\n"
        report += f"   Подтверждено: {confirmed_mark}\n"
        if notes:
            report += f"   📝 {notes}\n"
        report += "\n"
        
        total_hours += hours
    
    report += f"📊 ИТОГО: {total_hours:.2f} часов"
    
    await update.message.reply_text(report)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика за 30 дней по дням недели"""
    user_id = update.effective_user.id
    
    # Проверка регистрации
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
    # Получаем статистику за 30 дней
    end_date = get_today_date_utc8()
    start_date = (datetime.now(TIMEZONE) - timedelta(days=29)).date().isoformat()
    
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT date, hours
        FROM timesheet
        WHERE user_id = ? AND date BETWEEN ? AND ? AND status = 'completed'
    ''', (user_id, start_date, end_date))
    records = cursor.fetchall()
    conn.close()
    
    if not records:
        await update.message.reply_text("📊 Нет данных за последние 30 дней")
        return
    
    # Анализ по дням недели
    day_stats = {
        0: {'name': 'Пн', 'count': 0, 'hours': 0},
        1: {'name': 'Вт', 'count': 0, 'hours': 0},
        2: {'name': 'Ср', 'count': 0, 'hours': 0},
        3: {'name': 'Чт', 'count': 0, 'hours': 0},
        4: {'name': 'Пт', 'count': 0, 'hours': 0},
        5: {'name': 'Сб', 'count': 0, 'hours': 0},
        6: {'name': 'Вс', 'count': 0, 'hours': 0}
    }
    
    total_days = 0
    total_hours = 0
    
    for record in records:
        date_str, hours = record
        dt = datetime.fromisoformat(date_str)
        weekday = dt.weekday()
        
        day_stats[weekday]['count'] += 1
        day_stats[weekday]['hours'] += hours
        total_days += 1
        total_hours += hours
    
    # Формируем отчет
    report = "📊 СТАТИСТИКА ЗА 30 ДНЕЙ\n\n"
    report += "По дням недели:\n"
    
    for i in range(7):
        stats = day_stats[i]
        if stats['count'] > 0:
            avg_hours = stats['hours'] / stats['count']
            report += f"{stats['name']}: {stats['count']} дн., "
            report += f"в среднем {avg_hours:.2f} ч/день\n"
        else:
            report += f"{stats['name']}: нет данных\n"
    
    report += f"\n📈 Всего дней: {total_days}\n"
    report += f"📈 Всего часов: {total_hours:.2f}\n"
    report += f"📈 Среднее: {total_hours/total_days:.2f} ч/день"
    
    await update.message.reply_text(report)

@require_auth(admin_only=True)
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Панель администратора"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    is_super_admin = user[4] if user else 0
    
    # Основное меню админа
    keyboard = [
        [InlineKeyboardButton("👥 Все сотрудники", callback_data="admin_list")],
        [InlineKeyboardButton("📊 По магазинам", callback_data="admin_by_store")],
        [InlineKeyboardButton("📥 Экспорт CSV (подтв.)", callback_data="admin_export_menu")],
        [InlineKeyboardButton("📥 Экспорт CSV (все)", callback_data="admin_export_all_menu")],
        [InlineKeyboardButton("📅 Выбрать период", callback_data="period_selection")],
        [InlineKeyboardButton("📈 Статистика по магазинам", callback_data="admin_store_stats")],
        [InlineKeyboardButton("✅ Подтверждение смен", callback_data="admin_confirm")],
        [InlineKeyboardButton("🗑 Запросить удаление", callback_data="admin_delete_menu")],
        [InlineKeyboardButton("📋 Управление должностями", callback_data="admin_positions_menu")],
        [InlineKeyboardButton("🏪 Управление магазинами", callback_data="admin_stores_menu")],
    ]
    
    # Кнопки для супер-админа
    if is_super_admin:
        keyboard.extend([
            [InlineKeyboardButton("➕ Добавить админа", callback_data="admin_add")],
            [InlineKeyboardButton("📋 Запросы на удаление", callback_data="admin_requests")],
            [InlineKeyboardButton("👑 Заявки в админы", callback_data="admin_admin_requests")],
            [InlineKeyboardButton("⭐ Управление супер-админами", callback_data="assign_super_admin_menu")],
        ])
    
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="close")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🔐 ПАНЕЛЬ АДМИНИСТРАТОРА\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )

# ИСПРАВЛЕНИЕ 3: Обновленная функция button_callback
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на инлайн кнопки"""
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    user_id = query.from_user.id
    
    logger.info(f"Callback: {callback_data} от пользователя {user_id}")
    
    # ИСПРАВЛЕНИЕ: Обработка отмены регистрации
    if callback_data == "cancel_registration":
        await query.edit_message_text("❌ Регистрация отменена.")
        return ConversationHandler.END
    
    # Получаем информацию о пользователе
    user = get_user(user_id)
    
    # ИСПРАВЛЕНИЕ: Обработка регистрации (до проверки авторизации)
    if callback_data.startswith("reg_pos_"):
        if user:
            await query.edit_message_text("❌ Вы уже зарегистрированы!")
            return ConversationHandler.END
            
        position = callback_data[8:]
        context.user_data['reg_position'] = position
        
        stores = get_stores()
        if not stores:
            await query.edit_message_text(
                "❌ В системе нет магазинов. Обратитесь к администратору."
            )
            return ConversationHandler.END
        
        keyboard = []
        for store_name, address in stores:
            keyboard.append([
                InlineKeyboardButton(f"{store_name}", callback_data=f"reg_store_{store_name}")
            ])
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_registration")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "🏪 Теперь выберите ваш магазин:",
            reply_markup=reply_markup
        )
        return SELECT_STORE
    
    # ИСПРАВЛЕНИЕ: Обработка выбора магазина
    elif callback_data.startswith("reg_store_"):
        if user:
            await query.edit_message_text("❌ Вы уже зарегистрированы!")
            return ConversationHandler.END
            
        store = callback_data[10:]
        position = context.user_data.get('reg_position')
        
        if not position:
            await query.edit_message_text(
                "❌ Ошибка регистрации. Пожалуйста, начните заново с /start"
            )
            return ConversationHandler.END
        
        # Завершаем регистрацию
        full_name = query.from_user.full_name
        
        conn = sqlite3.connect('timesheet.db')
        cursor = conn.cursor()
        
        try:
            # Проверяем, не зарегистрирован ли уже пользователь
            cursor.execute("SELECT user_id FROM employees WHERE user_id = ?", (user_id,))
            if cursor.fetchone():
                await query.edit_message_text(
                    "❌ Вы уже зарегистрированы! Используйте /start"
                )
                conn.close()
                return ConversationHandler.END
            
            # Регистрируем нового пользователя
            cursor.execute('''
                INSERT INTO employees (user_id, full_name, position, store, reg_date, is_admin, is_super_admin)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, full_name, position, store, get_today_date_utc8(), 0, 0))
            conn.commit()
            
            logger.info(f"Новый пользователь зарегистрирован: {user_id} - {full_name} ({position}, {store})")
            
            await query.edit_message_text(
                f"✅ Регистрация успешно завершена!\n\n"
                f"👤 {full_name}\n"
                f"📋 Должность: {position}\n"
                f"🏪 Магазин: {store}\n\n"
                f"Теперь вы можете использовать команды:\n"
                f"/checkin - начать рабочий день\n"
                f"/checkout - закончить рабочий день\n"
                f"/timesheet - посмотреть табель\n"
                f"/stats - статистика"
            )
            
        except Exception as e:
            logger.error(f"Ошибка при регистрации: {e}")
            await query.edit_message_text(
                "❌ Произошла ошибка при регистрации. Попробуйте позже."
            )
        finally:
            conn.close()
        
        # Очищаем временные данные
        context.user_data.pop('reg_position', None)
        
        return ConversationHandler.END
    
    # Для всех остальных callback_data проверяем авторизацию
    if not user:
        await query.edit_message_text("❌ Вы не зарегистрированы. Используйте /start")
        return
    
    full_name, position, store, is_admin, is_super_admin = user
    
    if callback_data == "close":
        await query.delete_message()
        return
    
    elif callback_data == "request_admin":
        await handle_admin_request(query, context, user_id, user)
    
    elif callback_data == "admin_list":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_all_employees(query)
    
    elif callback_data == "admin_by_store":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_employees_by_store(query)
    
    elif callback_data == "admin_export_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_export_menu_confirmed(query)
    
    elif callback_data == "admin_export_all_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_export_menu_all(query)
    
    elif callback_data == "period_selection":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_period_selection(query)
    
    elif callback_data == "admin_store_stats":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_store_stats(query)
    
    elif callback_data == "admin_confirm":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_confirm_menu(query)
    
    elif callback_data == "admin_delete_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_delete_menu(query)
    
    elif callback_data == "admin_positions_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_positions_menu(query)
    
    elif callback_data == "admin_stores_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_stores_menu(query)
    
    elif callback_data == "back_to_admin":
        await show_admin_panel(query)
    
    elif callback_data == "create_position":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await query.edit_message_text(
            "✏️ Введите название новой должности:"
        )
        return CREATE_POSITION_NAME
    
    elif callback_data == "list_positions":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await list_positions(query)
    
    elif callback_data == "delete_position_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_delete_position_menu(query)
    
    elif callback_data.startswith("delete_position_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        position_name = callback_data[15:]
        await delete_position(query, position_name)
    
    elif callback_data == "create_store":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await query.edit_message_text(
            "✏️ Введите название магазина:"
        )
        return CREATE_STORE_NAME
    
    elif callback_data == "list_stores":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await list_stores(query)
    
    elif callback_data == "delete_store_from_list_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_delete_store_menu(query)
    
    elif callback_data.startswith("delete_store_list_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        store_name = callback_data[17:]
        await delete_store(query, store_name)
    
    elif callback_data.startswith("export_store_confirmed_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        store = callback_data[22:]
        await export_csv(query, store, confirmed_only=True)
    
    elif callback_data == "export_store_confirmed_all":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await export_csv(query, "all", confirmed_only=True)
    
    elif callback_data.startswith("export_store_all_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        store = callback_data[16:]
        await export_csv(query, store, confirmed_only=False)
    
    elif callback_data == "export_store_all_all":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await export_csv(query, "all", confirmed_only=False)
    
    elif callback_data.startswith("period_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        
        period = callback_data[7:]
        if period == "custom":
            await query.edit_message_text(
                "📅 Введите начальную дату в формате ГГГГ-ММ-ДД:"
            )
            return CUSTOM_PERIOD_START
        else:
            days = 0
            if period == "7":
                days = 7
            elif period == "14":
                days = 14
            elif period == "30":
                days = 30
            elif period == "90":
                days = 90
            elif period == "all":
                days = 36500  # примерно 100 лет
            
            context.user_data['period_days'] = days
            await show_export_options(query, days)
    
    elif callback_data == "export_confirmed":
        if not (is_admin or is_super_admin):
            return
        days = context.user_data.get('period_days', 30)
        await export_csv_period(query, days, confirmed_only=True)
    
    elif callback_data == "export_all":
        if not (is_admin or is_super_admin):
            return
        days = context.user_data.get('period_days', 30)
        await export_csv_period(query, days, confirmed_only=False)
    
    elif callback_data == "confirm_today":
        if not (is_admin or is_super_admin):
            return
        await show_unconfirmed_today(query)
    
    elif callback_data == "confirm_period":
        if not (is_admin or is_super_admin):
            return
        await show_period_confirm_menu(query)
    
    elif callback_data == "confirm_all_today":
        if not (is_admin or is_super_admin):
            return
        await confirm_all_today(query)
    
    elif callback_data == "confirm_by_store":
        if not (is_admin or is_super_admin):
            return
        await show_confirm_by_store(query)
    
    elif callback_data == "confirm_stats":
        if not (is_admin or is_super_admin):
            return
        await show_confirm_stats(query)
    
    elif callback_data.startswith("confirm_period_"):
        if not (is_admin or is_super_admin):
            return
        days = int(callback_data[14:])
        await show_unconfirmed_period(query, days)
    
    elif callback_data.startswith("confirm_store_"):
        if not (is_admin or is_super_admin):
            return
        store = callback_data[14:]
        await show_store_unconfirmed(query, store)
    
    elif callback_data.startswith("confirm_all_store_"):
        if not (is_admin or is_super_admin):
            return
        store = callback_data[17:]
        await confirm_all_store(query, store)
    
    elif callback_data.startswith("confirm_shift_"):
        if not (is_admin or is_super_admin):
            return
        shift_id = int(callback_data[14:])
        await confirm_shift(query, shift_id)
    
    elif callback_data == "back_to_confirm":
        await show_confirm_menu(query)
    
    elif callback_data == "delete_employee_menu":
        if not (is_admin or is_super_admin):
            return
        await show_delete_employee_menu(query)
    
    elif callback_data == "delete_store_menu":
        if not (is_admin or is_super_admin):
            return
        await show_delete_store_request_menu(query)
    
    elif callback_data.startswith("request_delete_employee_"):
        if not (is_admin or is_super_admin):
            return
        target_id = int(callback_data[23:])
        await create_delete_request(query, user_id, full_name, "employee", str(target_id))
    
    elif callback_data.startswith("request_delete_store_"):
        if not (is_admin or is_super_admin):
            return
        store_name = callback_data[20:]
        await create_delete_request(query, user_id, full_name, "store", store_name)
    
    elif callback_data == "admin_requests":
        if not is_super_admin:
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_delete_requests(query)
    
    elif callback_data.startswith("approve_request_"):
        if not is_super_admin:
            return
        request_id = int(callback_data[16:])
        await approve_delete_request(query, request_id)
    
    elif callback_data.startswith("reject_request_"):
        if not is_super_admin:
            return
        request_id = int(callback_data[15:])
        await reject_delete_request(query, request_id)
    
    elif callback_data == "admin_admin_requests":
        if not is_super_admin:
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_admin_requests(query)
    
    elif callback_data.startswith("approve_admin_"):
        if not is_super_admin:
            return
        req_id = int(callback_data[14:])
        await approve_admin_request(query, req_id)
    
    elif callback_data.startswith("reject_admin_"):
        if not is_super_admin:
            return
        req_id = int(callback_data[13:])
        await reject_admin_request(query, req_id)
    
    elif callback_data == "assign_super_admin_menu":
        if not is_super_admin:
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_assign_super_admin_menu(query)
    
    elif callback_data == "assign_super_admin_list":
        if not is_super_admin:
            return
        await show_assign_super_admin_list(query)
    
    elif callback_data == "list_super_admins":
        if not is_super_admin:
            return
        await list_super_admins(query)
    
    elif callback_data.startswith("select_super_admin_"):
        if not is_super_admin:
            return
        target_id = int(callback_data[19:])
        context.user_data['selected_super_admin'] = target_id
        await confirm_assign_super_admin(query, target_id)
    
    elif callback_data == "confirm_assign_super_admin":
        if not is_super_admin:
            return
        target_id = context.user_data.get('selected_super_admin')
        if target_id:
            await assign_super_admin(query, target_id)
    
    elif callback_data == "admin_add":
        if not is_super_admin:
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_add_admin_menu(query)

# Вспомогательные функции для административных панелей
async def show_admin_panel(query):
    """Показать панель администратора"""
    keyboard = [
        [InlineKeyboardButton("👥 Все сотрудники", callback_data="admin_list")],
        [InlineKeyboardButton("📊 По магазинам", callback_data="admin_by_store")],
        [InlineKeyboardButton("📥 Экспорт CSV (подтв.)", callback_data="admin_export_menu")],
        [InlineKeyboardButton("📥 Экспорт CSV (все)", callback_data="admin_export_all_menu")],
        [InlineKeyboardButton("📅 Выбрать период", callback_data="period_selection")],
        [InlineKeyboardButton("📈 Статистика по магазинам", callback_data="admin_store_stats")],
        [InlineKeyboardButton("✅ Подтверждение смен", callback_data="admin_confirm")],
        [InlineKeyboardButton("🗑 Запросить удаление", callback_data="admin_delete_menu")],
        [InlineKeyboardButton("📋 Управление должностями", callback_data="admin_positions_menu")],
        [InlineKeyboardButton("🏪 Управление магазинами", callback_data="admin_stores_menu")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="close")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🔐 ПАНЕЛЬ АДМИНИСТРАТОРА\n\nВыберите действие:",
        reply_markup=reply_markup
    )

async def show_all_employees(query):
    """Показать всех сотрудников"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT full_name, position, store, is_admin, is_super_admin 
        FROM employees ORDER BY store, full_name
    ''')
    employees = cursor.fetchall()
    conn.close()
    
    if not employees:
        await query.edit_message_text("👥 Нет зарегистрированных сотрудников")
        return
    
    text = "👥 ВСЕ СОТРУДНИКИ\n\n"
    for emp in employees:
        full_name, position, store, is_admin, is_super_admin = emp
        role = "⭐ Супер-админ" if is_super_admin else "👑 Админ" if is_admin else "👤 Сотрудник"
        text += f"• {full_name}\n  {role} | {position} | {store}\n\n"
    
    # Разбиваем длинное сообщение
    if len(text) > MAX_MESSAGE_LENGTH:
        for i in range(0, len(text), MAX_MESSAGE_LENGTH):
            part = text[i:i+MAX_MESSAGE_LENGTH]
            if i == 0:
                await query.edit_message_text(part)
            else:
                await query.message.reply_text(part)
    else:
        await query.edit_message_text(text)
    
    # Добавляем кнопку "Назад"
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def show_employees_by_store(query):
    """Показать сотрудников по магазинам"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT store, full_name, position, is_admin, is_super_admin 
        FROM employees ORDER BY store, full_name
    ''')
    employees = cursor.fetchall()
    conn.close()
    
    if not employees:
        await query.edit_message_text("👥 Нет зарегистрированных сотрудников")
        return
    
    # Группируем по магазинам
    stores_dict = {}
    for emp in employees:
        store, full_name, position, is_admin, is_super_admin = emp
        if store not in stores_dict:
            stores_dict[store] = []
        
        role = "⭐" if is_super_admin else "👑" if is_admin else "👤"
        stores_dict[store].append(f"{role} {full_name} - {position}")
    
    text = "📊 СОТРУДНИКИ ПО МАГАЗИНАМ\n\n"
    for store, employees_list in stores_dict.items():
        text += f"🏪 {store}\n"
        for emp in employees_list:
            text += f"  {emp}\n"
        text += "\n"
    
    # Разбиваем длинное сообщение
    if len(text) > MAX_MESSAGE_LENGTH:
        for i in range(0, len(text), MAX_MESSAGE_LENGTH):
            part = text[i:i+MAX_MESSAGE_LENGTH]
            if i == 0:
                await query.edit_message_text(part)
            else:
                await query.message.reply_text(part)
    else:
        await query.edit_message_text(text)
    
    # Добавляем кнопку "Назад"
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def show_export_menu_confirmed(query):
    """Меню экспорта подтвержденных смен"""
    stores = get_stores()
    
    keyboard = []
    for store_name, address in stores:
        keyboard.append([
            InlineKeyboardButton(f"🏪 {store_name}", 
                               callback_data=f"export_store_confirmed_{store_name}")
        ])
    
    keyboard.append([
        InlineKeyboardButton("📊 Все магазины", callback_data="export_store_confirmed_all")
    ])
    keyboard.append([
        InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "📥 ЭКСПОРТ CSV (ТОЛЬКО ПОДТВЕРЖДЕННЫЕ)\n\n"
        "Выберите магазин:",
        reply_markup=reply_markup
    )

async def show_export_menu_all(query):
    """Меню экспорта всех смен"""
    stores = get_stores()
    
    keyboard = []
    for store_name, address in stores:
        keyboard.append([
            InlineKeyboardButton(f"🏪 {store_name}", 
                               callback_data=f"export_store_all_{store_name}")
        ])
    
    keyboard.append([
        InlineKeyboardButton("📊 Все магазины", callback_data="export_store_all_all")
    ])
    keyboard.append([
        InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "📥 ЭКСПОРТ CSV (ВСЕ СМЕНЫ)\n\n"
        "Выберите магазин:",
        reply_markup=reply_markup
    )

async def show_period_selection(query):
    """Меню выбора периода"""
    keyboard = [
        [InlineKeyboardButton("📅 Последние 7 дней", callback_data="period_7")],
        [InlineKeyboardButton("📅 Последние 14 дней", callback_data="period_14")],
        [InlineKeyboardButton("📅 Последние 30 дней", callback_data="period_30")],
        [InlineKeyboardButton("📅 Последние 90 дней", callback_data="period_90")],
        [InlineKeyboardButton("📅 Весь период", callback_data="period_all")],
        [InlineKeyboardButton("📅 Выбрать даты", callback_data="period_custom")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "📅 ВЫБОР ПЕРИОДА\n\n"
        "Выберите период для экспорта:",
        reply_markup=reply_markup
    )

async def show_export_options(query, days):
    """Показать опции экспорта после выбора периода"""
    keyboard = [
        [InlineKeyboardButton("📥 CSV (только подтвержденные)", callback_data="export_confirmed")],
        [InlineKeyboardButton("📥 CSV (все смены)", callback_data="export_all")],
        [InlineKeyboardButton("◀️ Назад", callback_data="period_selection")]
    ]
    
    period_text = "весь период" if days > 365 else f"последние {days} дней"
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"📊 Период: {period_text}\n\n"
        f"Выберите тип экспорта:",
        reply_markup=reply_markup
    )

async def show_store_stats(query):
    """Показать статистику по магазинам"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Получаем список магазинов
    cursor.execute("SELECT name FROM stores")
    stores = cursor.fetchall()
    
    if not stores:
        await query.edit_message_text("❌ Нет созданных магазинов")
        return
    
    text = "📈 СТАТИСТИКА ПО МАГАЗИНАМ\n\n"
    
    for store in stores:
        store_name = store[0]
        
        # Количество сотрудников
        cursor.execute(
            "SELECT COUNT(*) FROM employees WHERE store = ?",
            (store_name,)
        )
        emp_count = cursor.fetchone()[0]
        
        # Количество смен за последние 30 дней
        today = get_today_date_utc8()
        month_ago = (datetime.now(TIMEZONE) - timedelta(days=30)).date().isoformat()
        
        cursor.execute('''
            SELECT COUNT(DISTINCT t.id), SUM(t.hours), COUNT(DISTINCT t.user_id)
            FROM timesheet t
            JOIN employees e ON t.user_id = e.user_id
            WHERE e.store = ? AND t.date BETWEEN ? AND ? AND t.status = 'completed'
        ''', (store_name, month_ago, today))
        
        shifts, total_hours, active_employees = cursor.fetchone()
        shifts = shifts or 0
        total_hours = total_hours or 0
        active_employees = active_employees or 0
        
        text += f"🏪 {store_name}\n"
        text += f"   👥 Сотрудников: {emp_count}\n"
        text += f"   📊 Активных (30 дн): {active_employees}\n"
        text += f"   📅 Смен (30 дн): {shifts}\n"
        text += f"   ⏱ Часов (30 дн): {total_hours:.2f}\n\n"
    
    conn.close()
    
    await query.edit_message_text(text)
    
    # Добавляем кнопку "Назад"
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def show_confirm_menu(query):
    """Меню подтверждения смен"""
    keyboard = [
        [InlineKeyboardButton("📋 Неподтвержденные сегодня", callback_data="confirm_today")],
        [InlineKeyboardButton("📅 Неподтвержденные за период", callback_data="confirm_period")],
        [InlineKeyboardButton("✅ Подтвердить все сегодня", callback_data="confirm_all_today")],
        [InlineKeyboardButton("🏪 По магазинам", callback_data="confirm_by_store")],
        [InlineKeyboardButton("📊 Статистика подтверждений", callback_data="confirm_stats")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "✅ ПОДТВЕРЖДЕНИЕ СМЕН\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )

async def show_delete_menu(query):
    """Меню запросов на удаление"""
    keyboard = [
        [InlineKeyboardButton("👤 Запросить удаление сотрудника", callback_data="delete_employee_menu")],
        [InlineKeyboardButton("🏪 Запросить удаление магазина", callback_data="delete_store_menu")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🗑 ЗАПРОС УДАЛЕНИЯ\n\n"
        "Выберите тип удаления:",
        reply_markup=reply_markup
    )

async def show_positions_menu(query):
    """Меню управления должностями"""
    keyboard = [
        [InlineKeyboardButton("➕ Создать должность", callback_data="create_position")],
        [InlineKeyboardButton("📋 Список должностей", callback_data="list_positions")],
        [InlineKeyboardButton("🗑 Удалить должность", callback_data="delete_position_menu")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "📋 УПРАВЛЕНИЕ ДОЛЖНОСТЯМИ\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )

async def show_stores_menu(query):
    """Меню управления магазинами"""
    keyboard = [
        [InlineKeyboardButton("➕ Создать магазин", callback_data="create_store")],
        [InlineKeyboardButton("📋 Список магазинов", callback_data="list_stores")],
        [InlineKeyboardButton("🗑 Удалить магазин", callback_data="delete_store_from_list_menu")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🏪 УПРАВЛЕНИЕ МАГАЗИНАМИ\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )

# Функции для управления должностями
async def create_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание новой должности"""
    user_id = update.effective_user.id
    position_name = update.message.text.strip()
    
    # Проверка на существование
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT INTO positions (name, created_by, created_date)
            VALUES (?, ?, ?)
        ''', (position_name, user_id, get_today_date_utc8()))
        conn.commit()
        await update.message.reply_text(f"✅ Должность '{position_name}' создана!")
    except sqlite3.IntegrityError:
        await update.message.reply_text(f"❌ Должность '{position_name}' уже существует")
    finally:
        conn.close()
    
    # Возвращаемся в меню должностей
    keyboard = [
        [InlineKeyboardButton("◀️ Назад в управление должностями", callback_data="admin_positions_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Выберите действие:",
        reply_markup=reply_markup
    )
    
    return ConversationHandler.END

async def list_positions(query):
    """Показать список должностей"""
    positions = get_positions()
    
    if not positions:
        await query.edit_message_text("📋 Список должностей пуст")
        return
    
    text = "📋 СПИСОК ДОЛЖНОСТЕЙ\n\n"
    for i, pos in enumerate(positions, 1):
        text += f"{i}. {pos}\n"
    
    await query.edit_message_text(text)
    
    # Добавляем кнопку "Назад"
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="admin_positions_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def show_delete_position_menu(query):
    """Меню удаления должностей"""
    positions = get_positions()
    
    if not positions:
        await query.edit_message_text("📋 Нет должностей для удаления")
        return
    
    keyboard = []
    for pos in positions:
        # Проверяем, используется ли должность
        conn = sqlite3.connect('timesheet.db')
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM employees WHERE position = ?", (pos,))
        count = cursor.fetchone()[0]
        conn.close()
        
        if count == 0:
            keyboard.append([
                InlineKeyboardButton(f"🗑 {pos}", callback_data=f"delete_position_{pos}")
            ])
    
    if not keyboard:
        await query.edit_message_text(
            "❌ Нет должностей, которые можно удалить\n"
            "(все должности используются сотрудниками)"
        )
        return
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_positions_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🗑 Выберите должность для удаления:",
        reply_markup=reply_markup
    )

async def delete_position(query, position_name):
    """Удаление должности"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Проверяем, используется ли должность
    cursor.execute("SELECT COUNT(*) FROM employees WHERE position = ?", (position_name,))
    count = cursor.fetchone()[0]
    
    if count > 0:
        await query.edit_message_text(
            f"❌ Невозможно удалить должность '{position_name}'\n"
            f"Она используется {count} сотрудником(ами)"
        )
        conn.close()
        return
    
    # Удаляем должность
    cursor.execute("DELETE FROM positions WHERE name = ?", (position_name,))
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Должность '{position_name}' удалена")
    
    # Возвращаемся в меню
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="admin_positions_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Функции для управления магазинами
async def create_store_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение названия магазина"""
    store_name = update.message.text.strip()
    context.user_data['new_store_name'] = store_name
    
    await update.message.reply_text(
        f"🏪 Название: {store_name}\n\n"
        f"✏️ Теперь введите адрес магазина:"
    )
    return CREATE_STORE_ADDRESS

async def create_store_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание магазина с адресом"""
    user_id = update.effective_user.id
    store_address = update.message.text.strip()
    store_name = context.user_data.get('new_store_name')
    
    if not store_name:
        await update.message.reply_text("❌ Ошибка создания. Начните заново.")
        return ConversationHandler.END
    
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT INTO stores (name, address, created_by, created_date)
            VALUES (?, ?, ?, ?)
        ''', (store_name, store_address, user_id, get_today_date_utc8()))
        conn.commit()
        await update.message.reply_text(
            f"✅ Магазин создан!\n\n"
            f"Название: {store_name}\n"
            f"Адрес: {store_address}"
        )
    except sqlite3.IntegrityError:
        await update.message.reply_text(f"❌ Магазин '{store_name}' уже существует")
    finally:
        conn.close()
    
    # Возвращаемся в меню магазинов
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="admin_stores_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Выберите действие:",
        reply_markup=reply_markup
    )
    
    # Очищаем временные данные
    context.user_data.pop('new_store_name', None)
    
    return ConversationHandler.END

async def list_stores(query):
    """Показать список магазинов"""
    stores = get_stores()
    
    if not stores:
        await query.edit_message_text("🏪 Список магазинов пуст")
        return
    
    text = "🏪 СПИСОК МАГАЗИНОВ\n\n"
    for i, (name, address) in enumerate(stores, 1):
        text += f"{i}. {name}\n   📍 {address}\n\n"
    
    await query.edit_message_text(text)
    
    # Добавляем кнопку "Назад"
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="admin_stores_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def show_delete_store_menu(query):
    """Меню удаления магазинов"""
    stores = get_stores()
    
    if not stores:
        await query.edit_message_text("🏪 Нет магазинов для удаления")
        return
    
    keyboard = []
    for store_name, address in stores:
        # Проверяем, используется ли магазин
        conn = sqlite3.connect('timesheet.db')
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM employees WHERE store = ?", (store_name,))
        count = cursor.fetchone()[0]
        conn.close()
        
        if count == 0:
            keyboard.append([
                InlineKeyboardButton(f"🗑 {store_name}", callback_data=f"delete_store_list_{store_name}")
            ])
    
    if not keyboard:
        await query.edit_message_text(
            "❌ Нет магазинов, которые можно удалить\n"
            "(во всех магазинах есть сотрудники)"
        )
        return
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_stores_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🗑 Выберите магазин для удаления:",
        reply_markup=reply_markup
    )

async def delete_store(query, store_name):
    """Удаление магазина"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Проверяем, используется ли магазин
    cursor.execute("SELECT COUNT(*) FROM employees WHERE store = ?", (store_name,))
    count = cursor.fetchone()[0]
    
    if count > 0:
        await query.edit_message_text(
            f"❌ Невозможно удалить магазин '{store_name}'\n"
            f"В нем работает {count} сотрудников"
        )
        conn.close()
        return
    
    # Удаляем магазин
    cursor.execute("DELETE FROM stores WHERE name = ?", (store_name,))
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Магазин '{store_name}' удален")
    
    # Возвращаемся в меню
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="admin_stores_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Функции для экспорта CSV
async def export_csv(query, store, confirmed_only=True):
    """Экспорт данных в CSV"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Формируем запрос
    if store == "all":
        if confirmed_only:
            cursor.execute('''
                SELECT e.full_name, e.position, e.store, t.date, t.check_in, t.check_out, 
                       t.hours, t.notes, t.confirmed
                FROM timesheet t
                JOIN employees e ON t.user_id = e.user_id
                WHERE t.status = 'completed' AND t.confirmed = 1
                ORDER BY t.date DESC, e.store
            ''')
        else:
            cursor.execute('''
                SELECT e.full_name, e.position, e.store, t.date, t.check_in, t.check_out, 
                       t.hours, t.notes, t.confirmed
                FROM timesheet t
                JOIN employees e ON t.user_id = e.user_id
                WHERE t.status = 'completed'
                ORDER BY t.date DESC, e.store
            ''')
    else:
        if confirmed_only:
            cursor.execute('''
                SELECT e.full_name, e.position, e.store, t.date, t.check_in, t.check_out, 
                       t.hours, t.notes, t.confirmed
                FROM timesheet t
                JOIN employees e ON t.user_id = e.user_id
                WHERE e.store = ? AND t.status = 'completed' AND t.confirmed = 1
                ORDER BY t.date DESC
            ''', (store,))
        else:
            cursor.execute('''
                SELECT e.full_name, e.position, e.store, t.date, t.check_in, t.check_out, 
                       t.hours, t.notes, t.confirmed
                FROM timesheet t
                JOIN employees e ON t.user_id = e.user_id
                WHERE e.store = ? AND t.status = 'completed'
                ORDER BY t.date DESC
            ''', (store,))
    
    records = cursor.fetchall()
    conn.close()
    
    if not records:
        await query.edit_message_text("📊 Нет данных для экспорта")
        return
    
    # Создаем CSV в памяти
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL)
    
    # Заголовки на русском
    writer.writerow([
        'Сотрудник', 'Должность', 'Магазин', 'Дата', 'Начало', 'Конец',
        'Часов', 'Примечания', 'Подтверждено'
    ])
    
    for record in records:
        full_name, position, store_name, date_str, checkin, checkout, hours, notes, confirmed = record
        
        # Форматирование времени
        checkin_time = format_time_utc8(datetime.fromisoformat(checkin)) if checkin else "-"
        checkout_time = format_time_utc8(datetime.fromisoformat(checkout)) if checkout else "-"
        
        # Конвертация в русские статусы
        confirmed_str = "Да" if confirmed else "Нет"
        
        # Замена точки на запятую в часах
        hours_str = str(hours).replace('.', ',')
        
        writer.writerow([
            full_name, position, store_name, date_str, checkin_time, checkout_time,
            hours_str, notes or "", confirmed_str
        ])
    
    # Получаем данные для отправки
    csv_data = output.getvalue().encode('utf-8-sig')
    output.close()
    
    # Формируем имя файла
    today = get_today_date_utc8()
    store_part = "all" if store == "all" else store
    confirmed_part = "confirmed" if confirmed_only else "all"
    filename = f"timesheet_{store_part}_{confirmed_part}_{today}.csv"
    
    # Отправляем файл
    await query.message.reply_document(
        document=io.BytesIO(csv_data),
        filename=filename,
        caption=f"📊 Экспорт данных{' (только подтвержденные)' if confirmed_only else ' (все смены)'}"
    )
    
    await query.edit_message_text("✅ Экспорт завершен!")
    
    # Возвращаемся в меню
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def export_csv_period(query, days, confirmed_only=True):
    """Экспорт данных за период"""
    end_date = get_today_date_utc8()
    start_date = (datetime.now(TIMEZONE) - timedelta(days=days-1)).date().isoformat()
    
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    if confirmed_only:
        cursor.execute('''
            SELECT e.full_name, e.position, e.store, t.date, t.check_in, t.check_out, 
                   t.hours, t.notes, t.confirmed
            FROM timesheet t
            JOIN employees e ON t.user_id = e.user_id
            WHERE t.date BETWEEN ? AND ? AND t.status = 'completed' AND t.confirmed = 1
            ORDER BY t.date DESC, e.store
        ''', (start_date, end_date))
    else:
        cursor.execute('''
            SELECT e.full_name, e.position, e.store, t.date, t.check_in, t.check_out, 
                   t.hours, t.notes, t.confirmed
            FROM timesheet t
            JOIN employees e ON t.user_id = e.user_id
            WHERE t.date BETWEEN ? AND ? AND t.status = 'completed'
            ORDER BY t.date DESC, e.store
        ''', (start_date, end_date))
    
    records = cursor.fetchall()
    conn.close()
    
    if not records:
        period_text = f"с {start_date} по {end_date}"
        await query.edit_message_text(f"📊 Нет данных за период {period_text}")
        return
    
    # Создаем CSV
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL)
    
    writer.writerow([
        'Сотрудник', 'Должность', 'Магазин', 'Дата', 'Начало', 'Конец',
        'Часов', 'Примечания', 'Подтверждено'
    ])
    
    for record in records:
        full_name, position, store_name, date_str, checkin, checkout, hours, notes, confirmed = record
        
        checkin_time = format_time_utc8(datetime.fromisoformat(checkin)) if checkin else "-"
        checkout_time = format_time_utc8(datetime.fromisoformat(checkout)) if checkout else "-"
        confirmed_str = "Да" if confirmed else "Нет"
        hours_str = str(hours).replace('.', ',')
        
        writer.writerow([
            full_name, position, store_name, date_str, checkin_time, checkout_time,
            hours_str, notes or "", confirmed_str
        ])
    
    csv_data = output.getvalue().encode('utf-8-sig')
    output.close()
    
    # Имя файла
    confirmed_part = "confirmed" if confirmed_only else "all"
    filename = f"timesheet_period_{start_date}_to_{end_date}_{confirmed_part}.csv"
    
    await query.message.reply_document(
        document=io.BytesIO(csv_data),
        filename=filename,
        caption=f"📊 Экспорт за период {start_date} - {end_date}"
    )
    
    await query.edit_message_text("✅ Экспорт завершен!")
    
    # Возвращаемся в меню
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Функции для подтверждения смен
async def show_unconfirmed_today(query):
    """Показать неподтвержденные смены за сегодня"""
    today = get_today_date_utc8()
    
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT t.id, e.full_name, e.store, t.check_in, t.check_out, t.hours
        FROM timesheet t
        JOIN employees e ON t.user_id = e.user_id
        WHERE t.date = ? AND t.status = 'completed' AND t.confirmed = 0
        ORDER BY e.store, e.full_name
    ''', (today,))
    
    unconfirmed = cursor.fetchall()
    conn.close()
    
    if not unconfirmed:
        await query.edit_message_text("✅ Сегодня нет неподтвержденных смен")
        return
    
    text = f"📋 НЕПОДТВЕРЖДЕННЫЕ СМЕНЫ ЗА {today}\n\n"
    
    for shift in unconfirmed:
        shift_id, full_name, store, checkin, checkout, hours = shift
        
        checkin_time = format_time_utc8(datetime.fromisoformat(checkin)) if checkin else "-"
        checkout_time = format_time_utc8(datetime.fromisoformat(checkout)) if checkout else "-"
        
        text += f"🆔 {shift_id}\n"
        text += f"👤 {full_name}\n"
        text += f"🏪 {store}\n"
        text += f"⏱ {checkin_time} - {checkout_time} ({hours} ч)\n\n"
    
    # Создаем клавиатуру
    keyboard = []
    for shift in unconfirmed:
        shift_id = shift[0]
        keyboard.append([
            InlineKeyboardButton(f"✅ Подтвердить смену #{shift_id}", 
                               callback_data=f"confirm_shift_{shift_id}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Разбиваем длинное сообщение
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
        await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)

async def show_period_confirm_menu(query):
    """Меню выбора периода для подтверждения"""
    keyboard = [
        [InlineKeyboardButton("📅 3 дня", callback_data="confirm_period_3")],
        [InlineKeyboardButton("📅 7 дней", callback_data="confirm_period_7")],
        [InlineKeyboardButton("📅 14 дней", callback_data="confirm_period_14")],
        [InlineKeyboardButton("📅 30 дней", callback_data="confirm_period_30")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "📅 ВЫБОР ПЕРИОДА\n\n"
        "Выберите период для просмотра неподтвержденных смен:",
        reply_markup=reply_markup
    )

async def show_unconfirmed_period(query, days):
    """Показать неподтвержденные смены за период"""
    end_date = get_today_date_utc8()
    start_date = (datetime.now(TIMEZONE) - timedelta(days=days-1)).date().isoformat()
    
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT t.id, e.full_name, e.store, t.date, t.check_in, t.check_out, t.hours
        FROM timesheet t
        JOIN employees e ON t.user_id = e.user_id
        WHERE t.date BETWEEN ? AND ? AND t.status = 'completed' AND t.confirmed = 0
        ORDER BY t.date DESC, e.store
    ''', (start_date, end_date))
    
    unconfirmed = cursor.fetchall()
    conn.close()
    
    if not unconfirmed:
        await query.edit_message_text(f"✅ Нет неподтвержденных смен за последние {days} дней")
        return
    
    text = f"📋 НЕПОДТВЕРЖДЕННЫЕ СМЕНЫ ЗА {days} ДНЕЙ\n\n"
    
    # Группируем по датам
    by_date = {}
    for shift in unconfirmed:
        date = shift[3]
        if date not in by_date:
            by_date[date] = []
        by_date[date].append(shift)
    
    for date in sorted(by_date.keys(), reverse=True):
        text += f"📅 {date}\n"
        for shift in by_date[date]:
            shift_id, full_name, store, _, checkin, checkout, hours = shift
            
            checkin_time = format_time_utc8(datetime.fromisoformat(checkin)) if checkin else "-"
            checkout_time = format_time_utc8(datetime.fromisoformat(checkout)) if checkout else "-"
            
            text += f"  🆔 {shift_id} | {full_name} | {store}\n"
            text += f"  ⏱ {checkin_time} - {checkout_time} ({hours} ч)\n\n"
    
    # Создаем клавиатуру (только первые 20 для избежания переполнения)
    keyboard = []
    for shift in unconfirmed[:20]:
        shift_id = shift[0]
        keyboard.append([
            InlineKeyboardButton(f"✅ Подтвердить #{shift_id}", 
                               callback_data=f"confirm_shift_{shift_id}")
        ])
    
    if len(unconfirmed) > 20:
        keyboard.append([InlineKeyboardButton("✅ Подтвердить все (первые 20)", 
                                            callback_data="confirm_all_today")])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Разбиваем длинное сообщение
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
        await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)

async def confirm_all_today(query):
    """Подтвердить все смены за сегодня"""
    today = get_today_date_utc8()
    
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE timesheet 
        SET confirmed = 1 
        WHERE date = ? AND status = 'completed' AND confirmed = 0
    ''', (today,))
    count = cursor.rowcount
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Подтверждено {count} смен за {today}")
    
    # Возвращаемся в меню
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def show_confirm_by_store(query):
    """Меню подтверждения по магазинам"""
    stores = get_stores()
    
    if not stores:
        await query.edit_message_text("❌ Нет созданных магазинов")
        return
    
    keyboard = []
    for store_name, address in stores:
        # Считаем неподтвержденные смены в магазине
        conn = sqlite3.connect('timesheet.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) 
            FROM timesheet t
            JOIN employees e ON t.user_id = e.user_id
            WHERE e.store = ? AND t.status = 'completed' AND t.confirmed = 0
        ''', (store_name,))
        count = cursor.fetchone()[0]
        conn.close()
        
        keyboard.append([
            InlineKeyboardButton(f"{store_name} ({count} неподтв.)", 
                               callback_data=f"confirm_store_{store_name}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🏪 ВЫБОР МАГАЗИНА\n\n"
        "Выберите магазин:",
        reply_markup=reply_markup
    )

async def show_store_unconfirmed(query, store):
    """Показать неподтвержденные смены в магазине"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT t.id, e.full_name, t.date, t.check_in, t.check_out, t.hours
        FROM timesheet t
        JOIN employees e ON t.user_id = e.user_id
        WHERE e.store = ? AND t.status = 'completed' AND t.confirmed = 0
        ORDER BY t.date DESC
    ''', (store,))
    
    unconfirmed = cursor.fetchall()
    conn.close()
    
    if not unconfirmed:
        await query.edit_message_text(f"✅ В магазине '{store}' нет неподтвержденных смен")
        return
    
    text = f"📋 НЕПОДТВЕРЖДЕННЫЕ СМЕНЫ В МАГАЗИНЕ {store}\n\n"
    
    for shift in unconfirmed:
        shift_id, full_name, date, checkin, checkout, hours = shift
        
        checkin_time = format_time_utc8(datetime.fromisoformat(checkin)) if checkin else "-"
        checkout_time = format_time_utc8(datetime.fromisoformat(checkout)) if checkout else "-"
        
        text += f"🆔 {shift_id} | {full_name}\n"
        text += f"📅 {date}\n"
        text += f"⏱ {checkin_time} - {checkout_time} ({hours} ч)\n\n"
    
    # Клавиатура
    keyboard = [
        [InlineKeyboardButton(f"✅ Подтвердить все в {store}", 
                            callback_data=f"confirm_all_store_{store}")]
    ]
    
    # Добавляем кнопки для отдельных смен (первые 10)
    for shift in unconfirmed[:10]:
        shift_id = shift[0]
        keyboard.append([
            InlineKeyboardButton(f"✅ Подтвердить #{shift_id}", 
                               callback_data=f"confirm_shift_{shift_id}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="confirm_by_store")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Разбиваем длинное сообщение
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
        await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)

async def confirm_all_store(query, store):
    """Подтвердить все смены в магазине"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Обновляем смены
    cursor.execute('''
        UPDATE timesheet 
        SET confirmed = 1 
        WHERE id IN (
            SELECT t.id 
            FROM timesheet t
            JOIN employees e ON t.user_id = e.user_id
            WHERE e.store = ? AND t.status = 'completed' AND t.confirmed = 0
        )
    ''', (store,))
    
    count = cursor.rowcount
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Подтверждено {count} смен в магазине '{store}'")
    
    # Возвращаемся в меню
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def confirm_shift(query, shift_id):
    """Подтвердить конкретную смену"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE timesheet SET confirmed = 1 WHERE id = ?", (shift_id,))
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Смена #{shift_id} подтверждена")
    
    # Возвращаемся в меню
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def show_confirm_stats(query):
    """Показать статистику подтверждений"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Общая статистика
    cursor.execute('''
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN confirmed = 1 THEN 1 ELSE 0 END) as confirmed,
            SUM(CASE WHEN confirmed = 0 AND status = 'completed' THEN 1 ELSE 0 END) as unconfirmed
        FROM timesheet
        WHERE status = 'completed'
    ''')
    
    total, confirmed, unconfirmed = cursor.fetchone()
    total = total or 0
    confirmed = confirmed or 0
    unconfirmed = unconfirmed or 0
    
    # Статистика по магазинам
    cursor.execute('''
        SELECT 
            e.store,
            COUNT(*) as total,
            SUM(CASE WHEN t.confirmed = 1 THEN 1 ELSE 0 END) as confirmed
        FROM timesheet t
        JOIN employees e ON t.user_id = e.user_id
        WHERE t.status = 'completed'
        GROUP BY e.store
        ORDER BY e.store
    ''')
    
    store_stats = cursor.fetchall()
    conn.close()
    
    text = "📊 СТАТИСТИКА ПОДТВЕРЖДЕНИЙ\n\n"
    text += f"Всего завершенных смен: {total}\n"
    text += f"✅ Подтверждено: {confirmed}\n"
    text += f"❌ Не подтверждено: {unconfirmed}\n"
    
    if total > 0:
        percent = (confirmed / total) * 100
        text += f"📈 Процент подтверждения: {percent:.1f}%\n\n"
    
    text += "По магазинам:\n"
    for store, store_total, store_confirmed in store_stats:
        store_confirmed = store_confirmed or 0
        text += f"🏪 {store}: {store_confirmed}/{store_total} "
        if store_total > 0:
            store_percent = (store_confirmed / store_total) * 100
            text += f"({store_percent:.1f}%)\n"
        else:
            text += "(0%)\n"
    
    await query.edit_message_text(text)
    
    # Добавляем кнопку "Назад"
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Функции для запросов на удаление
async def show_delete_employee_menu(query):
    """Меню выбора сотрудника для удаления"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Получаем всех сотрудников, кроме текущего и супер-админов
    cursor.execute('''
        SELECT user_id, full_name, position, store 
        FROM employees 
        WHERE is_super_admin = 0
        ORDER BY store, full_name
    ''')
    
    employees = cursor.fetchall()
    conn.close()
    
    if not employees:
        await query.edit_message_text("👥 Нет сотрудников для удаления")
        return
    
    text = "👤 ВЫБОР СОТРУДНИКА ДЛЯ УДАЛЕНИЯ\n\n"
    
    # Группируем по магазинам
    by_store = {}
    for emp in employees:
        user_id, full_name, position, store = emp
        if store not in by_store:
            by_store[store] = []
        by_store[store].append((user_id, full_name, position))
    
    for store, emps in by_store.items():
        text += f"🏪 {store}\n"
        for user_id, full_name, position in emps:
            text += f"  👤 {full_name} - {position}\n"
        text += "\n"
    
    # Создаем клавиатуру
    keyboard = []
    for user_id, full_name, position, store in employees:
        keyboard.append([
            InlineKeyboardButton(f"🗑 {full_name} ({store})", 
                               callback_data=f"request_delete_employee_{user_id}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_delete_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Разбиваем длинное сообщение
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
        await query.message.reply_text("Выберите сотрудника:", reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)

async def show_delete_store_request_menu(query):
    """Меню выбора магазина для удаления"""
    stores = get_stores()
    
    if not stores:
        await query.edit_message_text("🏪 Нет магазинов для удаления")
        return
    
    text = "🏪 ВЫБОР МАГАЗИНА ДЛЯ УДАЛЕНИЯ\n\n"
    for name, address in stores:
        text += f"• {name}\n  📍 {address}\n\n"
    
    keyboard = []
    for name, address in stores:
        keyboard.append([
            InlineKeyboardButton(f"🗑 {name}", callback_data=f"request_delete_store_{name}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_delete_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup)

async def create_delete_request(query, requester_id, requester_name, target_type, target_id):
    """Создание запроса на удаление"""
    # Проверяем, нет ли уже активного запроса
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id FROM delete_requests 
        WHERE target_type = ? AND target_id = ? AND status = 'pending'
    ''', (target_type, target_id))
    
    existing = cursor.fetchone()
    
    if existing:
        await query.edit_message_text(
            f"❌ Запрос на удаление этого {target_type} уже существует"
        )
        conn.close()
        return
    
    # Получаем имя цели
    if target_type == "employee":
        cursor.execute("SELECT full_name FROM employees WHERE user_id = ?", (target_id,))
        target_name = cursor.fetchone()
        if not target_name:
            await query.edit_message_text("❌ Сотрудник не найден")
            conn.close()
            return
        target_name = target_name[0]
    else:  # store
        target_name = target_id
    
    # Создаем запрос
    cursor.execute('''
        INSERT INTO delete_requests 
        (request_date, requester_id, requester_name, target_type, target_id, target_name, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (get_today_date_utc8(), requester_id, requester_name, 
          target_type, target_id, target_name, 'pending'))
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(
        f"✅ Запрос на удаление {target_type} '{target_name}' отправлен супер-администратору"
    )
    
    # Уведомляем супер-админов
    super_admins = get_super_admins()
    for admin_id, admin_name in super_admins:
        try:
            await query.message.bot.send_message(
                admin_id,
                f"🔔 Новый запрос на удаление!\n\n"
                f"От: {requester_name}\n"
                f"Тип: {target_type}\n"
                f"Цель: {target_name}\n\n"
                f"Используйте /admin для рассмотрения запроса."
            )
        except Exception as e:
            logger.error(f"Failed to notify super admin {admin_id}: {e}")

async def show_delete_requests(query):
    """Показать все запросы на удаление"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, request_date, requester_name, target_type, target_name, status
        FROM delete_requests
        ORDER BY 
            CASE status 
                WHEN 'pending' THEN 1
                WHEN 'approved' THEN 2
                ELSE 3
            END,
            request_date DESC
    ''')
    
    requests = cursor.fetchall()
    conn.close()
    
    if not requests:
        await query.edit_message_text("📋 Нет запросов на удаление")
        return
    
    text = "📋 ЗАПРОСЫ НА УДАЛЕНИЕ\n\n"
    
    pending_keyboard = []
    other_text = ""
    
    for req in requests:
        req_id, req_date, requester, target_type, target_name, status = req
        
        status_emoji = {
            'pending': '⏳',
            'approved': '✅',
            'rejected': '❌'
        }.get(status, '❓')
        
        status_text = {
            'pending': 'Ожидает',
            'approved': 'Одобрен',
            'rejected': 'Отклонен'
        }.get(status, 'Неизвестно')
        
        type_text = "сотрудника" if target_type == "employee" else "магазин"
        
        req_text = f"{status_emoji} Запрос #{req_id}\n"
        req_text += f"📅 {req_date}\n"
        req_text += f"👤 От: {requester}\n"
        req_text += f"🎯 Тип: {type_text}\n"
        req_text += f"📌 Цель: {target_name}\n"
        req_text += f"📊 Статус: {status_text}\n\n"
        
        if status == 'pending':
            # Добавляем в клавиатуру только ожидающие
            pending_keyboard.append([
                InlineKeyboardButton(f"✅ Одобрить #{req_id}", callback_data=f"approve_request_{req_id}"),
                InlineKeyboardButton(f"❌ Отклонить #{req_id}", callback_data=f"reject_request_{req_id}")
            ])
            text += req_text
        else:
            other_text += req_text
    
    if other_text:
        text += "📋 ЗАВЕРШЕННЫЕ ЗАПРОСЫ:\n\n" + other_text
    
    # Добавляем кнопки для ожидающих запросов
    keyboard = pending_keyboard
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Разбиваем длинное сообщение
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
        await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)

async def approve_delete_request(query, request_id):
    """Одобрить запрос на удаление"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Получаем информацию о запросе
    cursor.execute('''
        SELECT target_type, target_id, target_name, requester_id, requester_name
        FROM delete_requests
        WHERE id = ? AND status = 'pending'
    ''', (request_id,))
    
    request = cursor.fetchone()
    
    if not request:
        await query.edit_message_text(f"❌ Запрос #{request_id} не найден или уже обработан")
        conn.close()
        return
    
    target_type, target_id, target_name, requester_id, requester_name = request
    
    # Проверяем, можно ли удалить
    if target_type == "employee":
        # Нельзя удалить супер-админа
        cursor.execute("SELECT is_super_admin FROM employees WHERE user_id = ?", (target_id,))
        is_super_admin = cursor.fetchone()
        if is_super_admin and is_super_admin[0] == 1:
            await query.edit_message_text("❌ Нельзя удалить супер-администратора")
            conn.close()
            return
        
        # Удаляем сотрудника и его смены
        cursor.execute("DELETE FROM timesheet WHERE user_id = ?", (target_id,))
        cursor.execute("DELETE FROM employees WHERE user_id = ?", (target_id,))
        
    else:  # store
        # Проверяем, есть ли сотрудники в магазине
        cursor.execute("SELECT COUNT(*) FROM employees WHERE store = ?", (target_name,))
        emp_count = cursor.fetchone()[0]
        
        if emp_count > 0:
            await query.edit_message_text(
                f"❌ Нельзя удалить магазин '{target_name}'\n"
                f"В нем работает {emp_count} сотрудников"
            )
            conn.close()
            return
        
        # Удаляем магазин
        cursor.execute("DELETE FROM stores WHERE name = ?", (target_name,))
    
    # Обновляем статус запроса
    cursor.execute('''
        UPDATE delete_requests 
        SET status = 'approved' 
        WHERE id = ?
    ''', (request_id,))
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Запрос #{request_id} одобрен, удаление выполнено")
    
    # Уведомляем запросившего
    try:
        await query.message.bot.send_message(
            requester_id,
            f"✅ Ваш запрос на удаление {target_type} '{target_name}' одобрен и выполнен!"
        )
    except Exception as e:
        logger.error(f"Failed to notify requester {requester_id}: {e}")
    
    # Возвращаемся к списку запросов
    await show_delete_requests(query)

async def reject_delete_request(query, request_id):
    """Отклонить запрос на удаление"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Получаем информацию о запросе
    cursor.execute('''
        SELECT target_type, target_name, requester_id
        FROM delete_requests
        WHERE id = ? AND status = 'pending'
    ''', (request_id,))
    
    request = cursor.fetchone()
    
    if not request:
        await query.edit_message_text(f"❌ Запрос #{request_id} не найден или уже обработан")
        conn.close()
        return
    
    target_type, target_name, requester_id = request
    
    # Обновляем статус
    cursor.execute('''
        UPDATE delete_requests 
        SET status = 'rejected' 
        WHERE id = ?
    ''', (request_id,))
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"❌ Запрос #{request_id} отклонен")
    
    # Уведомляем запросившего
    try:
        await query.message.bot.send_message(
            requester_id,
            f"❌ Ваш запрос на удаление {target_type} '{target_name}' отклонен супер-администратором"
        )
    except Exception as e:
        logger.error(f"Failed to notify requester {requester_id}: {e}")
    
    # Возвращаемся к списку запросов
    await show_delete_requests(query)

# Функции для заявок на админа
async def handle_admin_request(query, context, user_id, user_info):
    """Обработка заявки на становление администратором"""
    full_name = user_info[0] if user_info else query.from_user.full_name
    
    # Проверяем, нет ли уже активной заявки
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id FROM admin_requests 
        WHERE user_id = ? AND status = 'pending'
    ''', (user_id,))
    
    existing = cursor.fetchone()
    
    if existing:
        await query.edit_message_text(
            "❌ У вас уже есть активная заявка на становление администратором"
        )
        conn.close()
        return
    
    # Создаем заявку
    cursor.execute('''
        INSERT INTO admin_requests 
        (request_date, user_id, user_name, status)
        VALUES (?, ?, ?, ?)
    ''', (get_today_date_utc8(), user_id, full_name, 'pending'))
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(
        "✅ Заявка на становление администратором отправлена!\n"
        "Ожидайте решения супер-администратора."
    )
    
    # Уведомляем супер-админов
    super_admins = get_super_admins()
    for admin_id, admin_name in super_admins:
        try:
            await query.message.bot.send_message(
                admin_id,
                f"👑 Новая заявка на становление администратором!\n\n"
                f"От: {full_name}\n"
                f"ID: {user_id}\n\n"
                f"Используйте /admin для рассмотрения заявки."
            )
        except Exception as e:
            logger.error(f"Failed to notify super admin {admin_id}: {e}")

async def show_admin_requests(query):
    """Показать все заявки на админа"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, request_date, user_name, user_id, status
        FROM admin_requests
        ORDER BY 
            CASE status 
                WHEN 'pending' THEN 1
                WHEN 'approved' THEN 2
                ELSE 3
            END,
            request_date DESC
    ''')
    
    requests = cursor.fetchall()
    conn.close()
    
    if not requests:
        await query.edit_message_text("📋 Нет заявок на становление администратором")
        return
    
    text = "👑 ЗАЯВКИ НА СТАНОВЛЕНИЕ АДМИНИСТРАТОРОМ\n\n"
    
    pending_keyboard = []
    other_text = ""
    
    for req in requests:
        req_id, req_date, user_name, user_id, status = req
        
        status_emoji = {
            'pending': '⏳',
            'approved': '✅',
            'rejected': '❌'
        }.get(status, '❓')
        
        status_text = {
            'pending': 'Ожидает',
            'approved': 'Одобрена',
            'rejected': 'Отклонена'
        }.get(status, 'Неизвестно')
        
        req_text = f"{status_emoji} Заявка #{req_id}\n"
        req_text += f"📅 {req_date}\n"
        req_text += f"👤 {user_name} (ID: {user_id})\n"
        req_text += f"📊 Статус: {status_text}\n\n"
        
        if status == 'pending':
            pending_keyboard.append([
                InlineKeyboardButton(f"✅ Одобрить #{req_id}", callback_data=f"approve_admin_{req_id}"),
                InlineKeyboardButton(f"❌ Отклонить #{req_id}", callback_data=f"reject_admin_{req_id}")
            ])
            text += req_text
        else:
            other_text += req_text
    
    if other_text:
        text += "📋 ЗАВЕРШЕННЫЕ ЗАЯВКИ:\n\n" + other_text
    
    # Добавляем кнопки для ожидающих заявок
    keyboard = pending_keyboard
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Разбиваем длинное сообщение
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
        await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)

async def approve_admin_request(query, request_id):
    """Одобрить заявку на админа"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Получаем информацию о заявке
    cursor.execute('''
        SELECT user_id, user_name
        FROM admin_requests
        WHERE id = ? AND status = 'pending'
    ''', (request_id,))
    
    request = cursor.fetchone()
    
    if not request:
        await query.edit_message_text(f"❌ Заявка #{request_id} не найдена или уже обработана")
        conn.close()
        return
    
    user_id, user_name = request
    
    # Проверяем, зарегистрирован ли пользователь
    cursor.execute("SELECT full_name FROM employees WHERE user_id = ?", (user_id,))
    employee = cursor.fetchone()
    
    if employee:
        # Пользователь уже зарегистрирован - делаем его админом
        cursor.execute('''
            UPDATE employees 
            SET is_admin = 1 
            WHERE user_id = ?
        ''', (user_id,))
    else:
        # Новый пользователь - создаем временную запись
        cursor.execute('''
            INSERT INTO employees 
            (user_id, full_name, position, store, reg_date, is_admin, is_super_admin)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, user_name, "Администратор", "Главный офис", 
              get_today_date_utc8(), 1, 0))
    
    # Обновляем статус заявки
    cursor.execute('''
        UPDATE admin_requests 
        SET status = 'approved' 
        WHERE id = ?
    ''', (request_id,))
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Заявка #{request_id} одобрена, пользователь стал администратором")
    
    # Уведомляем пользователя
    try:
        await query.message.bot.send_message(
            user_id,
            f"✅ Поздравляем! Ваша заявка на становление администратором одобрена!\n\n"
            f"Теперь вам доступна панель администратора (/admin).\n"
            f"Рекомендуем создать должности и магазины для начала работы."
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {e}")
    
    # Возвращаемся к списку заявок
    await show_admin_requests(query)

async def reject_admin_request(query, request_id):
    """Отклонить заявку на админа"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Получаем информацию о заявке
    cursor.execute('''
        SELECT user_id, user_name
        FROM admin_requests
        WHERE id = ? AND status = 'pending'
    ''', (request_id,))
    
    request = cursor.fetchone()
    
    if not request:
        await query.edit_message_text(f"❌ Заявка #{request_id} не найдена или уже обработана")
        conn.close()
        return
    
    user_id, user_name = request
    
    # Обновляем статус
    cursor.execute('''
        UPDATE admin_requests 
        SET status = 'rejected' 
        WHERE id = ?
    ''', (request_id,))
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"❌ Заявка #{request_id} отклонена")
    
    # Уведомляем пользователя
    try:
        await query.message.bot.send_message(
            user_id,
            f"❌ К сожалению, ваша заявка на становление администратором отклонена."
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {e}")
    
    # Возвращаемся к списку заявок
    await show_admin_requests(query)

# Функции для управления супер-админами
async def show_assign_super_admin_menu(query):
    """Меню управления супер-админами"""
    keyboard = [
        [InlineKeyboardButton("⭐ Назначить супер-администратора", 
                            callback_data="assign_super_admin_list")],
        [InlineKeyboardButton("📋 Список супер-админов", 
                            callback_data="list_super_admins")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "⭐ УПРАВЛЕНИЕ СУПЕР-АДМИНИСТРАТОРАМИ\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )

async def show_assign_super_admin_list(query):
    """Показать список администраторов для назначения супер-админом"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Получаем всех администраторов, которые не являются супер-админами
    cursor.execute('''
        SELECT user_id, full_name, position, store 
        FROM employees 
        WHERE is_admin = 1 AND is_super_admin = 0
        ORDER BY store, full_name
    ''')
    
    admins = cursor.fetchall()
    conn.close()
    
    if not admins:
        await query.edit_message_text(
            "👥 Нет администраторов для назначения супер-админом"
        )
        return
    
    text = "⭐ ВЫБОР АДМИНИСТРАТОРА ДЛЯ НАЗНАЧЕНИЯ СУПЕР-АДМИНОМ\n\n"
    
    keyboard = []
    for admin in admins:
        user_id, full_name, position, store = admin
        text += f"👑 {full_name}\n"
        text += f"   Должность: {position}\n"
        text += f"   Магазин: {store}\n\n"
        
        keyboard.append([
            InlineKeyboardButton(f"⭐ {full_name}", 
                               callback_data=f"select_super_admin_{user_id}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="assign_super_admin_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Разбиваем длинное сообщение
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
        await query.message.reply_text("Выберите администратора:", reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)

async def confirm_assign_super_admin(query, target_id):
    """Подтверждение назначения супер-админа"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Получаем информацию о кандидате
    cursor.execute('''
        SELECT full_name, position, store 
        FROM employees 
        WHERE user_id = ?
    ''', (target_id,))
    
    candidate = cursor.fetchone()
    conn.close()
    
    if not candidate:
        await query.edit_message_text("❌ Пользователь не найден")
        return
    
    full_name, position, store = candidate
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_assign_super_admin"),
            InlineKeyboardButton("❌ Отмена", callback_data="assign_super_admin_menu")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"⚠️ Вы уверены, что хотите назначить супер-администратором?\n\n"
        f"👤 {full_name}\n"
        f"📋 {position}\n"
        f"🏪 {store}\n\n"
        f"Этот пользователь получит все права, включая управление супер-админами!",
        reply_markup=reply_markup
    )

async def assign_super_admin(query, target_id):
    """Назначение супер-администратора"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Обновляем статус
    cursor.execute('''
        UPDATE employees 
        SET is_super_admin = 1 
        WHERE user_id = ?
    ''', (target_id,))
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Пользователь назначен супер-администратором!")
    
    # Уведомляем назначенного
    try:
        await query.message.bot.send_message(
            target_id,
            f"⭐ Поздравляем! Вы назначены супер-администратором!\n\n"
            f"Теперь вам доступны все функции управления ботом."
        )
    except Exception as e:
        logger.error(f"Failed to notify new super admin {target_id}: {e}")
    
    # Возвращаемся в меню
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="assign_super_admin_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def list_super_admins(query):
    """Показать список супер-админов"""
    super_admins = get_super_admins()
    
    if not super_admins:
        await query.edit_message_text("⭐ Нет супер-администраторов")
        return
    
    text = "⭐ СПИСОК СУПЕР-АДМИНИСТРАТОРОВ\n\n"
    
    for i, (user_id, full_name) in enumerate(super_admins, 1):
        text += f"{i}. {full_name} (ID: {user_id})\n"
    
    await query.edit_message_text(text)
    
    # Добавляем кнопку "Назад"
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="assign_super_admin_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def show_add_admin_menu(query):
    """Меню добавления администратора"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Получаем всех обычных сотрудников
    cursor.execute('''
        SELECT user_id, full_name, position, store 
        FROM employees 
        WHERE is_admin = 0 AND is_super_admin = 0
        ORDER BY store, full_name
    ''')
    
    employees = cursor.fetchall()
    conn.close()
    
    if not employees:
        await query.edit_message_text(
            "👥 Нет обычных сотрудников для назначения администраторами"
        )
        return
    
    text = "➕ ВЫБОР СОТРУДНИКА ДЛЯ НАЗНАЧЕНИЯ АДМИНИСТРАТОРОМ\n\n"
    
    keyboard = []
    for emp in employees:
        user_id, full_name, position, store = emp
        text += f"👤 {full_name}\n"
        text += f"   {position} | {store}\n\n"
        
        keyboard.append([
            InlineKeyboardButton(f"👑 {full_name}", 
                               callback_data=f"make_admin_{user_id}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Разбиваем длинное сообщение
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
        await query.message.reply_text("Выберите сотрудника:", reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)

# Обработчики текстовых сообщений для ConversationHandler
async def get_custom_period_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение начальной даты для пользовательского периода"""
    date_str = update.message.text.strip()
    
    try:
        # Проверяем формат даты
        start_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        context.user_data['period_start'] = date_str
        
        await update.message.reply_text(
            f"📅 Начальная дата: {date_str}\n\n"
            f"✏️ Теперь введите конечную дату в формате ГГГГ-ММ-ДД:"
        )
        return CUSTOM_PERIOD_END
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД\n"
            "Например: 2024-01-31"
        )
        return CUSTOM_PERIOD_START

async def get_custom_period_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение конечной даты для пользовательского периода"""
    end_date_str = update.message.text.strip()
    start_date_str = context.user_data.get('period_start')
    
    try:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        
        if end_date < start_date:
            await update.message.reply_text(
                "❌ Конечная дата не может быть раньше начальной"
            )
            return CUSTOM_PERIOD_END
        
        # Вычисляем количество дней
        days = (end_date - start_date).days + 1
        context.user_data['period_days'] = days
        
        # Показываем опции экспорта
        keyboard = [
            [InlineKeyboardButton("📥 CSV (только подтвержденные)", callback_data="export_confirmed")],
            [InlineKeyboardButton("📥 CSV (все смены)", callback_data="export_all")],
            [InlineKeyboardButton("◀️ Назад", callback_data="period_selection")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"📊 Период: с {start_date_str} по {end_date_str}\n\n"
            f"Выберите тип экспорта:",
            reply_markup=reply_markup
        )
        
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД\n"
            "Например: 2024-01-31"
        )
        return CUSTOM_PERIOD_END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущего действия"""
    await update.message.reply_text(
        "❌ Действие отменено"
    )
    return ConversationHandler.END

# ИСПРАВЛЕНИЕ 4: Основная функция запуска
async def main_async():
    """Основная асинхронная функция"""
    try:
        # Шаг 1: Принудительно удаляем webhook и сбрасываем очередь
        await delete_webhook()
        
        # Небольшая пауза
        await asyncio.sleep(1)
        
        # Шаг 2: Инициализируем базу данных
        init_database()
        
        # Шаг 3: Создаем приложение
        app = Application.builder().token(BOT_TOKEN).build()
        
        # Регистрируем обработчики команд
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("checkin", checkin))
        app.add_handler(CommandHandler("checkout", checkout))
        app.add_handler(CommandHandler("timesheet", timesheet))
        app.add_handler(CommandHandler("stats", stats))
        app.add_handler(CommandHandler("admin", admin_panel))
        app.add_handler(CommandHandler("cancel", cancel_registration))
        
        # ИСПРАВЛЕНИЕ: ConversationHandler для регистрации
        reg_conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler("start", start),
                CallbackQueryHandler(button_callback, pattern="^reg_pos_")
            ],
            states={
                SELECT_POSITION: [
                    CallbackQueryHandler(button_callback, pattern="^reg_pos_"),
                    CallbackQueryHandler(button_callback, pattern="^cancel_registration$"),
                    CommandHandler("cancel", cancel_registration)
                ],
                SELECT_STORE: [
                    CallbackQueryHandler(button_callback, pattern="^reg_store_"),
                    CallbackQueryHandler(button_callback, pattern="^cancel_registration$"),
                    CommandHandler("cancel", cancel_registration)
                ],
            },
            fallbacks=[
                CommandHandler("cancel", cancel_registration),
                CommandHandler("start", start)
            ],
            allow_reentry=True,
            per_message=False
        )
        app.add_handler(reg_conv_handler)
        
        # ConversationHandler для создания должности
        create_position_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(button_callback, pattern="^create_position$")],
            states={
                CREATE_POSITION_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_position)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True
        )
        app.add_handler(create_position_conv)
        
        # ConversationHandler для создания магазина
        create_store_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(button_callback, pattern="^create_store$")],
            states={
                CREATE_STORE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_store_name)],
                CREATE_STORE_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_store_address)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True
        )
        app.add_handler(create_store_conv)
        
        # ConversationHandler для пользовательского периода
        custom_period_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(button_callback, pattern="^period_custom$")],
            states={
                CUSTOM_PERIOD_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_period_start)],
                CUSTOM_PERIOD_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_period_end)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True
        )
        app.add_handler(custom_period_conv)
        
        # Основной обработчик callback-запросов
        app.add_handler(CallbackQueryHandler(button_callback))
        
        logger.info("🚀 Bot started successfully")
        
        # Запускаем polling
        await app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"❌ Fatal error in main_async: {e}", exc_info=True)
        raise

def main():
    """Точка входа"""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    except Exception as e:
        logger.error(f"💥 Fatal error: {e}", exc_info=True)

if __name__ == '__main__':
    main()
