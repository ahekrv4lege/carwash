"""Транспорт-заглушка для проверки штатной остановки aiogram сигналом ОС."""
import asyncio
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, GetUpdates
from aiogram.types import User


class OfflineSession(BaseSession):
    async def close(self):
        Path(sys.argv[1]).write_text("closed")

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="Synthetic", username="synthetic_bot")
        if isinstance(method, GetUpdates):
            print("polling-ready", flush=True)
            await asyncio.sleep(30)
            return []
        raise AssertionError("Unexpected Telegram method")

    async def stream_content(self, url, **kwargs):
        raise AssertionError("Network disabled")
        yield b""


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot("123456:synthetic_token_never_used_12345", session=OfflineSession())
    await Dispatcher().start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
