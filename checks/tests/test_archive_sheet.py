"""Архивный лист: записи со статусом «Архив» переносятся из «Записей» без дублей по ID."""
import tempfile
import unittest

from sheets import BOOKING_HEADERS
from tests.fakes import fixture


def booking_row(booking_id, day, *, status="Выполнена", name="Никита", car="Toyota Camry",
                services="Мойка кузова", total="700"):
    """Строка листа «Записи» в порядке канонической шапки."""
    values = {"ID": booking_id, "Создано": day, "Дата": day, "Время": "10:00",
              "Имя": name, "Телефон": "89001234567", "Автомобиль": car, "Класс": "Легковой",
              "Тариф": "", "Доп. услуги": services, "Состав": services, "Сумма": total,
              "Комментарий": "", "Telegram ID": "111", "Username": "", "Статус": status}
    return [values.get(header, "") for header in BOOKING_HEADERS]


class ArchiveSheetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)

    def seed(self, *rows):
        for row_values in rows:
            self.spreadsheet.worksheet("Записи").values.append(list(row_values))

    def archive_values(self):
        return self.spreadsheet.worksheet("Архив").get_all_values()

    def test_marked_records_move_with_id_data_and_statuses(self):
        # Прошедшая дата без статуса: archive_old_bookings ставит «Архив», перенос после метки.
        self.seed(booking_row("1", "01.01.2020", status=""),
                  booking_row("2", "15.06.2021", status="Архив", services="Пылесос салона", total="500"),
                  booking_row("3", "01.01.2099", status="Новая"),   # будущая: не архив
                  booking_row("4", "01.01.2099", status="Выполнена"),
                  booking_row("5", "01.01.2099", status="Отмена"))
        self.assertEqual(self.store.archive_old_bookings(), 1)
        self.assertEqual(self.store.move_archived_bookings(), 2)
        archive = self.archive_values()
        self.assertEqual([str(header).strip() for header in archive[0]], list(BOOKING_HEADERS))
        self.assertEqual([row[0] for row in archive[1:]], ["1", "2"])
        by_id = {row[0]: row for row in archive[1:]}
        self.assertEqual(by_id["1"][BOOKING_HEADERS.index("Дата")], "01.01.2020")
        self.assertEqual(by_id["1"][BOOKING_HEADERS.index("Автомобиль")], "Toyota Camry")
        self.assertEqual(by_id["1"][BOOKING_HEADERS.index("Статус")], "Архив")
        self.assertEqual(by_id["2"][BOOKING_HEADERS.index("Доп. услуги")], "Пылесос салона")
        self.assertEqual(by_id["2"][BOOKING_HEADERS.index("Сумма")], "500")
        # Не-архивные статусы и будущая запись остаются только в «Записях».
        self.assertNotIn("3", {row[0] for row in archive[1:]})
        self.assertNotIn("4", {row[0] for row in archive[1:]})
        self.assertNotIn("5", {row[0] for row in archive[1:]})
        self.assertEqual(len(self.spreadsheet.worksheet("Записи").values), 6)

    def test_repeated_runs_do_not_duplicate(self):
        self.seed(booking_row("1", "01.01.2020", status="Архив"),
                  booking_row("2", "15.06.2021", status="Архив"))
        self.assertEqual(self.store.move_archived_bookings(), 2)
        self.assertEqual(self.store.move_archived_bookings(), 0)
        self.assertEqual(len(self.archive_values()), 3)  # шапка + 2 записи

    def test_new_marked_record_archived_on_later_run(self):
        self.seed(booking_row("1", "01.01.2020", status="Архив"))
        self.assertEqual(self.store.move_archived_bookings(), 1)
        self.seed(booking_row("2", "02.01.2020", status="Архив"))
        self.assertEqual(self.store.move_archived_bookings(), 1)
        self.assertEqual([row[0] for row in self.archive_values()[1:]], ["1", "2"])

    def test_other_statuses_and_fresh_dates_are_not_moved(self):
        self.seed(booking_row("1", "01.01.2020", status="Выполнена"),
                  booking_row("2", "01.01.2020", status="Отмена"),
                  booking_row("3", "01.01.2099", status="Новая"))
        self.assertEqual(self.store.move_archived_bookings(), 0)
        self.assertFalse(any(sheet.title == "Архив" for sheet in self.spreadsheet.sheets))

    def test_legacy_non_numeric_id_is_archived_and_deduped(self):
        self.seed(booking_row("наследство", "01.01.2020", status="Архив"))
        self.assertEqual(self.store.move_archived_bookings(), 1)
        self.assertEqual(self.store.move_archived_bookings(), 0)
        self.assertEqual([row[0] for row in self.archive_values()[1:]], ["наследство"])


if __name__ == "__main__":
    unittest.main()
