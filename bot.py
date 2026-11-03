"""Telegram-бот «администратор автомойки» на aiogram 3. Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import namedtuple
from contextlib import nullcontext, suppress, asynccontextmanager
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

from aiogram import Bot, Dispatcher, F as Filter, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION, TelegramAPIServer
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

import analytics
from admin import AdminReporter
from ai import Agent, ChatUser
from config import Config, load_config
from recovery import SNAPSHOT_TTL_SEC
from conversation import plain_text
from sheets import Store

log = logging.getLogger(__name__)
router = Router()
router.message.filter(Filter.chat.type == "private")  # работаем только в личных чатах

TG_LIMIT = 4000  # лимит Telegram — 4096 символов
SHUTDOWN_GRACE_SEC = 8  # ждём уже принятые обновления, не новых; systemd даёт 30 с


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


def telegram_api():
    """Базовый адрес Bot API: PRODUCTION или TELEGRAM_API_BASE (прокси/зеркало)."""
    raw = os.getenv("TELEGRAM_API_BASE", "").strip().rstrip("/")
    if not raw:
        return PRODUCTION
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment:
        raise RuntimeError("TELEGRAM_API_BASE: нужен абсолютный http(s)-адрес")
    return TelegramAPIServer(
        base=f"{raw}/bot{{token}}/{{method}}",
        file=f"{raw}/file/bot{{token}}/{{path}}",
    )


def telegram_session() -> AiohttpSession:
    session = AiohttpSession(api=telegram_api(), proxy=telegram_proxy(), timeout=35)
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
        agent = data.get("agent")
        if not callback and text and not text.startswith("/") and isinstance(agent, Agent):
            identity = {"id": user_id, "username": message.from_user.username, "full_name": message.from_user.full_name}
            try:
                key = await asyncio.to_thread(agent.inbox.accept, user_id, message.message_id, identity, text)
                if await asyncio.to_thread(agent.inbox.delivered, [key]):return None
            except Exception as error:
                log.error("Сообщение не сохранено в очереди (%s)", type(error).__name__)
                return await message.answer("Не удалось надёжно сохранить сообщение. Повторите его чуть позже.")
            data = dict(data, inbox_keys=[key])
        queue_owner = user_id not in self.pending
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
                keys = [key for item in batch for key in item.data.get("inbox_keys", [])]
                result = await latest.handler(event, dict(latest.data, inbox_keys=keys))
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


def markup_from_rows(rows) -> InlineKeyboardMarkup | None:
    """Строки кнопок (подпись, callback_data) в разметку Telegram; None, если данных нет."""
    try:
        if not isinstance(rows, list) or not rows:
            return None
        keyboard = [[(str(label), str(data)) for label, data in row] for row in rows]
    except Exception:
        return None
    if any(not label or not data or len(data.encode()) > 64 for row in keyboard for label, data in row):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in keyboard])


async def send_answer(message: Message, agent: Agent, reporter: AdminReporter,
                      user: ChatUser, text: str, answer: str,
                      keyboard: InlineKeyboardMarkup | None = None) -> None:
    raw_answer = answer
    delivery_token = agent.delivery_token(user.id, raw_answer)
    answer = plain_text(raw_answer)
    pending = agent._pending.get(user.id, {})
    offer_id = pending.get("offer_id") if delivery_token is not None else None
    if keyboard is None and isinstance(offer_id, str):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Подтвердить", callback_data=f"booking:confirm:{offer_id}"),
        ]])
    if keyboard is None:
        # Кнопки выбора даты/времени строятся по актуальным слотам таблицы.
        try:
            keyboard = markup_from_rows(await agent.slot_keyboard(user))
        except Exception as error:
            log.warning("Клавиатура недоступна; отправляем текст (%s)", type(error).__name__)
    chunks = split_text(answer)
    for index, chunk in enumerate(chunks):
        if keyboard is not None and index == len(chunks) - 1:
            await message.answer(chunk, reply_markup=keyboard)
        else:
            await message.answer(chunk)
    agent.mark_delivered(user.id, delivery_token)
    agent.mark_turn_delivered(user.id, raw_answer)
    try:
        await reporter.dialog(user.id, user.full_name, user.username, text, answer)
    except Exception as error:
        log.warning("Журнал диалога недоступен после доставки (%s)", type(error).__name__)


@asynccontextmanager
async def optional_typing(message):
    sender, entered = ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id), False
    try:
        try:
            await sender.__aenter__()
            entered = True
        except Exception as error:
            log.warning("Индикатор набора недоступен (%s)", type(error).__name__)
        yield
    finally:
        if entered:
            try:
                await sender.__aexit__(None, None, None)
            except Exception as error:
                log.warning("Индикатор набора не завершён (%s)", type(error).__name__)


async def ask_agent(message: Message, agent: Agent, reporter: AdminReporter, text: str,
                    reply_gate: asyncio.Semaphore | None = None, inbox_keys=None) -> None:
    telegram_user = message.from_user
    user = ChatUser(id=telegram_user.id, username=telegram_user.username, full_name=telegram_user.full_name)
    keys = inbox_keys or []
    if isinstance(agent, Agent) and isinstance(getattr(message, "message_id", None), int):
        if not keys:
            keys = [await asyncio.to_thread(agent.inbox.accept, user.id, message.message_id,
                {"id": user.id, "username": user.username, "full_name": user.full_name}, text)]
        if await asyncio.to_thread(agent.inbox.delivered, keys):return
    started = time.monotonic()
    gate = reply_gate if reply_gate is not None else nullcontext()
    # Ограничитель параллельных диалогов (reply_gate из main): всплеск обновлений
    # после простоя не должен порождать сотни одновременных AI-запросов и чтений таблиц.
    async with optional_typing(message), gate:
        try:
            answer = await agent.reply(user, text)
        except Exception as error:
            log.error("Обработка сообщения прервана (%s)", type(error).__name__)
            answer = "Не удалось завершить обработку. Если вы подтверждали запись, проверьте «Мои записи» перед повтором. Иначе повторите сообщение чуть позже."
        await asyncio.sleep(max(0, agent.config.reply_min_delay_sec - (time.monotonic() - started)))
    if keys:agent._inbox_active.update(keys)
    try:
        if keys:
            await asyncio.to_thread(agent.inbox.prepare, keys, answer, agent._snapshot_payload(user.id, time.time()))
        await send_answer(message, agent, reporter, user, text, answer)
        if keys:await asyncio.to_thread(agent.inbox.acknowledge, keys)
    finally:
        if keys:agent._inbox_active.difference_update(keys)


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


@router.callback_query(Filter.data.startswith("slots:"))
async def on_slot_pick(callback: CallbackQuery, agent: Agent, reporter: AdminReporter) -> None:
    """Выбор дня и времени кнопками: показываются только свободные слоты из таблицы."""
    if not isinstance(callback.message, Message) or callback.message.chat.type != "private":
        return
    parts = (callback.data or "").split(":")
    telegram_user = callback.from_user
    user = ChatUser(id=telegram_user.id, username=telegram_user.username, full_name=telegram_user.full_name)
    if len(parts) == 3 and parts[1] == "date":
        label = "Выбор даты"
        answer, rows = await agent.pick_date(user, parts[2])
    elif len(parts) == 5 and parts[1] == "time":
        label = "Выбор времени"
        answer, rows = await agent.pick_time(user, parts[2], ":".join(parts[3:]))
    elif len(parts) == 3 and parts[1] == "back" and parts[2] == "dates":
        label = "Возврат к выбору дня"
        answer, rows = await agent.slot_back(user)
    else:
        return
    if answer or rows:
        # Меню выбора уезжает целиком: следующий шаг приходит новым сообщением,
        # а при возврате к дням меню дней показывается заново.
        try:
            await callback.message.delete()
        except Exception as error:
            log.warning("Не удалось убрать меню выбора (%s)", type(error).__name__)
            if callback.message.reply_markup:
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)
                except Exception as removal_error:
                    log.warning("Не удалось убрать старые кнопки (%s)", type(removal_error).__name__)
    if answer:
        await send_answer(callback.message, agent, reporter, user, label, answer,
                          keyboard=markup_from_rows(rows))


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


class AdminOnly:
    """Доступ только администратору: ADMIN_USER_IDS или ADMIN_CHAT_ID в личном чате.

    Для остальных фильтр не проходит, и сообщение идёт диалогу как обычно. Без настроек — отказ.
    """

    async def __call__(self, message: Message, config: Config) -> bool:
        if message.from_user is None:
            return False
        user_id = message.from_user.id
        if user_id in config.admin_user_ids:
            return True
        # ADMIN_CHAT_ID в личном чате совпадает с ID пользователя; ID группы админом не делает.
        return config.admin_chat_id is not None and user_id == config.admin_chat_id


@router.message(Command("stats"), AdminOnly())
async def cmd_stats(message: Message, command: CommandObject, store: Store) -> None:
    arguments = (command.args or "").strip()
    days = analytics.DEFAULT_PERIOD
    if arguments:
        if not arguments.isdigit() or int(arguments) not in analytics.PERIOD_CHOICES:
            await message.answer("Использование: /stats 1, /stats 7 или /stats 30")
            return
        days = int(arguments)
    try:
        rows, slots = await asyncio.to_thread(store.analytics_snapshot)
        text = analytics.render(analytics.compute(rows, slots, store.now().date(), days))
    except Exception as error:
        log.error("Статистика недоступна (%s)", type(error).__name__)
        await message.answer("Статистика временно недоступна. Попробуйте позже.")
        return
    await message.answer(text[:TG_LIMIT])


@router.message(Command("mycars"))
async def cmd_mycars(message: Message, agent: Agent) -> None:
    try:
        cars = await asyncio.to_thread(agent.cars.for_user, message.from_user.id)
    except Exception as error:
        log.warning("Память об автомобилях недоступна (%s)", type(error).__name__)
        await message.answer("Сохранённые автомобили сейчас недоступны. Попробуйте позже.")
        return
    if not cars:
        await message.answer("Сохранённых автомобилей пока нет. Они появятся после первой подтверждённой записи.")
        return
    lines = [f"{number}. {car.name} — {car.size_class}" for number, car in enumerate(cars, 1)]
    await message.answer("Ваши автомобили:\n" + "\n".join(lines) + "\n\nУдалить: /forgetcar номер")


@router.message(Command("forgetcar"))
async def cmd_forgetcar(message: Message, command: CommandObject, agent: Agent) -> None:
    arguments = (command.args or "").strip()
    try:
        cars = await asyncio.to_thread(agent.cars.for_user, message.from_user.id)
        if not arguments.isdigit() or not 1 <= int(arguments) <= len(cars):
            await message.answer("Укажите номер из списка: /mycars, затем /forgetcar номер")
            return
        car = cars[int(arguments) - 1]
        removed = await asyncio.to_thread(agent.cars.forget, message.from_user.id, car.id)
    except Exception as error:
        log.warning("Автомобиль не удалён из памяти (%s)", type(error).__name__)
        await message.answer("Не удалось удалить автомобиль. Попробуйте позже.")
        return
    if removed:
        agent.forget_vehicle_facts(message.from_user.id, car)
    await message.answer("Автомобиль удалён из памяти." if removed else "Такого автомобиля уже нет в памяти.")


@router.message(Filter.text)
async def on_text(message: Message, agent: Agent, reporter: AdminReporter, reply_gate: asyncio.Semaphore, inbox_keys=None) -> None:
    await ask_agent(message, agent, reporter, message.text, reply_gate, inbox_keys)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer("Напишите ваш вопрос текстом.")


class RecoveryMessage:
    """Минимальный получатель для send_answer при возобновлении после рестарта."""

    def __init__(self, bot: Bot, chat_id: int):
        self.bot, self.chat_id = bot, chat_id

    async def answer(self, text: str, reply_markup=None):
        return await self.bot.send_message(self.chat_id, text, reply_markup=reply_markup)


async def resume_recovered(agent: Agent, bot: Bot, reporter: AdminReporter) -> None:
    """Доставить готовый ответ или один раз безопасно повторить прерванный ход.

    Кнопочные действия вслепую не повторяются: подтверждение записи могло уже
    примениться. Обычный текст сообщения не содержит операции записи, поэтому
    его повтор допустим; предложение всё равно требует кнопки.
    """
    for user, turn in agent.pending_recoveries():
        channel = RecoveryMessage(bot, user.id)
        try:
            if turn.get("processed") and not turn.get("delivered"):
                log.info("Повторная доставка ответа после перезапуска: id=%s", user.id)
                await send_answer(channel, agent, reporter, user, turn.get("text", ""),
                                  turn.get("answer", ""))
            elif not turn.get("processed") and turn.get("kind") == "reply":
                log.info("Возобновление прерванного ответа после перезапуска: id=%s", user.id)
                answer = await agent.reply(user, turn.get("text", ""))
                await send_answer(channel, agent, reporter, user, turn.get("text", ""), answer)
        except Exception as error:
            log.warning("Не удалось возобновить диалог (%s)", type(error).__name__)
        else:
            agent.clear_recovery(user.id)


async def resume_inbox(agent, bot, reporter, *, processed_only=False):
    for entry in await asyncio.to_thread(agent.inbox.pending, processed_only):
        if set(entry["keys"]) & agent._inbox_active:continue
        user = ChatUser(**entry["user"])
        if processed_only and agent._active.get(user.id):continue
        try:
            answer, payload = entry["answer"], entry["payload"]
            expired = time.time()-entry["received"] > SNAPSHOT_TTL_SEC
            if expired:
                payload = None
                answer = "Сообщение не удалось обработать или доставить вовремя. Повторите запрос, чтобы проверить актуальные цены и время."
            if not processed_only and payload and not agent._active.get(user.id):
                await agent._restore_payload(user.id, entry["received"], payload, time.time())
            if payload and payload.get("state", {}).get("stage") == "awaiting_confirmation" and (
                    user.id not in agent._pending or (processed_only and agent.delivery_token(user.id, answer) is None)):
                answer = ("Не удалось вовремя доставить сводку записи. Попросите проверить её заново: неподтверждённая запись не создавалась."
                          if processed_only else None)
            if answer is None:
                if processed_only:continue
                if time.time()-entry["received"] > SNAPSHOT_TTL_SEC:
                    answer = "Сообщение не удалось обработать вовремя. Повторите запрос, чтобы проверить актуальные цены и время."
                else:answer = await agent.reply(user, "\n".join(entry["texts"]))
                await asyncio.to_thread(agent.inbox.prepare, entry["keys"], answer, agent._snapshot_payload(user.id, time.time()))
            channel = RecoveryMessage(bot, user.id)
            if processed_only:
                for chunk in split_text(plain_text(answer)):await channel.answer(chunk)
            else:
                await send_answer(channel, agent, reporter, user, "\n".join(entry["texts"]), answer)
            await asyncio.to_thread(agent.inbox.acknowledge, entry["keys"])
            agent.clear_recovery(user.id)
        except Exception as error:
            log.warning("Очередь ответа сохранена для повтора (%s)", type(error).__name__)


async def retry_inbox(agent, bot, reporter):
    while True:
        await asyncio.sleep(15)
        try:
            await resume_inbox(agent, bot, reporter, processed_only=True)
        except Exception as error:
            log.warning("Очередь ответов временно недоступна (%s)", type(error).__name__)


async def finish_updates(dispatcher: Dispatcher) -> None:
    """Дождаться уже принятых обработчиков; новые обновления polling не принимает."""
    active = getattr(dispatcher, "_handle_update_tasks", ())
    if not isinstance(active, (set, list, tuple)):
        return
    tasks = [task for task in list(active) if not task.done()]
    if not tasks:
        return
    log.info("Ожидание активных обработчиков: %d", len(tasks))
    _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SEC)
    if not pending:
        return
    log.warning("Обработчики не завершились за %s с: %d; отменяю", SHUTDOWN_GRACE_SEC, len(pending))
    for task in pending:
        task.cancel()
    await asyncio.wait(pending, timeout=2)


async def maintain_sessions(agent: Agent) -> None:
    while True:
        await asyncio.sleep(60)
        agent.cleanup_sessions()


async def maintain_vehicle_cache(agent: Agent) -> None:
    """Раз в минуту: выгрузка неактивных клиентов из RAM. Данные в SQLite не трогаются."""
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(agent.cars.maintenance)
        except Exception as error:
            log.warning("Обслуживание кэша автомобилей не выполнено (%s)", type(error).__name__)


async def maintain_grid_consistency(store: Store) -> None:
    """Раз в 10 минут: сверка клеток сеток постов с записями (см. Store.grid_maintenance)."""
    while True:
        await asyncio.sleep(600)
        try:
            await asyncio.to_thread(store.grid_maintenance)
        except Exception as error:
            log.warning("Сверка сетки постов не выполнена (%s)", type(error).__name__)


async def archive_old_bookings(store: Store) -> None:
    """Раз в 6 часов: прошедшие заявки получают статус «Архив», записи со
    статусами «Архив» и «Отмена» переносятся в отдельный архивный лист
    (без дублей), нумерация ID в «Записях» выставляется заново, затем
    очистка слотов."""
    while True:
        try:
            archived = await asyncio.to_thread(store.archive_old_bookings)
            # Периодическое обслуживание: безопасная хронологическая сортировка
            # и перенумерация ID по порядку строк. Формулы и данные вне шапки
            # блокируют перезапись.
            await asyncio.to_thread(store.refresh_bookings_sheet, sort=True)
            moved = await asyncio.to_thread(store.move_archived_bookings)
            if archived or moved:
                log.info("Архивировано устаревших записей: %d, перенесено в архивный лист: %d",
                         archived, moved)
        except Exception as error:
            log.warning("Не удалось архивировать старые записи (%s)", type(error).__name__)
        try:
            cleaned = await asyncio.to_thread(store.clean_expired_slots)
            if cleaned:
                log.info("Очищено устаревших строк расписания: %d", cleaned)
        except Exception as error:
            log.warning("Не удалось очистить устаревшие строки расписания (%s)", type(error).__name__)
        await asyncio.sleep(6 * 3600)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    bot = Bot(config.bot_token, session=telegram_session())
    agent = None
    dispatcher = None
    maintenance = None
    archiver = None
    vehicle_cache = None
    grid_auditor = None
    outbox = None
    try:
        store = Store(config)
        reporter = AdminReporter(bot, config.admin_chat_id)
        agent = Agent(config, store, reporter)
        dispatcher = Dispatcher(agent=agent, config=config, reporter=reporter, store=store,
                                reply_gate=asyncio.Semaphore(config.reply_concurrency))
        batcher = MessageBatcher(config.reply_batch_sec)
        router.message.outer_middleware(batcher)
        router.callback_query.outer_middleware(batcher)
        dispatcher.include_router(router)
        # Сначала поднимаем недавние диалоги и завершаем прерванные ответы,
        # только потом начинаем принимать новые обновления Telegram.
        restore_conversations = getattr(agent, "restore_conversations", None)
        if restore_conversations is not None and asyncio.iscoroutinefunction(restore_conversations):
            await restore_conversations()
            await resume_inbox(agent, bot, reporter)
            await resume_recovered(agent, bot, reporter)
        outbox = asyncio.create_task(retry_inbox(agent, bot, reporter))
        maintenance = asyncio.create_task(maintain_sessions(agent))
        archiver = asyncio.create_task(archive_old_bookings(store))
        vehicle_cache = asyncio.create_task(maintain_vehicle_cache(agent))
        grid_auditor = asyncio.create_task(maintain_grid_consistency(store))
        if config.sheets_adaptive_enabled and config.sheets_discover_on_start:
            await asyncio.to_thread(store.refresh_schema, force=config.sheets_force_schema_refresh)
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("Бот запущен. Модель: %s", config.ai_model if config.ai_enabled else "AI отключён")
        await dispatcher.start_polling(bot, close_bot_session=False)
    finally:
        # start_polling уже перестал принимать новые обновления. Даём активным
        # обработчикам завершиться и только затем пишем один снимок диалогов.
        if dispatcher is not None:
            await finish_updates(dispatcher)
        for task in (maintenance, archiver, vehicle_cache, grid_auditor, outbox):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        try:
            if agent is not None:
                try:
                    save_conversations = getattr(agent, "save_conversations", None)
                    if save_conversations is not None and asyncio.iscoroutinefunction(save_conversations):
                        await save_conversations()
                finally:
                    try:
                        await agent.close()
                    finally:
                        agent.cars.close()
        finally:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
