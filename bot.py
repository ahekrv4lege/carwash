"""Telegram-бот «администратор автомойки» на aiogram 3. Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.chat_action import ChatActionSender

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


async def ask_agent(message: Message, agent: Agent, text: str) -> None:
    u = message.from_user
    user = ChatUser(id=u.id, username=u.username, full_name=u.full_name)
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        answer = await agent.reply(user, text)
    for chunk in split_text(answer):
        await message.answer(chunk)


@router.message(CommandStart())
async def cmd_start(message: Message, agent: Agent, cfg: Config) -> None:
    agent.reset(message.from_user.id)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📞 Отправить номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        f"Здравствуйте! Меня зовут Светлана, я администратор автомойки «{cfg.carwash_name}» 🙂\n\n"
        "Подскажу услуги и цены, подберу свободное время и запишу вас.\n"
        "Как вас зовут?",
        reply_markup=kb,
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message, agent: Agent) -> None:
    agent.reset(message.from_user.id)
    await message.answer("Диалог сброшен. Чем могу помочь?")


@router.message(F.contact)
async def on_contact(message: Message, agent: Agent) -> None:
    await ask_agent(message, agent, f"Мой номер телефона: {message.contact.phone_number}")


@router.message(F.text)
async def on_text(message: Message, agent: Agent) -> None:
    await ask_agent(message, agent, message.text)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer("Пожалуйста, напишите ваш вопрос текстом 🙂")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    bot = Bot(cfg.bot_token)
    store = Store(cfg)

    async def notify_admin(text: str) -> None:
        if not cfg.admin_chat_id:
            return
        try:
            await bot.send_message(cfg.admin_chat_id, text)
        except Exception:
            log.exception("Не удалось отправить уведомление администратору")

    agent = Agent(cfg, store, notify_admin)

    dp = Dispatcher(agent=agent, cfg=cfg)  # попадут в хэндлеры по имени параметров
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен. Модель: %s", cfg.openrouter_model)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
