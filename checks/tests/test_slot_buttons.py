"""Кнопки выбора даты и времени: только свободные слоты из таблицы, без повторного выбора."""
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

from ai import Agent, ChatUser, SLOT_BACK_BUTTON, WEEKDAYS_SHORT
from sheets import WEEKDAYS_RU
from tests.fakes import fixture

USER = {"id": 111, "is_bot": False, "first_name": "Synthetic"}
DETAILS = {
    "client_name": ["Никита", "Никита"],
    "phone": ["+7 000 111-22-33", "+7 000 111-22-33"],
    "car": ["Lancer 9", "Lancer 9"],
    "size_class": ["Легковой", "Lancer 9"],
    "tariff": ["Базовый", "Базовый"],
}


def make_agent(store, side_effect):
    transport = Mock(close=AsyncMock())
    transport.chat.completions.create = AsyncMock(side_effect=side_effect)
    with patch("ai.AsyncOpenAI", return_value=transport):
        agent = Agent(store.config, store)
    return agent, transport


def upcoming_day(store, offset=2):
    """День в пределах недельного горизонта предложения, всегда в будущем."""
    return store.now().date() + timedelta(days=offset)


def day_label(day):
    """Подпись кнопки дня: короткий weekday в скобках, как в раскладке."""
    return f"{day:%d.%m}({WEEKDAYS_SHORT[day.weekday()]})"


def add_slot(spreadsheet, day, time_value, posts="2"):
    spreadsheet.worksheet("Слоты").values.append([f"{day:%d.%m.%Y}", time_value, posts])


