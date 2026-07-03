import logging
import asyncio
import sqlite3
import json
import pytz
from datetime import datetime, timedelta, time as dtime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, ChatMemberHandler, CallbackQueryHandler, \
    MessageHandler, filters, Defaults

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- ВАШИ ДАННЫЕ ----------

import os

BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения!")

# Админы – список чисел, разделённых запятыми
admins_str = os.getenv('ADMINS', '')
ADMINS = [int(x.strip()) for x in admins_str.split(',') if x.strip()]
if not ADMINS:
    raise ValueError("ADMINS не задан в переменных окружения!")  # Все администраторы (имеют доступ к админ-панели)
EXCLUDED_FROM_RATING = []  # ID пользователей, которые НЕ должны участвовать в рейтинге (например, другие админы)
# Если нужно исключить кого-то из рейтинга, добавьте его ID сюда

# ---------- ВРЕМЕННАЯ ЗОНА ----------
TIMEZONE = pytz.timezone('Europe/Moscow')

# ---------- БАЗА ДАННЫХ ----------
DB_NAME = 'reports.db'


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            amount INTEGER,
            boxes INTEGER DEFAULT 0,
            project_id INTEGER DEFAULT 1,
            date TEXT
        )
    ''')
    cur.execute("PRAGMA table_info(reports)")
    columns = [col[1] for col in cur.fetchall()]
    if 'project_id' not in columns:
        cur.execute("ALTER TABLE reports ADD COLUMN project_id INTEGER DEFAULT 1")
    if 'boxes' not in columns:
        cur.execute("ALTER TABLE reports ADD COLUMN boxes INTEGER DEFAULT 0")
    cur.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            plan INTEGER DEFAULT 0,
            accumulated INTEGER DEFAULT 0,
            shipped INTEGER DEFAULT 0,
            box_types TEXT DEFAULT '[]'
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    cur.execute("SELECT COUNT(*) FROM projects")
    if cur.fetchone()[0] == 0:
        default_boxes = [
            {"name": "Коробка 30", "count": 79, "details": 30, "types": 1},
            {"name": "Коробка 45", "count": 2, "details": 45, "types": 1},
            {"name": "Коробка 40", "count": 1, "details": 40, "types": 1}
        ]
        cur.execute("INSERT INTO projects (name, plan, accumulated, shipped, box_types) VALUES (?, ?, ?, ?, ?)",
                    ("Основная отгрузка", 2500, 0, 0, json.dumps(default_boxes)))
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('time_morning', '08:00')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('time_evening', '20:00')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('chat_id', '')")
    conn.commit()
    conn.close()


def get_project(project_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, name, plan, accumulated, shipped, box_types FROM projects WHERE id = ?", (project_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            "id": row[0],
            "name": row[1],
            "plan": row[2],
            "accumulated": row[3],
            "shipped": row[4],
            "box_types": json.loads(row[5]) if row[5] else []
        }
    return None


def get_all_projects():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM projects ORDER BY name")
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1]} for r in rows]


def create_project(name):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT INTO projects (name, plan, accumulated, shipped, box_types) VALUES (?, ?, ?, ?, ?)",
                (name, 0, 0, 0, json.dumps([])))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def delete_project(project_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM reports WHERE project_id = ?", (project_id,))
    cur.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    conn.close()
    return True


def update_project_boxes(project_id, box_types):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE projects SET box_types = ? WHERE id = ?", (json.dumps(box_types), project_id))
    conn.commit()
    conn.close()


def update_project_plan(project_id, plan):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE projects SET plan = ? WHERE id = ?", (plan, project_id))
    conn.commit()
    conn.close()


def update_project_stats(project_id, accumulated, shipped):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE projects SET accumulated = ?, shipped = ? WHERE id = ?", (accumulated, shipped, project_id))
    conn.commit()
    conn.close()


def add_report_to_project(user_id, username, amount, boxes, project_id):
    today = datetime.now().date().isoformat()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('INSERT INTO reports (user_id, username, amount, boxes, project_id, date) VALUES (?, ?, ?, ?, ?, ?)',
                (user_id, username, amount, boxes, project_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    proj = get_project(project_id)
    if proj:
        acc = proj["accumulated"] + amount
        shipped = proj["shipped"]
        plan = proj["plan"]
        while acc >= plan:
            acc -= plan
            shipped += plan
        update_project_stats(project_id, acc, shipped)
    return True


def get_project_stats(project_id):
    proj = get_project(project_id)
    if not proj:
        return None
    return {
        "accumulated": proj["accumulated"],
        "shipped": proj["shipped"],
        "plan": proj["plan"],
        "box_types": proj["box_types"]
    }


def get_user_today_amount(user_id, project_id):
    today = datetime.now().date().isoformat()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT SUM(amount) FROM reports WHERE user_id = ? AND project_id = ? AND date LIKE ?',
                (user_id, project_id, today + '%'))
    total = cur.fetchone()[0] or 0
    conn.close()
    return total


def get_rating(project_id, period_days=None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    # Исключаем только пользователей из списка EXCLUDED_FROM_RATING
    if EXCLUDED_FROM_RATING:
        placeholders = ','.join(['?'] * len(EXCLUDED_FROM_RATING))
        if period_days is None:
            cur.execute(f'''
                SELECT user_id, username, SUM(boxes), COUNT(DISTINCT date(date))
                FROM reports
                WHERE project_id = ? AND user_id NOT IN ({placeholders})
                GROUP BY user_id
                ORDER BY SUM(boxes) DESC
            ''', (project_id, *EXCLUDED_FROM_RATING))
        else:
            since = (datetime.now() - timedelta(days=period_days)).isoformat()
            cur.execute(f'''
                SELECT user_id, username, SUM(boxes), COUNT(DISTINCT date(date))
                FROM reports
                WHERE project_id = ? AND date >= ? AND user_id NOT IN ({placeholders})
                GROUP BY user_id
                ORDER BY SUM(boxes) DESC
            ''', (project_id, since, *EXCLUDED_FROM_RATING))
    else:
        # Если список пуст – все участвуют
        if period_days is None:
            cur.execute('''
                SELECT user_id, username, SUM(boxes), COUNT(DISTINCT date(date))
                FROM reports
                WHERE project_id = ?
                GROUP BY user_id
                ORDER BY SUM(boxes) DESC
            ''', (project_id,))
        else:
            since = (datetime.now() - timedelta(days=period_days)).isoformat()
            cur.execute('''
                SELECT user_id, username, SUM(boxes), COUNT(DISTINCT date(date))
                FROM reports
                WHERE project_id = ? AND date >= ?
                GROUP BY user_id
                ORDER BY SUM(boxes) DESC
            ''', (project_id, since))
    rows = cur.fetchall()
    conn.close()
    result = []
    for user_id, username, total_boxes, days in rows:
        avg = round(total_boxes / days, 1) if days > 0 else 0
        result.append({
            "user_id": user_id,
            "username": username or f"User_{user_id}",
            "total_boxes": total_boxes or 0,
            "days": days or 0,
            "avg": avg
        })
    return result


# ---------- КЛАВИАТУРЫ ----------
def main_menu(is_admin=False):
    kb = [
        [InlineKeyboardButton("📊 Статистика", callback_data='stats')],
        [InlineKeyboardButton("📝 Сдать отчёт", callback_data='report_start')]
    ]
    if is_admin:
        kb.append([InlineKeyboardButton("⚙️ Админ-панель", callback_data='admin_panel')])
    return InlineKeyboardMarkup(kb)


def admin_panel():
    kb = [
        [InlineKeyboardButton("📦 Управление отгрузками", callback_data='admin_projects')],
        [InlineKeyboardButton("⏰ Время уведомлений", callback_data='admin_time')],
        [InlineKeyboardButton("⏰ Отключить уведомления", callback_data='admin_time_off')],
        [InlineKeyboardButton("🏆 Рейтинг сотрудников", callback_data='admin_rating')],
        [InlineKeyboardButton("🔄 Сбросить рейтинг (обнулить коробки)", callback_data='admin_reset_rating')],
        [InlineKeyboardButton("🗑️ Сбросить статистику", callback_data='admin_clear')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_main')],
    ]
    return InlineKeyboardMarkup(kb)


def rating_period_keyboard():
    kb = [
        [InlineKeyboardButton("📅 Сегодня", callback_data='rating_today')],
        [InlineKeyboardButton("📅 Неделя", callback_data='rating_week')],
        [InlineKeyboardButton("📅 Месяц", callback_data='rating_month')],
        [InlineKeyboardButton("📅 Всё время", callback_data='rating_all')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_admin')],
    ]
    return InlineKeyboardMarkup(kb)


def projects_menu():
    kb = [
        [InlineKeyboardButton("➕ Создать отгрузку", callback_data='proj_create')],
        [InlineKeyboardButton("👁️ Список отгрузок", callback_data='proj_list')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_admin')],
    ]
    return InlineKeyboardMarkup(kb)


def project_list_keyboard(projects):
    kb = []
    for p in projects:
        kb.append([InlineKeyboardButton(p['name'], callback_data=f'proj_show_{p["id"]}')])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data='back_projects')])
    return InlineKeyboardMarkup(kb)


def project_detail_keyboard(project_id):
    kb = [
        [InlineKeyboardButton("📦 Добавить коробку", callback_data=f'proj_addbox_{project_id}')],
        [InlineKeyboardButton("🗑️ Удалить отгрузку", callback_data=f'proj_delete_{project_id}')],
        [InlineKeyboardButton("🔙 Назад", callback_data='proj_list')],
    ]
    return InlineKeyboardMarkup(kb)


def num_keyboard(cancel_data='cancel'):
    kb = [
        [InlineKeyboardButton("1", callback_data='n_1'), InlineKeyboardButton("2", callback_data='n_2'),
         InlineKeyboardButton("3", callback_data='n_3')],
        [InlineKeyboardButton("4", callback_data='n_4'), InlineKeyboardButton("5", callback_data='n_5'),
         InlineKeyboardButton("6", callback_data='n_6')],
        [InlineKeyboardButton("7", callback_data='n_7'), InlineKeyboardButton("8", callback_data='n_8'),
         InlineKeyboardButton("9", callback_data='n_9')],
        [InlineKeyboardButton("0", callback_data='n_0'), InlineKeyboardButton("⌫", callback_data='n_bksp'),
         InlineKeyboardButton("✅ Готово", callback_data='n_done')],
        [InlineKeyboardButton("❌ Отмена", callback_data=cancel_data)],
    ]
    return InlineKeyboardMarkup(kb)


def report_boxes_keyboard(boxes):
    if not boxes:
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Нет коробок", callback_data='report_cancel')]])
    kb = []
    for i, box in enumerate(boxes):
        kb.append([InlineKeyboardButton(box['name'], callback_data=f'report_box_{i}')])
    kb.append([InlineKeyboardButton("❌ Отмена", callback_data='report_cancel')])
    return InlineKeyboardMarkup(kb)


def report_done_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, добавить ещё", callback_data='report_more')],
        [InlineKeyboardButton("✅ Нет, завершить", callback_data='report_done')],
        [InlineKeyboardButton("❌ Отмена", callback_data='report_cancel')],
    ])


def time_keyboard():
    kb = [
        [InlineKeyboardButton("0", callback_data='t_0'), InlineKeyboardButton("1", callback_data='t_1'),
         InlineKeyboardButton("2", callback_data='t_2'), InlineKeyboardButton("3", callback_data='t_3'),
         InlineKeyboardButton("4", callback_data='t_4')],
        [InlineKeyboardButton("5", callback_data='t_5'), InlineKeyboardButton("6", callback_data='t_6'),
         InlineKeyboardButton("7", callback_data='t_7'), InlineKeyboardButton("8", callback_data='t_8'),
         InlineKeyboardButton("9", callback_data='t_9')],
        [InlineKeyboardButton(":", callback_data='t_colon'), InlineKeyboardButton("⌫", callback_data='t_bksp'),
         InlineKeyboardButton("✅ Готово", callback_data='t_done')],
        [InlineKeyboardButton("❌ Отмена", callback_data='t_cancel')],
    ]
    return InlineKeyboardMarkup(kb)


def time_period_keyboard():
    kb = [
        [InlineKeyboardButton("🌅 Утро", callback_data='time_morning')],
        [InlineKeyboardButton("🌇 Вечер", callback_data='time_evening')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_admin')],
    ]
    return InlineKeyboardMarkup(kb)


def project_stats_keyboard(projects):
    kb = []
    for p in projects:
        kb.append([InlineKeyboardButton(p['name'], callback_data=f'stats_proj_{p["id"]}')])
        kb.append([InlineKeyboardButton("🔙 Назад", callback_data='back_main')])
    return InlineKeyboardMarkup(kb)


# ---------- ОТПРАВКА СТАТИСТИКИ (без звука) ----------
async def send_stats_for_project(chat_id, context, project_id, is_admin=False):
    proj = get_project(project_id)
    if not proj:
        await context.bot.send_message(chat_id, "❌ Отгрузка не найдена.", reply_markup=main_menu(is_admin),
                                       disable_notification=True)
        return
    plan = proj["plan"]
    accumulated = proj["accumulated"]
    shipped = proj["shipped"]
    remaining = plan - accumulated
    if remaining < 0:
        remaining = 0
    percent = round((accumulated / plan) * 100, 1) if plan > 0 else 0
    box_lines = "\n".join([f"• {b['name']}" for b in proj["box_types"]]) if proj["box_types"] else "Нет коробок"

    text = (
        f"📊 СТАТИСТИКА: {proj['name']}\n"
        f"📦 Накоплено: {accumulated} / {plan} ({percent}%)\n"
        f"⏳ Осталось: {remaining}\n"
        f"🚛 Отгружено: {shipped}\n\n"
        f"📦 Коробки:\n{box_lines}"
    )
    await context.bot.send_message(chat_id, text, reply_markup=main_menu(is_admin), disable_notification=True)


# ---------- КОМАНДЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if not get_setting('chat_id'):
            set_setting('chat_id', str(chat_id))
            if context.job_queue:
                await reschedule_jobs(context)
    is_admin = update.effective_user.id in ADMINS
    await update.message.reply_text("👋 Главное меню:", reply_markup=main_menu(is_admin), disable_notification=True)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    is_admin = update.effective_user.id in ADMINS
    projects = get_all_projects()
    if not projects:
        await update.message.reply_text("❌ Нет отгрузок.", reply_markup=main_menu(is_admin), disable_notification=True)
        return
    await update.message.reply_text("📊 Выберите отгрузку для просмотра статистики:",
                                    reply_markup=project_stats_keyboard(projects), disable_notification=True)


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    is_admin = user_id in ADMINS
    projects = get_all_projects()
    if not projects:
        await update.message.reply_text("❌ Нет ни одной отгрузки.", reply_markup=main_menu(is_admin),
                                        disable_notification=True)
        return
    if len(projects) == 1:
        project_id = projects[0]["id"]
        proj = get_project(project_id)
        if not proj or not proj["box_types"]:
            await update.message.reply_text("❌ В отгрузке нет коробок.", reply_markup=main_menu(is_admin),
                                            disable_notification=True)
            return
        context.user_data['report_items'] = []
        context.user_data['report_project'] = project_id
        await update.message.reply_text("📦 Выберите коробку:", reply_markup=report_boxes_keyboard(proj["box_types"]),
                                        disable_notification=True)
    else:
        kb = []
        for p in projects:
            kb.append([InlineKeyboardButton(p['name'], callback_data=f'report_proj_{p["id"]}')])
        kb.append([InlineKeyboardButton("❌ Отмена", callback_data='report_cancel')])
        await update.message.reply_text("📦 Выберите отгрузку, для которой сдаёте отчёт:",
                                        reply_markup=InlineKeyboardMarkup(kb), disable_notification=True)


# ---------- ОСНОВНЫЕ ОБРАБОТЧИКИ ----------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    is_admin = user_id in ADMINS
    chat_id = query.message.chat.id

    logger.info(f"Получен callback: {data}")

    # ---- СТАТИСТИКА ----
    if data == 'stats':
        projects = get_all_projects()
        if not projects:
            await query.edit_message_text("❌ Нет отгрузок.", reply_markup=main_menu(is_admin))
            return
        await query.edit_message_text("📊 Выберите отгрузку для просмотра статистики:",
                                      reply_markup=project_stats_keyboard(projects))
        return

    if data.startswith('stats_proj_'):
        proj_id = int(data.split('_')[2])
        await send_stats_for_project(chat_id, context, proj_id, is_admin)
        try:
            await query.delete_message()
        except:
            pass
        return

    # ---- ОТЧЁТ ----
    if data == 'report_start':
        projects = get_all_projects()
        if not projects:
            await query.edit_message_text("❌ Нет ни одной отгрузки. Обратитесь к администратору.",
                                          reply_markup=main_menu(is_admin))
            return
        if len(projects) == 1:
            project_id = projects[0]["id"]
            proj = get_project(project_id)
            if not proj or not proj["box_types"]:
                await query.edit_message_text("❌ В отгрузке нет коробок.", reply_markup=main_menu(is_admin))
                return
            context.user_data['report_items'] = []
            context.user_data['report_project'] = project_id
            await query.message.reply_text("📦 Выберите коробку:", reply_markup=report_boxes_keyboard(proj["box_types"]),
                                           disable_notification=True)
            await query.delete_message()
            return
        else:
            kb = []
            for p in projects:
                kb.append([InlineKeyboardButton(p['name'], callback_data=f'report_proj_{p["id"]}')])
            kb.append([InlineKeyboardButton("❌ Отмена", callback_data='report_cancel')])
            await query.edit_message_text("📦 Выберите отгрузку, для которой сдаёте отчёт:",
                                          reply_markup=InlineKeyboardMarkup(kb))
            return

    if data.startswith('report_proj_'):
        project_id = int(data.split('_')[2])
        proj = get_project(project_id)
        if not proj:
            await query.edit_message_text("❌ Отгрузка не найдена.", reply_markup=main_menu(is_admin))
            return
        if not proj["box_types"]:
            await query.edit_message_text("❌ В отгрузке нет коробок.", reply_markup=main_menu(is_admin))
            return
        context.user_data['report_items'] = []
        context.user_data['report_project'] = project_id
        await query.edit_message_text("📦 Выберите коробку:", reply_markup=report_boxes_keyboard(proj["box_types"]))
        return

    if data == 'report_cancel':
        context.user_data.pop('report_items', None)
        context.user_data.pop('report_project', None)
        context.user_data.pop('report_box_idx', None)
        context.user_data.pop('report_temp_num', None)
        await query.edit_message_text("❌ Отменено.", reply_markup=main_menu(is_admin))
        return

    if data.startswith('report_box_'):
        idx = int(data.split('_')[2])
        proj = get_project(context.user_data.get('report_project'))
        if not proj or idx >= len(proj["box_types"]):
            await query.edit_message_text("❌ Тип не найден.", reply_markup=main_menu(is_admin))
            return
        context.user_data['report_box_idx'] = idx
        context.user_data['report_temp_num'] = ''
        await query.edit_message_text(
            f"✏️ {proj['box_types'][idx]['name']}\nВведите количество:",
            reply_markup=num_keyboard('report_cancel')
        )
        return

    if data.startswith('n_') and 'report_box_idx' in context.user_data:
        action = data.split('_')[1]
        if action == 'cancel':
            context.user_data.pop('report_box_idx', None)
            context.user_data.pop('report_temp_num', None)
            await query.edit_message_text("❌ Отменено.", reply_markup=main_menu(is_admin))
            return
        if action == 'bksp':
            context.user_data['report_temp_num'] = context.user_data['report_temp_num'][:-1]
        elif action == 'done':
            try:
                qty = int(context.user_data.get('report_temp_num', '0'))
                if qty <= 0:
                    await query.answer("Введите число > 0", show_alert=True)
                    return
                idx = context.user_data['report_box_idx']
                proj = get_project(context.user_data.get('report_project'))
                if not proj or idx >= len(proj["box_types"]):
                    await query.edit_message_text("❌ Ошибка.", reply_markup=main_menu(is_admin))
                    return
                box = proj["box_types"][idx]
                det = qty * box['details']
                context.user_data['report_items'].append((idx, qty, det))
                context.user_data.pop('report_box_idx', None)
                context.user_data.pop('report_temp_num', None)
                await query.edit_message_text(
                    f"✅ Добавлено: {qty} коробок = {det} дет.\nХотите добавить ещё?",
                    reply_markup=report_done_keyboard()
                )
                return
            except:
                await query.answer("Ошибка", show_alert=True)
                return
        else:
            context.user_data['report_temp_num'] += action
        current = context.user_data.get('report_temp_num', '')
        await query.edit_message_text(
            f"✏️ Текущее: {current or '0'}",
            reply_markup=num_keyboard('report_cancel')
        )
        return

    if data == 'report_more':
        proj = get_project(context.user_data.get('report_project'))
        if not proj:
            await query.edit_message_text("❌ Ошибка.", reply_markup=main_menu(is_admin))
            return
        await query.edit_message_text("📦 Выберите коробку:", reply_markup=report_boxes_keyboard(proj["box_types"]))
        return

    if data == 'report_done':
        items = context.user_data.get('report_items', [])
        if not items:
            await query.edit_message_text("❌ Нет позиций.", reply_markup=main_menu(is_admin))
            return
        total_det = sum(item[2] for item in items)
        total_boxes = sum(item[1] for item in items)
        project_id = context.user_data.get('report_project')
        if not project_id:
            await query.edit_message_text("❌ Ошибка.", reply_markup=main_menu(is_admin))
            return
        add_report_to_project(user_id, query.from_user.username or query.from_user.first_name, total_det, total_boxes,
                              project_id)
        context.user_data.pop('report_items', None)
        context.user_data.pop('report_project', None)
        await query.edit_message_text(
            f"✅ Отчёт принят!\nДобавлено: {total_det} дет. ({total_boxes} кор.)\nВсего за сегодня: {get_user_today_amount(user_id, project_id)} дет.",
            reply_markup=main_menu(is_admin)
        )
        gid = get_setting('chat_id')
        if gid:
            gid = int(gid)
            if gid != chat_id:
                await send_stats_for_project(gid, context, project_id, False)
        return

    # ---- АДМИН-ПАНЕЛЬ (все команды с проверкой прав) ----
    if data == 'admin_panel':
        if not is_admin:
            await query.edit_message_text("⛔ Доступ запрещён.", reply_markup=main_menu(False))
            return
        await query.edit_message_text("⚙️ Админ-панель:", reply_markup=admin_panel())
        return

    if data == 'back_main':
        await query.edit_message_text("📋 Главное меню:", reply_markup=main_menu(is_admin))
        return

    if data == 'back_admin':
        if not is_admin:
            return
        await query.edit_message_text("⚙️ Админ-панель:", reply_markup=admin_panel())
        return

    if data == 'back_projects':
        if not is_admin:
            return
        await query.edit_message_text("📦 Управление отгрузками:", reply_markup=projects_menu())
        return

    # ---- РЕЙТИНГ ----
    if data == 'admin_rating':
        if not is_admin:
            await query.edit_message_text("⛔ Доступ запрещён.", reply_markup=main_menu(False))
            return
        await query.edit_message_text("🏆 Выберите период для рейтинга:", reply_markup=rating_period_keyboard())
        return

    if data.startswith('rating_'):
        if not is_admin:
            return
        period_map = {
            'today': 1,
            'week': 7,
            'month': 30,
            'all': None
        }
        key = data.split('_')[1]
        period_days = period_map.get(key)
        projects = get_all_projects()
        if not projects:
            await query.edit_message_text("❌ Нет отгрузок.", reply_markup=admin_panel())
            return
        admin_id = user_id
        for proj in projects:
            rating_data = get_rating(proj["id"], period_days)
            if not rating_data:
                text = f"📊 {proj['name']}\nНет данных за выбранный период."
            else:
                lines = []
                for i, r in enumerate(rating_data, 1):
                    medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f"{i}."))
                    lines.append(f"{medal} {r['username']} — {r['total_boxes']} кор. (ср. {r['avg']} кор./день)")
                text = f"📊 {proj['name']}\n" + "\n".join(lines)
            await context.bot.send_message(chat_id=admin_id, text=text, disable_notification=True)
        await query.edit_message_text("🏆 Рейтинг отправлен в личные сообщения.", reply_markup=admin_panel())
        return

    # ---- СБРОС РЕЙТИНГА (обнуление коробок за всё время) ----
    if data == 'admin_reset_rating':
        if not is_admin:
            await query.edit_message_text("⛔ Доступ запрещён.", reply_markup=main_menu(False))
            return
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("UPDATE reports SET boxes = 0")
        conn.commit()
        conn.close()
        await query.edit_message_text("✅ Рейтинг сброшен (количество коробок обнулено во всех отчётах).",
                                      reply_markup=admin_panel())
        return

    # ---- УПРАВЛЕНИЕ ОТГРУЗКАМИ ----
    if data == 'admin_projects':
        if not is_admin:
            await query.edit_message_text("⛔ Доступ запрещён.", reply_markup=main_menu(False))
            return
        await query.edit_message_text("📦 Управление отгрузками:", reply_markup=projects_menu())
        return

    if data == 'proj_create':
        if not is_admin:
            await query.edit_message_text("⛔ Доступ запрещён.", reply_markup=main_menu(False))
            return
        context.user_data['create_project'] = True
        await query.edit_message_text("➡️ Введите название новой отгрузки (текстом):",
                                      reply_markup=InlineKeyboardMarkup(
                                          [[InlineKeyboardButton("❌ Отмена", callback_data='proj_create_cancel')]]))
        return

    if data == 'proj_create_cancel':
        if not is_admin:
            return
        context.user_data.pop('create_project', None)
        await query.edit_message_text("❌ Отменено.", reply_markup=projects_menu())
        return

    if data == 'proj_list':
        if not is_admin:
            return
        projects = get_all_projects()
        if not projects:
            await query.edit_message_text("❌ Нет отгрузок.", reply_markup=projects_menu())
            return
        await query.edit_message_text("📋 Список отгрузок:", reply_markup=project_list_keyboard(projects))
        return

    if data.startswith('proj_show_'):
        if not is_admin:
            return
        proj_id = int(data.split('_')[2])
        proj = get_project(proj_id)
        if not proj:
            await query.edit_message_text("❌ Отгрузка не найдена.", reply_markup=projects_menu())
            return
        box_list = "\n".join([f"• {b['name']} ({b['details']} дет., {b['count']} кор.)" for b in proj["box_types"]]) if \
        proj["box_types"] else "Нет коробок"
        text = (
            f"📦 {proj['name']}\n"
            f"План: {proj['plan']}\n"
            f"Накоплено: {proj['accumulated']}\n"
            f"Отгружено: {proj['shipped']}\n\n"
            f"📦 Коробки:\n{box_list}"
        )
        await query.edit_message_text(text, reply_markup=project_detail_keyboard(proj_id))
        return

    # ---- УДАЛЕНИЕ ОТГРУЗКИ ----
    if data.startswith('proj_delete_'):
        if data == 'proj_delete_confirm':
            if not is_admin or 'delete_project_id' not in context.user_data:
                return
            proj_id = context.user_data.pop('delete_project_id')
            delete_project(proj_id)
            await query.edit_message_text("✅ Отгрузка удалена.", reply_markup=projects_menu())
            return

        if data == 'proj_delete_cancel':
            if not is_admin:
                return
            context.user_data.pop('delete_project_id', None)
            await query.edit_message_text("❌ Отменено.", reply_markup=projects_menu())
            return

        if not is_admin:
            return
        proj_id = int(data.split('_')[2])
        proj = get_project(proj_id)
        if not proj:
            await query.edit_message_text("❌ Отгрузка не найдена.", reply_markup=projects_menu())
            return
        context.user_data['delete_project_id'] = proj_id
        await query.edit_message_text(
            f"⚠️ Вы уверены, что хотите удалить отгрузку «{proj['name']}»? Все связанные отчёты будут удалены.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да, удалить", callback_data='proj_delete_confirm')],
                [InlineKeyboardButton("❌ Отмена", callback_data='proj_delete_cancel')]
            ])
        )
        return

    # ---- ДОБАВЛЕНИЕ КОРОБКИ ----
    if data.startswith('proj_addbox_'):
        if not is_admin:
            return
        proj_id = int(data.split('_')[2])
        context.user_data['addbox_project'] = proj_id
        context.user_data['addbox_step'] = 'name'
        await query.edit_message_text("➡️ Введите название коробки (например, «Коробка 30»):",
                                      reply_markup=InlineKeyboardMarkup(
                                          [[InlineKeyboardButton("❌ Отмена", callback_data='addbox_cancel')]]))
        return

    if data == 'addbox_cancel':
        if not is_admin:
            return
        context.user_data.pop('addbox_project', None)
        context.user_data.pop('addbox_step', None)
        context.user_data.pop('addbox_name', None)
        context.user_data.pop('addbox_details', None)
        context.user_data.pop('addbox_count', None)
        context.user_data.pop('addbox_temp', None)
        await query.edit_message_text("❌ Отменено.", reply_markup=projects_menu())
        return

    if data.startswith('n_') and context.user_data.get('addbox_step') in ('details', 'count', 'types'):
        if not is_admin:
            return
        step = context.user_data['addbox_step']
        action = data.split('_')[1]
        if action == 'cancel':
            context.user_data.pop('addbox_project', None)
            context.user_data.pop('addbox_step', None)
            context.user_data.pop('addbox_name', None)
            context.user_data.pop('addbox_details', None)
            context.user_data.pop('addbox_count', None)
            context.user_data.pop('addbox_temp', None)
            await query.edit_message_text("❌ Отменено.", reply_markup=projects_menu())
            return
        if action == 'bksp':
            context.user_data['addbox_temp'] = context.user_data['addbox_temp'][:-1]
        elif action == 'done':
            try:
                val = int(context.user_data.get('addbox_temp', '0'))
                if val <= 0:
                    await query.answer("Введите число > 0", show_alert=True)
                    return
                if step == 'details':
                    context.user_data['addbox_details'] = val
                    context.user_data['addbox_step'] = 'count'
                    context.user_data['addbox_temp'] = ''
                    await query.edit_message_text("➡️ Введите количество коробок:",
                                                  reply_markup=num_keyboard('addbox_cancel'))
                elif step == 'count':
                    context.user_data['addbox_count'] = val
                    context.user_data['addbox_step'] = 'types'
                    context.user_data['addbox_temp'] = ''
                    await query.edit_message_text("➡️ Введите количество видов (если нет – введите 1):",
                                                  reply_markup=num_keyboard('addbox_cancel'))
                else:  # types
                    proj_id = context.user_data['addbox_project']
                    proj = get_project(proj_id)
                    if not proj:
                        await query.edit_message_text("❌ Ошибка.", reply_markup=projects_menu())
                        return
                    boxes = proj["box_types"]
                    new_box = {
                        "name": context.user_data['addbox_name'],
                        "details": context.user_data['addbox_details'],
                        "count": context.user_data['addbox_count'],
                        "types": val
                    }
                    boxes.append(new_box)
                    update_project_boxes(proj_id, boxes)
                    total_plan = sum(b['details'] * b['count'] for b in boxes)
                    update_project_plan(proj_id, total_plan)
                    context.user_data.pop('addbox_project', None)
                    context.user_data.pop('addbox_step', None)
                    context.user_data.pop('addbox_name', None)
                    context.user_data.pop('addbox_details', None)
                    context.user_data.pop('addbox_count', None)
                    context.user_data.pop('addbox_temp', None)
                    await query.edit_message_text(
                        f"✅ Коробка добавлена!\nНазвание: {new_box['name']}\n"
                        f"Деталей в коробке: {new_box['details']}\n"
                        f"Количество коробок: {new_box['count']}\n"
                        f"Виды: {new_box['types']}\n"
                        f"Новый план: {total_plan}",
                        reply_markup=projects_menu()
                    )
                    gid = get_setting('chat_id')
                    if gid:
                        gid = int(gid)
                        await send_stats_for_project(gid, context, proj_id, False)
                return
            except:
                await query.answer("Ошибка", show_alert=True)
                return
        else:
            context.user_data['addbox_temp'] += action
        current = context.user_data.get('addbox_temp', '')
        label = "Деталей" if step == 'details' else ("Коробок" if step == 'count' else "Видов")
        await query.edit_message_text(
            f"{label}: {current or '0'}",
            reply_markup=num_keyboard('addbox_cancel')
        )
        return

    # ---- ВРЕМЯ ----
    if data == 'admin_time':
        if not is_admin:
            await query.edit_message_text("⛔ Доступ запрещён.", reply_markup=main_menu(False))
            return
        await query.edit_message_text("⏰ Настройка времени уведомлений:", reply_markup=time_period_keyboard())
        return

    if data == 'admin_time_off':
        if not is_admin:
            await query.edit_message_text("⛔ Доступ запрещён.", reply_markup=main_menu(False))
            return
        if context.job_queue:
            for job in context.job_queue.jobs():
                if job.name in ('morning', 'evening', 'reminder_morning', 'reminder_evening'):
                    job.schedule_removal()
        set_setting('time_morning', 'off')
        set_setting('time_evening', 'off')
        await query.edit_message_text("✅ Уведомления отключены.", reply_markup=admin_panel())
        return

    if data == 'time_morning' or data == 'time_evening':
        if not is_admin:
            return
        period = 'morning' if data == 'time_morning' else 'evening'
        context.user_data['admin_time_period'] = period
        context.user_data['admin_mode'] = 'time_input'
        context.user_data['admin_temp_time'] = ''
        await query.edit_message_text(f"⏰ Введите время для {period} смены (HH:MM):", reply_markup=time_keyboard())
        return

    if data.startswith('t_') and context.user_data.get('admin_mode') == 'time_input':
        if not is_admin:
            return
        action = data.split('_')[1]
        if action == 'cancel':
            context.user_data.pop('admin_mode', None)
            context.user_data.pop('admin_temp_time', None)
            context.user_data.pop('admin_time_period', None)
            await query.edit_message_text("❌ Отменено.", reply_markup=admin_panel())
            return
        if action == 'bksp':
            context.user_data['admin_temp_time'] = context.user_data['admin_temp_time'][:-1]
        elif action == 'done':
            time_str = context.user_data.get('admin_temp_time', '')
            parts = time_str.split(':')
            if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2:
                await query.answer("Формат HH:MM (например, 08:30)", show_alert=True)
                return
            try:
                h = int(parts[0]);
                m = int(parts[1])
                if not (0 <= h < 24 and 0 <= m < 60):
                    raise ValueError
            except:
                await query.answer("Неверное время", show_alert=True)
                return
            period = context.user_data['admin_time_period']
            key = 'time_morning' if period == 'morning' else 'time_evening'
            set_setting(key, time_str)
            context.user_data.pop('admin_mode', None)
            context.user_data.pop('admin_temp_time', None)
            context.user_data.pop('admin_time_period', None)
            await query.edit_message_text(f"✅ {period} смена: уведомление в {time_str}", reply_markup=admin_panel())
            if context.job_queue:
                await reschedule_jobs(context)
            return
        else:
            if action == 'colon':
                context.user_data['admin_temp_time'] += ':'
            else:
                context.user_data['admin_temp_time'] += action
        current = context.user_data.get('admin_temp_time', '')
        await query.edit_message_text(
            f"Время: {current or '00:00'}",
            reply_markup=time_keyboard()
        )
        return

    # ---- СБРОС СТАТИСТИКИ ----
    if data == 'admin_clear':
        if not is_admin:
            await query.edit_message_text("⛔ Доступ запрещён.", reply_markup=main_menu(False))
            return
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("DELETE FROM reports")
        cur.execute("UPDATE projects SET accumulated = 0, shipped = 0")
        conn.commit()
        conn.close()
        await query.edit_message_text("✅ Статистика сброшена (все отчёты удалены, накопления обнулены).",
                                      reply_markup=admin_panel())
        return

    await query.edit_message_text("⚠️ Неизвестная команда.", reply_markup=main_menu(is_admin))
    logger.warning(f"Неизвестный callback: {data}")


# ---------- ОБРАБОТКА ТЕКСТА ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = user_id in ADMINS
    text = update.message.text.strip()

    if context.user_data.get('create_project'):
        if not is_admin:
            await update.message.reply_text("⛔ Доступ запрещён.", disable_notification=True)
            return
        name = text
        if not name:
            await update.message.reply_text("❌ Название не может быть пустым.", disable_notification=True)
            return
        projects = get_all_projects()
        if any(p["name"].lower() == name.lower() for p in projects):
            await update.message.reply_text("❌ Отгрузка с таким названием уже существует.", disable_notification=True)
            return
        new_id = create_project(name)
        context.user_data.pop('create_project', None)
        await update.message.reply_text(f"✅ Отгрузка «{name}» создана (ID: {new_id}).", reply_markup=projects_menu(),
                                        disable_notification=True)
        return

    if context.user_data.get('addbox_step') == 'name':
        if not is_admin:
            await update.message.reply_text("⛔ Доступ запрещён.", disable_notification=True)
            return
        name = text
        if not name:
            await update.message.reply_text("❌ Название не может быть пустым.", disable_notification=True)
            return
        context.user_data['addbox_name'] = name
        context.user_data['addbox_step'] = 'details'
        context.user_data['addbox_temp'] = ''
        await update.message.reply_text("➡️ Введите количество деталей в одной коробке:",
                                        reply_markup=num_keyboard('addbox_cancel'), disable_notification=True)
        return

    await update.message.reply_text("Используйте кнопки или команды.", reply_markup=main_menu(is_admin),
                                    disable_notification=True)


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def get_setting(key):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def set_setting(key, value):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


# ---------- ПЛАНИРОВЩИК ----------
async def scheduled_report(context: ContextTypes.DEFAULT_TYPE, period: str):
    chat_id = get_setting('chat_id')
    if not chat_id:
        return
    chat_id = int(chat_id)
    try:
        # Будильник – со звуком
        await context.bot.send_message(
            chat_id,
            f"🔔 {period} смена! Сдайте отчёт.",
            reply_markup=main_menu(False),
            disable_notification=False
        )
        if context.job_queue:
            job_name = f'reminder_{period}'
            for job in context.job_queue.jobs():
                if job.name == job_name:
                    job.schedule_removal()
            context.job_queue.run_once(
                lambda ctx: ctx.bot.send_message(
                    chat_id,
                    f"⏰ Напоминание ({period})",
                    reply_markup=main_menu(False),
                    disable_notification=False
                ),
                when=timedelta(minutes=30),
                chat_id=chat_id,
                name=job_name
            )
    except Exception as e:
        logger.error(f"Ошибка уведомления: {e}")


async def reschedule_jobs(context: ContextTypes.DEFAULT_TYPE):
    if context.job_queue is None:
        return
    for job in context.job_queue.jobs():
        if job.name in ('morning', 'evening', 'reminder_morning', 'reminder_evening'):
            job.schedule_removal()
    morning = get_setting('time_morning')
    evening = get_setting('time_evening')
    if morning == 'off' or not morning:
        morning = None
    if evening == 'off' or not evening:
        evening = None
    chat_id = get_setting('chat_id')
    if not chat_id:
        return
    chat_id = int(chat_id)

    def parse_time(t_str):
        h, m = map(int, t_str.split(':'))
        return dtime(hour=h, minute=m, tzinfo=TIMEZONE)

    if morning:
        context.job_queue.run_daily(
            lambda ctx: scheduled_report(ctx, 'Утренняя'),
            time=parse_time(morning),
            days=(0, 1, 2, 3, 4, 5, 6),
            chat_id=chat_id,
            name='morning'
        )
    if evening:
        context.job_queue.run_daily(
            lambda ctx: scheduled_report(ctx, 'Вечерняя'),
            time=parse_time(evening),
            days=(0, 1, 2, 3, 4, 5, 6),
            chat_id=chat_id,
            name='evening'
        )
    logger.info(f"Уведомления настроены: утро {morning}, вечер {evening} (временная зона {TIMEZONE})")


# ---------- ДОБАВЛЕНИЕ В ЧАТ ----------
async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member = update.chat_member
    if not chat_member:
        return
    if chat_member.new_chat_member.status == ChatMemberStatus.MEMBER:
        chat = chat_member.chat
        if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            set_setting('chat_id', str(chat.id))
            try:
                await context.bot.send_message(chat.id, "👋 Привет! Я бот учёта сборки.", reply_markup=main_menu(False),
                                               disable_notification=True)
                if context.job_queue:
                    await reschedule_jobs(context)
            except Exception as e:
                logger.error(f"Ошибка при добавлении в чат: {e}")


# ---------- ОШИБКИ ----------
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}")
    if update and update.effective_chat:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Ошибка. Попробуйте позже.",
                                       disable_notification=True)


# ---------- ЗАПУСК ----------
async def set_commands(application):
    commands = [
        BotCommand("start", "Главное меню"),
        BotCommand("stats", "Статистика"),
        BotCommand("report", "Сдать отчёт"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Команды успешно установлены")
    except Exception as e:
        logger.error(f"Ошибка при установке команд: {e}")


def main():
    init_db()
    defaults = Defaults(disable_notification=True)  # глобально без звука, но мы явно переопределим
    app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(set_commands(app))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(ChatMemberHandler(chat_member_update))
    app.add_error_handler(error_handler)

    async def post_init(application):
        await reschedule_jobs(application)

    app.post_init = post_init

    app.run_polling()


if __name__ == '__main__':
    main()
