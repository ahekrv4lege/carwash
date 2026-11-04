"""Целостность идентификаторов записей и защита листа «Записи».

Нумерация ID — по порядку строк (1..N без пропусков): перенумерация выполняется
при периодическом обслуживании, после хронологической сортировки и после удаления
строк при переносе в архив; операции клиента номера не сдвигают. Адресные операции
находят строку по ID текущей таблицы, формулы и данные вне шапки не разрушаются,
адаптивные раскладки не переписываются глобально.

Синтетические листы из tests/fakes.py умеют worksheet.update и format,
поэтому реальный производственный путь перезаписи (refresh_bookings_sheet)
выполняется, а не пропускается. Только офлайн, сеть не используется.
"""
from copy import deepcopy
import tempfile
import threading
import unittest

from sheets import BOOKING_HEADERS, Store
from adaptive import MappingError
from tests.fakes import Spreadsheet, Worksheet, booking, config as fake_config

HEADERS = BOOKING_HEADERS
ID, CREATED, DATE, TIME, NAME, PHONE, CAR, KLASS, TARIFF, EXTRAS, COMPOSITION, TOTAL, COMMENT, TG, USERNAME, STATUS = range(16)


def row(item_id="", date_str="", time_str="10:00", telegram="111", status="Новая",
        car="BMW X5", created="01.01.2099 09:00"):
    values = [""] * len(HEADERS)
    values[ID] = str(item_id)
    values[CREATED] = created
    values[DATE] = date_str
    values[TIME] = time_str
    values[NAME] = "Synthetic"
    values[PHONE] = "00000000000"
    values[CAR] = car
    values[KLASS] = "Кроссовер"
    values[TOTAL] = "700"
    values[TG] = str(telegram)
    values[STATUS] = status
    return values


