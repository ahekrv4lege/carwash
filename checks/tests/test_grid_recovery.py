"""Восстановление операций сетки постов: журнал, сверка, повтор после рестарта.

Все раскладки синтетические; «рестарт» — новый Store над тем же mappings-файлом
и той же таблицей. Чужие клетки и ручные пометки не трогаются.
"""
import json
import os
import tempfile
import unittest
from itertools import count
from unittest.mock import patch

from adaptive import MappingError, fingerprint
from tests.fakes import booking, fixture

_DIRS = count(1)


def sub(temp):
    path = os.path.join(temp, f"store-{next(_DIRS)}")
    os.makedirs(path, exist_ok=True)
    return path


def grid_fixture(temp):
    store, spreadsheet = fixture(sub(temp))
    grid = spreadsheet.worksheet("Слоты")
    grid.title = "01.01.2099"
    grid.values = [["Время", "Пост 1", "Пост 2"], ["10:00", "", ""], ["11:00", "", ""]]
    store._invalidate("slots", "bookings")
    return store, spreadsheet, grid


class RestartTestCase(unittest.TestCase):
    def restart(self, store, spreadsheet):
        """Новый Store над тем же mappings-файлом и той же таблицей."""
        restored, _ = fixture(os.path.dirname(store._adaptive.path.as_posix()) or sub(self.temp.name))
        restored._spreadsheet = spreadsheet
        restored._invalidate("catalog", "slots", "bookings")
        return restored

    def journal(self, store):
        path = store._adaptive.path.with_name(store._adaptive.path.stem + "-operations.json")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


