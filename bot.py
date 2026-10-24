"""Telegram-бот «администратор автомойки» на aiogram 3. Запуск: python bot.py"""
from __future__ import annotations

if __name__ == "__main__":
    from run import main as launch
    raise SystemExit(launch())

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from admin import AdminReporter
from ai import Agent, ChatUser
from config import Config, load_config
from sheets import Store

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type == "private")  # работаем только в личных чатах

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
    u = message.from_user
    user = ChatUser(id=u.id, username=u.username, full_name=u.full_name)
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        answer = await agent.reply(user, text)
    delivery_token = agent.delivery_token(u.id)
    for chunk in split_text(answer):
        await message.answer(chunk)
    agent.mark_delivered(u.id, delivery_token)
    await reporter.dialog(u.id, u.full_name, u.username, text, answer)


@router.message(CommandStart())
async def cmd_start(message: Message, agent: Agent, cfg: Config) -> None:
    agent.reset(message.from_user.id)
    await message.answer(
        f"Здравствуйте! Меня зовут Светлана, я администратор автомойки «{cfg.carwash_name}» 🙂\n\n"
        "Подскажу услуги и цены, подберу свободное время и запишу вас.\n"
        "Как вас зовут?",
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message, agent: Agent) -> None:
    agent.reset(message.from_user.id)
    await message.answer("Диалог сброшен. Чем могу помочь?")


@router.message(F.contact)
async def on_contact(message: Message, agent: Agent, reporter: AdminReporter) -> None:
    await ask_agent(message, agent, reporter, f"Мой номер телефона: {message.contact.phone_number}")


@router.message(F.text)
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
    cfg = load_config()
    bot = Bot(cfg.bot_token)
    store = Store(cfg)
    reporter = AdminReporter(bot, cfg.admin_chat_id)
    agent = Agent(cfg, store, reporter)

    dp = Dispatcher(agent=agent, cfg=cfg, reporter=reporter)  # попадут в хэндлеры по имени параметров
    dp.include_router(router)
    maintenance = asyncio.create_task(maintain_sessions(agent))

    try:
        if cfg.sheets_adaptive_enabled and cfg.sheets_discover_on_start:
            await asyncio.to_thread(store.refresh_schema, force=cfg.sheets_force_schema_refresh)
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("Бот запущен. Модель: %s", cfg.conversation_model if cfg.ai_enabled else "AI отключён")
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
        try:
            if agent.client is not None:
                await agent.client.close()
        finally:
            await bot.session.close()
