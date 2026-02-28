import os
import logging
import sqlite3
import csv
import io
import asyncio
import sys
import re
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

# Функция для отладки
def debug_print(*args, **kwargs):
    """Функция для отладки"""
    print(*args, **kwargs, file=sys.stderr)
    logging.info(*args)

# КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: добавляем nest_asyncio для работы на хостинге
import nest_asyncio
nest_asyncio.apply()

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')

# Настройка часового пояса UTC+8
TIMEZONE = pytz.timezone('Asia/Singapore')

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

# Состояния для ConversationHandler - РАСШИРЕНО
(
    SELECT_POSITION, SELECT_STORE, ENTER_FULL_NAME, CREATE_POSITION_NAME,
    CREATE_STORE_NAME, CREATE_STORE_ADDRESS, CUSTOM_PERIOD_START,
    CUSTOM_PERIOD_END, DELETE_EMPLOYEE_REQUEST, DELETE_STORE_REQUEST,
    ASSIGN_SUPER_ADMIN_SELECT, ADMIN_ADD_SHIFT_SELECT_STORE,
    ADMIN_ADD_SHIFT_SELECT_EMPLOYEE, ADMIN_ADD_SHIFT_ENTER_DATE,
    ADMIN_ADD_SHIFT_ENTER_HOURS, ADMIN_DELETE_SHIFT_SELECT_STORE,
    ADMIN_DELETE_SHIFT_SELECT_EMPLOYEE, ADMIN_DELETE_SHIFT_SELECT_DATE,
    ADMIN_CONFIRM_PERIOD_START, ADMIN_CONFIRM_PERIOD_END
) = range(20)

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

# Инициализация базы данных - РАСШИРЕНО
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
            is_super_admin INTEGER DEFAULT 0,
            can_request_admin INTEGER DEFAULT 0
        )
    ''')
    
    # Таблица табеля - ДОБАВЛЕНЫ ПОЛЯ ДЛЯ ПОДТВЕРЖДЕНИЯ
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
            confirmed_by INTEGER,
            confirmed_date TEXT,
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
    
    # НОВАЯ ТАБЛИЦА: аудит действий администраторов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_date TEXT NOT NULL,
            admin_id INTEGER NOT NULL,
            admin_name TEXT NOT NULL,
            action_type TEXT NOT NULL,
            target_user_id INTEGER,
            target_user_name TEXT,
            details TEXT
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
        "SELECT full_name, position, store, is_admin, is_super_admin, can_request_admin FROM employees WHERE user_id = ?",
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

# НОВАЯ ФУНКЦИЯ: логирование действий администратора
def log_admin_action(admin_id: int, admin_name: str, action_type: str, target_user_id: int = None, target_user_name: str = None, details: str = None):
    """Логирование действий администратора"""
    try:
        conn = sqlite3.connect('timesheet.db')
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO admin_audit 
               (action_date, admin_id, admin_name, action_type, target_user_id, target_user_name, details)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (get_now_utc8().isoformat(), admin_id, admin_name, action_type, target_user_id, target_user_name, details)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка при логировании действия администратора: {e}")

# Функция для создания клавиатуры обычного пользователя
def get_user_keyboard(can_request_admin: bool = False) -> ReplyKeyboardMarkup:
    """Создать клавиатуру для обычного пользователя"""
    keyboard = [
        [KeyboardButton("✅ Открыть смену"), KeyboardButton("✅ Закрыть смену")],
        [KeyboardButton("📊 Мой табель"), KeyboardButton("📈 Моя статистика")],
        [KeyboardButton("🏠 Главное меню")]
    ]
    if can_request_admin:
        keyboard.append([KeyboardButton("👑 Запросить права администратора")])
    
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Функция для создания клавиатуры администратора
def get_admin_keyboard(is_super_admin: bool = False) -> ReplyKeyboardMarkup:
    """Создать клавиатуру для администратора"""
    keyboard = [
        [KeyboardButton("✅ Открыть смену"), KeyboardButton("✅ Закрыть смену")],
        [KeyboardButton("📊 Мой табель"), KeyboardButton("📈 Моя статистика")],
        [KeyboardButton("🏠 Главное меню"), KeyboardButton("👑 Панель админа")]
    ]
    
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Функция для отмены регистрации
async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена регистрации"""
    await update.message.reply_text(
        "❌ Регистрация отменена. Если захотите зарегистрироваться, используйте /start"
    )
    return ConversationHandler.END

# ИСПРАВЛЕННАЯ ФУНКЦИЯ: проверка ФИО на три слова
async def enter_full_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение ФИО от пользователя с проверкой на три слова"""
    user_id = update.effective_user.id
    full_name = update.message.text.strip()

    logger.info(f"🔥 Регистрация: пользователь {user_id} ввел имя '{full_name}'")

    # Проверка: ФИО должно состоять из трех слов
    name_parts = full_name.split()
    if len(name_parts) != 3:
        await update.message.reply_text(
            "❌ Пожалуйста, введите полное ФИО (фамилия, имя, отчество).\n"
            "Пример: Иванов Иван Иванович\n\n"
            "Попробуйте снова:"
        )
        return ENTER_FULL_NAME

    # Проверка длины каждой части
    if any(len(part) < 2 for part in name_parts):
        await update.message.reply_text(
            "❌ Каждая часть ФИО должна содержать минимум 2 символа.\n"
            "Пример: Иванов Иван Иванович\n\n"
            "Попробуйте снова:"
        )
        return ENTER_FULL_NAME

    # Проверка на допустимые символы (только буквы, пробелы, дефисы)
    if not re.match(r'^[а-яА-ЯёЁa-zA-Z\s-]+$', full_name):
        await update.message.reply_text(
            "❌ ФИО может содержать только буквы, пробелы и дефисы.\n"
            "Попробуйте снова:"
        )
        return ENTER_FULL_NAME

    # Сохраняем имя
    context.user_data['full_name'] = full_name
    logger.info(f"✅ Имя сохранено: {full_name}")

    # Показываем должности
    positions = get_positions()
    logger.info(f"Список должностей: {positions}")

    if not positions:
        await update.message.reply_text(
            "❌ В системе нет должностей. Обратитесь к администратору."
        )
        return ConversationHandler.END

    # Создаем клавиатуру с должностями
    keyboard = []
    for pos in positions:
        keyboard.append([InlineKeyboardButton(pos, callback_data=f"reg_pos_{pos}")])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_registration")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"👤 Ваше имя: {full_name}\n\n"
        f"📝 Теперь выберите вашу должность:",
        reply_markup=reply_markup
    )

    return SELECT_POSITION

# Функции для удаления webhook
async def delete_webhook():
    """Удаление webhook перед запуском polling"""
    try:
        async with Application.builder().token(BOT_TOKEN).build() as app:
            result = await app.bot.delete_webhook(drop_pending_updates=True)
            if result:
                logger.info("✅ Webhook успешно удален, ожидающие обновления сброшены.")
            else:
                logger.warning("⚠️ Не удалось удалить webhook (возможно, его и не было).")
    except Exception as e:
        logger.error(f"❌ Ошибка при удалении webhook: {e}")

# Обновленная функция start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start - регистрация или главное меню"""
    user = update.effective_user
    user_id = user.id
    full_name = user.full_name

    logger.info(f"🔥 Команда /start от пользователя {user_id} ({full_name})")

    # Проверяем, зарегистрирован ли пользователь
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT is_admin, is_super_admin, can_request_admin FROM employees WHERE user_id = ?",
        (user_id,)
    )
    employee = cursor.fetchone()

    if employee:
        # Пользователь уже зарегистрирован
        is_admin, is_super_admin, can_request_admin = employee
        stored_name = get_user(user_id)[0]
        conn.close()

        logger.info(f"Пользователь уже зарегистрирован: is_admin={is_admin}, is_super_admin={is_super_admin}")

        if is_super_admin:
            keyboard = get_admin_keyboard(is_super_admin=True)
            await update.message.reply_text(
                f"👋 С возвращением, {stored_name}!\n"
                f"Ваш статус: ⭐ Супер-администратор\n"
                f"Используйте кнопку 👑 Панель админа для управления",
                reply_markup=keyboard
            )
        elif is_admin:
            keyboard = get_admin_keyboard(is_super_admin=False)
            await update.message.reply_text(
                f"👋 С возвращением, {stored_name}!\n"
                f"Ваш статус: 👑 Администратор\n"
                f"Используйте кнопку 👑 Панель админа для управления",
                reply_markup=keyboard
            )
        else:
            keyboard = get_user_keyboard(can_request_admin)
            await update.message.reply_text(
                f"👋 Привет, {stored_name}!\n\n"
                f"📋 Используйте кнопки ниже для работы:",
                reply_markup=keyboard
            )
        return ConversationHandler.END

    logger.info("Пользователь не зарегистрирован, проверяем наличие супер-админов")

    # Проверяем, есть ли в системе супер-администраторы
    cursor.execute("SELECT COUNT(*) FROM employees WHERE is_super_admin = 1")
    super_admin_count = cursor.fetchone()[0]

    if super_admin_count == 0:
        # Первый пользователь становится супер-администратором
        logger.info("Первый пользователь - назначаем супер-админом")
        cursor.execute('''
            INSERT INTO employees (user_id, full_name, position, store, reg_date, is_admin, is_super_admin, can_request_admin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, full_name, "Администратор", "Главный офис", 
              get_today_date_utc8(), 1, 1, 0))
        conn.commit()
        conn.close()

        keyboard = get_admin_keyboard(is_super_admin=True)
        await update.message.reply_text(
            "🎉 Вы зарегистрированы как первый супер-администратор!\n\n"
            "⚠️ Важно: Сейчас в системе нет должностей и магазинов.\n"
            "1️⃣ Используйте кнопку 👑 Панель админа для входа в панель администратора\n"
            "2️⃣ Создайте должности и магазины через панель администратора\n\n"
            "Только после этого другие сотрудники смогут регистрироваться.",
            reply_markup=keyboard
        )
        return ConversationHandler.END
    else:
        # Проверяем наличие должностей и магазинов
        cursor.execute("SELECT COUNT(*) FROM positions")
        positions_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM stores")
        stores_count = cursor.fetchone()[0]
        conn.close()

        logger.info(f"Должностей: {positions_count}, Магазинов: {stores_count}")

        if positions_count == 0 or stores_count == 0:
            # Нет должностей или магазинов - предлагаем стать администратором
            logger.info("Нет должностей или магазинов - предлагаем стать админом")
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
            # Запрашиваем имя перед регистрацией с правильным форматом
            logger.info("Запрашиваем имя пользователя")
            await update.message.reply_text(
                "📝 Для регистрации введите ваше полное ФИО (фамилия, имя, отчество):\n"
                "Пример: Иванов Иван Иванович"
            )
            logger.info(f"🔥 start возвращает ENTER_FULL_NAME = {ENTER_FULL_NAME}")
            return ENTER_FULL_NAME

