"""Настоящие API. Telegram и Sheets только читаются; AI-запросы расходуют квоту."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import unittest

from aiogram import Bot
import gspread
from openai import AsyncOpenAI

from ai import TOOLS
from config import ProviderConfig
from providers import client_options, completion_options
from run import validate_config
from sheets import Store, SheetsHTTPClient

READ_ONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


def failure(error):
    status = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    detail = f", HTTP {status}" if status is not None else ""
    return AssertionError(f"Настоящий API недоступен: {type(error).__name__}{detail}; ключи и тело ответа скрыты")


class Telegram(unittest.IsolatedAsyncioTestCase):
    async def test_real_identity_commands_and_polling_configuration(self):
        bot = Bot(self.config.bot_token)
        try:
            identity = await bot.get_me(request_timeout=20)
            self.assertTrue(identity.is_bot)
            self.assertEqual(identity.id, int(self.config.bot_token.split(":", 1)[0]))
            webhook = await bot.get_webhook_info(request_timeout=20)
            self.assertFalse(webhook.url, "У бота включён webhook; приложение настроено на polling")
            commands = await bot.get_my_commands(request_timeout=20)
            self.assertTrue(all(command.command for command in commands))
        except AssertionError:
            raise
        except Exception as error:
            raise failure(error) from None
        finally:
            await bot.session.close()


class GoogleSheets(unittest.TestCase):
    def test_real_catalog_slots_bookings_and_price_with_readonly_credentials(self):
        google = gspread.service_account(filename=self.config.google_credentials_file, scopes=[READ_ONLY_SCOPE], http_client=SheetsHTTPClient)
        google.set_timeout((10, 20))
        try:
            store = Store(self.config)
            store._spreadsheet = google.open_by_key(self.config.spreadsheet_id)
            for role in ("services", "tariffs", "slots", "bookings"):
                with self.subTest(role=role):
                    headers, records = store._table(getattr(self.config, "sheet_" + role))
                    self.assertTrue(headers, f"Пустая шапка таблицы {role}")
                    self.assertIsInstance(records, list)
            catalog = store.get_catalog(fresh=True)
            self.assertTrue(catalog.size_classes, "В прайсе нет классов автомобилей")
            available = [(service, size_class) for service in catalog.services.values() if service.available
                         for size_class in service.prices if size_class in catalog.size_classes]
            self.assertTrue(available, "Нет доступной услуги с ценой")
            service, size_class = available[0]
            quote = store.calculate_price(size_class, None, [service.name])
            self.assertTrue(quote["ok"], "Расчёт цены отклонён")
            self.assertEqual(quote["total"], service.prices[size_class])
            self.assertTrue(store.free_slots()["ok"], "Чтение свободного времени отклонено")
        except AssertionError:
            raise
        except Exception as error:
            raise failure(error) from None
        finally:
            google.http_client.session.close()


class AIProvider(unittest.IsolatedAsyncioTestCase):
    async def test_real_model_can_call_production_price_tool(self):
        config = replace(self.config, ai_base_url=self.provider.base_url,
                         ai_api_key=self.provider.api_key, ai_model=self.provider.model)
        client = AsyncOpenAI(**client_options(config))
        tool = next(tool for tool in TOOLS if tool["function"]["name"] == "get_price_list")
        options = completion_options(config)
        options[config.ai_token_limit_parameter] = 512
        try:
            response = await asyncio.wait_for(client.chat.completions.create(
                **options,
                messages=[{"role": "system", "content": "Для ответа на вопрос об услугах сначала вызови get_price_list без аргументов. Не отвечай текстом до вызова инструмента."},
                          {"role": "user", "content": "Какие услуги есть у автомойки?"}],
                tools=[tool], tool_choice="auto"), timeout=config.ai_timeout_sec)
            self.assertTrue(response.choices, "Модель вернула пустой choices")
            choice = response.choices[0]
            self.assertNotIn(choice.finish_reason, {"length", "content_filter"}, "Ответ модели не завершён")
            calls = choice.message.tool_calls or []
            self.assertEqual(len(calls), 1, f"Модель {config.ai_model} не вызвала один инструмент прайса")
            self.assertEqual(calls[0].function.name, "get_price_list")
            self.assertEqual(json.loads(calls[0].function.arguments or "{}"), {}, "Модель добавила незапрошенные аргументы")
        except AssertionError:
            raise
        except Exception as error:
            raise failure(error) from None
        finally:
            await client.close()


def suite():
    config = validate_config()
    providers = [ProviderConfig(config.ai_base_url, config.ai_api_key, config.ai_model), *config.llm_fallbacks]
    result = unittest.TestSuite()
    for base in (Telegram, GoogleSheets):
        case = type(base.__name__, (base,), {"config": config, "__module__": __name__})
        result.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    for index, provider in enumerate(providers, 1):
        case = type(f"AI_key_{index}", (AIProvider,), {
            "config": config, "provider": provider, "__module__": __name__,
        })
        result.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    return result
