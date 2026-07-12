import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import ALLOWED_TELEGRAM_USER_IDS, TELEGRAM_BOT_TOKEN, UMAG_PASSWORD, UMAG_PHONE
from nlu import parse_message
from umag_client import UmagClient, UmagError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("umag_bot")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

umag = UmagClient(UMAG_PHONE, UMAG_PASSWORD)

# pending confirmations: token -> parsed action dict
_pending: dict[str, dict] = {}


class NewProduct(StatesGroup):
    name = State()
    arrival_cost = State()
    selling_price = State()
    category = State()


class CashCheck(StatesGroup):
    actual_cash = State()


def _allowed(message: Message) -> bool:
    if not ALLOWED_TELEGRAM_USER_IDS:
        return True
    return message.from_user.id in ALLOWED_TELEGRAM_USER_IDS


def _ensure_login():
    if umag.store_id is None:
        umag.login()
        umag.ensure_store()


def _parse_number(text: str) -> float | None:
    cleaned = text.strip().replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Напиши, например:\n"
        "«списать 2 пирожных тирамису, испортились»\n"
        "«оприходовать 10 кофе капучино по 800»\n"
        "«добавить новый товар»\n"
        "«отчёт по кассе за сегодня»"
    )


@dp.message(StateFilter(None), F.text)
async def handle_text(message: Message, state: FSMContext):
    if not _allowed(message):
        await message.answer("У вас нет доступа к этому боту.")
        return

    try:
        _ensure_login()
    except UmagError as e:
        await message.answer(f"Не удалось подключиться к UMAG: {e}")
        return

    parsed = parse_message(message.text)
    action = parsed.get("action")

    if action == "cash_report":
        await _handle_cash_report(message, parsed, state)
    elif action in ("decommission", "debit"):
        await _handle_stock_action(message, parsed)
    elif action == "create_product":
        await _start_create_product(message, state, parsed)
    else:
        await message.answer(
            "Не понял запрос. Попробуй сформулировать как:\n"
            "«списать 1 шт. <товар>», «оприходовать 5 <товар> по 1200», "
            "«добавить новый товар» или «отчёт по кассе за 3 дня»."
        )


CASH_ACCOUNT_NAME = "Касса-1"


