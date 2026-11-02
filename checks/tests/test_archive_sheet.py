"""Архивный лист: записи со статусом «Архив» переносятся из «Записей» без дублей по ID,
источники очищаются адресно — в актуальной таблице остаются только живые записи."""
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

    def bookings_sheet(self):
        return self.spreadsheet.worksheet("Записи")

    def test_marked_records_move_and_sources_are_cleared(self):
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
        # Источники очищены: в «Записях» только живые записи, строки не сдвигались.
        values = self.bookings_sheet().values
        self.assertEqual(len(values), 6)
        for row_number in (1, 2):
            self.assertFalse(any(str(cell).strip() for cell in values[row_number]),
                             f"строка {row_number + 1} не очищена")
        for row_number, booking_id in ((3, "3"), (4, "4"), (5, "5")):
            self.assertEqual(values[row_number][0], booking_id)

    def test_repeated_runs_do_not_duplicate(self):
        self.seed(booking_row("1", "01.01.2020", status="Архив"),
                  booking_row("2", "15.06.2021", status="Архив"))
        self.assertEqual(self.store.move_archived_bookings(), 2)
        self.assertEqual(self.store.move_archived_bookings(), 0)
        self.assertEqual(len(self.archive_values()), 3)  # шапка + 2 записи
        self.assertTrue(all(not any(str(cell).strip() for cell in self.bookings_sheet().values[row])
                            for row in (1, 2)))

    def test_new_marked_record_archived_on_later_run(self):
        self.seed(booking_row("1", "01.01.2020", status="Архив"))
        self.assertEqual(self.store.move_archived_bookings(), 1)
        self.seed(booking_row("2", "02.01.2020", status="Архив"))
        self.assertEqual(self.store.move_archived_bookings(), 1)
        self.assertEqual([row[0] for row in self.archive_values()[1:]], ["1", "2"])
        self.assertTrue(all(not any(str(cell).strip() for cell in self.bookings_sheet().values[row])
                            for row in (1, 2)))

    def test_other_statuses_and_fresh_dates_are_not_moved(self):
        self.seed(booking_row("1", "01.01.2020", status="Выполнена"),
                  booking_row("2", "01.01.2020", status="Отмена"),
                  booking_row("3", "01.01.2099", status="Новая"))
        self.assertEqual(self.store.move_archived_bookings(), 0)
        self.assertFalse(any(sheet.title == "Архив" for sheet in self.spreadsheet.sheets))
        self.assertEqual([row[0] for row in self.bookings_sheet().values[1:]], ["1", "2", "3"])

    def test_legacy_non_numeric_id_is_archived_and_source_cleared(self):
        self.seed(booking_row("наследство", "01.01.2020", status="Архив"))
        self.assertEqual(self.store.move_archived_bookings(), 1)
        self.assertEqual([row[0] for row in self.archive_values()[1:]], ["наследство"])
        self.assertFalse(any(str(cell).strip() for cell in self.bookings_sheet().values[1]))

    def test_formula_row_is_copied_but_source_stays(self):
        # Формулу в источнике не затираем: копия в архиве есть, строка остаётся с пометкой.
        self.seed(booking_row("1", "01.01.2020", status="Архив"))
        self.bookings_sheet().values[1][BOOKING_HEADERS.index("Сумма")] = "=C2*2"
        self.assertEqual(self.store.move_archived_bookings(), 1)
        self.assertEqual(self.archive_values()[1][0], "1")
        self.assertEqual(self.bookings_sheet().values[1][0], "1")
        self.assertEqual(self.bookings_sheet().values[1][BOOKING_HEADERS.index("Сумма")], "=C2*2")
        # Повтор: дубля в архиве нет, строка по-прежнему не очищается.
        self.assertEqual(self.store.move_archived_bookings(), 0)
        self.assertEqual(len(self.archive_values()), 2)


if __name__ == "__main__":
    unittest.main()
