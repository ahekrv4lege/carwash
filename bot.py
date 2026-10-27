"""Telegram-бот «администратор автомойки» на aiogram 3. Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import namedtuple
from contextlib import suppress
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

from aiogram import Bot, Dispatcher, F as Filter, Router
from aiogram.filters import Command, CommandStart
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from admin import AdminReporter
from ai import Agent, ChatUser
from config import Config, load_config
from conversation import plain_text
from sheets import Store

log = logging.getLogger(__name__)
router = Router()
router.message.filter(Filter.chat.type == "private")  # работаем только в личных чатах

TG_LIMIT = 4000  # лимит Telegram — 4096 символов


QueuedMessage = namedtuple("QueuedMessage", "handler message data batchable future ack")


async def retry_telegram(make_request, bot, method):
    retryable = method.__api_method__ in {
        "getMe", "getWebhookInfo", "getMyCommands", "deleteWebhook", "sendMessage",
        "sendChatAction", "answerCallbackQuery", "editMessageReplyMarkup",
    }
    for attempt in range(2):
        try:
            return await make_request(bot, method)
        except TelegramNetworkError:
            if attempt or not retryable:
                raise
            log.warning("Обрыв Telegram (%s); повтор подключения", method.__api_method__)
            await asyncio.sleep(1)


def telegram_proxy() -> str | None:
    value = os.getenv("TELEGRAM_PROXY", "auto").strip()
    if value.lower() in {"", "none", "direct"}:
        return None
    if value.lower() == "auto":
        if proxy_bypass("api.telegram.org"):
            return None
        proxies = getproxies()
        value = proxies.get("https") or proxies.get("all") or proxies.get("http") or ""
        if not value:
            return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "socks4", "socks5"} or not parsed.hostname or parsed.fragment or parsed.query:
        raise RuntimeError("TELEGRAM_PROXY: нужен адрес HTTP(S)/SOCKS-прокси, auto или direct")
    try:
        parsed.port
    except ValueError:
        raise RuntimeError("TELEGRAM_PROXY: неверный порт") from None
    return value


def telegram_session() -> AiohttpSession:
    session = AiohttpSession(proxy=telegram_proxy(), timeout=35)
    session.middleware(retry_telegram)
    return session


async def acknowledge(callback):
    try:
        await callback.answer()
    except Exception as error:
        log.warning("Не удалось ответить на нажатие (%s)", type(error).__name__)

class MessageBatcher:
    def __init__(self, delay):
        self.delay, self.pending = delay, {}

    async def __call__(self, handler, message, data):
        callback = isinstance(message, CallbackQuery)
        chat = message.message.chat if callback and isinstance(message.message, Message) else getattr(message, "chat", None)
        if chat is None or chat.type != "private" or not message.from_user:
            return await handler(message, data)
        user_id, text = message.from_user.id, "" if callback else message.text or ""
        affirmative = any(Agent._affirmative(text, action) for action in ("create_booking", "cancel_booking"))
        batchable = bool(text) and not text.startswith("/") and not affirmative and self.delay > 0
        future = asyncio.get_running_loop().create_future()
        queue_owner = user_id not in self.pending
        agent = data.get("agent")
        if affirmative and not queue_owner and agent and agent.delivery_token(user_id) is None:
            data = dict(data, queued_before_proposal=True)
        queue = self.pending.setdefault(user_id, [])
        ack = asyncio.create_task(acknowledge(message)) if callback else None
        queue.append(QueuedMessage(handler, message, data, batchable, future, ack))
        if queue_owner:
            try:
                await self._drain(queue)
            finally:
                self.pending.pop(user_id, None)
        return await asyncio.shield(future)

    async def _drain(self, queue):
        batch = []
        try:
            while queue:
                if queue[0].batchable:
                    await asyncio.sleep(self.delay)
                batch = [queue.pop(0)]
                while batch[0].batchable and queue and queue[0].batchable:
                    batch.append(queue.pop(0))
                await self._deliver(batch)
        finally:
            for item in (*batch, *queue):
                item.future.cancel()
                if item.ack is not None:
                    item.ack.cancel()

    async def _deliver(self, batch):
        latest = batch[-1]
        event = latest.message
        if len(batch) > 1:
            event = event.model_copy(update={"text": "\n".join(item.message.text for item in batch),
                                             "entities": None})
        try:
            if latest.ack is not None:
                await latest.ack
            agent = latest.data.get("agent")
            if latest.data.get("queued_before_proposal") and agent.delivery_token(event.from_user.id) is not None:
                result = None
            else:
                result = await latest.handler(event, latest.data)
        except Exception as error:
            for item in batch:
                item.future.set_exception(error)
        else:
            for item in batch:
                item.future.set_result(result)


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


async def send_answer(message: Message, agent: Agent, reporter: AdminReporter,
                      user: ChatUser, text: str, answer: str) -> None:
    delivery_token = agent.delivery_token(user.id, answer)
    answer = plain_text(answer)
    pending = agent._pending.get(user.id, {})
    offer_id = pending.get("offer_id") if delivery_token is not None else None
    keyboard = None
    if isinstance(offer_id, str):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Подтвердить", callback_data=f"booking:confirm:{offer_id}"),
        ]])
    chunks = split_text(answer)
    for index, chunk in enumerate(chunks):
        if keyboard is not None and index == len(chunks) - 1:
            await message.answer(chunk, reply_markup=keyboard)
        else:
            await message.answer(chunk)
    agent.mark_delivered(user.id, delivery_token)
    await reporter.dialog(user.id, user.full_name, user.username, text, answer)


async def ask_agent(message: Message, agent: Agent, reporter: AdminReporter, text: str) -> None:
    telegram_user = message.from_user
    user = ChatUser(id=telegram_user.id, username=telegram_user.username, full_name=telegram_user.full_name)
    started = time.monotonic()
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        answer = await agent.reply(user, text)
        await asyncio.sleep(max(0, agent.config.reply_min_delay_sec - (time.monotonic() - started)))
    await send_answer(message, agent, reporter, user, text, answer)


@router.callback_query(Filter.data.startswith("booking:"))
async def on_proposal(callback: CallbackQuery, agent: Agent, reporter: AdminReporter) -> None:
    if not isinstance(callback.message, Message) or callback.message.chat.type != "private":
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in {"confirm", "edit"}:
        return
    telegram_user = callback.from_user
    user = ChatUser(id=telegram_user.id, username=telegram_user.username, full_name=telegram_user.full_name)
    answer = await agent.proposal_action(user, parts[2], parts[1])
    pending = agent._pending.get(user.id, {})
    if pending.get("offer_id") != parts[2] and callback.message.reply_markup:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception as error:
            log.warning("Не удалось убрать старые кнопки (%s)", type(error).__name__)
    label = "Подтвердить" if parts[1] == "confirm" else "Изменить"
    if answer:
        await send_answer(callback.message, agent, reporter, user, label, answer)


@router.message(CommandStart())
async def cmd_start(message: Message, agent: Agent, config: Config) -> None:
    agent.reset(message.from_user.id)
    await message.answer(
        plain_text(f"Здравствуйте! Вас приветствует автомойка «{config.carwash_name}». Вам ответит первый свободный сотрудник. Чем можем помочь?"),
    )
    agent._state[message.from_user.id]["greeted"] = True


@router.message(Command("reset"))
async def cmd_reset(message: Message, agent: Agent) -> None:
    agent.reset(message.from_user.id)
    await message.answer("Диалог сброшен. Чем могу помочь?")


@router.message(Filter.text)
async def on_text(message: Message, agent: Agent, reporter: AdminReporter) -> None:
    await ask_agent(message, agent, reporter, message.text)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer("Напишите ваш вопрос текстом.")


async def maintain_sessions(agent: Agent) -> None:
    while True:
        await asyncio.sleep(60)
        agent.cleanup_sessions()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    bot = Bot(config.bot_token, session=telegram_session())
    agent = None
    maintenance = None
    try:
        store = Store(config)
        reporter = AdminReporter(bot, config.admin_chat_id)
        agent = Agent(config, store, reporter)
        dispatcher = Dispatcher(agent=agent, config=config, reporter=reporter)
        batcher = MessageBatcher(config.reply_batch_sec)
        router.message.outer_middleware(batcher)
        router.callback_query.outer_middleware(batcher)
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
