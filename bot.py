import os
import logging
import sqlite3
import csv
import io
import asyncio
import sys
import random
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

# Состояния для ConversationHandler
(
    SELECT_POSITION, SELECT_STORE, ENTER_FULL_NAME, CREATE_POSITION_NAME,
    CREATE_STORE_NAME, CREATE_STORE_ADDRESS, CUSTOM_PERIOD_START,
    CUSTOM_PERIOD_END, DELETE_EMPLOYEE_REQUEST, DELETE_STORE_REQUEST,
    ASSIGN_SUPER_ADMIN_SELECT, ADD_SHIFT_SELECT_STORE, ADD_SHIFT_SELECT_EMPLOYEE,
    ADD_SHIFT_SELECT_DATE, ADD_SHIFT_ENTER_HOURS, DELETE_SHIFT_SELECT_STORE,
    DELETE_SHIFT_SELECT_EMPLOYEE, DELETE_SHIFT_SELECT_DATE,
    ADD_EMPLOYEE_START, ADD_EMPLOYEE_NAME, ADD_EMPLOYEE_POSITION, ADD_EMPLOYEE_STORE
) = range(22)

# Константы
MAX_MESSAGE_LENGTH = 4000
MIN_WORK_HOURS = 2
MAX_WORK_HOURS = 12
WORK_START_HOUR = 7

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
            is_super_admin INTEGER DEFAULT 0,
            can_request_admin INTEGER DEFAULT 0
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
            created_by_admin INTEGER DEFAULT 0,
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

