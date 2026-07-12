import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiohttp import web

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from config import ALLOWED_TELEGRAM_USER_IDS, TELEGRAM_BOT_TOKEN, UMAG_PASSWORD, UMAG_PHONE
from nlu import parse_message, parse_product_list
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
    morning_cash = State()
    evening_cash = State()
    custom_date = State()


class StockFlow(StatesGroup):
    product = State()
    comment = State()


# last confirmed "Деньги вечером" per chat, used as next day's "Деньги утром"
_last_evening_cash: dict[int, float] = {}


def _allowed_user_id(user_id: int) -> bool:
    if not ALLOWED_TELEGRAM_USER_IDS:
        return True
    return user_id in ALLOWED_TELEGRAM_USER_IDS


class AccessMiddleware(BaseMiddleware):
    """Blocks every message/button tap from users not in ALLOWED_TELEGRAM_USER_IDS."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user and not _allowed_user_id(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("У вас нет доступа к этому боту.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("У вас нет доступа к этому боту.")
            return
        return await handler(event, data)


dp.message.outer_middleware(AccessMiddleware())
dp.callback_query.outer_middleware(AccessMiddleware())


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


def _main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📤 Списать товар", callback_data="menu:decommission"),
                InlineKeyboardButton(text="📥 Оприходовать", callback_data="menu:debit"),
            ],
            [
                InlineKeyboardButton(text="🆕 Новый товар", callback_data="menu:create_product"),
                InlineKeyboardButton(text="💰 Отчёт по кассе", callback_data="menu:cash_report"),
            ],
        ]
    )


async def _show_main_menu(message: Message, text: str = "Что делаем?"):
    await message.answer(text, reply_markup=_main_menu_markup())


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Я помогу вести учёт в UMAG.\n\n"
        "Выбери действие кнопкой ниже, или просто напиши текстом, например:\n"
        "«списать 2 пирожных тирамису, испортились»"
    )
    await _show_main_menu(message)


@dp.message(F.text == "/menu")
async def menu_command(message: Message, state: FSMContext):
    await state.clear()
    await _show_main_menu(message)


@dp.message(StateFilter(None), F.text)
async def handle_text(message: Message, state: FSMContext):
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


def _day_bounds(parsed: dict) -> tuple[datetime, datetime]:
    if parsed.get("specific_date"):
        d = datetime.strptime(parsed["specific_date"], "%Y-%m-%d")
        return d.replace(hour=0, minute=0, second=0, microsecond=0), d.replace(
            hour=23, minute=59, second=59, microsecond=999000
        )
    days = int(parsed.get("period_days") or 1)
    target = (datetime.now() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return target, target.replace(hour=23, minute=59, second=59, microsecond=999000)


async def _handle_cash_report(message: Message, parsed: dict, state: FSMContext):
    date_from, date_to = _day_bounds(parsed)

    try:
        pnl = umag.profit_and_loss(date_from, date_to)
    except UmagError as e:
        await message.answer(f"Ошибка получения отчёта: {e}")
        return

    pr = pnl.get("profitReport", {})
    revenue = pr.get("revenueAmount", 0)

    expenses = [e for e in pnl.get("expenses", []) if e.get("amount")]
    kr_expense = sum(e["amount"] for e in expenses if "кр" in e["name"].lower())
    other_expense = sum(e["amount"] for e in expenses) - kr_expense

    chat_id = message.chat.id
    morning_default = _last_evening_cash.get(chat_id)

    await state.update_data(
        date_label=date_from.strftime("%d.%m.%Y"),
        revenue=revenue,
        other_expense=other_expense,
        kr_expense=kr_expense,
        morning_cash=morning_default,
    )

    if morning_default is not None:
        await state.set_state(CashCheck.evening_cash)
        await message.answer(
            f"Дата: {date_from.strftime('%d.%m.%Y')}\n"
            f"📍Деньги утром: {morning_default:,.0f} тг (взято из вчерашнего вечера)\n"
            f"📍Выручка: {revenue:,.0f} тг\n"
            f"📍Расходы: {other_expense:,.0f} тг\n"
            f"📍Кр оплата: {kr_expense:,.0f} тг\n\n"
            f"Сколько денег в кассе вечером по факту?"
        )
    else:
        await state.set_state(CashCheck.morning_cash)
        await message.answer(
            f"Дата: {date_from.strftime('%d.%m.%Y')}\n"
            f"📍Выручка: {revenue:,.0f} тг\n"
            f"📍Расходы: {other_expense:,.0f} тг\n"
            f"📍Кр оплата: {kr_expense:,.0f} тг\n\n"
            f"Сколько денег было в кассе утром?"
        )


@dp.message(CashCheck.morning_cash, F.text)
async def cash_morning_entered(message: Message, state: FSMContext):
    value = _parse_number(message.text)
    if value is None:
        await message.answer("Не понял число. Введи сумму цифрами.")
        return
    await state.update_data(morning_cash=value)
    await state.set_state(CashCheck.evening_cash)
    await message.answer("Сколько денег в кассе вечером по факту?")


@dp.message(CashCheck.evening_cash, F.text)
async def cash_evening_entered(message: Message, state: FSMContext):
    evening = _parse_number(message.text)
    if evening is None:
        await message.answer("Не понял число. Введи сумму цифрами.")
        return

    data = await state.get_data()
    morning = data["morning_cash"]
    revenue = data["revenue"]
    other_expense = data["other_expense"]
    kr_expense = data["kr_expense"]
    expected_evening = morning + revenue - other_expense - kr_expense
    diff = evening - expected_evening

    _last_evening_cash[message.chat.id] = evening
    await state.clear()

    await message.answer(
        f"Дата: {data['date_label']}\n"
        f"📍Деньги утром: {morning:,.0f} тг\n"
        f"📍Выручка: {revenue:,.0f} тг\n"
        f"📍Расходы: {other_expense:,.0f} тг\n"
        f"📍Кр оплата: {kr_expense:,.0f} тг\n"
        f"📍Деньги вечером: {evening:,.0f} тг\n"
        f"Изл/нед: {diff:+,.0f} тг"
    )
    await _show_main_menu(message)


@dp.message(CashCheck.custom_date, F.text)
async def cash_custom_date_entered(message: Message, state: FSMContext):
    parsed = parse_message(f"отчёт по кассе за {message.text}")
    if parsed.get("action") != "cash_report" or not (parsed.get("specific_date") or parsed.get("period_days")):
        await message.answer("Не понял дату. Напиши, например: 11 июля или 2026-07-11.")
        return
    await _handle_cash_report(message, parsed, state)


def _period_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="period:today"),
                InlineKeyboardButton(text="Вчера", callback_data="period:yesterday"),
            ],
            [
                InlineKeyboardButton(text="3 дня", callback_data="period:3days"),
                InlineKeyboardButton(text="📅 Указать дату", callback_data="period:customdate"),
            ],
        ]
    )


def _build_cart(text: str) -> tuple[list[dict], list[str]]:
    """Parses free text (one product or a multi-line list) into resolved
    UMAG products. Returns (cart, not_found_queries)."""
    items = parse_product_list(text)
    cart: list[dict] = []
    not_found: list[str] = []
    for item in items:
        query = (item.get("product_query") or "").strip()
        if not query:
            continue
        quantity = item.get("quantity") or 1
        matches = umag.search_product(query)
        if not matches:
            not_found.append(query)
            continue
        product = matches[0]
        cart.append(
            {
                "barcode": product["barcode"],
                "name": product["name"],
                "quantity": quantity,
                "price": product.get("arrivalCost") or product.get("sellingPrice") or 0,
                "measure": product.get("measureName", "шт."),
            }
        )
    return cart, not_found


async def _present_cart(message: Message, action: str, cart: list[dict], comment: str = ""):
    token = f"{message.from_user.id}:{message.message_id}"
    _pending[token] = {"kind": "stock_action", "action": action, "items": cart, "comment": comment}

    verb = "Списать" if action == "decommission" else "Оприходовать"
    items_text = "\n".join(f"• {it['name']} — {it['quantity']:g} {it.get('measure', 'шт.')}" for it in cart)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, подтверждаю", callback_data=f"confirm:{token}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel:{token}"),
            ]
        ]
    )
    await message.answer(f"{verb}:\n{items_text}", reply_markup=kb)


async def _handle_stock_action(message: Message, parsed: dict):
    cart, not_found = _build_cart(message.text)
    if not cart:
        await message.answer("Не нашёл такой товар в номенклатуре. Попробуй другое название.")
        return
    if not_found:
        await message.answer(f"⚠️ Не нашёл: {', '.join(not_found)} — их пропускаю.")
    await _present_cart(message, parsed["action"], cart, parsed.get("comment", "") or "")


# ----------------------------------------------------- guided menu flow

def _stock_verb(action: str) -> str:
    return "списать" if action == "decommission" else "оприходовать"


@dp.message(StockFlow.product, F.text)
async def stock_product_entered(message: Message, state: FSMContext):
    cart, not_found = _build_cart(message.text)
    if not cart:
        await message.answer("Не нашёл ни один товар. Напиши название ещё раз, можно списком по одному в строке.")
        return

    await state.update_data(cart=cart, not_found=not_found)
    data = await state.get_data()

    if data["action"] == "decommission":
        await state.set_state(StockFlow.comment)
        note = f"⚠️ Не нашёл: {', '.join(not_found)}\n\n" if not_found else ""
        await message.answer(f"{note}Причина списания (одна на все товары)? Или напиши \"-\".")
    else:
        await _finish_stock_flow(message, state)


@dp.message(StockFlow.comment, F.text)
async def stock_comment_entered(message: Message, state: FSMContext):
    comment = "" if message.text.strip() == "-" else message.text.strip()
    await state.update_data(comment=comment)
    await _finish_stock_flow(message, state)


async def _finish_stock_flow(message: Message, state: FSMContext):
    data = await state.get_data()
    not_found = data.get("not_found") or []
    if not_found:
        await message.answer(f"⚠️ Не нашёл: {', '.join(not_found)} — их пропускаю.")
    await _present_cart(message, data["action"], data["cart"], data.get("comment", ""))
    await state.clear()


@dp.callback_query(F.data.startswith("menu:"))
async def menu_selected(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":", 1)[1]

    try:
        _ensure_login()
    except UmagError as e:
        await callback.message.edit_text(f"Не удалось подключиться к UMAG: {e}")
        await callback.answer()
        return

    await state.clear()

    if action in ("decommission", "debit"):
        await state.update_data(action=action)
        await state.set_state(StockFlow.product)
        await callback.message.edit_text(
            f"Какой товар {_stock_verb(action)}? Можно списком, по одному на строку:\n"
            f"Тирамису 2\nМедовик 1"
        )
    elif action == "create_product":
        await _advance_product_creation(callback.message, state)
    elif action == "cash_report":
        await callback.message.edit_text("За какой период отчёт?", reply_markup=_period_menu_markup())

    await callback.answer()


@dp.callback_query(F.data.startswith("period:"))
async def period_selected(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":", 1)[1]

    if choice == "customdate":
        await state.set_state(CashCheck.custom_date)
        await callback.message.edit_text("Напиши дату, например: 11 июля или 2026-07-11")
        await callback.answer()
        return

    parsed = {
        "today": {"period_days": 1},
        "yesterday": {"specific_date": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")},
        "3days": {"period_days": 3},
    }[choice]

    try:
        _ensure_login()
    except UmagError as e:
        await callback.message.edit_text(f"Не удалось подключиться к UMAG: {e}")
        await callback.answer()
        return

    await callback.answer()
    await _handle_cash_report(callback.message, parsed, state)


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
            comment = pending.get("comment", "")
            lines = [
                {"barcode": it["barcode"], "quantity": it["quantity"], "price": it["price"], "comment": comment, "type": 1}
                for it in pending["items"]
            ]
            doc = umag.create_decommission()
            umag.add_decommission_products(doc["id"], lines)
            umag.provide_decommission(doc["id"])
            await callback.message.edit_text(f"✅ Проведено (№{doc['id']}), позиций: {len(lines)}.")
        else:
            lines = [{"barcode": it["barcode"], "quantity": it["quantity"]} for it in pending["items"]]
            doc = umag.create_debit()
            umag.add_debit_products(doc["id"], lines)
            umag.provide_debit(doc["id"])
            await callback.message.edit_text(f"✅ Проведено (№{doc['id']}), позиций: {len(lines)}.")
    except UmagError as e:
        await callback.message.edit_text(f"Ошибка: {e}")
    await callback.answer()
    await _show_main_menu(callback.message)


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_action(callback: CallbackQuery):
    token = callback.data.split(":", 1)[1]
    _pending.pop(token, None)
    await callback.message.edit_text("Отменено.")
    await callback.answer()
    await _show_main_menu(callback.message)


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