# Функция checkin
async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отметка начала рабочего дня"""
    user_id = update.effective_user.id
    
    user = get_user(user_id)
    if not user:
        if update.callback_query:
            await update.callback_query.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        else:
            await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
    active_shift = get_active_shift(user_id)
    if active_shift:
        checkin_time = format_time_utc8(datetime.fromisoformat(active_shift[1]))
        if update.callback_query:
            await update.callback_query.message.reply_text(
                f"❌ У вас уже есть активная смена, начатая в {checkin_time}"
            )
        else:
            await update.message.reply_text(
                f"❌ У вас уже есть активная смена, начатая в {checkin_time}"
            )
        return
    
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
    
    result_message = f"✅ Начало смены отмечено в {format_time_utc8(now)}\n📅 Дата: {today}\nНе забудьте закрыть смену"
    
    if update.callback_query:
        await update.callback_query.message.reply_text(result_message)
    else:
        await update.message.reply_text(result_message)

# Функция checkout
async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отметка конца рабочего дня"""
    user_id = update.effective_user.id
    
    user = get_user(user_id)
    if not user:
        if update.callback_query:
            await update.callback_query.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        else:
            await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
    active_shift = get_active_shift(user_id)
    if not active_shift:
        if update.callback_query:
            await update.callback_query.message.reply_text(
                "❌ У вас нет активной смены. Используйте открытие смены"
            )
        else:
            await update.message.reply_text(
                "❌ У вас нет активной смены. Используйте открытие смены"
            )
        return
    
    shift_id, checkin_time_str = active_shift
    checkin_time = datetime.fromisoformat(checkin_time_str)
    checkout_time = get_now_utc8()
    
    hours_worked = (checkout_time - checkin_time).total_seconds() / 3600
    
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE timesheet 
        SET status = 'completed', check_out = ?, hours = ?
        WHERE id = ?
    ''', (checkout_time.isoformat(), round(hours_worked, 2), shift_id))
    conn.commit()
    conn.close()
    
    result_message = f"✅ Конец смены отмечен в {format_time_utc8(checkout_time)}\n⏱ Отработано часов: {hours_worked:.2f}"
    
    if update.callback_query:
        await update.callback_query.message.reply_text(result_message)
    else:
        await update.message.reply_text(result_message)

# Функция timesheet
async def timesheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр табеля за указанный период"""
    user_id = update.effective_user.id
    
    user = get_user(user_id)
    if not user:
        if update.callback_query:
            await update.callback_query.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        else:
            await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
    args = context.args
    days = 7
    
    if args and args[0].isdigit():
        days = int(args[0])
    
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
        if update.callback_query:
            await update.callback_query.message.reply_text(f"📊 Нет записей за последние {days} дней")
        else:
            await update.message.reply_text(f"📊 Нет записей за последние {days} дней")
        return
    
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
    
    if update.callback_query:
        await update.callback_query.message.reply_text(report)
    else:
        await update.message.reply_text(report)

# Функция stats
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика за 30 дней по дням недели"""
    user_id = update.effective_user.id
    
    user = get_user(user_id)
    if not user:
        if update.callback_query:
            await update.callback_query.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        else:
            await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
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
        if update.callback_query:
            await update.callback_query.message.reply_text("📊 Нет данных за последние 30 дней")
        else:
            await update.message.reply_text("📊 Нет данных за последние 30 дней")
        return
    
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
    
    if update.callback_query:
        await update.callback_query.message.reply_text(report)
    else:
        await update.message.reply_text(report)

# Функция для показа открытых смен
async def show_open_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все открытые смены"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user or not (user[3] or user[4]):  # Проверка прав администратора
        if update.callback_query:
            await update.callback_query.message.reply_text("❌ Недостаточно прав")
        else:
            await update.message.reply_text("❌ Недостаточно прав")
        return
    
    today = get_today_date_utc8()
    
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT e.full_name, e.store, e.position, t.check_in, t.user_id
        FROM timesheet t
        JOIN employees e ON t.user_id = e.user_id
        WHERE t.date = ? AND t.status = 'working'
        ORDER BY e.store, e.full_name
    ''', (today,))
    
    open_shifts = cursor.fetchall()
    conn.close()
    
    if not open_shifts:
        if update.callback_query:
            await update.callback_query.message.reply_text("✅ Сегодня нет открытых смен")
        else:
            await update.message.reply_text("✅ Сегодня нет открытых смен")
        return
    
    text = "🔓 ОТКРЫТЫЕ СМЕНЫ СЕГОДНЯ\n\n"
    
    for shift in open_shifts:
        full_name, store, position, check_in, user_id = shift
        check_in_time = format_time_utc8(datetime.fromisoformat(check_in)) if check_in else ""
        text += f"👤 {full_name}\n"
        text += f"   🏪 {store} | 📋 {position}\n"
        text += f"   ⏱ Открыта в {check_in_time}\n\n"
    
    if update.callback_query:
        await update.callback_query.message.reply_text(text)
    else:
        await update.message.reply_text(text)

# НОВАЯ ФУНКЦИЯ: Меню управления сменами
async def admin_manage_shifts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления сменами для администратора"""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("➕ Добавить смену", callback_data="admin_add_shift")],
        [InlineKeyboardButton("➖ Удалить смену", callback_data="admin_del_shift")],
        [InlineKeyboardButton("📋 Отчет по сменам", callback_data="admin_shifts_report")],
        [InlineKeyboardButton("◀️ Назад в панель админа", callback_data="back_to_admin")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🔧 **Управление сменами**\n\n"
        "Выберите действие:\n"
        "➕ Добавить смену - создать смену для любого сотрудника\n"
        "➖ Удалить смену - удалить существующую смену\n"
        "📋 Отчет по сменам - просмотр всех смен за период",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# НОВАЯ ФУНКЦИЯ: Добавление смены - выбор магазина
async def admin_add_shift_select_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор магазина для добавления смены"""
    query = update.callback_query
    await query.answer()

    stores = get_stores()
    if not stores:
        await query.edit_message_text("❌ Нет доступных магазинов. Сначала создайте магазин.")
        return ConversationHandler.END

    keyboard = []
    for store_name, store_address in stores:
        display_name = f"{store_name}" + (f" ({store_address})" if store_address else "")
        keyboard.append([InlineKeyboardButton(display_name, callback_data=f"add_store_{store_name}")])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_admin_action")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🏪 **Выберите магазин**\n\n"
        "Сотрудники будут выбраны из этого магазина:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return ADMIN_ADD_SHIFT_SELECT_STORE

