"""Проверка Telegram и чтения Google Sheets без сообщений, записей и платного AI."""
from __future__ import annotations

import asyncio

from aiogram import Bot
from network import telegram_session
from providers import api_error_label
from sheets import Store


async def diagnose(config) -> bool:
    ok = True
    bot = Bot(config.bot_token, session=telegram_session())
    try:
        for attempt in range(2):
            try:
                identity = await bot.get_me(request_timeout=12)
                webhook = await bot.get_webhook_info(request_timeout=12)
                break
            except Exception:
                if attempt:
                    raise
                await asyncio.sleep(1)
        print(f"Telegram: @{identity.username}; авторизация работает; ожидает сообщений: {webhook.pending_update_count}")
        if webhook.url:
            print("При запуске polling бот переключит существующий webhook.")
    except Exception as error:
        ok = False
        print("Telegram недоступен: " + api_error_label(error) + ". Проверьте токен, сеть и TELEGRAM_PROXY.")
    finally:
        await bot.session.close()
    try:
        store = Store(config)
        catalog = await asyncio.wait_for(asyncio.to_thread(store.price_list), timeout=30)
        slots = await asyncio.wait_for(asyncio.to_thread(store.free_slots), timeout=30)
        print(f"Google Sheets: прайс прочитан; услуг: {len(catalog.get('services', []))}; тарифов: {len(catalog.get('tariffs', []))}; дней в ответе слотов: {len(slots.get('days', []))}")
    except Exception as error:
        ok = False
        print("Google Sheets недоступна: " + api_error_label(error) + ". Проверьте credentials.json, доступ и названия листов.")
    print("AI: " + config.ai_model + "; платный запрос не выполнялся.")
    return ok
