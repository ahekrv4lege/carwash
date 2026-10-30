"""Правила классов из листа «Классы» (задача №5 из TODO.md).

Модель подходит под несколько категорий (BMW X5 = кроссовер/внедорожник):
правило задаётся администратором в таблице, бот читает его, передаёт
нейросети вместе с прайсом, и нейросеть выбирает класс сама, без уточнений
клиенту. Всё офлайн: модель и таблица синтетические.
"""
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from ai import Agent, ChatUser
from sheets import Store
from tests.fakes import Worksheet, fixture
from tests.test_conversation import ARGS, response


def classes_sheet(rows: list) -> Worksheet:
    return Worksheet(5, "Классы", [["Автомобиль", "Класс"], *rows])


X5_ARGS = dict(ARGS, car="BMW X5", size_class="Кроссовер")


class ClassRulesStoreTests(unittest.TestCase):
    """Чтение правил из таблицы: что попало в прайс, а что отброшено."""

    def setUp(self):
        root = Path(__file__).resolve().parents[2] / "tmp"
        root.mkdir(exist_ok=True)
        self.mapping = root / ("classes-test-" + uuid4().hex + ".json")
        self.addCleanup(self.mapping.unlink)
        self.store, self.spreadsheet = fixture(root, sheets_mapping_file=str(self.mapping))

    def add_rules(self, rows: list) -> Store:
        self.spreadsheet.sheets.append(classes_sheet(rows))
        return self.store

    def test_price_list_contains_rules_for_multi_class_cars(self):
        self.add_rules([["BMW X5", "Кроссовер"], ["Лада Веста", "Легковой"]])
        rules = self.store.price_list()["class_rules"]
        self.assertEqual(rules, [{"car": "BMW X5", "class": "Кроссовер"},
                                 {"car": "Лада Веста", "class": "Легковой"}])

    def test_rule_class_matched_without_regard_to_case(self):
        self.add_rules([["BMW X5", "кроссовер"]])
        rules = self.store.price_list()["class_rules"]
        self.assertEqual(rules, [{"car": "BMW X5", "class": "Кроссовер"}])

    def test_rule_resolves_car_exactly_and_by_part_of_name(self):
        self.add_rules([["BMW X5", "Кроссовер"]])
        catalog = self.store.get_catalog()
        self.assertEqual(catalog.resolve_class_rule("BMW X5"), "Кроссовер")
        self.assertEqual(catalog.resolve_class_rule("bmw x5"), "Кроссовер")
        self.assertEqual(catalog.resolve_class_rule("BMW X5 2020 дизель"), "Кроссовер")
        self.assertEqual(catalog.resolve_class_rule("Х5"), None)

    def test_unknown_class_rule_is_dropped(self):
        self.add_rules([["Велосипед", "Мотоцикл"], ["BMW X5", "Кроссовер"]])
        rules = self.store.price_list()["class_rules"]
        self.assertEqual(rules, [{"car": "BMW X5", "class": "Кроссовер"}])

    def test_contradictory_rules_are_dropped_entirely(self):
        self.add_rules([["BMW X5", "Кроссовер"], ["BMW X5", "Легковой"]])
        self.assertEqual(self.store.price_list()["class_rules"], [])

    def test_missing_classes_sheet_changes_nothing(self):
        data = self.store.price_list()
        self.assertTrue(data["ok"])
        self.assertEqual(data["class_rules"], [])

    def test_empty_cells_and_wrong_headers_are_ignored(self):
        self.add_rules([["", "Кроссовер"], ["BMW X5", ""]])
        self.assertEqual(self.store.price_list()["class_rules"], [])


class ClassRulesAgentTests(unittest.IsolatedAsyncioTestCase):
    """Правила доезжают до нейросети, и класс выбирается без вопросов клиенту."""

    def setUp(self):
        root = Path(__file__).resolve().parents[2] / "tmp"
        root.mkdir(exist_ok=True)
        self.mapping = root / ("classes-agent-" + uuid4().hex + ".json")
        self.addCleanup(self.cleanup)
        self.store, self.spreadsheet = fixture(root, sheets_mapping_file=str(self.mapping))
        self.spreadsheet.sheets.append(classes_sheet([["BMW X5", "Кроссовер"]]))
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

    async def test_rules_reach_the_model_with_price_list(self):
        await self.say("БМВ Х5 — это кроссовер или внедорожник?", response(calls=[("get_price_list", {})]))
        catalog = self.agent._state[111]["catalog"]
        self.assertIn({"car": "BMW X5", "class": "Кроссовер"}, catalog["class_rules"])

    async def test_prompt_mentions_rules_and_no_clarifying_question_is_needed(self):
        self.assertIn("class_rules", self.agent._system_prompt())
        answer = await self.say("Хочу обсудить запись: " + json.dumps(X5_ARGS, ensure_ascii=False),
                                response(calls=[("get_price_list", {})]),
                                response(calls=[("create_booking", X5_ARGS)]))
        # Запись предложена прямо по правилу прайса, без переспроса категории.
        self.assertIn("«Подтвердить»", answer)
        self.assertNotIn("уточните", answer.lower())
        self.assertNotIn("кроссовер или внедорожник", answer.lower())
        self.assertTrue(self.agent._pending)
        self.assertEqual(sum(len(sheet.writes) for sheet in self.spreadsheet.sheets), 0)

    async def test_booking_with_rule_class_confirms_normally(self):
        await self.say("Хочу обсудить запись: " + json.dumps(X5_ARGS, ensure_ascii=False),
                       response(calls=[("get_price_list", {})]),
                       response(calls=[("create_booking", X5_ARGS)]))
        # Подтверждает только кнопка на доставленной сводке.
        offer_id = self.agent._pending[self.user.id]["offer_id"]
        answer = await self.agent.proposal_action(self.user, offer_id, "confirm")
        self.assertIn("Записала", answer)
        created = self.spreadsheet.worksheet("Записи").values[-1]
        self.assertIn("BMW X5", created)
        self.assertIn("Кроссовер", created)


if __name__ == "__main__":
    unittest.main()
