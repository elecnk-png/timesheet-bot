import logging
import sqlite3
from datetime import datetime, date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters, ContextTypes
import os
from dotenv import load_dotenv

# Загрузка токена
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(_name_)

# Состояния для регистрации
REGISTER_NAME, REGISTER_POSITION = range(2)

# Инициализация базы данных
def init_database():
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS employees 
                      (user_id INTEGER PRIMARY KEY, full_name TEXT, position TEXT, reg_date TEXT, is_admin INTEGER DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS timesheet 
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date TEXT, status TEXT, 
                       check_in TEXT, check_out TEXT, hours REAL, notes TEXT)''')
    conn.commit()
    conn.close()

# Функции БД
def add_employee(user_id, name, position):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM employees')
    count = cursor.fetchone()[0]
    is_admin = 1 if count == 0 else 0
    cursor.execute('INSERT OR REPLACE INTO employees VALUES (?, ?, ?, ?, ?)',
                  (user_id, name, position, datetime.now().isoformat(), is_admin))
    conn.commit()
    conn.close()
    return is_admin

def get_employee(user_id):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM employees WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result

def add_checkin(user_id):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    today = date.today().isoformat()
    now = datetime.now().strftime('%H:%M')
    cursor.execute('INSERT OR REPLACE INTO timesheet (user_id, date, status, check_in) VALUES (?, ?, ?, ?)',
                  (user_id, today, 'working', now))
    conn.commit()
    conn.close()
    return now

def add_checkout(user_id):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    today = date.today().isoformat()
    now = datetime.now().strftime('%H:%M')
    
    cursor.execute('SELECT * FROM timesheet WHERE user_id = ? AND date = ?', (user_id, today))
    entry = cursor.fetchone()
    
    if entry:
        check_in = entry[4]
        check_in_time = datetime.strptime(check_in, '%H:%M')
        check_out_time = datetime.strptime(now, '%H:%M')
        hours = (check_out_time - check_in_time).seconds / 3600
        
        cursor.execute('''UPDATE timesheet SET status = ?, check_out = ?, hours = ? 
                          WHERE user_id = ? AND date = ?''',
                      ('completed', now, hours, user_id, today))
        conn.commit()
        conn.close()
        return now, hours
    conn.close()
    return None, None

def get_timesheet(user_id, days=7):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    start_date = (date.today() - timedelta(days=days)).isoformat()
    cursor.execute('''SELECT * FROM timesheet WHERE user_id = ? AND date >= ? 
                      ORDER BY date DESC''', (user_id, start_date))
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

def get_all_timesheet(limit=20):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''SELECT e.full_name, e.position, t.date, t.status, t.hours 
                      FROM timesheet t JOIN employees e ON t.user_id = e.user_id
                      ORDER BY t.date DESC LIMIT ?''', (limit,))
    result = cursor.fetchall()
    conn.close()
    return result

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    employee = get_employee(user_id)
    
    if employee:
        admin_star = " 👑" if employee[4] == 1 else ""
        await update.message.reply_text(
            f"👋 С возвращением, {employee[1]}{admin_star}!\n\n"
            "📋 Доступные команды:\n"
            "/checkin - Начать рабочий день\n"
            "/checkout - Закончить рабочий день\n"
            "/timesheet - Мой табель\n"
            "/stats - Моя статистика\n"
            "/help - Помощь"
        )
    else:
        keyboard = [[InlineKeyboardButton("📝 Зарегистрироваться", callback_data="register")]]
        await update.message.reply_text(
            "Добро пожаловать! Для работы необходимо зарегистрироваться.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите ваше полное имя:")
    return REGISTER_NAME

async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text
    await update.message.reply_text("Введите вашу должность:")
    return REGISTER_POSITION

async def register_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = context.user_data['name']
    position = update.message.text
    
    is_admin = add_employee(user_id, name, position)
    
    admin_text = "\n\n👑 Вы первый пользователь, поэтому вы назначены администратором!" if is_admin else ""
    
    await update.message.reply_text(
        f"✅ Регистрация завершена!\n"
        f"Имя: {name}\n"
        f"Должность: {position}{admin_text}"
    )
    return ConversationHandler.END

