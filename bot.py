import logging
import sqlite3
from datetime import datetime, date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters, ContextTypes
import os
import csv
import io
from dotenv import load_dotenv

# Загрузка токена
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния для регистрации
REGISTER_NAME, REGISTER_POSITION, REGISTER_STORE = range(3)

# Состояния для добавления администратора
ADD_ADMIN_ID, ADD_ADMIN_CONFIRM = range(3, 5)

# Состояния для подтверждения
CONFIRM_SELECT, CONFIRM_ACTION, CONFIRM_PERIOD = range(5, 8)

# Инициализация базы данных
def init_database():
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    # Таблица сотрудников
    cursor.execute('''CREATE TABLE IF NOT EXISTS employees 
                      (user_id INTEGER PRIMARY KEY, 
                       full_name TEXT, 
                       position TEXT, 
                       store TEXT,
                       reg_date TEXT, 
                       is_admin INTEGER DEFAULT 0)''')
    
    # Таблица записей табеля с добавленным полем confirmed
    cursor.execute('''CREATE TABLE IF NOT EXISTS timesheet 
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                       user_id INTEGER, 
                       date TEXT, 
                       status TEXT, 
                       check_in TEXT, 
                       check_out TEXT, 
                       hours REAL, 
                       notes TEXT,
                       confirmed INTEGER DEFAULT 0)''')  # Новое поле: 0 - не подтверждено, 1 - подтверждено
    
    conn.commit()
    conn.close()

# Функции БД для сотрудников
def add_employee(user_id, name, position, store):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM employees')
    count = cursor.fetchone()[0]
    is_admin = 1 if count == 0 else 0
    cursor.execute('INSERT OR REPLACE INTO employees VALUES (?, ?, ?, ?, ?, ?)',
                  (user_id, name, position, store, datetime.now().isoformat(), is_admin))
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

def get_all_employees():
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM employees ORDER BY store, full_name')
    result = cursor.fetchall()
    conn.close()
    return result

def get_employees_by_store(store):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM employees WHERE store = ? ORDER BY full_name', (store,))
    result = cursor.fetchall()
    conn.close()
    return result

def get_all_stores():
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT store FROM employees WHERE store IS NOT NULL')
    result = cursor.fetchall()
    conn.close()
    return [r[0] for r in result]

def add_admin(user_id):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE employees SET is_admin = 1 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def is_admin(user_id):
    emp = get_employee(user_id)
    return emp and emp[5] == 1

# Функции для табеля
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

def get_all_timesheet_by_period(start_date, end_date, store=None):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    if store:
        cursor.execute('''SELECT e.full_name, e.position, e.store, t.date, t.status, t.check_in, t.check_out, t.hours, t.notes
                          FROM timesheet t 
                          JOIN employees e ON t.user_id = e.user_id
                          WHERE t.date BETWEEN ? AND ? AND e.store = ?
                          ORDER BY e.store, e.full_name, t.date''', (start_date, end_date, store))
    else:
        cursor.execute('''SELECT e.full_name, e.position, e.store, t.date, t.status, t.check_in, t.check_out, t.hours, t.notes
                          FROM timesheet t 
                          JOIN employees e ON t.user_id = e.user_id
                          WHERE t.date BETWEEN ? AND ?
                          ORDER BY e.store, e.full_name, t.date''', (start_date, end_date))
    
    result = cursor.fetchall()
    conn.close()
    return result