class InterruptedCancelTests(RestartTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_cancel_marked_in_sheet_but_cell_left_is_released_on_retry(self):
        store, spreadsheet, grid = grid_fixture(self.temp.name)
        created = store.create_booking(**booking())
        marker = f"Запись {created['booking_id']}"
        # Отмена применилась, процесс умер до освобождения клетки.
        spreadsheet.worksheet("Записи").values[1][-1] = "Отмена"
        store._invalidate("bookings")
        answer = store.cancel_booking(created["booking_id"], 111)
        self.assertIn("место в сетке освобождено", answer["error"])
        self.assertEqual(grid.values[1][1], "")
        self.assertEqual(spreadsheet.worksheet("Записи").values[1][-1], "Отмена")

    def test_pending_cancel_journal_survives_restart(self):
        store, spreadsheet, grid = grid_fixture(self.temp.name)
        created = store.create_booking(**booking())
        marker = f"Запись {created['booking_id']}"
        # Отмена применилась, процесс умер до освобождения клетки и до закрытия журнала.
        spreadsheet.worksheet("Записи").values[1][-1] = "Отмена"
        key = fingerprint({"cancel": str(created["booking_id"]), "sheet": 4, "row": 2})
        path = store._adaptive.path.with_name(store._adaptive.path.stem + "-operations.json")
        path.write_text(json.dumps({key: {"state": "pending", "marker": marker, "sheet_id": 3, "row": 2, "at": 0.0}}),
                        encoding="utf-8")
        restored = self.restart(store, spreadsheet)
        restored._invalidate("bookings")
        answer = restored.cancel_booking(created["booking_id"], 111)
        self.assertIn("место в сетке освобождено", answer["error"])
        self.assertEqual(grid.values[1][1], "")
        self.assertEqual(self.journal(restored)[key]["state"], "done")

    def test_unknown_cancel_outcome_keeps_journal_then_reconciles(self):
        store, spreadsheet, grid = grid_fixture(self.temp.name)
        created = store.create_booking(**booking())
        marker = f"Запись {created['booking_id']}"
        spreadsheet.worksheet("Записи").fail_write = True
        with patch.object(store._adaptive, "_booking_row_state", return_value=None):
            with self.assertRaisesRegex(MappingError, "отмены неизвестен"):
                store.cancel_booking(created["booking_id"], 111)
        spreadsheet.worksheet("Записи").fail_write = False
        self.assertEqual(grid.values[1][1], marker)  # клетка ждёт сверки
        journal = self.journal(store)
        pending = [entry for entry in journal.values() if entry.get("marker") == marker]
        self.assertEqual([entry["state"] for entry in pending], ["pending"])
        restored = self.restart(store, spreadsheet)
        restored._invalidate("bookings")
        # Запись всё ещё активна (статус не применился): рестарт спокойно завершает отмену.
        answer = restored.cancel_booking(created["booking_id"], 111)
        self.assertTrue(answer["ok"], answer)
        self.assertIn("слот освобождён", answer["message"])
        self.assertEqual(grid.values[1][1], "")
        pending = [entry for entry in self.journal(restored).values() if entry.get("marker") == marker]
        self.assertEqual([entry["state"] for entry in pending], ["done"])

    def test_cancelled_booking_without_grid_stays_unchanged(self):
        store, spreadsheet, grid = grid_fixture(self.temp.name)
        grid.title = "Слоты"
        grid.values = [["Дата", "Время", "Постов"], ["01.01.2099", "10:00", "2"]]
        store._invalidate("slots", "bookings")
        created = store.create_booking(**booking())
        spreadsheet.worksheet("Записи").values[1][-1] = "Отмена"
        store._invalidate("bookings")
        answer = store.cancel_booking(created["booking_id"], 111)
        self.assertEqual(answer["error"], "Эта запись уже отменена")


class GridAuditTests(RestartTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet, self.grid = grid_fixture(self.temp.name)

    def bookings(self):
        return self.spreadsheet.worksheet("Записи")

    def test_stale_marker_of_cancelled_booking_is_cleared(self):
        created = self.store.create_booking(**booking())
        self.assertTrue(self.store.cancel_booking(created["booking_id"], 111)["ok"])
        self.grid.values[1][1] = f"Запись {created['booking_id']}"  # авария после отмены
        report = self.store.grid_maintenance()
        self.assertEqual(report["cleared"], [{"day": "01.01.2099", "cell": "B2",
                                              "booking_id": str(created["booking_id"]),
                                              "reason": "запись отменена"}])
        self.assertEqual(self.grid.values[1][1], "")

    def test_marker_without_booking_is_only_reported(self):
        self.grid.values[1][1] = "Запись 999"
        report = self.store.grid_maintenance()
        self.assertEqual(report["cleared"], [])
        self.assertEqual(report["ambiguous"][0]["reason"], "запись не найдена или дублируется")
        self.assertEqual(self.grid.values[1][1], "Запись 999")

    def test_manual_note_is_untouched(self):
        self.grid.values[1][1] = "занято вручную"
        report = self.store.grid_maintenance()
        self.assertEqual(report["cleared"], [])
        self.assertEqual(self.grid.values[1][1], "занято вручную")

    def test_active_booking_without_marker_is_reported_not_invented(self):
        self.store.create_booking(**booking())
        self.grid.values[1][1] = ""
        report = self.store.grid_maintenance()
        self.assertEqual(report["missing"], [{"day": "01.01.2099", "booking_id": "1",
                                              "reason": "у активной записи нет клетки в сетке"}])
        self.assertEqual(report["cleared"], [])

    def test_marker_on_wrong_cell_is_cleared_only_with_journal_evidence(self):
        created = self.store.create_booking(**booking())  # клетка B2
        self.store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "11:00")
        self.grid.values[1][1] = f"Запись {created['booking_id']}"  # старая клетка осталась
        report = self.store.grid_maintenance()
        self.assertEqual(report["cleared"][0]["reason"], "перенос подтверждён журналом")
        self.assertEqual(self.grid.values[1][1], "")
        self.assertEqual(self.grid.values[2][1], f"Запись {created['booking_id']}")


class RescheduleRecoveryTests(RestartTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_lost_release_after_move_is_completed_by_audit(self):
        store, spreadsheet, grid = grid_fixture(self.temp.name)
        created = store.create_booking(**booking())
        original = grid.batch_update

        def fail_release(updates, value_input_option=None):
            if updates == [{"range": "B2", "values": [[""]]}]:
                raise OSError("release failed")
            return original(updates, value_input_option=value_input_option)

        grid.batch_update = fail_release
        moved = store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "11:00")
        grid.batch_update = original
        self.assertTrue(moved["ok"], moved)
        self.assertEqual(grid.values[2][1], f"Запись {created['booking_id']}")
        self.assertEqual(grid.values[1][1], f"Запись {created['booking_id']}")  # авария
        restored = self.restart(store, spreadsheet)
        report = restored.grid_maintenance()
        self.assertEqual(report["cleared"][0]["reason"], "перенос подтверждён журналом")
        self.assertEqual(grid.values[1][1], "")

    def test_claim_cell_taken_by_someone_else_stays_blocked(self):
        store, spreadsheet, grid = grid_fixture(self.temp.name)
        created = store.create_booking(**booking())
        bookings_sheet = spreadsheet.worksheet("Записи")
        bookings_sheet.fail_write = True
        with self.assertRaisesRegex(MappingError, "неизвестен"):
            store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "11:00")
        grid.values[2][1] = "Запись 777"  # клетку успел занять другой
        bookings_sheet.fail_write = False
        with self.assertRaisesRegex(MappingError, "ручная сверка"):
            store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "11:00")
        self.assertEqual(bookings_sheet.values[1][3], "10:00")
        self.assertEqual(grid.values[1][1], f"Запись {created['booking_id']}")  # исходная запись цела

    def test_repeated_reschedule_is_idempotent(self):
        store, spreadsheet, grid = grid_fixture(self.temp.name)
        created = store.create_booking(**booking())
        self.assertTrue(store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "11:00")["ok"])
        again = store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "11:00")
        self.assertFalse(again["ok"])
        self.assertIn("уже назначена", again["error"])
        self.assertEqual(grid.values[2][1], f"Запись {created['booking_id']}")
        self.assertEqual(grid.values[1][1], "")
        third = store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "10:00")
        self.assertTrue(third["ok"], third)
        self.assertEqual(str(third["booking_id"]), str(created["booking_id"]))
        self.assertEqual(grid.values[1][1], f"Запись {created['booking_id']}")
        self.assertEqual(grid.values[2][1], "")

    def test_capacity_schedule_is_unaffected_by_grid_recovery(self):
        store, spreadsheet, grid = grid_fixture(self.temp.name)
        grid.title = "Слоты"
        grid.values = [["Дата", "Время", "Постов"], ["01.01.2099", "10:00", "1"]]
        store._invalidate("slots", "bookings")
        created = store.create_booking(**booking())
        self.assertTrue(created["ok"])
        report = store.grid_maintenance()
        self.assertEqual(report["checked"], 0)  # сеток нет: сверка не трогает таблицу
        self.assertEqual(store.free_slots("2099-01-01")["days"][0]["free_times"], [])


if __name__ == "__main__":
    unittest.main()
