"""Запись по длительности услуги: график работы + интервалы вместо готовых слотов.

Раскладки синтетические: график «Начало/Конец/Постов», длительности — в колонке
«Услуги», у пакета — в «Тарифы», либо на отдельном листе «Длительности».
Длительность не выводится из имён услуг, интервалов между строками и соседних записей.
"""
import os
import tempfile
from copy import deepcopy
import threading
import unittest
from datetime import date
from itertools import count
from unittest.mock import AsyncMock, patch

from adaptive import MappingError, parse_duration_minutes
from ai import Agent, ChatUser
from sheets import DurationSchedule
from tests.fakes import Worksheet, booking, fixture
from tests.test_conversation import response

# «Записи»: 16 стандартных колонок BOOKING_HEADERS, длительность добавляется в конец.
BOOKING_DURATION = len(["ID", "Создано", "Дата", "Время", "Имя", "Телефон", "Автомобиль", "Класс",
                        "Тариф", "Доп. услуги", "Состав", "Сумма", "Комментарий", "Telegram ID",
                        "Username", "Статус"])

_DIRS = count(1)


def fresh_dir(temp):
    path = os.path.join(temp, f"store-{next(_DIRS)}")
    os.makedirs(path, exist_ok=True)
    return path


DEFAULT_SERVICES = [["Мойка", "Кузов", "700", "800", "да", "60"],
                    ["Полировка", "Кузов", "2000", "2500", "да", "120"]]
DEFAULT_TARIFFS = [["Базовый", "Мойка", "10", "да", ""],
                   ["Комплекс", "Мойка, Полировка", "5", "да", "180"]]
DEFAULT_SCHEDULE = [["09:00", "19:00", "2"]]


def duration_store(temp, *, services=None, tariffs=None, schedule=None, durations_sheet=None):
    """Магазин с графиком работы и длительностями услуг."""
    store, spreadsheet = fixture(fresh_dir(temp))
    svc = spreadsheet.worksheet("Услуги")
    svc.values = [["Услуга", "Описание", "Легковой", "Кроссовер", "Доступна", "Длительность, мин"],
                  *deepcopy(services if services is not None else DEFAULT_SERVICES)]
    tr = spreadsheet.worksheet("Тарифы")
    tr.values = [["Тариф", "Состав", "Скидка %", "Доступен", "Длительность"],
                 *deepcopy(tariffs if tariffs is not None else DEFAULT_TARIFFS)]
    slots = spreadsheet.worksheet("Слоты")
    slots.values = [["Начало", "Конец", "Постов"], *deepcopy(schedule or DEFAULT_SCHEDULE)]
    spreadsheet.worksheet("Записи").values[0].append("Длительность")
    if durations_sheet is not None:
        spreadsheet.sheets = [sheet for sheet in spreadsheet.sheets if sheet.title != "Слоты"]
        spreadsheet.sheets.append(Worksheet(9, "Режим работы", [["Начало", "Конец", "Постов"],
                                                                *(schedule or DEFAULT_SCHEDULE)]))
        spreadsheet.sheets.append(Worksheet(10, "Длительности", durations_sheet))
    store._invalidate("catalog", "slots", "bookings")
    return store, spreadsheet


class DurationParsingTests(unittest.TestCase):
    def test_minutes_hours_ranges_and_combinations(self):
        self.assertEqual(parse_duration_minutes("90"), 90)
        self.assertEqual(parse_duration_minutes("полтора часа"), 90)
        self.assertEqual(parse_duration_minutes("полчаса"), 30)
        self.assertEqual(parse_duration_minutes("два часа"), 120)
        self.assertEqual(parse_duration_minutes("90 мин"), 90)
        self.assertEqual(parse_duration_minutes("1,5 ч"), 90)
        self.assertEqual(parse_duration_minutes("2 часа"), 120)
        self.assertEqual(parse_duration_minutes("1 ч 30 мин"), 90)
        self.assertEqual(parse_duration_minutes("1:30"), 90)
        self.assertEqual(parse_duration_minutes("10:00-11:30"), 90)
        self.assertEqual(parse_duration_minutes("2", "hours"), 120)
        self.assertEqual(parse_duration_minutes("2", "minutes"), 2)

    def test_unreadable_or_absurd_values_are_not_invented(self):
        for value in ("", "мусор", "0", "полтора", "-30", "100:00"):
            self.assertIsNone(parse_duration_minutes(value), value)


class DurationScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = duration_store(self.temp.name)

    def slots(self):
        return self.spreadsheet.worksheet("Слоты")

    def test_schedule_table_becomes_duration_mode(self):
        slots = self.store._get_slots()
        self.assertIsInstance(slots, DurationSchedule)
        self.assertEqual(slots.intervals(self.store.now().date()), ((540, 1140),))
        self.assertEqual(slots.posts(self.store.now().date()), 2)

    def test_free_starts_use_service_duration(self):
        day = "2099-01-01"
        full = self.store.free_slots(day, 1, 60)["days"][0]["free_times"]
        self.assertEqual(full[0], "09:00")
        self.assertEqual(full[-1], "18:00")  # 18:30+60 = 19:30 — уже вне графика
        long_ = self.store.free_slots(day, 1, 120)["days"][0]["free_times"]
        self.assertEqual(long_[-1], "17:00")

    def test_without_service_the_shortest_known_duration_is_used(self):
        result = self.store.free_slots("2099-01-01")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["days"][0]["free_times"][-1], "18:00")  # минимальная длительность 60 мин
        self.assertIn("минимальной длительности", result["note"])

    def test_no_duration_data_is_a_safe_refusal(self):
        store, _ = duration_store(self.temp.name, services=[["Мойка", "Кузов", "700", "800", "да", ""]],
                                  tariffs=[["Базовый", "Мойка", "10", "да", ""]])
        result = store.free_slots("2099-01-01")
        self.assertFalse(result["ok"])
        self.assertIn("Нет данных о длительности", result["error"])
        refused = store.create_booking(**booking())
        self.assertFalse(refused["ok"])
        self.assertIn("Нет данных о длительности", refused["error"])

    def test_malformed_duration_is_neither_price_nor_duration(self):
        store, _ = duration_store(self.temp.name,
                                  services=[["Мойка", "Кузов", "700", "800", "да", "как получится"]],
                                  tariffs=[["Базовый", "Мойка", "10", "да", ""]])
        catalog = store.get_catalog(fresh=True)
        self.assertFalse(catalog.durations_present)
        self.assertNotIn("как получится", str(catalog.size_classes))
        refused = store.create_booking(**booking())
        self.assertFalse(refused["ok"])
        self.assertIn("Нет данных о длительности", refused["error"])

    def test_working_hour_boundaries(self):
        early = self.store.create_booking(**booking(time_str="08:30", car="A"))
        self.assertFalse(early["ok"])
        self.assertIn("вне графика работы", early["error"])

    def test_boundary_end_is_inclusive(self):
        result = self.store.create_booking(**booking(time_str="18:00", services=["Мойка"], tariff=None))
        self.assertTrue(result["ok"], result)  # 18:00 + 60 = 19:00 — ровно конец графика

    def test_two_resources_allow_parallel_bookings_third_refused(self):
        self.assertTrue(self.store.create_booking(**booking(time_str="10:00", telegram_id=111, car="A"))["ok"])
        self.assertTrue(self.store.create_booking(**booking(time_str="10:00", telegram_id=222, car="B"))["ok"])
        refused = self.store.create_booking(**booking(time_str="10:00", telegram_id=333, car="C"))
        self.assertFalse(refused["ok"])
        self.assertIn("нет свободного поста", refused["error"])

    def test_overlapping_appointments_count_resources(self):
        self.assertTrue(self.store.create_booking(**booking(time_str="10:00", telegram_id=111, car="A"))["ok"])
        self.assertTrue(self.store.create_booking(**booking(time_str="10:30", telegram_id=222, car="B"))["ok"])
        refused = self.store.create_booking(**booking(time_str="10:15", telegram_id=333, car="C"))
        self.assertFalse(refused["ok"])  # 10:15–11:15 пересекает обе записи

    def test_start_right_after_a_booking_is_allowed(self):
        self.slots().values = [["Начало", "Конец", "Постов"], ["09:00", "19:00", "1"]]
        self.store._invalidate("slots", "bookings")
        self.assertTrue(self.store.create_booking(**booking(time_str="10:00", telegram_id=111, car="A"))["ok"])
        self.assertTrue(self.store.create_booking(**booking(time_str="11:00", telegram_id=222, car="B"))["ok"])

    def test_canceled_booking_frees_the_interval(self):
        self.slots().values = [["Начало", "Конец", "Постов"], ["09:00", "19:00", "1"]]
        self.store._invalidate("slots", "bookings")
        created = self.store.create_booking(**booking(time_str="10:00", telegram_id=111, car="A"))
        self.assertTrue(created["ok"])
        refused = self.store.create_booking(**booking(time_str="10:30", telegram_id=222, car="B"))
        self.assertFalse(refused["ok"])
        self.assertTrue(self.store.cancel_booking(created["booking_id"], 111)["ok"])
        self.store._invalidate("bookings")
        self.assertTrue(self.store.create_booking(**booking(time_str="10:30", telegram_id=222, car="B"))["ok"])

    def test_reschedule_checks_duration_interval_and_keeps_id(self):
        created = self.store.create_booking(**booking(time_str="10:00", telegram_id=111, car="A"))
        self.assertTrue(self.store.create_booking(**booking(time_str="12:00", telegram_id=222, car="B"))["ok"])
        moved = self.store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "11:30")
        self.assertTrue(moved["ok"], moved)
        self.assertEqual(str(moved["booking_id"]), str(created["booking_id"]))
        self.store._invalidate("bookings")
        self.assertEqual([item["time"] for item in self.store.my_bookings(111)["bookings"]], ["11:30"])
        refused = self.store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "18:30")
        self.assertFalse(refused["ok"])
        self.assertIn("Исходная запись сохранена", refused["error"])

    def test_per_date_schedule_rows_override_the_daily_one(self):
        self.slots().values = [["Начало", "Конец", "Постов", "Дата"],
                               ["09:00", "19:00", "2", ""],
                               ["10:00", "15:00", "1", "01.01.2099"]]
        self.store._invalidate("slots", "bookings")
        slots = self.store._get_slots()
        self.assertEqual(slots.intervals(self.store.now().date()), ((540, 1140),))
        self.assertEqual(slots.intervals(date(2099, 1, 1)), ((600, 900),))
        free = self.store.free_slots("2099-01-01", 1, 60)["days"][0]["free_times"]
        self.assertEqual(free[0], "10:00")
        self.assertEqual(free[-1], "14:00")
        self.assertNotIn("15:30", free)
        self.assertTrue(self.store.create_booking(**booking(time_str="11:00", telegram_id=111, car="A"))["ok"])
        refused = self.store.create_booking(**booking(time_str="11:30", telegram_id=222, car="B"))
        self.assertFalse(refused["ok"])  # один пост на 01.01, интервалы пересекаются

    def test_break_between_intervals_is_respected(self):
        self.slots().values = [["Начало", "Конец", "Постов"],
                               ["09:00", "13:00", "1"], ["14:00", "19:00", "1"]]
        self.store._invalidate("slots", "bookings")
        refused = self.store.create_booking(**booking(time_str="12:30", services=["Мойка"], tariff=None))
        self.assertFalse(refused["ok"])  # 12:30+60 пересекает перерыв
        self.assertIn("вне графика работы", refused["error"])
        fits = self.store.create_booking(**booking(time_str="14:00", services=["Мойка"], tariff=None))
        self.assertTrue(fits["ok"], fits)

    def test_conflicting_posts_in_schedule_are_refused(self):
        self.slots().values = [["Начало", "Конец", "Постов"],
                               ["09:00", "13:00", "1"], ["14:00", "19:00", "2"]]
        self.store._invalidate("slots", "bookings")
        with self.assertRaises(MappingError):
            self.store._get_slots()

    def test_malformed_schedule_interval_blocks_reads(self):
        self.slots().values = [["Начало", "Конец", "Постов"], ["19:00", "09:00", "2"]]
        self.store._invalidate("slots", "bookings")
        with self.assertRaises(MappingError):
            self.store._get_slots()

    def test_package_duration_is_taken_from_the_tariff(self):
        package = self.store.create_booking(**booking(tariff="Комплекс", services=[]))
        self.assertTrue(package["ok"], package)  # 180 мин: 09:00–12:00 в графике
        self.store._invalidate("bookings")
        self.assertEqual(self.spreadsheet.worksheet("Записи").values[-1][BOOKING_DURATION], "180")

    def test_extras_add_to_package_duration(self):
        store, spreadsheet = duration_store(self.temp.name, tariffs=[["Базовый", "Мойка", "10", "да", "60"]])
        result = store.create_booking(**booking(tariff="Базовый", services=["Полировка"]))
        self.assertTrue(result["ok"], result)
        self.store._invalidate("bookings")
        self.assertEqual(spreadsheet.worksheet("Записи").values[-1][BOOKING_DURATION], "180")

    def test_missing_duration_for_one_service_rejects_only_that_order(self):
        store, _ = duration_store(self.temp.name,
                                  services=[["Мойка", "Кузов", "700", "800", "да", "60"],
                                            ["Химчистка", "Салон", "3000", "3500", "да", ""]])
        refused = store.create_booking(**booking(services=["Химчистка"], tariff=None))
        self.assertFalse(refused["ok"])
        self.assertIn("Химчистка", refused["error"])
        fine = store.create_booking(**booking(telegram_id=222, car="B", services=["Мойка"], tariff=None))
        self.assertTrue(fine["ok"], fine)

    def test_duration_changes_between_proposal_and_confirmation(self):
        quote = self.store.calculate_price("Легковой", "Базовый", [])
        self.assertEqual(quote.get("duration_minutes"), 60)
        expected = dict(quote, currency="₽", duration_minutes=60)
        self.spreadsheet.worksheet("Услуги").values[1][5] = "90"
        self.store._invalidate("catalog")
        changed = self.store.create_booking(**booking(expected_quote=expected))
        self.assertFalse(changed["ok"])
        self.assertTrue(changed.get("quote_changed"), changed)
        self.assertEqual(changed["quote"]["duration_minutes"], 90)

    def test_durations_on_a_separate_worksheet(self):
        store, spreadsheet = duration_store(
            self.temp.name,
            services=[["Мойка", "Кузов", "700", "800", "да", ""],
                      ["Полировка", "Кузов", "2000", "2500", "да", ""]],
            schedule=[["09:00", "19:00", "1"]],
            durations_sheet=[["Услуга", "Длительность, мин"], ["Мойка", "45"], ["Полировка", "2 ч"]])
        catalog = store.get_catalog(fresh=True)
        self.assertTrue(catalog.durations_present)
        self.assertEqual(catalog.services["мойка"].duration_minutes, 45)
        self.assertEqual(catalog.services["полировка"].duration_minutes, 120)
        free = store.free_slots("2099-01-01", 1, 45)["days"][0]["free_times"]
        self.assertEqual(free[-1], "18:00")
        created = store.create_booking(**booking(time_str="10:00", telegram_id=111, car="A"))
        self.assertTrue(created["ok"], created)
        self.store._invalidate("bookings")
        self.assertEqual(spreadsheet.worksheet("Записи").values[-1][BOOKING_DURATION], "45")

    def test_conflicting_durations_are_not_used(self):
        store, _ = duration_store(
            self.temp.name,
            schedule=[["09:00", "19:00", "1"]],
            durations_sheet=[["Услуга", "Длительность, мин"], ["Мойка", "90"]])
        catalog = store.get_catalog(fresh=True)
        self.assertIsNone(catalog.services["мойка"].duration_minutes)
        refused = store.create_booking(**booking(services=["Мойка"], tariff=None))
        self.assertFalse(refused["ok"])
        self.assertIn("Нет данных о длительности", refused["error"])

    def test_duration_source_change_is_picked_up(self):
        quote = self.store.calculate_price("Легковой", "Базовый", [])
        self.assertEqual(quote["duration_minutes"], 60)
        self.spreadsheet.worksheet("Услуги").values[1][5] = "45"
        self.store._invalidate("catalog")
        self.assertEqual(self.store.calculate_price("Легковой", "Базовый", [])["duration_minutes"], 45)

    def test_unknown_service_and_conflicts_on_durations_sheet_are_dropped(self):
        store, _ = duration_store(
            self.temp.name,
            durations_sheet=[["Услуга", "Длительность, мин"], ["Несуществующая", "30"], ["Полировка", "90"]])
        catalog = store.get_catalog(fresh=True)
        self.assertEqual(catalog.services["мойка"].duration_minutes, 60)
        self.assertIsNone(catalog.services["полировка"].duration_minutes)  # противоречие 120/90 отключено

    def test_classic_slot_layout_ignores_duration_column(self):
        store, spreadsheet = fixture(fresh_dir(self.temp.name))
        spreadsheet.worksheet("Услуги").values[0].append("Длительность")
        for row in spreadsheet.worksheet("Услуги").values[1:]:
            row.append("60")
        spreadsheet.worksheet("Записи").values[0].append("Длительность")
        store._invalidate("catalog", "slots", "bookings")
        slots = store._get_slots()
        self.assertNotIsInstance(slots, DurationSchedule)
        result = store.create_booking(**booking())
        self.assertTrue(result["ok"], result)
        self.store._invalidate("bookings")
        row = spreadsheet.worksheet("Записи").values[-1]
        self.assertEqual(row[BOOKING_DURATION], "")  # занятость считается по слотам, длительность не нужна
        free = store.free_slots("2099-01-01")["days"][0]["free_times"]
        self.assertEqual(free, ["10:00"])

    def test_mixing_schedule_and_capacity_rows_is_refused(self):
        store, spreadsheet = fixture(fresh_dir(self.temp.name))
        spreadsheet.sheets.append(Worksheet(9, "Режим работы", [["Начало", "Конец"], ["09:00", "19:00"]]))
        store._invalidate("slots", "bookings")
        with self.assertRaises(MappingError):
            store._get_slots()

    def test_concurrent_bookings_respect_resource_limit(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store, spreadsheet = duration_store(temp.name, schedule=[["09:00", "19:00", "1"]])
        results = []

        def attempt(number):
            results.append(store.create_booking(**booking(telegram_id=4000 + number, car=f"Car {number}")))

        threads = [threading.Thread(target=attempt, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for result in results if result["ok"]), 1)
        self.assertEqual(len(spreadsheet.worksheet("Записи").values), 2)


class DurationAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = duration_store(self.temp.name, schedule=[["09:00", "19:00", "1"]])
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(self.store.config, self.store)
        self.addCleanup(self.agent.cars.close)
        self.agent.client.chat.completions.create = AsyncMock()
        self.user = ChatUser(111, None, "Synthetic")

    async def test_get_free_slots_uses_catalog_duration_not_the_model(self):
        self.agent._state[111]["facts"] = {"services": ["Полировка"]}
        result = await self.agent._execute("get_free_slots", {"date": "2099-01-01"}, self.user)
        self.assertEqual(result["days"][0]["free_times"][-1], "17:00")  # 120 мин
        self.agent._state[111]["facts"] = {"services": ["Мойка"]}
        result = await self.agent._execute("get_free_slots", {"date": "2099-01-01"}, self.user)
        self.assertEqual(result["days"][0]["free_times"][-1], "18:00")  # 60 мин

    def test_quote_state_keeps_duration_for_reconfirmation(self):
        state = self.agent._quote_state({"total": 630, "currency": "₽", "lines": [], "duration_minutes": 90})
        self.assertEqual(state["duration_minutes"], 90)


if __name__ == "__main__":
    unittest.main()
