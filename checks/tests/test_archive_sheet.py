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

    def test_marked_records_move_and_sources_are_deleted(self):
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
        # Источники удалены физически: без пустых рядов, только живые записи.
        values = self.bookings_sheet().values
        self.assertEqual([row[0] for row in values[1:]], ["3", "4", "5"])
        self.assertTrue(all(any(str(cell).strip() for cell in row) for row in values[1:]))

    def test_repeated_runs_do_not_duplicate(self):
        self.seed(booking_row("1", "01.01.2020", status="Архив"),
                  booking_row("2", "15.06.2021", status="Архив"))
        self.assertEqual(self.store.move_archived_bookings(), 2)
        self.assertEqual(self.store.move_archived_bookings(), 0)
        self.assertEqual(len(self.archive_values()), 3)  # шапка + 2 записи
        self.assertEqual(self.bookings_sheet().values, [list(BOOKING_HEADERS)])

    def test_new_marked_record_archived_on_later_run(self):
        self.seed(booking_row("1", "01.01.2020", status="Архив"))
        self.assertEqual(self.store.move_archived_bookings(), 1)
        self.seed(booking_row("2", "02.01.2020", status="Архив"))
        self.assertEqual(self.store.move_archived_bookings(), 1)
        self.assertEqual([row[0] for row in self.archive_values()[1:]], ["1", "2"])
        self.assertEqual(self.bookings_sheet().values, [list(BOOKING_HEADERS)])

    def test_other_statuses_and_fresh_dates_are_not_moved(self):
        self.seed(booking_row("1", "01.01.2020", status="Выполнена"),
                  booking_row("2", "01.01.2020", status="Отмена"),
                  booking_row("3", "01.01.2099", status="Новая"))
        self.assertEqual(self.store.move_archived_bookings(), 0)
        self.assertFalse(any(sheet.title == "Архив" for sheet in self.spreadsheet.sheets))
        self.assertEqual([row[0] for row in self.bookings_sheet().values[1:]], ["1", "2", "3"])

    def test_pre_existing_gaps_are_swept_even_without_new_moves(self):
        # Дырки от прежних чисток убираются при каждом цикле, даже если переносить нечего.
        self.spreadsheet.worksheet("Записи").values.append(booking_row("1", "01.01.2099", status="Новая"))
        self.spreadsheet.worksheet("Записи").values.append([""] * len(BOOKING_HEADERS))
        self.spreadsheet.worksheet("Записи").values.append(booking_row("2", "02.01.2099", status="Новая"))
        self.assertEqual(self.store.move_archived_bookings(), 0)
        self.assertEqual([row[0] for row in self.bookings_sheet().values[1:]], ["1", "2"])

    def test_legacy_non_numeric_id_is_archived_and_source_deleted(self):
        self.seed(booking_row("наследство", "01.01.2020", status="Архив"))
        self.assertEqual(self.store.move_archived_bookings(), 1)
        self.assertEqual([row[0] for row in self.archive_values()[1:]], ["наследство"])
        self.assertEqual(self.bookings_sheet().values, [list(BOOKING_HEADERS)])

    def test_formulas_block_deletion_but_copy_still_happens(self):
        # В листе формулы: удаление строк опасно (сдвинет чужие ссылки),
        # копия в архив всё равно выполняется, источник остаётся как был.
        self.seed(booking_row("1", "01.01.2020", status="Архив"))
        self.bookings_sheet().values[1][BOOKING_HEADERS.index("Сумма")] = "=C2*2"
        self.assertEqual(self.store.move_archived_bookings(), 1)
        self.assertEqual(self.archive_values()[1][0], "1")
        self.assertEqual(self.bookings_sheet().values[1][0], "1")
        self.assertEqual(self.bookings_sheet().values[1][BOOKING_HEADERS.index("Сумма")], "=C2*2")
        # Повтор: дубля в архиве нет.
        self.assertEqual(self.store.move_archived_bookings(), 0)
        self.assertEqual(len(self.archive_values()), 2)


if __name__ == "__main__":
    unittest.main()
