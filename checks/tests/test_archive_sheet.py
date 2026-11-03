"""Архивный лист: записи со статусами «Архив» и «Отмена» переносятся из «Записей»
без дублей (по неизменяемым полям), источники очищаются адресно, колонка «ID»
перенумеровывается по порядку строк, архив раскрашивается: отмены — красным,
остальной архив — серым. В актуальной таблице остаются только живые записи."""
import tempfile
import unittest

from sheets import BOOKING_HEADERS, Store
from tests.fakes import Spreadsheet, Worksheet, config, fixture


def booking_row(booking_id, day, *, status="Выполнена", name="Никита", car="Toyota Camry",
                services="Мойка кузова", total="700"):
    """Строка листа «Записи» в порядке канонической шапки."""
    values = {"ID": booking_id, "Создано": day, "Дата": day, "Время": "10:00",
              "Имя": name, "Телефон": "89001234567", "Автомобиль": car, "Класс": "Легковой",
              "Тариф": "", "Доп. услуги": services, "Состав": services, "Сумма": total,
              "Комментарий": "", "Telegram ID": "111", "Username": "", "Статус": status}
    return [values.get(header, "") for header in BOOKING_HEADERS]


def offline_fixture(path):
    """Офлайн-магазин: перенумерация ID выполняется только вне адаптива."""
    cfg = config(path, sheets_adaptive_enabled=False)
    spreadsheet = Spreadsheet([
        Worksheet(1, "Услуги", [["Услуга", "Описание", "Легковой", "Доступна"],
                                ["Мойка", "Кузов", "700", "да"]]),
        Worksheet(2, "Тарифы", [["Тариф", "Состав", "Скидка %", "Доступен"], ["Базовый", "Мойка", "0", "да"]]),
        Worksheet(3, "Слоты", [["Дата", "Время", "Постов"], ["01.01.2099", "10:00", "2"]]),
        Worksheet(4, "Записи", [BOOKING_HEADERS])])
    store = Store(cfg)
    store._spreadsheet = spreadsheet
    return store, spreadsheet


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
                  booking_row("5", "01.01.2099", status="Отмена"))  # отмена уезжает сразу
        self.assertEqual(self.store.archive_old_bookings(), 1)
        self.assertEqual(self.store.move_archived_bookings(), 3)
        archive = self.archive_values()
        self.assertEqual([str(header).strip() for header in archive[0]], list(BOOKING_HEADERS))
        self.assertEqual([row[0] for row in archive[1:]], ["1", "2", "5"])
        by_id = {row[0]: row for row in archive[1:]}
        self.assertEqual(by_id["1"][BOOKING_HEADERS.index("Дата")], "01.01.2020")
        self.assertEqual(by_id["1"][BOOKING_HEADERS.index("Автомобиль")], "Toyota Camry")
        self.assertEqual(by_id["1"][BOOKING_HEADERS.index("Статус")], "Архив")
        self.assertEqual(by_id["2"][BOOKING_HEADERS.index("Доп. услуги")], "Пылесос салона")
        self.assertEqual(by_id["2"][BOOKING_HEADERS.index("Сумма")], "500")
        self.assertEqual(by_id["5"][BOOKING_HEADERS.index("Статус")], "Отмена")
        # Источники удалены физически: без пустых рядов, только живые записи.
        values = self.bookings_sheet().values
        self.assertEqual([row[0] for row in values[1:]], ["3", "4"])
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
        # «Выполнена» без пометки «Архив» и будущая «Новая» остаются, отмена уезжает.
        self.assertEqual(self.store.move_archived_bookings(), 1)
        self.assertEqual([row[0] for row in self.archive_values()[1:]], ["2"])
        self.assertEqual([row[0] for row in self.bookings_sheet().values[1:]], ["1", "3"])

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

    # ---------- раскраска архива ----------

    def painted_colors(self, sheet):
        """Строка листа -> цвет фона из вызовов format."""
        colors = {}
        for ranges, cell_format in sheet.format_log:
            background = cell_format["backgroundColor"]
            color = (round(background["red"], 2), round(background["green"], 2), round(background["blue"], 2))
            for cell_range in ranges:
                colors[cell_range] = color
        return colors

    def test_archive_is_painted_red_for_cancellations_gray_for_archive(self):
        self.seed(booking_row("1", "01.01.2099", status="Отмена"),
                  booking_row("2", "01.01.2020", status="Архив"))
        self.assertEqual(self.store.move_archived_bookings(), 2)
        archive = self.spreadsheet.worksheet("Архив")
        colors = self.painted_colors(archive)
        # Строки 2-3 архива: отмена — красная, архив — серый; шапка не красится.
        self.assertEqual(colors["A2:P2"], (0.98, 0.8, 0.8))
        self.assertEqual(colors["A3:P3"], (0.93, 0.93, 0.93))
        self.assertFalse(any(cell_range.startswith("A1:") for cell_range in colors))

    def test_existing_archive_is_painted_even_without_new_moves(self):
        # Архив мог быть наполнен раньше: раскраска выполняется и без нового переноса.
        self.store, self.spreadsheet = offline_fixture(self.temp.name)
        archive = self.spreadsheet.add_worksheet("Архив", rows=100, cols=16)
        archive.append_row(list(BOOKING_HEADERS), value_input_option="RAW")
        archive.append_row(booking_row("1", "01.01.2020", status="Отмена"), value_input_option="RAW")
        archive.append_row(booking_row("2", "01.01.2020", status="Архив"), value_input_option="RAW")
        self.seed(booking_row("3", "01.01.2099", status="Новая"))
        self.assertEqual(self.store.move_archived_bookings(), 0)
        colors = self.painted_colors(self.spreadsheet.worksheet("Архив"))
        self.assertEqual(colors["A2:P2"], (0.98, 0.8, 0.8))
        self.assertEqual(colors["A3:P3"], (0.93, 0.93, 0.93))

    # ---------- нумерация «Записей» после переноса ----------

    def test_reused_id_with_fresh_content_is_copied_not_deduped(self):
        # После перенумерации номера в «Записях» начинаются заново и совпадают
        # с давними архивными: новую запись с таким ID архив не должен терять.
        self.store, self.spreadsheet = offline_fixture(self.temp.name)
        archive = self.spreadsheet.add_worksheet("Архив", rows=100, cols=16)
        archive.append_row(list(BOOKING_HEADERS), value_input_option="RAW")
        archive.append_row(booking_row("1", "01.01.2020", status="Архив"), value_input_option="RAW")
        self.seed(booking_row("1", "01.01.2099", status="Отмена", name="Другой клиент"))
        self.assertEqual(self.store.move_archived_bookings(), 1)
        rows = self.archive_values()[1:]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][BOOKING_HEADERS.index("Дата")], "01.01.2099")
        self.assertEqual(rows[1][BOOKING_HEADERS.index("Имя")], "Другой клиент")
        self.assertEqual(self.bookings_sheet().values, [list(BOOKING_HEADERS)])

    def test_identical_rows_are_not_duplicated_by_content(self):
        # Идемпотентность: копия уже в архиве — источник очищается без повторной копии.
        self.store, self.spreadsheet = offline_fixture(self.temp.name)
        archive = self.spreadsheet.add_worksheet("Архив", rows=100, cols=16)
        archive.append_row(list(BOOKING_HEADERS), value_input_option="RAW")
        archive.append_row(booking_row("7", "01.01.2020", status="Отмена"), value_input_option="RAW")
        self.seed(booking_row("7", "01.01.2020", status="Отмена"))
        self.assertEqual(self.store.move_archived_bookings(), 0)
        self.assertEqual(len(self.archive_values()), 2)
        self.assertEqual(self.bookings_sheet().values, [list(BOOKING_HEADERS)])

    def test_moves_renumber_ids_in_bookings_sheet(self):
        # После удаления перенесённых строк нумерация в «Записях» идёт по порядку строк.
        self.store, self.spreadsheet = offline_fixture(self.temp.name)
        self.seed(booking_row("5", "01.01.2020", status="Архив"),
                  booking_row("9", "01.01.2099", status="Новая"),
                  booking_row("12", "01.01.2099", status="Отмена"))
        self.assertEqual(self.store.move_archived_bookings(), 2)  # архив + отмена
        values = self.bookings_sheet().values
        self.assertEqual([row[0] for row in values[1:]], ["1"])
        self.assertEqual(values[1][BOOKING_HEADERS.index("Дата")], "01.01.2099")
        self.assertEqual(values[1][BOOKING_HEADERS.index("Статус")], "Новая")

    # ---------- статистика ----------

    def test_analytics_snapshot_includes_archive_rows(self):
        # Отмены переехали в архив, но метрики периода считают их как раньше.
        self.store, self.spreadsheet = offline_fixture(self.temp.name)
        self.seed(booking_row("1", "01.01.2020", status="Отмена", total="500"))
        self.assertEqual(self.store.move_archived_bookings(), 1)
        rows, _ = self.store.analytics_snapshot()
        self.assertIn("1", [str(row.get("ID", "")) for row in rows])
        cancelled = next(row for row in rows if str(row.get("ID", "")) == "1")
        self.assertEqual(str(cancelled["Статус"]), "Отмена")
        self.assertEqual(str(cancelled["Сумма"]), "500")


if __name__ == "__main__":
    unittest.main()
