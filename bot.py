"""Telegram-бот «администратор автомойки» на aiogram 3. Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher, F as Filter, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from admin import AdminReporter
from ai import Agent, ChatUser
from config import Config, load_config
from sheets import Store

log = logging.getLogger(__name__)
router = Router()
router.message.filter(Filter.chat.type == "private")  # работаем только в личных чатах

TG_LIMIT = 4000  # лимит Telegram — 4096 символов


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


async def ask_agent(message: Message, agent: Agent, reporter: AdminReporter, text: str) -> None:
    telegram_user = message.from_user
    user = ChatUser(id=telegram_user.id, username=telegram_user.username, full_name=telegram_user.full_name)
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        answer = await agent.reply(user, text)
    delivery_token = agent.delivery_token(telegram_user.id, answer)
    for chunk in split_text(answer):
        await message.answer(chunk)
    agent.mark_delivered(telegram_user.id, delivery_token)
    await reporter.dialog(telegram_user.id, telegram_user.full_name, telegram_user.username, text, answer)


@router.message(CommandStart())
async def cmd_start(message: Message, agent: Agent, config: Config) -> None:
    agent.reset(message.from_user.id)
    await message.answer(
        f"Здравствуйте! Меня зовут Светлана, я администратор автомойки «{config.carwash_name}» 🙂\n\n"
        "Подскажу услуги и цены, подберу свободное время и запишу вас.\n"
        "Как вас зовут?",
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message, agent: Agent) -> None:
    agent.reset(message.from_user.id)
    await message.answer("Диалог сброшен. Чем могу помочь?")


@router.message(Filter.contact)
async def on_contact(message: Message, agent: Agent, reporter: AdminReporter) -> None:
    await ask_agent(message, agent, reporter, f"Мой номер телефона: {message.contact.phone_number}")


@router.message(Filter.text)
async def on_text(message: Message, agent: Agent, reporter: AdminReporter) -> None:
    await ask_agent(message, agent, reporter, message.text)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer("Пожалуйста, напишите ваш вопрос текстом 🙂")


async def maintain_sessions(agent: Agent) -> None:
    while True:
        await asyncio.sleep(60)
        agent.cleanup_sessions()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    bot = Bot(config.bot_token)
    agent = None
    maintenance = None
    try:
        store = Store(config)
        reporter = AdminReporter(bot, config.admin_chat_id)
        agent = Agent(config, store, reporter)
        dispatcher = Dispatcher(agent=agent, config=config, reporter=reporter)
        dispatcher.include_router(router)
        maintenance = asyncio.create_task(maintain_sessions(agent))
        if config.sheets_adaptive_enabled and config.sheets_discover_on_start:
            await asyncio.to_thread(store.refresh_schema, force=config.sheets_force_schema_refresh)
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("Бот запущен. Модель: %s", config.ai_model if config.ai_enabled else "AI отключён")
        await dispatcher.start_polling(bot, close_bot_session=False)
    finally:
        if maintenance is not None:
            maintenance.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance
        try:
            if agent is not None:
                await agent.close()
        finally:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
