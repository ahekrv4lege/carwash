"""Одинаковое подключение к Telegram при запуске и диагностике."""
from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError

log = logging.getLogger(__name__)


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
