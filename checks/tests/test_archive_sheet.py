"""Архивный лист: записи старше недели переносятся из «Записей» без дублей по ID."""
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

    def test_aged_records_move_with_id_data_services_and_statuses(self):
        self.seed(booking_row("1", "01.01.2020", status="Выполнена"),
                  booking_row("2", "15.06.2021", status="Отменена", services="Пылесос салона", total="500"),
                  booking_row("3", "01.01.2099", status="Новая"))  # свежая — в архив не идёт
        self.assertEqual(self.store.move_aged_bookings_to_archive(), 2)
        archive = self.archive_values()
        self.assertEqual([str(header).strip() for header in archive[0]], list(BOOKING_HEADERS))
        self.assertEqual([row[0] for row in archive[1:]], ["1", "2"])
        by_id = {row[0]: row for row in archive[1:]}
        # id, данные, услуги и статус переносятся как были.
        self.assertEqual(by_id["1"][BOOKING_HEADERS.index("Дата")], "01.01.2020")
        self.assertEqual(by_id["1"][BOOKING_HEADERS.index("Автомобиль")], "Toyota Camry")
        self.assertEqual(by_id["1"][BOOKING_HEADERS.index("Статус")], "Выполнена")
        self.assertEqual(by_id["2"][BOOKING_HEADERS.index("Статус")], "Отмена")
        self.assertEqual(by_id["2"][BOOKING_HEADERS.index("Доп. услуги")], "Пылесос салона")
        self.assertEqual(by_id["2"][BOOKING_HEADERS.index("Сумма")], "500")
        # Свежая запись осталась только в «Записях».
        self.assertNotIn("3", {row[0] for row in archive[1:]})
        self.assertEqual(len(self.spreadsheet.worksheet("Записи").values), 4)

    def test_repeated_runs_do_not_duplicate(self):
        self.seed(booking_row("1", "01.01.2020"), booking_row("2", "15.06.2021", status="Отменена"))
        self.assertEqual(self.store.move_aged_bookings_to_archive(), 2)
        self.assertEqual(self.store.move_aged_bookings_to_archive(), 0)
        self.assertEqual(len(self.archive_values()), 3)  # шапка + 2 записи

    def test_full_flow_marking_then_move_keeps_statuses_without_dupes(self):
        self.seed(booking_row("7", "01.01.2020", status=""))  # прошедшая без статуса
        self.assertEqual(self.store.archive_old_bookings(), 1)  # сначала помечается «Архив»
        self.assertEqual(self.store.move_aged_bookings_to_archive(), 1)
        archive = self.archive_values()
        self.assertEqual(archive[1][BOOKING_HEADERS.index("Статус")], "Архив")
        self.assertEqual(self.store.archive_old_bookings(), 0)
        self.assertEqual(self.store.move_aged_bookings_to_archive(), 0)
        self.assertEqual(len(self.archive_values()), 2)

    def test_new_aged_record_archived_on_later_run(self):
        self.seed(booking_row("1", "01.01.2020"))
        self.assertEqual(self.store.move_aged_bookings_to_archive(), 1)
        self.seed(booking_row("2", "02.01.2020", status="Отменена"))
        self.assertEqual(self.store.move_aged_bookings_to_archive(), 1)
        self.assertEqual([row[0] for row in self.archive_values()[1:]], ["1", "2"])

    def test_legacy_non_numeric_id_is_archived_and_deduped(self):
        self.seed(booking_row("наследство", "01.01.2020"))
        self.assertEqual(self.store.move_aged_bookings_to_archive(), 1)
        self.assertEqual(self.store.move_aged_bookings_to_archive(), 0)
        self.assertEqual([row[0] for row in self.archive_values()[1:]], ["наследство"])

    def test_recent_records_within_week_are_not_moved(self):
        self.seed(booking_row("1", "02.11.2026", status="Выполнена"))
        self.assertEqual(self.store.move_aged_bookings_to_archive(), 0)
        self.assertFalse(any(sheet.title == "Архив" for sheet in self.spreadsheet.sheets))


if __name__ == "__main__":
    unittest.main()
