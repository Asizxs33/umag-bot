"""Turns a free-text Telegram message into a structured action using Claude."""

from __future__ import annotations

import json
from datetime import date

import anthropic

from config import ANTHROPIC_API_KEY

MODEL = "claude-sonnet-5"

TOOLS = [
    {
        "name": "parse_action",
        "description": "Extract the accounting action the user wants performed in UMAG.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["decommission", "debit", "cash_report", "create_product", "unknown"],
                    "description": (
                        "decommission = списание товара (что-то испортилось/потеряно/выброшено); "
                        "debit = оприходование товара (что-то поступило/нашлось), но товар уже есть в номенклатуре; "
                        "create_product = создать НОВЫЙ товар в номенклатуре (пользователь явно просит "
                        "добавить/создать новый товар, завести карточку товара); "
                        "cash_report = запрос отчёта по кассе/сменам; "
                        "unknown = не удалось понять запрос."
                    ),
                },
                "product_query": {
                    "type": "string",
                    "description": "Название товара как его написал пользователь (для decommission/debit).",
                },
                "quantity": {
                    "type": "number",
                    "description": "Количество (для decommission/debit). По умолчанию 1.",
                },
                "comment": {
                    "type": "string",
                    "description": "Причина/комментарий, если пользователь её указал.",
                },
                "period_days": {
                    "type": "integer",
                    "description": "Для cash_report: за сколько последних дней нужен отчёт (по умолчанию 1 = сегодня).",
                },
                "product_name": {
                    "type": "string",
                    "description": "Название нового товара, если пользователь уже его указал (для create_product).",
                },
                "arrival_cost": {
                    "type": "number",
                    "description": "Закупочная цена нового товара, если пользователь уже её указал (для create_product).",
                },
                "selling_price": {
                    "type": "number",
                    "description": "Продажная цена нового товара, если пользователь уже её указал (для create_product).",
                },
                "category_name": {
                    "type": "string",
                    "description": "Название категории нового товара, если пользователь уже её указал (для create_product).",
                },
            },
            "required": ["action"],
        },
    }
]

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def parse_message(text: str) -> dict:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        tools=TOOLS,
        tool_choice={"type": "tool", "name": "parse_action"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"Сегодня {date.today().isoformat()}. "
                    f"Разбери сообщение пользователя магазина и вызови parse_action.\n\n"
                    f"Сообщение: {text}"
                ),
            }
        ],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "parse_action":
            return block.input
    return {"action": "unknown"}
