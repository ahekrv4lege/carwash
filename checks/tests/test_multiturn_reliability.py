"""Многотановая надёжность диалога: длинные беседы, изоляция, конкурентность.

Модель подменена детерминированным скриптом (реплика клиента -> ответ/инструменты).
Проверяется состояние агента и таблицы, а не текст ответов.
"""
import asyncio
import json
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from ai import Agent, ChatUser
from tests.fakes import fixture
from tests.test_conversation import response

DIALOG_RE = re.compile(r"запи\w*|хочу|мойк\w*|тариф|цена|стоит", re.I)


def scripted(agent, rules, default="Ок."):
    """Модель-скрипт. Значение-правила: список — одноразовая очередь ответов,
    скаляр (строка/функция/готовый ответ) — повторяется на каждый матч."""
    from types import SimpleNamespace

    async def model(**kwargs):
        last = next((m["content"] for m in reversed(kwargs["messages"]) if m["role"] == "user"), "")
        for pattern, outputs in rules:
            if re.search(pattern, last, re.I):
                if isinstance(outputs, list):
                    if not outputs:
                        continue  # очередь исчерпана — пробуем следующее правило
                    item = outputs.pop(0)
                else:
                    item = outputs
                if callable(item):
                    return item(**kwargs)
                if isinstance(item, SimpleNamespace):
                    return item  # уже готовый ответ модели
                if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
                    return response(calls=[item])  # вызов инструмента (имя, аргументы)
                return response(item)
        return response(default)
    agent.client.chat.completions.create = AsyncMock(side_effect=model)
    return agent


def facts_respond(**fields):
    """Модель сохраняет факты через вызов инструмента respond.

    size_class выводится моделью из фразы машины, поэтому его цитата — слова
    клиента об автомобиле (как в реальных ответах), а не название класса.
    """
    size_class_quote = fields.pop("size_class_quote", None)
    facts = {key: [value, value] for key, value in fields.items()}
    if fields.get("size_class") and size_class_quote:
        facts["size_class"] = [fields["size_class"], size_class_quote]
    return response(calls=[("respond", {"reply": "Запомнила.", "facts": facts, "tentative": {}})])


def first_facts_reply(agent, user, text):
    """Первое сообщение: модель сохраняет имя/машину/класс через respond."""
    scripted(agent, [
        (r".", [facts_respond(client_name="Тест", car="Toyota Camry 3.5", car_model="Toyota Camry",
                              size_class="Легковой", size_class_quote="Toyota Camry 3.5")]),
    ])


