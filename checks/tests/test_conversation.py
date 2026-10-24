"""Сценарии без модели, Google API и Telegram-сети."""
import asyncio
import json
import logging
import tempfile
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock, patch

from openai import APIConnectionError, APITimeoutError, BadRequestError, RateLimitError

from ai import Agent, ChatUser
from tests.fakes import booking, fixture


ARGS = dict(date="2099-01-01", time="10:00", client_name="Synthetic", phone="00000000000",
            car="Fixture car", size_class="Легковой", tariff="Базовый")


def response(text="", calls=(), finish=None, facts=None, raw=False):
    if facts is not None or text and not raw and not text.startswith(("{", "```")):
        text = json.dumps({"reply": text, "facts": facts or {}}, ensure_ascii=False)
    return N(choices=[N(finish_reason=finish or ("tool_calls" if calls else "stop"),
        message=N(content=text, tool_calls=[N(id=f"call{i}", function=N(name=name,
        arguments=args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)))
        for i, (name, args) in enumerate(calls)]))])


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(self.store.config, self.store)
        self.transport = AsyncMock()
        self.agent.client.chat.completions.create = self.transport
        self.user = ChatUser(111, None, "Synthetic")

    async def say(self, text, *responses, delivered=True):
        self.transport.side_effect = list(responses)
        answer = await self.agent.reply(self.user, text)
        if delivered:
            self.agent.mark_delivered(self.user.id, self.agent.delivery_token(self.user.id))
        return answer

    async def offer(self, args=None):
        details = args or ARGS
        return await self.say("Хочу обсудить запись: " + json.dumps(details, ensure_ascii=False),
                              response(calls=[("create_booking", details)]))

    def writes(self):
        return sum(len(s.writes) for s in self.spreadsheet.sheets)

    async def test_russian_booking_requires_delivered_explicit_confirmation(self):
        answer = await self.offer()
        self.assertIn("630", answer)
        self.assertIn("без: Воск", answer)
        self.assertIn(ARGS["phone"], answer)
        self.assertEqual(self.writes(), 0)
        before = self.transport.call_count
        answer = await self.say("Да, подтверждаю запись")
        self.assertIn("Записала", answer)
        self.assertEqual(self.transport.call_count, before)
        self.assertEqual(self.writes(), 1)
        self.assertTrue(self.agent._state[111]["confirmed"])

    async def test_exact_details_and_availability_question_never_write(self):
        answer = await self.say("Свободно на 2099-01-01 в 10:00? Базовый, Легковой, Synthetic, Fixture car, 00000000000", response(calls=[("create_booking", ARGS)]))
        self.assertIn("Подтверждаете", answer)
        self.assertEqual(self.writes(), 0)

    async def test_plain_availability_query_does_not_prepare_or_write(self):
        answer = await self.say("Свободно ли в 10?", response(calls=[("get_free_slots", {"date": "2099-01-01"})]), response("Свободно в 10:00."))
        self.assertEqual(answer, "Свободно в 10:00.")
        self.assertFalse(self.agent._pending)
        self.assertEqual(self.writes(), 0)

    async def test_confirmation_before_any_offer_cannot_write(self):
        await self.say("Да, записывайте", response(calls=[("create_booking", ARGS)]))
        self.assertEqual(self.writes(), 0)

    async def test_failed_delivery_cannot_authorize_write(self):
        await self.say(json.dumps(ARGS, ensure_ascii=False), response(calls=[("create_booking", ARGS)]), delivered=False)
        await self.say("Да")
        self.assertEqual(self.writes(), 0)
        self.assertFalse(self.agent._pending)

    async def test_stale_delivery_ack_does_not_authorize_new_proposal(self):
        await self.offer()
        old = self.agent.delivery_token(111)
        new = dict(ARGS, car="Other car")
        await self.say("Другая машина Other car, Легковой", response(calls=[("create_booking", new)],
            facts={"car": ["Other car", "Other car"], "size_class": ["Легковой", "Легковой"]}), delivered=False)
        self.agent.mark_delivered(111, old)
        await self.say("Да")
        self.assertEqual(self.writes(), 0)

    async def test_negation_question_and_conditional_yes_do_not_confirm(self):
        for text in ("Нет", "Да, но другую машину", "Да?", "Подтверждаю ли я?", "Не записывайте", "Да, отменяйте"):
            with self.subTest(text=text):
                await self.offer()
                await self.say(text, response("Уточните детали."))
                self.assertEqual(self.writes(), 0)
                self.assertFalse(self.agent._pending)

    async def test_customer_correction_replaces_proposal_and_old_vehicle(self):
        await self.offer()
        await self.say("Нет, автомобиль Other car, Легковой", response(calls=[("create_booking", dict(ARGS, car="Other car"))],
            facts={"car": ["Other car", "Other car"], "size_class": ["Легковой", "Легковой"]}))
        await self.say("Да")
        mine = self.store.my_bookings(111)["bookings"]
        self.assertEqual([b["car"] for b in mine], ["Other car"])

    async def test_price_change_requires_another_confirmation(self):
        await self.offer()
        self.spreadsheet.worksheet("Услуги").values[1][2] = "900"
        answer = await self.say("Да")
        self.assertIn("810", answer)
        self.assertIn("изменились", answer)
        self.assertEqual(self.writes(), 0)
        await self.say("Да, подтверждаю запись")
        self.assertEqual(self.store.my_bookings(111)["bookings"][0]["total"], "810")

    async def test_same_total_but_changed_tariff_composition_requires_confirmation(self):
        await self.offer()
        # Воск включён бесплатно: сумма прежняя, состав иной.
        self.spreadsheet.worksheet("Услуги").values[2][2] = "0"
        self.spreadsheet.worksheet("Услуги").values[2][4] = "да"
        answer = await self.say("Да")
        self.assertIn("изменились", answer)
        self.assertEqual(self.writes(), 0)

    async def test_tariff_disabled_between_offer_and_confirmation(self):
        await self.offer()
        self.spreadsheet.worksheet("Тарифы").values[1][3] = "нет"
        answer = await self.say("Да")
        self.assertIn("недоступен", answer)
        self.assertEqual(self.writes(), 0)

    async def test_capacity_change_before_confirmation_is_not_cached(self):
        await self.offer()
        self.spreadsheet.worksheet("Слоты").values[1][2] = "0"
        answer = await self.say("Да")
        self.assertIn("недоступно", answer)
        self.assertEqual(self.writes(), 0)

    async def test_repeated_confirmation_does_not_repeat_write(self):
        await self.offer()
        await self.say("Да")
        await self.say("Да", response("Ваша запись уже оформлена."))
        self.assertEqual(self.writes(), 1)

    async def test_store_duplicate_protection_after_second_confirmed_offer(self):
        await self.offer()
        await self.say("Да")
        await self.offer()
        answer = await self.say("Да")
        self.assertIn("уже есть", answer)
        self.assertEqual(self.writes(), 1)

    async def test_expired_proposal_does_not_write(self):
        await self.offer()
        self.agent._pending[111]["created"] = time.monotonic() - 601
        self.assertIn("устарело", await self.say("Да"))
        self.assertEqual(self.writes(), 0)

    async def test_separate_users_cannot_confirm_each_others_proposals(self):
        await self.offer()
        other = ChatUser(222, None, "Other")
        self.transport.side_effect = [response("Какую услугу вы хотите?")]
        await self.agent.reply(other, "Да")
        self.assertEqual(self.writes(), 0)
        self.assertNotIn(ARGS["phone"], json.dumps(self.transport.call_args.kwargs, ensure_ascii=False))
        await self.say("Да")
        self.assertEqual(self.store.my_bookings(222)["bookings"], [])
        self.assertEqual(len(self.store.my_bookings(111)["bookings"]), 1)

    async def test_reset_discards_proposal_and_state(self):
        await self.offer()
        self.agent.reset(111)
        await self.say("Да", response("Уточните запись."))
        self.assertEqual(self.writes(), 0)
        self.assertNotIn("details", self.agent._state[111])

    async def test_uncertain_write_never_retries_or_claims_success(self):
        await self.offer()
        self.spreadsheet.worksheet("Записи").fail_write = True
        with patch.object(self.store, "create_booking", wraps=self.store.create_booking) as write:
            answer = await self.say("Да")
            self.assertEqual(write.call_count, 1)
        self.assertIn("Не удалось подтвердить", answer)
        self.assertEqual(self.agent._state[111]["stage"], "uncertain")
        await self.say("Да", response("Проверим ваши записи."))
        self.assertEqual(self.writes(), 0)

    async def test_admin_failure_does_not_hide_successful_booking(self):
        self.agent.reporter = Mock(send=AsyncMock(side_effect=RuntimeError("PRIVATE")))
        await self.offer()
        self.assertIn("Записала", await self.say("Да"))
        self.assertEqual(self.writes(), 1)

    async def test_cancel_requires_separate_confirmation(self):
        b = self.store.create_booking(**booking())
        await self.say("Отмените", response(calls=[("cancel_booking", {"booking_id": str(b["booking_id"])})]))
        self.assertEqual(len(self.store.my_bookings(111)["bookings"]), 1)
        answer = await self.say("Да, отменяйте")
        self.assertIn("отменена", answer)
        self.assertEqual(self.store.my_bookings(111)["bookings"], [])

    async def test_cancel_does_not_act_on_changed_booking_details(self):
        b = self.store.create_booking(**booking())
        await self.say("Отмена", response(calls=[("cancel_booking", {"booking_id": str(b["booking_id"])})]))
        worksheet = self.spreadsheet.worksheet("Записи")
        worksheet.values[1][worksheet.values[0].index("Автомобиль")] = "Other car"
        answer = await self.say("Да")
        self.assertIn("изменились", answer)
        self.assertEqual(len(self.store.my_bookings(111)["bookings"]), 1)

    async def test_unknown_or_foreign_cancellation_does_not_prepare(self):
        b = self.store.create_booking(**booking(telegram_id=222))
        await self.say("Отмена", response(calls=[("cancel_booking", {"booking_id": str(b["booking_id"])})]), response("Запись не найдена."))
        self.assertFalse(self.agent._pending)
        self.assertEqual(len(self.store.my_bookings(222)["bookings"]), 1)

    async def test_malformed_arguments_and_hallucinated_fields_are_rejected(self):
        for name, args in [("create_booking", "{broken"), ("create_booking", "[]"),
                           ("create_booking", "null"), ("create_booking", json.dumps(dict(ARGS, confirmed=True))),
                           ("create_booking", json.dumps(dict(ARGS, phone=123))),
                           ("calculate_price", '{"size_class":"Легковой","services":"Мойка"}'),
                           ("calculate_price", '{"size_class":"Легковой","services":[1]}'),
                           ("create_booking", json.dumps(dict(ARGS, car=""))),
                           ("unknown", "{}"), ("create_booking", "x"*16001)]:
            with self.subTest(name=name, args=args[:80]):
                result = await self.agent._call_tool(name, args, self.user)
                self.assertFalse(result["ok"])
                self.assertEqual(self.writes(), 0)
                self.assertFalse(self.agent._pending)

    async def test_invalid_customer_details_return_tool_errors(self):
        for updates in ({"date": "invalid"}, {"time": "25:00"}, {"phone": "12"}, {"size_class": "invented"}, {"tariff": "invented"}):
            result = await self.agent._call_tool("create_booking", json.dumps(dict(ARGS, **updates)), self.user)
            self.assertFalse(result["ok"])
            self.assertFalse(self.agent._pending)

    async def test_duplicate_read_calls_with_reordered_json_are_memoized(self):
        a = {"size_class": "Легковой", "tariff": "Базовый"}
        with patch.object(self.store, "calculate_price", wraps=self.store.calculate_price) as quote:
            await self.say("Цена?", response(calls=[("calculate_price", a), ("calculate_price", json.dumps(dict(reversed(list(a.items())))))]), response("630 ₽."))
        self.assertEqual(quote.call_count, 1)
        messages = self.transport.call_args.kwargs["messages"]
        self.assertEqual(len([m for m in messages if m["role"] == "tool"]), 2)

    async def test_read_memoization_does_not_cross_customer_turns(self):
        with patch.object(self.store, "free_slots", wraps=self.store.free_slots) as read:
            for _ in range(2):
                await self.say("Время?", response(calls=[("get_free_slots", {"date": "2099-01-01"})]), response("10:00."))
        self.assertEqual(read.call_count, 2)

    async def test_multiple_mutations_in_one_response_do_not_prepare(self):
        await self.say("Запись", response(calls=[("create_booking", ARGS), ("create_booking", ARGS)]))
        self.assertFalse(self.agent._pending)
        self.assertEqual(self.writes(), 0)

    async def test_tool_batch_and_iteration_limits(self):
        self.agent.config = replace(self.agent.config, ai_max_tool_calls=1)
        await self.say("Цены", response(calls=[("get_price_list", {}), ("get_price_list", {})]))
        self.assertEqual(sum(s.reads for s in self.spreadsheet.sheets), 0)
        self.agent.config = replace(self.agent.config, ai_max_tool_iterations=1, ai_max_tool_calls=12)
        answer = await self.say("Цена", response(calls=[("get_price_list", {})]))
        self.assertIn("число шагов", answer)

    async def test_truncated_tool_arguments_never_execute(self):
        for reason in ("length", "content_filter"):
            await self.say("Запись", response(calls=[("create_booking", ARGS)], finish=reason))
            self.assertFalse(self.agent._pending)
            self.assertEqual(self.writes(), 0)

    async def test_empty_content_choices_and_invalid_tool_ids_fail_safely(self):
        duplicate = response(calls=[("get_price_list", {}), ("get_price_list", {})])
        duplicate.choices[0].message.tool_calls[1].id = "call0"
        for value in (response(), N(choices=[]), duplicate):
            answer = await self.say("Вопрос", value)
            self.assertIn("не получилось", answer)
            self.assertEqual(self.writes(), 0)

    async def test_provider_timeouts_and_rate_limits_retry_only_model_request(self):
        request = N(method="POST", url="https://example.invalid")
        errors = [APITimeoutError(request=request), APIConnectionError(request=request, message="PRIVATE"),
                  RateLimitError("PRIVATE", response=N(status_code=429, request=request, headers={}), body=None)]
        for error in errors:
            with self.subTest(kind=type(error).__name__), patch("ai.asyncio.sleep", new=AsyncMock()):
                before = self.transport.call_count
                answer = await self.say("Цена?", error, response("Подскажу цену."))
                self.assertEqual(answer, "Подскажу цену.")
                self.assertEqual(self.transport.call_count - before, 2)
        self.assertEqual(self.writes(), 0)

    async def test_bad_parameter_and_context_errors_do_not_retry_or_leak(self):
        request = N(method="POST", url="https://example.invalid")
        for code in ("unsupported_parameter", "context_length_exceeded"):
            error = BadRequestError("PRIVATE", response=N(status_code=400, request=request, headers={}), body={"code": code})
            before = self.transport.call_count
            with self.assertLogs("ai", level="ERROR") as logs:
                answer = await self.say("Вопрос", error)
            self.assertEqual(self.transport.call_count - before, 1)
            self.assertNotIn("PRIVATE", answer + str(logs.output))

    async def test_retry_exhaustion_is_bounded(self):
        request = N(method="POST", url="https://example.invalid")
        error = APITimeoutError(request=request)
        with patch("ai.asyncio.sleep", new=AsyncMock()):
            before = self.transport.call_count
            await self.say("Вопрос", error, error)
        self.assertEqual(self.transport.call_count - before, 2)

    async def test_proofreading_is_opt_in_and_skips_proposals_and_receipts(self):
        await self.say("Привет", response("Здравствуйте!"))
        self.assertEqual(self.transport.call_count, 1)
        self.agent.config = replace(self.agent.config, ai_proofread_enabled=True)
        await self.offer()
        before = self.transport.call_count
        await self.say("Да")
        self.assertEqual(self.transport.call_count, before)
        await self.say("Спасибо", response("Пожалуйста."), response("Пожалуйста!", raw=True))
        self.assertEqual(self.transport.call_count - before, 2)

    async def test_long_history_is_bounded_and_only_whole_pairs_are_removed(self):
        for i in range(35):
            await self.say(f"Вопрос {i} " + "сведения " * 30, response("Подскажу по услугам."))
        history = self.agent._history[111]
        self.assertLessEqual(len(history), 8)
        self.assertLessEqual(sum(len(m["content"]) for m in history), self.agent.config.ai_history_chars)
        self.assertEqual([m["role"] for m in history], ["user", "assistant"] * (len(history)//2))
        self.assertIn("Вопрос 34", history[-2]["content"])
        self.assertFalse(any(m["role"] == "tool" for m in history))

    async def test_validated_quote_survives_compaction_without_summarization_calls(self):
        await self.say("Базовый для легкового", response(calls=[("calculate_price", {"size_class": "Легковой", "tariff": "Базовый"})]), response("630 ₽, без воска."))
        for _ in range(10):
            await self.say("Где вы находитесь?", response("Адрес уточнит администратор."))
        messages = self.transport.call_args.kwargs["messages"]
        state = json.loads(next(m["content"][6:] for m in messages if m["content"].startswith("STATE ")))
        self.assertEqual(state["quote"]["total"], 630)
        self.assertEqual(state["selection"]["size_class"], "Легковой")
        self.assertEqual(self.transport.call_count, 12)

    async def test_oversized_input_is_not_sent_or_truncated(self):
        await self.say("x" * 6001)
        self.transport.assert_not_called()

    async def test_ai_disabled_does_not_construct_or_call_provider(self):
        with patch("ai.AsyncOpenAI") as factory:
            agent = Agent(replace(self.store.config, ai_enabled=False), self.store)
        self.assertIsNone(agent.client)
        factory.assert_not_called()
        self.assertIn("отключён", await agent.reply(self.user, "Запишите"))
        self.assertEqual(self.writes(), 0)

    async def test_sdk_debug_logging_does_not_leak_synthetic_payload(self):
        from providers import private_sdk_logging
        sdk = logging.getLogger("openai._base_client")
        sdk.setLevel(logging.DEBUG)
        private_sdk_logging()
        with self.assertLogs(level="DEBUG") as logs:
            sdk.debug("PRIVATE_TOKEN_PHONE_PAYLOAD")
            logging.getLogger("ai").info("safe")
        self.assertNotIn("PRIVATE", str(logs.output))

    async def test_store_reads_run_off_event_loop(self):
        import threading
        loop_thread = threading.get_ident()
        seen = []
        original = self.store.calculate_price
        def quote(*args):
            seen.append(threading.get_ident())
            return original(*args)
        with patch.object(self.store, "calculate_price", side_effect=quote):
            await self.say("Цена", response(calls=[("calculate_price", {"size_class": "Легковой", "tariff": "Базовый"})]), response("630 ₽."))
        self.assertTrue(seen)
        self.assertTrue(all(t != loop_thread for t in seen))

    async def test_class_filtered_catalog_keeps_removed_items_and_correct_prices(self):
        all_prices = await self.agent._call_tool("get_price_list", "{}", self.user)
        selected = await self.agent._call_tool("get_price_list", '{"size_class":"Легковой"}', self.user)
        self.assertEqual(selected["tariffs"][0]["prices"], {"Легковой": 630})
        self.assertEqual(selected["tariffs"][0]["not_included_now"], ["Воск"])
        self.assertLess(len(json.dumps(selected, ensure_ascii=False)), len(json.dumps(all_prices, ensure_ascii=False)))
        wrong = await self.agent._call_tool("get_price_list", '{"size_class":"invented"}', self.user)
        self.assertFalse(wrong["ok"])

    async def test_identical_reproposal_does_not_accept_old_delivery_token(self):
        await self.offer()
        old = self.agent.delivery_token(111)
        await self.say("Покажите ещё раз", response(calls=[("create_booking", ARGS)]), delivered=False)
        self.agent.mark_delivered(111, old)
        await self.say("Да")
        self.assertEqual(self.writes(), 0)

    async def test_reset_during_model_request_discards_late_proposal(self):
        async def reply_then_reset(**kwargs):
            self.agent.reset(111)
            return response(calls=[("create_booking", ARGS)])
        self.transport.side_effect = reply_then_reset
        answer = await self.agent.reply(self.user, "Запись")
        self.assertIn("сброшен", answer)
        self.assertFalse(self.agent._pending)
        self.assertNotIn(111, self.agent._state)
        self.assertEqual(self.writes(), 0)

    async def test_provider_failure_after_read_tools_does_not_repeat_tool_execution(self):
        request = N(method="POST", url="https://example.invalid")
        error = APITimeoutError(request=request)
        with patch.object(self.store, "calculate_price", wraps=self.store.calculate_price) as quote, \
             patch("ai.asyncio.sleep", new=AsyncMock()):
            await self.say("Цена", response(calls=[("calculate_price", {"size_class": "Легковой", "tariff": "Базовый"})]), error, response("630 ₽."))
        self.assertEqual(quote.call_count, 1)
        self.assertEqual(self.writes(), 0)

    async def test_google_read_failure_does_not_create_proposal(self):
        with patch.object(self.store, "get_catalog", side_effect=TimeoutError("PRIVATE")) as read:
            answer = await self.say(json.dumps(ARGS, ensure_ascii=False), response(calls=[("create_booking", ARGS)]), response("Таблица недоступна."))
            read.assert_called_once()
        self.assertFalse(self.agent._pending)
        self.assertEqual(self.writes(), 0)
        self.assertNotIn("PRIVATE", answer)

    async def test_readonly_mapping_remains_readonly_after_confirmation(self):
        await self.offer()
        region = next(r for r in self.store._adaptive.document["regions"] if r["role"] == "bookings")
        region["permissions"]["write"] = False
        from adaptive import atomic_json
        atomic_json(self.store._adaptive.path, self.store._adaptive.document)
        self.store.refresh_schema(force=True, validate=True)
        answer = await self.say("Да")
        self.assertNotIn("Записала", answer)
        self.assertEqual(self.writes(), 0)

    async def test_two_concurrent_confirmations_write_once(self):
        await self.offer()
        self.transport.side_effect = [response("Запись уже оформлена.")]
        answers = await asyncio.gather(self.agent.reply(self.user, "Да"), self.agent.reply(self.user, "Да"))
        self.assertEqual(self.writes(), 1)
        self.assertEqual(sum("Записала" in a for a in answers), 1)

    async def test_telegram_delivery_marks_proposal_only_after_all_chunks(self):
        import bot
        message = Mock()
        message.from_user = N(id=111, username=None, full_name="Synthetic")
        message.chat.id = 111
        message.answer = AsyncMock()
        reporter = Mock(dialog=AsyncMock())
        sender = Mock()
        sender.__aenter__ = AsyncMock()
        sender.__aexit__ = AsyncMock()
        self.transport.side_effect = [response(calls=[("create_booking", ARGS)])]
        with patch("bot.ChatActionSender.typing", return_value=sender):
            await bot.ask_agent(message, self.agent, reporter, json.dumps(ARGS, ensure_ascii=False))
        self.assertTrue(self.agent._pending[111]["delivered"])
        await self.say("Да")
        self.assertEqual(self.writes(), 1)

    async def test_telegram_delivery_failure_cannot_confirm_unseen_proposal(self):
        import bot
        message = Mock()
        message.from_user = N(id=111, username=None, full_name="Synthetic")
        message.chat.id = 111
        message.answer = AsyncMock(side_effect=TimeoutError("PRIVATE"))
        reporter = Mock(dialog=AsyncMock())
        sender = Mock()
        sender.__aenter__ = AsyncMock()
        sender.__aexit__ = AsyncMock()
        self.transport.side_effect = [response(calls=[("create_booking", ARGS)])]
        with patch("bot.ChatActionSender.typing", return_value=sender), self.assertRaises(TimeoutError):
            await bot.ask_agent(message, self.agent, reporter, json.dumps(ARGS, ensure_ascii=False))
        await self.say("Да")
        self.assertEqual(self.writes(), 0)

    async def test_correction_invalidates_old_quote_after_history_compaction(self):
        await self.say("Базовый, легковой", response(calls=[("calculate_price", {"size_class": "Легковой", "tariff": "Базовый"})]), response("630 ₽."))
        await self.say("Нет, у меня теперь кроссовер", response("Учту исправление.",
            facts={"size_class": ["Кроссовер", "кроссовер"]}))
        for _ in range(6):
            await self.say("Адрес?", response("Уточните у администратора."))
        self.assertNotIn("selection", self.agent._state[111])
        self.assertNotIn("quote", self.agent._state[111])
        self.assertNotIn("Легковой", json.dumps(self.transport.call_args.kwargs["messages"], ensure_ascii=False))

    async def test_corrected_tariff_and_class_use_recalculated_price(self):
        await self.offer()
        args = dict(ARGS, size_class="Кроссовер", tariff="", services=["Мойка"])
        proposal = await self.say("Нет, кроссовер и только мойка", response(calls=[("create_booking", args)],
            facts={"size_class": ["Кроссовер", "кроссовер"], "tariff": [None, "только"], "services": [["Мойка"], "мойка"]}))
        self.assertIn("800", proposal)
        self.assertNotIn("630", proposal)
        await self.say("Да")
        self.assertEqual(self.store.my_bookings(111)["bookings"][0]["total"], "800")

    async def test_oversized_correction_discards_pending_consent(self):
        await self.offer()
        await self.say("Нет, исправьте " + "x" * 6000)
        self.assertFalse(self.agent._pending)
        await self.say("Да", response("Проверим детали заново."))
        self.assertEqual(self.writes(), 0)

    async def test_repricing_does_not_offer_slot_that_became_unavailable(self):
        await self.offer()
        self.spreadsheet.worksheet("Услуги").values[1][2] = "900"
        self.spreadsheet.worksheet("Слоты").values[1][2] = "0"
        answer = await self.say("Да")
        self.assertIn("недоступно", answer)
        self.assertNotIn("Подтверждаете", answer)
        self.assertFalse(self.agent._pending)
        self.assertEqual(self.writes(), 0)

    async def test_filtered_prices_load_catalog_once_even_with_cache_disabled(self):
        self.store.config = replace(self.store.config, catalog_cache_sec=0)
        with patch.object(self.store, "get_catalog", wraps=self.store.get_catalog) as read:
            result = await self.agent._call_tool("get_price_list", '{"size_class":"Легковой"}', self.user)
        self.assertTrue(result["ok"])
        self.assertEqual(read.call_count, 1)

    async def test_proposal_loads_fresh_catalog_once_with_cache_disabled(self):
        self.store.config = replace(self.store.config, catalog_cache_sec=0)
        with patch.object(self.store, "get_catalog", wraps=self.store.get_catalog) as read:
            await self.offer()
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.writes(), 0)