# НОВАЯ ФУНКЦИЯ: Добавление смены - выбор сотрудника
async def admin_add_shift_select_employee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор сотрудника из выбранного магазина"""
    query = update.callback_query
    await query.answer()

    store_name = query.data.replace("add_store_", "")
    context.user_data['admin_selected_store'] = store_name

    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, full_name FROM employees WHERE store = ? ORDER BY full_name",
        (store_name,)
    )
    employees = cursor.fetchall()
    conn.close()

    if not employees:
        await query.edit_message_text(f"❌ В магазине {store_name} нет сотрудников")
        return ConversationHandler.END

    keyboard = []
    for user_id, full_name in employees:
        keyboard.append([InlineKeyboardButton(full_name, callback_data=f"add_emp_{user_id}")])

    keyboard.append([InlineKeyboardButton("◀️ Назад к магазинам", callback_data="back_to_stores")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_admin_action")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"🏪 **Магазин:** {store_name}\n\n"
        f"👤 **Выберите сотрудника:**",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return ADMIN_ADD_SHIFT_SELECT_EMPLOYEE

# НОВАЯ ФУНКЦИЯ: Добавление смены - ввод даты
async def admin_add_shift_enter_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод даты для добавления смены"""
    query = update.callback_query
    await query.answer()

    user_id = int(query.data.replace("add_emp_", ""))
    context.user_data['admin_target_user'] = user_id

    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute("SELECT full_name FROM employees WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()

    employee_name = result[0] if result else "Неизвестно"
    context.user_data['admin_target_name'] = employee_name

    today = get_today_date_utc8()
    await query.edit_message_text(
        f"👤 **Сотрудник:** {employee_name}\n"
        f"🏪 **Магазин:** {context.user_data['admin_selected_store']}\n\n"
        f"📅 **Введите дату смены** в формате `ГГГГ-ММ-ДД`\n"
        f"(например, `{today}`):\n\n"
        f"❗️ Дата не может быть в будущем\n"
        f"Или отправьте /cancel для отмены",
        parse_mode='Markdown'
    )
    return ADMIN_ADD_SHIFT_ENTER_DATE

# НОВАЯ ФУНКЦИЯ: Добавление смены - ввод часов
async def admin_add_shift_enter_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенной даты и запрос количества часов"""
    date_str = update.message.text.strip()

    try:
        shift_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = get_today_date_utc8()
        today_date = datetime.strptime(today, "%Y-%m-%d").date()
        shift_date_str = shift_date.isoformat()

        if shift_date_str > today:
            await update.message.reply_text(
                "❌ Нельзя добавить смену на будущую дату.\n"
                "Введите другую дату в формате ГГГГ-ММ-ДД:"
            )
            return ADMIN_ADD_SHIFT_ENTER_DATE

        context.user_data['admin_shift_date'] = shift_date_str

        await update.message.reply_text(
            f"📅 **Дата:** {shift_date_str}\n\n"
            f"⏰ **Введите количество часов** (от 2 до 12):\n"
            f"Начало смены всегда в 7:00\n"
            f"Пример: 8 или 8.5",
            parse_mode='Markdown'
        )
        return ADMIN_ADD_SHIFT_ENTER_HOURS

    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД\n"
            "Например: 2024-01-31\n\n"
            "Попробуйте снова:"
        )
        return ADMIN_ADD_SHIFT_ENTER_DATE

# НОВАЯ ФУНКЦИЯ: Добавление смены - сохранение
async def admin_add_shift_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранение добавленной смены"""
    try:
        hours_text = update.message.text.strip().replace(',', '.')
        hours = float(hours_text)

        if hours < 2 or hours > 12:
            await update.message.reply_text(
                "❌ Количество часов должно быть от 2 до 12.\n"
                "Введите корректное значение:"
            )
            return ADMIN_ADD_SHIFT_ENTER_HOURS

        if hours * 2 != int(hours * 2):
            await update.message.reply_text(
                "❌ Часы должны быть целыми или половинчатыми (например, 8 или 8.5).\n"
                "Введите корректное значение:"
            )
            return ADMIN_ADD_SHIFT_ENTER_HOURS

        user_id = context.user_data['admin_target_user']
        employee_name = context.user_data['admin_target_name']
        shift_date = context.user_data['admin_shift_date']
        admin_id = update.effective_user.id
        admin_name = update.effective_user.full_name

        check_in = "07:00"
        total_minutes = int(hours * 60)
        check_out_hour = 7 + total_minutes // 60
        check_out_minute = total_minutes % 60
        check_out = f"{check_out_hour:02d}:{check_out_minute:02d}"

        conn = sqlite3.connect('timesheet.db')
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id, status FROM timesheet WHERE user_id = ? AND date = ?",
            (user_id, shift_date)
        )
        existing = cursor.fetchone()

        if existing:
            shift_id, status = existing
            if status == 'working':
                await update.message.reply_text(
                    f"❌ У сотрудника {employee_name} уже есть активная смена на {shift_date}"
                )
            else:
                await update.message.reply_text(
                    f"❌ У сотрудника {employee_name} уже есть смена на {shift_date} (закрыта)"
                )
            conn.close()
            return ConversationHandler.END

        cursor.execute(
            """INSERT INTO timesheet 
               (user_id, date, status, check_in, check_out, hours, confirmed)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (user_id, shift_date, 'closed', check_in, check_out, hours)
        )

        conn.commit()
        conn.close()

        log_admin_action(
            admin_id, admin_name, 'add_shift', 
            user_id, employee_name,
            f"Дата: {shift_date}, Часы: {hours} ({check_in}-{check_out})"
        )

        await update.message.reply_text(
            f"✅ **Смена успешно добавлена!**\n\n"
            f"👤 **Сотрудник:** {employee_name}\n"
            f"📅 **Дата:** {shift_date}\n"
            f"⏰ **Время:** {check_in} - {check_out}\n"
            f"📊 **Часы:** {hours}\n"
            f"👑 **Администратор:** {admin_name}",
            parse_mode='Markdown'
        )

    except ValueError:
        await update.message.reply_text(
            "❌ Введите число (часы)\n"
            "Например: 8 или 8.5"
        )
        return ADMIN_ADD_SHIFT_ENTER_HOURS

    return ConversationHandler.END

# НОВАЯ ФУНКЦИЯ: Удаление смены - выбор магазина
async def admin_delete_shift_select_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор магазина для удаления смены"""
    query = update.callback_query
    await query.answer()

    stores = get_stores()
    if not stores:
        await query.edit_message_text("❌ Нет доступных магазинов")
        return ConversationHandler.END

    keyboard = []
    for store_name, store_address in stores:
        display_name = f"{store_name}" + (f" ({store_address})" if store_address else "")
        keyboard.append([InlineKeyboardButton(display_name, callback_data=f"del_store_{store_name}")])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_admin_action")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🏪 **Выберите магазин** для удаления смены:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return ADMIN_DELETE_SHIFT_SELECT_STORE

# НОВАЯ ФУНКЦИЯ: Удаление смены - выбор сотрудника
async def admin_delete_shift_select_employee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор сотрудника для удаления смены"""
    query = update.callback_query
    await query.answer()

    store_name = query.data.replace("del_store_", "")
    context.user_data['admin_del_store'] = store_name

    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, full_name FROM employees WHERE store = ? ORDER BY full_name",
        (store_name,)
    )
    employees = cursor.fetchall()
    conn.close()

    if not employees:
        await query.edit_message_text(f"❌ В магазине {store_name} нет сотрудников")
        return ConversationHandler.END

    keyboard = []
    for user_id, full_name in employees:
        keyboard.append([InlineKeyboardButton(full_name, callback_data=f"del_emp_{user_id}")])

    keyboard.append([InlineKeyboardButton("◀️ Назад к магазинам", callback_data="back_to_del_stores")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_admin_action")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"🏪 **Магазин:** {store_name}\n\n"
        f"👤 **Выберите сотрудника:**",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return ADMIN_DELETE_SHIFT_SELECT_EMPLOYEE

# НОВАЯ ФУНКЦИЯ: Удаление смены - выбор даты
async def admin_delete_shift_select_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор даты для удаления смены"""
    query = update.callback_query
    await query.answer()

    user_id = int(query.data.replace("del_emp_", ""))
    context.user_data['admin_del_user'] = user_id

    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute("SELECT full_name FROM employees WHERE user_id = ?", (user_id,))
    emp_result = cursor.fetchone()
    employee_name = emp_result[0] if emp_result else "Неизвестно"
    context.user_data['admin_del_name'] = employee_name

    cursor.execute(
        """SELECT date, hours, status, confirmed 
           FROM timesheet 
           WHERE user_id = ? 
           ORDER BY date DESC 
           LIMIT 20""",
        (user_id,)
    )
    shifts = cursor.fetchall()
    conn.close()

    if not shifts:
        await query.edit_message_text(
            f"❌ У сотрудника {employee_name} нет смен для удаления"
        )
        return ConversationHandler.END

    keyboard = []
    for shift_date, hours, status, confirmed in shifts:
        status_icon = "✅" if status == 'closed' else "🟢"
        confirm_icon = "🔒" if confirmed else "🔓"
        display_text = f"{shift_date} ({hours} ч.) {status_icon}{confirm_icon}"
        keyboard.append([InlineKeyboardButton(display_text, callback_data=f"del_date_{shift_date}")])

    keyboard.append([InlineKeyboardButton("◀️ Назад к сотрудникам", callback_data="back_to_del_employees")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_admin_action")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"👤 **Сотрудник:** {employee_name}\n"
        f"🏪 **Магазин:** {context.user_data['admin_del_store']}\n\n"
        f"📅 **Выберите дату смены для удаления**\n"
        f"(показаны последние 20 смен):",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return ADMIN_DELETE_SHIFT_SELECT_DATE

# НОВАЯ ФУНКЦИЯ: Удаление смены - подтверждение
async def admin_delete_shift_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение и удаление выбранной смены"""
    query = update.callback_query
    await query.answer()

    shift_date = query.data.replace("del_date_", "")
    user_id = context.user_data['admin_del_user']
    employee_name = context.user_data['admin_del_name']
    store_name = context.user_data['admin_del_store']
    admin_id = update.effective_user.id
    admin_name = update.effective_user.full_name

    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, hours FROM timesheet WHERE user_id = ? AND date = ?",
        (user_id, shift_date)
    )
    shift = cursor.fetchone()

    if not shift:
        await query.edit_message_text(
            f"❌ Смена на {shift_date} не найдена"
        )
        conn.close()
        return ConversationHandler.END

    shift_id, hours = shift

    cursor.execute(
        "DELETE FROM timesheet WHERE user_id = ? AND date = ?",
        (user_id, shift_date)
    )
    conn.commit()
    conn.close()

    log_admin_action(
        admin_id, admin_name, 'delete_shift', 
        user_id, employee_name,
        f"Дата: {shift_date}, Часы: {hours}"
    )

    await query.edit_message_text(
        f"✅ **Смена успешно удалена!**\n\n"
        f"👤 **Сотрудник:** {employee_name}\n"
        f"🏪 **Магазин:** {store_name}\n"
        f"📅 **Дата:** {shift_date}\n"
        f"📊 **Часы:** {hours}\n"
        f"👑 **Администратор:** {admin_name}",
        parse_mode='Markdown'
    )

    return ConversationHandler.END

# НОВАЯ ФУНКЦИЯ: Отчет по сменам
async def admin_shifts_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отчет по всем сменам за период"""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("📅 За сегодня", callback_data="report_today")],
        [InlineKeyboardButton("📅 За вчера", callback_data="report_yesterday")],
        [InlineKeyboardButton("📅 За неделю", callback_data="report_week")],
        [InlineKeyboardButton("📅 За месяц", callback_data="report_month")],
        [InlineKeyboardButton("📅 Произвольный период", callback_data="report_custom")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_manage_shifts")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "📋 **Отчет по сменам**\n\n"
        "Выберите период:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# ИСПРАВЛЕННАЯ ФУНКЦИЯ: Подтверждение смен за период
async def confirm_shifts_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора периода для подтверждения смен"""
    query = update.callback_query
    await query.answer()

    action = query.data
    today = get_today_date_utc8()
    today_dt = datetime.strptime(today, "%Y-%m-%d").date()

    if action == "confirm_week":
        start_date = (today_dt - timedelta(days=7)).isoformat()
        end_date = today
        period_name = "последние 7 дней"
    elif action == "confirm_month":
        start_date = (today_dt - timedelta(days=30)).isoformat()
        end_date = today
        period_name = "последние 30 дней"
    elif action == "confirm_all":
        start_date = "2000-01-01"
        end_date = today
        period_name = "все время"
    elif action == "confirm_custom":
        context.user_data['confirm_action'] = 'confirm'
        await query.edit_message_text(
            "📅 Введите начальную дату периода в формате ГГГГ-ММ-ДД\n"
            "Например: 2024-01-01"
        )
        return ADMIN_CONFIRM_PERIOD_START
    else:
        await query.edit_message_text("❌ Неизвестное действие")
        return

    context.user_data['confirm_start'] = start_date
    context.user_data['confirm_end'] = end_date

    await show_unconfirmed_shifts_by_date(update, context, start_date, end_date, period_name)

# НОВАЯ ФУНКЦИЯ: Начало произвольного периода подтверждения
async def confirm_period_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение начальной даты для произвольного периода подтверждения"""
    date_str = update.message.text.strip()

    try:
        start_date = datetime.strptime(date_str, "%Y-%m-%d").date().isoformat()
        context.user_data['confirm_start'] = start_date

        await update.message.reply_text(
            f"✅ Начальная дата: {start_date}\n\n"
            f"📅 Теперь введите конечную дату периода в формате ГГГГ-ММ-ДД:"
        )
        return ADMIN_CONFIRM_PERIOD_END

    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД\n"
            "Например: 2024-01-31\n\n"
            "Попробуйте снова:"
        )
        return ADMIN_CONFIRM_PERIOD_START

# НОВАЯ ФУНКЦИЯ: Конец произвольного периода подтверждения
async def confirm_period_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение конечной даты для произвольного периода подтверждения"""
    date_str = update.message.text.strip()

    try:
        end_date = datetime.strptime(date_str, "%Y-%m-%d").date().isoformat()
        start_date = context.user_data.get('confirm_start')

        if end_date < start_date:
            await update.message.reply_text(
                "❌ Конечная дата не может быть раньше начальной.\n"
                "Введите корректную конечную дату:"
            )
            return ADMIN_CONFIRM_PERIOD_END

        context.user_data['confirm_end'] = end_date
        period_name = f"{start_date} - {end_date}"

        await show_unconfirmed_shifts_by_date(update, context, start_date, end_date, period_name, is_message=True)
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД\n"
            "Например: 2024-01-31\n\n"
            "Попробуйте снова:"
        )
        return ADMIN_CONFIRM_PERIOD_END

# ИСПРАВЛЕННАЯ ФУНКЦИЯ: Показать неподтвержденные смены по датам
async def show_unconfirmed_shifts_by_date(update: Update, context: ContextTypes.DEFAULT_TYPE, start_date: str, end_date: str, period_name: str, is_message: bool = False):
    """Показать неподтвержденные смены, сгруппированные по датам"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()

    cursor.execute("""
        SELECT t.date, e.full_name, e.store, t.hours, t.id, t.user_id
        FROM timesheet t
        JOIN employees e ON t.user_id = e.user_id
        WHERE t.date BETWEEN ? AND ? 
          AND t.confirmed = 0
          AND t.status = 'closed'
        ORDER BY t.date DESC, e.store, e.full_name
    """, (start_date, end_date))

    shifts = cursor.fetchall()
    conn.close()

    if is_message:
        message = update.message
    else:
        message = update.callback_query.message

    if not shifts:
        await message.reply_text(
            f"📊 За период {period_name} нет неподтвержденных закрытых смен."
        )
        return

    shifts_by_date = {}
    for shift_date, emp_name, store, hours, shift_id, user_id in shifts:
        if shift_date not in shifts_by_date:
            shifts_by_date[shift_date] = []
        shifts_by_date[shift_date].append({
            'emp_name': emp_name,
            'store': store,
            'hours': hours,
            'shift_id': shift_id,
            'user_id': user_id
        })

    keyboard = []
    for shift_date in sorted(shifts_by_date.keys(), reverse=True):
        date_dt = datetime.strptime(shift_date, "%Y-%m-%d")
        date_str = date_dt.strftime("%d.%m.%Y")
        count = len(shifts_by_date[shift_date])
        total_hours = sum(s['hours'] for s in shifts_by_date[shift_date])
        keyboard.append([
            InlineKeyboardButton(
                f"📅 {date_str} | {count} смен | {total_hours} ч.",
                callback_data=f"confirm_date_{shift_date}"
            )
        ])

    keyboard.append([InlineKeyboardButton("✅ Подтвердить все", callback_data="confirm_all_shifts")])
    keyboard.append([InlineKeyboardButton("◀️ Назад к периодам", callback_data="admin_confirm_shifts")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.reply_text(
        f"📊 **Неподтвержденные смены**\n"
        f"Период: {period_name}\n\n"
        f"Найдено смен: {len(shifts)}\n"
        f"Всего часов: {sum(s[3] for s in shifts):.1f}\n\n"
        f"Выберите дату для просмотра:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# ИСПРАВЛЕННАЯ ФУНКЦИЯ: Показать смены за выбранную дату
async def show_shifts_for_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все неподтвержденные смены за выбранную дату"""
    query = update.callback_query
    await query.answer()

    date_str = query.data.replace("confirm_date_", "")

    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()

    cursor.execute("""
        SELECT t.id, e.full_name, e.store, t.hours, t.check_in, t.check_out, t.user_id
        FROM timesheet t
        JOIN employees e ON t.user_id = e.user_id
        WHERE t.date = ? AND t.confirmed = 0 AND t.status = 'closed'
        ORDER BY e.store, e.full_name
    """, (date_str,))

    shifts = cursor.fetchall()
    conn.close()

    if not shifts:
        await query.edit_message_text(f"❌ На дату {date_str} нет неподтвержденных смен")
        return

    date_dt = datetime.strptime(date_str, "%Y-%m-%d")
    display_date = date_dt.strftime("%d.%m.%Y")

    message_text = f"📅 **Смены за {display_date}**\n\n"

    total_hours = 0
    for shift_id, emp_name, store, hours, check_in, check_out, user_id in shifts:
        message_text += f"👤 {emp_name}\n"
        message_text += f"🏪 {store}\n"
        message_text += f"⏰ {check_in} - {check_out} ({hours} ч.)\n"
        message_text += f"🆔 {shift_id}\n\n"
        total_hours += hours

    message_text += f"📊 **Итого: {len(shifts)} смен, {total_hours:.1f} часов**"

    keyboard = []
    for shift_id, emp_name, store, hours, check_in, check_out, user_id in shifts:
        keyboard.append([
            InlineKeyboardButton(
                f"✅ {emp_name} ({hours} ч.)",
                callback_data=f"confirm_shift_{shift_id}"
            )
        ])

    keyboard.append([InlineKeyboardButton("✅ Подтвердить все за эту дату", callback_data=f"confirm_date_all_{date_str}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад к датам", callback_data="back_to_dates")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        message_text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# ИСПРАВЛЕННАЯ ФУНКЦИЯ: Подтверждение одной смены
async def confirm_single_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение одной смены"""
    query = update.callback_query
    await query.answer()

    shift_id = int(query.data.replace("confirm_shift_", ""))
    admin_id = update.effective_user.id
    admin_name = update.effective_user.full_name
    now = get_now_utc8().isoformat()

    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()

    cursor.execute("""
        SELECT t.user_id, e.full_name, t.date, t.hours
        FROM timesheet t
        JOIN employees e ON t.user_id = e.user_id
        WHERE t.id = ?
    """, (shift_id,))
    shift_info = cursor.fetchone()

    if not shift_info:
        await query.answer("❌ Смена не найдена")
        conn.close()
        return

    user_id, emp_name, shift_date, hours = shift_info

    cursor.execute(
        "UPDATE timesheet SET confirmed = 1, confirmed_by = ?, confirmed_date = ? WHERE id = ?",
        (admin_id, now, shift_id)
    )
    conn.commit()
    conn.close()

    log_admin_action(
        admin_id, admin_name, 'confirm_shift',
        user_id, emp_name,
        f"Смена ID: {shift_id}, Дата: {shift_date}, Часы: {hours}"
    )

    await query.answer("✅ Смена подтверждена!")
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"✅ Смена для {emp_name} на {shift_date} подтверждена.")

# ИСПРАВЛЕННАЯ ФУНКЦИЯ: Подтверждение всех смен за дату
async def confirm_all_shifts_for_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение всех смен за выбранную дату"""
    query = update.callback_query
    await query.answer()

    date_str = query.data.replace("confirm_date_all_", "")
    admin_id = update.effective_user.id
    admin_name = update.effective_user.full_name
    now = get_now_utc8().isoformat()

    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()

    cursor.execute("""
        SELECT t.id, t.user_id, e.full_name, t.hours
        FROM timesheet t
        JOIN employees e ON t.user_id = e.user_id
        WHERE t.date = ? AND t.confirmed = 0 AND t.status = 'closed'
    """, (date_str,))
    shifts = cursor.fetchall()

    if not shifts:
        await query.answer("❌ Нет смен для подтверждения")
        conn.close()
        return

    cursor.execute(
        "UPDATE timesheet SET confirmed = 1, confirmed_by = ?, confirmed_date = ? WHERE date = ? AND confirmed = 0 AND status = 'closed'",
        (admin_id, now, date_str)
    )
    conn.commit()

    for shift_id, user_id, emp_name, hours in shifts:
        log_admin_action(
            admin_id, admin_name, 'confirm_shift',
            user_id, emp_name,
            f"Смена ID: {shift_id}, Дата: {date_str}, Часы: {hours} (все за дату)"
        )

    conn.close()

    await query.answer(f"✅ Подтверждено {len(shifts)} смен!")
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"✅ Все смены за {date_str} подтверждены ({len(shifts)} смен).")

# ИСПРАВЛЕННАЯ ФУНКЦИЯ: Подтверждение всех смен за период
async def confirm_all_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение всех неподтвержденных смен за выбранный период"""
    query = update.callback_query
    await query.answer()

    start_date = context.user_data.get('confirm_start')
    end_date = context.user_data.get('confirm_end')
    admin_id = update.effective_user.id
    admin_name = update.effective_user.full_name
    now = get_now_utc8().isoformat()

    if not start_date or not end_date:
        await query.edit_message_text("❌ Ошибка: не выбран период")
        return

    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()

    cursor.execute("""
        SELECT t.id, t.user_id, e.full_name, t.date, t.hours
        FROM timesheet t
        JOIN employees e ON t.user_id = e.user_id
        WHERE t.date BETWEEN ? AND ? AND t.confirmed = 0 AND t.status = 'closed'
    """, (start_date, end_date))
    shifts = cursor.fetchall()

    if not shifts:
        await query.edit_message_text("❌ Нет смен для подтверждения")
        conn.close()
        return

    cursor.execute(
        "UPDATE timesheet SET confirmed = 1, confirmed_by = ?, confirmed_date = ? WHERE date BETWEEN ? AND ? AND confirmed = 0 AND status = 'closed'",
        (admin_id, now, start_date, end_date)
    )
    conn.commit()

    for shift_id, user_id, emp_name, shift_date, hours in shifts:
        log_admin_action(
            admin_id, admin_name, 'confirm_shift',
            user_id, emp_name,
            f"Смена ID: {shift_id}, Дата: {shift_date}, Часы: {hours} (все за период)"
        )

    conn.close()

    await query.edit_message_text(
        f"✅ **Все смены подтверждены!**\n\n"
        f"Период: {start_date} - {end_date}\n"
        f"Подтверждено смен: {len(shifts)}\n"
        f"Всего часов: {sum(s[4] for s in shifts):.1f}",
        parse_mode='Markdown'
    )

# НОВЫЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ для навигации
async def back_to_stores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к выбору магазина при добавлении"""
    query = update.callback_query
    await query.answer()
    return await admin_add_shift_select_store(update, context)

async def back_to_del_stores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к выбору магазина при удалении"""
    query = update.callback_query
    await query.answer()
    return await admin_delete_shift_select_store(update, context)

async def back_to_del_employees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к выбору сотрудника при удалении"""
    query = update.callback_query
    await query.answer()
    return await admin_delete_shift_select_employee(update, context)

async def back_to_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к списку дат при подтверждении"""
    query = update.callback_query
    await query.answer()

    start_date = context.user_data.get('confirm_start')
    end_date = context.user_data.get('confirm_end')
    period_name = f"{start_date} - {end_date}"

    await show_unconfirmed_shifts_by_date(update, context, start_date, end_date, period_name)
    return

async def cancel_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена действия администратора"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Действие отменено")
    return ConversationHandler.END

# Обновленная функция admin_panel с добавлением кнопки управления сменами
@require_auth(admin_only=True)
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Панель администратора"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    is_super_admin = user[4] if user else 0
    
    # Добавляем пункты меню
    keyboard = [
        [InlineKeyboardButton("✅ Открыть смену", callback_data="admin_checkin")],
        [InlineKeyboardButton("✅ Закрыть смену", callback_data="admin_checkout")],
        [InlineKeyboardButton("📊 Мой табель", callback_data="admin_timesheet")],
        [InlineKeyboardButton("📈 Моя статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Все сотрудники", callback_data="admin_list")],
        [InlineKeyboardButton("📊 По магазинам", callback_data="admin_by_store")],
        [InlineKeyboardButton("🔓 Открытые смены", callback_data="admin_open_shifts")],
        [InlineKeyboardButton("📅 Выбрать период", callback_data="period_selection")],
        [InlineKeyboardButton("📈 Статистика по магазинам", callback_data="admin_store_stats")],
        [InlineKeyboardButton("✅ Подтверждение смен", callback_data="admin_confirm_shifts")],
        [InlineKeyboardButton("🔧 Управление сменами", callback_data="admin_manage_shifts")],
        [InlineKeyboardButton("🗑 Запросить удаление", callback_data="admin_delete_menu")],
        [InlineKeyboardButton("📋 Управление должностями", callback_data="admin_positions_menu")],
        [InlineKeyboardButton("🏪 Управление магазинами", callback_data="admin_stores_menu")],
    ]
    
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

# Функция для показа меню подтверждения смен
async def show_confirm_menu(query):
    """Меню подтверждения смен"""
    keyboard = [
        [InlineKeyboardButton("📋 Неподтвержденные сегодня", callback_data="confirm_today")],
        [InlineKeyboardButton("📅 За период", callback_data="confirm_period_menu")],
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

# Функция для показа меню выбора периода подтверждения
async def show_period_confirm_menu(query):
    """Меню выбора периода для подтверждения"""
    keyboard = [
        [InlineKeyboardButton("📅 7 дней", callback_data="confirm_week")],
        [InlineKeyboardButton("📅 30 дней", callback_data="confirm_month")],
        [InlineKeyboardButton("📅 Все время", callback_data="confirm_all")],
        [InlineKeyboardButton("📅 Свой период", callback_data="confirm_custom")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_confirm_shifts")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "📅 ВЫБОР ПЕРИОДА\n\n"
        "Выберите период для просмотра неподтвержденных смен:",
        reply_markup=reply_markup
    )

# Вспомогательная функция для показа всех сотрудников
async def show_all_employees(query):
    """Показать всех сотрудников"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT full_name, position, store, is_admin, is_super_admin, can_request_admin 
        FROM employees ORDER BY store, full_name
    ''')
    employees = cursor.fetchall()
    conn.close()
    
    if not employees:
        await query.edit_message_text("👥 Нет зарегистрированных сотрудников")
        return
    
    text = "👥 ВСЕ СОТРУДНИКИ\n\n"
    for emp in employees:
        full_name, position, store, is_admin, is_super_admin, can_request_admin = emp
        role = "⭐ Супер-админ" if is_super_admin else "👑 Админ" if is_admin else "👤 Сотрудник"
        request_status = " ✅ может запросить админку" if can_request_admin and not (is_admin or is_super_admin) else ""
        text += f"• {full_name}\n  {role} | {position} | {store}{request_status}\n\n"
    
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
    else:
        await query.edit_message_text(text)
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Функция для отображения сотрудников по магазинам
async def show_employees_by_store(query):
    """Показать сотрудников по магазинам с отметками о сменах"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    today = get_today_date_utc8()
    
    cursor.execute('''
        SELECT e.store, e.user_id, e.full_name, e.position, e.is_admin, e.is_super_admin, e.can_request_admin,
               t.status, t.check_in, t.check_out
        FROM employees e
        LEFT JOIN timesheet t ON e.user_id = t.user_id AND t.date = ?
        ORDER BY e.store, e.full_name
    ''', (today,))
    
    employees = cursor.fetchall()
    conn.close()
    
    if not employees:
        await query.edit_message_text("👥 Нет зарегистрированных сотрудников")
        return
    
    stores_dict = {}
    for emp in employees:
        store, user_id, full_name, position, is_admin, is_super_admin, can_request_admin, status, check_in, check_out = emp
        
        if store not in stores_dict:
            stores_dict[store] = []
        
        shift_status = ""
        if status == "working":
            check_in_time = format_time_utc8(datetime.fromisoformat(check_in)) if check_in else ""
            shift_status = f" 🔓 Смена открыта в {check_in_time}"
        elif status == "completed":
            check_in_time = format_time_utc8(datetime.fromisoformat(check_in)) if check_in else ""
            check_out_time = format_time_utc8(datetime.fromisoformat(check_out)) if check_out else ""
            shift_status = f" ✅ Смена завершена ({check_in_time} - {check_out_time})"
        else:
            shift_status = " ⏳ Смена не открыта"
        
        role = "⭐" if is_super_admin else "👑" if is_admin else "👤"
        request_mark = " 📝" if can_request_admin and not (is_admin or is_super_admin) else ""
        
        stores_dict[store].append(f"{role} {full_name} - {position}{request_mark}{shift_status}")
    
    text = "📊 СОТРУДНИКИ ПО МАГАЗИНАМ\n"
    text += f"📅 {today}\n\n"
    
    for store, employees_list in stores_dict.items():
        text += f"🏪 {store}\n"
        for emp in employees_list:
            text += f"  {emp}\n"
        text += "\n"
    
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
    else:
        await query.edit_message_text(text)
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Функция для показа выбора периода экспорта
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

# Функция для показа опций экспорта
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

# Функция экспорта CSV
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
    
    confirmed_part = "confirmed" if confirmed_only else "all"
    filename = f"timesheet_period_{start_date}_to_{end_date}_{confirmed_part}.csv"
    
    await query.message.reply_document(
        document=io.BytesIO(csv_data),
        filename=filename,
        caption=f"📊 Экспорт за период {start_date} - {end_date}"
    )
    
    await query.edit_message_text("✅ Экспорт завершен!")
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Функция для создания временного query объекта
def create_dummy_query(update):
    """Создать временный query объект для совместимости"""
    return type('Query', (), {
        'data': '',
        'from_user': update.effective_user,
        'message': update.message,
        'answer': lambda: None,
        'edit_message_text': lambda text, reply_markup=None: None
    })()

# Обработчик текстовых сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений"""
    text = update.message.text
    user_id = update.effective_user.id
    
    logger.info(f"Получено сообщение от {user_id}: {text}")
    
    if context.user_data.get('conversation_state'):
        logger.info(f"Пользователь в состоянии {context.user_data['conversation_state']}, пропускаем обработку")
        return
    
    if text == "🏠 Главное меню":
        await start(update, context)
        return
    elif text == "👑 Панель админа":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            await admin_panel(update, context)
        else:
            await update.message.reply_text("❌ У вас нет прав доступа к панели администратора")
        return
    elif text == "✅ Открыть смену":
        await checkin(update, context)
        return
    elif text == "✅ Закрыть смену":
        await checkout(update, context)
        return
    elif text == "📊 Мой табель":
        await timesheet(update, context)
        return
    elif text == "📈 Моя статистика":
        await stats(update, context)
        return
    elif text == "👑 Запросить права администратора":
        user = get_user(user_id)
        if not user:
            await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
            return
        
        full_name, position, store, is_admin, is_super_admin, can_request_admin = user
        
        if is_admin or is_super_admin:
            await update.message.reply_text("❌ Вы уже являетесь администратором")
            return
        
        if not can_request_admin:
            await update.message.reply_text("❌ У вас нет прав для запроса администраторских полномочий")
            return
        
        await handle_admin_request_from_message(update, context, user_id, user)
        return
    
    # Обработка текстовых команд из панели администратора
    user = get_user(user_id)
    if user and (user[3] or user[4]):
        if text == "👥 Все сотрудники":
            query = create_dummy_query(update)
            await show_all_employees(query)
        elif text == "📊 По магазинам":
            query = create_dummy_query(update)
            await show_employees_by_store(query)
        elif text == "🔓 Открытые смены":
            await show_open_shifts(update, context)
        elif text == "📅 Выбрать период":
            query = create_dummy_query(update)
            await show_period_selection(query)
        elif text == "📈 Статистика по магазинам":
            query = create_dummy_query(update)
            await show_store_stats(query)
        elif text == "✅ Подтверждение смен":
            query = create_dummy_query(update)
            await show_confirm_menu(query)
        elif text == "🗑 Запросить удаление":
            query = create_dummy_query(update)
            await show_delete_menu(query)
        elif text == "📋 Управление должностями":
            query = create_dummy_query(update)
            await show_positions_menu(query)
        elif text == "🏪 Управление магазинами":
            query = create_dummy_query(update)
            await show_stores_menu(query)

# Функция для обработки заявки на админа из сообщения
async def handle_admin_request_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, user_info: Tuple):
    """Обработка заявки на становление администратором из сообщения"""
    full_name, position, store, is_admin, is_super_admin, can_request_admin = user_info
    
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id FROM admin_requests 
        WHERE user_id = ? AND status = 'pending'
    ''', (user_id,))
    
    existing = cursor.fetchone()
    
    if existing:
        await update.message.reply_text(
            "❌ У вас уже есть активная заявка на становление администратором"
        )
        conn.close()
        return
    
    cursor.execute('''
        INSERT INTO admin_requests 
        (request_date, user_id, user_name, user_position, user_store, status)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (get_today_date_utc8(), user_id, full_name, position, store, 'pending'))
    
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        "✅ Заявка на становление администратором отправлена!\n"
        "Ожидайте решения супер-администратора."
    )
    
    super_admins = get_super_admins()
    for admin_id, admin_name in super_admins:
        try:
            await update.message.bot.send_message(
                admin_id,
                f"👑 Новая заявка на становление администратором!\n\n"
                f"От: {full_name}\n"
                f"Должность: {position}\n"
                f"Магазин: {store}\n"
                f"ID: {user_id}\n\n"
                f"Используйте 👑 Панель админа для рассмотрения заявки."
            )
        except Exception as e:
            logger.error(f"Failed to notify super admin {admin_id}: {e}")

# Функция для обработки заявки на админа из callback
async def handle_admin_request(query, context, user_id, user_info):
    """Обработка заявки на становление администратором из callback"""
    full_name = user_info[0] if user_info else query.from_user.full_name
    position = user_info[1] if user_info and len(user_info) > 1 else "Не указана"
    store = user_info[2] if user_info and len(user_info) > 2 else "Не указан"
    
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
    
    cursor.execute('''
        INSERT INTO admin_requests 
        (request_date, user_id, user_name, user_position, user_store, status)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (get_today_date_utc8(), user_id, full_name, position, store, 'pending'))
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(
        "✅ Заявка на становление администратором отправлена!\n"
        "Ожидайте решения супер-администратора."
    )
    
    super_admins = get_super_admins()
    for admin_id, admin_name in super_admins:
        try:
            await query.message.bot.send_message(
                admin_id,
                f"👑 Новая заявка на становление администратором!\n\n"
                f"От: {full_name}\n"
                f"Должность: {position}\n"
                f"Магазин: {store}\n"
                f"ID: {user_id}\n\n"
                f"Используйте 👑 Панель админа для рассмотрения заявки."
            )
        except Exception as e:
            logger.error(f"Failed to notify super admin {admin_id}: {e}")

# Вспомогательная функция для показа панели администратора
async def show_admin_panel(query):
    """Показать панель администратора"""
    keyboard = [
        [InlineKeyboardButton("✅ Открыть смену", callback_data="admin_checkin")],
        [InlineKeyboardButton("✅ Закрыть смену", callback_data="admin_checkout")],
        [InlineKeyboardButton("📊 Мой табель", callback_data="admin_timesheet")],
        [InlineKeyboardButton("📈 Моя статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Все сотрудники", callback_data="admin_list")],
        [InlineKeyboardButton("📊 По магазинам", callback_data="admin_by_store")],
        [InlineKeyboardButton("🔓 Открытые смены", callback_data="admin_open_shifts")],
        [InlineKeyboardButton("📅 Выбрать период", callback_data="period_selection")],
        [InlineKeyboardButton("📈 Статистика по магазинам", callback_data="admin_store_stats")],
        [InlineKeyboardButton("✅ Подтверждение смен", callback_data="admin_confirm_shifts")],
        [InlineKeyboardButton("🔧 Управление сменами", callback_data="admin_manage_shifts")],
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

# Функция для отмены действия
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущего действия"""
    await update.message.reply_text("❌ Действие отменено")
    return ConversationHandler.END

# ============================================================
# ПОЛНАЯ ФУНКЦИЯ button_callback (со всеми обработчиками)
# ============================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на инлайн кнопки"""
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    user_id = query.from_user.id
    
    logger.info(f"Callback: {callback_data} от пользователя {user_id}")
    logger.info(f"Текущий user_data: {context.user_data}")
    
    # --- ОБРАБОТЧИКИ РЕГИСТРАЦИИ ---
    
    # Обработка отмены регистрации
    if callback_data == "cancel_registration":
        await query.edit_message_text("❌ Регистрация отменена.")
        return ConversationHandler.END
    
    user = get_user(user_id)
    
    # Обработка регистрации с именем
    if callback_data.startswith("reg_pos_"):
        logger.info("🔥🔥🔥 Обработка выбора должности")
        
        if user:
            await query.edit_message_text("❌ Вы уже зарегистрированы!")
            return ConversationHandler.END
        
        # Получаем имя из user_data
        full_name = context.user_data.get('full_name')
        logger.info(f"Получено имя из user_data: {full_name}")
        
        if not full_name:
            # Если нет имени, используем имя из Telegram
            full_name = query.from_user.full_name
            logger.info(f"Имя не найдено, используем из Telegram: {full_name}")
            await query.edit_message_text(
                f"⚠️ Внимание! Будет использовано имя из Telegram: {full_name}\n"
                f"Если хотите изменить имя, начните регистрацию заново."
            )
            await asyncio.sleep(2)
        
        position = callback_data[8:]
        context.user_data['reg_position'] = position
        context.user_data['full_name'] = full_name
        
        logger.info(f"Выбрана должность: {position}")
        
        stores = get_stores()
        logger.info(f"Получен список магазинов: {stores}")
        
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
            f"👤 Имя: {full_name}\n"
            f"📋 Должность: {position}\n\n"
            f"🏪 Теперь выберите ваш магазин:",
            reply_markup=reply_markup
        )
        
        logger.info(f"🔥 button_callback возвращает SELECT_STORE = {SELECT_STORE}")
        return SELECT_STORE
    
    elif callback_data.startswith("reg_store_"):
        logger.info("🔥🔥🔥 Обработка выбора магазина")
        
        if user:
            await query.edit_message_text("❌ Вы уже зарегистрированы!")
            return ConversationHandler.END
            
        store = callback_data[10:]
        position = context.user_data.get('reg_position')
        full_name = context.user_data.get('full_name')
        
        logger.info(f"Выбран магазин: {store}")
        logger.info(f"Должность из user_data: {position}")
        logger.info(f"Имя из user_data: {full_name}")
        
        if not position:
            logger.error("ОШИБКА: должность не найдена в user_data")
            await query.edit_message_text(
                "❌ Ошибка регистрации. Пожалуйста, начните заново с /start"
            )
            return ConversationHandler.END
        
        if not full_name:
            full_name = query.from_user.full_name
            logger.info(f"Имя не найдено, используем из Telegram: {full_name}")
        
        user_id = query.from_user.id
        
        # Проверяем, является ли должность "директор магазина"
        can_request_admin = 1 if position.lower() == "директор магазина" else 0
        
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
                INSERT INTO employees (user_id, full_name, position, store, reg_date, is_admin, is_super_admin, can_request_admin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, full_name, position, store, get_today_date_utc8(), 0, 0, can_request_admin))
            conn.commit()
            
            logger.info(f"✅✅✅ Новый пользователь зарегистрирован: {user_id} - {full_name} ({position}, {store})")
            logger.info(f"Может запрашивать админку: {can_request_admin}")
            
            await query.edit_message_text(
                f"✅ Регистрация успешно завершена!\n\n"
                f"👤 {full_name}\n"
                f"📋 Должность: {position}\n"
                f"🏪 Магазин: {store}"
            )
            
            # Создаем клавиатуру для обычного сотрудника
            keyboard = get_user_keyboard(can_request_admin)
            
            await query.message.reply_text(
                f"👋 Привет, {full_name}!\n\n"
                f"📋 Используйте кнопки ниже для работы:",
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"ОШИБКА при регистрации: {e}")
            await query.edit_message_text(
                "❌ Произошла ошибка при регистрации. Попробуйте позже."
            )
        finally:
            conn.close()
        
        # Очищаем временные данные
        context.user_data.pop('reg_position', None)
        context.user_data.pop('full_name', None)
        
        logger.info("🔥 Регистрация завершена, возвращаем ConversationHandler.END")
        return ConversationHandler.END
    
    # --- ПРОВЕРКА АВТОРИЗАЦИИ ДЛЯ ОСТАЛЬНЫХ ДЕЙСТВИЙ ---
    
    # Для всех остальных callback_data проверяем авторизацию
    if not user:
        await query.edit_message_text("❌ Вы не зарегистрированы. Используйте /start")
        return
    
    full_name, position, store, is_admin, is_super_admin, can_request_admin = user
    
    # --- ОБРАБОТЧИКИ ЗАКРЫТИЯ И ВОЗВРАТА ---
    
    if callback_data == "close":
        await query.delete_message()
        return
    
    elif callback_data == "back_to_admin":
        await show_admin_panel(query)
        return
    
    elif callback_data == "request_admin":
        await handle_admin_request(query, context, user_id, user)
        return
    
    # --- ОБРАБОТЧИКИ ДЛЯ КОМАНД ОБЫЧНОГО ПОЛЬЗОВАТЕЛЯ В АДМИНКЕ ---
    
    elif callback_data == "admin_checkin":
        logger.info(f"Выполняется admin_checkin для пользователя {user_id}")
        await checkin(update, context)
        return
    
    elif callback_data == "admin_checkout":
        logger.info(f"Выполняется admin_checkout для пользователя {user_id}")
        await checkout(update, context)
        return
    
    elif callback_data == "admin_timesheet":
        logger.info(f"Выполняется admin_timesheet для пользователя {user_id}")
        await timesheet(update, context)
        return
    
    elif callback_data == "admin_stats":
        logger.info(f"Выполняется admin_stats для пользователя {user_id}")
        await stats(update, context)
        return
    
    elif callback_data == "admin_open_shifts":
        logger.info(f"Выполняется admin_open_shifts для пользователя {user_id}")
        await show_open_shifts(update, context)
        return
    
    # --- НОВЫЕ ОБРАБОТЧИКИ ДЛЯ УПРАВЛЕНИЯ СМЕНАМИ ---
    
    elif callback_data == "admin_manage_shifts":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await admin_manage_shifts_menu(update, context)
        return
    
    elif callback_data == "admin_add_shift":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        return await admin_add_shift_select_store(update, context)
    
    elif callback_data == "admin_del_shift":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        return await admin_delete_shift_select_store(update, context)
    
    elif callback_data == "admin_shifts_report":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await admin_shifts_report(update, context)
        return
    
    # --- ОБРАБОТЧИКИ ДЛЯ ДОБАВЛЕНИЯ СМЕНЫ (навигационные) ---
    
    elif callback_data.startswith("add_store_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        return await admin_add_shift_select_employee(update, context)
    
    elif callback_data.startswith("add_emp_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        return await admin_add_shift_enter_date(update, context)
    
    # --- ОБРАБОТЧИКИ ДЛЯ УДАЛЕНИЯ СМЕНЫ (навигационные) ---
    
    elif callback_data.startswith("del_store_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        return await admin_delete_shift_select_employee(update, context)
    
    elif callback_data.startswith("del_emp_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        return await admin_delete_shift_select_date(update, context)
    
    elif callback_data.startswith("del_date_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        return await admin_delete_shift_confirm(update, context)
    
    # --- НОВЫЕ ОБРАБОТЧИКИ ДЛЯ ПОДТВЕРЖДЕНИЯ СМЕН ---
    
    elif callback_data == "admin_confirm_shifts":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_confirm_menu(query)
        return
    
    elif callback_data == "confirm_period_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_period_confirm_menu(query)
        return
    
    elif callback_data in ["confirm_week", "confirm_month", "confirm_all", "confirm_custom"]:
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        return await confirm_shifts_period(update, context)
    
    elif callback_data.startswith("confirm_date_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_shifts_for_date(update, context)
        return
    
    elif callback_data.startswith("confirm_shift_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await confirm_single_shift(update, context)
        return
    
    elif callback_data.startswith("confirm_date_all_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await confirm_all_shifts_for_date(update, context)
        return
    
    elif callback_data == "confirm_all_shifts":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await confirm_all_shifts(update, context)
        return
    
    # --- НАВИГАЦИОННЫЕ ОБРАБОТЧИКИ ДЛЯ УПРАВЛЕНИЯ СМЕНАМИ ---
    
    elif callback_data == "back_to_stores":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        return await back_to_stores(update, context)
    
    elif callback_data == "back_to_del_stores":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        return await back_to_del_stores(update, context)
    
    elif callback_data == "back_to_del_employees":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        return await back_to_del_employees(update, context)
    
    elif callback_data == "back_to_dates":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await back_to_dates(update, context)
        return
    
    elif callback_data == "cancel_admin_action":
        return await cancel_admin_action(update, context)
    
    # --- ОСТАЛЬНЫЕ ОБРАБОТЧИКИ ИЗ ИСХОДНОГО КОДА ---
    # (здесь должны быть все остальные case из вашего исходного кода)
    
    elif callback_data == "admin_list":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_all_employees(query)
        return
    
    elif callback_data == "admin_by_store":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_employees_by_store(query)
        return
    
    elif callback_data == "period_selection":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_period_selection(query)
        return
    
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
                days = 36500
            
            context.user_data['period_days'] = days
            await show_export_options(query, days)
        return
    
    elif callback_data == "export_confirmed":
        if not (is_admin or is_super_admin):
            return
        days = context.user_data.get('period_days', 30)
        await export_csv_period(query, days, confirmed_only=True)
        return
    
    elif callback_data == "export_all":
        if not (is_admin or is_super_admin):
            return
        days = context.user_data.get('period_days', 30)
        await export_csv_period(query, days, confirmed_only=False)
        return
    
    elif callback_data == "admin_store_stats":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_store_stats(query)
        return
    
    elif callback_data == "admin_delete_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_delete_menu(query)
        return
    
    elif callback_data == "admin_positions_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_positions_menu(query)
        return
    
    elif callback_data == "admin_stores_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_stores_menu(query)
        return
    
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
        return
    
    elif callback_data == "delete_position_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_delete_position_menu(query)
        return
    
    elif callback_data.startswith("delete_position_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        position_name = callback_data[15:]
        await delete_position(query, position_name)
        return
    
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
        return
    
    elif callback_data == "delete_store_from_list_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_delete_store_menu(query)
        return
    
    elif callback_data.startswith("delete_store_list_"):
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        store_name = callback_data[17:]
        if store_name.startswith('_'):
            store_name = store_name[1:]
        logger.info(f"Запрос на удаление магазина: {store_name}")
        await delete_store(query, store_name)
        return
    
    elif callback_data == "confirm_today":
        if not (is_admin or is_super_admin):
            return
        await show_unconfirmed_today(query)
        return
    
    elif callback_data == "confirm_all_today":
        if not (is_admin or is_super_admin):
            return
        await confirm_all_today(query)
        return
    
    elif callback_data == "confirm_by_store":
        if not (is_admin or is_super_admin):
            return
        await show_confirm_by_store(query)
        return
    
    elif callback_data == "confirm_stats":
        if not (is_admin or is_super_admin):
            return
        await show_confirm_stats(query)
        return
    
    elif callback_data.startswith("confirm_store_"):
        if not (is_admin or is_super_admin):
            return
        store = callback_data[14:]
        await show_store_unconfirmed(query, store)
        return
    
    elif callback_data.startswith("confirm_all_store_"):
        if not (is_admin or is_super_admin):
            return
        store = callback_data[17:]
        await confirm_all_store(query, store)
        return
    
    elif callback_data == "back_to_confirm":
        await show_confirm_menu(query)
        return
    
    elif callback_data == "delete_employee_menu":
        if not (is_admin or is_super_admin):
            return
        await show_delete_employee_menu(query)
        return
    
    elif callback_data == "delete_store_menu":
        if not (is_admin or is_super_admin):
            return
        await show_delete_store_request_menu(query)
        return
    
    elif callback_data.startswith("request_delete_employee_"):
        if not (is_admin or is_super_admin):
            return
        target_id_str = callback_data[23:]
        logger.info(f"Получен ID сотрудника для удаления: {target_id_str}")
        
        try:
            target_id = int(target_id_str)
            await create_delete_request(query, user_id, full_name, "employee", str(target_id))
        except ValueError as e:
            logger.error(f"Ошибка преобразования ID: {target_id_str} - {e}")
            if target_id_str.startswith('_'):
                target_id_str = target_id_str[1:]
                try:
                    target_id = int(target_id_str)
                    await create_delete_request(query, user_id, full_name, "employee", str(target_id))
                except ValueError as e2:
                    logger.error(f"Ошибка преобразования после удаления подчеркивания: {e2}")
                    await query.edit_message_text("❌ Ошибка в идентификаторе сотрудника")
            else:
                await query.edit_message_text("❌ Ошибка в идентификаторе сотрудника")
        return
    
    elif callback_data.startswith("request_delete_store_"):
        if not (is_admin or is_super_admin):
            return
        store_name = callback_data[20:]
        logger.info(f"Получено название магазина для удаления: {store_name}")
        if store_name.startswith('_'):
            store_name = store_name[1:]
        await create_delete_request(query, user_id, full_name, "store", store_name)
        return
    
    elif callback_data == "admin_requests":
        if not is_super_admin:
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_delete_requests(query)
        return
    
    elif callback_data.startswith("approve_request_"):
        if not is_super_admin:
            return
        request_id = int(callback_data[16:])
        await approve_delete_request(query, request_id)
        return
    
    elif callback_data.startswith("reject_request_"):
        if not is_super_admin:
            return
        request_id = int(callback_data[15:])
        await reject_delete_request(query, request_id)
        return
    
    elif callback_data == "admin_admin_requests":
        if not is_super_admin:
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_admin_requests(query)
        return
    
    elif callback_data.startswith("approve_admin_"):
        if not is_super_admin:
            return
        req_id = int(callback_data[14:])
        await approve_admin_request(query, req_id)
        return
    
    elif callback_data.startswith("reject_admin_"):
        if not is_super_admin:
            return
        req_id = int(callback_data[13:])
        await reject_admin_request(query, req_id)
        return
    
    elif callback_data == "assign_super_admin_menu":
        if not is_super_admin:
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_assign_super_admin_menu(query)
        return
    
    elif callback_data == "assign_super_admin_list":
        if not is_super_admin:
            return
        await show_assign_super_admin_list(query)
        return
    
    elif callback_data == "list_super_admins":
        if not is_super_admin:
            return
        await list_super_admins(query)
        return
    
    elif callback_data.startswith("select_super_admin_"):
        if not is_super_admin:
            return
        target_id = int(callback_data[19:])
        context.user_data['selected_super_admin'] = target_id
        await confirm_assign_super_admin(query, target_id)
        return
    
    elif callback_data == "confirm_assign_super_admin":
        if not is_super_admin:
            return
        target_id = context.user_data.get('selected_super_admin')
        if target_id:
            await assign_super_admin(query, target_id)
        return
    
    elif callback_data == "admin_add":
        if not is_super_admin:
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_add_admin_menu(query)
        return
    
    elif callback_data.startswith("make_admin_"):
        if not is_super_admin:
            await query.edit_message_text("❌ Недостаточно прав")
            return
        
        target_id = int(callback_data[10:])
        logger.info(f"Назначение администратором пользователя {target_id}")
        
        conn = sqlite3.connect('timesheet.db')
        cursor = conn.cursor()
        
        try:
            cursor.execute("SELECT full_name FROM employees WHERE user_id = ?", (target_id,))
            result = cursor.fetchone()
            
            if not result:
                await query.edit_message_text("❌ Сотрудник не найден")
                conn.close()
                return
            
            target_name = result[0]
            
            cursor.execute("UPDATE employees SET is_admin = 1 WHERE user_id = ?", (target_id,))
            conn.commit()
            
            logger.info(f"Сотрудник {target_name} назначен администратором")
            await query.edit_message_text(f"✅ Сотрудник {target_name} назначен администратором!")
            
            try:
                await query.message.bot.send_message(
                    target_id,
                    f"👑 Поздравляем! Вы назначены администратором!\n\n"
                    f"Теперь вам доступна панель администратора."
                )
            except Exception as e:
                logger.error(f"Failed to notify new admin {target_id}: {e}")
            
        except Exception as e:
            logger.error(f"Ошибка при назначении администратора: {e}")
            await query.edit_message_text("❌ Произошла ошибка при назначении администратора")
        finally:
            conn.close()
        
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)
        return
    
    # Если callback_data не обработан
    logger.warning(f"Неизвестный callback_data: {callback_data}")
    await query.edit_message_text("❌ Неизвестная команда")

# ============================================================
# ФУНКЦИЯ MAIN (запуск бота)
# ============================================================

async def main():
    """Упрощенная функция запуска"""
    try:
        await delete_webhook()
        await asyncio.sleep(1)
        
        init_database()
        
        app = Application.builder().token(BOT_TOKEN).build()
        
        # ConversationHandler для регистрации
        reg_conv_handler = ConversationHandler(
            entry_points=[CommandHandler("start", start)],
            states={
                ENTER_FULL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_full_name)],
                SELECT_POSITION: [CallbackQueryHandler(button_callback, pattern="^reg_pos_")],
                SELECT_STORE: [CallbackQueryHandler(button_callback, pattern="^reg_store_")],
            },
            fallbacks=[CommandHandler("cancel", cancel_registration)],
            allow_reentry=True
        )
        app.add_handler(reg_conv_handler)
        
        # НОВЫЙ ConversationHandler для добавления смены
        admin_add_shift_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(admin_add_shift_select_store, pattern="^admin_add_shift$")],
            states={
                ADMIN_ADD_SHIFT_SELECT_STORE: [CallbackQueryHandler(admin_add_shift_select_employee, pattern="^add_store_")],
                ADMIN_ADD_SHIFT_SELECT_EMPLOYEE: [CallbackQueryHandler(admin_add_shift_enter_date, pattern="^add_emp_")],
                ADMIN_ADD_SHIFT_ENTER_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_shift_enter_hours)],
                ADMIN_ADD_SHIFT_ENTER_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_shift_save)],
            },
            fallbacks=[CommandHandler('cancel', cancel_registration),
                       CallbackQueryHandler(cancel_admin_action, pattern="^cancel_admin_action$")],
            name="admin_add_shift_conversation",
            persistent=False,
            allow_reentry=True
        )
        app.add_handler(admin_add_shift_handler)
        
        # НОВЫЙ ConversationHandler для удаления смены
        admin_delete_shift_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(admin_delete_shift_select_store, pattern="^admin_del_shift$")],
            states={
                ADMIN_DELETE_SHIFT_SELECT_STORE: [CallbackQueryHandler(admin_delete_shift_select_employee, pattern="^del_store_")],
                ADMIN_DELETE_SHIFT_SELECT_EMPLOYEE: [CallbackQueryHandler(admin_delete_shift_select_date, pattern="^del_emp_")],
                ADMIN_DELETE_SHIFT_SELECT_DATE: [CallbackQueryHandler(admin_delete_shift_confirm, pattern="^del_date_")],
            },
            fallbacks=[CommandHandler('cancel', cancel_registration),
                       CallbackQueryHandler(cancel_admin_action, pattern="^cancel_admin_action$")],
            name="admin_delete_shift_conversation",
            persistent=False,
            allow_reentry=True
        )
        app.add_handler(admin_delete_shift_handler)
        
        # НОВЫЙ ConversationHandler для произвольного периода подтверждения
        confirm_period_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(confirm_shifts_period, pattern="^confirm_custom$")],
            states={
                ADMIN_CONFIRM_PERIOD_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_period_start)],
                ADMIN_CONFIRM_PERIOD_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_period_end)],
            },
            fallbacks=[CommandHandler('cancel', cancel_registration)],
            name="confirm_period_conversation",
            persistent=False,
            allow_reentry=True
        )
        app.add_handler(confirm_period_handler)
        
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
        
        # Обычные обработчики команд
        app.add_handler(CommandHandler("checkin", checkin))
        app.add_handler(CommandHandler("checkout", checkout))
        app.add_handler(CommandHandler("timesheet", timesheet))
        app.add_handler(CommandHandler("stats", stats))
        app.add_handler(CommandHandler("admin", admin_panel))
        app.add_handler(CommandHandler("cancel", cancel_registration))
        
        # Обработчик текстовых сообщений
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        # Обработчик callback-запросов
        app.add_handler(CallbackQueryHandler(button_callback))
        
        logger.info("🚀 Бот запускается...")
        
        await app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}", exc_info=True)
        raise

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен")
    except Exception as e:
        logger.error(f"💥 Фатальная ошибка: {e}", exc_info=True)
