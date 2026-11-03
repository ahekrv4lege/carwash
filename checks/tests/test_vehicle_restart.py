"""Память об автомобилях через полный перезапуск процесса.

Новый Agent над тем же файлом SQLite имитирует перезапуск бота: RAM-кэш и
STATE пусты, база на диске сохраняется. Проверяется, что подтверждённый
автомобиль возвращается в факты диалога без запроса к модели, что чужие
записи не видны, а /start, /reset, истечение сессии и удаление записи
не подменяют данные клиента.
"""
import json
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ai import Agent, ChatUser
from bot import cmd_forgetcar, cmd_mycars, cmd_reset, cmd_start
from tests.fakes import fixture
from tests.test_conversation import ARGS, response
from vehicles import VehicleMemory


class RestartMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        self.user = ChatUser(111, None, "Synthetic")
        self.other = ChatUser(222, None, "Other")
        self.agent = self.new_agent()

    def new_agent(self):
        with patch("ai.AsyncOpenAI"):
            agent = Agent(self.store.config, self.store)
        self.addCleanup(agent.cars.close)
        agent.client.chat.completions.create = AsyncMock()
        return agent

    async def restart(self):
        """Новый процесс: закрыть прежнюю базу и создать Agent с пустым RAM."""
        self.agent.cars.close()
        self.agent = self.new_agent()
        return self.agent

    async def say(self, agent, user, text, *outputs):
        agent.client.chat.completions.create.side_effect = list(outputs)
        answer = await agent.reply(user, text)
        agent.mark_delivered(user.id, agent.delivery_token(user.id, answer))
        return answer

    @staticmethod
    def saved(car, quote=None, model=None, cls=None):
        quote = quote or car
        facts = {"car": [car, quote]}
        if model:
            facts["car_model"] = [model, quote]
        if cls:
            facts["size_class"] = [cls, quote]
        return response(calls=[("respond", {"reply": "Запомнила.", "facts": facts, "tentative": {}})])

    async def remember_camry(self, agent=None, user=None):
        agent = agent or self.agent
        user = user or self.user
        await self.say(agent, user, "У меня Toyota Camry", self.saved("Toyota Camry", cls="Легковой"))
        return agent

    async def book(self, agent, car="Toyota Camry 3.5", services=None):
        args = dict(ARGS, car=car, services=list(services or []))
        await self.say(agent, self.user, "Хочу обсудить запись: " + json.dumps(args, ensure_ascii=False),
                       response(calls=[("get_price_list", {})]),
                       response(calls=[("create_booking", args)]))
        quote = dict(agent._pending[self.user.id]["quote"])
        offer = agent._pending[self.user.id]["offer_id"]
        answer = await agent.proposal_action(self.user, offer, "confirm")
        return quote, answer

    def message(self, user_id=111):
        return SimpleNamespace(from_user=SimpleNamespace(id=user_id), answer=AsyncMock())

    async def test_vehicle_is_committed_without_booking_and_restored_after_restart(self):
        await self.remember_camry()
        self.assertEqual(len(self.spreadsheet.worksheet("Записи").values), 1)  # только шапка
        saved = self.agent.cars.for_user(111)[0]
        self.assertEqual((saved.name, saved.size_class), ("Toyota Camry", "Легковой"))

        restarted = await self.restart()
        self.assertEqual(len(restarted.cars.cache), 0)  # RAM-кэш пуст
        self.assertEqual(dict(restarted._state[111]), {})  # STATE пуст

        await self.say(restarted, self.user, "Хочу записаться на мойку завтра в 10:00",
                       response(text="Как вас зовут и какой телефон?"))
        facts = restarted._state[111]["facts"]
        self.assertEqual(facts["car"], "Toyota Camry")
        self.assertEqual(facts["car_model"], "Toyota Camry")
        self.assertEqual(facts["size_class"], "Легковой")
        self.assertEqual(restarted.cars.for_user(111)[0].id, saved.id)  # ID записи не изменился
        sent = restarted.client.chat.completions.create.call_args.kwargs["messages"]
        customer = next(m["content"] for m in sent if m["content"].startswith("CUSTOMER "))
        self.assertIn("Toyota Camry", customer)
        self.assertFalse([m for m in sent if m["content"].startswith("SAVED_CARS ")])
        self.assertEqual(restarted.client.chat.completions.create.await_count, 1)  # без лишнего запроса к модели

    async def test_restarted_customer_books_without_naming_the_vehicle_again(self):
        await self.remember_camry()
        saved_id = self.agent.cars.for_user(111)[0].id
        restarted = await self.restart()
        await self.say(restarted, self.user, "Хочу на мойку", response(text="Как вас зовут и телефон?"))
        facts = restarted._state[111]["facts"]
        facts.update(date="2099-01-01", time="10:00", client_name="Synthetic",
                     phone="00000000000", tariff="Базовый")

        result = await restarted._call_tool("create_booking", "{}", self.user)
        self.assertTrue(result.get("confirmation_required"), result)
        pending = restarted._pending[111]
        self.assertEqual(pending["args"]["car"], "Toyota Camry")  # машина из памяти, не из сообщения
        text = restarted._offer(111, pending)
        restarted.mark_delivered(111, restarted.delivery_token(111, text))
        self.assertIn("Записала", await restarted.proposal_action(self.user, pending["offer_id"], "confirm"))
        self.assertEqual(len(self.spreadsheet.worksheet("Записи").writes), 1)
        after = restarted.cars.for_user(111)[0]
        self.assertEqual(after.id, saved_id)  # повторная запись не создала новый автомобиль
        self.assertEqual(after.last_tariff, "Базовый")

    async def test_mycars_lists_vehicle_after_restart(self):
        await self.remember_camry()
        restarted = await self.restart()
        message = self.message()
        await cmd_mycars(message, restarted)
        text = message.answer.await_args.args[0]
        self.assertIn("Toyota Camry", text)
        self.assertIn("Легковой", text)

    async def test_other_telegram_id_cannot_see_or_restore_foreign_vehicle(self):
        await self.remember_camry()
        restarted = await self.restart()
        await self.say(restarted, self.other, "Здравствуйте, хочу на мойку",
                       response(text="Какой у вас автомобиль?"))
        self.assertNotIn("car", restarted._state[222].get("facts", {}))
        sent = restarted.client.chat.completions.create.call_args.kwargs["messages"]
        self.assertFalse([m for m in sent if m["content"].startswith("SAVED_CARS ")])
        message = self.message(222)
        await cmd_mycars(message, restarted)
        self.assertNotIn("Toyota Camry", message.answer.await_args.args[0])
        self.assertEqual(restarted.cars.for_user(111)[0].name, "Toyota Camry")  # чужая запись цела

    async def test_start_and_reset_keep_long_term_vehicle(self):
        await self.remember_camry()
        restarted = await self.restart()
        for command in (cmd_start, cmd_reset):
            message = self.message()
            if command is cmd_start:
                await command(message, restarted, self.store.config)
            else:
                await command(message, restarted)
                self.assertIn("сброшен", message.answer.await_args.args[0].lower())
            self.assertNotIn("facts", restarted._state.get(111, {}))  # временный диалог очищен
            self.assertFalse(restarted._history.get(111))
            await self.say(restarted, self.user, "Привет", response(text="Здравствуйте!"))
            self.assertEqual(restarted._state[111]["facts"]["car"], "Toyota Camry")
            self.assertEqual(restarted.cars.for_user(111)[0].name, "Toyota Camry")

    async def test_session_expiration_restores_vehicle_on_next_message(self):
        await self.remember_camry()
        await self.say(self.agent, self.user, "Привет", response(text="Здравствуйте!"))
        expired_at = max(self.agent._last_seen.values()) + self.store.config.ai_session_ttl_sec + 1
        self.agent.cleanup_sessions(now=expired_at)
        self.assertNotIn(111, self.agent._state)
        await self.say(self.agent, self.user, "Привет снова", response(text="Здравствуйте!"))
        self.assertEqual(self.agent._state[111]["facts"]["car"], "Toyota Camry")

    async def test_multiple_vehicles_are_not_restored_arbitrarily(self):
        await self.remember_camry()
        await self.say(self.agent, self.user, "У меня ещё BMW X5",
                       self.saved("BMW X5", model="BMW X5", cls="Кроссовер"))
        self.assertEqual(sorted(car.name for car in self.agent.cars.for_user(111)),
                         ["BMW X5", "Toyota Camry"])

        restarted = await self.restart()
        await self.say(restarted, self.user, "Хочу на мойку", response(text="Какой автомобиль выбрать?"))
        self.assertNotIn("car", restarted._state[111].get("facts", {}))
        sent = restarted.client.chat.completions.create.call_args.kwargs["messages"]
        hint = next(m["content"] for m in sent if m["content"].startswith("SAVED_CARS "))
        self.assertIn("Toyota Camry", hint)
        self.assertIn("BMW X5", hint)
        restarted._state[111]["facts"].update(date="2099-01-01", time="10:00",
                                             client_name="Synthetic", phone="00000000000")
        with self.assertRaisesRegex(ValueError, "missing_booking_details:car"):
            restarted._booking_args({}, self.user)

    async def test_explicit_new_vehicle_overrides_restored_memory(self):
        await self.remember_camry()
        restarted = await self.restart()
        await self.say(restarted, self.user, "У меня теперь Kia Rio",
                       self.saved("Kia Rio", model="Kia Rio", cls="Легковой"))
        facts = restarted._state[111]["facts"]
        self.assertEqual(facts["car"], "Kia Rio")
        self.assertEqual(facts["car_model"], "Kia Rio")
        self.assertEqual(sorted(car.name for car in restarted.cars.for_user(111)),
                         ["Kia Rio", "Toyota Camry"])

    async def test_repeated_confirmation_keeps_one_record_and_updates_it(self):
        await self.remember_camry()
        saved_id = self.agent.cars.for_user(111)[0].id
        restarted = await self.restart()
        await self.say(restarted, self.user, "У меня Toyota Camry",
                       self.saved("Toyota Camry", cls="Кроссовер"))
        cars = restarted.cars.for_user(111)
        self.assertEqual(len(cars), 1)
        self.assertEqual(cars[0].id, saved_id)
        self.assertEqual(cars[0].size_class, "Кроссовер")
        again = await self.restart()
        await self.say(again, self.user, "Привет", response(text="Здравствуйте!"))
        self.assertEqual(again._state[111]["facts"]["car"], "Toyota Camry")

    async def test_deleted_vehicle_is_not_restored_and_leaves_dialogue(self):
        await self.remember_camry()
        restarted = await self.restart()
        await self.say(restarted, self.user, "Привет", response(text="Здравствуйте!"))
        self.assertEqual(restarted._state[111]["facts"]["car"], "Toyota Camry")
        message = self.message()
        await cmd_forgetcar(message, SimpleNamespace(args="1"), restarted)
        self.assertIn("удалён", message.answer.await_args.args[0])
        self.assertNotIn("car", restarted._state[111].get("facts", {}))
        self.assertEqual(restarted.cars.for_user(111), [])

        again = await self.restart()
        await self.say(again, self.user, "Хочу на мойку", response(text="Какой у вас автомобиль?"))
        self.assertNotIn("car", again._state[111].get("facts", {}))

    async def test_forgetting_old_record_keeps_the_current_corrected_vehicle(self):
        await self.remember_camry()
        restarted = await self.restart()
        await self.say(restarted, self.user, "Привет", response(text="Здравствуйте!"))
        await self.say(restarted, self.user, "У меня теперь Kia Rio",
                       self.saved("Kia Rio", model="Kia Rio", cls="Легковой"))
        cars = restarted.cars.for_user(111)
        old = next(car for car in cars if car.name == "Toyota Camry")
        index = [car.id for car in cars].index(old.id) + 1
        await cmd_forgetcar(self.message(), SimpleNamespace(args=str(index)), restarted)
        self.assertEqual(restarted._state[111]["facts"]["car"], "Kia Rio")
        self.assertEqual([car.name for car in restarted.cars.for_user(111)], ["Kia Rio"])

    async def test_repeat_restores_last_tariff_and_services_with_fresh_price(self):
        self.spreadsheet.worksheet("Услуги").values.append(["Пылесос", "Салон", "200", "250", "да"])
        self.store._invalidate("catalog", "slots", "bookings")
        first_quote, answer = await self.book(self.agent, services=["Пылесос"])
        self.assertIn("Записала", answer)
        saved = self.agent.cars.for_user(111)[0]
        self.assertEqual(saved.last_tariff, "Базовый")
        self.assertEqual(saved.last_services, ("Пылесос",))

        services = self.spreadsheet.worksheet("Услуги").values
        services[1][2] = "1000"  # Мойка подорожала
        services[3][2] = "300"   # Пылесос подорожал
        self.spreadsheet.worksheet("Слоты").values.append(["01.01.2099", "11:00", "1"])
        self.store._invalidate("catalog", "slots", "bookings")

        restarted = await self.restart()
        await self.say(restarted, self.user, "Хочу как в прошлый раз 01.01.2099 в 11:00",
                       response(text="Уточните имя и телефон."))
        state = restarted._state[111]
        self.assertEqual(state["repeat_last"]["tariff"], "Базовый")
        self.assertEqual(state["repeat_last"]["services"], ["Пылесос"])
        state["facts"].update(client_name="Synthetic", phone="00000000000")

        result = await restarted._call_tool("create_booking", "{}", self.user)
        self.assertTrue(result.get("confirmation_required"), result)
        pending = restarted._pending[111]
        self.assertEqual(pending["args"]["car"], "Toyota Camry 3.5")
        self.assertEqual(pending["args"]["tariff"], "Базовый")
        self.assertEqual(pending["args"]["services"], ["Пылесос"])
        self.assertNotEqual(pending["quote"]["total"], first_quote["total"])  # не старая цена
        self.assertEqual(pending["quote"]["total"], 1200)  # 1000 - 10% + 300, свежий прайс

    async def test_repeat_does_not_reuse_stale_availability(self):
        first_quote, answer = await self.book(self.agent)
        self.assertIn("Записала", answer)
        self.assertEqual(self.agent.cars.for_user(111)[0].last_tariff, "Базовый")
        self.spreadsheet.worksheet("Слоты").values[1][2] = "0"  # 10:00 больше не принимает
        self.store._invalidate("slots", "bookings", "catalog")

        restarted = await self.restart()
        await self.say(restarted, self.user, "Хочу как в прошлый раз 01.01.2099 в 10:00",
                       response(text="Уточните имя и телефон."))
        restarted._state[111]["facts"].update(client_name="Synthetic", phone="00000000000")
        result = await restarted._call_tool("create_booking", "{}", self.user)
        self.assertFalse(result.get("ok"), result)
        self.assertIn("недоступно", result.get("error", "").lower())
        self.assertNotIn(111, restarted._pending)
        self.assertEqual(len(self.spreadsheet.worksheet("Записи").writes), 1)  # только первая запись

    async def test_saved_plate_is_restored_with_the_single_vehicle(self):
        self.agent.cars.remember(111, "Toyota Camry", "Легковой", plate="а 123 ав 77")
        restarted = await self.restart()
        await self.say(restarted, self.user, "Привет", response(text="Здравствуйте!"))
        self.assertEqual(restarted._state[111]["facts"]["plate"], "а 123 ав 77")

    async def test_database_read_failure_is_logged_and_not_faked_as_memory(self):
        with patch.object(VehicleMemory, "for_user",
                          side_effect=sqlite3.OperationalError("unable to open database file")):
            with self.assertLogs("ai", level="WARNING") as logs:
                answer = await self.say(self.agent, self.user, "Привет", response(text="Здравствуйте!"))
        self.assertEqual(answer, "Здравствуйте!")
        self.assertNotIn("car", self.agent._state[111].get("facts", {}))
        self.assertTrue(any("Память об автомобилях недоступна" in line for line in logs.output))

    async def test_database_write_failure_is_logged_and_not_restored(self):
        with patch.object(VehicleMemory, "remember",
                          side_effect=sqlite3.OperationalError("disk full")):
            with self.assertLogs("ai", level="WARNING") as logs:
                await self.say(self.agent, self.user, "У меня Toyota Camry",
                               self.saved("Toyota Camry", cls="Легковой"))
        self.assertTrue(any("Автомобиль не сохранён в памяти" in line for line in logs.output))
        restarted = await self.restart()
        await self.say(restarted, self.user, "Привет", response(text="Здравствуйте!"))
        self.assertNotIn("car", restarted._state[111].get("facts", {}))


if __name__ == "__main__":
    unittest.main()