def standard_fixture(path, **kwargs):
    """Не-адаптивный магазин с каноническим листом «Записи»."""
    cfg = fake_config(path, sheets_adaptive_enabled=False, **kwargs)
    spreadsheet = Spreadsheet([
        Worksheet(1, "Услуги", [["Услуга", "Описание", "Легковой", "Кроссовер", "Доступна"],
                                ["Мойка", "Кузов", "700", "800", "да"]]),
        Worksheet(2, "Тарифы", [["Тариф", "Состав", "Скидка %", "Доступен"], ["Базовый", "Мойка", "0", "да"]]),
        Worksheet(3, "Слоты", [["Дата", "Время", "Постов"],
                               ["01.01.2099", "09:00", "3"], ["01.01.2099", "10:00", "3"],
                               ["01.01.2099", "11:00", "3"], ["02.01.2099", "10:00", "10"],
                               ["03.01.2099", "10:00", "3"], ["04.01.2099", "10:00", "3"],
                               ["04.01.2099", "11:00", "3"]]),
        Worksheet(4, "Записи", [HEADERS])])
    store = Store(cfg)
    store._spreadsheet = spreadsheet
    return store, spreadsheet


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = standard_fixture(self.temp.name)
        self.sheet = self.spreadsheet.worksheet("Записи")

    def seed(self, *rows):
        self.sheet.values = [list(HEADERS), *(deepcopy(list(row_values)) for row_values in rows)]

    def ids(self):
        return [str(values[ID]) for values in self.sheet.values[1:]]

    def records(self):
        """ID -> (дата, время) по текущему листу."""
        return {str(values[ID]): (str(values[DATE]), str(values[TIME])) for values in self.sheet.values[1:] if str(values[ID]).strip()}

    def create(self, date_str="2099-01-02", time_str="10:00", **kwargs):
        arguments = booking(date_str=date_str, time_str=time_str, **kwargs)
        return self.store.create_booking(**arguments)

    @staticmethod
    def new_id(result):
        return str(result.get("booking_id", ""))

    # ---------- исходный баг: сортировка ломала порядок нумерации ----------

    def test_periodic_sort_keeps_numbering_ascending(self):
        # Хронологический лист с номерами 1 и 2; новая запись получает 3.
        self.seed(row(1, "01.01.2099", "10:00"), row(2, "02.01.2099", "10:00"))
        result = self.create(date_str="2099-01-01", time_str="09:00")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.new_id(result), "3")
        self.assertEqual(self.ids(), ["1", "2", "3"])
        self.assertEqual(self.records()["3"], ("01.01.2099", "09:00"))
        # Периодическая сортировка ставит новую запись на первое место,
        # перенумерация выставляет номера по порядку строк: 1..N по возрастанию.
        self.store.refresh_bookings_sheet(sort=True)
        self.assertEqual(self.ids(), ["1", "2", "3"])
        self.assertEqual(self.records(), {"1": ("01.01.2099", "09:00"),
                                          "2": ("01.01.2099", "10:00"),
                                          "3": ("02.01.2099", "10:00")})
        # Отмена по свежему номеру бьёт в ту же запись (раньше это был ID 3).
        cancelled = self.store.cancel_booking("1", 111)
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertEqual(self.records()["1"], ("01.01.2099", "09:00"))
        self.assertEqual(str(self.sheet.values[1][STATUS]), "Отмена")
        self.assertEqual(self.ids(), ["1", "2", "3"])

    def test_real_rewrite_path_is_exercised(self):
        # Инфраструктура: сортировка действительно выполняет worksheet.update,
        # а не молча пропускается из-за отсутствия метода.
        self.seed(row(1, "03.01.2099", "10:00"), row(2, "01.01.2099", "10:00"))
        self.assertEqual(self.sheet.updates, [])
        self.store.refresh_bookings_sheet(sort=True)
        # Две перезаписи: хронологическая перестановка строк и перенумерация ID.
        self.assertEqual(len(self.sheet.updates), 2)
        written = self.sheet.updates[0]
        self.assertTrue(written["range"].startswith("A2:"))
        self.assertEqual(written["value_input_option"] if "value_input_option" in written else None, None)
        self.assertEqual([values[ID] for values in self.sheet.values[1:]], ["1", "2"])
        # Повторная сортировка ничего не меняет — перезаписей больше нет.
        self.store.refresh_bookings_sheet(sort=True)
        self.assertEqual(len(self.sheet.updates), 2)

    # ---------- создание ----------

    def test_create_with_unsorted_dates_keeps_stable_ids(self):
        first = self.create(date_str="2099-01-03", time_str="10:00")
        second = self.create(date_str="2099-01-01", time_str="10:00")
        third = self.create(date_str="2099-01-02", time_str="10:00")
        self.assertTrue(all(r["ok"] for r in (first, second, third)), (first, second, third))
        self.assertEqual([self.new_id(r) for r in (first, second, third)], ["1", "2", "3"])
        self.assertEqual(self.ids(), ["1", "2", "3"])
        self.assertEqual(self.records(), {"1": ("03.01.2099", "10:00"),
                                          "2": ("01.01.2099", "10:00"),
                                          "3": ("02.01.2099", "10:00")})
        mine = self.store.my_bookings(111)["bookings"]
        self.assertEqual([item["booking_id"] for item in mine], ["2", "3", "1"])  # показ по датам

    def test_create_after_cancel_and_archive_does_not_reuse_ids(self):
        self.seed(row(1, "01.01.2099", "10:00"),
                  row(2, "02.01.2099", "10:00"),
                  row(3, "01.01.2020", "10:00", status=""))
        self.assertTrue(self.store.cancel_booking("2", 111)["ok"])
        archived = self.store.archive_old_bookings()
        self.assertEqual(archived, 1)  # прошедшая дата без статуса
        created = self.create(date_str="2099-01-04", time_str="10:00")
        self.assertTrue(created["ok"], created)
        # Отменённый и архивный ID не используются заново.
        self.assertEqual(self.new_id(created), "4")
        self.assertEqual(self.ids(), ["1", "2", "3", "4"])
        self.assertEqual(str(self.sheet.values[3][STATUS]), "Архив")

    def test_nonconsecutive_ids_and_manually_deleted_rows(self):
        self.seed(row(5, "01.01.2099", "10:00"), row(9, "02.01.2099", "10:00"), row(12, "03.01.2099", "10:00"))
        del self.sheet.values[2]  # администратор удалил строку с ID 9
        cancelled = self.store.cancel_booking("12", 111)
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertEqual(str(self.sheet.values[2][STATUS]), "Отмена")
        created = self.create(date_str="2099-01-04", time_str="10:00")
        self.assertEqual(self.new_id(created), "13")
        self.assertEqual(self.ids(), ["5", "12", "13"])
        # Обслуживание выставляет номера по порядку строк: пропусков больше нет.
        self.store.refresh_bookings_sheet(sort=True)
        self.assertEqual(self.ids(), ["1", "2", "3"])
        self.assertEqual(self.records()["3"], ("04.01.2099", "10:00"))
        self.assertEqual(str(self.sheet.values[2][STATUS]), "Отмена")  # прежний ID 12

    def test_duplicate_and_malformed_legacy_ids(self):
        self.seed(row(7, "01.01.2099", "10:00"), row(7, "02.01.2099", "10:00"),
                  row("abc", "03.01.2099", "10:00"), row("", "04.01.2099", "10:00"))
        # Дубль ID не даёт угадывать строку.
        ambiguous = self.store.cancel_booking("7", 111)
        self.assertFalse(ambiguous["ok"])
        self.assertIn("неоднозначна", ambiguous["error"])
        # Нечисловые и пустые ID не мешают выделению следующего номера.
        created = self.create(date_str="2099-01-04", time_str="10:00")
        self.assertTrue(created["ok"], created)
        self.assertEqual(self.new_id(created), "8")
        self.store.refresh_bookings_sheet(sort=True)
        # Обслуживание выставляет номера по порядку строк, включая строки
        # с наследственными и пустыми ID.
        self.assertEqual(self.ids(), ["1", "2", "3", "4", "5"])

    # ---------- отмена и перенос ----------

    def test_cancel_first_middle_last_keeps_other_rows(self):
        self.seed(row(101, "01.01.2099", "09:00"), row(102, "01.01.2099", "10:00"),
                  row(103, "01.01.2099", "11:00"), row(104, "02.01.2099", "10:00"),
                  row(105, "03.01.2099", "10:00"))
        snapshot = deepcopy(self.sheet.values)
        for item_id in ("101", "103", "105"):
            result = self.store.cancel_booking(item_id, 111)
            self.assertTrue(result["ok"], result)
        self.assertEqual(self.ids(), ["101", "102", "103", "104", "105"])
        statuses = [str(values[STATUS]) for values in self.sheet.values[1:]]
        self.assertEqual(statuses, ["Отмена", "Новая", "Отмена", "Новая", "Отмена"])
        # Нетронутые строки не изменились ни в одной колонке.
        self.assertEqual(self.sheet.values[2], snapshot[2])
        self.assertEqual(self.sheet.values[4], snapshot[4])

    def test_reschedule_across_dates_preserves_identity(self):
        self.seed(row(201, "01.01.2099", "10:00"), row(202, "02.01.2099", "10:00"), row(203, "03.01.2099", "10:00"))
        snapshot = deepcopy(self.sheet.values)
        result = self.store.reschedule_booking("202", 111, "2099-01-04", "10:00")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["booking_id"], "202")
        self.assertEqual(self.ids(), ["201", "202", "203"])
        self.assertEqual(self.records()["202"], ("04.01.2099", "10:00"))
        self.assertEqual(self.sheet.values[2][CAR], snapshot[2][CAR])
        self.assertEqual(self.sheet.values[2][TOTAL], snapshot[2][TOTAL])
        self.assertEqual(self.sheet.values[1], snapshot[1])
        self.assertEqual(self.sheet.values[3], snapshot[3])
        # Тот же ID продолжает работать: повторный перенос и отмена.
        moved = self.store.reschedule_booking("202", 111, "2099-01-04", "11:00")
        self.assertTrue(moved["ok"], moved)
        cancelled = self.store.cancel_booking("202", 111)
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertEqual(str(self.sheet.values[2][STATUS]), "Отмена")
        self.assertEqual(self.records()["202"], ("04.01.2099", "11:00"))

    def test_manually_reordered_rows_are_found_by_id(self):
        # Администратор вручную перетасовал строки: ID больше не по порядку и не по хронологии.
        self.seed(row(301, "03.01.2099", "10:00"), row(302, "01.01.2099", "10:00"), row(303, "02.01.2099", "10:00"))
        result = self.store.cancel_booking("302", 111)
        self.assertTrue(result["ok"], result)
        self.assertEqual(str(self.sheet.values[2][STATUS]), "Отмена")
        self.assertEqual(self.ids(), ["301", "302", "303"])
        moved = self.store.reschedule_booking("301", 111, "2099-01-04", "10:00")
        self.assertTrue(moved["ok"], moved)
        self.store.refresh_bookings_sheet(sort=True)
        # Обслуживание сортирует по хронологии и выставляет номера по порядку строк.
        self.assertEqual(self.ids(), ["1", "2", "3"])
        self.assertEqual(self.records()["3"], ("04.01.2099", "10:00"))  # прежний ID 301

    # ---------- защита данных листа ----------

    def test_formulas_and_extra_columns_block_reordering_but_not_mutations(self):
        self.seed(row(401, "03.01.2099", "10:00"), row(402, "01.01.2099", "10:00"))
        self.sheet.values[1][TOTAL] = "=2*350"        # формула в данных
        self.sheet.values[1].append("пометка")        # данные правее шапки
        self.sheet.values[2].append("пометка2")
        before = deepcopy(self.sheet.values)
        self.store.refresh_bookings_sheet(sort=True)
        self.assertEqual(self.sheet.values, before)   # ни значений, ни порядка не тронуто
        self.assertEqual(self.sheet.updates, [])
        # Адресные операции по-прежнему работают.
        created = self.create(date_str="2099-01-04", time_str="10:00")
        self.assertTrue(created["ok"], created)
        self.assertEqual(self.new_id(created), "403")
        cancelled = self.store.cancel_booking("402", 111)
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertEqual(str(self.sheet.values[2][STATUS]), "Отмена")
        self.assertEqual(self.sheet.values[1][TOTAL], "=2*350")
        self.assertEqual(self.sheet.values[1][16], "пометка")

    def test_periodic_refresh_without_sort_is_never_a_rewrite(self):
        self.seed(row(501, "03.01.2099", "10:00"), row(502, "01.01.2099", "10:00"))
        self.store.refresh_bookings_sheet()
        self.assertEqual(self.sheet.updates, [])
        self.assertEqual(self.ids(), ["501", "502"])

    def test_mutations_do_not_rewrite_the_whole_sheet(self):
        self.seed(row(601, "01.01.2099", "10:00"), row(602, "02.01.2099", "10:00"))
        self.create(date_str="2099-01-03", time_str="10:00")
        self.assertEqual(self.sheet.updates, [])
        self.store.cancel_booking("601", 111)
        self.assertEqual(self.sheet.updates, [])
        self.store.reschedule_booking("602", 111, "2099-01-04", "10:00")
        self.assertEqual(self.sheet.updates, [])
        # Обычные обновления остаются адресными: batch-записи не шире двух ячеек.
        cell_batches = [write for write in self.sheet.writes if write and isinstance(write[0], dict)]
        self.assertTrue(cell_batches)
        self.assertTrue(all(len(batch) <= 2 for batch in cell_batches))

    def test_headers_are_found_by_name_not_position(self):
        # Нестандартный порядок колонок: шапка переставлена, строки соответственно.
        order = [DATE, TIME, ID, STATUS] + [index for index in range(len(HEADERS)) if index not in {DATE, TIME, ID, STATUS}]
        shuffled_headers = [HEADERS[index] for index in order]
        rows = []
        for values in (row(701, "02.01.2099", "10:00"), row(702, "01.01.2099", "10:00")):
            rows.append([values[index] for index in order])
        self.sheet.values = [shuffled_headers, *rows]
        self.store.refresh_bookings_sheet(sort=True)
        # Сортировка и перенумерация работают через поиск колонок по имени.
        self.assertEqual([str(values[2]) for values in self.sheet.values[1:]], ["1", "2"])
        self.assertEqual([str(values[0]) for values in self.sheet.values[1:]], ["01.01.2099", "02.01.2099"])
        cancelled = self.store.cancel_booking("1", 111)  # была запись 702
        self.assertTrue(cancelled["ok"], cancelled)
        status_index = shuffled_headers.index("Статус")
        self.assertEqual(str(self.sheet.values[1][status_index]), "Отмена")

    def test_archive_marks_permuted_status_column_by_name(self):
        # При переставленной шапке архивация пишет «Архив» в колонку «Статус» по имени,
        # остальные данные строки не затрагивает.
        order = [STATUS, DATE, ID] + [index for index in range(len(HEADERS)) if index not in {STATUS, DATE, ID}]
        shuffled_headers = [HEADERS[index] for index in order]
        def line(item_id, date_str, status):
            values = row(item_id, date_str, "18:00", status=status)
            return [values[index] for index in order]
        self.sheet.values = [shuffled_headers, line("1", "01.01.2024", "Новая"),
                             line("2", "01.01.2024", "Отмена"), line("3", "01.01.2099", "Новая")]
        self.assertEqual(self.store.archive_old_bookings(), 1)
        status_index = shuffled_headers.index("Статус")
        self.assertEqual(str(self.sheet.values[1][status_index]), "Архив")
        self.assertEqual(str(self.sheet.values[2][status_index]), "Отмена")
        self.assertEqual(str(self.sheet.values[3][status_index]), "Новая")
        identity_index = shuffled_headers.index("ID")
        for row_number, item_id in zip((1, 2, 3), ("1", "2", "3")):
            self.assertEqual(str(self.sheet.values[row_number][identity_index]), item_id)

    # ---------- конкуренция и повторы ----------

    def test_concurrent_creation_gets_unique_ids(self):
        barrier = threading.Barrier(8)
        results = []

        def customer(offset):
            barrier.wait()
            results.append(self.create(date_str="2099-01-02", time_str="10:00", telegram_id=1000 + offset))

        threads = [threading.Thread(target=customer, args=(offset,)) for offset in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(all(result["ok"] for result in results), results)
        identifiers = [self.new_id(result) for result in results]
        self.assertEqual(sorted(map(int, identifiers)), list(range(1, 9)))
        self.assertEqual(len(self.sheet.values) - 1, 8)

    def test_concurrent_cancel_and_reschedule_of_different_bookings(self):
        self.seed(row(801, "01.01.2099", "09:00", telegram="111"),
                  row(802, "01.01.2099", "10:00", telegram="111"),
                  row(803, "02.01.2099", "10:00", telegram="222"))
        outcomes = {}

        def cancel():
            outcomes["cancel"] = self.store.cancel_booking("801", 111)

        def move():
            outcomes["move"] = self.store.reschedule_booking("802", 111, "2099-01-04", "10:00")

        def foreign():
            outcomes["foreign"] = self.store.cancel_booking("802", 222)

        threads = [threading.Thread(target=cancel), threading.Thread(target=move), threading.Thread(target=foreign)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(outcomes["cancel"]["ok"], outcomes)
        self.assertTrue(outcomes["move"]["ok"], outcomes)
        self.assertFalse(outcomes["foreign"]["ok"])  # чужая запись не отменяется
        self.assertEqual(self.records(), {"801": ("01.01.2099", "09:00"),
                                          "802": ("04.01.2099", "10:00"),
                                          "803": ("02.01.2099", "10:00")})
        self.assertEqual(str(self.sheet.values[1][STATUS]), "Отмена")

    def test_repeated_confirmation_and_retry_do_not_duplicate(self):
        self.seed(row(901, "01.01.2099", "10:00"))
        duplicate = self.create(date_str="2099-01-01", time_str="10:00", car="BMW X5")
        self.assertFalse(duplicate["ok"])
        self.assertIn("уже есть", duplicate["error"])
        first = self.store.cancel_booking("901", 111)
        self.assertTrue(first["ok"], first)
        second = self.store.cancel_booking("901", 111)
        self.assertFalse(second["ok"])
        self.assertIn("уже отменена", second["error"])
        # Сбой записи не оставляет полузаписи: повтор создаёт ровно одну строку.
        self.sheet.fail_write = True
        with self.assertRaises(MappingError):
            self.create(date_str="2099-01-02", time_str="10:00")
        self.sheet.fail_write = False
        self.assertEqual(len(self.sheet.values) - 1, 1)
        retry = self.create(date_str="2099-01-02", time_str="10:00")
        # Неопределённый результат первой попытки блокирует вслепую повтор;
        # после ручной/клиентской сверки повтор разрешён отдельным запросом.
        if not retry["ok"]:
            self.assertIn("неизвестен", retry.get("error", "").lower())

    # ---------- запуск и обслуживание ----------

    def test_refresh_on_empty_and_single_row_sheets(self):
        self.store.refresh_bookings_sheet()
        self.store.refresh_bookings_sheet(sort=True)
        self.assertEqual(self.sheet.values, [list(HEADERS)])
        self.sheet.values.append(row(1001, "02.01.2099", "10:00"))
        self.store.refresh_bookings_sheet(sort=True)
        self.assertEqual(len(self.sheet.values), 2)
        # Одиночная строка тоже приводится к нумерации с единицы.
        self.assertEqual(str(self.sheet.values[1][ID]), "1")

    def test_archive_then_periodic_sort_renumbers(self):
        self.seed(row(1101, "03.01.2099", "10:00"), row(1102, "01.01.2020", "10:00"), row(1103, "01.01.2099", "10:00"))
        self.assertEqual(self.store.archive_old_bookings(), 1)
        self.store.refresh_bookings_sheet(sort=True)
        self.assertEqual(self.ids(), ["1", "2", "3"])
        self.assertEqual(str(self.sheet.values[1][STATUS]), "Архив")
        self.assertEqual(self.records()["3"], ("03.01.2099", "10:00"))  # прежний ID 1101

    def test_large_sheet_sort_is_two_updates_and_renumbered(self):
        count = 2000
        rows = [row(index, f"01.{(index % 12) + 1:02d}.2099", f"{9 + index % 10:02d}:{index % 60:02d}")
                for index in range(count, 0, -1)]
        self.seed(*rows)
        self.assertEqual(self.ids()[0], str(count))
        self.store.refresh_bookings_sheet(sort=True)
        # Две перезаписи: хронологическая перестановка строк и перенумерация ID-колонки.
        self.assertEqual(len(self.sheet.updates), 2)
        self.assertEqual(len(self.sheet.values) - 1, count)
        self.assertEqual(self.ids(), [str(number) for number in range(1, count + 1)])
        target = self.store.cancel_booking("777", 111)
        self.assertTrue(target["ok"], target)
        self.assertEqual(str(self.sheet.values[777][STATUS]), "Отмена")

    # ---------- адаптивные раскладки ----------

    def test_adaptive_layout_with_other_column_order_is_not_globally_rewritten(self):
        cfg = fake_config(self.temp.name)  # адаптивный режим по умолчанию
        spreadsheet = Spreadsheet([
            Worksheet(1, "Услуги", [["Услуга", "Описание", "Легковой", "Кроссовер", "Доступна"],
                                    ["Мойка", "Кузов", "700", "800", "да"]]),
            Worksheet(2, "Тарифы", [["Тариф", "Состав", "Скидка %", "Доступен"], ["Базовый", "Мойка", "0", "да"]]),
            Worksheet(3, "Слоты", [["Дата", "Время", "Постов"],
                                   ["01.01.2099", "10:00", "3"], ["02.01.2099", "10:00", "3"]]),
            Worksheet(4, "Записи", [["Статус", "Создано", "Время", "Дата", "ID", "Имя", "Телефон",
                                     "Автомобиль", "Класс", "Тариф", "Доп. услуги", "Состав", "Сумма",
                                     "Комментарий", "Telegram ID", "Username"]])])
        store = Store(cfg)
        store._spreadsheet = spreadsheet
        sheet = spreadsheet.worksheet("Записи")
        result = store.create_booking(**booking(date_str="2099-01-01", time_str="10:00"))
        self.assertTrue(result["ok"], result)
        # Запись легла в правильные колонки переставленной шапки.
        self.assertEqual(str(sheet.values[1][4]), "1")
        self.assertEqual(str(sheet.values[1][3]), "01.01.2099")
        self.assertEqual(str(sheet.values[1][2]), "10:00")
        self.assertEqual(str(sheet.values[1][0]), "Новая")
        # Глобальной перезаписи нет: адаптивную раскладку сортер не трогает.
        store.refresh_bookings_sheet(sort=True)
        self.assertEqual(sheet.updates, [])
        self.assertEqual(sheet.values[0][0], "Статус")
        cancelled = store.cancel_booking("1", 111)
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertEqual(str(sheet.values[1][0]), "Отмена")

    def test_adaptive_day_sheets_are_untouched_by_refresh(self):
        from adaptive import atomic_json
        cfg = fake_config(self.temp.name)  # адаптивный режим по умолчанию
        spreadsheet = Spreadsheet([
            Worksheet(1, "Услуги", [["Услуга", "Описание", "Легковой", "Кроссовер", "Доступна"],
                                    ["Мойка", "Кузов", "700", "800", "да"]]),
            Worksheet(2, "Тарифы", [["Тариф", "Состав", "Скидка %", "Доступен"], ["Базовый", "Мойка", "0", "да"]]),
            Worksheet(3, "Slots 2099-01-01", [["Время", "Постов"], ["10:00", "3"]]),
            Worksheet(4, "Bookings 2099-01-01", [["Статус", "Создано", "Время", "ID", "Имя", "Телефон",
                                                  "Автомобиль", "Класс", "Тариф", "Доп. услуги", "Состав",
                                                  "Сумма", "Комментарий", "Telegram ID", "Username"]])])
        store = Store(cfg)
        store._spreadsheet = spreadsheet
        bookings = spreadsheet.worksheet("Bookings 2099-01-01")
        store._adaptive.refresh()
        atomic_json(store._adaptive.path, store._adaptive.document)
        result = store.create_booking(**booking(date_str="2099-01-01", time_str="10:00"))
        self.assertTrue(result["ok"], result)
        writes = len(bookings.writes)
        store.refresh_bookings_sheet(sort=True)  # листа «Записи» нет: ничего не падает и не переписывается
        self.assertEqual(len(bookings.writes), writes)
        self.assertEqual(bookings.updates, [])


if __name__ == "__main__":
    unittest.main()
