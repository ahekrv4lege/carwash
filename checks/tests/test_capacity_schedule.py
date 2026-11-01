"""Производственная раскладка «Слоты»: часовые слоты с числом постов.

Число в «Постов» — общая вместимость слота; занятость считают активные записи.
Ожидаемые значения заданы вручную.
"""
import tempfile
import threading
import unittest
from collections import Counter
from datetime import date

from adaptive import MappingError
from tests.fakes import Worksheet, booking, fixture


def capacity_store(temp, rows):
    store, spreadsheet = fixture(temp)
    spreadsheet.worksheet("Слоты").values = [["Дата", "Время", "Постов"], *rows]
    return store, spreadsheet


class CapacityScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = capacity_store(self.temp.name, [
            ["01.01.2099", "09:00", "2"],
            ["01.01.2099", "10:00", "1"],
            ["01.01.2099", "11:30", "1"],
            ["02.01.2099", "09:00", "1"],
        ])

    def test_only_slots_from_the_table_are_offered(self):
        free = self.store.free_slots("2099-01-01")["days"][0]["free_times"]
        self.assertEqual(free, ["09:00", "10:00", "11:30"])  # нестандартный интервал сохраняется

    def test_time_outside_schedule_is_refused(self):
        result = self.store.create_booking(**booking(time_str="10:30"))
        self.assertFalse(result["ok"])
        self.assertIn("недоступно или уже занято", result["error"])

    def test_capacity_exhaustion_uses_active_bookings(self):
        self.assertTrue(self.store.create_booking(**booking(time_str="10:00", telegram_id=111, car="A"))["ok"])
        second = self.store.create_booking(**booking(time_str="10:00", telegram_id=222, car="B"))
        self.assertFalse(second["ok"])
        self.assertIn("недоступно или уже занято", second["error"])
        self.store._invalidate("slots", "bookings")
        self.assertNotIn("10:00", self.store.free_slots("2099-01-01")["days"][0]["free_times"])

    def test_two_posts_allow_two_bookings_and_no_more(self):
        self.assertTrue(self.store.create_booking(**booking(time_str="09:00", telegram_id=111, car="A"))["ok"])
        self.assertTrue(self.store.create_booking(**booking(time_str="09:00", telegram_id=222, car="B"))["ok"])
        self.assertFalse(self.store.create_booking(**booking(time_str="09:00", telegram_id=333, car="C"))["ok"])

    def test_cancel_frees_capacity_for_another_customer(self):
        first = self.store.create_booking(**booking(time_str="10:00", telegram_id=111, car="A"))
        self.assertTrue(first["ok"])
        self.assertTrue(self.store.cancel_booking(first["booking_id"], 111)["ok"])
        self.assertTrue(self.store.create_booking(**booking(time_str="10:00", telegram_id=222, car="B"))["ok"])

    def test_reschedule_checks_destination_capacity(self):
        first = self.store.create_booking(**booking(time_str="10:00", telegram_id=111, car="A"))
        self.assertTrue(self.store.create_booking(**booking(time_str="09:00", telegram_id=222, car="B"))["ok"])
        self.assertTrue(self.store.create_booking(**booking(time_str="09:00", telegram_id=333, car="C"))["ok"])
        refused = self.store.reschedule_booking(first["booking_id"], 111, "2099-01-01", "09:00")
        self.assertFalse(refused["ok"])
        self.assertIn("Исходная запись сохранена", refused["error"])
        self.store._invalidate("bookings")
        mine = self.store.my_bookings(111)["bookings"]
        self.assertEqual([(item["time"], str(item["booking_id"])) for item in mine], [("10:00", str(first["booking_id"]))])

    def test_reschedule_keeps_id_and_frees_old_capacity(self):
        first = self.store.create_booking(**booking(time_str="10:00", telegram_id=111, car="A"))
        moved = self.store.reschedule_booking(first["booking_id"], 111, "2099-01-01", "11:30")
        self.assertTrue(moved["ok"], moved)
        self.assertEqual(str(moved["booking_id"]), str(first["booking_id"]))
        self.assertTrue(self.store.create_booking(**booking(time_str="10:00", telegram_id=222, car="B"))["ok"])

    def test_date_boundary_is_respected(self):
        # 01.01 09:00 — два поста; запись на 02.01 не занимает место 01.01.
        self.assertTrue(self.store.create_booking(**booking(time_str="09:00", telegram_id=111, car="A"))["ok"])
        next_day = self.store.create_booking(**booking(date_str="2099-01-02", time_str="09:00", telegram_id=222, car="B"))
        self.assertTrue(next_day["ok"])
        self.store._invalidate("slots", "bookings")
        self.assertIn("09:00", self.store.free_slots("2099-01-01")["days"][0]["free_times"])
        self.assertNotIn("09:00", self.store.free_slots("2099-01-02")["days"][0]["free_times"])

    def test_capacity_two_reports_two_one_and_zero_places(self):
        slots = self.store._get_slots()
        busy = Counter()
        key = (date(2099, 1, 1), "09:00")
        self.assertEqual(self.store._capacity_left(slots, busy, key), 2)
        busy[key] = 1
        self.assertEqual(self.store._capacity_left(slots, busy, key), 1)
        busy[key] = 2
        self.assertEqual(self.store._capacity_left(slots, busy, key), 0)

    def test_eight_simultaneous_attempts_for_two_posts_create_two_rows(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store, spreadsheet = capacity_store(temp.name, [["01.01.2099", "10:00", "2"]])
        results = []

        def attempt(number):
            results.append(store.create_booking(**booking(telegram_id=3000 + number, car=f"Car {number}")))

        threads = [threading.Thread(target=attempt, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for result in results if result["ok"]), 2)
        self.assertEqual(len(spreadsheet.worksheet("Записи").values), 3)

    def test_cancelled_status_frees_a_place_and_unknown_status_stays_conservative(self):
        first = self.store.create_booking(**booking(time_str="09:00", telegram_id=111, car="A"))
        second = self.store.create_booking(**booking(time_str="09:00", telegram_id=222, car="B"))
        self.assertTrue(first["ok"] and second["ok"])
        sheet = self.spreadsheet.worksheet("Записи")
        sheet.values[1][-1] = "Отменена"
        sheet.values[2][-1] = "Архив"  # неизвестный для будущей записи статус считаем занятым
        self.store._invalidate("slots", "bookings")
        self.assertTrue(self.store.create_booking(**booking(time_str="09:00", telegram_id=333, car="C"))["ok"])
        self.assertFalse(self.store.create_booking(**booking(time_str="09:00", telegram_id=444, car="D"))["ok"])

    def test_mixed_grid_and_capacity_rows_for_one_time_are_rejected(self):
        self.spreadsheet.sheets.append(Worksheet(9, "01.01.2099", [["Время", "Пост 1"], ["10:00", ""]]))
        with self.assertRaises(MappingError):
            self.store.free_slots("2099-01-01")

    def test_concurrent_bookings_for_single_post_create_one_row(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store, spreadsheet = capacity_store(temp.name, [["01.01.2099", "10:00", "1"]])
        results = []

        def attempt(number):
            results.append(store.create_booking(**booking(telegram_id=2000 + number, car=f"Car {number}")))

        threads = [threading.Thread(target=attempt, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for result in results if result["ok"]), 1)
        self.assertEqual(len(spreadsheet.worksheet("Записи").values), 2)


if __name__ == "__main__":
    unittest.main()
