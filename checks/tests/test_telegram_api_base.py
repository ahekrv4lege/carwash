"""TELEGRAM_API_BASE: базовый адрес Bot API (прокси/зеркало, необязательный)."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiogram.client.telegram import PRODUCTION

from bot import telegram_api


class TelegramApiBaseTest(unittest.TestCase):
    def test_default_is_production(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TELEGRAM_API_BASE", None)
            self.assertIs(telegram_api(), PRODUCTION)

    def test_empty_values_use_production(self):
        for raw in ("", " "):
            with mock.patch.dict(os.environ, {"TELEGRAM_API_BASE": raw}):
                self.assertIs(telegram_api(), PRODUCTION)

    def test_custom_base_builds_server(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_API_BASE": "https://proxy.example/tg/"}):
            server = telegram_api()
        self.assertEqual(server.base, "https://proxy.example/tg/bot{token}/{method}")
        self.assertEqual(server.file, "https://proxy.example/tg/file/bot{token}/{path}")
        self.assertEqual(server.api_url("1:A", "getMe"), "https://proxy.example/tg/bot1:A/getMe")
        self.assertEqual(server.file_url("1:A", "docs/x.pdf"), "https://proxy.example/tg/file/bot1:A/docs/x.pdf")

    def test_invalid_values_rejected(self):
        for raw in ("ftp://x", "proxy.example", "https://x?q=1", "https://x#f"):
            with mock.patch.dict(os.environ, {"TELEGRAM_API_BASE": raw}):
                with self.assertRaises(RuntimeError):
                    telegram_api()


if __name__ == "__main__":
    unittest.main()
