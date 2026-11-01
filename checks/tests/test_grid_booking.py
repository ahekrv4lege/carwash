"""Запись в сетку постов: занятие клетки маркером «Запись <ID>», перенос и отмена.

Синтетические таблицы; реальная таблица клиентов не используется.
Ожидаемые значения заданы явно, а не вычислены кодом.
"""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import adaptive
from adaptive import MappingError
from sheets import FreeCapacity
from tests.fakes import Worksheet, booking, fixture

GRID_TITLE = "01.01.2099"


def grid_store(temp, rows, bays=("Пост 1", "Пост 2"), extra=None):
    """Магазин, где «Слоты» заменены датированной сеткой постов."""
    store, spreadsheet = fixture(temp)
    spreadsheet.sheets = [sheet for sheet in spreadsheet.sheets if sheet.title != "Слоты"]
    values = [["Время", *bays], *rows]
    grid = Worksheet(9, GRID_TITLE, values)
    if extra:
        extra(grid)
    spreadsheet.sheets.append(grid)
    return store, spreadsheet, grid


class GridBookingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet, self.grid = grid_store(
            self.temp.name, [["10:00", "", ""], ["11:00", "", ""]])

    def bookings(self):
        return self.spreadsheet.worksheet("Записи")

    def cell(self, row, column):
        return self.grid.values[row - 1][column - 1]

    def test_booking_claims_first_free_bay_and_marks_it(self):
        result = self.store.create_booking(**booking())
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.cell(2, 2), f"Запись {result['booking_id']}")
        self.assertEqual(self.cell(2, 3), "")
        self.assertEqual(str(self.bookings().values[-1][0]), str(result["booking_id"]))

    def test_second_customer_gets_second_bay_third_is_refused(self):
        self.assertTrue(self.store.create_booking(**booking(telegram_id=111, car="A"))["ok"])
        self.assertTrue(self.store.create_booking(**booking(telegram_id=222, car="B"))["ok"])
        refused = self.store.create_booking(**booking(telegram_id=333, car="C"))
        self.assertFalse(refused["ok"])
        self.assertIn("недоступно или уже занято", refused["error"])
        self.assertEqual(self.cell(2, 2).startswith("Запись "), True)
        self.assertEqual(self.cell(2, 3).startswith("Запись "), True)
        self.assertEqual(len(self.bookings().values), 3)  # заголовок + две записи

    def test_free_slots_do_not_double_subtract_claimed_bays(self):
        self.store.create_booking(**booking(telegram_id=111, car="A"))
        self.store._invalidate("slots", "bookings")
        self.assertIn("10:00", self.store.free_slots("2099-01-01")["days"][0]["free_times"])
        self.store.create_booking(**booking(telegram_id=222, car="B"))
        self.store._invalidate("slots", "bookings")
        self.assertNotIn("10:00", self.store.free_slots("2099-01-01")["days"][0]["free_times"])

    def test_cancel_releases_the_bay(self):
        result = self.store.create_booking(**booking())
        cancelled = self.store.cancel_booking(result["booking_id"], 111)
        self.assertTrue(cancelled["ok"])
        self.assertIn("слот освобождён", cancelled["message"])
        self.assertEqual(self.cell(2, 2), "")
        self.assertEqual(self.bookings().values[-1][-1], "Отмена")
        self.store._invalidate("slots", "bookings")
        self.assertIn("10:00", self.store.free_slots("2099-01-01")["days"][0]["free_times"])

    def test_manually_changed_bay_is_not_cleared_on_cancel(self):
        result = self.store.create_booking(**booking())
        self.grid.values[1][1] = "занято вручную"
        cancelled = self.store.cancel_booking(result["booking_id"], 111)
        self.assertTrue(cancelled["ok"])
        self.assertIn("не освобождено автоматически", cancelled["message"])
        self.assertEqual(self.cell(2, 2), "занято вручную")

    def test_reschedule_moves_marker_and_keeps_booking_id(self):
        result = self.store.create_booking(**booking())
        moved = self.store.reschedule_booking(result["booking_id"], 111, "2099-01-01", "11:00")
        self.assertTrue(moved["ok"], moved)
        self.assertEqual(self.cell(2, 2), "")
        self.assertEqual(self.cell(3, 2), f"Запись {result['booking_id']}")
        row = self.bookings().values[-1]
        self.assertEqual((str(row[0]), row[2], row[3]), (str(result["booking_id"]), "01.01.2099", "11:00"))

    def test_reschedule_into_full_time_keeps_original_bay(self):
        original = self.store.create_booking(**booking(telegram_id=111, car="A"))
        self.assertTrue(original["ok"], original)
        self.assertTrue(self.store.create_booking(**booking(telegram_id=222, car="B", time_str="11:00"))["ok"])
        self.assertTrue(self.store.create_booking(**booking(telegram_id=333, car="C", time_str="11:00"))["ok"])
        refused = self.store.reschedule_booking(original["booking_id"], 111, "2099-01-01", "11:00")
        self.assertFalse(refused["ok"])
        self.assertIn("Исходная запись сохранена", refused["error"])
        self.assertEqual(self.cell(2, 2), f"Запись {original['booking_id']}")
        self.assertEqual(self.bookings().values[1][3], "10:00")

    def test_unknown_write_keeps_claim_and_blocks_retry(self):
        self.bookings().fail_write = True
        with self.assertRaisesRegex(MappingError, "неизвестен"):
            self.store.create_booking(**booking())
        self.assertTrue(self.cell(2, 2).startswith("Запись "))
        self.bookings().fail_write = False
        with self.assertRaises(MappingError):
            self.store.create_booking(**booking())

    def test_write_that_landed_before_error_is_reconciled_as_success(self):
        original = self.bookings().batch_update

        def landed_then_failed(updates, value_input_option=None):
            original(updates, value_input_option=value_input_option)
            raise OSError("timeout after server applied the write")

        self.bookings().batch_update = landed_then_failed
        result = self.store.create_booking(**booking())
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.cell(2, 2), f"Запись {result['booking_id']}")

    def test_time_outside_grid_is_unavailable(self):
        result = self.store.create_booking(**booking(time_str="12:00"))
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.bookings().values), 1)

    def test_concurrent_requests_for_last_bay_produce_one_booking(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store, spreadsheet, grid = grid_store(temp.name, [["10:00", ""]], bays=("Пост 1",))
        results = []

        def attempt(number):
            results.append(store.create_booking(**booking(telegram_id=1000 + number, car=f"Car {number}")))

        threads = [threading.Thread(target=attempt, args=(number,)) for number in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for result in results if result["ok"]), 1)
        self.assertEqual(len(spreadsheet.worksheet("Записи").values), 2)
        self.assertTrue(grid.values[1][1].startswith("Запись "))

    def test_formula_in_bay_cell_makes_grid_read_only_without_writes(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store, spreadsheet, grid = grid_store(temp.name, [["10:00", "=1+1", ""]])
        with self.assertRaises(MappingError):
            store.create_booking(**booking())
        self.assertEqual(grid.values[1][1], "=1+1")
        self.assertEqual(len(spreadsheet.worksheet("Записи").values), 1)

    def test_duplicate_time_rows_are_refused(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store, spreadsheet, grid = grid_store(temp.name, [["10:00", ""], ["10:00", ""]])
        with self.assertRaises(MappingError):
            store.create_booking(**booking())
        self.assertEqual(len(spreadsheet.worksheet("Записи").values), 1)

    def test_unrelated_cells_outside_grid_are_preserved(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        def add_note(grid_sheet):
            grid_sheet.values[0].append("Заметка")
            grid_sheet.values[1].append("=SUM(1,2)")

        store, spreadsheet, grid = grid_store(temp.name, [["10:00", "", ""], ["11:00", "", ""]], extra=add_note)
        result = store.create_booking(**booking())
        self.assertTrue(result["ok"], result)
        self.assertEqual(grid.values[0][3], "Заметка")
        self.assertEqual(grid.values[1][3], "=SUM(1,2)")
        self.assertTrue(grid.values[1][1].startswith("Запись "))
        self.assertEqual(grid.values[1][0], "10:00")

    def test_capacity_helper_treats_grid_counts_as_free_counts(self):
        slots = {("2099-01-01", "10:00"): FreeCapacity(1)}
        self.assertEqual(self.store._capacity_left(slots, {("2099-01-01", "10:00"): 5}, ("2099-01-01", "10:00")), 1)
    # ---- отказы на промежуточных шагах: клетка и журнал не должны терять доказательства

    def fail_after_apply(self, worksheet, matches=lambda updates: True):
        """Первый подходящий batch_update применяется, затем ответ теряется."""
        original = worksheet.batch_update

        def wrapped(updates, value_input_option=None):
            if matches(updates):
                worksheet.batch_update = original
                original(updates, value_input_option=value_input_option)
                raise OSError("response lost after apply")
            return original(updates, value_input_option=value_input_option)

        worksheet.batch_update = wrapped
        return original

    def journal_writer(self, fail_at):
        calls = []

        def write(path, data):
            if str(path).endswith("-operations.json"):
                calls.append(path)
                if len(calls) == fail_at:
                    raise OSError("journal write failed")
            return real_atomic_json(path, data)

        real_atomic_json = adaptive.atomic_json
        return write

    def test_claim_failure_before_write_keeps_no_marker_and_blocks_retry(self):
        self.grid.fail_write = True
        with self.assertRaises(OSError):
            self.store.create_booking(**booking())
        self.assertEqual(self.cell(2, 2), "")
        self.assertEqual(len(self.bookings().values), 1)
        # Журнал уже записан до занятия клетки: повтор требует ручной сверки.
        with self.assertRaisesRegex(MappingError, "повтор заблокирован"):
            self.store.create_booking(**booking())

    def test_claim_failure_after_write_keeps_marker_and_blocks_retry(self):
        self.fail_after_apply(self.grid)
        with self.assertRaises(OSError):
            self.store.create_booking(**booking())
        self.assertTrue(self.cell(2, 2).startswith("Запись "))
        self.assertEqual(len(self.bookings().values), 1)
        with self.assertRaisesRegex(MappingError, "повтор заблокирован"):
            self.store.create_booking(**booking())

    def test_journal_failure_before_claim_leaves_no_marker_and_allows_retry(self):
        with patch.object(adaptive, "atomic_json", side_effect=self.journal_writer(1)):
            with self.assertRaises(OSError):
                self.store.create_booking(**booking())
        self.assertEqual(self.cell(2, 2), "")
        self.assertEqual(len(self.bookings().values), 1)
        self.assertTrue(self.store.create_booking(**booking())["ok"])

    def test_journal_failure_after_confirmed_row_is_not_reported_as_failure(self):
        with patch.object(adaptive, "atomic_json", side_effect=self.journal_writer(2)):
            result = self.store.create_booking(**booking())
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.cell(2, 2).startswith(f"Запись {result['booking_id']}"))
        self.assertEqual(len(self.bookings().values), 2)

    def test_reschedule_claim_failure_after_write_keeps_original_row_and_blocks_retry(self):
        created = self.store.create_booking(**booking())
        self.fail_after_apply(self.grid)
        with self.assertRaises(OSError):
            self.store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "11:00")
        self.assertEqual(self.cell(2, 2), f"Запись {created['booking_id']}")
        self.assertEqual(self.cell(3, 2), f"Запись {created['booking_id']}")
        self.assertEqual(self.bookings().values[1][3], "10:00")
        with self.assertRaisesRegex(MappingError, "ручная сверка"):
            self.store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "11:00")

    def test_reschedule_row_failure_before_write_is_completed_on_retry(self):
        created = self.store.create_booking(**booking())
        self.bookings().fail_write = True
        with self.assertRaisesRegex(MappingError, "неизвестен"):
            self.store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "11:00")
        self.assertEqual(self.bookings().values[1][3], "10:00")
        self.assertEqual(self.cell(2, 2), f"Запись {created['booking_id']}")
        self.assertEqual(self.cell(3, 2), f"Запись {created['booking_id']}")
        # Журнал и клетка доказывают план: повтор доводит перенос без нового занятия.
        self.bookings().fail_write = False
        resumed = self.store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "11:00")
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(self.bookings().values[1][3], "11:00")
        self.assertEqual(self.cell(2, 2), "")
        self.assertEqual(self.cell(3, 2), f"Запись {created['booking_id']}")

    def test_reschedule_old_bay_release_failure_keeps_moved_row_and_old_marker(self):
        created = self.store.create_booking(**booking())
        original = self.grid.batch_update

        def fail_clear(updates, value_input_option=None):
            if updates == [{"range": "B2", "values": [[""]]}]:
                raise OSError("release failed")
            return original(updates, value_input_option=value_input_option)

        self.grid.batch_update = fail_clear
        moved = self.store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "11:00")
        self.assertTrue(moved["ok"], moved)
        self.assertIs(moved.get("grid_released"), False)  # администратор увидит предупреждение
        self.assertEqual(self.bookings().values[1][3], "11:00")
        self.assertEqual(self.cell(3, 2), f"Запись {created['booking_id']}")
        self.assertEqual(self.cell(2, 2), f"Запись {created['booking_id']}")  # остаётся до ручной сверки

    def test_cancel_status_failure_before_write_keeps_active_row_and_retries(self):
        created = self.store.create_booking(**booking())
        self.bookings().fail_write = True
        with self.assertRaisesRegex(MappingError, "неизвестен"):
            self.store.cancel_booking(created["booking_id"], 111)
        self.assertEqual(self.bookings().values[1][-1], "Новая")
        self.assertEqual(self.cell(2, 2), f"Запись {created['booking_id']}")
        self.bookings().fail_write = False
        cancelled = self.store.cancel_booking(created["booking_id"], 111)
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertEqual(self.bookings().values[1][-1], "Отмена")
        self.assertEqual(self.cell(2, 2), "")

    def test_cancel_status_landed_before_error_is_reconciled(self):
        created = self.store.create_booking(**booking())
        self.fail_after_apply(self.bookings())
        cancelled = self.store.cancel_booking(created["booking_id"], 111)
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertIn("слот освобождён", cancelled["message"])
        self.assertEqual(self.bookings().values[1][-1], "Отмена")
        self.assertEqual(self.cell(2, 2), "")

    def test_cancel_release_landed_before_error_is_reported_as_released(self):
        created = self.store.create_booking(**booking())
        self.fail_after_apply(self.grid, lambda updates: updates == [{"range": "B2", "values": [[""]]}])
        cancelled = self.store.cancel_booking(created["booking_id"], 111)
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertIn("слот освобождён", cancelled["message"])
        self.assertEqual(self.cell(2, 2), "")

    def test_cancel_release_failure_keeps_marker_and_warns(self):
        created = self.store.create_booking(**booking())
        original = self.grid.batch_update

        def fail_clear(updates, value_input_option=None):
            if updates == [{"range": "B2", "values": [[""]]}]:
                raise OSError("release failed")
            return original(updates, value_input_option=value_input_option)

        self.grid.batch_update = fail_clear
        cancelled = self.store.cancel_booking(created["booking_id"], 111)
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertIn("не освобождено автоматически", cancelled["message"])
        self.assertEqual(self.cell(2, 2), f"Запись {created['booking_id']}")



if __name__ == "__main__":
    unittest.main()