# НОВЫЕ ФУНКЦИИ ДЛЯ ПОДТВЕРЖДЕНИЯ СМЕН
def get_unconfirmed_shifts(store=None):
    """Получить все неподтвержденные смены"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    today = date.today().isoformat()
    
    if store:
        cursor.execute('''SELECT t.id, e.full_name, e.position, e.store, t.date, t.check_in, t.check_out, t.hours, t.notes
                          FROM timesheet t 
                          JOIN employees e ON t.user_id = e.user_id
                          WHERE t.date = ? AND t.confirmed = 0 AND e.store = ?
                          ORDER BY e.full_name''', (today, store))
    else:
        cursor.execute('''SELECT t.id, e.full_name, e.position, e.store, t.date, t.check_in, t.check_out, t.hours, t.notes
                          FROM timesheet t 
                          JOIN employees e ON t.user_id = e.user_id
                          WHERE t.date = ? AND t.confirmed = 0
                          ORDER BY e.store, e.full_name''', (today,))
    
    result = cursor.fetchall()
    conn.close()
    return result

def get_unconfirmed_shifts_by_period(days=7, store=None):
    """Получить неподтвержденные смены за период"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    start_date = (date.today() - timedelta(days=days)).isoformat()
    end_date = date.today().isoformat()
    
    if store:
        cursor.execute('''SELECT t.id, e.full_name, e.position, e.store, t.date, t.check_in, t.check_out, t.hours, t.notes
                          FROM tim

esheet t 
                          JOIN employees e ON t.user_id = e.user_id
                          WHERE t.date BETWEEN ? AND ? AND t.confirmed = 0 AND e.store = ?
                          ORDER BY t.date DESC, e.full_name''', (start_date, end_date, store))
    else:
        cursor.execute('''SELECT t.id, e.full_name, e.position, e.store, t.date, t.check_in, t.check_out, t.hours, t.notes
                          FROM timesheet t 
                          JOIN employees e ON t.user_id = e.user_id
                          WHERE t.date BETWEEN ? AND ? AND t.confirmed = 0
                          ORDER BY t.date DESC, e.store, e.full_name''', (start_date, end_date))
    
    result = cursor.fetchall()
    conn.close()
    return result

def confirm_shift(shift_id):
    """Подтвердить конкретную смену"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE timesheet SET confirmed = 1 WHERE id = ?', (shift_id,))
    conn.commit()
    conn.close()

def confirm_all_shifts(store=None, date_str=None):
    """Подтвердить все смены за дату"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    if not date_str:
        date_str = date.today().isoformat()
    
    if store:
        cursor.execute('''UPDATE timesheet 
                          SET confirmed = 1 
                          WHERE date = ? AND user_id IN 
                          (SELECT user_id FROM employees WHERE store = ?)''', (date_str, store))
    else:
        cursor.execute('UPDATE timesheet SET confirmed = 1 WHERE date = ?', (date_str,))
    
    conn.commit()
    conn.close()

