"""Русские реплики и заданные ответы AI: проверка приложения, не понимания живой моделью."""
import json
import asyncio
import time
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from ai import Agent, ChatUser
from conversation import model_state
from tests.fakes import fixture
from tests.test_conversation import ARGS, response


def envelope(reply="Учту.", facts=None, tentative=None):
    return json.dumps(dict(reply=reply, facts=facts or {}, tentative=tentative or {}), ensure_ascii=False)


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(self.store.config, self.store)
        self.transport = AsyncMock()
        self.agent.client.chat.completions.create = self.transport
        self.user = ChatUser(111, None, "Имя из Telegram не является именем клиента")
        self.turns = 0

    async def say(self, text, facts=None, tentative=None, calls=(), reply="Учту.", follow=None):
        self.turns += 1
        content = envelope(reply, facts, tentative)
        self.transport.side_effect = [response(content, calls=calls)]
        if calls and calls[0][0] not in {"create_booking", "cancel_booking"} or follow is not None:
            self.transport.side_effect = [response(content, calls=calls), response(envelope(follow or reply))]
        answer = await self.agent.reply(self.user, text)
        self.agent.mark_delivered(111, self.agent.delivery_token(111))
        return answer

    def facts(self):
        return self.agent._state[111].get("facts", {})

    async def test_thirty_two_turn_russian_booking_with_corrections_and_failures(self):
        services = self.spreadsheet.worksheet("Услуги").values
        services.extend([["Полировка", "Не выполняется", "3000", "4000", "нет"],
                         ["Пылесос", "Салон", "200", "250", "да"]])
        self.spreadsheet.worksheet("Слоты").values[1][2] = "0"
        self.spreadsheet.worksheet("Слоты").values.extend([
            ["01.01.2099", "11:00", "1"], ["02.01.2099", "12:00", "1"]])
        await self.say("сам мо бой вы же там че эта мастерстая ну короче у меня есть камрюха 3.5 че там по полировке",
            {"car": ["камрюха 3.5", "камрюха 3.5"], "requested_service": ["полировке", "полировке"]})
        await self.say("я Антон", {"client_name": ["Антон", "Антон"]})
        await self.say("телефон +7 000 111-22-33", {"phone": ["+7 000 111-22-33", "+7 000 111-22-33"]})
        await self.say("класс Легковой", {"size_class": ["Легковой", "Легковой"]})
        await self.say("Полировка есть?", calls=[("get_price_list", {"size_class": "Легковой"})], reply="Полировка недоступна. Есть мойка.")
        self.assertIn("Полировка", self.agent._state[111]["catalog"]["unavailable_services"])
        await self.say("Тогда Мойка", {"services": [["Мойка"], "Мойка"], "requested_service": ["Мойка", "Мойка"]})
        await self.say("Сколько?", calls=[("calculate_price", {"size_class": "Легковой", "services": ["Мойка"]})], reply="700 ₽.")
        await self.say("А где вы?")
        await self.say("А тариф Базовый сколько?", calls=[("calculate_price", {"size_class": "Легковой", "tariff": "Базовый"})], reply="630 ₽, без воска.")
        await self.say("Базовый пока сравниваю", tentative={"tariff": ["Базовый", "Базовый"]})
        self.assertEqual(self.facts()["services"], ["Мойка"])
        await self.say("Оставляем Мойка, тариф не нужен", {"services": [["Мойка"], "Мойка"], "tariff": [None, "тариф не нужен"]})
        await self.say("2099-01-01", {"date": ["2099-01-01", "2099-01-01"]})
        await self.say("Есть время?", calls=[("get_free_slots", {"date": "2099-01-01"})], reply="Есть 11:00, 10:00 занято.")
        occupied = await self.say("Хочу 10:00", {"time": ["10:00", "10:00"]}, calls=[("create_booking", {})], follow="10:00 занято, есть 11:00.")
        self.assertIn("занято", occupied)
        self.assertNotIn(111, self.agent._pending)
        await self.say("Тогда 11:00", {"time": ["11:00", "11:00"]})
        await self.say("Нет, 2099-01-02", {"date": ["2099-01-02", "2099-01-02"]})
        self.assertNotIn("time", self.facts())
        await self.say("12:00", {"time": ["12:00", "12:00"]})
        await self.say("Имя исправь: Артём", {"client_name": ["Артём", "Артём"]})
        await self.say("Телефон другой: +7 000 222-33-44", {"phone": ["+7 000 222-33-44", "+7 000 222-33-44"]})
        await self.say("Авто Toyota Corolla 1.6", {"car": ["Toyota Corolla 1.6", "Toyota Corolla 1.6"]})
        self.assertNotIn("size_class", self.facts())
        await self.say("Легковой", {"size_class": ["Легковой", "Легковой"]})
        await self.say("Мойка и Пылесос", {"services": [["Мойка", "Пылесос"], "Мойка и Пылесос"]})
        await self.say("Сколько вместе?", calls=[("calculate_price", {"size_class": "Легковой", "services": ["Мойка", "Пылесос"]})], reply="900 ₽.")
        await self.say("Нет, только Мойка", {"services": [["Мойка"], "Мойка"]})
        self.assertNotIn("quote", self.agent._state[111])
        await self.say("Про полировку потом уточню")
        await self.say("Воск есть?", calls=[("get_price_list", {})], reply="Воск недоступен.")
        with patch.object(self.store, "free_slots", side_effect=TimeoutError("synthetic private failure")) as read:
            await self.say("Проверьте ещё время", calls=[("get_free_slots", {"date": "2099-01-02"})], reply="Временно не удалось проверить.")
            self.assertEqual(read.call_count, 1)
        await self.say("Попробуйте ещё", calls=[("get_free_slots", {"date": "2099-01-02"})], reply="12:00 свободно.")
        with patch.object(self.store, "calculate_price", wraps=self.store.calculate_price) as quote:
            await self.say("А цена ещё раз?", calls=[("calculate_price", {"size_class": "sedan", "services": ["Мойка"]})], reply="Нужен класс из прайса.")
            self.assertEqual(quote.call_count, 1)
        await self.say("Адрес напомните")
        before = self.transport.call_count
        proposal = await self.say("Теперь оформляем", calls=[("create_booking", {})])
        self.assertEqual(self.transport.call_count, before + 1)
        for value in ("Артём", "Toyota Corolla 1.6", "+7 000 222-33-44", "2099-01-02", "12:00", "700"):
            self.assertIn(value, proposal)
        self.assertNotIn("Пылесос", proposal)
        self.assertNotIn("Базовый", proposal)
        self.assertEqual(sum(len(s.writes) for s in self.spreadsheet.sheets), 0)
        with patch.object(self.store, "create_booking", wraps=self.store.create_booking) as write:
            result = await self.say("Да, подтверждаю запись")
            self.assertEqual(write.call_count, 1)
            args = write.call_args.kwargs
            self.assertEqual((args["name"], args["car"], args["phone"]), ("Артём", "Toyota Corolla 1.6", "+7 000 222-33-44"))
            self.assertEqual((args["date_str"], args["time_str"], args["tariff"], args["services"]), ("2099-01-02", "12:00", "", ["Мойка"]))
        self.assertIn("Записала", result)
        self.assertEqual(self.turns, 32)
        self.assertLessEqual(len(self.agent._history[111]), 8)
        self.assertNotIn("+7 000 222-33-44", json.dumps(model_state(self.agent._state[111]), ensure_ascii=False))

    async def test_early_facts_survive_thirty_messages_without_extra_requests_or_reasking(self):
        await self.say("Toyota Camry 3.5", {"car": ["Toyota Camry 3.5", "Toyota Camry 3.5"]})
        await self.say("Меня зовут Антон", {"client_name": ["Антон", "Антон"]})
        for _ in range(30):
            async def from_context(**kwargs):
                state = json.loads(next(m["content"][9:] for m in kwargs["messages"] if m["content"].startswith("CUSTOMER ")))
                self.assertEqual(state["facts"]["car"], "Toyota Camry 3.5")
                self.assertEqual(state["facts"]["client_name"], "Антон")
                return response(envelope("Антон, подскажу услуги для Toyota Camry 3.5."))
            self.transport.side_effect = from_context
            answer = await self.agent.reply(self.user, "Подскажите про мойку")
            self.assertNotIn("Как вас зовут", answer)
        self.assertEqual(self.transport.call_count, 32)

    async def test_fabricated_facts_and_model_text_sources_are_rejected_atomically(self):
        for facts in ({"car": ["Toyota Camry", "привет"]}, {"phone": ["70009990000", "привет"]},
                      {"confirmed": [True, "привет"]}, {"client_name": ["Антон", "Антон"]}):
            await self.say("привет", facts)
            self.assertFalse(self.facts())
        await self.say("Антон", {"client_name": ["Антон", "Антон"], "car": ["Toyota", "не было"]})
        self.assertFalse(self.facts())

    async def test_unconfigured_aliases_and_wrong_catalog_kinds_are_not_guessed(self):
        await self.say("sedan", {"size_class": ["Легковой", "sedan"]})
        self.assertNotIn("size_class", self.facts())
        for args, code in (({"size_class": "Легковой", "tariff": "Мойка"}, "service_is_not_tariff"),
                           ({"size_class": "Легковой", "services": ["Базовый"]}, "tariff_is_not_service")):
            result = await self.agent._call_tool("calculate_price", json.dumps(args), self.user)
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], code)
        self.assertIsNone(self.store.get_catalog().match_class("Лег"))

    async def test_invalid_model_booking_cannot_replace_corrected_facts(self):
        await self.say("Антон", {"client_name": ["Антон", "Антон"]})
        await self.say("Готово", calls=[("create_booking", ARGS)], follow="Уточним данные.")
        self.assertNotIn(111, self.agent._pending)
        self.assertEqual(self.facts()["client_name"], "Антон")
        self.assertEqual(sum(len(s.writes) for s in self.spreadsheet.sheets), 0)

    async def test_unknown_details_cannot_be_invented_from_telegram_profile_or_tool_call(self):
        await self.say("Хочу записаться", calls=[("create_booking", ARGS)], follow="Нужны данные.")
        self.assertNotIn(111, self.agent._pending)
        self.assertFalse(self.facts())

    async def test_tentative_choices_are_separate_and_do_not_authorize_proposal(self):
        await self.say("Может Базовый", tentative={"tariff": ["Базовый", "Базовый"]})
        self.assertNotIn("tariff", self.facts())
        self.assertEqual(self.agent._state[111]["tentative"]["tariff"], "Базовый")
        await self.say(json.dumps(ARGS, ensure_ascii=False), calls=[("create_booking", ARGS)], follow="Уточните выбор.")
        self.assertNotIn(111, self.agent._pending)
        self.agent._state[111] = {"facts": dict(ARGS), "tentative": {"requested_datetime": "завтра к шести"}}
        with self.assertRaises(ValueError):
            self.agent._booking_args({}, self.user)
        self.assertIn("time", model_state(self.agent._state[111])["missing"])

    async def test_ordinary_negation_and_oversize_do_not_erase_unrelated_facts(self):
        await self.say("Антон", {"client_name": ["Антон", "Антон"]})
        await self.say("Не знаю где вы, адрес?")
        await self.agent.reply(self.user, "x" * 6001)
        self.assertEqual(self.facts()["client_name"], "Антон")

    async def test_phone_is_local_and_not_repeated_in_later_requests(self):
        await self.say("+7 000 111-22-33", {"phone": ["+7 000 111-22-33", "+7 000 111-22-33"]})
        await self.say("А адрес?")
        payload = json.dumps(self.transport.call_args.kwargs, ensure_ascii=False)
        self.assertNotIn("+7 000 111-22-33", payload)
        self.assertIn("phone_known", payload)

    async def test_users_and_resets_isolate_customer_memory(self):
        await self.say("Антон", {"client_name": ["Антон", "Антон"]})
        self.transport.side_effect = [response(envelope("Как вас зовут?"))]
        await self.agent.reply(ChatUser(222, None, "Другой"), "Привет")
        self.assertNotIn("Антон", json.dumps(self.transport.call_args.kwargs, ensure_ascii=False))
        self.agent.reset(111)
        await self.say("Привет")
        self.assertNotIn("client_name", self.facts())

    async def test_repeated_invalid_calls_stop_without_spending_iteration_budget(self):
        bad = ("calculate_price", {"size_class": "sedan", "tariff": "Мойка"})
        self.transport.side_effect = [response(calls=[bad]), response(calls=[bad])]
        with patch.object(self.store, "calculate_price", wraps=self.store.calculate_price) as read:
            answer = await self.agent.reply(self.user, "Сколько?")
        self.assertIn("остановлен", answer)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.transport.call_count, 2)

    async def test_success_claims_are_blocked_in_plain_json_and_proofread_responses(self):
        claims = ["Вы записаны.", "Бронирование создано.", "Запись отменена.", "Записала вас на завтра.",
                  "Я оформила запись.", "Место забронировано.", "Уже записан!", "Запись успешно создана.",
                  "Я отменила вашу запись."]
        for text in claims:
            answer = await self.say("Оформить?", reply=text)
            self.assertIn("не подтверждён", answer, text)
        self.agent.config = replace(self.agent.config, ai_proofread_enabled=True)
        self.transport.side_effect = [response(envelope("Проверим данные.")), response("Вы записаны.", raw=True)]
        answer = await self.agent.reply(self.user, "Проверить?")
        self.assertIn("не подтверждён", answer)
        self.assertEqual(sum(len(s.writes) for s in self.spreadsheet.sheets), 0)

    async def test_plain_response_is_rejected_instead_of_silently_losing_memory(self):
        self.transport.side_effect = [response("Учту вашу Toyota.", raw=True)] * 2
        answer = await self.agent.reply(self.user, "Toyota Camry 3.5")
        self.assertIn("Не удалось", answer)
        self.assertFalse(self.facts())
        self.assertEqual(self.transport.call_count, 2)

    async def test_plain_answer_is_repaired_only_with_valid_sourced_memory(self):
        self.transport.side_effect = [response("Какой автомобиль?", raw=True),
            response(calls=[("respond", {"reply": "Никита, какой автомобиль?", "facts": {
                "client_name": ["Никита", "Никита"]}})])]
        answer = await self.agent.reply(self.user, "Никита")
        self.assertEqual(answer, "Никита, какой автомобиль?")
        self.assertEqual(self.facts(), {"client_name": "Никита"})
        self.assertEqual(self.transport.call_args_list[1].kwargs["tool_choice"],
                         {"type": "function", "function": {"name": "respond"}})

    async def test_production_requests_explain_the_required_memory_protocol(self):
        async def reply_from_contract(**payload):
            self.assertEqual(payload["tool_choice"], "required")
            prompt = payload["messages"][0]["content"]
            self.assertIn("заверши ответ вызовом respond", prompt)
            self.assertIn("точная цитата из текущего сообщения", prompt)
            example = prompt.split("Пример на сообщение «Никита»: ", 1)[1].splitlines()[0]
            memory = json.loads(example.removesuffix("."))
            self.assertEqual(memory["facts"]["client_name"], ["Никита", "Никита"])
            self.assertIn("respond", [tool["function"]["name"] for tool in payload["tools"]])
            return response(calls=[("respond", memory)])

        self.transport.side_effect = reply_from_contract
        answer = await self.agent.reply(self.user, "Никита")
        self.assertEqual(answer, "Никита, какой у вас автомобиль?")
        self.assertEqual(self.facts()["client_name"], "Никита")
        self.assertFalse(sum(len(sheet.writes) for sheet in self.spreadsheet.sheets))

    async def test_inferred_catalog_class_is_repaired_before_any_booking_tool(self):
        self.transport.side_effect = [response(calls=[
            ("respond", {"reply": "Класс D.", "facts": {
                "car": ["Toyota Camry", "Toyota Camry"],
                "size_class": ["D", "Toyota Camry"]}}),
            ("create_booking", ARGS)]), response(calls=[
                ("respond", {"reply": "Какой класс автомобиля?", "facts": {
                    "car": ["Toyota Camry", "Toyota Camry"]}})])]
        answer = await self.agent.reply(self.user, "Toyota Camry")
        self.assertEqual(answer, "Какой класс автомобиля?")
        self.assertEqual(self.facts(), {"car": "Toyota Camry"})
        self.assertEqual(self.transport.call_count, 2)
        self.assertNotIn(111, self.agent._pending)
        self.assertFalse(sum(len(sheet.writes) for sheet in self.spreadsheet.sheets))
        feedback = [message for message in self.transport.call_args.kwargs["messages"]
                    if message["role"] == "tool"]
        self.assertEqual(len(feedback), 2)
        self.assertTrue(all(json.loads(message["content"])["executed"] is False for message in feedback))

    async def test_catalog_memory_repair_is_limited_and_keeps_validation(self):
        invalid = response(calls=[("respond", {"reply": "Класс D.", "facts": {
            "car": ["Toyota Camry", "Toyota Camry"], "size_class": ["D", "Toyota Camry"]}})])
        self.transport.side_effect = [invalid, invalid]
        answer = await self.agent.reply(self.user, "Toyota Camry")
        self.assertIn("Не удалось", answer)
        self.assertFalse(self.facts())
        self.assertEqual(self.transport.call_count, 2)

    async def test_repeated_successful_read_requests_a_final_memory_reply(self):
        self.transport.side_effect = [response(calls=[("get_price_list", {})]),
            response(calls=[("get_price_list", {})]),
            response(calls=[("respond", {"reply": "Есть мойка кузова."})])]
        with patch.object(self.agent, "_call_tool", wraps=self.agent._call_tool) as tool:
            answer = await self.agent.reply(self.user, "Какие услуги есть?")
        self.assertEqual(answer, "Есть мойка кузова.")
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(self.transport.call_args_list[2].kwargs["tool_choice"],
                         {"type": "function", "function": {"name": "respond"}})
        self.assertFalse(sum(len(sheet.writes) for sheet in self.spreadsheet.sheets))

    async def test_old_customer_source_can_be_repaired_without_reapplying_old_facts(self):
        await self.say("Никита", {"client_name": ["Никита", "Никита"]})
        self.transport.side_effect = [response(calls=[("respond", {"reply": "Учту.", "facts": {
            "client_name": ["Никита", "Никита"], "car": ["Toyota Camry", "Toyota Camry"]}})]),
            response(calls=[("respond", {"reply": "Какой класс?", "facts": {
                "car": ["Toyota Camry", "Toyota Camry"]}})])]
        answer = await self.agent.reply(self.user, "у меня Toyota Camry")
        self.assertEqual(answer, "Какой класс?")
        self.assertEqual(self.facts(), {"client_name": "Никита", "car": "Toyota Camry"})

    async def test_repair_cannot_accept_a_fabricated_customer_quote(self):
        self.transport.side_effect = [response("Учту.", raw=True), response(calls=[
            ("respond", {"reply": "Учту Toyota.", "facts": {"car": ["Toyota", "Toyota"]}})])]
        answer = await self.agent.reply(self.user, "привет")
        self.assertIn("Не удалось", answer)
        self.assertFalse(self.facts())
        self.assertEqual(self.transport.call_count, 2)

    async def test_date_and_time_evidence_rejects_stale_or_trailing_values(self):
        await self.say("завтра", {"date": ["2099-01-01", "завтра"]})
        self.assertNotIn("date", self.facts())
        result = await self.agent._call_tool("create_booking", json.dumps(dict(ARGS, time="10:00 garbage")), self.user)
        self.assertFalse(result["ok"])

    async def test_malformed_memory_envelope_never_reaches_customer_or_tools(self):
        self.transport.side_effect = [response('{"reply":"Записала",', calls=[("create_booking", ARGS)])]
        answer = await self.agent.reply(self.user, "Запишите")
        self.assertNotIn("{", answer)
        self.assertNotIn("Записала", answer)
        self.assertNotIn(111, self.agent._pending)

    async def test_terminal_memory_tool_needs_one_request_and_keeps_original_prompt(self):
        self.transport.side_effect = [response(calls=[("respond", {
            "reply": "Учту автомобиль.", "facts": {"car": ["камрюха 3.5", "камрюха 3.5"]}})])]
        answer = await self.agent.reply(self.user, "у меня камрюха 3.5")
        self.assertEqual(answer, "Учту автомобиль.")
        self.assertEqual(self.facts()["car"], "камрюха 3.5")
        self.assertEqual(self.transport.call_count, 1)
        payload = self.transport.call_args.kwargs
        self.assertNotIn("CURRENT", payload["messages"][0]["content"])
        self.assertIn("respond", [t["function"]["name"] for t in payload["tools"]])

    async def test_memory_tool_can_precede_business_tools_without_extra_extraction_request(self):
        self.transport.side_effect = [response(calls=[
            ("respond", {"reply": "", "facts": {"car": ["Toyota Camry 3.5", "Toyota Camry 3.5"]}}),
            ("get_price_list", {})]), response(calls=[("respond", {"reply": "Подскажу по мойке."})])]
        answer = await self.agent.reply(self.user, "Toyota Camry 3.5, что есть?")
        self.assertEqual(answer, "Подскажу по мойке.")
        self.assertEqual(self.facts()["car"], "Toyota Camry 3.5")
        self.assertEqual(self.transport.call_count, 2)

    async def test_arbitrary_order_and_service_correction_invalidates_old_proposal(self):
        for text, facts in [("10:00", {"time": ["10:00", "10:00"]}),
                            ("70001112233", {"phone": ["70001112233", "70001112233"]}),
                            ("Базовый", {"tariff": ["Базовый", "Базовый"]}),
                            ("2099-01-01", {"date": ["2099-01-01", "2099-01-01"]}),
                            ("Антон", {"client_name": ["Антон", "Антон"]}),
                            ("Toyota Camry 3.5 Легковой", {"car": ["Toyota Camry 3.5", "Toyota Camry 3.5"], "size_class": ["Легковой", "Легковой"]})]:
            await self.say(text, facts)
        proposal = await self.say("Оформляем", calls=[("create_booking", {})])
        self.assertIn("630", proposal)
        await self.say("Не, хочу полировку", {"requested_service": ["полировку", "полировку"]})
        self.assertNotIn(111, self.agent._pending)
        self.assertNotIn("quote", self.agent._state[111])
        self.assertNotIn("tariff", self.facts())
        self.assertNotIn("services", self.facts())
        stale = dict(ARGS, client_name="Антон", phone="70001112233", car="Toyota Camry 3.5")
        await self.say("Да", calls=[("create_booking", stale)], follow="Нужен новый выбор услуги.")
        self.assertNotIn(111, self.agent._pending)
        self.assertEqual(sum(len(s.writes) for s in self.spreadsheet.sheets), 0)

    async def test_car_correction_cannot_resurrect_class_from_recent_history(self):
        await self.say("Toyota Camry Легковой", {"car": ["Toyota Camry", "Toyota Camry"], "size_class": ["Легковой", "Легковой"]})
        await self.say("Теперь Другой автомобиль", {"car": ["Другой автомобиль", "Другой автомобиль"]})
        result = self.agent._booking_args
        with self.assertRaises(ValueError):
            result(dict(ARGS, car="Другой автомобиль"), self.user)

    async def test_concurrent_users_keep_facts_and_locks_separate(self):
        async def transport(**kwargs):
            text = kwargs["messages"][-1]["content"]
            await asyncio.sleep(0)
            return response(calls=[("respond", {"reply": "Учту.", "facts": {"client_name": [text, text]}})])
        self.transport.side_effect = transport
        await asyncio.gather(self.agent.reply(self.user, "Антон"),
                             self.agent.reply(ChatUser(222, None, "Другой"), "Борис"))
        self.assertEqual(self.agent._state[111]["facts"]["client_name"], "Антон")
        self.assertEqual(self.agent._state[222]["facts"]["client_name"], "Борис")
        self.assertIsNot(self.agent._locks[111], self.agent._locks[222])

    async def test_idle_cleanup_removes_personal_data_histories_proposals_and_locks(self):
        await self.say("Антон", {"client_name": ["Антон", "Антон"]})
        self.agent._pending[111] = {"action": "create_booking", "phone": "70001112233"}
        self.agent.cleanup_sessions(now=time.monotonic() + self.agent.config.ai_session_ttl_sec + 1)
        for sessions in (self.agent._history, self.agent._state, self.agent._pending, self.agent._locks,
                         self.agent._generation, self.agent._last_seen, self.agent._active, self.agent._turn_text):
            self.assertNotIn(111, sessions)

    async def test_session_cap_does_not_erase_reused_or_active_customer(self):
        self.agent.config = replace(self.agent.config, ai_max_sessions=2)
        await self.say("Антон", {"client_name": ["Антон", "Антон"]})
        self.transport.side_effect = [response(envelope("Учту."))]
        await self.agent.reply(ChatUser(222, None, ""), "Привет")
        await self.say("Снова я")
        self.assertEqual(self.facts()["client_name"], "Антон")
        async with self.agent._session(111):
            self.agent.cleanup_sessions(now=time.monotonic() + 100000)
            self.assertIn(111, self.agent._state)
        for user_id in (333, 444, 555):
            self.transport.side_effect = [response(envelope("Учту."))]
            await self.agent.reply(ChatUser(user_id, None, ""), "Привет")
        self.assertLessEqual(len(self.agent._state), 2)
        self.assertLessEqual(len(self.agent._history), 2)
        self.assertLessEqual(len(self.agent._locks), 2)

    async def test_session_state_stays_compact_across_one_hundred_messages(self):
        await self.say("Toyota Camry 3.5", {"car": ["Toyota Camry 3.5", "Toyota Camry 3.5"]})
        for _ in range(100):
            await self.say("Адрес?")
        self.assertLess(len(json.dumps(self.agent._state[111]).encode()), 1024)
        self.assertLessEqual(len(self.agent._history[111]), 8)
        self.assertLessEqual(sum(len(m["content"]) for m in self.agent._history[111]), 6000)
        self.assertNotIn(111, self.agent._turn_text)

    async def test_class_before_initial_car_and_additional_service_do_not_lose_known_facts(self):
        self.spreadsheet.worksheet("Услуги").values.append(["Пылесос", "Салон", "200", "250", "да"])
        await self.say("Легковой", {"size_class": ["Легковой", "Легковой"]})
        await self.say("Toyota Camry", {"car": ["Toyota Camry", "Toyota Camry"]})
        self.assertEqual(self.facts()["size_class"], "Легковой")
        await self.say("Мойка", {"services": [["Мойка"], "Мойка"]})
        await self.say("И Пылесос", {"services": [["Мойка", "Пылесос"], "Пылесос"]})
        self.assertEqual(self.facts()["services"], ["Мойка", "Пылесос"])
        await self.say("Добавьте Базовый", {"tariff": ["Базовый", "Базовый"]})
        self.assertEqual(self.facts()["services"], ["Мойка", "Пылесос"])
        self.assertEqual(self.facts()["tariff"], "Базовый")

    async def test_slang_time_remains_an_inference_and_source_phrase_survives(self):
        await self.say("завтра к шести", {"requested_datetime": ["завтра к шести", "завтра к шести"]},
                       tentative={"time": ["18:00", "к шести"]})
        self.assertNotIn("time", self.facts())
        self.assertEqual(self.agent._state[111]["tentative"]["time"], "18:00")
        for _ in range(8):
            await self.say("Адрес?")
        customer = json.loads(next(m["content"][9:] for m in self.transport.call_args.kwargs["messages"] if m["content"].startswith("CUSTOMER ")))
        self.assertEqual(customer["facts"]["requested_datetime"], "завтра к шести")
        self.assertIn("time", customer["missing"])

    async def test_phone_in_memory_tool_is_not_retransmitted_during_read_cycle(self):
        self.transport.side_effect = [response(calls=[
            ("respond", {"reply": "", "facts": {"phone": ["70001112233", "70001112233"]}}),
            ("get_price_list", {})]), response(calls=[("respond", {"reply": "Подскажу услуги."})])]
        await self.agent.reply(self.user, "70001112233, какие услуги есть?")
        payload = json.dumps(self.transport.call_args.kwargs, ensure_ascii=False)
        self.assertNotIn("70001112233", payload)
        self.assertIn("phone_known", payload)