def get_employees_by_store(store_name: str = None) -> List[Tuple]:
    """Получить список сотрудников по магазину"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    if store_name:
        cursor.execute('''
            SELECT user_id, full_name, position 
            FROM employees 
            WHERE store = ?
            ORDER BY full_name
        ''', (store_name,))
    else:
        cursor.execute('''
            SELECT user_id, full_name, position, store 
            FROM employees 
            ORDER BY store, full_name
        ''')
    
    result = cursor.fetchall()
    conn.close()
    return result

def get_shifts_by_date(user_id: int, date_str: str) -> List[Tuple]:
    """Получить смены сотрудника за указанную дату"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, check_in, check_out, hours, confirmed, status
        FROM timesheet 
        WHERE user_id = ? AND date = ?
        ORDER BY check_in
    ''', (user_id, date_str))
    
    result = cursor.fetchall()
    conn.close()
    return result

def delete_shift(shift_id: int) -> bool:
    """Удалить смену по ID"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM timesheet WHERE id = ?", (shift_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

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
    """Создать клавиатуру для администратора - только основные функции"""
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

def validate_full_name(name: str) -> Tuple[bool, str]:
    """Проверка ФИО - должно быть минимум 3 слова"""
    parts = name.strip().split()
    if len(parts) < 3:
        return False, "❌ ФИО должно содержать минимум 3 слова (Имя Отчество Фамилия)"
    if any(len(part) < 2 for part in parts):
        return False, "❌ Каждая часть ФИО должна содержать минимум 2 символа"
    return True, ""

async def enter_full_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение ФИО от пользователя с проверкой"""
    user_id = update.effective_user.id
    full_name = update.message.text.strip()
    
    logger.info(f"🔥🔥🔥🔥🔥 enter_full_name ВЫЗВАНА для пользователя {user_id}")
    logger.info(f"🔥 Введенное имя: '{full_name}'")
    
    # Проверяем ФИО
    is_valid, error_message = validate_full_name(full_name)
    if not is_valid:
        await update.message.reply_text(
            f"{error_message}\n\n"
            f"Пожалуйста, введите полное ФИО (Имя Отчество Фамилия):\n"
            f"Например: Иванов Иван Иванович"
        )
        return ENTER_FULL_NAME
    
    # Сохраняем имя
    context.user_data['full_name'] = full_name
    logger.info(f"✅ Имя сохранено в user_data: {context.user_data['full_name']}")
    
    # Показываем должности
    positions = get_positions()
    logger.info(f"Получен список должностей: {positions}")
    
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
    
    logger.info(f"✅ Отправлено меню выбора должности")
    logger.info(f"✅ Возвращаем состояние SELECT_POSITION = {SELECT_POSITION}")
    
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
            # Запрашиваем имя перед регистрацией
            logger.info("Запрашиваем имя пользователя")
            await update.message.reply_text(
                "📝 Для регистрации введите ваше полное ФИО (Имя Отчество Фамилия):\n"
                "Например: Иванов Иван Иванович"
            )
            logger.info(f"🔥 start возвращает ENTER_FULL_NAME = {ENTER_FULL_NAME}")
            return ENTER_FULL_NAME

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

async def show_open_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все открытые смены"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user or not (user[3] or user[4]):
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

@require_auth(admin_only=True)
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Панель администратора"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    is_super_admin = user[4] if user else 0
    
    keyboard = [
        [InlineKeyboardButton("✅ Открыть смену", callback_data="admin_checkin")],
        [InlineKeyboardButton("✅ Закрыть смену", callback_data="admin_checkout")],
        [InlineKeyboardButton("📊 Мой табель", callback_data="admin_timesheet")],
        [InlineKeyboardButton("📈 Моя статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Управление сотрудниками", callback_data="admin_employees_menu")],
        [InlineKeyboardButton("📊 По магазинам", callback_data="admin_by_store")],
        [InlineKeyboardButton("🔓 Открытые смены", callback_data="admin_open_shifts")],
        [InlineKeyboardButton("📅 Выбрать период", callback_data="period_selection")],
        [InlineKeyboardButton("📈 Статистика по магазинам", callback_data="admin_store_stats")],
        [InlineKeyboardButton("✅ Подтверждение смен", callback_data="admin_confirm")],
        [InlineKeyboardButton("🗑 Запросить удаление", callback_data="admin_delete_menu")],
        [InlineKeyboardButton("📋 Управление должностями", callback_data="admin_positions_menu")],
        [InlineKeyboardButton("🏪 Управление магазинами", callback_data="admin_stores_menu")],
        [InlineKeyboardButton("🔄 Управление сменами", callback_data="admin_shifts_menu")],
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

async def show_shifts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать меню управления сменами"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("➕ Добавить смену", callback_data="add_shift_start")],
        [InlineKeyboardButton("🗑 Удалить смену", callback_data="delete_shift_start")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🔄 УПРАВЛЕНИЕ СМЕНАМИ\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )

async def show_employees_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать меню управления сотрудниками"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("➕ Добавить сотрудника", callback_data="add_employee_start")],
        [InlineKeyboardButton("👥 Список сотрудников", callback_data="admin_list")],
        [InlineKeyboardButton("📊 По магазинам", callback_data="admin_by_store")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "👥 УПРАВЛЕНИЕ СОТРУДНИКАМИ\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )

async def add_employee_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления сотрудника"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "📝 Введите ФИО нового сотрудника (Имя Отчество Фамилия):\n"
        "Например: Иванов Иван Иванович"
    )
    return ADD_EMPLOYEE_NAME

async def add_employee_enter_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод ФИО нового сотрудника"""
    full_name = update.message.text.strip()
    
    # Проверяем ФИО
    is_valid, error_message = validate_full_name(full_name)
    if not is_valid:
        await update.message.reply_text(
            f"{error_message}\n\n"
            f"Пожалуйста, введите полное ФИО (Имя Отчество Фамилия):\n"
            f"Например: Иванов Иван Иванович"
        )
        return ADD_EMPLOYEE_NAME
    
    # Сохраняем имя
    context.user_data['add_employee_name'] = full_name
    
    # Показываем должности
    positions = get_positions()
    
    if not positions:
        await update.message.reply_text(
            "❌ В системе нет должностей. Сначала создайте должность в управлении должностями."
        )
        return ConversationHandler.END
    
    # Создаем клавиатуру с должностями
    keyboard = []
    for pos in positions:
        keyboard.append([InlineKeyboardButton(pos, callback_data=f"add_emp_pos_{pos}")])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="back_to_employee_management")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"👤 ФИО: {full_name}\n\n"
        f"📝 Выберите должность сотрудника:",
        reply_markup=reply_markup
    )
    
    return ADD_EMPLOYEE_POSITION

async def add_employee_select_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор должности для нового сотрудника"""
    query = update.callback_query
    await query.answer()
    
    position = query.data.replace("add_emp_pos_", "", 1)
    context.user_data['add_employee_position'] = position
    
    stores = get_stores()
    
    if not stores:
        await query.edit_message_text(
            "❌ В системе нет магазинов. Сначала создайте магазин в управлении магазинами."
        )
        return ConversationHandler.END
    
    keyboard = []
    for store_name, address in stores:
        keyboard.append([
            InlineKeyboardButton(f"{store_name}", callback_data=f"add_emp_store_{store_name}")
        ])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="back_to_employee_management")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"👤 ФИО: {context.user_data.get('add_employee_name')}\n"
        f"📋 Должность: {position}\n\n"
        f"🏪 Выберите магазин для сотрудника:",
        reply_markup=reply_markup
    )
    
    return ADD_EMPLOYEE_STORE

async def add_employee_select_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор магазина и сохранение нового сотрудника"""
    query = update.callback_query
    await query.answer()
    
    store = query.data.replace("add_emp_store_", "", 1)
    position = context.user_data.get('add_employee_position')
    full_name = context.user_data.get('add_employee_name')
    
    if not position or not full_name:
        await query.edit_message_text(
            "❌ Ошибка регистрации. Пожалуйста, начните заново."
        )
        return ConversationHandler.END
    
    # Генерируем случайный user_id для сотрудника без телеграм
    temp_user_id = -random.randint(10000, 99999)
    
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    try:
        # Проверяем, не существует ли уже такой user_id
        while True:
            cursor.execute("SELECT user_id FROM employees WHERE user_id = ?", (temp_user_id,))
            if not cursor.fetchone():
                break
            temp_user_id = -random.randint(10000, 99999)
        
        # Определяем, может ли сотрудник запрашивать админку
        can_request_admin = 1 if position.lower() == "директор магазина" else 0
        
        # Регистрируем нового сотрудника
        cursor.execute('''
            INSERT INTO employees (user_id, full_name, position, store, reg_date, is_admin, is_super_admin, can_request_admin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (temp_user_id, full_name, position, store, get_today_date_utc8(), 0, 0, can_request_admin))
        
        conn.commit()
        
        await query.edit_message_text(
            f"✅ Сотрудник успешно добавлен!\n\n"
            f"👤 {full_name}\n"
            f"📋 Должность: {position}\n"
            f"🏪 Магазин: {store}\n"
            f"🆔 Внутренний ID: {temp_user_id}\n\n"
            f"Теперь вы можете управлять сменами этого сотрудника через меню управления сменами."
        )
        
    except Exception as e:
        logger.error(f"ОШИБКА при добавлении сотрудника: {e}")
        await query.edit_message_text(
            "❌ Произошла ошибка при добавлении сотрудника. Попробуйте позже."
        )
    finally:
        conn.close()
    
    # Очищаем временные данные
    context.user_data.pop('add_employee_name', None)
    context.user_data.pop('add_employee_position', None)
    
    keyboard = [[InlineKeyboardButton("◀️ Назад в управление сотрудниками", 
                                     callback_data="admin_employees_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)
    
    return ConversationHandler.END

async def back_to_employee_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат в меню управления сотрудниками"""
    query = update.callback_query
    await query.answer()
    
    await show_employees_management_menu(update, context)
    return ConversationHandler.END

async def add_shift_select_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор магазина для добавления смены"""
    query = update.callback_query
    await query.answer()
    
    stores = get_stores()
    if not stores:
        await query.edit_message_text("❌ Нет созданных магазинов")
        return ConversationHandler.END
    
    keyboard = []
    for store_name, address in stores:
        keyboard.append([InlineKeyboardButton(f"🏪 {store_name}", callback_data=f"add_shift_store_{store_name}")])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_shifts_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🏪 ВЫБОР МАГАЗИНА\n\n"
        "Выберите магазин для добавления смены:",
        reply_markup=reply_markup
    )
    
    return ADD_SHIFT_SELECT_STORE

async def add_shift_select_employee_fixed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор сотрудника для добавления смены (исправленная версия)"""
    query = update.callback_query
    await query.answer()
    
    store_name = query.data.replace("add_shift_store_", "", 1)
    context.user_data['add_shift_store'] = store_name
    
    employees = get_employees_by_store(store_name)
    if not employees:
        await query.edit_message_text(f"❌ В магазине '{store_name}' нет сотрудников")
        return ConversationHandler.END
    
    keyboard = []
    for user_id, full_name, position in employees:
        keyboard.append([InlineKeyboardButton(f"👤 {full_name} - {position}", 
                                             callback_data=f"add_shift_emp_{user_id}")])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="add_shift_start")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"🏪 Магазин: {store_name}\n\n"
        f"👤 ВЫБОР СОТРУДНИКА\n\n"
        f"Выберите сотрудника:",
        reply_markup=reply_markup
    )
    
    return ADD_SHIFT_SELECT_EMPLOYEE

async def add_shift_select_date_fixed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор даты для добавления смены (исправленная версия)"""
    query = update.callback_query
    await query.answer()
    
    user_id = int(query.data.replace("add_shift_emp_", "", 1))
    context.user_data['add_shift_user_id'] = user_id
    
    # Получаем информацию о сотруднике
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute("SELECT full_name FROM employees WHERE user_id = ?", (user_id,))
    employee = cursor.fetchone()
    conn.close()
    
    if not employee:
        await query.edit_message_text("❌ Сотрудник не найден")
        return ConversationHandler.END
    
    context.user_data['add_shift_employee_name'] = employee[0]
    
    await query.edit_message_text(
        f"👤 Сотрудник: {employee[0]}\n"
        f"🏪 Магазин: {context.user_data['add_shift_store']}\n\n"
        f"📅 Введите дату смены в формате ГГГГ-ММ-ДД:\n"
        f"Например: 2024-01-31"
    )
    
    return ADD_SHIFT_SELECT_DATE

async def add_shift_enter_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод количества часов для смены"""
    date_str = update.message.text.strip()
    
    try:
        shift_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        context.user_data['add_shift_date'] = date_str
        
        await update.message.reply_text(
            f"📅 Дата смены: {date_str}\n\n"
            f"⏱ Введите количество часов (от {MIN_WORK_HOURS} до {MAX_WORK_HOURS}):\n"
            f"Учитывайте, что рабочий день начинается с {WORK_START_HOUR}:00"
        )
        return ADD_SHIFT_ENTER_HOURS
        
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД\n"
            "Например: 2024-01-31"
        )
        return ADD_SHIFT_SELECT_DATE

async def add_shift_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранение добавленной смены"""
    hours_text = update.message.text.strip()
    
    try:
        hours = float(hours_text.replace(',', '.'))
        
        if hours < MIN_WORK_HOURS or hours > MAX_WORK_HOURS:
            await update.message.reply_text(
                f"❌ Количество часов должно быть от {MIN_WORK_HOURS} до {MAX_WORK_HOURS}"
            )
            return ADD_SHIFT_ENTER_HOURS
        
        # Получаем данные из контекста
        user_id = context.user_data.get('add_shift_user_id')
        store = context.user_data.get('add_shift_store')
        employee_name = context.user_data.get('add_shift_employee_name')
        date_str = context.user_data.get('add_shift_date')
        
        if not all([user_id, store, employee_name, date_str]):
            await update.message.reply_text("❌ Ошибка: потеряны данные. Начните заново.")
            return ConversationHandler.END
        
        # Рассчитываем время начала (7:00) и окончания
        start_time = datetime.strptime(f"{date_str} {WORK_START_HOUR}:00", "%Y-%m-%d %H:%M")
        start_time = TIMEZONE.localize(start_time)
        end_time = start_time + timedelta(hours=hours)
        
        # Сохраняем смену
        conn = sqlite3.connect('timesheet.db')
        cursor = conn.cursor()
        
        # Проверяем, нет ли уже смены в этот день
        cursor.execute('''
            SELECT id FROM timesheet 
            WHERE user_id = ? AND date = ? AND status = 'completed'
        ''', (user_id, date_str))
        
        existing = cursor.fetchone()
        if existing:
            await update.message.reply_text(
                f"❌ У сотрудника уже есть завершенная смена в этот день"
            )
            conn.close()
            return ConversationHandler.END
        
        cursor.execute('''
            INSERT INTO timesheet 
            (user_id, date, status, check_in, check_out, hours, confirmed, created_by_admin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, date_str, 'completed', start_time.isoformat(), 
              end_time.isoformat(), hours, 0, 1))
        
        conn.commit()
        conn.close()
        
        await update.message.reply_text(
            f"✅ Смена успешно добавлена!\n\n"
            f"👤 Сотрудник: {employee_name}\n"
            f"🏪 Магазин: {store}\n"
            f"📅 Дата: {date_str}\n"
            f"⏱ Часов: {hours}\n"
            f"⏰ Начало: {WORK_START_HOUR}:00\n"
            f"⏰ Окончание: {WORK_START_HOUR + int(hours)}:{int((hours % 1) * 60):02d}\n\n"
            f"⚠️ Смена требует подтверждения администратором."
        )
        
        # Очищаем данные
        for key in ['add_shift_user_id', 'add_shift_store', 'add_shift_employee_name', 'add_shift_date']:
            context.user_data.pop(key, None)
        
        # Возвращаемся в меню
        keyboard = [[InlineKeyboardButton("◀️ Назад в управление сменами", callback_data="admin_shifts_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Выберите действие:",
            reply_markup=reply_markup
        )
        
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text(
            "❌ Введите корректное число часов (например: 8 или 7.5)"
        )
        return ADD_SHIFT_ENTER_HOURS

async def delete_shift_select_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор магазина для удаления смены"""
    query = update.callback_query
    await query.answer()
    
    stores = get_stores()
    if not stores:
        await query.edit_message_text("❌ Нет созданных магазинов")
        return ConversationHandler.END
    
    keyboard = []
    for store_name, address in stores:
        keyboard.append([InlineKeyboardButton(f"🏪 {store_name}", callback_data=f"delete_shift_store_{store_name}")])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_shifts_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🏪 ВЫБОР МАГАЗИНА\n\n"
        "Выберите магазин для удаления смены:",
        reply_markup=reply_markup
    )
    
    return DELETE_SHIFT_SELECT_STORE

async def delete_shift_select_employee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор сотрудника для удаления смены"""
    query = update.callback_query
    await query.answer()
    
    store_name = query.data.replace("delete_shift_store_", "", 1)
    context.user_data['delete_shift_store'] = store_name
    
    employees = get_employees_by_store(store_name)
    if not employees:
        await query.edit_message_text(f"❌ В магазине '{store_name}' нет сотрудников")
        return ConversationHandler.END
    
    keyboard = []
    for user_id, full_name, position in employees:
        keyboard.append([InlineKeyboardButton(f"👤 {full_name} - {position}", 
                                             callback_data=f"delete_shift_emp_{user_id}")])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="delete_shift_start")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"🏪 Магазин: {store_name}\n\n"
        f"👤 ВЫБОР СОТРУДНИКА\n\n"
        f"Выберите сотрудника:",
        reply_markup=reply_markup
    )
    
    return DELETE_SHIFT_SELECT_EMPLOYEE

async def delete_shift_select_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор даты для удаления смены"""
    query = update.callback_query
    await query.answer()
    
    user_id = int(query.data.replace("delete_shift_emp_", "", 1))
    context.user_data['delete_shift_user_id'] = user_id
    
    # Получаем информацию о сотруднике
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute("SELECT full_name FROM employees WHERE user_id = ?", (user_id,))
    employee = cursor.fetchone()
    conn.close()
    
    if not employee:
        await query.edit_message_text("❌ Сотрудник не найден")
        return ConversationHandler.END
    
    context.user_data['delete_shift_employee_name'] = employee[0]
    
    # Получаем список доступных дат для удаления
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT date, hours, confirmed, id
        FROM timesheet 
        WHERE user_id = ? AND status = 'completed'
        ORDER BY date DESC
        LIMIT 20
    ''', (user_id,))
    
    shifts = cursor.fetchall()
    conn.close()
    
    if not shifts:
        await query.edit_message_text(
            f"❌ У сотрудника {employee[0]} нет завершенных смен для удаления"
        )
        return ConversationHandler.END
    
    text = f"👤 Сотрудник: {employee[0]}\n"
    text += f"🏪 Магазин: {context.user_data['delete_shift_store']}\n\n"
    text += "📅 ДОСТУПНЫЕ СМЕНЫ ДЛЯ УДАЛЕНИЯ:\n\n"
    
    keyboard = []
    for date_str, hours, confirmed, shift_id in shifts:
        confirmed_mark = "✅" if confirmed else "❌"
        text += f"🆔 {shift_id} | 📅 {date_str} | ⏱ {hours} ч | {confirmed_mark}\n"
        keyboard.append([InlineKeyboardButton(f"🗑 Удалить смену от {date_str}", 
                                             callback_data=f"delete_shift_confirm_{shift_id}")])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="delete_shift_start")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
        await query.message.reply_text("Выберите смену для удаления:", reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)
    
    return DELETE_SHIFT_SELECT_DATE

async def delete_shift_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления смены"""
    query = update.callback_query
    await query.answer()
    
    shift_id = int(query.data.replace("delete_shift_confirm_", "", 1))
    
    # Получаем информацию о смене для подтверждения
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT t.date, t.hours, e.full_name, e.store
        FROM timesheet t
        JOIN employees e ON t.user_id = e.user_id
        WHERE t.id = ?
    ''', (shift_id,))
    
    shift = cursor.fetchone()
    conn.close()
    
    if not shift:
        await query.edit_message_text("❌ Смена не найдена")
        return ConversationHandler.END
    
    date_str, hours, full_name, store = shift
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"delete_shift_execute_{shift_id}"),
            InlineKeyboardButton("❌ Нет, отмена", callback_data="admin_shifts_menu")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"⚠️ ВЫ УВЕРЕНЫ?\n\n"
        f"Вы собираетесь удалить смену:\n"
        f"👤 Сотрудник: {full_name}\n"
        f"🏪 Магазин: {store}\n"
        f"📅 Дата: {date_str}\n"
        f"⏱ Часов: {hours}\n\n"
        f"Это действие нельзя отменить!",
        reply_markup=reply_markup
    )

async def delete_shift_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнение удаления смены"""
    query = update.callback_query
    await query.answer()
    
    shift_id = int(query.data.replace("delete_shift_execute_", "", 1))
    
    if delete_shift(shift_id):
        await query.edit_message_text(f"✅ Смена #{shift_id} успешно удалена!")
    else:
        await query.edit_message_text(f"❌ Не удалось удалить смену #{shift_id}")
    
    keyboard = [[InlineKeyboardButton("◀️ Назад в управление сменами", callback_data="admin_shifts_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)
    
    return ConversationHandler.END

async def show_unconfirmed_period_fixed(query, days):
    """Показать неподтвержденные смены за период (исправленная версия)"""
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
    
    # Создаем клавиатуру для подтверждения
    keyboard = []
    
    # Добавляем кнопку подтверждения всех за период
    keyboard.append([
        InlineKeyboardButton(f"✅ Подтвердить все за {days} дней", 
                           callback_data=f"confirm_all_period_{days}")
    ])
    
    # Добавляем кнопки для каждой смены
    for shift in unconfirmed[:10]:
        shift_id = shift[0]
        keyboard.append([
            InlineKeyboardButton(f"✅ Подтвердить #{shift_id}", 
                               callback_data=f"confirm_shift_{shift_id}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
        await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)

async def confirm_all_period(query, days):
    """Подтвердить все смены за период"""
    end_date = get_today_date_utc8()
    start_date = (datetime.now(TIMEZONE) - timedelta(days=days-1)).date().isoformat()
    
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE timesheet 
        SET confirmed = 1 
        WHERE date BETWEEN ? AND ? AND status = 'completed' AND confirmed = 0
    ''', (start_date, end_date))
    
    count = cursor.rowcount
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Подтверждено {count} смен за последние {days} дней")
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def handle_confirm_period_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора периода для неподтвержденных смен"""
    query = update.callback_query
    await query.answer()
    
    days = int(query.data.replace("confirm_period_", "", 1))
    await show_unconfirmed_period_fixed(query, days)

async def delete_position_fixed(query, position_name):
    """Удаление должности (исправленная версия)"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Очищаем название от возможных подчеркиваний в начале
    if position_name.startswith('_'):
        position_name = position_name[1:]
    
    logger.info(f"Удаление должности: '{position_name}'")
    
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
    
    # Проверяем, есть ли такая должность в таблице positions
    cursor.execute("SELECT COUNT(*) FROM positions WHERE name = ?", (position_name,))
    pos_count = cursor.fetchone()[0]
    
    if pos_count == 0:
        await query.edit_message_text(f"❌ Должность '{position_name}' не найдена в базе данных")
        conn.close()
        return
    
    # Удаляем должность
    cursor.execute("DELETE FROM positions WHERE name = ?", (position_name,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    
    if deleted:
        await query.edit_message_text(f"✅ Должность '{position_name}' успешно удалена!")
    else:
        await query.edit_message_text(f"❌ Не удалось удалить должность '{position_name}'")
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="admin_positions_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Основной обработчик callback-запросов
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на инлайн кнопки"""
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    user_id = query.from_user.id
    
    logger.info(f"Callback: {callback_data} от пользователя {user_id}")
    
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
    
    # Для всех остальных callback_data проверяем авторизацию
    if not user:
        await query.edit_message_text("❌ Вы не зарегистрированы. Используйте /start")
        return
    
    full_name, position, store, is_admin, is_super_admin, can_request_admin = user
    
    if callback_data == "close":
        await query.delete_message()
        return
    
    elif callback_data == "request_admin":
        await handle_admin_request(query, context, user_id, user)
    
    elif callback_data == "admin_checkin":
        logger.info(f"Выполняется admin_checkin для пользователя {user_id}")
        await checkin(update, context)
    
    elif callback_data == "admin_checkout":
        logger.info(f"Выполняется admin_checkout для пользователя {user_id}")
        await checkout(update, context)
    
    elif callback_data == "admin_timesheet":
        logger.info(f"Выполняется admin_timesheet для пользователя {user_id}")
        await timesheet(update, context)
    
    elif callback_data == "admin_stats":
        logger.info(f"Выполняется admin_stats для пользователя {user_id}")
        await stats(update, context)
    
    elif callback_data == "admin_open_shifts":
        logger.info(f"Выполняется admin_open_shifts для пользователя {user_id}")
        await show_open_shifts(update, context)
    
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
    
    elif callback_data == "admin_employees_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_employees_management_menu(update, context)
    
    elif callback_data == "add_employee_start":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await add_employee_start(update, context)
        return ADD_EMPLOYEE_NAME
    
    elif callback_data.startswith("add_emp_pos_"):
        if not (is_admin or is_super_admin):
            return
        await add_employee_select_position(update, context)
        return ADD_EMPLOYEE_STORE
    
    elif callback_data.startswith("add_emp_store_"):
        if not (is_admin or is_super_admin):
            return
        await add_employee_select_store(update, context)
        return ConversationHandler.END
    
    elif callback_data == "back_to_employee_management":
        await back_to_employee_management(update, context)
    
    # Обработка выбора периода
    elif callback_data == "period_selection":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_period_selection(query)
    
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
    
    # Обработка экспорта
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
    
    elif callback_data == "admin_shifts_menu":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await show_shifts_menu(update, context)
    
    elif callback_data == "add_shift_start":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await add_shift_select_store(update, context)
        return ADD_SHIFT_SELECT_STORE
    
    elif callback_data.startswith("add_shift_store_"):
        if not (is_admin or is_super_admin):
            return
        await add_shift_select_employee_fixed(update, context)
        return ADD_SHIFT_SELECT_EMPLOYEE
    
    elif callback_data.startswith("add_shift_emp_"):
        if not (is_admin or is_super_admin):
            return
        await add_shift_select_date_fixed(update, context)
        return ADD_SHIFT_SELECT_DATE
    
    elif callback_data == "delete_shift_start":
        if not (is_admin or is_super_admin):
            await query.edit_message_text("❌ Недостаточно прав")
            return
        await delete_shift_select_store(update, context)
        return DELETE_SHIFT_SELECT_STORE
    
    elif callback_data.startswith("delete_shift_store_"):
        if not (is_admin or is_super_admin):
            return
        await delete_shift_select_employee(update, context)
        return DELETE_SHIFT_SELECT_EMPLOYEE
    
    elif callback_data.startswith("delete_shift_emp_"):
        if not (is_admin or is_super_admin):
            return
        await delete_shift_select_date(update, context)
        return DELETE_SHIFT_SELECT_DATE
    
    elif callback_data.startswith("delete_shift_confirm_"):
        if not (is_admin or is_super_admin):
            return
        await delete_shift_confirm(update, context)
    
    elif callback_data.startswith("delete_shift_execute_"):
        if not (is_admin or is_super_admin):
            return
        await delete_shift_execute(update, context)
        return ConversationHandler.END
    
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
        logger.info(f"Получен запрос на удаление должности: {position_name}")
        await delete_position_fixed(query, position_name)
    
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
        if store_name.startswith('_'):
            store_name = store_name[1:]
        logger.info(f"Запрос на удаление магазина: {store_name}")
        await delete_store(query, store_name)
    
    elif callback_data == "confirm_today":
        if not (is_admin or is_super_admin):
            return
        await show_unconfirmed_today(query)
    
    elif callback_data == "confirm_period":
        if not (is_admin or is_super_admin):
            return
        await show_period_confirm_menu(query)
    
    elif callback_data.startswith("confirm_period_"):
        if not (is_admin or is_super_admin):
            return
        await handle_confirm_period_selection(update, context)
    
    elif callback_data.startswith("confirm_all_period_"):
        if not (is_admin or is_super_admin):
            return
        days = int(callback_data[18:])
        await confirm_all_period(query, days)
    
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
        target_id_str = callback_data[23:]
        logger.info(f"Получен ID сотрудника для удаления: {target_id_str}")
        
        try:
            target_id = int(target_id_str)
            logger.info(f"Успешное преобразование ID: {target_id}")
            await create_delete_request(query, user_id, full_name, "employee", str(target_id))
        except ValueError as e:
            logger.error(f"Ошибка преобразования ID: {target_id_str} - {e}")
            if target_id_str.startswith('_'):
                target_id_str = target_id_str[1:]
                try:
                    target_id = int(target_id_str)
                    logger.info(f"Успешное преобразование после удаления подчеркивания: {target_id}")
                    await create_delete_request(query, user_id, full_name, "employee", str(target_id))
                except ValueError as e2:
                    logger.error(f"Ошибка преобразования после удаления подчеркивания: {e2}")
                    await query.edit_message_text("❌ Ошибка в идентификаторе сотрудника")
            else:
                await query.edit_message_text("❌ Ошибка в идентификаторе сотрудника")
    
    elif callback_data.startswith("request_delete_store_"):
        if not (is_admin or is_super_admin):
            return
        store_name = callback_data[20:]
        logger.info(f"Получено название магазина для удаления: {store_name}")
        if store_name.startswith('_'):
            store_name = store_name[1:]
            logger.info(f"Название после удаления подчеркивания: {store_name}")
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

# Остальные вспомогательные функции
async def show_admin_panel(query):
    """Показать панель администратора"""
    keyboard = [
        [InlineKeyboardButton("✅ Открыть смену", callback_data="admin_checkin")],
        [InlineKeyboardButton("✅ Закрыть смену", callback_data="admin_checkout")],
        [InlineKeyboardButton("📊 Мой табель", callback_data="admin_timesheet")],
        [InlineKeyboardButton("📈 Моя статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Управление сотрудниками", callback_data="admin_employees_menu")],
        [InlineKeyboardButton("📊 По магазинам", callback_data="admin_by_store")],
        [InlineKeyboardButton("🔓 Открытые смены", callback_data="admin_open_shifts")],
        [InlineKeyboardButton("📅 Выбрать период", callback_data="period_selection")],
        [InlineKeyboardButton("📈 Статистика по магазинам", callback_data="admin_store_stats")],
        [InlineKeyboardButton("✅ Подтверждение смен", callback_data="admin_confirm")],
        [InlineKeyboardButton("🗑 Запросить удаление", callback_data="admin_delete_menu")],
        [InlineKeyboardButton("📋 Управление должностями", callback_data="admin_positions_menu")],
        [InlineKeyboardButton("🏪 Управление магазинами", callback_data="admin_stores_menu")],
        [InlineKeyboardButton("🔄 Управление сменами", callback_data="admin_shifts_menu")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="close")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🔐 ПАНЕЛЬ АДМИНИСТРАТОРА\n\nВыберите действие:",
        reply_markup=reply_markup
    )

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
    
    # Группируем по магазинам
    stores_dict = {}
    for emp in employees:
        store, user_id, full_name, position, is_admin, is_super_admin, can_request_admin, status, check_in, check_out = emp
        
        if store not in stores_dict:
            stores_dict[store] = []
        
        # Определяем статус смены
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
    
    # Разбиваем длинное сообщение
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

async def show_store_stats(query):
    """Показать статистику по магазинам с открытыми/закрытыми сменами"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Получаем список магазинов
    cursor.execute("SELECT name FROM stores")
    stores = cursor.fetchall()
    
    if not stores:
        await query.edit_message_text("❌ Нет созданных магазинов")
        return
    
    today = get_today_date_utc8()
    text = "📈 СТАТИСТИКА ПО МАГАЗИНАМ\n\n"
    
    for store in stores:
        store_name = store[0]
        
        # Количество сотрудников в магазине
        cursor.execute(
            "SELECT COUNT(*) FROM employees WHERE store = ?",
            (store_name,)
        )
        emp_count = cursor.fetchone()[0]
        
        # Количество открытых смен сегодня
        cursor.execute('''
            SELECT COUNT(*) 
            FROM timesheet t
            JOIN employees e ON t.user_id = e.user_id
            WHERE e.store = ? AND t.date = ? AND t.status = 'working'
        ''', (store_name, today))
        open_shifts = cursor.fetchone()[0]
        
        # Количество закрытых смен сегодня
        cursor.execute('''
            SELECT COUNT(*) 
            FROM timesheet t
            JOIN employees e ON t.user_id = e.user_id
            WHERE e.store = ? AND t.date = ? AND t.status = 'completed'
        ''', (store_name, today))
        closed_shifts = cursor.fetchone()[0]
        
        # Статистика за 30 дней
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
        text += f"   ⏱ Часов (30 дн): {total_hours:.2f}\n"
        text += f"   🔓 Открытых смен сегодня: {open_shifts}\n"
        text += f"   ✅ Закрытых смен сегодня: {closed_shifts}\n\n"
    
    conn.close()
    
    await query.edit_message_text(text)
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

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
        for i in range(0, len(text), MAX_MESSAGE_LENGTH):
            part = text[i:i+MAX_MESSAGE_LENGTH]
            if i == 0:
                await query.edit_message_text(part)
            else:
                await query.message.reply_text(part)
    else:
        await query.edit_message_text(text)
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

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
    
    keyboard = []
    for shift in unconfirmed:
        shift_id = shift[0]
        keyboard.append([
            InlineKeyboardButton(f"✅ Подтвердить смену #{shift_id}", 
                               callback_data=f"confirm_shift_{shift_id}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
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
        [InlineKeyboardButton("📅 90 дней", callback_data="confirm_period_90")],
        [InlineKeyboardButton("📅 Весь период", callback_data="confirm_period_36500")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "📅 ВЫБОР ПЕРИОДА\n\n"
        "Выберите период для просмотра неподтвержденных смен:",
        reply_markup=reply_markup
    )

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
    
    keyboard = [
        [InlineKeyboardButton(f"✅ Подтвердить все в {store}", 
                            callback_data=f"confirm_all_store_{store}")]
    ]
    
    for shift in unconfirmed[:10]:
        shift_id = shift[0]
        keyboard.append([
            InlineKeyboardButton(f"✅ Подтвердить #{shift_id}", 
                               callback_data=f"confirm_shift_{shift_id}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="confirm_by_store")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
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
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def show_confirm_stats(query):
    """Показать статистику подтверждений"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
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
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Функции для управления должностями
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
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="admin_positions_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def show_delete_position_menu(query):
    """Меню удаления должностей - показывает все должности"""
    positions = get_positions()
    
    if not positions:
        await query.edit_message_text("📋 Нет должностей для удаления")
        return
    
    # Получаем информацию о том, какие должности используются
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    text = "🗑 ВЫБОР ДОЛЖНОСТИ ДЛЯ УДАЛЕНИЯ\n\n"
    text += "❌ - нельзя удалить (используется)\n"
    text += "✅ - можно удалить\n\n"
    
    keyboard = []
    for pos in positions:
        cursor.execute("SELECT COUNT(*) FROM employees WHERE position = ?", (pos,))
        count = cursor.fetchone()[0]
        
        if count == 0:
            # Должность не используется - можно удалить
            text += f"✅ {pos}\n"
            callback_data = f"delete_position_{pos}"
            logger.info(f"Создаем callback_data для должности {pos}: {callback_data}")
            keyboard.append([
                InlineKeyboardButton(f"🗑 {pos}", callback_data=callback_data)
            ])
        else:
            # Должность используется - нельзя удалить
            text += f"❌ {pos} (используется {count} сотрудниками)\n"
    
    conn.close()
    
    if not keyboard:
        text += "\n❌ Нет должностей, которые можно удалить\n(все должности используются сотрудниками)"
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_positions_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
        await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)

async def create_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание новой должности"""
    user_id = update.effective_user.id
    position_name = update.message.text.strip()
    
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
    
    keyboard = [
        [InlineKeyboardButton("◀️ Назад в управление должностями", callback_data="admin_positions_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Выберите действие:",
        reply_markup=reply_markup
    )
    
    return ConversationHandler.END

# Функции для управления магазинами
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
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="admin_stores_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Выберите действие:",
        reply_markup=reply_markup
    )
    
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
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="admin_stores_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def show_delete_store_menu(query):
    """Меню удаления магазинов"""
    stores = get_stores()
    
    if not stores:
        await query.edit_message_text("🏪 Нет магазинов для удаления")
        return
    
    # Получаем информацию о том, какие магазины используются
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    text = "🗑 ВЫБОР МАГАЗИНА ДЛЯ УДАЛЕНИЯ\n\n"
    text += "❌ - нельзя удалить (есть сотрудники)\n"
    text += "✅ - можно удалить\n\n"
    
    keyboard = []
    for store_name, address in stores:
        cursor.execute("SELECT COUNT(*) FROM employees WHERE store = ?", (store_name,))
        count = cursor.fetchone()[0]
        
        if count == 0:
            # Магазин не используется - можно удалить
            text += f"✅ {store_name}\n"
            text += f"   📍 {address}\n\n"
            keyboard.append([
                InlineKeyboardButton(f"🗑 {store_name}", callback_data=f"delete_store_list_{store_name}")
            ])
        else:
            # Магазин используется - нельзя удалить
            text += f"❌ {store_name} (используется {count} сотрудниками)\n"
            text += f"   📍 {address}\n\n"
    
    conn.close()
    
    if not keyboard:
        text += "\n❌ Нет магазинов, которые можно удалить\n(во всех магазинах есть сотрудники)"
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_stores_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
        await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)

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
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="admin_stores_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Функции для меню удаления
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

async def show_delete_employee_menu(query):
    """Меню выбора сотрудника для удаления"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
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
    
    keyboard = []
    for user_id, full_name, position, store in employees:
        keyboard.append([
            InlineKeyboardButton(f"🗑 {full_name} ({store})", 
                               callback_data=f"request_delete_employee_{user_id}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_delete_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
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
    
    # Получаем информацию о том, какие магазины используются
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    text = "🏪 ВЫБОР МАГАЗИНА ДЛЯ ЗАПРОСА УДАЛЕНИЯ\n\n"
    text += "❌ - нельзя удалить (есть сотрудники)\n"
    text += "✅ - можно удалить\n\n"
    
    keyboard = []
    for store_name, address in stores:
        cursor.execute("SELECT COUNT(*) FROM employees WHERE store = ?", (store_name,))
        count = cursor.fetchone()[0]
        
        if count == 0:
            # Магазин не используется - можно запросить удаление
            text += f"✅ {store_name}\n"
            text += f"   📍 {address}\n\n"
            keyboard.append([
                InlineKeyboardButton(f"🗑 {store_name}", callback_data=f"request_delete_store_{store_name}")
            ])
        else:
            # Магазин используется - нельзя удалить
            text += f"❌ {store_name} (используется {count} сотрудниками)\n"
            text += f"   📍 {address}\n\n"
    
    conn.close()
    
    if not keyboard:
        text += "\n❌ Нет магазинов, для которых можно запросить удаление\n(во всех магазинах есть сотрудники)"
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_delete_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if len(text) > MAX_MESSAGE_LENGTH:
        await query.edit_message_text(text[:MAX_MESSAGE_LENGTH])
        remaining = text[MAX_MESSAGE_LENGTH:]
        while remaining:
            await query.message.reply_text(remaining[:MAX_MESSAGE_LENGTH])
            remaining = remaining[MAX_MESSAGE_LENGTH:]
        await query.message.reply_text("Выберите магазин:", reply_markup=reply_markup)
    else:
        await query.edit_message_text(text, reply_markup=reply_markup)

# Функции для запросов на удаление
async def create_delete_request(query, requester_id, requester_name, target_type, target_id):
    """Создание запроса на удаление"""
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
                f"Используйте 👑 Панель админа для рассмотрения запроса."
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
            pending_keyboard.append([
                InlineKeyboardButton(f"✅ Одобрить #{req_id}", callback_data=f"approve_request_{req_id}"),
                InlineKeyboardButton(f"❌ Отклонить #{req_id}", callback_data=f"reject_request_{req_id}")
            ])
            text += req_text
        else:
            other_text += req_text
    
    if other_text:
        text += "📋 ЗАВЕРШЕННЫЕ ЗАПРОСЫ:\n\n" + other_text
    
    keyboard = pending_keyboard
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
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
    
    if target_type == "employee":
        cursor.execute("SELECT is_super_admin FROM employees WHERE user_id = ?", (target_id,))
        is_super_admin = cursor.fetchone()
        if is_super_admin and is_super_admin[0] == 1:
            await query.edit_message_text("❌ Нельзя удалить супер-администратора")
            conn.close()
            return
        
        cursor.execute("DELETE FROM timesheet WHERE user_id = ?", (target_id,))
        cursor.execute("DELETE FROM employees WHERE user_id = ?", (target_id,))
        
    else:
        cursor.execute("SELECT COUNT(*) FROM employees WHERE store = ?", (target_name,))
        emp_count = cursor.fetchone()[0]
        
        if emp_count > 0:
            await query.edit_message_text(
                f"❌ Нельзя удалить магазин '{target_name}'\n"
                f"В нем работает {emp_count} сотрудников"
            )
            conn.close()
            return
        
        cursor.execute("DELETE FROM stores WHERE name = ?", (target_name,))
    
    cursor.execute('''
        UPDATE delete_requests 
        SET status = 'approved' 
        WHERE id = ?
    ''', (request_id,))
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Запрос #{request_id} одобрен, удаление выполнено")
    
    try:
        await query.message.bot.send_message(
            requester_id,
            f"✅ Ваш запрос на удаление {target_type} '{target_name}' одобрен и выполнен!"
        )
    except Exception as e:
        logger.error(f"Failed to notify requester {requester_id}: {e}")
    
    await show_delete_requests(query)

async def reject_delete_request(query, request_id):
    """Отклонить запрос на удаление"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
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
    
    cursor.execute('''
        UPDATE delete_requests 
        SET status = 'rejected' 
        WHERE id = ?
    ''', (request_id,))
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"❌ Запрос #{request_id} отклонен")
    
    try:
        await query.message.bot.send_message(
            requester_id,
            f"❌ Ваш запрос на удаление {target_type} '{target_name}' отклонен супер-администратором"
        )
    except Exception as e:
        logger.error(f"Failed to notify requester {requester_id}: {e}")
    
    await show_delete_requests(query)

# Функции для заявок на админа
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

async def show_admin_requests(query):
    """Показать все заявки на админа"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, request_date, user_name, user_position, user_store, user_id, status
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
        req_id, req_date, user_name, user_position, user_store, user_id, status = req
        
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
        req_text += f"👤 {user_name}\n"
        req_text += f"📋 Должность: {user_position}\n"
        req_text += f"🏪 Магазин: {user_store}\n"
        req_text += f"🆔 ID: {user_id}\n"
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
    
    keyboard = pending_keyboard
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
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
    
    cursor.execute('''
        SELECT user_id, user_name, user_position, user_store
        FROM admin_requests
        WHERE id = ? AND status = 'pending'
    ''', (request_id,))
    
    request = cursor.fetchone()
    
    if not request:
        await query.edit_message_text(f"❌ Заявка #{request_id} не найдена или уже обработана")
        conn.close()
        return
    
    user_id, user_name, user_position, user_store = request
    
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
        # Новый пользователь - создаем запись
        cursor.execute('''
            INSERT INTO employees 
            (user_id, full_name, position, store, reg_date, is_admin, is_super_admin, can_request_admin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, user_name, user_position, user_store, 
              get_today_date_utc8(), 1, 0, 0))
    
    cursor.execute('''
        UPDATE admin_requests 
        SET status = 'approved' 
        WHERE id = ?
    ''', (request_id,))
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Заявка #{request_id} одобрена, пользователь стал администратором")
    
    try:
        await query.message.bot.send_message(
            user_id,
            f"✅ Поздравляем! Ваша заявка на становление администратором одобрена!\n\n"
            f"Теперь вам доступна панель администратора."
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {e}")
    
    await show_admin_requests(query)

async def reject_admin_request(query, request_id):
    """Отклонить заявку на админа"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
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
    
    cursor.execute('''
        UPDATE admin_requests 
        SET status = 'rejected' 
        WHERE id = ?
    ''', (request_id,))
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"❌ Заявка #{request_id} отклонена")
    
    try:
        await query.message.bot.send_message(
            user_id,
            f"❌ К сожалению, ваша заявка на становление администратором отклонена."
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {e}")
    
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
    
    cursor.execute('''
        UPDATE employees 
        SET is_super_admin = 1 
        WHERE user_id = ?
    ''', (target_id,))
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(f"✅ Пользователь назначен супер-администратором!")
    
    try:
        await query.message.bot.send_message(
            target_id,
            f"⭐ Поздравляем! Вы назначены супер-администратором!\n\n"
            f"Теперь вам доступны все функции управления ботом."
        )
    except Exception as e:
        logger.error(f"Failed to notify new super admin {target_id}: {e}")
    
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
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="assign_super_admin_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Функции для добавления администраторов
async def show_add_admin_menu(query):
    """Меню добавления администратора"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
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
        
        days = (end_date - start_date).days + 1
        context.user_data['period_days'] = days
        
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

# Обработчик текстовых сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений"""
    text = update.message.text
    user_id = update.effective_user.id
    
    logger.info(f"Получено сообщение от {user_id}: {text}")
    
    # Проверяем, не находимся ли мы в каком-либо ConversationHandler
    if context.user_data.get('conversation_state'):
        logger.info(f"Пользователь в состоянии {context.user_data['conversation_state']}, пропускаем обработку")
        return
    
    # Обработка кнопок из нижнего меню
    if text == "🏠 Главное меню":
        await start(update, context)
        return
    elif text == "👑 Панель админа":
        # Проверяем, есть ли права администратора
        user = get_user(user_id)
        if user and (user[3] or user[4]):  # is_admin или is_super_admin
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
    elif text == "👥 Все сотрудники":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            query = type('Query', (), {
                'data': 'admin_list',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_all_employees(query)
        return
    elif text == "📊 По магазинам":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            query = type('Query', (), {
                'data': 'admin_by_store',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_employees_by_store(query)
        return
    elif text == "🔓 Открытые смены":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            await show_open_shifts(update, context)
        return
    elif text == "📅 Выбрать период":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            query = type('Query', (), {
                'data': 'period_selection',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_period_selection(query)
        return
    elif text == "📈 Статистика по магазинам":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            query = type('Query', (), {
                'data': 'admin_store_stats',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_store_stats(query)
        return
    elif text == "✅ Подтверждение смен":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            query = type('Query', (), {
                'data': 'admin_confirm',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_confirm_menu(query)
        return
    elif text == "🗑 Запросить удаление":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            query = type('Query', (), {
                'data': 'admin_delete_menu',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_delete_menu(query)
        return
    elif text == "📋 Управление должностями":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            query = type('Query', (), {
                'data': 'admin_positions_menu',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_positions_menu(query)
        return
    elif text == "🏪 Управление магазинами":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            query = type('Query', (), {
                'data': 'admin_stores_menu',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_stores_menu(query)
        return
    elif text == "🔄 Управление сменами":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            query = type('Query', (), {
                'data': 'admin_shifts_menu',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_shifts_menu(update, context)
        return
    elif text == "👥 Управление сотрудниками":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            query = type('Query', (), {
                'data': 'admin_employees_menu',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_employees_management_menu(update, context)
        return
    elif text == "➕ Добавить админа":
        user = get_user(user_id)
        if user and user[4]:  # is_super_admin
            query = type('Query', (), {
                'data': 'admin_add',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_add_admin_menu(query)
        return
    elif text == "📋 Запросы на удаление":
        user = get_user(user_id)
        if user and user[4]:  # is_super_admin
            query = type('Query', (), {
                'data': 'admin_requests',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_delete_requests(query)
        return
    elif text == "👑 Заявки в админы":
        user = get_user(user_id)
        if user and user[4]:  # is_super_admin
            query = type('Query', (), {
                'data': 'admin_admin_requests',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_admin_requests(query)
        return
    elif text == "⭐ Управление супер-админами":
        user = get_user(user_id)
        if user and user[4]:  # is_super_admin
            query = type('Query', (), {
                'data': 'assign_super_admin_menu',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_assign_super_admin_menu(query)
        return
    elif text == "👤 Запросить удаление сотрудника":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            query = type('Query', (), {
                'data': 'delete_employee_menu',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_delete_employee_menu(query)
        return
    elif text == "🏪 Запросить удаление магазина":
        user = get_user(user_id)
        if user and (user[3] or user[4]):
            query = type('Query', (), {
                'data': 'delete_store_menu',
                'from_user': update.effective_user,
                'message': update.message,
                'answer': lambda: None,
                'edit_message_text': lambda text, reply_markup=None: None
            })()
            await show_delete_store_request_menu(query)
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

async def handle_admin_request_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, user_info: Tuple):
    """Обработка заявки на становление администратором из сообщения"""
    full_name, position, store, is_admin, is_super_admin, can_request_admin = user_info
    
    # Проверяем, нет ли уже активной заявки
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
    
    # Создаем заявку
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
    
    # Уведомляем супер-админов
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

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущего действия"""
    await update.message.reply_text(
        "❌ Действие отменено"
    )
    return ConversationHandler.END

# Основная функция запуска
async def main():
    """Упрощенная функция запуска"""
    try:
        # Удаляем webhook
        await delete_webhook()
        await asyncio.sleep(1)
        
        # Инициализируем базу данных
        init_database()
        
        # Создаем приложение
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
        
        # ConversationHandler для добавления смен
        add_shift_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(button_callback, pattern="^add_shift_start$")],
            states={
                ADD_SHIFT_SELECT_STORE: [CallbackQueryHandler(button_callback, pattern="^add_shift_store_")],
                ADD_SHIFT_SELECT_EMPLOYEE: [CallbackQueryHandler(button_callback, pattern="^add_shift_emp_")],
                ADD_SHIFT_SELECT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_shift_enter_hours)],
                ADD_SHIFT_ENTER_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_shift_save)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True
        )
        app.add_handler(add_shift_conv)
        
        # ConversationHandler для удаления смен
        delete_shift_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(button_callback, pattern="^delete_shift_start$")],
            states={
                DELETE_SHIFT_SELECT_STORE: [CallbackQueryHandler(button_callback, pattern="^delete_shift_store_")],
                DELETE_SHIFT_SELECT_EMPLOYEE: [CallbackQueryHandler(button_callback, pattern="^delete_shift_emp_")],
                DELETE_SHIFT_SELECT_DATE: [CallbackQueryHandler(button_callback, pattern="^delete_shift_confirm_")],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True
        )
        app.add_handler(delete_shift_conv)
        
        # ConversationHandler для добавления сотрудников
        add_employee_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(button_callback, pattern="^add_employee_start$")],
            states={
                ADD_EMPLOYEE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_employee_enter_name)],
                ADD_EMPLOYEE_POSITION: [CallbackQueryHandler(button_callback, pattern="^add_emp_pos_")],
                ADD_EMPLOYEE_STORE: [CallbackQueryHandler(button_callback, pattern="^add_emp_store_")],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True
        )
        app.add_handler(add_employee_conv)
        
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
        
        # Запускаем polling
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