async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not get_employee(user_id):
        await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
    time = add_checkin(user_id)
    await update.message.reply_text(f"✅ Начало рабочего дня отмечено в {time}")

async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not get_employee(user_id):
        await update.message.reply_text("❌ Сначала зарегистрируйтесь через /start")
        return
    
    time, hours = add_checkout(user_id)
    if time:
        await update.message.reply_text(f"✅ Конец рабочего дня отмечен в {time}\n⏱ Отработано часов: {hours:.1f}")
    else:
        await update.message.reply_text("❌ Сначала отметьте начало дня через /checkin")

async def timesheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    employee = get_employee(user_id)
    if not employee:
        await update.message.reply_text("❌ Сначала зарегистрируйтесь")
        return
    
    entries = get_timesheet(user_id)
    if not entries:
        await update.message.reply_text("📊 За последние 7 дней записей нет")
        return
    
    msg = f"📋 *Табель {employee[1]}*\n\n"
    for e in entries:
        date_obj = datetime.strptime(e[2], '%Y-%m-%d').strftime('%d.%m.%Y')
        status = "✅" if e[3] == 'completed' else "⏳"
        hours = f"({e[6]:.1f}ч)" if e[6] else ""
        msg += f"{date_obj} {status} {e[4]}-{e[5] or '...'} {hours}\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    employee = get_employee(user_id)
    if not employee:
        await update.message.reply_text("❌ Сначала зарегистрируйтесь")
        return
    
    entries = get_timesheet(user_id, 30)
    total_hours = sum(e[6] for e in entries if e[6])
    days_worked = len([e for e in entries if e[3] == 'completed'])
    avg_hours = total_hours / days_worked if days_worked > 0 else 0
    
    msg = f"""
📊 *Статистика за 30 дней*

👤 {employee[1]}
📅 Отработано дней: {days_worked}
⏱ Всего часов: {total_hours:.1f}
📈 Среднее часов: {avg_hours:.1f}
    """
    await update.message.reply_text(msg, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📋 *Команды бота:*

/start - Начать работу
/checkin - Начать рабочий день
/checkout - Закончить рабочий день
/timesheet - Мой табель
/stats - Статистика
/help - Помощь

👑 *Администратору:*
/admin - Панель управления
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    employee = get_employee(user_id)
    if not employee or employee[4] != 1:
        await update.message.reply_text("❌ Только для администраторов")
        return
    
    keyboard = [
        [InlineKeyboardButton("👥 Все сотрудники", callback_data="admin_list")],
        [InlineKeyboardButton("📊 Общий табель", callback_data="admin_all")]
    ]
    await update.message.reply_text(
        "🔐 *Панель администратора*", 
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    employees = get_all_employees()
    
    msg = "👥 *Сотрудники:*\n\n"
    for e in employees:
        admin = "👑 " if e[4] == 1 else ""
        msg += f"{admin}{e[1]} - {e[2]}\n"
    
    await query.edit_message_text(msg, parse_mode='Markdown')

async def admin_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    entries = get_all_timesheet()
    
    msg = "📊 *Последние записи:*\n\n"
    for e in entries:
        msg += f"• {e[0]} ({e[1]}) - {e[2]}: {e[3]}"
        if e[4]:
            msg += f" {e[4]:.1f}ч"
        msg += "\n"
    
    await query.edit_message_text(msg, parse_mode='Markdown')

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено")
    return ConversationHandler.END

def main():
    # Инициализация базы данных
    init_database()
    
    # Создание приложения
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Регистрация
    reg_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(register_start, pattern='^register$')],
        states={
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            REGISTER_POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_position)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("checkin", checkin))
    app.add_handler(CommandHandler("checkout", checkout))
    app.add_handler(CommandHandler("timesheet", timesheet))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(reg_conv)
    app.add_handler(CallbackQueryHandler(admin_list, pattern='^admin_list$'))
    app.add_handler(CallbackQueryHandler(admin_all, pattern='^admin_all$'))
    
    print("✅ Бот успешно запущен!")
    app.run_polling()

if name == 'main':
    main()