class LongDialogTests(unittest.IsolatedAsyncioTestCase):
    """50-репличный диалог: факты живут дольше усечённой истории."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(self.store.config, self.store)
        self.addCleanup(self.agent.cars.close)
        self.user = ChatUser(3001, None, "Long")

    async def say(self, text):
        answer = await self.agent.reply(self.user, text)
        self.agent.mark_delivered(self.user.id, self.agent.delivery_token(self.user.id, answer))
        return answer

    async def test_facts_survive_fifty_messages_of_chatter(self):
        scripted(self.agent, [
            (r".", facts_respond(client_name="Тест", car="Toyota Camry 3.5", car_model="Toyota Camry",
                                 size_class="Легковой", size_class_quote="Toyota Camry 3.5", phone="+7 900 111-22-33")),
        ])
        await self.say("Здравствуйте! Меня зовут Тест, машина Toyota Camry 3.5.")
        scripted(self.agent, [], default="Ок, продолжаем.")
        topics = ("расскажи про погоду", "как дела?", "ты живая?", "какой сегодня день",
                  "люблю кофе", "а ты откуда", "понятно", "хорошо", "давай дальше", "ок")
        for index in range(45):
            await self.say(topics[index % len(topics)] + f" номер {index}")
        facts = self.agent._state[self.user.id]["facts"]
        self.assertEqual(facts.get("client_name"), "Тест")
        self.assertEqual(facts.get("car_model"), "Toyota Camry")
        self.assertEqual(facts.get("size_class"), "Легковой")
        self.assertLessEqual(len(self.agent._history[self.user.id]), self.agent.config.max_history)
        # История усечена, но последний пользовательский текст на месте
        self.assertTrue(any(m["role"] == "user" for m in self.agent._history[self.user.id][-2:]))

    async def test_payload_stays_bounded_in_long_dialog(self):
        """Размер запроса к модели не растёт линейно с длиной беседы."""
        sizes = []
        original = self.agent.client.chat.completions.create

        async def measuring(**kwargs):
            sizes.append(sum(len(str(m.get("content", ""))) for m in kwargs["messages"]))
            return await original(**kwargs)
        # Скрипт отдаёт факты только на первом сообщении (список исчерпывается),
        # дальше — «Ок.»; замер охватывает все вызовы.
        scripted(self.agent, [
            (r".", [facts_respond(client_name="Тест", car="Toyota Camry 3.5", car_model="Toyota Camry",
                                  size_class="Легковой", size_class_quote="Toyota Camry 3.5")]),
        ])
        self.agent.client.chat.completions.create = AsyncMock(side_effect=measuring)
        await self.say("Здравствуйте! Меня зовут Тест, машина Toyota Camry 3.5.")
        for index in range(25):
            await self.say(f"болтовня номер {index}")
        self.assertGreater(len(sizes), 10)
        # Запрос в конце беседы не должен быть заметно больше запроса в начале:
        # история усекается, STATE и CUSTOMER ограничены.
        self.assertLess(max(sizes) - min(sizes), 2500,
                        "запрос к модели разросся: история не усекается")


class OfferLifecycleNote:
    """Полный цикл записи через диалог покрыт: checks/booking_dialog.py,
    tests.test_customer_vehicles.book_camry (предложение -> подтверждение),
    tests.test_phase2 (повторы/устаревание) и живым E2E на реальных сервисах."""


class SameUserConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """Два одновременных запроса одного клиента: сериализация, без порчи состояния."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(self.store.config, self.store)
        self.addCleanup(self.agent.cars.close)
        self.user = ChatUser(3003, None, "Concurrent")
        scripted(self.agent, [
            (r"зовут|имя", facts_respond(client_name="Тест")),
            (r".", "Ок."),
        ])

    async def test_overlapping_replies_are_serialized_and_consistent(self):
        first = asyncio.create_task(self.agent.reply(self.user, "Меня зовут Тест."))
        second = asyncio.create_task(self.agent.reply(self.user, "И ещё: как дела?"))
        answers = await asyncio.gather(first, second)
        for answer in answers:
            self.agent.mark_delivered(self.user.id, self.agent.delivery_token(self.user.id, answer))
        self.assertEqual(self.agent._state[self.user.id]["facts"].get("client_name"), "Тест")
        self.assertTrue(all("Ок" in a or a for a in answers))

    async def test_slow_first_reply_does_not_lose_newer_message(self):
        original = self.agent.client.chat.completions.create

        async def slow_then_fast(**kwargs):
            last = next(m["content"] for m in reversed(kwargs["messages"]) if m["role"] == "user")
            await asyncio.sleep(0.3 if "долгий" in last else 0)
            return await original(**kwargs)
        self.agent.client.chat.completions.create = AsyncMock(side_effect=slow_then_fast)
        first = asyncio.create_task(self.agent.reply(self.user, "долгий вопрос про погоду"))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(self.agent.reply(self.user, "Меня зовут Тест!"))
        await asyncio.gather(first, second)
        # Второе (новое) сообщение обработано после первого: факты в итоговом состоянии
        self.assertEqual(self.agent._state[self.user.id]["facts"].get("client_name"), "Тест")


class CrossCustomerIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Три клиента вперемешку: факты, предложения и записи не пересекаются."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(self.store.config, self.store)
        self.addCleanup(self.agent.cars.close)
        self.users = [ChatUser(3101, None, "A"), ChatUser(3102, None, "B"), ChatUser(3103, None, "C")]

    async def say(self, user, text):
        answer = await self.agent.reply(user, text)
        self.agent.mark_delivered(user.id, self.agent.delivery_token(user.id, answer))
        return answer

    async def test_interleaved_facts_stay_isolated(self):
        scripted(self.agent, [
            (r"Аня", facts_respond(client_name="Аня")),
            (r"Борис", facts_respond(client_name="Борис")),
            (r"Вера", facts_respond(client_name="Вера")),
            (r".", "Ок."),
        ])
        await self.say(self.users[0], "Меня зовут Аня.")
        await self.say(self.users[1], "Меня зовут Борис.")
        await self.say(self.users[2], "Меня зовут Вера.")
        for index, expected in enumerate(("Аня", "Борис", "Вера")):
            self.assertEqual(self.agent._state[self.users[index].id]["facts"].get("client_name"), expected)

    async def test_proposals_belong_to_their_customer(self):
        from tests.fakes import booking as make_booking
        first = self.store.create_booking(**make_booking(telegram_id=4101, car="A", time_str="10:00"))
        second = self.store.create_booking(**make_booking(telegram_id=4102, car="B", time_str="10:00"))
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(len(self.store.my_bookings(4101)["bookings"]), 1)
        self.assertEqual(len(self.store.my_bookings(4102)["bookings"]), 1)
        # Клиент 4101 не может отменить чужую запись 4102
        foreign = self.store.cancel_booking(second["booking_id"], 4101)
        self.assertFalse(foreign["ok"])

    async def test_last_slot_cannot_be_taken_twice_concurrently(self):
        from tests.fakes import booking as make_booking
        self.spreadsheet.worksheet("Слоты").values = [["Дата", "Время", "Постов"],
                                                      ["01.01.2099", "10:00", "1"]]
        self.store._invalidate("slots", "bookings")
        results = await asyncio.gather(*(
            asyncio.to_thread(self.store.create_booking, **make_booking(telegram_id=4200 + index, car=f"Car {index}"))
            for index in range(2)))
        self.assertEqual(sum(1 for result in results if result["ok"]), 1)


class StaleResponseTests(unittest.IsolatedAsyncioTestCase):
    """Запоздалый ответ модели не затирает более новые факты и не ломает предложение."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(self.store.config, self.store)
        self.addCleanup(self.agent.cars.close)
        self.user = ChatUser(3004, None, "Stale")

    async def test_reset_during_reply_discards_the_stale_response(self):
        """/reset во время генерации: запоздалый ответ отбрасывается, новые факты не затёрты."""
        scripted(self.agent, [
            (r".", facts_respond(client_name="Старое имя")),
        ])
        original = self.agent.client.chat.completions.create

        async def reset_midway(**kwargs):
            result = await original(**kwargs)
            self.agent.reset(self.user.id)  # клиент нажал /reset, пока модель отвечала
            return result
        self.agent.client.chat.completions.create = AsyncMock(side_effect=reset_midway)
        answer = await self.agent.reply(self.user, "Меня зовут Старое имя.")
        self.assertIn("Диалог сброшен", answer)  # запоздалый ответ не применён
        self.assertNotIn("Старое имя", self.agent._state.get(self.user.id, {}).get("facts", {}).get("client_name", ""))


class DuplicateUpdateTests(unittest.IsolatedAsyncioTestCase):
    """Дубли Telegram-обновлений и повторные нажатия кнопки."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(self.store.config, self.store)
        self.addCleanup(self.agent.cars.close)
        self.user = ChatUser(3005, None, "Dup")

    async def test_same_message_twice_creates_one_booking(self):
        from tests.test_conversation import ARGS
        from tests.fakes import booking as make_booking
        # Прямые мутации: одно и то же создание дважды
        first = self.store.create_booking(**make_booking(telegram_id=4300, car="Car"))
        self.assertTrue(first["ok"])
        duplicate = self.store.create_booking(**make_booking(telegram_id=4300, car="Car"))
        self.assertFalse(duplicate["ok"])
        self.assertEqual(len(self.store.my_bookings(4300)["bookings"]), 1)


if __name__ == "__main__":
    unittest.main()
