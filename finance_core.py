"""finance_core.py: ядро финансового учёта. Только стандартная библиотека Python 3.9+.

Что здесь есть:
  - база SQLite (путь задаётся переменной DB_PATH, по умолчанию рядом с этим файлом);
  - деньги хранятся в КОПЕЙКАХ (целые числа), без float;
  - доходы, расходы и выводы владельца как три разных типа операций;
  - резерв налога для самозанятого: 4% с оплат от физлиц, 6% от юрлиц и ИП
    (проверяй актуальные ставки на nalog.gov.ru, они меняются законом);
  - капитал, отчёт за период, точка безубыточности, прогресс цели;
  - проверка подписи Telegram initData (чтобы чужие не читали твои финансы).
"""
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qsl

# Абсолютный путь: не зависит от того, из какой папки запущен бот.
DB_PATH = os.getenv("DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tracker.db"
)

TAX_BP = {"individual": 400, "business": 600}  # базисные пункты: 400 = 4%
DEFAULT_PAYER = "business"  # по умолчанию резервируем больше: безопаснее
KINDS = ("income", "expense", "withdrawal")
PERIODS = ("week", "month", "all")
PERIOD_DAYS = {"week": 7, "month": 30}
MAX_KOP = 10 ** 11  # 1 млрд рублей: защита от опечаток

DEFAULT_SETTINGS = {
    "opening_balance_kop": 0,  # сколько денег уже было в бизнесе на старте
    "fixed_monthly_kop": 0,    # постоянные расходы в месяц
    "price_kop": 0,            # цена одной продажи
    "var_cost_kop": 0,         # переменные расходы на одну продажу
    "goal_kop": 0,             # цель по капиталу
    "goal_name": "",
}
INT_SETTINGS = [k for k, v in DEFAULT_SETTINGS.items() if isinstance(v, int)]


class AuthError(ValueError):
    """Данные Telegram не прошли проверку."""


