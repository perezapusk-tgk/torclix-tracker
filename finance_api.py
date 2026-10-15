
Finance api · PY
"""finance_api.py: API для Mini App. Подключается одной строкой: app.include_router(router)
 
Переменные окружения (обязательны):
  BOT_TOKEN  токен бота из @BotFather (нужен, чтобы проверять подпись Telegram)
  OWNER_ID   твой числовой Telegram id (узнать: бот @userinfobot). Только он видит финансы.
"""
import asyncio
import os
from typing import Optional
 
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
 
import finance_core as fc
 
router = APIRouter(prefix="/api/finance", tags=["finance"])
 
 
def _env(name):
    value = os.getenv(name)
    if not value:
        raise HTTPException(500, f"На сервере не задана переменная {name}")
    return value
 
 
async def owner_only(authorization: str = Header(default="")):
    """Каждый запрос должен нести подпись Telegram: Authorization: tma <initData>."""
    if not authorization.startswith("tma "):
        raise HTTPException(401, "Открой приложение из Telegram")
    try:
        user = fc.validate_init_data(authorization[4:], _env("BOT_TOKEN"))
    except fc.AuthError as e:
        raise HTTPException(401, str(e))
    if int(user.get("id", 0)) != int(_env("OWNER_ID")):
        raise HTTPException(403, "Доступ только владельцу")
    return user
 
 
class TxIn(BaseModel):
    kind: str                       # income | expense | withdrawal
    amount: str                     # "3500" или "3 500,50"
    category: str = "other"
    payer_type: Optional[str] = None  # individual | business (только для дохода)
    note: str = ""
 
 
class SettingsIn(BaseModel):
    opening_balance: Optional[str] = None
    fixed_monthly: Optional[str] = None
    price: Optional[str] = None
    var_cost: Optional[str] = None
    goal: Optional[str] = None
    goal_name: Optional[str] = None
 
 
_RUB_FIELDS = {
    "opening_balance": "opening_balance_kop",
    "fixed_monthly": "fixed_monthly_kop",
    "price": "price_kop",
    "var_cost": "var_cost_kop",
    "goal": "goal_kop",
}
 
 
@router.get("/summary")
async def summary(period: str = "month", _=Depends(owner_only)):
    try:
        return await asyncio.to_thread(fc.summary, period)
    except ValueError as e:
        raise HTTPException(400, str(e))
 
 
@router.get("/transactions")
async def transactions(limit: int = 50, offset: int = 0, _=Depends(owner_only)):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    return await asyncio.to_thread(fc.list_transactions, limit, offset)
 
 
@router.post("/transactions")
async def add_transaction(tx: TxIn, _=Depends(owner_only)):
    try:
        kop = fc.parse_money(tx.amount)
        tid = await asyncio.to_thread(
            fc.add_transaction, tx.kind, kop, tx.category, tx.payer_type, tx.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": tid}
 
 
@router.delete("/transactions/{tid}")
async def delete_transaction(tid: int, _=Depends(owner_only)):
    if not await asyncio.to_thread(fc.delete_transaction, tid):
        raise HTTPException(404, "Операция не найдена")
    return {"ok": True}
 
 
@router.get("/settings")
async def get_settings(_=Depends(owner_only)):
    return await asyncio.to_thread(fc.get_settings)
 
 
@router.post("/settings")
async def save_settings(body: SettingsIn, _=Depends(owner_only)):
    updates = {}
    try:
        for field, key in _RUB_FIELDS.items():
            raw = getattr(body, field)
            if raw is not None and str(raw).strip() != "":
                updates[key] = fc.parse_money(raw, allow_zero=True)
        if body.goal_name is not None:
            updates["goal_name"] = body.goal_name
        return await asyncio.to_thread(fc.set_settings, updates)
    except ValueError as e:
        raise HTTPException(400, str(e))
 
 
@router.get("/breakeven")
async def breakeven(payer: str = fc.DEFAULT_PAYER, _=Depends(owner_only)):
    if payer not in fc.TAX_BP:
        raise HTTPException(400, "payer: individual или business")
    return await asyncio.to_thread(fc.breakeven_from_settings, None, payer)
 

