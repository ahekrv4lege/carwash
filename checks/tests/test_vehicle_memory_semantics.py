"""Память об автомобиле без оформления записи.

Сохраняется только подтверждённый словами клиента автомобиль, о котором он говорит
как о своём. Догадки, чужие машины и предположения не сохраняются. Перезапуск
имитируется новым агентом над тем же файлом SQLite.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from ai import Agent, ChatUser, car_ownership
from tests.fakes import fixture
from tests.test_conversation import response


class OwnershipPhraseTests(unittest.TestCase):
    def test_possessive_phrases_mean_ownership(self):
        for text in ("У меня камрюха 3.5", "моя машина королла", "у нас тойота", "мою тачку помыть"):
            with self.subTest(text=text):
                self.assertTrue(car_ownership(text))

    def test_quote_selects_the_clause_with_the_fact(self):
        text = "У меня камри, но у друга лансер"
        self.assertTrue(car_ownership(text, "камри"))
        self.assertFalse(car_ownership(text, "лансер"))
        self.assertFalse(car_ownership("У друга лансер, а у меня камри", "лансер"))
        self.assertTrue(car_ownership("У друга лансер, а у меня камри", "камри"))

    def test_contrastive_correction_counts_as_ownership(self):
        self.assertTrue(car_ownership("У меня не камри, а лансер", "лансер"))
        self.assertFalse(car_ownership("У меня камри, а у друга лансер", "лансер"))
        self.assertFalse(car_ownership("У меня камри, а если лансер", "лансер"))
        self.assertFalse(car_ownership("У меня камри, а брат купил лансер", "лансер"))
        self.assertTrue(car_ownership("У меня камри, а брат купил лансер", "камри"))

    def test_hypothetical_and_third_party_cars_are_not_ownership(self):
        for text in ("если у меня была бы камри", "у друга камри", "или моя королла", "наверное у меня камри",
                     "Какая цена на мойку?", "а что если моя машина сломалась"):
            with self.subTest(text=text):
                self.assertFalse(car_ownership(text))


class MemoryWithoutBookingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store, self.spreadsheet = fixture(self.root)
        self.agent = self.make_agent()
        self.user = ChatUser(111, None, "Synthetic")

    def make_agent(self):
        with patch("ai.AsyncOpenAI"):
            agent = Agent(self.store.config, self.store)
        self.addCleanup(agent.cars.close)
        agent.client.chat.completions.create = AsyncMock()
        return agent

    async def say(self, agent, text, *outputs):
        agent.client.chat.completions.create.side_effect = list(outputs)
        result = await agent.reply(self.user, text)
        agent.mark_delivered(self.user.id, agent.delivery_token(self.user.id))
        return result

    def facts(self, car, quote, model=None):
        payload = {"car": [car, quote]}
        if model:
            payload["car_model"] = [model, quote]
        return response(calls=[("respond", {"reply": "Запомнила.", "facts": payload, "tentative": {}})])

    async def test_owned_vehicle_is_saved_without_booking_and_survives_restart(self):
        await self.say(self.agent, "У меня камрюха 3.5", self.facts("камрюха 3.5", "камрюха 3.5", "Toyota Camry 3.5"))
        self.assertEqual([c.name for c in self.agent.cars.for_user(111)], ["Toyota Camry 3.5"])
        self.assertEqual(len(self.spreadsheet.worksheet("Записи").values), 1)  # записи нет, только шапка

        restarted = self.make_agent()
        self.assertEqual([c.name for c in restarted.cars.for_user(111)], ["Toyota Camry 3.5"])

    async def test_mention_without_ownership_is_not_saved(self):
        await self.say(self.agent, "А у друга камри 3.5, сколько стоит?", self.facts("камри 3.5", "камри 3.5"))
        self.assertEqual(self.agent.cars.for_user(111), [])

    async def test_third_party_car_in_message_with_owned_car_is_not_saved(self):
        await self.say(self.agent, "У меня камри, но у друга лансер",
                       self.facts("лансер", "лансер", "Mitsubishi Lancer"))
        self.assertEqual(self.agent.cars.for_user(111), [])

    async def test_owned_car_in_mixed_message_is_saved(self):
        await self.say(self.agent, "У меня камри, но у друга лансер",
                       self.facts("камри", "камри", "Toyota Camry"))
        self.assertEqual([c.name for c in self.agent.cars.for_user(111)], ["Toyota Camry"])

    async def test_hypothetical_vehicle_is_not_saved(self):
        await self.say(self.agent, "Если моя королла, то сколько?", self.facts("королла", "королла"))
        self.assertEqual(self.agent.cars.for_user(111), [])

    async def test_tentative_vehicle_is_not_saved(self):
        call = ("respond", {"reply": "Уточню модель.", "facts": {}, "tentative": {"car": ["камри", "вроде камри"]}})
        await self.say(self.agent, "У меня вроде камри", response(calls=[call]))
        self.assertEqual(self.agent.cars.for_user(111), [])

    async def test_correction_replaces_class_and_keeps_one_record(self):
        await self.say(self.agent, "У меня камри 3.5",
                       self.facts("камри 3.5", "камри 3.5", "Toyota Camry 3.5"))
        await self.say(self.agent, "Нет, у меня лансер",
                       self.facts("лансер", "лансер", "Mitsubishi Lancer"))
        names = sorted(car.name for car in self.agent.cars.for_user(111))
        self.assertEqual(names, ["Mitsubishi Lancer", "Toyota Camry 3.5"])
        restarted = self.make_agent()
        self.assertEqual(sorted(c.name for c in restarted.cars.for_user(111)), names)

    async def test_ambiguous_model_is_not_saved_until_clarified(self):
        await self.say(self.agent, "У меня королла", self.facts("королла", "королла", "Toyota Corolla"))
        self.assertEqual(self.agent.cars.for_user(111), [])
        self.assertEqual(self.agent._state[111].get("facts", {}).get("car"), "королла")  # слова клиента сохранены

    async def test_contrastive_correction_is_saved(self):
        await self.say(self.agent, "У меня не камри, а лансер",
                       self.facts("лансер", "лансер", "Mitsubishi Lancer"))
        self.assertEqual([c.name for c in self.agent.cars.for_user(111)], ["Mitsubishi Lancer"])

    async def test_repeated_mention_does_not_duplicate(self):
        for _ in range(3):
            await self.say(self.agent, "У меня камри 3.5", self.facts("камри 3.5", "камри 3.5", "Toyota Camry 3.5"))
        self.assertEqual(len(self.agent.cars.for_user(111)), 1)

    async def test_memory_failure_does_not_break_reply(self):
        from vehicles import VehicleMemory
        with patch.object(VehicleMemory, "remember", side_effect=OSError("db down")):
            answer = await self.say(self.agent, "У меня камри 3.5", self.facts("камри 3.5", "камри 3.5"))
        self.assertIn("Запомнила", answer)
        self.assertEqual(self.agent.cars.for_user(111), [])


if __name__ == "__main__":
    unittest.main()
