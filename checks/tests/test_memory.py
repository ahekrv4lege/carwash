"""Русские реплики и заданные ответы AI: проверка приложения, не понимания живой моделью."""
import json
import asyncio
import re
import time
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from ai import Agent, ChatUser
from conversation import model_state, validate_patch
from tests.fakes import fixture
from tests.test_conversation import ARGS, response


def envelope(reply="Учту.", facts=None, tentative=None):
    return json.dumps(dict(reply=reply, facts=facts or {}, tentative=tentative or {}), ensure_ascii=False)


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recognized_model_allows_explicit_ai_class_resolution_in_proposal(self):
        self.agent._state[111] = {"facts": dict(ARGS, car_model="Toyota Camry"),
            "tentative": {"size_class": "Легковой"}, "catalog": {"size_classes": ["Легковой", "Кроссовер"]}}
        result = await self.agent._call_tool("create_booking", '{"size_class":"Легковой"}', self.user)
        self.assertTrue(result["confirmation_required"])
        self.assertNotIn("size_class", self.agent._state[111].get("tentative", {}))
        self.assertEqual(self.agent._pending[111]["args"]["size_class"], "Легковой")
        text = self.agent._proposal_text(self.agent._pending[111])
        # В сводке — распознанное название, без внутреннего класса прайса.
        self.assertIn("Toyota Camry", text)
        self.assertNotIn("Fixture car", text)
        self.assertNotIn("класс", text)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_full_message_creates_python_proposal(self):
        self.spreadsheet.worksheet("Слоты").values.append(["29.10.2026", "18:00", "1"])
        facts = {"car": ["бив м5", "бив м5"], "car_model": ["BMW M5", "бив м5"],
                 "size_class": ["Легковой", "бив м5"], "requested_service": ["мойка кузова", "МОЙКА кузова"],
                 "client_name": ["Иван", "иван"], "phone": ["89994994944", "89994994944"]}
        self.transport.side_effect = [response(calls=[("get_price_list", {})]),
                                      response(calls=[("respond", {"reply": "Какой у вас автомобиль?"})]),
                                      response(calls=[("respond", {"reply": "", "facts": facts})])]
        with patch.object(self.store, "now", return_value=datetime(2026, 10, 28, 12, tzinfo=ZoneInfo("Europe/Moscow"))):
            answer = await self.agent.reply(self.user, "МОЙКА кузова НА 6 вечера завтра у меня бив м5 иван 89994994944")
        self.assertIn("Предлагаю запись:", answer)
        self.assertIn("29.10.2026", answer)
        self.assertIn("BMW M5", answer)
        self.assertNotIn("Проверю свободное время", answer)
        self.assertIn(111, self.agent._pending)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_prepared_record_claim_uses_time_from_answer_message(self):
        self.spreadsheet.worksheet("Слоты").values.append(["29.10.2026", "19:00", "1"])
        self.agent._state[111] = {"facts": dict(ARGS, car="рено дастер", car_model="Renault Duster",
                                                size_class="Кроссовер", services=["Мойка"], date="2026-10-29")}
        self.transport.side_effect = [response(
            "На 29 октября 19:00 свободно. Подготовлено запись на мойку кузова для Renault Duster, 700 ₽.", raw=True)]
        answer = await self.agent.reply(self.user, "19")
        self.assertIn("Предлагаю запись:", answer)
        self.assertIn("19:00", answer)
        self.assertEqual(self.agent._pending[111]["args"]["time"], "19:00")
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_five_oclock_means_evening_slot_and_night_time_is_rejected(self):
        self.spreadsheet.worksheet("Слоты").values.append(["29.10.2026", "17:00", "1"])
        self.agent._state[111] = {"facts": dict(ARGS, car="рено дастер", car_model="Renault Duster",
                                                size_class="Кроссовер", services=["Мойка"], date="2026-10-29")}
        self.transport.side_effect = [response("Записываю вас в 5.", raw=True)]
        answer = await self.agent.reply(self.user, "дава в 5")
        self.assertIn("Предлагаю запись:", answer)
        self.assertEqual(self.agent._pending[111]["args"]["time"], "17:00")

        self.transport.side_effect = [response("Записываю вас в 21.", raw=True)]
        answer = await self.agent.reply(self.user, "дава в 21")
        self.assertIn("с 9:00 до 19:00", answer)
        self.assertNotIn(111, self.agent._pending)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_claim_after_offered_slots_resolves_evening_time_and_proposes(self):
        self.spreadsheet.worksheet("Слоты").values.append(["29.10.2026", "17:00", "1"])
        self.agent._state[111] = {"facts": dict(ARGS, car="логан", car_model="Renault Logan",
                                                date="2026-10-29", time="18:00"),
                                  "availability": {"days": [{"date": "2026-10-29", "free_times": ["17:00", "19:00"]}]}}
        self.transport.side_effect = [response("Записываю вас на 29 октября, 17:00, Renault Logan, 700 ₽.", raw=True)]
        answer = await self.agent.reply(self.user, "дава в 5 и логан бартік")
        self.assertIn("Предлагаю запись:", answer)
        self.assertIn("17:00", answer)
        self.assertIn("Renault Logan", answer)
        self.assertEqual(self.agent._pending[111]["args"]["time"], "17:00")
        self.assertEqual(self.agent._pending[111]["args"]["car"], "Renault Logan")
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    def test_old_bookings_get_archived_but_fresh_and_cancelled_stay(self):
        sheet = self.spreadsheet.worksheet("Записи")
        sheet.values.append(["1", "27.10.2026 10:00", "27.10.2026", "18:00", "Иван", "89994994944",
                             "Lada Priora", "Легковой", "", "", "", "700", "", "111", "", "Новая"])
        sheet.values.append(["2", "27.10.2026 11:00", "27.10.2026", "12:00", "Иван", "89994994944",
                             "Lada Priora", "Легковой", "", "", "", "700", "", "111", "", "Отмена"])
        sheet.values.append(["3", "01.01.2099 10:00", "01.01.2099", "10:00", "Иван", "89994994944",
                             "BMW M5", "Легковой", "", "", "", "700", "", "111", "", "Новая"])
        self.assertEqual(self.store.archive_old_bookings(), 1)
        values = sheet.values
        self.assertEqual(values[1][-1], "Архив")
        self.assertEqual(values[2][-1], "Отмена")
        self.assertEqual(values[3][-1], "Новая")
        self.assertEqual(self.store.archive_old_bookings(), 0)

    async def test_recognized_car_model_in_proposal_and_sheet(self):
        self.agent._state[111] = {"facts": dict(ARGS, car="бив м5", car_model="BMW M5")}
        result = await self.agent._call_tool("create_booking", "{}", self.user)
        self.assertTrue(result.get("confirmation_required"), result)
        self.assertEqual(self.agent._pending[111]["args"]["car"], "BMW M5")
        text = self.agent._offer(111, self.agent._pending[111])
        self.assertIn("Автомобиль: BMW M5, класс «Легковой»", text)
        self.assertNotIn("бив м5", text)
        self.agent.mark_delivered(111, self.agent.delivery_token(111, text))
        await self.confirm()
        rows = self.spreadsheet.worksheet("Записи").values
        self.assertTrue(any("BMW M5" in row for row in rows))
        self.assertFalse(any("бив м5" in row for row in rows))

    async def test_class_resolution_does_not_bypass_other_uncertainty_or_catalog_validation(self):
        for guesses, supplied in (({"size_class": "Легковой", "car": "Toyota Camry"}, "Легковой"),
                                  ({"size_class": "Легковой", "car_model": "Toyota Camry"}, "Легковой"),
                                  ({"size_class": "Легковой", "phone": ARGS["phone"]}, "Легковой"),
                                  ({"size_class": "Легковой"}, "Кроссовер"),
                                  ({"size_class": "Unknown"}, "Unknown")):
            with self.subTest(guesses=guesses, supplied=supplied):
                self.agent.reset(111)
                self.agent._state[111] = {"facts": dict(ARGS, car_model="Toyota Camry"),
                    "tentative": guesses, "catalog": {"size_classes": ["Легковой", "Кроссовер"]}}
                result = await self.agent._call_tool("create_booking", json.dumps({"size_class": supplied}), self.user)
                self.assertFalse(result["ok"])
                self.assertNotIn(111, self.agent._pending)
                self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_tentative_class_gets_actionable_memory_repair_before_proposal(self):
        self.agent._state[111] = {"facts": {field: value for field, value in ARGS.items() if field != "size_class"},
            "tentative": {"size_class": "Легковой"}, "catalog": {"size_classes": ["Легковой", "Кроссовер"]}}
        self.transport.side_effect = [response(calls=[("create_booking", {"size_class": "Легковой"})]),
            response(calls=[("respond", {"reply": "Проверим цену и время.",
                "facts": {"size_class": ["Легковой", ARGS["car"]]}})])]
        answer = await self.agent.reply(self.user, "Подготовьте запись")
        self.assertIn("Предлагаю запись:", answer)
        self.assertEqual(self.facts()["size_class"], "Легковой")
        self.assertNotIn("size_class", self.agent._state[111].get("tentative", {}))
        self.assertEqual(self.transport.await_count, 2)
        self.assertEqual(self.transport.call_args.kwargs["tool_choice"]["function"]["name"], "respond")
        messages = self.transport.call_args.kwargs["messages"]
        self.assertTrue(any("Класс пока предположительный" in message.get("content", "") for message in messages))
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_vehicle_in_price_question_uses_memory_reply_after_catalog_without_extra_request(self):
        self.transport.side_effect = [response(calls=[("get_price_list", {})]), response(calls=[("respond", {
            "reply": "Для него есть Базовый.", "facts": {"car": ["лансера девять", "лансера девять"]}})])]
        answer = await self.agent.reply(self.user, "Что есть для лансера девять?")
        self.assertEqual(answer, "Для него есть Базовый.")
        self.assertEqual(self.facts()["car"], "лансера девять")
        self.assertEqual(self.transport.await_count, 2)
        self.assertEqual(self.transport.call_args.kwargs["tool_choice"]["function"]["name"], "respond")
        self.assertEqual([tool["function"]["name"] for tool in self.transport.call_args.kwargs["tools"]], ["respond"])

    async def test_explicit_tariff_change_is_saved_even_when_ai_only_calculates(self):
        self.spreadsheet.worksheet("Тарифы").values.append(["Стандарт", "Мойка", "0", "да"])
        self.agent._state[111] = {"facts": dict(ARGS)}
        self.transport.side_effect = [response(calls=[("calculate_price", {"size_class": "Легковой", "tariff": "Стандарт", "services": []})]),
                                      response("Стандарт стоит 700 ₽.", raw=True)]
        await self.agent.reply(self.user, "Передумал, вместо Базового Стандарт.")
        self.assertEqual(self.facts()["tariff"], "Стандарт")
        self.assertEqual(self.agent._state[111]["quote"]["total"], 700)
        self.assertNotIn(111, self.agent._pending)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_hypothetical_tariff_quote_does_not_change_customer_choice(self):
        self.spreadsheet.worksheet("Тарифы").values.append(["Стандарт", "Мойка", "0", "да"])
        for source in ("А если вместо Базового Стандарт, сколько будет?", "Сравните Базовый и Стандарт.", "Не выбираю Стандарт, просто посчитайте."):
            with self.subTest(source=source):
                self.agent.reset(111)
                self.agent._state[111] = {"facts": dict(ARGS)}
                self.transport.side_effect = [response(calls=[("calculate_price", {"size_class": "Легковой", "tariff": "Стандарт", "services": []})]),
                                              response("Для сравнения: 700 ₽.", raw=True)]
                await self.agent.reply(self.user, source)
                self.assertEqual(self.facts()["tariff"], ARGS["tariff"])
                self.assertNotIn(111, self.agent._pending)

    async def test_explicit_prepare_after_hypothetical_quote_works_without_ai_tool_call(self):
        self.agent._state[111] = {"facts": dict(ARGS),
            "selection": {"size_class": "Легковой", "tariff": "Другой", "services": []}, "quote": {"total": 900}}
        self.transport.side_effect = [response("Хорошо, подготовим.", raw=True)]
        answer = await self.agent.reply(self.user, "Оставляем Базовый, без дополнительных услуг. Подготовьте запись, я проверю.")
        self.assertIn("Предлагаю запись:", answer)
        self.assertEqual(self.agent._pending[111]["quote"]["total"], 630)
        self.assertEqual(self.transport.await_count, 1)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)
        self.agent.mark_delivered(111, self.agent.delivery_token(111, answer))
        confirmed = await self.confirm()
        self.assertIn("10:00", confirmed)
        self.assertEqual(len(self.spreadsheet.worksheet("Записи").writes), 1)
        await self.confirm()
        self.assertEqual(len(self.spreadsheet.worksheet("Записи").writes), 1)

    async def test_automatic_proposal_requires_explicit_unconditional_request(self):
        for text in ("Если подготовьте запись — это команда?", "Не подготовьте запись, пока только смотрю.",
                     "Можно ли подготовить запись?", "Подготовьте запись или просто расчёт, я не решил."):
            with self.subTest(text=text):
                self.agent.reset(111)
                self.agent._state[111] = {"facts": dict(ARGS)}
                self.transport.side_effect = [response("Уточним пожелания.", raw=True)]
                await self.agent.reply(self.user, text)
                self.assertNotIn(111, self.agent._pending)
                self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_repeated_catalog_result_has_only_one_full_copy_in_model_context(self):
        self.transport.side_effect = [response(calls=[("get_price_list", {})]),
            response(calls=[("get_price_list", {})]), response("Цена 700 ₽.", raw=True)]
        with patch.object(self.agent, "_call_tool", wraps=self.agent._call_tool) as tool:
            answer = await self.agent.reply(self.user, "Какие услуги есть?")
        results = [json.loads(message["content"]) for message in self.transport.call_args.kwargs["messages"] if message["role"] == "tool"]
        self.assertEqual(answer, "Цена 700 ₽.")
        self.assertEqual(tool.call_count, 1)
        self.assertIn("services", results[0])
        self.assertIn("tariffs", results[0])
        self.assertTrue(results[1]["cached"])
        self.assertNotIn("services", results[1])
        self.assertLess(len(json.dumps(results[1])), len(json.dumps(results[0])))

    async def test_different_catalog_filters_keep_both_full_results(self):
        self.transport.side_effect = [response(calls=[("get_price_list", {"size_class": "Легковой"})]),
            response(calls=[("get_price_list", {"size_class": "Кроссовер"})]), response("Цены разные.", raw=True)]
        await self.agent.reply(self.user, "Сравните цены для легкового и кроссовера.")
        results = [json.loads(message["content"]) for message in self.transport.call_args.kwargs["messages"] if message["role"] == "tool"]
        self.assertEqual([result["selected_class"] for result in results], ["Легковой", "Кроссовер"])
        self.assertTrue(all("services" in result and not result.get("cached") for result in results))

    async def test_explicit_return_to_old_choice_discards_different_hypothetical_quote(self):
        self.agent._state[111] = {"facts": dict(ARGS),
            "selection": {"size_class": "Легковой", "tariff": "Другой", "services": []}, "quote": {"total": 900}}
        await self.say("Оставляем Базовый.", facts={"tariff": ["Базовый", "Базовый"]})
        self.assertNotIn("quote", self.agent._state[111])
        self.assertNotIn("selection", self.agent._state[111])
        self.assertEqual(self.agent._booking_args({}, self.user)["tariff"], "Базовый")

    async def test_phone_is_kept_before_an_ordinary_reply_redacts_the_history(self):
        self.transport.side_effect = [response("Уточните модель автомобиля.", raw=True)]
        await self.agent.reply(self.user, "Мой телефон +7 000 111-22-33.")
        self.assertEqual(self.facts()["phone"], "+7 000 111-22-33")
        self.assertNotIn("111-22-33", self.agent._history[111][0]["content"])
        self.assertEqual(self.transport.await_count, 1)

    async def test_two_phones_negation_and_order_id_are_not_auto_selected(self):
        for text in ("Телефон +7 000 111-22-33 или +7 000 222-33-44", "Не телефон 70001112233", "Заказ 70001112233", "2026-10-26 11:00", "Вроде телефон +7 000 111-22-33"):
            with self.subTest(text=text):
                self.agent.reset(self.user.id)
                self.transport.side_effect = [response("Уточните номер.", raw=True)]
                await self.agent.reply(self.user, text)
                self.assertNotIn("phone", self.facts())

    async def test_unambiguous_booking_datetime_is_kept_without_extra_ai_call(self):
        now = datetime(2026, 10, 25, 12)
        self.transport.side_effect = [response("Какой тариф вам нужен?", raw=True)]
        with patch.object(self.store, "now", return_value=now):
            await self.agent.reply(self.user, "Можно завтра в 11?")
        self.assertEqual(self.facts()["date"], "2026-10-26")
        self.assertEqual(self.facts()["time"], "11:00")
        self.assertEqual(self.transport.await_count, 1)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_hypothetical_negative_and_alternative_times_are_not_auto_selected(self):
        for text in ("Хочу завтра или послезавтра", "Если можно завтра в 11", "Не хочу завтра в 11", "Можно в 11 или 12?", "Возможно хочу завтра в 11"):
            with self.subTest(text=text):
                self.agent.reset(self.user.id)
                self.transport.side_effect = [response("Уточните время.", raw=True)]
                await self.agent.reply(self.user, text)
                self.assertNotIn("date", self.facts())
                self.assertNotIn("time", self.facts())

    async def test_booking_question_does_not_rewrite_confirmed_datetime(self):
        self.agent._state[111] = {"facts": dict(ARGS), "stage": "booked", "confirmed": True,
            "booking": {"booking_id": "1", "date": ARGS["date"], "time": "10:00", "car": ARGS["car"], "total": 630, "currency": "₽"}}
        self.transport.side_effect = [response("У вас запись на 10:00.", raw=True)]
        await self.agent.reply(self.user, "Можно завтра в 11 или у меня уже есть запись?")
        self.assertEqual(self.facts()["date"], ARGS["date"])
        self.assertEqual(self.facts()["time"], "10:00")

    async def test_confirmed_total_question_does_not_require_new_catalog(self):
        self.agent._state[111] = {"facts": dict(ARGS)}
        await self.say("Подготовьте запись", calls=[("create_booking", {})])
        await self.confirm()
        self.transport.side_effect = [response("Итого 630 ₽, запись на 10:00.", raw=True)]
        before = self.transport.await_count
        answer = await self.agent.reply(self.user, "Напомните итог, я забыл цену.")
        self.assertIn("630", answer)
        self.assertEqual(self.transport.await_count, before + 1)
        self.assertEqual(self.transport.call_args.kwargs["tool_choice"], "auto")

    async def test_sheet_read_failure_returns_without_another_ai_request(self):
        with patch.object(self.store, "price_list", side_effect=OSError("offline")) as read:
            self.transport.side_effect = [response(calls=[("get_price_list", {})]),
                response(calls=[("respond", {"reply": "Прайс временно недоступен. Попробуйте позже."})])]
            answer = await self.agent.reply(self.user, "Какие услуги и цены?")
        self.assertIn("Не удалось прочитать таблицу", answer)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.transport.await_count, 1)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_adding_extra_preserves_already_selected_tariff(self):
        self.agent._state[111] = {"facts": dict(ARGS)}
        self.spreadsheet.worksheet("Услуги").values[2][-1] = "да"
        await self.say("Добавьте Воск.", facts={"services": [["Воск"], "Воск"]})
        self.assertEqual(self.facts()["tariff"], "Базовый")
        self.assertEqual(self.facts()["services"], ["Воск"])

    async def test_preview_cannot_silently_use_old_choice_after_different_quote(self):
        self.agent._state[111] = {"facts": dict(ARGS),
            "selection": {"size_class": "Легковой", "tariff": "Другой", "services": []}, "quote": {"total": 900}}
        result = await self.agent._call_tool("create_booking", "{}", self.user)
        self.assertFalse(result["ok"])
        self.assertEqual(result["field"], "tariff")
        self.assertIn("Уточните выбор", result["error"])
        self.assertNotIn(111, self.agent._pending)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_revision_schema_allows_known_fields_without_changing_facts(self):
        self.agent._state[111] = {"facts": dict(ARGS), "stage": "collecting", "quote": {"total": 630}}
        schema = next(tool["function"]["parameters"] for tool in self.agent._tools(self.user)
                      if tool["function"]["name"] == "create_booking")
        self.assertIn("time", schema["properties"])
        self.assertIn("car", schema["properties"])
        self.assertEqual(schema["required"], [])
        self.assertEqual(self.facts()["time"], "10:00")

    async def test_conflicting_time_is_repaired_from_customer_quote_before_proposal(self):
        self.agent._state[111] = {"facts": dict(ARGS), "stage": "collecting", "quote": {"total": 630}}
        self.spreadsheet.worksheet("Слоты").values.append([ARGS["date"], "12:00", "1"])
        self.transport.side_effect = [response(calls=[("create_booking", {"time": "12:00"})]),
            response(calls=[("respond", {"reply": "Проверю новое время.",
                "facts": {"time": ["12:00", "12:00"]}})])]
        answer = await self.agent.reply(self.user, "Да, но давайте в 12:00 вместо 10:00.")
        self.assertIn("Время: 12:00", answer)
        self.assertEqual(self.agent._pending[111]["args"]["time"], "12:00")
        self.assertEqual(self.transport.await_count, 2)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_invented_class_without_catalog_routes_repair_to_catalog(self):
        self.agent._state[111] = {"facts": {key: value for key, value in ARGS.items() if key != "size_class"}}
        self.transport.side_effect = [response(calls=[("create_booking", {"size_class": "compact"})]),
            response(calls=[("get_price_list", {})]), response(calls=[("create_booking", {"size_class": "Легковой"})])]
        answer = await self.agent.reply(self.user, "Подготовьте запись")
        self.assertIn("Предлагаю запись:", answer)
        repair = self.transport.await_args_list[1].kwargs
        self.assertEqual(repair["tool_choice"]["function"]["name"], "get_price_list")
        self.assertEqual([tool["function"]["name"] for tool in repair["tools"]], ["get_price_list"])
        self.assertTrue(any("Выберите size_class" in message.get("content", "") for message in repair["messages"]))
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_first_price_question_requires_real_catalog_with_one_schema(self):
        self.transport.side_effect = [response(calls=[("get_price_list", {})]), response("Есть мойка.", raw=True)]
        await self.agent.reply(self.user, "Что у вас есть и сколько стоит?")
        first, following = [call.kwargs for call in self.transport.await_args_list]
        self.assertEqual(first["tool_choice"]["function"]["name"], "get_price_list")
        self.assertEqual([tool["function"]["name"] for tool in first["tools"]], ["get_price_list"])
        # После чтения прайса без известного авто модель обязана ответить клиенту.
        self.assertEqual(following["tool_choice"]["function"]["name"], "respond")
        self.assertTrue(self.agent._state[111]["catalog"])

    async def test_provider_cannot_bypass_required_catalog_with_invented_services(self):
        self.transport.side_effect = [response("У нас есть химчистка за 100 рублей.", raw=True)]
        answer = await self.agent.reply(self.user, "Какие услуги есть?")
        self.assertNotIn("химчистка", answer)
        self.assertNotIn("100 рублей", answer)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_missing_phone_forces_question_instead_of_repeated_booking_calls(self):
        self.agent._state[111] = {"facts": {key: value for key, value in ARGS.items() if key != "phone"}}
        self.transport.side_effect = [response(calls=[("create_booking", {})]),
                                     response(calls=[("respond", {"reply": "Подскажите телефон."})])]
        answer = await self.agent.reply(self.user, "Подготовьте запись")
        self.assertEqual(answer, "Подскажите телефон.")
        self.assertEqual(self.transport.await_count, 2)
        self.assertEqual(self.transport.call_args.kwargs["tool_choice"]["function"]["name"], "respond")
        self.assertNotIn(111, self.agent._pending)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_phone_repair_can_complete_proposal_without_another_ai_request(self):
        self.agent._state[111] = {"facts": {key: value for key, value in ARGS.items() if key != "phone"}}
        self.transport.side_effect = [response(calls=[("create_booking", {})]), response(calls=[("respond", {
            "reply": "Проверим запись.", "facts": {"phone": [ARGS["phone"], ARGS["phone"]]}})])]
        answer = await self.agent.reply(self.user, "Телефон " + ARGS["phone"])
        self.assertIn("Предлагаю запись:", answer)
        # Телефон распознан до запроса к модели: достаточно одного вызова.
        self.assertEqual(self.transport.await_count, 1)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_confirmed_booking_history_keeps_time_and_price_not_internal_instructions(self):
        self.agent._state[111] = {"facts": dict(ARGS)}
        await self.say("Подготовьте запись", calls=[("create_booking", {})])
        answer = await self.confirm()
        saved = self.agent._history[111][-1]["content"]
        self.assertEqual(saved, answer)
        self.assertIn("10:00", saved)
        self.assertIn("630", saved)
        self.assertNotIn("STATE", saved)
        self.assertNotIn("Python", saved)

    def test_booking_schema_requires_only_details_not_already_saved(self):
        from ai import SCHEMAS
        def schema():
            return next(tool["function"]["parameters"] for tool in self.agent._tools(self.user)
                        if tool["function"]["name"] == "create_booking")
        self.agent._state[111] = {"facts": {key: value for key, value in ARGS.items() if key != "phone"}}
        self.assertEqual(schema()["required"], ["phone"])
        self.assertIn("phone", schema()["properties"])
        self.assertNotIn("car", schema()["properties"])
        self.agent._state[111]["facts"]["phone"] = ARGS["phone"]
        self.assertEqual(schema()["required"], [])
        self.agent._state[111]["facts"].clear()
        self.assertEqual(set(schema()["required"]), {"date", "time", "client_name", "phone", "car", "size_class"})
        self.assertEqual(SCHEMAS["create_booking"]["required"], [])

    async def test_memory_repair_sends_only_the_forced_tool_schema(self):
        self.transport.side_effect = [response(calls=[("respond", "{broken")]),
            response(calls=[("respond", {"reply": "Какой автомобиль?", "facts": {
                "client_name": ["Никита", "Никита"]}})])]
        answer = await self.agent.reply(self.user, "Никита")
        self.assertEqual(answer, "Какой автомобиль?")
        self.assertEqual(self.facts()["client_name"], "Никита")
        first, repaired = [call.kwargs for call in self.transport.await_args_list]
        self.assertEqual(len(first["tools"]), 8)
        self.assertEqual([tool["function"]["name"] for tool in repaired["tools"]], ["respond"])
        self.assertEqual(repaired["tool_choice"]["function"]["name"], "respond")

    async def test_booking_reports_schema_fields_and_literal_source_without_writing(self):
        self.agent._state[111] = {"facts": {key: value for key, value in ARGS.items() if key != "car"}}
        self.agent._turn_text[111] = "Для лансера девять"
        result = await self.agent._call_tool("create_booking", '{"car":"Mitsubishi Lancer IX"}', self.user)
        self.assertFalse(result["ok"])
        self.assertEqual(result["field"], "car")
        result = await self.agent._call_tool("create_booking", '{"requested_datetime":"завтра"}', self.user)
        self.assertFalse(result["ok"])
        self.assertIn("Разрешены только:", result["error"])
        self.assertNotIn(111, self.agent._pending)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_date_and_time_keep_an_unambiguous_sentence_as_source(self):
        now = datetime(2026, 10, 25, 12)
        with patch.object(self.store, "now", return_value=now):
            await self.say("Можно завтра в 11?", facts={
                "date": ["2026-10-26", "Можно завтра в 11?"],
                "time": ["11:00", "Можно завтра в 11?"],
            })
        self.assertEqual(self.facts()["date"], "2026-10-26")
        self.assertEqual(self.facts()["time"], "11:00")

    def test_compound_datetime_quote_does_not_choose_between_two_options(self):
        for field, value, text in [("date", "2026-10-26", "завтра или послезавтра"),
                                   ("time", "11:00", "в 11 или в 12")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_patch({field: [value, text]}, text, datetime(2026, 10, 25, 12))

    def test_booking_accepts_catalog_class_for_grounded_car_without_memory_echo(self):
        facts = {name: value for name, value in ARGS.items() if name != "size_class"}
        self.agent._state[111] = {"facts": facts, "catalog": {"size_classes": ["Легковой", "Кроссовер"]}}
        self.agent._turn_text[111] = "Подготовьте запись"
        self.assertEqual(self.agent._booking_args({"size_class": "Легковой"}, self.user)["size_class"], "Легковой")
        with self.assertRaises(ValueError):
            self.agent._booking_args({"size_class": "Компакт"}, self.user)

    async def test_booking_ignores_redundant_tariff_components_from_verified_quote(self):
        facts = dict(ARGS, tariff="Базовый", services=[])
        self.agent._state[111] = {"facts": facts}
        quote = await self.agent._call_tool("calculate_price", '{"size_class":"Легковой","tariff":"Базовый"}', self.user)
        self.assertTrue(quote["ok"])
        arguments = self.agent._booking_args({"services": ["Мойка"]}, self.user)
        self.assertEqual(arguments["services"], [])
        self.assertEqual(arguments["tariff"], "Базовый")
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)
        with self.assertRaises(ValueError):
            self.agent._booking_args({"services": ["Выдуманная"]}, self.user)

    async def test_recognized_car_metadata_never_replaces_booking_car(self):
        self.agent._state[111] = {"facts": dict(ARGS)}
        result = await self.agent._call_tool("create_booking", json.dumps({"car_model": "Other model"}), self.user)
        self.assertTrue(result.get("confirmation_required"), result)
        self.assertEqual(self.agent._pending[111]["args"]["car"], ARGS["car"])
        self.assertNotIn("car_model", self.agent._pending[111]["args"])
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)
        self.agent._state[111]["facts"].pop("car")
        rejected = await self.agent._call_tool("create_booking", json.dumps({"car_model": "Toyota Camry"}), self.user)
        self.assertFalse(rejected["ok"])
        rejected = await self.agent._call_tool("create_booking", '{"requested_datetime":"tomorrow"}', self.user)
        self.assertFalse(rejected["ok"])

    async def test_summary_and_sheet_show_recognized_car_not_customer_typos(self):
        # «бив м5» — слова клиента; сводка и таблица получают распознанное «BMW M5».
        self.agent._state[111] = {"facts": dict(ARGS, car="бив м5", car_model="BMW M5")}
        text = await self.say("Записывайте, пожалуйста", calls=[("create_booking", {})])
        self.assertIn("BMW M5", text)
        self.assertNotIn("бив м5", text)
        self.assertNotIn("класс", text)
        answer = await self.confirm()
        self.assertIn("Записала", answer)
        created = self.spreadsheet.worksheet("Записи").values[-1]
        self.assertIn("BMW M5", created)
        self.assertNotIn("бив м5", str(created))

    async def test_repaired_class_completes_existing_proposal_without_another_ai_call(self):
        self.agent._state[111] = {"facts": {key: value for key, value in ARGS.items() if key != "size_class"},
                                  "tentative": {"size_class": "Легковой"}}
        self.transport.side_effect = [response(calls=[("create_booking", {"size_class": "Легковой"})]),
            response(calls=[("respond", {"reply": "Всё верно?", "facts": {
                "size_class": ["Легковой", ARGS["car"]]}})])]
        answer = await self.agent.reply(self.user, "Подготовьте запись, телефон " + ARGS["phone"])
        self.assertIn("Предлагаю запись:", answer)
        self.assertEqual(self.transport.await_count, 2)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)
        self.agent.mark_delivered(111, self.agent.delivery_token(111))
        await self.confirm()
        self.assertEqual(len(self.store.my_bookings(111)["bookings"]), 1)
        await self.confirm()
        self.assertEqual(len(self.store.my_bookings(111)["bookings"]), 1)

    def test_inferred_class_requires_current_known_car_and_real_catalog(self):
        catalog = self.store.get_catalog()
        for value, quote in (("Легковой", "Previous car"), ("Выдуманный класс", ARGS["car"])):
            with self.subTest(value=value, quote=quote), self.assertRaises(ValueError):
                validate_patch({"size_class": [value, quote]}, "Мой телефон", self.store.now(),
                               catalog, known={"car": ARGS["car"]})
        with self.assertRaises(ValueError):
            validate_patch({"date": ["2099-01-01", "2099-01-01"]}, "Мой телефон", self.store.now(),
                           catalog, known={"car": ARGS["car"]})

    async def test_proposal_retry_keeps_uncertain_class_blocked(self):
        self.agent._state[111] = {"facts": dict(ARGS), "tentative": {"size_class": "Легковой"}}
        phone = "00011122333"
        self.transport.side_effect = [response(calls=[("create_booking", {})]),
            response(calls=[("respond", {"reply": "Уточним автомобиль.", "facts": {"phone": [phone, phone]}})])]
        await self.agent.reply(self.user, phone)
        self.assertNotIn(111, self.agent._pending)
        self.assertEqual(self.transport.await_count, 2)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_booking_does_not_drop_explicit_services_or_use_stale_tariff(self):
        facts = dict(ARGS, tariff="Базовый", services=["Мойка"])
        self.agent._state[111] = {"facts": facts}
        await self.agent._call_tool("calculate_price", '{"size_class":"Легковой","tariff":"Базовый"}', self.user)
        self.assertEqual(self.agent._booking_args({"services": ["Мойка"]}, self.user)["services"], ["Мойка"])
        facts["services"] = []
        self.agent._state[111]["quote"]["stale"] = True
        with self.assertRaises(ValueError):
            self.agent._booking_args({"services": ["Мойка"]}, self.user)

    async def test_inflected_vehicle_quote_keeps_customer_spelling(self):
        await self.say("Что есть для лансера девять?", facts={
            "car": ["лансер девять", "лансер девять"],
            "car_model": ["Mitsubishi Lancer IX", "лансер девять"],
        })
        self.assertEqual(self.facts()["car"], "лансера девять")
        self.assertEqual(self.facts()["car_model"], "Mitsubishi Lancer IX")
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_ambiguous_inflected_vehicle_quote_is_not_guessed(self):
        await self.say("Для лансера девять или лансеру девять?", facts={
            "car": ["лансер девять", "лансер девять"],
            "car_model": ["Mitsubishi Lancer IX", "лансер девять"],
        })
        self.assertNotIn("car", self.facts())
        self.assertNotIn("car_model", self.facts())

    async def test_vehicle_quote_repair_does_not_accept_unrelated_phone(self):
        await self.say("Для лансера девять", facts={
            "car": ["лансер девять", "лансер девять"],
            "phone": ["+79991234567", "лансер девять"],
        })
        self.assertEqual(self.facts()["car"], "лансера девять")
        self.assertNotIn("phone", self.facts())

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
        self.agent.mark_delivered(111, self.agent.delivery_token(111, answer))
        return answer

    async def confirm(self):
        self.turns += 1
        offer_id = self.agent._pending.get(111, {}).get("offer_id", "missing")
        answer = await self.agent.proposal_action(self.user, offer_id, "confirm")
        self.agent.mark_delivered(111, self.agent.delivery_token(111, answer))
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
        # Смена даты больше не сбрасывает время: доступность проверит create_booking.
        self.assertEqual(self.facts().get("time"), "11:00")
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
        for value in ("Артём", "Toyota Corolla 1.6", "+7 000 222-33-44", "02.01.2099", "12:00", "700"):
            self.assertIn(value, proposal)
        self.assertNotIn("Пылесос", proposal)
        self.assertNotIn("Базовый", proposal)
        self.assertEqual(sum(len(s.writes) for s in self.spreadsheet.sheets), 0)
        with patch.object(self.store, "create_booking", wraps=self.store.create_booking) as write:
            result = await self.confirm()
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

    async def test_fabricated_fields_are_rejected_without_discarding_independent_valid_facts(self):
        for facts in ({"car": ["Toyota Camry", "привет"]}, {"phone": ["70009990000", "привет"]},
                      {"confirmed": [True, "привет"]}, {"client_name": ["Антон", "Антон"]}):
            await self.say("привет", facts)
            self.assertFalse(self.facts())
        await self.say("Антон", {"client_name": ["Антон", "Антон"], "car": ["Toyota", "не было"]})
        self.assertEqual(self.facts(), {"client_name": "Антон"})
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

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
        self.assertIn("Как вас зовут?", answer)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.transport.call_count, 2)

    async def test_success_claims_are_blocked_in_plain_responses(self):
        claims = ["Вы записаны.", "Бронирование создано.", "Запись отменена.", "Записала вас на завтра.",
                  "Я оформила запись.", "Место забронировано.", "Уже записан!", "Запись успешно создана.",
                  "Я отменила вашу запись."]
        for text in claims:
            answer = await self.say("Оформить?", reply=text)
            self.assertNotEqual(answer, text, text)
            self.assertFalse(re.search(r"запис(?:ал|ан)|забронир|отменен|оформлен", answer, re.I), text)
        self.assertEqual(sum(len(s.writes) for s in self.spreadsheet.sheets), 0)

    async def test_plain_response_needs_one_request_and_keeps_customer_words(self):
        self.transport.side_effect = [response("Учту вашу Toyota.", raw=True)]
        answer = await self.agent.reply(self.user, "Toyota Camry 3.5")
        self.assertEqual(answer, "Учту вашу Toyota.")
        self.assertFalse(self.facts())
        self.assertEqual(self.transport.call_count, 1)
        for _ in range(5):
            await self.say("Адрес?", reply="Подскажу адрес.")
        self.assertIn("Toyota Camry 3.5", self.agent._state[self.user.id]["context"])
        self.assertEqual(self.transport.call_count, 6)

    async def test_plain_answer_does_not_start_an_extra_memory_request(self):
        self.transport.side_effect = [response("Какой автомобиль?", raw=True)]
        answer = await self.agent.reply(self.user, "Никита")
        self.assertEqual(answer, "Какой автомобиль?")
        self.assertFalse(self.facts())
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(self.agent._history[self.user.id][0]["content"], "Никита")

    async def test_production_requests_allow_plain_text_and_explain_optional_memory(self):
        async def reply_from_contract(**payload):
            self.assertEqual(payload["tool_choice"], "auto")
            prompt = payload["messages"][0]["content"]
            # Промпт нормализован plain_text: прямые кавычки и обычные дефисы.
            self.assertIn("Нет новых данных - обычный текст", prompt)
            self.assertIn("Есть данные - respond", prompt)
            self.assertIn("точная цитата текущего сообщения", prompt)
            example = prompt.split('Пример "у мя раф 4": ', 1)[1].splitlines()[0]
            memory = json.loads(example.removesuffix("."))
            self.assertEqual(memory["facts"]["car"], ["раф 4", "раф 4"])
            self.assertEqual(memory["facts"]["car_model"], ["Toyota RAV4", "раф 4"])
            self.assertIn("respond", [tool["function"]["name"] for tool in payload["tools"]])
            return response(calls=[("respond", memory)])

        self.transport.side_effect = reply_from_contract
        answer = await self.agent.reply(self.user, "у мя раф 4")
        self.assertEqual(answer, "Какую мойку хотите?")
        self.assertEqual(self.facts()["car"], "раф 4")
        self.assertEqual(self.facts()["car_model"], "Toyota RAV4")
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

    async def test_invalid_catalog_memory_does_not_drop_the_valid_car_or_repeat_ai(self):
        invalid = response(calls=[("respond", {"reply": "Класс D.", "facts": {
            "car": ["Toyota Camry", "Toyota Camry"], "size_class": ["D", "Toyota Camry"]}})])
        self.transport.side_effect = [invalid, invalid]
        answer = await self.agent.reply(self.user, "Toyota Camry")
        self.assertTrue(answer)
        self.assertEqual(self.facts(), {"car": "Toyota Camry"})
        self.assertEqual(self.transport.call_count, 1)

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

    async def test_unchanged_facts_need_no_new_quote_or_repair_request(self):
        await self.say("Никита", {"client_name": ["Никита", "Никита"]})
        self.transport.side_effect = [response(calls=[("respond", {"reply": "Учту.", "facts": {
            "client_name": ["Никита", "Никита"], "car": ["Toyota Camry", "Toyota Camry"]}})]),
            response(calls=[("respond", {"reply": "Какой класс?", "facts": {
                "car": ["Toyota Camry", "Toyota Camry"]}})])]
        answer = await self.agent.reply(self.user, "у меня Toyota Camry")
        self.assertEqual(answer, "Учту.")
        self.assertEqual(self.facts(), {"client_name": "Никита", "car": "Toyota Camry"})
        self.assertEqual(self.transport.call_count, 2)  # имя и автомобиль, без дополнительного запроса

    async def test_unchanged_tentative_fields_do_not_block_a_price_reply(self):
        await self.say("lancer 9", {"car": ["lancer 9", "lancer 9"]},
                       {"size_class": ["Легковой", "lancer 9"]})
        self.transport.side_effect = [response(calls=[("respond", {"reply": "Какая услуга нужна?", "facts": {
            "car": ["lancer 9", "lancer 9"]}, "tentative": {"size_class": ["Легковой", "lancer 9"]}})])]
        answer = await self.agent.reply(self.user, "какие услуги есть?")
        self.assertEqual(answer, "Какая услуга нужна?")
        self.assertEqual(self.facts(), {"car": "lancer 9"})
        self.assertEqual(self.agent._state[self.user.id]["tentative"]["size_class"], "Легковой")

    async def test_unchanged_fresh_fact_still_blocks_an_earlier_conflicting_quote(self):
        await self.say("Новая машина", {"car": ["Новая машина", "Новая машина"]})
        self.transport.side_effect = [response(calls=[
            ("respond", {"reply": "", "facts": {"car": ["Новая машина", "оставляю Новая машина"]}}),
            ("get_price_list", {})]), response(calls=[
                ("respond", {"reply": "Учту.", "facts": {"car": ["Старая машина", "Старая машина"]}})])]
        await self.agent.reply(self.user, "Старая машина, но оставляю Новая машина")
        self.assertEqual(self.facts()["car"], "Новая машина")

    async def test_bare_null_means_no_update_but_sourced_null_still_clears_a_fact(self):
        await self.say("Никита", {"client_name": ["Никита", "Никита"]})
        await self.say("лансер девятый", {"client_name": None, "car": ["лансер девятый", "лансер девятый"]})
        self.assertEqual(self.facts(), {"client_name": "Никита", "car": "лансер девятый"})
        await self.say("лансер 9", {"client_name": [None, None], "car": ["лансер 9", "лансер 9"]})
        self.assertEqual(self.facts()["client_name"], "Никита")
        await self.say("Уберите имя", {"client_name": [None, "Уберите имя"]})
        self.assertNotIn("client_name", self.facts())
        self.assertIn("client_name", self.agent._state[self.user.id]["invalidated"])

    async def test_single_service_string_is_normalized_without_accepting_unknown_catalog_values(self):
        await self.say("Мойка", {"services": ["Мойка", "Мойка"], "tariff": [None, None]})
        self.assertEqual(self.facts()["services"], ["Мойка"])
        await self.say("Неизвестная услуга", {"services": ["Неизвестная услуга", "Неизвестная услуга"]})
        self.assertEqual(self.facts()["services"], ["Мойка"])

    async def test_optional_null_tool_fields_are_omitted_but_required_and_unknown_fields_stay_checked(self):
        result = await self.agent._call_tool("get_price_list", '{"size_class":null}', self.user)
        self.assertTrue(result["ok"])
        for name, arguments in (("cancel_booking", '{"booking_id":null}'),
                                ("get_price_list", '{"unknown":null}')):
            result = await self.agent._call_tool(name, arguments, self.user)
            self.assertFalse(result["ok"])

    async def test_single_wrapped_service_pair_still_requires_customer_source(self):
        await self.say("Мойка", {"services": [["Мойка", "Мойка"]]})
        self.assertEqual(self.facts()["services"], ["Мойка"])
        await self.say("11:00", {"services": [["Мойка", "Мойка"]], "time": ["11:00", "11:00"]})
        self.assertEqual(self.facts()["time"], "11:00")
        self.assertEqual(self.facts()["services"], ["Мойка"])
        await self.say("Что дальше?", {"services": [["Воск", "Воск"]]})
        self.assertEqual(self.facts()["services"], ["Мойка"])

    async def test_null_price_argument_cache_uses_the_actual_customer_class(self):
        self.agent._state[self.user.id]["facts"] = {"size_class": "Легковой"}
        first = self.agent._normalized_arguments("calculate_price", '{"size_class":null,"services":["Мойка"]}', self.user)
        self.assertEqual(json.loads(first)["size_class"], "легковой")
        self.agent._state[self.user.id]["facts"]["size_class"] = "Кроссовер"
        second = self.agent._normalized_arguments("calculate_price", '{"size_class":null,"services":["Мойка"]}', self.user)
        self.assertEqual(json.loads(second)["size_class"], "кроссовер")
        self.assertNotEqual(first, second)

    async def test_inferred_options_need_no_customer_quote_but_cannot_authorize_booking(self):
        await self.say("Какие есть варианты?", tentative={
            "date": ["2099-01-02", "из расписания"], "time": ["11:00", None],
            "size_class": ["Седан", "по модели автомобиля"]})
        self.assertFalse(self.facts())
        self.assertEqual(self.agent._state[self.user.id]["tentative"]["time"], "11:00")
        with self.assertRaisesRegex(ValueError, "tentative_choice_requires_clarification"):
            self.agent._booking_args(ARGS, self.user)
        await self.say("2099-01-02 в 11:00, класс Легковой", facts={
            "date": ["2099-01-02", "2099-01-02"], "time": ["11:00", "11:00"],
            "size_class": ["Легковой", "Легковой"]})
        self.assertFalse(self.agent._state[self.user.id].get("tentative"))
        self.assertEqual(self.facts()["size_class"], "Легковой")

    async def test_explicit_service_can_prepare_booking_without_an_invented_summary_comment(self):
        state = self.agent._state[self.user.id]
        state.update(facts={key: value for key, value in ARGS.items() if key != "tariff"},
                     invalidated=["services", "tariff"])
        state["facts"]["requested_service"] = "Мойка"
        self.agent._turn_text[self.user.id] = "Хочу услугу Мойка"
        result = await self.agent._call_tool("create_booking", json.dumps({
            "services": ["Мойка"], "comment": "Сводка заявки, которую клиент не диктовал"}), self.user)
        self.assertTrue(result["confirmation_required"])
        self.assertEqual(self.agent._pending[self.user.id]["args"]["comment"], "")
        self.assertFalse(sum(len(sheet.writes) for sheet in self.spreadsheet.sheets))

    async def test_explicit_service_does_not_authorize_other_selection_or_drop_literal_comment(self):
        state = self.agent._state[self.user.id]
        state.update(facts={key: value for key, value in ARGS.items() if key != "tariff"},
                     invalidated=["services", "tariff"])
        state["facts"]["requested_service"] = "Мойка"
        self.agent._turn_text[self.user.id] = "Мойка. Салон не мочить"
        with self.assertRaisesRegex(ValueError, "invalidated_booking_field"):
            self.agent._booking_args({"services": ["Воск"]}, self.user)
        arguments = self.agent._booking_args({"services": ["Мойка"], "comment": "Салон не мочить"}, self.user)
        self.assertEqual(arguments["comment"], "Салон не мочить")

    async def test_invalid_readonly_memory_does_not_accept_a_fabricated_customer_quote(self):
        self.transport.side_effect = [response(envelope(facts={"car": ["Toyota", "привет"]})), response(calls=[
            ("respond", {"reply": "Учту Toyota.", "facts": {"car": ["Toyota", "Toyota"]}})])]
        answer = await self.agent.reply(self.user, "привет")
        self.assertTrue(answer)
        self.assertFalse(self.facts())
        self.assertEqual(self.transport.call_count, 1)

    async def test_recognized_car_is_separate_from_the_literal_customer_spelling(self):
        for spelling in ("лансер девятый", "лансер 9", "lancer 9"):
            with self.subTest(spelling=spelling):
                self.agent.reset(self.user.id)
                self.transport.side_effect = [response(calls=[("respond", {"reply": "Посмотрю цены.", "facts": {
                    "car": [spelling, spelling], "car_model": ["Mitsubishi Lancer IX", spelling]}})])]
                answer = await self.agent.reply(self.user, spelling)
                self.assertEqual(answer, "Посмотрю цены.")
                self.assertEqual(self.facts(), {"car": spelling, "car_model": "Mitsubishi Lancer IX"})
                self.assertNotIn(self.user.id, self.agent._pending)
        self.assertEqual(self.transport.call_count, 3)
        self.assertFalse(sum(len(sheet.writes) for sheet in self.spreadsheet.sheets))

    async def test_expanded_car_name_cannot_replace_the_literal_fact_or_force_an_ai_retry(self):
        invalid = response(calls=[("respond", {"reply": "Учту.", "facts": {
            "car": ["Mitsubishi Lancer IX", "лансер девятый"]}})])
        self.transport.side_effect = [invalid, invalid]
        answer = await self.agent.reply(self.user, "лансер девятый")
        self.assertTrue(answer)
        self.assertFalse(self.facts())
        self.assertEqual(self.transport.call_count, 1)

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


