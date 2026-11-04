"""Госномер при записи: колонка через адаптер, флаг обязательности, без допроса известного.

Старые листы без колонки «Госномер» продолжают работать; правки клиента текстом
попадают через обычную память фактов (plate в FIELDS).
"""
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from ai import Agent, ChatUser
from conversation import model_state
from tests.fakes import booking, fixture
from tests.test_conversation import ARGS, response

import asyncio
from unittest.mock import AsyncMock, patch


class OfflinePlateTests(unittest.TestCase):
    """Не-адаптивный путь: запись содержит колонку, если она есть в листе."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name, sheets_adaptive_enabled=False)
        self.sheet = self.spreadsheet.worksheet("Записи")

    def with_plate_column(self, header="Госномер"):
        self.sheet.values[0].append(header)
        return self.store.create_booking(**booking(plate="а 123 ав 77"))

    def test_plate_is_written_to_the_sheet(self):
        result = self.with_plate_column()
        self.assertTrue(result["ok"], result)
        self.assertEqual(str(self.sheet.values[1][-1]), "а 123 ав 77")
        result = self.store.create_booking(**booking(telegram_id=222, car="Other", plate="в 777 вв 11"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(str(self.sheet.values[2][-1]), "в 777 вв 11")

    def test_plate_is_written_via_alias_header(self):
        # Реальная колонка может называться «Гос. Номер»: ищем по алиасам заголовка.
        result = self.with_plate_column("Гос. Номер")
        self.assertTrue(result["ok"], result)
        plate_column = self.sheet.values[0].index("Гос. Номер")
        self.assertEqual(str(self.sheet.values[1][plate_column]), "а 123 ав 77")

    def test_flag_demands_the_plate_when_column_exists(self):
        required = replace(self.store.config, car_plate_required=True)
        from sheets import Store
        store = Store(required)
        store._spreadsheet = self.spreadsheet
        self.sheet.values[0].append("Госномер")
        store._invalidate("bookings")
        result = store.create_booking(**booking())
        self.assertFalse(result["ok"])
        self.assertIn("госномер", str(result["error"]).lower())
        created = store.create_booking(**booking(plate="а 123 ав 77"))
        self.assertTrue(created["ok"], created)
        self.assertEqual(str(self.sheet.values[1][-1]), "а 123 ав 77")

    def test_old_table_without_plate_column_keeps_working(self):
        # Флаг включён, но колонки в старом листе нет: запись работает, номер просто некуда писать.
        required = replace(self.store.config, car_plate_required=True)
        from sheets import Store
        store = Store(required)
        store._spreadsheet = self.spreadsheet
        store._invalidate("bookings")
        result = store.create_booking(**booking())
        self.assertTrue(result["ok"], result)  # старая таблица продолжает работать
        self.assertNotIn("госномер", str(self.sheet.values[0]))  # колонку за клиентом не создали
        self.assertEqual(len(self.sheet.values) - 1, 1)  # ровно одна строка записи

    def test_long_plate_value_is_trimmed(self):
        self.with_plate_column()
        self.assertLessEqual(len(str(self.sheet.values[1][-1])), 40)


class AdaptivePlateTests(unittest.TestCase):
    """Адаптивная раскладка: колонка «Номер авто» распознаётся алиасом и пишется."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        self.sheet = self.spreadsheet.worksheet("Записи")
        self.sheet.values[0].append("Номер авто")  # алиас колонки компаний
        self.store._adaptive.refresh(force=True, validate=True)
        self.store._invalidate("bookings")

    def test_alias_column_is_mapped_and_written(self):
        result = self.store.create_booking(**booking(plate="а 123 ав 77"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(str(self.sheet.values[1][-1]), "а 123 ав 77")
        # Прежние колонки не сместились: дата на прежнем месте.
        self.assertEqual(str(self.sheet.values[1][2]), "01.01.2099")

    def test_without_column_the_sheet_stays_valid_and_writable(self):
        # Обычный адаптивный лист без колонки «Госномер» работает как раньше.
        store2, _ = fixture(self.temp.name, sheets_adaptive_enabled=False)
        result = store2.create_booking(**booking())
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.sheet.values) - 1, 0)  # колонку не создали — ничего не дописали


class AgentSchemaTests(unittest.TestCase):
    """Схема create_booking: plate обязателен только по флагу и только без известного факта."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(replace(self.store.config, car_plate_required=True), self.store)
        self.agent._state[111] = {"stage": "collecting", "facts": {}}
        self.addCleanup(self.agent.cars.close)

    def test_plate_required_by_flag_when_not_yet_known(self):
        tools = self.agent._tools(ChatUser(111, None, "Synthetic"), only="create_booking")
        schema = tools[0]["function"]["parameters"]
        self.assertIn("plate", schema["required"])
        self.assertIn("plate", schema["properties"])

    def test_known_plate_is_not_asked_again(self):
        self.agent._state[111]["facts"]["plate"] = "а 123 ав 77"
        tools = self.agent._tools(ChatUser(111, None, "Synthetic"), only="create_booking")
        schema = tools[0]["function"]["parameters"]
        self.assertNotIn("plate", schema["required"])
        self.assertNotIn("plate", schema["properties"])

    def test_flag_off_keeps_schema_without_plate_requirement(self):
        self.agent.config = replace(self.agent.config, car_plate_required=False)
        tools = self.agent._tools(ChatUser(111, None, "Synthetic"), only="create_booking")
        schema = tools[0]["function"]["parameters"]
        self.assertNotIn("plate", schema.get("required", []))


class ModelStateTests(unittest.TestCase):
    def test_missing_plate_only_when_required_and_unknown(self):
        state = {"stage": "collecting"}
        self.assertNotIn("plate", model_state(state)["missing"])
        self.assertIn("plate", model_state(state, True)["missing"])
        known = {"facts": {"plate": "а 123 ав 77"}}
        self.assertNotIn("plate", model_state(known, True)["missing"])


class DirectPlateAskTests(unittest.IsolatedAsyncioTestCase):
    """Оптимизация: вопрос про госномер задаёт Python без второго поколения модели."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(replace(self.store.config, car_plate_required=True), self.store)
        self.transport = AsyncMock()
        self.agent.client.chat.completions.create = self.transport
        self.user = ChatUser(111, None, "Synthetic")
        self.addCleanup(self.agent.cars.close)

    async def test_missing_plate_is_answered_without_extra_model_generation(self):
        self.agent._state[111].clear()
        self.agent._state[111].update(stage="collecting", facts=dict(ARGS))
        self.transport.side_effect = [response(calls=[("create_booking", {})])]
        answer = await self.agent.reply(self.user, "Запишите меня завтра")
        self.assertIn("госномер", answer.lower())
        self.assertEqual(self.transport.call_count, 1)  # один вызов модели: вопрос задал Python
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)  # запись не начиналась

    def test_proposal_message_shows_the_plate(self):
        offer = {"action": "create_booking", "args": {"date": "2099-01-01", "time": "10:00",
                 "car": "Toyota Camry", "plate": "а 123 ав 77", "client_name": "Саша",
                 "phone": "70001112233", "comment": ""},
                 "quote": {"size_class": "Легковой", "tariff": "Базовый", "lines": [], "total": 700, "currency": "₽"}}
        text = self.agent._proposal_text(offer)
        self.assertIn("Госномер: а 123 ав 77", text)


if __name__ == "__main__":
    unittest.main()
