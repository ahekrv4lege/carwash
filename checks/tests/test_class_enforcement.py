"""Класс автомобиля не может обойти правило прайса (лист «Классы»).

Правило проверяет Python на границе расчёта и записи. Модель, клиент и
предыдущий диалог не меняют класс, если для модели есть правило.
Всё офлайн: таблица и нейросеть синтетические.
"""
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from ai import Agent, ChatUser
from sheets import Catalog, Service, Tariff
from tests.fakes import Worksheet, booking, fixture
from tests.test_conversation import ARGS, response


def make_catalog(rules):
    services = {"мойка": Service("Мойка", "", True, {"Легковой": 700, "Кроссовер": 800})}
    tariffs = {"базовый": Tariff("Базовый", ["Мойка"], 0.0, True)}
    return Catalog(["Легковой", "Кроссовер"], services, tariffs, rules)


class RuleMatchingTests(unittest.TestCase):
    def test_whole_word_tokens_do_not_confuse_similar_codes(self):
        catalog = make_catalog([("BMW X5", "Кроссовер")])
        self.assertEqual(catalog.class_rule_for("BMW X50"), ("none", None))
        self.assertEqual(catalog.class_rule_for("BMW X5 2020 дизель"), ("rule", "Кроссовер"))
        self.assertEqual(catalog.class_rule_for("bmw x5"), ("rule", "Кроссовер"))

    def test_most_specific_rule_wins(self):
        catalog = make_catalog([("Toyota Camry", "Легковой"), ("Toyota Camry Hybrid", "Кроссовер")])
        self.assertEqual(catalog.class_rule_for("Toyota Camry Hybrid 2.5"), ("rule", "Кроссовер"))
        self.assertEqual(catalog.class_rule_for("Toyota Camry 3.5"), ("rule", "Легковой"))

    def test_equally_specific_rules_with_different_classes_are_ambiguous(self):
        catalog = make_catalog([("Audi Q5", "Кроссовер"), ("Q5 TDI", "Легковой")])
        self.assertEqual(catalog.class_rule_for("Audi Q5 TDI"), ("ambiguous", None))
        self.assertEqual(catalog.resolve_class_rule("Audi Q5 TDI"), None)

    def test_unknown_car_has_no_rule(self):
        catalog = make_catalog([("BMW X5", "Кроссовер")])
        self.assertEqual(catalog.class_rule_for("Lada Vesta"), ("none", None))
        self.assertEqual(catalog.class_rule_for(""), ("none", None))

    def test_problem_reports_mismatch_and_ambiguity_but_allows_matching_or_unknown(self):
        catalog = make_catalog([("BMW X5", "Кроссовер"), ("Audi Q5", "Кроссовер"), ("Q5 TDI", "Легковой")])
        mismatch = catalog.class_rule_problem("BMW X5", "Легковой")
        self.assertFalse(mismatch["ok"])
        self.assertEqual(mismatch["code"], "class_rule_mismatch")
        self.assertEqual(mismatch["required_class"], "Кроссовер")
        self.assertIsNone(catalog.class_rule_problem("BMW X5", "Кроссовер"))
        self.assertEqual(catalog.class_rule_problem("Audi Q5 TDI", "Кроссовер")["code"], "class_rule_ambiguous")
        self.assertIsNone(catalog.class_rule_problem("Lada Vesta", "Легковой"))
        self.assertIsNone(catalog.class_rule_problem("", "Легковой"))


class StoreEnforcementTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / "tmp"
        root.mkdir(exist_ok=True)
        self.mapping = root / ("enforce-" + uuid4().hex + ".json")
        self.addCleanup(self.mapping.unlink, missing_ok=True)
        self.store, self.spreadsheet = fixture(root, sheets_mapping_file=str(self.mapping))
        self.spreadsheet.sheets.append(Worksheet(5, "Классы", [["Автомобиль", "Класс"], ["BMW X5", "Кроссовер"]]))

    def test_calculate_price_refuses_cheaper_class_for_ruled_car(self):
        result = self.store.calculate_price("Легковой", "Базовый", [], car="BMW X5")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "class_rule_mismatch")
        self.assertNotIn("total", result)

    def test_calculate_price_uses_rule_class_and_legacy_call_is_unchanged(self):
        self.assertEqual(self.store.calculate_price("Кроссовер", "Базовый", [], car="BMW X5")["total"], 720)
        self.assertEqual(self.store.calculate_price("Легковой", "Базовый", [])["total"], 630)

    def test_booking_with_wrong_class_is_not_written(self):
        worksheet = self.spreadsheet.worksheet("Записи")
        rows_before = len(worksheet.values)
        result = self.store.create_booking(**booking(car="BMW X5", size_class="Легковой"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "class_rule_mismatch")
        self.assertEqual(len(worksheet.values), rows_before)
        self.assertEqual(sum(len(sheet.writes) for sheet in self.spreadsheet.sheets), 0)

    def test_booking_with_rule_class_succeeds(self):
        result = self.store.create_booking(**booking(car="BMW X5", size_class="Кроссовер"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["total"], 720)


class AgentEnforcementTests(unittest.IsolatedAsyncioTestCase):
    """Модель не может подменить класс: инструменты получают подтверждённый автомобиль из состояния."""

    def setUp(self):
        root = Path(__file__).resolve().parents[2] / "tmp"
        root.mkdir(exist_ok=True)
        self.mapping = root / ("enforce-agent-" + uuid4().hex + ".json")
        self.addCleanup(self.cleanup)
        self.store, self.spreadsheet = fixture(root, sheets_mapping_file=str(self.mapping))
        self.spreadsheet.sheets.append(Worksheet(5, "Классы", [["Автомобиль", "Класс"], ["BMW X5", "Кроссовер"]]))
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(self.store.config, self.store)
        self.transport = AsyncMock()
        self.agent.client.chat.completions.create = self.transport
        self.user = ChatUser(111, None, "Synthetic")

    def cleanup(self):
        for path in (self.mapping, self.mapping.with_name(self.mapping.stem + "-operations.json")):
            path.unlink(missing_ok=True)

    async def say(self, text, *outputs):
        self.transport.side_effect = list(outputs)
        result = await self.agent.reply(self.user, text)
        self.agent.mark_delivered(self.user.id, self.agent.delivery_token(self.user.id))
        return result

    async def test_model_cannot_price_ruled_car_with_cheaper_class(self):
        self.agent._state[111]["facts"] = {"car": "BMW X5"}
        raw = await self.agent._call_tool("calculate_price", json.dumps({"size_class": "Легковой", "tariff": "Базовый"}), self.user)
        self.assertFalse(raw["ok"])
        self.assertEqual(raw["code"], "class_rule_mismatch")
        self.assertNotIn("total", raw)

    async def test_wrong_class_in_booking_creates_no_proposal(self):
        self.agent._state[111]["facts"] = {"car": "BMW X5", "size_class": "Легковой", "tariff": "Базовый",
                                           "date": "2099-01-01", "time": "10:00", "client_name": "Synthetic",
                                           "phone": "00000000000"}
        raw = await self.agent._call_tool("create_booking", json.dumps({"size_class": "Легковой"}), self.user)
        self.assertFalse(raw["ok"], raw)
        self.assertEqual(raw.get("code"), "class_rule_mismatch", raw)
        self.assertFalse(self.agent._pending.get(self.user.id))
        self.assertEqual(sum(len(sheet.writes) for sheet in self.spreadsheet.sheets), 0)
        self.assertEqual(sum(len(sheet.writes) for sheet in self.spreadsheet.sheets), 0)


if __name__ == "__main__":
    unittest.main()