class BookingOrdinalTests(unittest.IsolatedAsyncioTestCase):
    """Порядковый номер записи клиента считается по прочитанным строкам, без повторного чтения."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store, self.spreadsheet = fixture(folder.name)

    def test_ordinal_follows_date_order_and_cancel_has_no_number(self):
        from tests.fakes import booking as booking_args
        self.spreadsheet.worksheet("Слоты").values.extend(
            [["01.01.2099", "11:00", "2"], ["01.01.2099", "12:00", "2"]])
        evening = self.store.create_booking(**booking_args(time_str="12:00"))
        self.assertTrue(evening["ok"], evening)
        self.assertEqual(evening["user_no"], "1")
        earlier = self.store.create_booking(**booking_args(time_str="11:00"))
        self.assertTrue(earlier["ok"], earlier)
        self.assertEqual(earlier["user_no"], "1", "более ранняя запись получает меньший номер")
        cancelled = self.store.cancel_booking(evening["booking_id"], 111)
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertIsNone(cancelled.get("user_no"), "отменённая запись уже не активна")
        mine = self.store.my_bookings(111)["bookings"]
        self.assertEqual([str(b["booking_id"]) for b in mine], [str(earlier["booking_id"])])


class NaturalDialogue(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store, self.spreadsheet = fixture(folder.name)
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(self.store.config, self.store)
        self.transport = AsyncMock()
        self.agent.client.chat.completions.create = self.transport
        self.user = ChatUser(111, None, "Тестовый клиент")

    async def test_car_and_automatic_catalog_class_are_saved_in_one_request(self):
        self.transport.side_effect = [response(calls=[("respond", {
            "reply": "Для легкового посмотрю цены. Нужна мойка кузова?",
            "facts": {"size_class": ["Легковой", "лансер девять"],
                      "car": ["лансер девять", "лансер девять"]}})])]
        answer = await self.agent.reply(self.user, "лансер девять")
        self.assertNotIn("Не удалось", answer)
        self.assertEqual(self.agent._state[111]["facts"], {"car": "лансер девять", "size_class": "Легковой"})
        self.assertEqual(self.transport.call_count, 1)
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    def test_inferred_class_requires_a_customer_car_and_an_existing_catalog_label(self):
        catalog = self.store.get_catalog()
        for value, source in (("Неизвестный", "лансер девять"), ("Легковой", "синий")):
            with self.subTest(value=value, source=source), self.assertRaises(ValueError):
                validate_patch({"size_class": [value, source]}, source, self.store.now(), catalog,
                               known={"car": "лансер девять"})

    def test_service_interpretation_uses_sourced_request_and_the_actual_catalog(self):
        catalog = self.store.get_catalog()
        text = "помойте кузов"
        result = validate_patch({"requested_service": [text, text], "services": [["Мойка"], text]},
                                text, self.store.now(), catalog)
        self.assertEqual(result["services"], ["Мойка"])
        for services in (["Выдуманная"], ["Мойка"]):
            with self.subTest(services=services), self.assertRaises(ValueError):
                validate_patch({"services": [services, "привет"]}, "привет", self.store.now(), catalog)

    def test_weekdays_and_hour_only_times_are_checked_without_ai(self):
        now = datetime(2026, 10, 25, 12)
        for text, expected in (("в понедельник", "2026-10-26"), ("во вторник", "2026-10-27"),
                               ("в среду", "2026-10-28"), ("в пятницу", "2026-10-30")):
            with self.subTest(text=text):
                self.assertEqual(validate_patch({"date": [expected, text]}, text, now)["date"], expected)
        for text in ("в 11", "к 11", "11 часов", "11:00", "11.00"):
            with self.subTest(text=text):
                self.assertEqual(validate_patch({"time": ["11:00", text]}, text, now)["time"], "11:00")

    def test_normalization_does_not_accept_wrong_dates_times_or_trailing_text(self):
        now = datetime(2026, 10, 25, 12)
        for field, value, text in (("date", "2026-10-27", "в понедельник"),
                                   ("time", "18:00", "в 6"), ("time", "25:00", "в 25"),
                                   ("time", "11:00", "11:00 garbage")):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_patch({field: [value, text]}, text, now)

    async def test_plain_dialogue_keeps_old_customer_words_in_the_same_context_budget(self):
        for text in ("Никита", "лансер девять", *[f"Вопрос {index}" for index in range(8)]):
            self.transport.side_effect = [response("Подскажу по услугам.", raw=True)]
            await self.agent.reply(self.user, text)
            history = self.agent._history[111]
            context = self.agent._state[111]["context"]
            self.assertLessEqual(len(history), 8)
            self.assertLessEqual(len(context), 6)
            self.assertLessEqual(sum(map(len, context)), 1500)
            self.assertLessEqual(sum(len(message["content"]) for message in history) + sum(map(len, context)), 6000)
        payload = json.dumps(self.transport.call_args.kwargs, ensure_ascii=False)
        self.assertIn("Никита", payload)
        self.assertIn("лансер девять", payload)
        self.assertEqual(self.transport.call_count, 10)
        self.assertFalse(self.agent._state[111].get("facts"))
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_recognized_model_is_cleared_when_customer_changes_the_car(self):
        self.transport.side_effect = [response(calls=[("respond", {"reply": "Учту.", "facts": {
            "car": ["лансер девять", "лансер девять"],
            "car_model": ["Mitsubishi Lancer IX", "лансер девять"]}})])]
        await self.agent.reply(self.user, "лансер девять")
        self.transport.side_effect = [response(calls=[("respond", {"reply": "Поменяю автомобиль.", "facts": {
            "car": ["Toyota Camry", "Toyota Camry"]}})])]
        await self.agent.reply(self.user, "Теперь Toyota Camry")
        facts = self.agent._state[111]["facts"]
        self.assertEqual(facts["car"], "Toyota Camry")
        self.assertNotIn("car_model", facts)
        self.assertNotIn("size_class", facts)

    async def test_valid_price_resolves_an_automotive_segment_instead_of_asking_customer_for_class(self):
        self.agent._state[111] = {"facts": {"car": "лансер девять"}, "tentative": {"size_class": "C"},
                                  "catalog": {"size_classes": ["Легковой", "Кроссовер"]}}
        result = await self.agent._call_tool("calculate_price", '{"size_class":"Легковой","services":["Мойка"]}', self.user)
        self.assertTrue(result["ok"])
        self.assertEqual(self.agent._state[111]["facts"]["size_class"], "Легковой")
        self.assertNotIn("size_class", self.agent._state[111].get("tentative", {}))
        self.assertFalse(self.spreadsheet.worksheet("Записи").writes)

    async def test_quote_does_not_override_a_valid_ambiguous_price_class(self):
        self.agent._state[111] = {"facts": {"car": "BMW"}, "tentative": {"size_class": "Кроссовер"},
                                  "catalog": {"size_classes": ["Легковой", "Кроссовер"]}}
        result = await self.agent._call_tool("calculate_price", '{"size_class":"Легковой","services":["Мойка"]}', self.user)
        self.assertTrue(result["ok"])
        self.assertNotIn("size_class", self.agent._state[111]["facts"])
        self.assertEqual(self.agent._state[111]["tentative"]["size_class"], "Кроссовер")

    def test_normalized_booking_datetime_uses_latest_customer_words_not_a_suggested_slot(self):
        from tests.test_conversation import ARGS
        self.agent._state[111] = {"facts": {name: value for name, value in ARGS.items() if name not in {"date", "time"}}}
        self.agent._turn_text[111] = "Хочу в понедельник в 11"
        with patch.object(self.store, "now", return_value=datetime(2026, 10, 25, 12)):
            result = self.agent._booking_args(dict(ARGS, date="2026-10-26", time="11:00"), self.user)
            self.assertEqual(result["time"], "11:00")
            with self.assertRaises(ValueError):
                self.agent._booking_args(dict(ARGS, date="2026-10-27", time="12:00"), self.user)

    def test_model_gets_price_categories_from_sheets_instead_of_automotive_segments(self):
        before = self.agent._tools(self.user)
        price = next(tool for tool in before if tool["function"]["name"] == "get_price_list")
        self.assertFalse(price["function"]["parameters"]["properties"])
        self.agent._state[111]["catalog"] = {"size_classes": ["Легковой", "Кроссовер"]}
        for tool in self.agent._tools(self.user):
            field = tool["function"]["parameters"]["properties"].get("size_class")
            if field:
                self.assertEqual(field["enum"], ["Легковой", "Кроссовер"])
                self.assertNotIn("D", field["enum"])

    async def test_repeated_name_prefix_is_removed_without_changing_facts_or_asking_ai_again(self):
        self.agent._state[111] = {"facts": {"client_name": "Иван"}}
        for text, reply in (("Привет", "Иван, какой у вас автомобиль?"),
                            ("синий", "Иван, уточните модель BMW.")):
            self.transport.side_effect = [response(reply, raw=True)]
            answer = await self.agent.reply(self.user, text)
        self.assertEqual(answer, "Уточните модель BMW.")
        self.assertEqual(self.agent._state[111]["facts"], {"client_name": "Иван"})
        self.assertEqual(self.transport.call_count, 2)
        self.assertEqual(self.agent._history[111][-1]["content"], answer)

    async def test_long_current_message_does_not_add_old_context_beyond_the_existing_budget(self):
        previous = "Старые сведения " * 20
        self.agent._state[111] = {"context": [previous]}
        self.transport.side_effect = [response("Уточним детали.", raw=True)]
        await self.agent.reply(self.user, "x" * 5990)
        payload = json.dumps(self.transport.call_args.kwargs, ensure_ascii=False)
        self.assertNotIn(previous, payload)
        self.assertEqual(self.transport.call_count, 1)
        self.assertIn(previous, self.agent._state[111]["context"])