async def _handle_cash_report(message: Message, parsed: dict, state: FSMContext):
    days = int(parsed.get("period_days") or 1)
    date_to = datetime.now()
    date_from = (date_to - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        pnl = umag.profit_and_loss(date_from, date_to)
        cash_balance = umag.get_cash_account_balance(CASH_ACCOUNT_NAME)
    except UmagError as e:
        await message.answer(f"Ошибка получения отчёта: {e}")
        return

    pr = pnl.get("profitReport", {})
    revenue = pr.get("revenueAmount", 0)
    cash_sales = pr.get("saleCashAmount", 0)
    bank_sales = pr.get("saleBankAmount", 0)

    expenses = [e for e in pnl.get("expenses", []) if e.get("amount")]
    total_expense = sum(e["amount"] for e in expenses)

    decommissions = [d for d in pnl.get("decommissionSums", []) if d.get("amount")]
    total_decom = sum(d["amount"] for d in decommissions)

    lines = [
        f"Отчёт по кассе с {date_from.strftime('%d.%m')} по {date_to.strftime('%d.%m %H:%M')}:\n",
        f"Выручка: {revenue:,.0f} ₸ (нал {cash_sales:,.0f} / безнал {bank_sales:,.0f})",
    ]
    if expenses:
        lines.append("\nРасходы:")
        for e in expenses:
            lines.append(f"  {e['name']}: {e['amount']:,.0f} ₸")
        lines.append(f"Итого расходов: {total_expense:,.0f} ₸")
    if decommissions:
        lines.append(f"\nСписания: {total_decom:,.0f} ₸")

    if cash_balance is not None:
        lines.append(f"\n💰 Текущий остаток по системе ({CASH_ACCOUNT_NAME}): {cash_balance:,.0f} ₸")
        await state.update_data(expected_cash=cash_balance)
        await state.set_state(CashCheck.actual_cash)
        lines.append("\nСколько по факту наличных в кассе? (напиши число, или \"пропустить\")")
    else:
        lines.append(f"\n(счёт «{CASH_ACCOUNT_NAME}» не найден — сверка остатка недоступна)")

    await message.answer("\n".join(lines))


@dp.message(CashCheck.actual_cash, F.text)
async def cash_check_entered(message: Message, state: FSMContext):
    if message.text.strip().lower() in ("пропустить", "skip", "-"):
        await state.clear()
        await message.answer("Ок, без сверки.")
        return

    actual = _parse_number(message.text)
    if actual is None:
        await message.answer("Не понял число. Введи фактический остаток наличных цифрами, или \"пропустить\".")
        return

    data = await state.get_data()
    expected = data.get("expected_cash", 0)
    diff = actual - expected
    await state.clear()

    if abs(diff) < 1:
        await message.answer(f"Сходится ✅ (факт {actual:,.0f} ₸)")
    elif diff > 0:
        await message.answer(f"Излишек: +{diff:,.0f} ₸ (по системе {expected:,.0f}, по факту {actual:,.0f})")
    else:
        await message.answer(f"Недостача: {diff:,.0f} ₸ (по системе {expected:,.0f}, по факту {actual:,.0f})")


async def _handle_stock_action(message: Message, parsed: dict):
    product_query = parsed.get("product_query")
    if not product_query:
        await message.answer("Не понял, какой товар. Укажи название явно.")
        return

    matches = umag.search_product(product_query)
    if not matches:
        await message.answer(f"Товар «{product_query}» не найден в номенклатуре.")
        return

    product = matches[0]
    quantity = parsed.get("quantity") or 1
    action = parsed["action"]
    price = product.get("arrivalCost") or product.get("sellingPrice") or 0

    token = f"{message.from_user.id}:{message.message_id}"
    _pending[token] = {
        "kind": "stock_action",
        "action": action,
        "barcode": product["barcode"],
        "name": product["name"],
        "quantity": quantity,
        "price": price,
        "comment": parsed.get("comment", "") or "",
    }

    verb = "Списать" if action == "decommission" else "Оприходовать"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, подтверждаю", callback_data=f"confirm:{token}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"cancel:{token}"),
            ]
        ]
    )
    await message.answer(
        f"{verb} «{product['name']}» — {quantity} {product.get('measureName', 'шт.')} "
        f"по {price}?",
        reply_markup=kb,
    )


# ------------------------------------------------------- create new product

async def _start_create_product(message: Message, state: FSMContext, parsed: dict):
    await state.update_data(
        name=parsed.get("product_name"),
        arrival_cost=parsed.get("arrival_cost"),
        selling_price=parsed.get("selling_price"),
        category_name=parsed.get("category_name"),
    )
    await _advance_product_creation(message, state)


async def _advance_product_creation(message: Message, state: FSMContext):
    data = await state.get_data()

    if not data.get("name"):
        await state.set_state(NewProduct.name)
        await message.answer("Как называется новый товар?")
        return

    if data.get("arrival_cost") is None:
        await state.set_state(NewProduct.arrival_cost)
        await message.answer("Закупочная цена (тенге)?")
        return

    if data.get("selling_price") is None:
        await state.set_state(NewProduct.selling_price)
        await message.answer("Продажная цена (тенге)?")
        return

    if not data.get("category_id"):
        await state.set_state(NewProduct.category)
        if data.get("category_name"):
            cat = umag.find_category_by_name(data["category_name"])
            if cat:
                await state.update_data(category_id=cat["id"], category_name=cat["name"])
                await _advance_product_creation(message, state)
                return
            await message.answer(f"Категория «{data['category_name']}» не найдена. Попробуй ещё раз, например:")
        else:
            await message.answer("Какая категория?")
        categories = umag.list_categories()
        names = ", ".join(c["name"] for c in categories[:30])
        await message.answer(f"Доступные категории: {names}")
        return

    # all fields present -> confirm
    token = f"{message.from_user.id}:{message.message_id}"
    _pending[token] = {
        "kind": "create_product",
        "name": data["name"],
        "arrival_cost": data["arrival_cost"],
        "selling_price": data["selling_price"],
        "category_id": data["category_id"],
        "category_name": data["category_name"],
    }
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, создать", callback_data=f"confirm:{token}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"cancel:{token}"),
            ]
        ]
    )
    await message.answer(
        f"Создать товар «{data['name']}»?\n"
        f"Категория: {data['category_name']}\n"
        f"Закупочная цена: {data['arrival_cost']}\n"
        f"Продажная цена: {data['selling_price']}",
        reply_markup=kb,
    )
    await state.clear()