def get_shift_stats(store=None):
    """Получить статистику по подтверждениям"""
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    
    today = date.today().isoformat()
    
    if store:
        cursor.execute('''SELECT 
                          COUNT(CASE WHEN t.confirmed = 0 THEN 1 END) as unconfirmed,
                          COUNT(CASE WHEN t.confirmed = 1 THEN 1 END) as confirmed,
                          COUNT(*) as total
                          FROM timesheet t 
                          JOIN employees e ON t.user_id = e.user_id
                          WHERE t.date = ? AND e.store = ?''', (today, store))
    else:
        cursor.execute('''SELECT 
                          COUNT(CASE WHEN confirmed = 0 THEN 1 END) as unconfirmed,
                          COUNT(CASE WHEN confirmed = 1 THEN 1 END) as confirmed,
                          COUNT(*) as total
                          FROM timesheet 
                          WHERE date = ?''', (today,))
    
    result = cursor.fetchone()
    conn.close()
    return result

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    employee = get_employee(user_id)
    
    if employee:
        admin_star = " 👑" if employee[5] == 1 else ""
        store_info = f"\n🏪 Магазин: {employee[3]}" if employee[3] else ""
        
        await update.message.reply_text(
            f"👋 С возвращением, {employee[1]}{admin_star}!\n"
            f"📌 Должность: {employee[2]}{store_info}\n\n"
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
    context.user_data['full_name'] = update.message.text
    await update.message.reply_text("Введите вашу должность:")
    return REGISTER_POSITION

async def register_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['position'] = update.message.text
    await update.message.reply_text("Введите название магазина:")
    return REGISTER_STORE

async def register_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    full_name = context.user_data['full_name']
    position = context.user_data['position']
    store = update.message.text
    
    is_admin = add_employee(user_id, full_name, position, store)
    
    admin_text = "\n\n👑 Вы первый пользователь, поэтому вы назначены администратором!" if is_admin else ""
    
    await update.message.reply_text(
        f"✅ Регистрация завершена!\n"
        f"Имя: {full_name}\n"
        f"Должность: {position}\n"
        f"Магазин: {store}{admin_text}"
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
    
    if context.args:
        try:
            days = int(context.args[0])
        except:
            days = 7
    else:
        days = 7
    
    entries = get_timesheet(user_id, days)
    if not entries:
        await update.message.reply_text(f"📊 За последние {days} дней записей нет")
        return
    
    msg = f"📋 *Табель {employee[1]} ({employee[2]}, {employee[3]})*\n"
    msg += f"📅 За последние {days} дней:\n\n"
    
    total_hours = 0
    for e in entries:
        date_obj = datetime.strptime(e[2], '%Y-%m-%d').strftime('%d.%m.%Y')
        status = "✅" if e[3] == 'completed' else "⏳"
        hours = f"({e[6]:.1f}ч)" if e[6] else ""
        confirmed = " ✓" if e[8] == 1 else " ⏳"
        msg += f"{date_obj} {status}{confirmed} {e[4]}-{e[5] or '...'} {hours}\n"
        if e[6]:
            total_hours += e[6]
    
    msg += f"\n⏱ Всего часов: {total_hours:.1f}"
    
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
    
    days_of_week = {0: 'Пн', 1: 'Вт', 2: 'Ср', 3: 'Чт', 4: 'Пт', 5: 'Сб', 6: 'Вс'}
    day_stats = {d: 0 for d in range(7)}
    
    for e in entries:
        if e[6]:
            entry_date = datetime.strptime(e[2], '%Y-%m-%d')
            day_stats[entry_date.weekday()] += 1
    
    msg = f"""
📊 *Статистика за 30 дней*

👤 {employee[1]}
📌 {employee[2]}, 🏪 {employee[3]}

📅 Отработано дней: {days_worked}
⏱ Всего часов: {total_hours:.1f}
📈 Среднее часов: {avg_hours:.1f}

📆 По дням недели:
"""
    for day_num, count in day_stats.items():
        if count > 0:
            msg += f"{days_of_week[day_num]}: {count} дней\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin = is_admin(user_id)
    
    help_text = """
📋 *Команды бота:*

👤 *Для всех:*
/start - Начать работу
/checkin - Начать рабочий день
/checkout - Закончить рабочий день
/timesheet [дней] - Мой табель
/stats - Моя статистика
/help - Помощь

"""
    if admin:
        help_text += """
👑 *Для администраторов:*
/admin - Панель управления
/employees - Список сотрудников
/export [дней] - Выгрузить табель в CSV
/addadmin - Добавить администратора
/stores - Магазины и сотрудники
/confirm - Подтверждение смен
"""
    
    await update.message.reply_text(help_text, parse_mode='Markdown')

# Административные функции
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Только для администраторов")
        return
    
    keyboard = [
        [InlineKeyboardButton("👥 Все сотрудники", callback_data="admin_list")],
        [InlineKeyboardButton("📊 По магазинам", callback_data="admin_by_store")],
        [InlineKeyboardButton("📥 Экспорт за период", callback_data="admin_export_menu")],
        [InlineKeyboardButton("➕ Добавить админа", callback_data="admin_add")],
        [InlineKeyboardButton("📈 Статистика по магазинам", callback_data="admin_store_stats")],
        [InlineKeyboardButton("✅ Подтверждение смен", callback_data="admin_confirm")]  # Новая кнопка
    ]
    await update.message.reply_text(
        "🔐 *Панель администратора*\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def employees_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Только для администраторов")
        return
    
    employees = get_all_employees()
    
    if not employees:
        await update.message.reply_text("❌ Нет зарегистрированных сотрудников")
        return
    
    by_store = {}
    for e in employees:
        store = e[3] or "Без магазина"
        if store not in by_store:
            by_store[store] = []
        by_store[store].append(e)
    
    msg = "👥 *Все сотрудники*\n\n"
    for store, emps in by_store.items():
        msg += f"🏪 *{store}*\n"
        for e in emps:
            admin = "👑 " if e[5] == 1 else ""
            msg += f"  {admin}{e[1]} - {e[2]}\n"
        msg += "\n"
    
    if len(msg) > 4000:
        for i in range(0, len(msg), 4000):
            await update.message.reply_text(msg[i:i+4000], parse_mode='Markdown')
    else:
        await update.message.reply_text(msg, parse_mode='Markdown')

async def export_timesheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Только для администраторов")
        return
    
    if context.args:
        try:
            days = int(context.args[0])
        except:
            days = 30
    else:
        days = 30
    
    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=days)).isoformat()
    
    entries = get_all_timesheet_by_period(start_date, end_date)
    
    if not entries:
        await update.message.reply_text(f"❌ Нет записей за последние {days} дней")
        return
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow(['Сотрудник', 'Должность', 'Магазин', 'Дата', 'Статус', 
                     'Начало', 'Конец', 'Часов', 'Примечание', 'Подтверждено'])
    
    for e in entries:
        status_rus = 'Завершен' if e[4] == 'completed' else 'В работе' if e[4] == 'working' else e[4]
        
        writer.writerow([
            e[0], e[1], e[2], e[3], status_rus, e[5] or '', e[6] or '',
            f"{e[7]:.1f}".replace('.', ',') if e[7] else '', e[8] or '',
            'Да' if e[8] else 'Нет'
        ])
    
    csv_data = output.getvalue()
    output.close()
    
    filename = f"timesheet_{start_date}_to_{end_date}.csv"
    await update.message.reply_document(
        document=csv_data.encode('utf-8-sig'),
        filename=filename,
        caption=f"📊 Табель за {days} дней (с {start_date} по {end_date})"
    )

