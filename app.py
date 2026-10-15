import asyncio
import os
from datetime import datetime
from contextlib import asynccontextmanager

import aiosqlite
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Update,
    WebAppInfo
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import finance_core as fc
from finance_api import router as finance_router
from fastapi.responses import FileResponse

BOT_TOKEN = os.getenv("BOT_TOKEN", "8946677929:AAHDpPknWmm12DVFXncbp23q1Ysm8p8XTag")
DB_PATH = "tracker.db"
OWNER_ID = int(os.getenv("OWNER_ID", "8011530004"))
PORT = int(os.getenv("PORT", 8000))

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

class IncomeForm(StatesGroup):
    amount = State()
    payer = State()
    category = State()

class ExpenseForm(StatesGroup):
    amount = State()
    category = State()

class WithdrawForm(StatesGroup):
    amount = State()

class ContactForm(StatesGroup):
    name = State()
    phone = State()
    channel = State()
    status = State()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT DEFAULT 'todo',
            created_at TEXT NOT NULL,
            updated_at TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            phone TEXT,
            channel TEXT,
            status TEXT DEFAULT 'sent',
            created_at TEXT NOT NULL
        )""")
        await db.commit()

GROUPS = {1: "🔴 Критично", 2: "🟡 Важно", 3: "🟢 Подождать", 4: "⚪ Долгосрочно"}

INITIAL_TASKS = [
    (1, "Продукт готов", "done"), (1, "Все страницы работают", "done"),
    (1, "Документы", "done"), (1, "Безопасность", "done"), (1, "UptimeRobot", "done"),
    (1, "Проверить бэкап", "todo"), (1, "Список 10 моек", "todo"),
    (1, "10 сообщений", "todo"), (1, "20 сообщений", "todo"),
    (1, "30 сообщений", "todo"), (1, "50 сообщений", "todo"),
    (1, "1-й диалог", "todo"), (1, "1-й пилот", "todo"), (1, "2-й пилот", "todo"),
    (1, "3 отзыва", "todo"), (1, "Кейс с цифрами", "todo"),
    (2, "Домен куплен", "todo"), (2, "Домен подключён", "todo"),
    (2, "Договор рассрочки", "todo"), (2, "Инструкция клиента", "todo"),
    (2, "Инструкция сотрудника", "todo"), (2, "Канал поддержки", "todo"),
    (2, "Условия продления", "todo"), (2, "Перенос в РФ", "todo"),
    (2, "1-я Enterprise", "todo"), (2, "2-я Enterprise", "todo"),
    (2, "Партнёрская программа", "todo"), (2, "Реферальные бонусы", "todo"),
    (2, "Лендинг с кейсом", "todo"), (2, "5+ клиентов", "todo"),
    (3, "ИП/ООО", "todo"), (3, "УСН 6%", "todo"), (3, "Sentry", "todo"),
    (3, "Резервный бот", "todo"), (3, "UI часов работы", "todo"),
    (3, "Отзывы в системе", "todo"), (3, "Нанять менеджера", "todo"),
    (3, "Авторассылки", "todo"), (3, "CRM касаний", "todo"), (3, "Тест цен", "todo"),
    (3, "Анализ конкурентов", "todo"), (3, "10+ клиентов", "todo"),
    (4, "Банковская рассрочка", "todo"), (4, "White-label", "todo"),
    (4, "Мультиязычность", "todo"), (4, "Мобильное приложение", "todo"),
    (4, "API", "todo"), (4, "Франшиза", "todo"),
    (4, "Выставки", "todo"), (4, "50+ клиентов", "todo"),
]

async def seed_tasks():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM tasks")
        row = await cur.fetchone()
        if row[0] == 0:
            now = datetime.now().isoformat()
            for gid, title, status in INITIAL_TASKS:
                await db.execute("INSERT INTO tasks (group_id, title, status, created_at) VALUES (?, ?, ?, ?)", (gid, title, status, now))
            await db.commit()

async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        stats = {}
        for gid in GROUPS:
            cur = await db.execute("SELECT COUNT(*) FROM tasks WHERE group_id = ? AND status = 'done'", (gid,))
            done = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM tasks WHERE group_id = ?", (gid,))
            total = (await cur.fetchone())[0]
            stats[gid] = (done, total)
        return stats

async def get_group_tasks(gid):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, title, status FROM tasks WHERE group_id = ? ORDER BY id", (gid,))
        return await cur.fetchall()

async def toggle_task_by_id(task_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        if not row: return None
        new = "todo" if row[0] == "done" else "done"
        await db.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (new, datetime.now().isoformat(), task_id))
        await db.commit()
        return new

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Прогресс", callback_data="stats"),
         InlineKeyboardButton(text="📋 Задачи", callback_data="tasks")],
        [InlineKeyboardButton(
            text="💰 Финансы",
            web_app=WebAppInfo(url="https://torclix-tracker.onrender.com/finance")
         ),
         InlineKeyboardButton(text="📞 Касания", callback_data="contacts")],
    ])

def finance_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Доход", callback_data="fin_income"),
         InlineKeyboardButton(text="➖ Расход", callback_data="fin_expense")],
        [InlineKeyboardButton(text="💸 Вывод", callback_data="fin_withdraw"),
         InlineKeyboardButton(text="📊 Отчёт", callback_data="fin_report")],
        [InlineKeyboardButton(text="📋 История", callback_data="fin_history"),
         InlineKeyboardButton(text="↩️ Отмена", callback_data="fin_undo")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main")],
    ])

def payer_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Физлицо (4%)", callback_data="payer_individual")],
        [InlineKeyboardButton(text="🏢 Юрлицо (6%)", callback_data="payer_business")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="fin_cancel")],
    ])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="fin_cancel")]])

def report_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Неделя", callback_data="rep_week")],
        [InlineKeyboardButton(text="📅 Месяц", callback_data="rep_month")],
        [InlineKeyboardButton(text="📅 Всё", callback_data="rep_all")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="finance")],
    ])

def tasks_groups_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Критично", callback_data="grp_1")],
        [InlineKeyboardButton(text="🟡 Важно", callback_data="grp_2")],
        [InlineKeyboardButton(text="🟢 Подождать", callback_data="grp_3")],
        [InlineKeyboardButton(text="⚪ Долгосрочно", callback_data="grp_4")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main")],
    ])

def after_action_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Финансы", callback_data="finance")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main")],
    ])

def back_to_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="main")]])

def back_to_fin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="finance")]])

CONTACT_STATUSES = {"sent": "✉️ Написал", "replied": "💬 Ответил", "demo": "👀 Демо", "pilot": "🚀 Пилот", "client": "💰 Клиент", "rejected": "❌ Отказ"}

def contacts_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Мойка", callback_data="add_wash"), InlineKeyboardButton(text="➕ Студия", callback_data="add_studio")],
        [InlineKeyboardButton(text="📋 Список", callback_data="list_contacts"), InlineKeyboardButton(text="📊 Стата", callback_data="stats_contacts")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main")],
    ])

def contact_channel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 WhatsApp", callback_data="cch_whatsapp")],
        [InlineKeyboardButton(text="📱 Telegram", callback_data="cch_telegram")],
        [InlineKeyboardButton(text="📞 Звонок", callback_data="cch_call")],
        [InlineKeyboardButton(text="📘 ВК", callback_data="cch_vk")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="contact_cancel")],
    ])

def contact_status_kb():
    kb = []
    for key, name in CONTACT_STATUSES.items():
        kb.append([InlineKeyboardButton(text=name, callback_data=f"cst_{key}")])
    kb.append([InlineKeyboardButton(text="❌ Отмена", callback_data="contact_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def contact_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="contact_cancel")]])

def finance_text():
    b = fc.summary("all")
    text = "💰 <b>Финансы</b>\n\n"
    text += f"📈 Доход: <b>{fc.fmt_money(b['income_kop'])}</b>\n"
    text += f"📉 Расход: <b>{fc.fmt_money(b['expense_kop'])}</b>\n"
    text += f"🏛 Налог: <b>{fc.fmt_money(b['tax_reserve_kop'])}</b>\n"
    text += f"💸 Вывод: <b>{fc.fmt_money(b['withdrawal_kop'])}</b>\n\n"
    text += f"✅ <b>Капитал: {fc.fmt_money(b['capital_kop'])}</b>"
    return text

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("🚀 <b>Torclix Tracker</b>", reply_markup=main_kb())

@dp.callback_query(F.data == "main")
async def cb_main(call: CallbackQuery, state: FSMContext):
    await state.clear(); await call.answer()
    await call.message.edit_text("🚀 <b>Torclix Tracker</b>", reply_markup=main_kb())

@dp.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    await call.answer()
    stats = await get_stats()
    text = "📊 <b>Прогресс</b>\n\n"
    td, ta = 0, 0
    for gid, name in GROUPS.items():
        done, total = stats[gid]; td += done; ta += total
        pct = int(done / total * 100) if total else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        text += f"{name}\n{bar} {done}/{total} ({pct}%)\n\n"
    text += f"<b>Итого: {int(td/ta*100) if ta else 0}%</b>"
    await call.message.edit_text(text[:4000], reply_markup=back_to_main_kb())

@dp.callback_query(F.data == "tasks")
async def cb_tasks(call: CallbackQuery):
    await call.answer()
    stats = await get_stats()
    text = "📋 <b>Задачи</b>\n\n"
    for gid, name in GROUPS.items():
        done, total = stats[gid]
        pct = int(done / total * 100) if total else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        text += f"{name}\n{bar} {done}/{total} ({pct}%)\n\n"
    await call.message.edit_text(text[:4000], reply_markup=tasks_groups_kb())

@dp.callback_query(F.data.startswith("grp_"))
async def cb_group(call: CallbackQuery):
    gid = int(call.data.split("_")[1]); await call.answer()
    tasks = await get_group_tasks(gid)
    kb = []
    for tid, title, status in tasks:
        icon = "✅" if status == "done" else "⬜"
        short = title[:35] + "..." if len(title) > 35 else title
        kb.append([InlineKeyboardButton(text=f"{icon} {short}", callback_data=f"tg_{tid}")])
    kb.append([InlineKeyboardButton(text="⬅️ К группам", callback_data="tasks")])
    await call.message.edit_text(f"{GROUPS[gid]}\n\nНажми:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("tg_"))
async def cb_toggle(call: CallbackQuery):
    tid = int(call.data.split("_")[1])
    new = await toggle_task_by_id(tid)
    await call.answer("✅" if new == "done" else "⬜")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT group_id FROM tasks WHERE id = ?", (tid,))
        row = await cur.fetchone()
    if row:
        gid = row[0]
        tasks = await get_group_tasks(gid)
        kb = []
        for task_id, title, status in tasks:
            icon = "✅" if status == "done" else "⬜"
            short = title[:35] + "..." if len(title) > 35 else title
            kb.append([InlineKeyboardButton(text=f"{icon} {short}", callback_data=f"tg_{task_id}")])
        kb.append([InlineKeyboardButton(text="⬅️ К группам", callback_data="tasks")])
        await call.message.edit_text(f"{GROUPS[gid]}\n\nНажми:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "contacts")
async def cb_contacts(call: CallbackQuery):
    await call.answer()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM contacts")
        total = (await cur.fetchone())[0]
    await call.message.edit_text(f"📞 <b>Касания</b>\n\nВсего: <b>{total}</b>", reply_markup=contacts_kb())

@dp.callback_query(F.data == "add_wash")
async def cb_add_wash(call: CallbackQuery, state: FSMContext):
    await call.answer(); await state.set_state(ContactForm.name); await state.update_data(type="wash")
    await call.message.edit_text("🚗 Введи название:", reply_markup=contact_cancel_kb())

@dp.callback_query(F.data == "add_studio")
async def cb_add_studio(call: CallbackQuery, state: FSMContext):
    await call.answer(); await state.set_state(ContactForm.name); await state.update_data(type="studio")
    await call.message.edit_text("🏢 Введи название:", reply_markup=contact_cancel_kb())

@dp.message(ContactForm.name)
async def contact_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(ContactForm.phone)
    await message.answer("Телефон или <code>-</code>:", reply_markup=contact_cancel_kb())

@dp.message(ContactForm.phone)
async def contact_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if phone == "-": phone = ""
    await state.update_data(phone=phone); await state.set_state(ContactForm.channel)
    await message.answer("Канал:", reply_markup=contact_channel_kb())

@dp.callback_query(F.data.startswith("cch_"), ContactForm.channel)
async def contact_channel(call: CallbackQuery, state: FSMContext):
    await call.answer(); await state.update_data(channel=call.data.replace("cch_", ""))
    await state.set_state(ContactForm.status)
    await call.message.edit_text("Статус:", reply_markup=contact_status_kb())

@dp.callback_query(F.data.startswith("cst_"), ContactForm.status)
async def contact_status(call: CallbackQuery, state: FSMContext):
    status = call.data.replace("cst_", ""); await call.answer()
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO contacts (type, name, phone, channel, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (data["type"], data["name"], data.get("phone", ""), data.get("channel", ""), status, datetime.now().isoformat()))
        await db.commit()
    await state.clear()
    icon = "🚗" if data["type"] == "wash" else "🏢"
    await call.message.edit_text(f"✅ {icon} <b>{data['name']}</b>\n{CONTACT_STATUSES.get(status, status)}", reply_markup=contacts_kb())

@dp.callback_query(F.data == "list_contacts")
async def cb_list_contacts(call: CallbackQuery):
    await call.answer()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, type, name, status FROM contacts ORDER BY created_at DESC LIMIT 20")
        rows = await cur.fetchall()
    if not rows:
        await call.message.edit_text("Пусто.", reply_markup=contacts_kb()); return
    text = "📋 <b>Касания</b>\n\n"
    for r in rows:
        icon = "🚗" if r[1] == "wash" else "🏢"
        text += f"{icon} <b>{r[2]}</b> — {CONTACT_STATUSES.get(r[3], r[3])}\n"
    await call.message.edit_text(text[:4000], reply_markup=contacts_kb())

@dp.callback_query(F.data == "stats_contacts")
async def cb_stats_contacts(call: CallbackQuery):
    await call.answer()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT type, status, COUNT(*) FROM contacts GROUP BY type, status")
        rows = await cur.fetchall()
    washes = sum(r[2] for r in rows if r[0] == "wash")
    studios = sum(r[2] for r in rows if r[0] == "studio")
    text = f"📊 <b>Стата</b>\n\n🚗 Моек: <b>{washes}</b>\n🏢 Студий: <b>{studios}</b>\n\n"
    by_status = {}
    for r in rows: by_status[r[1]] = by_status.get(r[1], 0) + r[2]
    for key, name in CONTACT_STATUSES.items():
        if by_status.get(key, 0) > 0:
            text += f"{name}: <b>{by_status[key]}</b>\n"
    await call.message.edit_text(text, reply_markup=contacts_kb())

@dp.callback_query(F.data == "contact_cancel")
async def cb_contact_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear(); await call.answer("Отменено")
    await call.message.edit_text("📞 <b>Касания</b>", reply_markup=contacts_kb())

@dp.callback_query(F.data == "finance")
async def cb_finance(call: CallbackQuery, state: FSMContext):
    await state.clear(); await call.answer()
    await call.message.edit_text(finance_text(), reply_markup=finance_kb())

@dp.callback_query(F.data == "fin_income")
async def cb_fin_income(call: CallbackQuery, state: FSMContext):
    await call.answer(); await state.set_state(IncomeForm.amount)
    await call.message.edit_text("➕ <b>Доход</b>\n\nВведи сумму:", reply_markup=cancel_kb())

@dp.message(IncomeForm.amount)
async def income_amount(message: Message, state: FSMContext):
    try: kop = fc.parse_money(message.text)
    except ValueError as e: await message.answer(f"❌ {e}", reply_markup=cancel_kb()); return
    await state.update_data(amount_kop=kop); await state.set_state(IncomeForm.payer)
    await message.answer(f"Сумма: <b>{fc.fmt_money(kop)}</b>\nОт кого?", reply_markup=payer_kb())

@dp.callback_query(F.data.startswith("payer_"), IncomeForm.payer)
async def income_payer(call: CallbackQuery, state: FSMContext):
    await call.answer(); await state.update_data(payer_type=call.data.replace("payer_", ""))
    await state.set_state(IncomeForm.category)
    await call.message.edit_text("Категория или <code>-</code>:", reply_markup=cancel_kb())

@dp.message(IncomeForm.category)
async def income_category(message: Message, state: FSMContext):
    cat = message.text.strip()
    if cat == "-": cat = "other"
    data = await state.get_data()
    try: fc.add_transaction("income", data["amount_kop"], cat, data.get("payer_type", "business"))
    except Exception as e: await message.answer(f"❌ {e}", reply_markup=cancel_kb()); return
    await state.clear()
    await message.answer(f"✅ Доход: <b>{fc.fmt_money(data['amount_kop'])}</b>", reply_markup=after_action_kb())

@dp.callback_query(F.data == "fin_expense")
async def cb_fin_expense(call: CallbackQuery, state: FSMContext):
    await call.answer(); await state.set_state(ExpenseForm.amount)
    await call.message.edit_text("➖ <b>Расход</b>\n\nВведи сумму:", reply_markup=cancel_kb())

@dp.message(ExpenseForm.amount)
async def expense_amount(message: Message, state: FSMContext):
    try: kop = fc.parse_money(message.text)
    except ValueError as e: await message.answer(f"❌ {e}", reply_markup=cancel_kb()); return
    await state.update_data(amount_kop=kop); await state.set_state(ExpenseForm.category)
    await message.answer("Категория или <code>-</code>:", reply_markup=cancel_kb())

@dp.message(ExpenseForm.category)
async def expense_category(message: Message, state: FSMContext):
    cat = message.text.strip()
    if cat == "-": cat = "other"
    data = await state.get_data()
    try: fc.add_transaction("expense", data["amount_kop"], cat, "business")
    except Exception as e: await message.answer(f"❌ {e}", reply_markup=cancel_kb()); return
    await state.clear()
    await message.answer(f"✅ Расход: <b>{fc.fmt_money(data['amount_kop'])}</b>", reply_markup=after_action_kb())

@dp.callback_query(F.data == "fin_withdraw")
async def cb_fin_withdraw(call: CallbackQuery, state: FSMContext):
    await call.answer(); await state.set_state(WithdrawForm.amount)
    await call.message.edit_text("💸 <b>Вывод</b>\n\nВведи сумму:", reply_markup=cancel_kb())

@dp.message(WithdrawForm.amount)
async def withdraw_amount(message: Message, state: FSMContext):
    try: kop = fc.parse_money(message.text)
    except ValueError as e: await message.answer(f"❌ {e}", reply_markup=cancel_kb()); return
    data = await state.get_data()
    try: fc.add_transaction("withdrawal", kop, "owner", "business")
    except Exception as e: await message.answer(f"❌ {e}", reply_markup=cancel_kb()); return
    await state.clear()
    await message.answer(f"✅ Вывод: <b>{fc.fmt_money(kop)}</b>", reply_markup=after_action_kb())

@dp.callback_query(F.data == "fin_report")
async def cb_fin_report(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text("📊 <b>Отчёт</b>\n\nВыбери период:", reply_markup=report_kb())

@dp.callback_query(F.data.startswith("rep_"))
async def cb_report_period(call: CallbackQuery):
    period = call.data.replace("rep_", ""); await call.answer()
    b = fc.summary(period)
    text = f"📊 <b>Отчёт ({period})</b>\n\n"
    text += f"📈 Доход: <b>{fc.fmt_money(b['income_kop'])}</b>\n"
    text += f"📉 Расход: <b>{fc.fmt_money(b['expense_kop'])}</b>\n"
    text += f"🏛 Налог: <b>{fc.fmt_money(b['tax_reserve_kop'])}</b>\n"
    text += f"💸 Вывод: <b>{fc.fmt_money(b['withdrawal_kop'])}</b>\n\n"
    text += f"✅ <b>Капитал: {fc.fmt_money(b['capital_kop'])}</b>"
    await call.message.edit_text(text, reply_markup=report_kb())

@dp.callback_query(F.data == "fin_history")
async def cb_fin_history(call: CallbackQuery):
    await call.answer()
    try:
        rows = fc.history(limit=15)
    except Exception:
        rows = []
    if not rows:
        await call.message.edit_text("📭 История пуста", reply_markup=back_to_fin_kb()); return
    text = "📋 <b>История</b>\n\n"
    for r in rows:
        sign = "➕" if r.get("kind") == "income" else "➖"
        text += f"{sign} {fc.fmt_money(r.get('amount_kop', 0))} — {r.get('category', '-')}\n"
    await call.message.edit_text(text[:4000], reply_markup=back_to_fin_kb())

@dp.callback_query(F.data == "fin_undo")
async def cb_fin_undo(call: CallbackQuery):
    await call.answer()
    try:
        ok = fc.undo_last()
    except Exception:
        ok = False
    if ok:
        await call.message.edit_text("↩️ Последняя операция отменена", reply_markup=finance_kb())
    else:
        await call.message.edit_text("Нечего отменять", reply_markup=finance_kb())

@dp.callback_query(F.data == "fin_cancel")
async def cb_fin_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear(); await call.answer("Отменено")
    await call.message.edit_text(finance_text(), reply_markup=finance_kb())

# ---------- FastAPI / webhook ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    fc.init_db()
    await seed_tasks()
    url = os.getenv("RENDER_EXTERNAL_URL")
    if url:
        try:
            await bot.set_webhook(f"{url}/webhook")
        except Exception as e:
            print("Webhook set error:", e)
    yield
    await bot.session.close()

app = FastAPI(lifespan=lifespan)
app.include_router(finance_router)

@app.post("/webhook")
async def webhook(update: dict):
    try:
        tg_update = Update.model_validate(update, context={"bot": bot})
        await dp.feed_update(bot, tg_update)
    except Exception as e:
        print("Update error:", e)
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1>Torclix Tracker</h1><p>OK</p>"

@app.get("/finance", response_class=HTMLResponse)
async def finance_page():
    return FileResponse("finance.html")

@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page():
    return FileResponse("index.html")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
