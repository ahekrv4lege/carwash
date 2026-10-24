"""Уведомления администратору в один чат (ADMIN_CHAT_ID): лента диалогов и записи.

Карточка диалога — одно сообщение на обмен «клиент → бот». Клик по имени/юзернейму
или кнопке «Открыть профиль» открывает профиль клиента в Telegram. Ошибки отправки
не роняют бота — пишутся в лог.
"""
from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

ANSWER_LIMIT = 3500  # запас до лимита Telegram 4096 на заголовок карточки и вопрос


class AdminReporter:
    """Отправка карточек диалогов и уведомлений о записях в чат администратора."""

    def __init__(self, bot: Bot, chat_id: int | None) -> None:
        self.bot = bot
        self.chat_id = chat_id

    async def send(self, text: str, username: str | None = None, parse_mode: str | None = None) -> None:
        """Сообщение в админ-чат; при наличии юзернейма — кнопка «Открыть профиль»."""
        if not self.chat_id:
            return
        kb = None
        if username:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть профиль", url=f"https://t.me/{username}")
            ]])
        try:
            await self.bot.send_message(self.chat_id, text, reply_markup=kb, parse_mode=parse_mode)
        except Exception as exc:
            log.error("Не удалось отправить сообщение администратору (%s)", type(exc).__name__)

    async def dialog(
        self,
        user_id: int,
        full_name: str,
        username: str | None,
        question: str,
        answer: str,
    ) -> None:
        """Карточка одного обмена «клиент спросил — бот ответил»."""
        if len(answer) > ANSWER_LIMIT:
            answer = answer[:ANSWER_LIMIT] + " …"
        if username:
            link = f"https://t.me/{username}"
            who = f'👤 <a href="{link}">{html.escape(full_name)}</a> (<a href="{link}">@{username}</a>)'
        else:
            who = f"👤 {html.escape(full_name)} · id {user_id}"
        await self.send(
            f"{who}\n"
            f"❓ {html.escape(question)}\n"
            f"──────────────\n"
            f"🤖 {html.escape(answer)}",
            username=username,
            parse_mode="HTML",
        )
