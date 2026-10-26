import asyncio
import json
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock, patch

from ai import Agent, ChatUser
from tests.fakes import fixture


class BotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_button_then_text_requires_new_summary_and_new_button(self):
        import bot
        from dataclasses import replace
        from aiogram.types import CallbackQuery, Message
        from tests.test_conversation import ARGS, response
        with tempfile.TemporaryDirectory() as folder:
            store, spreadsheet = fixture(folder)
            spreadsheet.worksheet("Слоты").values.append(["01.01.2099", "12:00", "2"])
            transport = Mock(close=AsyncMock())
            transport.chat.completions.create = AsyncMock(side_effect=[
                response(calls=[("create_booking", ARGS)]),
                response("Поменяла время."),
            ])
            with patch("ai.AsyncOpenAI", return_value=transport):
                agent = Agent(replace(store.config, reply_min_delay_sec=0), store)
            self.addAsyncCleanup(agent.close)
            queue = bot.MessageBatcher(0.01)
            reporter = Mock(dialog=AsyncMock())
            sender = Mock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
            user = {"id": 111, "is_bot": False, "first_name": "Synthetic"}
            message = Message.model_validate({"message_id": 1, "date": 0,
                "chat": {"id": 111, "type": "private"}, "from": user, "text": json.dumps(ARGS, ensure_ascii=False)})
            async def text_handler(event, data):
                await bot.ask_agent(event, agent, reporter, event.text)
            async def click(data):
                callback = CallbackQuery.model_validate({"id": data, "chat_instance": "synthetic", "from": user,
                    "message": message.model_dump(), "data": data})
                async def callback_handler(event, values):
                    await bot.on_proposal(event, agent, reporter)
                await queue(callback_handler, callback, {"agent": agent})
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer, \
                 patch.object(CallbackQuery, "answer", new_callable=AsyncMock), \
                 patch("bot.ChatActionSender.typing", return_value=sender):
                await queue(text_handler, message, {"agent": agent})
                old = answer.await_args.kwargs["reply_markup"].inline_keyboard[0]
                await click(old[1].callback_data)
                self.assertFalse(spreadsheet.worksheet("Записи").writes)
                await queue(text_handler, message.model_copy(update={"text": "Хочу в 12:00"}), {"agent": agent})
                new = answer.await_args.kwargs["reply_markup"].inline_keyboard[0]
                self.assertNotEqual(old[0].callback_data, new[0].callback_data)
                self.assertIn(ARGS["phone"], answer.await_args.args[0])
                await click(old[0].callback_data)
                self.assertFalse(spreadsheet.worksheet("Записи").writes)
                await click(new[0].callback_data)
                self.assertEqual(len(spreadsheet.worksheet("Записи").writes), 1)
                self.assertEqual(store.my_bookings(111)["bookings"][0]["time"], "12:00")

    async def test_only_last_chunk_has_buttons_and_partial_delivery_cannot_confirm(self):
        import bot
        for fails in (False, True):
            with self.subTest(fails=fails):
                token = object()
                agent = Mock()
                agent.delivery_token.return_value = token
                agent._pending = {111: {"offer_id": "a" * 24}}
                message = Mock(answer=AsyncMock(side_effect=[None, TimeoutError()] if fails else None))
                reporter = Mock(dialog=AsyncMock())
                user = ChatUser(111, None, "Synthetic")
                context = self.assertRaises(TimeoutError) if fails else nullcontext()
                with context:
                    await bot.send_answer(message, agent, reporter, user, "Запись", "a" * 9000)
                calls = message.answer.await_args_list
                self.assertNotIn("reply_markup", calls[0].kwargs)
                if fails:
                    agent.mark_delivered.assert_not_called()
                    reporter.dialog.assert_not_awaited()
                else:
                    self.assertEqual(len(calls), 3)
                    keyboard = calls[-1].kwargs["reply_markup"]
                    self.assertTrue(all(len(button.callback_data.encode()) <= 64 for button in keyboard.inline_keyboard[0]))
                    agent.mark_delivered.assert_called_once_with(111, token)

    async def test_batched_dialogue_through_confirmation_writes_once_and_keeps_delivery_order(self):
        import bot
        from dataclasses import replace
        from tests.test_conversation import response
        from aiogram.types import CallbackQuery, Message
        with tempfile.TemporaryDirectory() as folder:
            store, spreadsheet = fixture(folder)
            transport = Mock(close=AsyncMock())
            transport.chat.completions.create = AsyncMock(side_effect=[
                response(calls=[("respond", {
                    "reply": "Когда удобно?",
                    "facts": {
                        "client_name": ["Никита", "Никита"],
                        "car": ["Lancer 9", "Lancer 9"],
                        "size_class": ["Легковой", "Lancer 9"],
                        "tariff": ["Базовый", "Базовый"],
                    },
                })]),
                response(calls=[
                    ("respond", {
                        "reply": "Проверю данные.",
                        "facts": {
                            "date": ["2099-01-01", "01.01.2099"],
                            "time": ["10:00", "в 10:00"],
                            "phone": ["+7 000 111-22-33", "+7 000 111-22-33"],
                        },
                    }),
                    ("create_booking", {}),
                ]),
            ])
            with patch("ai.AsyncOpenAI", return_value=transport):
                agent = Agent(replace(store.config, reply_min_delay_sec=0), store)
            self.addAsyncCleanup(agent.close)
            queue = bot.MessageBatcher(0.01)
            reporter = Mock(dialog=AsyncMock())
            sender = Mock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
            async def handler(message, data):
                await bot.ask_agent(message, agent, reporter, message.text)
            async def send(*texts):
                messages = [Message.model_validate({
                    "message_id": index + 1, "date": 0,
                    "chat": {"id": 111, "type": "private"},
                    "from": {"id": 111, "is_bot": False, "first_name": "Synthetic"},
                    "text": text,
                })
                    for index, text in enumerate(texts)]
                await asyncio.gather(*(queue(handler, message, {"agent": agent}) for message in messages))
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer, \
                 patch("bot.ChatActionSender.typing", return_value=sender):
                await send("Меня зовут Никита.", "Мой автомобиль Lancer 9.", "Нужен Базовый.")
                await send("01.01.2099 в 10:00", "Телефон +7 000 111-22-33. Подготовьте запись.")
                self.assertEqual(transport.chat.completions.create.await_count, 2)
                self.assertTrue(agent._pending[111]["delivered"])
                self.assertFalse(spreadsheet.worksheet("Записи").writes)
                await send("Да, подтверждаю запись")
                self.assertFalse(spreadsheet.worksheet("Записи").writes)
                keyboard = answer.await_args_list[1].kwargs["reply_markup"]
                self.assertEqual([button.text for button in keyboard.inline_keyboard[0]], ["Подтвердить", "Изменить"])
                data = keyboard.inline_keyboard[0][0].callback_data
                callback = CallbackQuery.model_validate({"id": "synthetic-click", "chat_instance": "synthetic",
                    "from": {"id": 111, "is_bot": False, "first_name": "Synthetic"},
                    "message": {"message_id": 2, "date": 0, "chat": {"id": 111, "type": "private"},
                                "from": {"id": 999, "is_bot": True, "first_name": "Carwash"},
                                "text": "Предложение", "reply_markup": keyboard.model_dump()}, "data": data})
                async def click(event, values):
                    await bot.on_proposal(event, agent, reporter)
                with patch.object(CallbackQuery, "answer", new_callable=AsyncMock), \
                     patch.object(Message, "edit_reply_markup", new_callable=AsyncMock):
                    await queue(click, callback, {"agent": agent})
                    await queue(click, callback, {"agent": agent})
                self.assertEqual(transport.chat.completions.create.await_count, 2)
                self.assertEqual(len(spreadsheet.worksheet("Записи").writes), 1)
                self.assertIn("Предлагаю запись:", answer.await_args_list[1].args[0])
                self.assertIn("10:00", answer.await_args_list[-2].args[0])
                self.assertEqual(sum("Записала вас" in call.args[0] for call in answer.await_args_list), 1)
                self.assertFalse(queue.pending)

    async def test_existing_agent_tools_work_with_adaptive_store(self):
        with tempfile.TemporaryDirectory() as temp:
            store, _ = fixture(temp)
            with patch("ai.AsyncOpenAI") as provider:
                agent = Agent(store.config, store)
                user = ChatUser(id=111, username=None, full_name="Synthetic")
                prices = await agent._execute("get_price_list", {}, user)
                self.assertEqual(prices["services"][0]["prices"]["Легковой"], 700)
                result = await agent._execute("create_booking", {"date": "2099-01-01", "time": "10:00",
                    "client_name": "Synthetic", "phone": "00000000000", "car": "Fixture car",
                    "size_class": "Легковой", "tariff": "Базовый"}, user)
                self.assertTrue(result["ok"])
                mine = await agent._execute("get_my_bookings", {}, user)
                self.assertEqual(len(mine["bookings"]), 1)
                cancelled = await agent._execute("cancel_booking", {"booking_id": result["booking_id"]}, user)
                self.assertTrue(cancelled["ok"])
                provider.return_value.chat.completions.create.assert_not_called()

    async def test_application_closes_provider_and_telegram_sessions(self):
        import bot
        with tempfile.TemporaryDirectory() as temp:
            store, _ = fixture(temp)
            transport = Mock()
            transport.delete_webhook = AsyncMock()
            transport.session.close = AsyncMock()
            agent = Mock()
            agent.close = AsyncMock()
            dispatcher = Mock()
            dispatcher.start_polling = AsyncMock()
            with patch("bot.load_config", return_value=store.config), patch("bot.Bot", return_value=transport), \
                 patch("bot.Store", return_value=store), patch("bot.Agent", return_value=agent), \
                 patch("bot.Dispatcher", return_value=dispatcher):
                await bot.main()
            transport.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
            dispatcher.start_polling.assert_awaited_once_with(transport, close_bot_session=False)
            transport.session.close.assert_awaited_once()
            agent.close.assert_awaited_once()

    async def test_minimum_reply_delay_includes_model_time_without_extra_ai_calls(self):
        import bot
        from types import SimpleNamespace as N
        for minimum, elapsed, expected in ((1.5, 0.2, 1.3), (1.5, 3, 0), (0, 0.2, 0)):
            with self.subTest(minimum=minimum, elapsed=elapsed):
                agent = Mock(config=N(reply_min_delay_sec=minimum), reply=AsyncMock(return_value="Подскажу цены."))
                message = Mock(from_user=N(id=111, username=None, full_name="Тест"), chat=N(id=111), answer=AsyncMock())
                reporter = Mock(dialog=AsyncMock())
                sender = Mock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
                timer = N(monotonic=Mock(side_effect=[100, 100 + elapsed]))
                with patch("bot.time", timer), patch("bot.ChatActionSender.typing", return_value=sender), \
                     patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
                    await bot.ask_agent(message, agent, reporter, "Прайс?")
                self.assertAlmostEqual(sleep.await_args.args[0], expected)
                agent.reply.assert_awaited_once()
                message.answer.assert_awaited_once_with("Подскажу цены.")
                agent.mark_delivered.assert_called_once()


class MessageBatchingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from bot import MessageBatcher
        self.batcher = MessageBatcher(0.01)
        self.agent = Mock()
        self.agent.delivery_token.return_value = None
        self.agent._pending = {}
        self.data = {"agent": self.agent}

    def message(self, text, user_id=111, contact=None):
        from aiogram.types import Message
        return Message.model_validate({"message_id": 1, "date": 0,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Synthetic"},
            "text": text, "contact": contact})

    async def test_three_quick_messages_need_one_handler_call_in_order(self):
        handler = AsyncMock(return_value="reply")
        results = await asyncio.gather(*(self.batcher(handler, self.message(text), self.data)
                                        for text in ("Лансер", "девятый", "завтра в 11")))
        handler.assert_awaited_once()
        self.assertEqual(handler.await_args.args[0].text, "Лансер\nдевятый\nзавтра в 11")
        self.assertEqual(results, ["reply"] * 3)
        self.assertFalse(self.batcher.pending)

    async def test_commands_and_confirmation_are_separate_boundaries(self):
        handler = AsyncMock()
        messages = [self.message("Лансер"), self.message("/reset"), self.message("Иван"),
                    self.message("Да, подтверждаю запись"), self.message("в 11")]
        await asyncio.gather(*(self.batcher(handler, message, self.data) for message in messages))
        self.assertEqual([call.args[0].text for call in handler.await_args_list],
                         ["Лансер", "/reset", "Иван", "Да, подтверждаю запись", "в 11"])

    async def test_next_reply_waits_until_previous_delivery_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        steps = []
        async def handler(message, data):
            steps.append("start:" + message.text)
            if message.text == "first":
                entered.set()
                await release.wait()
            steps.append("sent:" + message.text)
        first = asyncio.create_task(self.batcher(handler, self.message("first"), self.data))
        await entered.wait()
        second = asyncio.create_task(self.batcher(handler, self.message("second"), self.data))
        await asyncio.sleep(0)
        self.assertEqual(steps, ["start:first"])
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(steps, ["start:first", "sent:first", "start:second", "sent:second"])

    async def test_different_clients_are_not_blocked_by_one_slow_reply(self):
        both, release = asyncio.Event(), asyncio.Event()
        entered = set()
        async def handler(message, data):
            entered.add(message.from_user.id)
            if len(entered) == 2:
                both.set()
            await release.wait()
        tasks = [asyncio.create_task(self.batcher(handler, self.message("hello", user), self.data)) for user in (111, 222)]
        try:
            await asyncio.wait_for(both.wait(), 1)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertFalse(self.batcher.pending)

    async def test_confirmation_received_before_new_offer_cannot_confirm_it_later(self):
        from aiogram.types import Message
        token = None
        async def handler(message, data):
            nonlocal token
            token = object()
        self.agent.delivery_token.side_effect = lambda user_id: token
        wrapped = AsyncMock(side_effect=handler)
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            await asyncio.gather(self.batcher(wrapped, self.message("подготовьте запись"), self.data),
                                 self.batcher(wrapped, self.message("Да, подтверждаю запись"), self.data))
        wrapped.assert_awaited_once()
        answer.assert_awaited_once()
        self.assertIn("подтвердите заново", answer.await_args.args[0])

    async def test_undelivered_offer_confirmation_is_rejected_without_handler(self):
        from aiogram.types import Message
        self.agent.delivery_token.return_value = object()
        self.agent._pending = {111: {"delivered": False}}
        handler = AsyncMock()
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            await self.batcher(handler, self.message("Да, подтверждаю запись"), self.data)
        handler.assert_not_awaited()
        answer.assert_awaited_once()

    async def test_delivered_confirmation_is_not_batched_with_following_text(self):
        self.agent.delivery_token.return_value = object()
        self.agent._pending = {111: {"delivered": True}}
        handler = AsyncMock()
        await asyncio.gather(self.batcher(handler, self.message("Да, подтверждаю запись"), self.data),
                             self.batcher(handler, self.message("На когда запись?"), self.data))
        self.assertEqual(handler.await_count, 2)

    async def test_button_waits_for_earlier_correction_and_is_acknowledged_immediately(self):
        from aiogram.types import CallbackQuery
        callback = CallbackQuery.model_validate({"id": "synthetic-click", "chat_instance": "synthetic",
            "from": {"id": 111, "is_bot": False, "first_name": "Synthetic"},
            "message": {"message_id": 5, "date": 0, "chat": {"id": 111, "type": "private"},
                        "text": "Предложение"}, "data": "booking:confirm:old-offer"})
        started, release = asyncio.Event(), asyncio.Event()
        steps = []
        async def handler(event, data):
            if isinstance(event, CallbackQuery):
                steps.append("button")
            else:
                steps.append("correction")
                started.set()
                await release.wait()
        first = asyncio.create_task(self.batcher(handler, self.message("Нет, в 15:00"), self.data))
        await started.wait()
        acknowledged = asyncio.Event()
        async def acknowledge_click():
            acknowledged.set()
        with patch.object(CallbackQuery, "answer", new_callable=AsyncMock, side_effect=acknowledge_click) as ack:
            second = asyncio.create_task(self.batcher(handler, callback, self.data))
            await asyncio.wait_for(acknowledged.wait(), 1)
            ack.assert_awaited_once()
            self.assertEqual(steps, ["correction"])
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(steps, ["correction", "button"])

    async def test_slow_button_ack_cannot_reorder_edit_before_confirm(self):
        from aiogram.types import CallbackQuery
        def callback(action):
            return CallbackQuery.model_validate({"id": action, "chat_instance": "synthetic",
                "from": {"id": 111, "is_bot": False, "first_name": "Synthetic"},
                "message": {"message_id": 5, "date": 0, "chat": {"id": 111, "type": "private"}, "text": "Сводка"},
                "data": "booking:" + action + ":offer"})
        waiting, release = asyncio.Event(), asyncio.Event()
        steps = []
        async def ack(event, **kwargs):
            if event.id == "edit":
                waiting.set()
                await release.wait()
        async def handler(event, data):
            steps.append(event.id)
        with patch.object(CallbackQuery, "answer", ack):
            first = asyncio.create_task(self.batcher(handler, callback("edit"), self.data))
            await waiting.wait()
            second = asyncio.create_task(self.batcher(handler, callback("confirm"), self.data))
            await asyncio.sleep(0)
            self.assertEqual(steps, [])
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(steps, ["edit", "confirm"])

    async def test_failed_button_ack_does_not_drop_the_queued_action(self):
        from aiogram.types import CallbackQuery
        callback = CallbackQuery.model_validate({"id": "confirm", "chat_instance": "synthetic",
            "from": {"id": 111, "is_bot": False, "first_name": "Synthetic"},
            "message": {"message_id": 5, "date": 0, "chat": {"id": 111, "type": "private"}, "text": "Сводка"},
            "data": "booking:confirm:offer"})
        handler = AsyncMock()
        with patch.object(CallbackQuery, "answer", new_callable=AsyncMock, side_effect=TimeoutError):
            await self.batcher(handler, callback, self.data)
        handler.assert_awaited_once()

    async def test_callback_user_is_separate_from_bot_author_of_card(self):
        import bot
        from aiogram.types import CallbackQuery, Message
        callback = CallbackQuery.model_validate({"id": "synthetic-edit", "chat_instance": "synthetic",
            "from": {"id": 111, "is_bot": False, "first_name": "Клиент"},
            "message": {"message_id": 7, "date": 0, "chat": {"id": 111, "type": "private"},
                        "from": {"id": 999, "is_bot": True, "first_name": "Carwash"},
                        "text": "Предложение"}, "data": "booking:edit:offer-1"})
        self.agent.proposal_action = AsyncMock(return_value="Что изменить?")
        self.agent.delivery_token.return_value = None
        reporter = Mock(dialog=AsyncMock())
        with patch.object(Message, "answer", new_callable=AsyncMock):
            await bot.on_proposal(callback, self.agent, reporter)
        user, token, action = self.agent.proposal_action.await_args.args
        self.assertEqual((user.id, user.full_name, token, action), (111, "Клиент", "offer-1", "edit"))
        reporter.dialog.assert_awaited_once_with(111, "Клиент", None, "Изменить", "Что изменить?")

    async def test_batch_errors_reach_every_waiter_and_do_not_poison_next_message(self):
        handler = AsyncMock(side_effect=RuntimeError("synthetic failure"))
        results = await asyncio.gather(*(self.batcher(handler, self.message(text), self.data)
                                        for text in ("one", "two", "three")), return_exceptions=True)
        self.assertTrue(all(isinstance(result, RuntimeError) for result in results))
        self.assertFalse(self.batcher.pending)
        handler.side_effect = None
        await self.batcher(handler, self.message("new"), self.data)
        self.assertEqual(handler.await_count, 2)

    async def test_cancelled_waiter_does_not_cancel_the_shared_reply(self):
        handler = AsyncMock(return_value="reply")
        first = asyncio.create_task(self.batcher(handler, self.message("one"), self.data))
        second = asyncio.create_task(self.batcher(handler, self.message("two"), self.data))
        await asyncio.sleep(0)
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        self.assertEqual(await first, "reply")
        handler.assert_awaited_once()
        self.assertEqual(handler.await_args.args[0].text, "one\ntwo")
        self.assertFalse(self.batcher.pending)

    async def test_zero_delay_disables_grouping(self):
        self.batcher.delay = 0
        handler = AsyncMock()
        await asyncio.gather(*(self.batcher(handler, self.message(text), self.data) for text in ("one", "two")))
        self.assertEqual(handler.await_count, 2)

    async def test_cancelled_owner_releases_all_waiters_and_cleans_the_queue(self):
        entered = asyncio.Event()
        async def blocked(message, data):
            entered.set()
            await asyncio.Event().wait()
        owner = asyncio.create_task(self.batcher(blocked, self.message("one"), self.data))
        await entered.wait()
        waiter = asyncio.create_task(self.batcher(blocked, self.message("two"), self.data))
        await asyncio.sleep(0)
        owner.cancel()
        results = await asyncio.wait_for(asyncio.gather(owner, waiter, return_exceptions=True), 1)
        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
        self.assertFalse(self.batcher.pending)
        handler = AsyncMock()
        await self.batcher(handler, self.message("new"), self.data)
        handler.assert_awaited_once()