# НОВЫЕ ФУНКЦИИ ДЛЯ ПОДТВЕРЖДЕНИЯ СМЕН
async def confirm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню подтверждения смен"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Только для администраторов")
        return
    
    employee = get_employee(user_id)
    store = employee[3]
    
    stats = get_shift_stats(store)
    
    keyboard = [
        [InlineKeyboardButton("📋 Неподтвержденные сегодня", callback_data="confirm_today")],
        [InlineKeyboardButton("📅 Неподтвержденные за период", callback_data="confirm_period")],
        [InlineKeyboardButton("✅ Подтвердить все сегодня", callback_data="confirm_all_today")],
        [InlineKeyboardButton("🏪 По магазинам", callback_data="confirm_by_store")],
        [InlineKeyboardButton("📊 Статистика подтверждений", callback_data="confirm_stats")],
        [InlineKeyboardButton("◀️ Назад в админку", callback_data="back_to_admin")]
    ]
    
    stats_text = f"\n\n📊 *Статистика на сегодня:*\n"
    stats_text += f"✅ Подтверждено: {stats[1] if stats else 0}\n"
    stats_text += f"⏳ Ожидают: {stats[0] if stats else 0}\n"
    stats_text += f"📝 Всего смен: {stats[2] if stats else 0}"
    
    await update.message.reply_text(
        f"🔐 *Меню подтверждения смен*\n"
        f"🏪 Ваш магазин: {store}{stats_text}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def confirm_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать неподтвержденные смены за сегодня"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    employee = get_employee(user_id)
    store = employee[3]
    
    unconfirmed = get_unconfirmed_shifts(store)
    
    if not unconfirmed:
        await query.edit_message_text(
            "✅ Все смены за сегодня подтверждены!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")
            ]])
        )
        return
    
    msg = f"📋 *Неподтвержденные смены на сегодня*\n\n"
    
    # Создаем кнопки для каждой смены
    keyboard = []
    for shift in unconfirmed:
        msg += f"👤 {shift[1]} ({shift[2]})\n"
        msg += f"🕐 {shift[5] or '??'} - {shift[6] or '??'}"
        if shift[7]:
            msg += f" ({shift[7]:.1f}ч)"
        if shift[8]:
            msg += f"\n📝 {shift[8]}"
        msg += "\n\n"
        keyboard.append([InlineKeyboardButton(
            f"✅ Подтвердить: {shift[1][:20]}", 
            callback_data=f"confirm_shift_{shift[0]}"
        )])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")])
    
    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def confirm_shift_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтвердить конкретную смену"""
    query = update.callback_query
    await query.answer()
    
    shift_id = int(query.data.replace('confirm_shift_', ''))
    confirm_shift(shift_id)
    
    await query.edit_message_text(
        "✅ Смена подтверждена!",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ К списку", callback_data="confirm_today")
        ]])
    )

async def confirm_all_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтвердить все смены за сегодня"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    employee = get_employee(user_id)
    store = employee[3]
    
    confirm_all_shifts(store)
    
    await query.edit_message_text(
        "✅ Все смены за сегодня подтверждены!",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")
        ]])
    )

async def confirm_period_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню выбора периода для просмотра неподтвержденных"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("3 дня", callback_data="confirm_period_3")],
        [InlineKeyboardButton("7 дней", callback_data="confirm_period_7")],
        [InlineKeyboardButton("14 дней", callback_data="confirm_period_14")],
        [InlineKeyboardButton("30 дней", callback_data="confirm_period_30")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")]
    ]
    
    await query.edit_message_text(
        "📅 Выберите период для просмотра неподтвержденных смен:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def confirm_period_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать неподтвержденные смены за период"""
    query = update.callback_query
    await query.answer()
    
    days = int(query.data.replace('confirm_period_', ''))
    
    user_id = query.from_user.id
    employee = get_employee(user_id)
    store = employee[3]
    
    unconfirmed = get_unconfirmed_shifts_by_period(days, store)
    
    if not unconfirmed:
        await query.edit_message_text(
            f"✅ Все смены за последние {days} дней подтверждены!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")
            ]])
        )
        return
    
    by_date = {}
    for shift in unconfirmed:
        if shift[4] not in by_date:
            by_date[shift[4]] = []
        by_date[shift[4]].append(shift)
    
    msg = f"📋 *Неподтвержденные смены за {days} дней*\n\n"
    
    for date_str, shifts in by_date.items():
        msg += f"📅 *{date_str}*\n"
        for shift in shifts:
            msg += f"  👤 {shift[1]} ({shift[2]})\n"
            msg += f"  🕐 {shift[5] or '??'} - {shift[6] or '??'}"
            if shift[7]:
                msg += f" ({shift[7]:.1f}ч)"
            msg += "\n"
        msg += "\n"
    
    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")
        ]]),
        parse_mode='Markdown'
    )

