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

ANSWER_LIMIT = 3500  # общий бюджет вопроса и ответа, с запасом на заголовок


def _shorten(text: str, limit: int) -> str:
    encoded = text.encode("utf-16-le")
    if len(encoded) <= limit * 2:
        return text
    return encoded[:max(limit - 2, 0) * 2].decode("utf-16-le", errors="ignore") + " …"


class AdminReporter:
    """Отправка карточек диалогов и уведомлений о записях в чат администратора."""

    def __init__(self, bot: Bot, chat_id: int | None) -> None:
        self.bot = bot
        self.chat_id = chat_id

    async def send(self, text: str, username: str | None = None, parse_mode: str | None = None) -> None:
        """Сообщение в админ-чат; при наличии юзернейма — кнопка «Открыть профиль»."""
        if not self.chat_id:
            return
        keyboard = None
        if username:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть профиль", url=f"https://t.me/{username}")
            ]])
        try:
            await self.bot.send_message(self.chat_id, text, reply_markup=keyboard, parse_mode=parse_mode)
        except Exception as error:
            log.error("Не удалось отправить сообщение администратору (%s)", type(error).__name__)

    async def dialog(
        self,
        user_id: int,
        full_name: str,
        username: str | None,
        question: str,
        answer: str,
    ) -> None:
        """Карточка одного обмена «клиент спросил — бот ответил»."""
        question = _shorten(question, ANSWER_LIMIT // 2)
        remaining = ANSWER_LIMIT - len(question.encode("utf-16-le")) // 2
        answer = _shorten(answer, remaining)
        if username:
            link = f"https://t.me/{username}"
            client_label = f'👤 <a href="{link}">{html.escape(full_name)}</a> (<a href="{link}">@{username}</a>)'
        else:
            client_label = f"👤 {html.escape(full_name)} · id {user_id}"
        await self.send(
            f"{client_label}\n"
            f"❓ {html.escape(question)}\n"
            f"──────────────\n"
            f"🤖 {html.escape(answer)}",
            username=username,
            parse_mode="HTML",
        )