class SlotButtonDialogTests(unittest.IsolatedAsyncioTestCase):
    async def test_date_buttons_lead_to_free_hours_and_existing_confirmation(self):
        import bot
        from aiogram.types import CallbackQuery, Message
        from tests.test_conversation import response
        with tempfile.TemporaryDirectory() as folder:
            store, spreadsheet = fixture(folder)
            day = upcoming_day(store)
            add_slot(spreadsheet, day, "10:00")
            agent, transport = make_agent(store, [
                response(calls=[("respond", {"reply": "На какой день вас записать?", "facts": DETAILS})]),
            ])
            self.addAsyncCleanup(agent.close)
            self.addAsyncCleanup(agent.reset, 111)  # сводка не должна переживать тест
            agent.reset(111)
            queue = bot.MessageBatcher(0.01)
            reporter = Mock(dialog=AsyncMock())
            sender = Mock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
            message = Message.model_validate({"message_id": 1, "date": 0,
                "chat": {"id": 111, "type": "private"}, "from": USER,
                "text": "Меня зовут Никита, машина Lancer 9, тариф Базовый, телефон +7 000 111-22-33."})
            async def text_handler(event, data):
                await bot.ask_agent(event, agent, reporter, event.text)
            async def click(data, handler):
                callback = CallbackQuery.model_validate({"id": data, "chat_instance": "synthetic", "from": USER,
                    "message": message.model_dump(), "data": data})
                await queue(handler, callback, {"agent": agent})
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer, \
                 patch.object(Message, "delete", new_callable=AsyncMock) as delete_message, \
                 patch.object(CallbackQuery, "answer", new_callable=AsyncMock), \
                 patch("bot.ChatActionSender.typing", return_value=sender):
                await queue(text_handler, message, {"agent": agent})
                # 1. Бот спросил про день: под ответом кнопки только с днями, где есть свободные часы.
                date_keyboard = answer.await_args.kwargs["reply_markup"]
                self.assertEqual([button.text for row in date_keyboard.inline_keyboard for button in row],
                                 [day_label(day)])
                date_data = date_keyboard.inline_keyboard[0][0].callback_data
                self.assertEqual(date_data, f"slots:date:{day.isoformat()}")
                await click(date_data, lambda event, values: bot.on_slot_pick(event, agent, reporter))
                # Старое меню дней удалено: следующий шаг приходит новым сообщением.
                self.assertEqual(delete_message.await_count, 1)
                # 2. После выбора даты - свободные часы этого дня и кнопка возврата к дням.
                time_keyboard = answer.await_args.kwargs["reply_markup"]
                self.assertEqual([button.text for row in time_keyboard.inline_keyboard for button in row],
                                 ["10:00", SLOT_BACK_BUTTON[0]])
                self.assertEqual(time_keyboard.inline_keyboard[-1][0].callback_data, SLOT_BACK_BUTTON[1])
                time_data = time_keyboard.inline_keyboard[0][0].callback_data
                self.assertEqual(time_data, f"slots:time:{day.isoformat()}:10:00")
                await click(time_data, lambda event, values: bot.on_slot_pick(event, agent, reporter))
                self.assertEqual(delete_message.await_count, 2)
                # 3. Данных хватает: прежнее подтверждение - сводка и одна кнопка «Подтвердить».
                confirm_keyboard = answer.await_args.kwargs["reply_markup"]
                confirm_data = confirm_keyboard.inline_keyboard[0][0].callback_data
                self.assertTrue(confirm_data.startswith("booking:confirm:"))
                self.assertNotIn("slots:", confirm_data)
                self.assertIn("Предлагаю запись:", answer.await_args.args[0])
                await click(confirm_data, lambda event, values: bot.on_proposal(event, agent, reporter))
                # Сводку с подтверждением клик не удаляет.
                self.assertEqual(delete_message.await_count, 2)
            # Выбор слота кнопками не обращается к модели: один запрос на весь диалог.
            self.assertEqual(transport.chat.completions.create.await_count, 1)
            self.assertEqual(len(spreadsheet.worksheet("Записи").writes), 1)
            booking = store.my_bookings(111)["bookings"][0]
            self.assertEqual((booking["date"], booking["time"]), (day.isoformat(), "10:00"))

    async def test_customer_stated_slot_gets_confirmation_without_reselection(self):
        import bot
        from aiogram.types import Message
        from tests.test_conversation import response
        with tempfile.TemporaryDirectory() as folder:
            store, spreadsheet = fixture(folder)
            day = upcoming_day(store)
            add_slot(spreadsheet, day, "10:00")
            stated = {**DETAILS, "date": [day.isoformat(), f"{day:%d.%m.%Y}"], "time": ["10:00", "в 10:00"]}
            agent, transport = make_agent(store, [
                response(calls=[("respond", {"reply": "", "facts": stated}), ("create_booking", {})]),
            ])
            self.addAsyncCleanup(agent.reset, 111)  # сводка не должна переживать тест
            self.addAsyncCleanup(agent.close)
            queue = bot.MessageBatcher(0.01)
            reporter = Mock(dialog=AsyncMock())
            sender = Mock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
            message = Message.model_validate({"message_id": 1, "date": 0,
                "chat": {"id": 111, "type": "private"}, "from": USER,
                "text": f"Никита, +7 000 111-22-33, Lancer 9, тариф Базовый, {day:%d.%m.%Y} в 10:00."})
            async def text_handler(event, data):
                await bot.ask_agent(event, agent, reporter, event.text)
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer, \
                 patch("bot.ChatActionSender.typing", return_value=sender):
                await queue(text_handler, message, {"agent": agent})
            self.assertTrue(agent._pending[111]["delivered"])
            keyboard = answer.await_args.kwargs["reply_markup"]
            self.assertTrue(keyboard.inline_keyboard[0][0].callback_data.startswith("booking:confirm:"))
            self.assertFalse(spreadsheet.worksheet("Записи").writes)
            # Слот уже указан клиентом: кнопки выбора даты не показываются.
            self.assertIsNone(agent._state[111].get("slot_menu"))
            self.assertIsNone(await agent.slot_keyboard(ChatUser(111, None, "Synthetic")))

    async def test_free_slots_request_answers_with_button_menu_not_text_list(self):
        # «Что свободно?» — меню дней кнопками; дни и часы текстом не перечисляем,
        # модель для этого не вызывается.
        import bot
        from aiogram.types import Message
        with tempfile.TemporaryDirectory() as folder:
            store, spreadsheet = fixture(folder)
            day = upcoming_day(store)
            add_slot(spreadsheet, day, "10:00")
            agent, transport = make_agent(store, [])  # пустая очередь: вызов модели уронил бы тест
            self.addAsyncCleanup(agent.close)
            self.addAsyncCleanup(agent.reset, 111)
            agent.reset(111)
            queue = bot.MessageBatcher(0.01)
            reporter = Mock(dialog=AsyncMock())
            sender = Mock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
            def user_message(message_id, text_value):
                return Message.model_validate({"message_id": message_id, "date": 0,
                    "chat": {"id": 111, "type": "private"}, "from": USER, "text": text_value})
            async def text_handler(event, data):
                await bot.ask_agent(event, agent, reporter, event.text)
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer, \
                 patch("bot.ChatActionSender.typing", return_value=sender):
                await queue(text_handler, user_message(1, "Что свободно на этой неделе?"), {"agent": agent})
                text = answer.await_args.args[0]
                self.assertIn("Выберите день записи:", text)
                self.assertNotIn("10:00", text)  # часы остались в кнопках
                keyboard = answer.await_args.kwargs["reply_markup"]
                self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, f"slots:date:{day.isoformat()}")
                # Конкретный день назван: меню часов этого дня.
                await queue(text_handler, user_message(2, f"А что свободно на {day:%d.%m.%Y}?"), {"agent": agent})
                text = answer.await_args.args[0]
                self.assertIn("Выберите время записи на", text)
                keyboard = answer.await_args.kwargs["reply_markup"]
                self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, f"slots:time:{day.isoformat()}:10:00")
            self.assertEqual(transport.chat.completions.create.await_count, 0)


class SlotButtonUnitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)

    def agent(self):
        with patch("ai.AsyncOpenAI"):
            return Agent(self.store.config, self.store)

    def book(self, index, car, time_value, day=None):
        result = self.store.create_booking(date_str=(day or "2099-01-01"), time_str=time_value,
                                           name=f"Other {index}", phone=f"0000000000{index}", car=car,
                                           size_class="Легковой", tariff="Базовый", services=[], comment="",
                                           telegram_id=900 + index, username=None)
        self.assertTrue(result["ok"], result.get("error"))

    async def test_no_buttons_while_confirmation_is_pending(self):
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        user = ChatUser(111, None, "Synthetic")
        agent._state[111] = {"slot_menu": "date", "stage": "collecting"}
        agent._pending[111] = {"offer_id": "x", "delivery_token": object()}
        self.assertIsNone(await agent.slot_keyboard(user))

    async def test_slot_menu_flags_are_hidden_from_the_model_state(self):
        from conversation import model_state
        state = {"slot_menu": "date", "slot_pick": {"date": "2099-01-01"}, "facts": {"date": "2099-01-01"}}
        projected = model_state(state)
        self.assertNotIn("slot_menu", projected)
        self.assertNotIn("slot_pick", projected)

    async def test_stale_past_slot_does_not_block_date_buttons(self):
        # В фактах осталась дата прошлой записи: она не «подходящий слот», кнопки нужны.
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        agent._state[111]["facts"] = {"date": "2020-01-01", "time": "10:00"}
        agent._offer_slot_menu(111, "date")
        self.assertEqual(agent._state[111].get("slot_menu"), "date")

    async def test_future_slot_from_text_still_suppresses_buttons(self):
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        agent._state[111]["facts"] = {"date": "2099-01-01", "time": "10:00"}
        agent._offer_slot_menu(111, "date")
        self.assertIsNone(agent._state[111].get("slot_menu"))

    async def test_stuck_date_choice_is_forgotten_when_customer_names_another_day(self):
        from conversation import apply_patch
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        state = agent._state[111]
        state["slot_pick"] = {"date": "2099-01-01"}
        apply_patch(state, {"date": "2099-01-02"}, {})
        self.assertIsNone(state.get("slot_pick"))

    async def test_taken_time_is_rechecked_against_the_table(self):
        self.book(0, "Alpha", "10:00")
        self.book(1, "Beta", "10:00")
        self.spreadsheet.worksheet("Слоты").values.append(["01.01.2099", "11:00", "1"])
        self.book(2, "Gamma", "11:00")
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        user = ChatUser(111, None, "Synthetic")
        # Оба слота дня заполнены чужими записями - свободных часов не осталось.
        text, rows = await agent.pick_time(user, "2099-01-01", "10:00")
        self.assertIn("заняли", text)
        self.assertIsNone(rows)
        self.assertEqual(len(self.spreadsheet.worksheet("Записи").writes), 3)
        self.assertIsNone(agent._pending.get(111))

    async def test_taken_time_offers_remaining_hours_of_the_same_day(self):
        self.spreadsheet.worksheet("Слоты").values.append(["01.01.2099", "12:00", "1"])
        self.book(0, "Alpha", "10:00")
        self.book(1, "Beta", "10:00")
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        user = ChatUser(111, None, "Synthetic")
        text, rows = await agent.pick_time(user, "2099-01-01", "10:00")
        self.assertIn("заняли", text)
        self.assertEqual(rows, [[("12:00", "slots:time:2099-01-01:12:00")], [SLOT_BACK_BUTTON]])
        # Занятое время не записалось: чужие записи не тронуты, своей записи нет.
        self.assertEqual(len(self.spreadsheet.worksheet("Записи").writes), 2)
        self.assertIsNone(agent._pending.get(111))

    async def test_full_day_suggests_other_days_with_free_hours(self):
        self.book(0, "Alpha", "10:00")
        self.book(1, "Beta", "10:00")
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        user = ChatUser(111, None, "Synthetic")
        text, rows = await agent.pick_date(user, "2099-01-01")
        self.assertIn("свободного времени не осталось", text)
        self.assertIsNone(rows)  # на недельном горизонте свободных дней тоже нет

    async def test_table_failure_on_button_press_is_polite_not_silent(self):
        # Обрыв сети при нажатии кнопки не должен ронять обработчик: клиенту нужен ответ.
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        user = ChatUser(111, None, "Synthetic")
        with patch.object(self.store, "free_slots", side_effect=RuntimeError("network gone")):
            text, rows = await agent.pick_date(user, "2099-01-01")
            self.assertIn("Не удалось посмотреть расписание", text)
            self.assertIsNone(rows)
            text, rows = await agent.pick_time(user, "2099-01-01", "10:00")
            self.assertIn("Не удалось проверить расписание", text)
            self.assertIsNone(rows)
        self.assertEqual(self.spreadsheet.worksheet("Записи").writes, [])

    async def test_picked_date_shows_only_free_hours_of_that_day(self):
        self.spreadsheet.worksheet("Слоты").values.append(["01.01.2099", "12:00", "1"])
        self.book(0, "Alpha", "10:00")  # один пост из двух на 10:00 занят
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        user = ChatUser(111, None, "Synthetic")
        text, rows = await agent.pick_date(user, "2099-01-01")
        self.assertIn("Выберите время записи на 01.01.2099", text)
        self.assertNotIn("— 10:00", text)  # часы не перечисляем текстом: они в кнопках
        self.assertEqual(rows, [[("10:00", "slots:time:2099-01-01:10:00"), ("12:00", "slots:time:2099-01-01:12:00")],
                                [SLOT_BACK_BUTTON]])
        self.assertEqual(agent._state[111]["slot_pick"], {"date": "2099-01-01"})

    async def test_many_times_fit_rows_of_four_with_back_button(self):
        self.spreadsheet.worksheet("Слоты").values.extend(
            [["01.01.2099", f"{hour}:00", "2"] for hour in range(9, 21)])
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        user = ChatUser(111, None, "Synthetic")
        text, rows = await agent.pick_date(user, "2099-01-01")
        # 11 рабочих часов рядами по 4 плюс отдельная кнопка возврата к дням.
        self.assertEqual([len(row) for row in rows], [4, 4, 3, 1])
        flat = [data for row in rows for _, data in row]
        self.assertEqual(flat, [f"slots:time:2099-01-01:{hour:02d}:00" for hour in range(9, 20)]
                         + [SLOT_BACK_BUTTON[1]])
        self.assertEqual(rows[-1][0], SLOT_BACK_BUTTON)

    async def test_dates_lay_out_two_per_row_with_short_weekday(self):
        spreadsheet_days = [upcoming_day(self.store, offset) for offset in range(1, 6)]
        for day in spreadsheet_days:
            add_slot(self.spreadsheet, day, "10:00", posts="1")
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        user = ChatUser(111, None, "Synthetic")
        rows = await agent._date_rows(user)
        self.assertEqual([len(row) for row in rows], [2, 2, 1])
        for row in rows:
            for label, data in row:
                self.assertLessEqual(len(data.encode()), 64)
                self.assertRegex(label, r"^\d{2}\.\d{2}\((пн|вт|ср|чт|пт|сб|вс)\)$")

    async def test_back_button_returns_to_dates_and_drops_picked_day(self):
        import bot
        from aiogram.types import CallbackQuery, Message
        day = upcoming_day(self.store)
        add_slot(self.spreadsheet, day, "10:00")
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        self.addAsyncCleanup(agent.reset, 111)
        user = ChatUser(111, None, "Synthetic")
        text, rows = await agent.pick_date(user, day.isoformat())
        self.assertEqual(agent._state[111]["slot_pick"], {"date": day.isoformat()})
        # Нажатие «← К выбору дня»: день сброшен, меню дней показано снова.
        callback = CallbackQuery.model_validate({"id": "back", "chat_instance": "synthetic", "from": USER,
            "message": Message.model_validate({"message_id": 5, "date": 0,
                "chat": {"id": 111, "type": "private"}, "from": {"id": 999, "is_bot": True, "first_name": "Carwash"},
                "text": "Свободное время"}).model_dump(), "data": SLOT_BACK_BUTTON[1]})
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer, \
             patch.object(Message, "delete", new_callable=AsyncMock) as delete_message, \
             patch.object(CallbackQuery, "answer", new_callable=AsyncMock):
            queue = bot.MessageBatcher(0.01)
            async def handler(event, values):
                await bot.on_slot_pick(event, agent, Mock(dialog=AsyncMock()))
            await queue(handler, callback, {"agent": agent})
        # Меню часов удалено, кнопки дней показаны заново.
        self.assertEqual(delete_message.await_count, 1)
        self.assertIsNone(agent._state[111].get("slot_pick"))
        text = answer.await_args.args[0]
        self.assertIn("Выберите день", text)
        keyboard = answer.await_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, f"slots:date:{day.isoformat()}")
        # После возврата выбор дня снова работает: сброс не блокирует новую дату.
        text, rows = await agent.pick_date(user, day.isoformat())
        self.assertEqual(agent._state[111]["slot_pick"], {"date": day.isoformat()})
        self.assertEqual(agent._state[111]["facts"]["date"], day.isoformat())
        self.assertNotIn("date", agent._state[111].get("invalidated", []))
        self.assertIn("Выберите время записи на", text)

    async def test_stale_past_slot_does_not_block_date_buttons(self):
        # В фактах осталась дата прошлой записи: она не «подходящий слот», кнопки нужны.
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        agent._state[111]["facts"] = {"date": "2020-01-01", "time": "10:00"}
        agent._offer_slot_menu(111, "date")
        self.assertEqual(agent._state[111].get("slot_menu"), "date")

    async def test_future_slot_from_text_still_suppresses_buttons(self):
        agent = self.agent()
        self.addAsyncCleanup(agent.close)
        agent._state[111]["facts"] = {"date": "2099-01-01", "time": "10:00"}
        agent._offer_slot_menu(111, "date")
        self.assertIsNone(agent._state[111].get("slot_menu"))


if __name__ == "__main__":
    unittest.main()