async def confirm_by_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать неподтвержденные смены по магазинам"""
    query = update.callback_query
    await query.answer()
    
    stores = get_all_stores()
    
    keyboard = []
    for store in stores:
        unconfirmed = get_unconfirmed_shifts(store)
        if unconfirmed:
            count = len(unconfirmed)
            keyboard.append([InlineKeyboardButton(
                f"🏪 {store} ({count})", 
                callback_data=f"confirm_store_{store}"
            )])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")])
    
    await query.edit_message_text(
        "Выберите магазин для просмотра:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def confirm_store_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать смены конкретного магазина"""
    query = update.callback_query
    await query.answer()
    
    store = query.data.replace('confirm_store_', '')
    unconfirmed = get_unconfirmed_shifts(store)
    
    msg = f"🏪 *Магазин: {store}*\n"
    msg += f"📋 Неподтвержденных смен: {len(unconfirmed)}\n\n"
    
    for shift in unconfirmed:
        msg += f"👤 {shift[1]} ({shift[2]})\n"
        msg += f"🕐 {shift[5] or '??'} - {shift[6] or '??'}"
        if shift[7]:
            msg += f" ({shift[7]:.1f}ч)"
        msg += "\n\n"
    
    keyboard = [
        [InlineKeyboardButton("✅ Подтвердить все", callback_data=f"confirm_all_store_{store}")],
        [InlineKeyboardButton("◀️ Назад", callback_data="confirm_by_store")]
    ]
    
    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def confirm_all_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтвердить все смены в магазине за сегодня"""
    query = update.callback_query
    await query.answer()
    
    store = query.data.replace('confirm_all_store_', '')
    confirm_all_shifts(store)
    
    await query.edit_message_text(
        f"✅ Все смены в магазине {store} подтверждены!",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Назад", callback_data="confirm_by_store")
        ]])
    )

async def confirm_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика подтверждений"""
    query = update.callback_query
    await query.answer()
    
    stores = get_all_stores()
    
    msg = "📊 *Статистика подтверждений*\n\n"
    
    for store in stores:
        stats = get_shift_stats(store)
        if stats and stats[2] > 0:
            percent = (stats[1] / stats[2] * 100) if stats[2] > 0 else 0
            msg += f"🏪 *{store}*\n"
            msg += f"✅ Подтверждено: {stats[1]}\n"
            msg += f"⏳ Ожидают: {stats[0]}\n"
            msg += f"📈 Процент: {percent:.1f}%\n\n"
    
    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")
        ]]),
        parse_mode='Markdown'
    )