# ---------------------------------------------------------------- база данных
@contextmanager
def connect(path=None):
    path = path or DB_PATH
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path=None):
    with connect(path) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS fin_transactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK(kind IN ('income','expense','withdrawal')),
                amount_kop INTEGER NOT NULL CHECK(amount_kop > 0),
                category TEXT NOT NULL DEFAULT 'other',
                payer_type TEXT CHECK(payer_type IN ('individual','business')),
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS ix_fin_created ON fin_transactions(created_at)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS fin_settings("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )


# ---------------------------------------------------------------- деньги
_MONEY_RE = re.compile(r"^\d{1,10}(\.\d{1,2})?$")


def parse_money(value, allow_zero=False):
    """'3500', '3 500,50', '3500₽' -> копейки (int). Бросает ValueError."""
    s = str(value).strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    s = s.rstrip("₽")
    if not _MONEY_RE.match(s):
        raise ValueError("Сумма: число, например 3500 или 3500,50")
    kop = int((Decimal(s) * 100).to_integral_value(ROUND_HALF_UP))
    if kop > MAX_KOP:
        raise ValueError("Слишком большая сумма")
    if kop == 0 and not allow_zero:
        raise ValueError("Сумма должна быть больше нуля")
    return kop


def fmt_money(kop):
    sign = "-" if kop < 0 else ""
    rub, k = divmod(abs(int(kop)), 100)
    text = f"{rub:,}".replace(",", " ")
    return f"{sign}{text}" + (f",{k:02d}" if k else "") + " ₽"


def tax_kop(amount_kop, payer):
    """Налог с одной оплаты, округление до копейки вверх от .5."""
    return (amount_kop * TAX_BP[payer] + 5000) // 10000


def _now_iso(now=None):
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- операции
def add_transaction(kind, amount_kop, category="other", payer_type=None,
                    note="", created_at=None, path=None):
    if kind not in KINDS:
        raise ValueError("Тип операции: income, expense или withdrawal")
    if not isinstance(amount_kop, int) or not 0 < amount_kop <= MAX_KOP:
        raise ValueError("Некорректная сумма")
    category = (category or "other").strip()[:40] or "other"
    note = (note or "").strip()[:200]
    if kind == "income":
        payer_type = payer_type or DEFAULT_PAYER
        if payer_type not in TAX_BP:
            raise ValueError("Плательщик: individual (физлицо) или business (юрлицо/ИП)")
    else:
        payer_type = None
    with connect(path) as c:
        cur = c.execute(
            "INSERT INTO fin_transactions(kind, amount_kop, category, payer_type, note, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (kind, amount_kop, category, payer_type, note, created_at or _now_iso()),
        )
        return cur.lastrowid


def delete_transaction(tid, path=None):
    with connect(path) as c:
        return c.execute("DELETE FROM fin_transactions WHERE id = ?", (tid,)).rowcount > 0


def undo_last(path=None):
    """Удаляет последнюю операцию и возвращает её, либо None."""
    with connect(path) as c:
        row = c.execute("SELECT * FROM fin_transactions ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        c.execute("DELETE FROM fin_transactions WHERE id = ?", (row["id"],))
        return dict(row)


def list_transactions(limit=50, offset=0, path=None):
    with connect(path) as c:
        rows = c.execute(
            "SELECT id, kind, amount_kop, category, payer_type, note, created_at"
            " FROM fin_transactions ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- настройки
def get_settings(path=None):
    result = dict(DEFAULT_SETTINGS)
    with connect(path) as c:
        for row in c.execute("SELECT key, value FROM fin_settings"):
            if row["key"] in INT_SETTINGS:
                result[row["key"]] = int(row["value"])
            elif row["key"] in result:
                result[row["key"]] = row["value"]
    return result


def set_settings(updates, path=None):
    clean = {}
    for key, value in updates.items():
        if key in INT_SETTINGS:
            if not isinstance(value, int) or not 0 <= value <= MAX_KOP:
                raise ValueError(f"Некорректное значение: {key}")
            clean[key] = str(value)
        elif key == "goal_name":
            clean[key] = str(value).strip()[:60]
        else:
            raise ValueError(f"Неизвестная настройка: {key}")
    with connect(path) as c:
        for key, value in clean.items():
            c.execute(
                "INSERT INTO fin_settings(key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    return get_settings(path)


# ---------------------------------------------------------------- расчёты
def _totals(rows):
    income = tax = expense = withdrawal = 0
    for r in rows:
        if r["kind"] == "income":
            income += r["amount_kop"]
            tax += tax_kop(r["amount_kop"], r["payer_type"] or DEFAULT_PAYER)
        elif r["kind"] == "expense":
            expense += r["amount_kop"]
        else:
            withdrawal += r["amount_kop"]
    return income, tax, expense, withdrawal


def period_start(period, now=None):
    if period == "all":
        return None
    now = now or datetime.now(timezone.utc)
    return _now_iso(now - timedelta(days=PERIOD_DAYS[period]))


def summary(period="month", path=None, now=None):
    """Итоги за период + капитал за всё время + прогресс цели."""
    if period not in PERIODS:
        raise ValueError("Период: week, month или all")
    start = period_start(period, now)
    with connect(path) as c:
        if start:
            rows = c.execute(
                "SELECT kind, amount_kop, category, payer_type FROM fin_transactions"
                " WHERE created_at >= ?", (start,)).fetchall()
        else:
            rows = c.execute(
                "SELECT kind, amount_kop, category, payer_type FROM fin_transactions").fetchall()
        all_rows = c.execute(
            "SELECT kind, amount_kop, payer_type FROM fin_transactions").fetchall()
    st = get_settings(path)

    income, tax, expense, withdrawal = _totals(rows)
    a_income, a_tax, a_expense, a_withdrawal = _totals(all_rows)
    capital = st["opening_balance_kop"] + a_income - a_tax - a_expense - a_withdrawal

    by_cat = {"income": {}, "expense": {}}
    for r in rows:
        if r["kind"] in by_cat:
            by_cat[r["kind"]][r["category"]] = by_cat[r["kind"]].get(r["category"], 0) + r["amount_kop"]

    goal = None
    if st["goal_kop"] > 0:
        pct = max(0, min(100, int(capital * 100 / st["goal_kop"])))
        goal = {"name": st["goal_name"], "goal_kop": st["goal_kop"], "progress_pct": pct,
                "left_kop": max(0, st["goal_kop"] - capital)}

    return {
        "period": period,
        "income_kop": income,
        "tax_reserve_kop": tax,
        "expense_kop": expense,
        "withdrawal_kop": withdrawal,
        "net_profit_kop": income - tax - expense,
        "capital_kop": capital,
        "by_category": by_cat,
        "goal": goal,
    }


def breakeven(price_kop, var_cost_kop, fixed_monthly_kop, payer=DEFAULT_PAYER):
    """Сколько продаж в месяц нужно, чтобы покрыть постоянные расходы.

    Вклад одной продажи = цена - переменные расходы - налог с неё.
    Точка безубыточности = постоянные расходы / вклад (округляем вверх).
    """
    tax = tax_kop(price_kop, payer)
    contribution = price_kop - var_cost_kop - tax
    result = {
        "price_kop": price_kop, "var_cost_kop": var_cost_kop, "tax_per_sale_kop": tax,
        "contribution_kop": contribution, "fixed_monthly_kop": fixed_monthly_kop,
        "breakeven_sales_per_month": None, "note": "",
    }
    if price_kop <= 0:
        result["note"] = "Укажи цену одной продажи"
    elif contribution <= 0:
        result["note"] = "С одной продажи ничего не остаётся после расходов и налога"
    else:
        result["breakeven_sales_per_month"] = (
            0 if fixed_monthly_kop == 0 else math.ceil(fixed_monthly_kop / contribution))
    return result


def breakeven_from_settings(path=None, payer=DEFAULT_PAYER):
    st = get_settings(path)
    return breakeven(st["price_kop"], st["var_cost_kop"], st["fixed_monthly_kop"], payer)


def export_csv(path=None):
    """Все операции одним CSV: для бэкапа и бухгалтерии."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "created_at_utc", "kind", "amount_rub", "category", "payer_type", "note"])
    for r in reversed(list_transactions(limit=10 ** 6, path=path)):
        writer.writerow([r["id"], r["created_at"], r["kind"], f"{r['amount_kop'] / 100:.2f}",
                         r["category"], r["payer_type"] or "", r["note"]])
    return buf.getvalue()


# ---------------------------------------------------------------- Telegram
def validate_init_data(init_data, bot_token, max_age_sec=86400, now=None):
    """Проверяет подпись initData Mini App и возвращает словарь user.

    Алгоритм из документации Telegram: ключ = HMAC_SHA256("WebAppData", токен бота),
    подпись = HMAC_SHA256(ключ, строка "ключ=значение" по алфавиту через \\n без hash).
    """
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        raise AuthError("Не удалось разобрать initData")
    received = pairs.pop("hash", None)
    if not received:
        raise AuthError("Нет подписи")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthError("Подпись не совпала")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise AuthError("Некорректная дата")
    if (now if now is not None else time.time()) - auth_date > max_age_sec:
        raise AuthError("Данные устарели, открой приложение заново")
    try:
        return json.loads(pairs["user"])
    except (KeyError, ValueError):
        raise AuthError("Нет данных пользователя")
