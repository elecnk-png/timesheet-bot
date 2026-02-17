import logging
import sqlite3
import pandas as pd
from datetime import datetime, date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes
)
import os
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Токен бота
BOT_TOKEN = os.getenv('BOT_TOKEN')

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(name)

# Состояния для разговоров
REGISTER_NAME, REGISTER_POSITION, SELECT_DATE, SELECT_STATUS = range(4)

# Инициализация базы данных
def init_database():
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Таблица сотрудников
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employees (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL,
            position TEXT NOT NULL,
            registration_date TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0
        )
    ''')
    
    # Таблица записей табеля
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS timesheet (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            check_in TEXT,
            check_out TEXT,
            hours_worked REAL,
            notes TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, date)
        )
    ''')
    
    # Таблица настроек
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    
    conn.commit()
    conn.close()

# Функции для работы с БД
def add_employee(user_id, full_name, position, is_admin=0):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute(
        'INSERT OR REPLACE INTO employees (user_id, full_name, position, registration_date, is_admin) VALUES (?, ?, ?, ?, ?)',
        (user_id, full_name, position, datetime.now().isoformat(), is_admin)
    )
    conn.commit()
    conn.close()

def get_employee(user_id):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM employees WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result

def add_timesheet_entry(user_id, date_str, status, notes='', check_in=None, check_out=None):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    hours_worked = None
    if check_in and check_out:
        try:
            start = datetime.strptime(check_in, '%H:%M')
            end = datetime.strptime(check_out, '%H:%M')
            hours_worked = (end - start).seconds / 3600
        except:
            pass
    
    cursor.execute('''
        INSERT OR REPLACE INTO timesheet 
        (user_id, date, status, check_in, check_out, hours_worked, notes, created_at) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, date_str, status, check_in, check_out, hours_worked, notes, datetime.now().isoformat()))
    
    conn.commit()
    conn.close()

def get_employee_timesheet(user_id, start_date=None, end_date=None):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    if start_date and end_date:
        cursor.execute('''
            SELECT * FROM timesheet 
            WHERE user_id = ? AND date BETWEEN ? AND ?
            ORDER BY date DESC
        ''', (user_id, start_date, end_date))
    else:
        cursor.execute('SELECT * FROM timesheet WHERE user_id = ? ORDER BY date DESC', (user_id,))
    
    result = cursor.fetchall()
    conn.close()
    return result

def get_all_employees():
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM employees')
    result = cursor.fetchall()
    conn.close()
    return result

def get_all_timesheet():
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT t.*, e.full_name, e.position 
        FROM timesheet t 
        JOIN employees e ON t.user_id = e.user_id 
        ORDER BY t.date DESC, e.full_name
    ''')
    result = cursor.fetchall()
    conn.close()
    return result

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    employee = get_employee(user_id)
    
    if employee:
        await update.message.reply_text(
            f"👋 С возвращением, {employee[1]}!\n\n"
            "Доступные команды:\n"
            "/checkin - Отметить начало рабочего дня\n"
            "/checkout - Отметить конец рабочего дня\n"
            "/timesheet - Мой табель\n"
            "/report - Отчет за период\n"
            "/stats - Моя статистика\n"
            "/help - Помощь"
        )
    else:
        keyboard = [[InlineKeyboardButton("Зарегистрироваться", callback_data="register")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Добро пожаловать! Для работы с ботом необходимо зарегистрироваться.",
            reply_markup=reply_markup
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
    📋 *Команды бота табеля:*
    
    /start - Начать работу с ботом
    /checkin - Отметить начало рабочего дня
    /checkout - Отметить конец рабочего дня
    /timesheet - Просмотреть свой табель
    /report - Сформировать отчет за период
    /stats - Показать статистику
    /help - Показать это сообщение
    
    *Для администраторов:*
    /admin - Панель администратора
    /export - Экспорт табеля в Excel
    /all_employees - Список всех сотрудников
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

# Регистрация
async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите ваше полное имя:")
    return REGISTER_NAME

async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['full_name'] = update.message.text
    await update.message.reply_text("Введите вашу должность:")
    return REGISTER_POSITION

async def register_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    full_name = context.user_data['full_name']
    position = update.message.text
    
    # Проверяем, является ли пользователь админом (первый пользователь становится админом)
    employees = get_all_employees()
    is_admin = 1 if len(employees) == 0 else 0
    
    add_employee(user_id, full_name, position, is_admin)
    
    await update.message.reply_text(
        f"✅ Регистрация завершена!\n\n"
        f"Имя: {full_name}\n"
        f"Должность: {position}\n\n"
        f"Теперь вы можете использовать команды бота."
    )
    return ConversationHandler.END

# Отметка начала рабочего дня
async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    employee = get_employee(user_id)
    
    if not employee:
        await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
    today = date.today().isoformat()
    current_time = datetime.now().strftime('%H:%M')
    
    add_timesheet_entry(
        user_id=user_id,
        date_str=today,
        status='working',
        check_in=current_time
    )
    
    await update.message.reply_text(f"✅ Начало рабочего дня отмечено в {current_time}")

# Отметка конца рабочего дня
async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):

user_id = update.effective_user.id
    employee = get_employee(user_id)
    
    if not employee:
        await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
    today = date.today().isoformat()
    current_time = datetime.now().strftime('%H:%M')
    
    # Получаем запись за сегодня
    entries = get_employee_timesheet(user_id, today, today)
    
    if entries:
        add_timesheet_entry(
            user_id=user_id,
            date_str=today,
            status='completed',
            check_in=entries[0][4],
            check_out=current_time
        )
        await update.message.reply_text(f"✅ Конец рабочего дня отмечен в {current_time}")
    else:
        await update.message.reply_text("❌ Сначала отметьте начало рабочего дня через /checkin")

# Просмотр табеля
async def view_timesheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    employee = get_employee(user_id)
    
    if not employee:
        await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
    # Получаем записи за последние 30 дней
    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=30)).isoformat()
    
    entries = get_employee_timesheet(user_id, start_date, end_date)
    
    if not entries:
        await update.message.reply_text("📊 За последние 30 дней записей нет")
        return
    
    message = f"📋 *Табель сотрудника {employee[1]}*\n\n"
    for entry in entries:
        entry_date = datetime.strptime(entry[2], '%Y-%m-%d').strftime('%d.%m.%Y')
        status = entry[3]
        
        if status == 'working':
            status_emoji = "⏳"
        elif status == 'completed':
            status_emoji = "✅"
        elif status == 'absent':
            status_emoji = "❌"
        elif status == 'vacation':
            status_emoji = "🏖"
        elif status == 'sick':
            status_emoji = "🤒"
        else:
            status_emoji = "📝"
        
        message += f"{entry_date} {status_emoji} {status.capitalize()}\n"
        
        if entry[4]:  # check_in
            message += f"   Начало: {entry[4]}\n"
        if entry[5]:  # check_out
            message += f"   Конец: {entry[5]}\n"
        if entry[6]:  # hours_worked
            message += f"   Часов: {entry[6]:.1f}\n"
        if entry[7]:  # notes
            message += f"   Примечание: {entry[7]}\n"
        
        message += "\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