@dp.message(NewProduct.name, F.text)
async def product_name_entered(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await _advance_product_creation(message, state)


@dp.message(NewProduct.arrival_cost, F.text)
async def product_arrival_cost_entered(message: Message, state: FSMContext):
    value = _parse_number(message.text)
    if value is None:
        await message.answer("Не понял число. Введи закупочную цену цифрами, например 500.")
        return
    await state.update_data(arrival_cost=value)
    await _advance_product_creation(message, state)


@dp.message(NewProduct.selling_price, F.text)
async def product_selling_price_entered(message: Message, state: FSMContext):
    value = _parse_number(message.text)
    if value is None:
        await message.answer("Не понял число. Введи продажную цену цифрами, например 800.")
        return
    await state.update_data(selling_price=value)
    await _advance_product_creation(message, state)


@dp.message(NewProduct.category, F.text)
async def product_category_entered(message: Message, state: FSMContext):
    cat = umag.find_category_by_name(message.text.strip())
    if not cat:
        categories = umag.list_categories()
        names = ", ".join(c["name"] for c in categories[:30])
        await message.answer(f"Не нашёл такую категорию. Доступные: {names}")
        return
    await state.update_data(category_id=cat["id"], category_name=cat["name"])
    await _advance_product_creation(message, state)


@dp.callback_query(F.data.startswith("confirm:"))
async def confirm_action(callback: CallbackQuery):
    token = callback.data.split(":", 1)[1]
    pending = _pending.pop(token, None)
    if not pending:
        await callback.answer("Запрос устарел.")
        return

    try:
        _ensure_login()
        if pending["kind"] == "create_product":
            umag.create_product(
                name=pending["name"],
                arrival_cost=pending["arrival_cost"],
                selling_price=pending["selling_price"],
                category_id=pending["category_id"],
            )
            await callback.message.edit_text(f"✅ Товар «{pending['name']}» создан.")
        elif pending["action"] == "decommission":
            product_line = {
                "barcode": pending["barcode"],
                "quantity": pending["quantity"],
                "price": pending["price"],
                "comment": pending["comment"],
                "type": 1,  # "Испорченный"
            }
            doc = umag.create_decommission()
            umag.add_decommission_products(doc["id"], [product_line])
            umag.provide_decommission(doc["id"])
            await callback.message.edit_text(f"✅ Проведено (№{doc['id']}).")
        else:
            product_line = {
                "barcode": pending["barcode"],
                "quantity": pending["quantity"],
            }
            doc = umag.create_debit()
            umag.add_debit_products(doc["id"], [product_line])
            umag.provide_debit(doc["id"])
            await callback.message.edit_text(f"✅ Проведено (№{doc['id']}).")
    except UmagError as e:
        await callback.message.edit_text(f"Ошибка: {e}")
    await callback.answer()


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_action(callback: CallbackQuery):
    token = callback.data.split(":", 1)[1]
    _pending.pop(token, None)
    await callback.message.edit_text("Отменено.")
    await callback.answer()


async def _run_health_server():
    """Render's free Web Service tier requires an open $PORT to stay up."""
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", lambda _req: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health check server listening on port %s", port)


async def main():
    await _run_health_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