async def back_to_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вернуться в меню подтверждения"""
    query = update.callback_query
    await query.answer()
    await confirm_menu(query.message, context)

async def export_by_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    stores = get_all_stores()
    
    if not stores:
        await query.edit_message_text("❌ Нет магазинов с сотрудниками")
        return
    
    keyboard = []
    for store in stores:
        keyboard.append([InlineKeyboardButton(f"🏪 {store}", callback_data=f"export_store_{store}")])
    
    keyboard.append([InlineKeyboardButton("📊 Все магазины", callback_data="export_store_all")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_admin")])
    
    await query.edit_message_text(
        "Выберите магазин для экспорта:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def export_store_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    store = query.data.replace('export_store_', '')
    
    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=30)).isoformat()
    
    if store == 'all':
        entries = get_all_timesheet_by_period(start_date, end_date)
        filename = f"timesheet_all_stores_{start_date}_to_{end_date}.csv"
        caption = "📊 Все магазины"
    else:
        entries = get_all_timesheet_by_period(start_date, end_date, store)
        filename = f"timesheet_{store}_{start_date}_to_{end_date}.csv"
        caption = f"📊 Магазин: {store}"
    
    if not entries:
        await query.edit_message_text(f"❌ Нет записей за период")
        return
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Сотрудник', 'Должность', 'Магазин', 'Дата', 'Статус', 
                     'Начало', 'Конец', 'Часов', 'Примечание'])
    
    for e in entries:
        status_rus = 'Завершен' if e[4] == 'completed' else 'В работе' if e[4] == 'working' else e[4]
        
        writer.writerow([
            e[0], e[1], e[2], e[3], status_rus, e[5] or '', e[6] or '',
            f"{e[7]:.1f}".replace('.', ',') if e[7] else '', e[8] or ''
        ])
    
    csv_data = output.getvalue()
    output.close()
    
    await query.message.reply_document(
        document=csv_data.encode('utf-8-sig'),
        filename=filename,
        caption=f"{caption} за 30 дней"
    )
    
    await admin_panel(update, context)

async def add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "Введите Telegram ID пользователя, которого хотите сделать администратором:\n\n"
        "ID можно узнать у пользователя - он должен отправить команду /id боту @userinfobot"
    )
    return ADD_ADMIN_ID

async def add_admin_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_admin_id = int(update.message.text)
    except:
        await update.message.reply_text("❌ Неверный формат ID. Введите число.")
        return ADD_ADMIN_ID
    
    employee = get_employee(new_admin_id)
    
    if not employee:
        await update.message.reply_text("❌ Пользователь с таким ID не зарегистрирован в боте")
        return ConversationHandler.END
    
    context.user_data['new_admin_id'] = new_admin_id
    
    keyboard = [
        [InlineKeyboardButton("✅ Да", callback_data="confirm_add_admin")],
        [InlineKeyboardButton("❌ Нет", callback_data="cancel_add_admin")]
    ]
    
    await update.message.reply_text(
        f"Сделать администратором:\n"
        f"👤 {employee[1]}\n"
        f"📌 {employee[2]}\n"
        f"🏪 {employee[3]}\n\n"
        f"Подтвердите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADD_ADMIN_CONFIRM

async def confirm_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    new_admin_id = context.user_data.get('new_admin_id')
    if new_admin_id:
        add_admin(new_admin_id)
        employee = get_employee(new_admin_id)
        await query.edit_message_text(
            f"✅ Пользователь {employee[1]} теперь администратор!"
        )
    
    return ConversationHandler.END

async def cancel_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Добавление администратора отменено")
    return ConversationHandler.END

async def store_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    stores = get_all_stores()
    
    if not stores:
        await query.edit_message_text("❌ Нет магазинов с сотрудниками")
        return
    
    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=30)).isoformat()
    
    msg = "📈 *Статистика по магазинам за 30 дней*\n\n"
    
    for store in stores:
        employees = get_employees_by_store(store)
        entries = get_all_timesheet_by_period(start_date, end_date, store)
        
        total_hours = sum(e[7] for e in entries if e[7])
        total_days = len(set([e[3] for e in entries]))
        
        msg += f"🏪 *{store}*\n"
        msg += f"👥 Сотрудников: {len(employees)}\n"
        msg += f"⏱ Всего часов: {total_hours:.1f}\n"
        msg += f"📅 Рабочих дней: {total_days}\n\n"
    
    await query.edit_message_text(msg, parse_mode='Markdown')

async def back_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await admin_panel(query.message, context)

async def stores_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Только для администраторов")
        return
    
    stores = get_all_stores()
    
    if not stores:
        await update.message.reply_text("❌ Нет магазинов с сотрудниками")
        return
    
    msg = "🏪 *Магазины и сотрудники*\n\n"
    
    for store in stores:
        employees = get_employees_by_store(store)
        msg += f"*{store}* ({len(employees)} чел.)\n"
        for e in employees:
            admin = "👑 " if e[5] == 1 else ""
            msg += f"  {admin}{e[1]} - {e[2]}\n"
        msg += "\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено")
    return ConversationHandler.END

def main():
    init_database()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Conversation Handlers
    reg_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(register_start, pattern='^register$')],
        states={
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            REGISTER_POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_position)],
            REGISTER_STORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_store)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    add_admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_admin_start, pattern='^admin_add$')],
        states={
            ADD_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_id)],
            ADD_ADMIN_CONFIRM: [CallbackQueryHandler(confirm_add_admin, pattern='^confirm_add_admin$'),
                               CallbackQueryHandler(cancel_add_admin, pattern='^cancel_add_admin$')],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # Basic commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("checkin", checkin))
    app.add_handler(CommandHandler("checkout", checkout))
    app.add_handler(CommandHandler("timesheet", timesheet))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("employees", employees_list))
    app.add_handler(CommandHandler("export", export_timesheet))
    app.add_handler(CommandHandler("stores", stores_menu))
    app.add_handler(CommandHandler("confirm", confirm_menu))  # Новая команда
    
    # Admin panel callbacks
    app.add_handler(CallbackQueryHandler(back_to_admin, pattern='^back_to_admin$'))
    app.add_handler(CallbackQueryHandler(export_by_store, pattern='^admin_export_menu$'))
    app.add_handler(CallbackQueryHandler(store_stats, pattern='^admin_store_stats$'))
    app.add_handler(CallbackQueryHandler(employees_list, pattern='^admin_list$'))
    app.add_handler(CallbackQueryHandler(export_by_store, pattern='^admin_by_store$'))
    app.add_handler(CallbackQueryHandler(export_store_data, pattern='^export_store_'))
    app.add_handler(CallbackQueryHandler(confirm_menu, pattern='^admin_confirm$'))  # Новая кнопка
    
    # Confirmation menu callbacks
    app.add_handler(CallbackQueryHandler(confirm_today, pattern='^confirm_today$'))
    app.add_handler(CallbackQueryHandler(confirm_period_menu, pattern='^confirm_period$'))
    app.add_handler(CallbackQueryHandler(confirm_all_today, pattern='^confirm_all_today$'))
    app.add_handler(CallbackQueryHandler(confirm_by_store, pattern='^confirm_by_store$'))
    app.add_handler(CallbackQueryHandler(confirm_stats, pattern='^confirm_stats$'))
    app.add_handler(CallbackQueryHandler(back_to_confirm, pattern='^back_to_confirm$'))
    app.add_handler(CallbackQueryHandler(confirm_period_shifts, pattern='^confirm_period_\\d+$'))
    app.add_handler(CallbackQueryHandler(confirm_store_shifts, pattern='^confirm_store_'))
    app.add_handler(CallbackQueryHandler(confirm_all_store, pattern='^confirm_all_store_'))
    app.add_handler(CallbackQueryHandler(confirm_shift_action, pattern='^confirm_shift_\\d+$'))
    
    # Conversation handlers
    app.add_handler(reg_conv)
    app.add_handler(add_admin_conv)
    
    print("✅ Бот успешно запущен!")
    app.run_polling()

if __name__ == '__main__':
    main()