# Статистика
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    employee = get_employee(user_id)
    
    if not employee:
        await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
    # Получаем записи за последние 30 дней
    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=30)).isoformat()
    
    entries = get_employee_timesheet(user_id, start_date, end_date)
    
    if not entries:
        await update.message.reply_text("📊 За последние 30 дней записей нет")
        return
    
    total_hours = 0
    working_days = 0
    completed_days = 0
    
    for entry in entries:
        if entry[6]:  # hours_worked
            total_hours += entry[6]
        if entry[3] == 'working':
            working_days += 1
        elif entry[3] == 'completed':
            completed_days += 1
            if entry[6]:
                total_hours += entry[6]
    
    avg_hours = total_hours / max(completed_days, 1)
    
    message = f"""
📊 *Статистика за 30 дней*

👤 Сотрудник: {employee[1]}
📅 Отработанных дней: {completed_days}
⏳ Текущих дней: {working_days}
⏱ Всего часов: {total_hours:.1f}
📈 Среднее часов в день: {avg_hours:.1f}
    """
    
    await update.message.reply_text(message, parse_mode='Markdown')

# Команды администратора
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    employee = get_employee(user_id)
    
    if not employee or employee[5] != 1:  # Проверка на админа
        await update.message.reply_text("❌ У вас нет прав администратора")
        return
    
    keyboard = [
        [InlineKeyboardButton("👥 Все сотрудники", callback_data="admin_employees")],
        [InlineKeyboardButton("📊 Общий табель", callback_data="admin_timesheet")],
        [InlineKeyboardButton("📈 Отчет за месяц", callback_data="admin_monthly")],
        [InlineKeyboardButton("📥 Экспорт в Excel", callback_data="admin_export")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="admin_settings")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🔐 *Панель администратора*\nВыберите действие:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def admin_employees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    employees = get_all_employees()
    
    if not employees:
        await query.edit_message_text("❌ Нет зарегистрированных сотрудников")
        return
    
    message = "👥 *Список сотрудников*\n\n"
    for emp in employees:
        message += f"• {emp[1]} ({emp[2]})\n"
        if emp[5] == 1:
            message += "  👑 Администратор\n"
        message += f"  ID: {emp[0]}\n\n"
    
    await query.edit_message_text(message, parse_mode='Markdown')

async def admin_timesheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    entries = get_all_timesheet()
    
    if not entries:
        await query.edit_message_text("❌ Нет записей в табеле")
        return
    
    message = "📊 *Общий табель*\n\n"
    for entry in entries[-20:]:  # Последние 20 записей
        entry_date = datetime.strptime(entry[2], '%Y-%m-%d').strftime('%d.%m.%Y')
        message += f"• {entry[10]} ({entry[11]}) - {entry_date}: {entry[3]}\n"
        if entry[6]:
            message += f"  Часов: {entry[6]:.1f}\n"
    
    await query.edit_message_text(message, parse_mode='Markdown')

async def export_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    employee = get_employee(user_id)
    
    if not employee or employee[5] != 1:
        await update.message.reply_text("❌ У вас нет прав администратора")
        return
    
    entries = get_all_timesheet()
    
    if not entries:
        await update.message.reply_text("❌ Нет данных для экспорта")
        return
    
    # Создаем DataFrame
    data = []
    for entry in entries:
        data.append({
            'Дата': entry[2],
            'Сотрудник': entry[10],
            'Должность': entry[11],
            'Статус': entry[3],
            'Начало': entry[4],
            'Конец': entry[5],
            'Часов': entry[6],
            'Примечание': entry[7]
        })
    
    df = pd.DataFrame(data)
    
    # Сохраняем в Excel
    filename = f"timesheet_{date.today().isoformat()}.xlsx"
    df.to_excel(filename, index=False)
    
    # Отправляем файл
    with open(filename, 'rb') as file:
        await update.message.reply_document(
            document=file,
            filename=filename,
            caption=f"📊 Экспорт табеля от {date.today().strftime('%d.%m.%Y')}"
        )
    
    # Удаляем временный файл
    os.remove(filename)

# Обработка неизвестных команд
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Неизвестная команда. Используйте /help для списка команд.")

# Отмена разговора
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Действие отменено.")
    return ConversationHandler.END

def main():
    # Инициализация базы данных
    init_database()
    
    # Создание приложения
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Обработчик регистрации
    register_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(register_start, pattern='^register$')],
        states={
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            REGISTER_POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_position)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # Регистрация обработчиков команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("checkin", checkin))
    application.add_handler(CommandHandler("checkout", checkout))
    application.add_handler(CommandHandler("timesheet", view_timesheet))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("export", export_excel))
    
    # Регистрация обработчиков CallbackQuery
    application.add_handler(CallbackQueryHandler(admin_employees, pattern='^admin_employees$'))
    application.add_handler(CallbackQueryHandler(admin_timesheet, pattern='^admin_timesheet$'))
    
    # Регистрация обработчика неизвестных команд
    application.add_handler(MessageHandler(filters.COMMAND, unknown))
    
    # Регистрация ConversationHandler
    application.add_handler(register_conv)
    
    # Запуск бота
    print("Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if name == 'main':
    main()
