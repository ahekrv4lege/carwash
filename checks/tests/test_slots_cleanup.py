"""Обслуживание листа «Слоты»: очистка прошедших дат, сдвиг строк вверх, автопроизводство.

Часовой пояс бота задаёт «сегодня»; сеточные раскладки (дата в названии),
график без даты, строки с формулами и непонятные строки останавливают сдвиг.
Все раскладки синтетические, сеть не используется.
"""
import datetime as dt
import tempfile
import unittest

from tests.fakes import fixture

PAST, PAST2, FUTURE = "01.01.2024", "02.01.2024", "01.01.2099"


class AdaptiveCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        sheet = self.spreadsheet.worksheet("Слоты")
        sheet.values = [["Дата", "Время", "Постов"],
                        [PAST, "10:00", "2"],
                        [PAST2, "11:00", "1"],
                        [FUTURE, "10:00", "3"],
                        [FUTURE, "11:00", "3"]]

    def test_past_rows_are_cleared_future_rows_shift_up(self):
        cleaned = self.store.clean_expired_slots()
        self.assertEqual(cleaned, 2)
        sheet = self.spreadsheet.worksheet("Слоты")
        self.assertEqual([str(cell) for cell in sheet.values[1]], [FUTURE, "10:00", "3"])  # сдвиг вверх
        self.assertEqual([str(cell) for cell in sheet.values[2]], [FUTURE, "11:00", "3"])
        self.assertEqual([str(cell) for cell in sheet.values[3]], ["", "", ""])  # хвост опустел
        self.assertEqual([str(cell) for cell in sheet.values[4]], ["", "", ""])
        # Повторная очистка ничего не находит; автопроизводство не срабатывает без шаблона
        self.assertEqual(self.store.clean_expired_slots(), 0)
        slots = self.store._get_slots()
        self.assertFalse([day for (day, time_value) in slots if day.isoformat() <= PAST])

    def test_formulas_stop_the_shift_but_past_is_still_cleared(self):
        sheet = self.spreadsheet.worksheet("Слоты")
        sheet.values.append([PAST, "12:00", "2", "=1+1"])  # формула в прошедшей строке
        cleaned = self.store.clean_expired_slots()
        self.assertEqual(cleaned, 2)  # формульная строка не очищается
        # Сдвиг остановлен формулой: она осталась на месте целиком
        row = sheet.values[-1]
        self.assertEqual([str(cell) for cell in row[:3]], [PAST, "12:00", "2"])
        self.assertEqual(str(row[3]), "=1+1")

    def test_history_row_stops_the_shift_and_keeps_the_note(self):
        sheet = self.spreadsheet.worksheet("Слоты")
        sheet.values.append([PAST, "13:00", "2", "заметка администратора"])  # данные вне раскладки
        cleaned = self.store.clean_expired_slots()
        self.assertEqual(cleaned, 3)  # прошлая строка тоже очищается (дата проходит)
        # Сдвиг остановлен непонятной строкой: заметка остаётся в своей позиции
        row = sheet.values[-1]
        self.assertEqual(str(row[3]), "заметка администратора")

    def test_grid_layouts_are_not_touched(self):
        grid = self.spreadsheet.worksheet("Слоты")
        grid.title = PAST  # сетка боксов с датой в названии листа
        grid.values = [["Время", "Пост 1", "Пост 2"], ["10:00", "", ""]]
        self.store._invalidate("slots")
        cleaned = self.store.clean_expired_slots()
        self.assertEqual(cleaned, 0)
        self.assertEqual([str(cell) for cell in grid.values[1]], ["10:00", "", ""])


class NonAdaptiveCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name, sheets_adaptive_enabled=False)
        sheet = self.spreadsheet.worksheet("Слоты")
        sheet.values = [["Дата", "Время", "Постов"],
                        [PAST, "10:00", "2"],
                        [FUTURE, "10:00", "3"]]

    def test_past_rows_are_cleared_and_shifted_up_without_adaptive(self):
        self.assertEqual(self.store.clean_expired_slots(), 1)
        sheet = self.spreadsheet.worksheet("Слоты")
        self.assertEqual([str(cell) for cell in sheet.values[1]], [FUTURE, "10:00", "3"])  # сдвиг вверх
        self.assertEqual([str(cell) for cell in sheet.values[2]], ["", "", ""])  # хвост опустел
        slots = self.store._get_slots()
        self.assertFalse([day for (day, time_value) in slots if day.isoformat() <= PAST])


class AutoGenerateTests(unittest.TestCase):
    def test_future_days_are_generated_from_last_week(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store, spreadsheet = fixture(temp.name)
        today = store.now().date()
        sheet = spreadsheet.worksheet("Слоты")
        sheet.values = [["Дата", "Время", "Постов"]]
        # Полная прошлая неделя: каждый день недели как шаблон.
        for offset in range(2, 9):
            template_day = today - dt.timedelta(days=offset)
            sheet.values.append([template_day.strftime("%d.%m.%Y"), "10:00", "2"])
        cleaned = store.clean_expired_slots()
        self.assertEqual(cleaned, 7)  # все прошлые дни очищены
        rows = [row for row in sheet.values[1:] if any(str(cell).strip() for cell in row)]
        future_rows = [row for row in rows
                       if dt.datetime.strptime(str(row[0]).strip(), "%d.%m.%Y").date() > today]
        self.assertEqual(len(future_rows), 7)  # неделя вперёд сгенерирована
        tomorrow = (today + dt.timedelta(days=1)).strftime("%d.%m.%Y")
        first_future = next(row for row in future_rows if str(row[0]) == tomorrow)
        self.assertEqual([str(cell) for cell in first_future[1:]], ["10:00", "2"])  # состав от шаблона

    def test_existing_future_days_are_not_recreated(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store, spreadsheet = fixture(temp.name)
        today = store.now().date()
        sheet = spreadsheet.worksheet("Слоты")
        upcoming = (today + dt.timedelta(days=1)).strftime("%d.%m.%Y")
        template_date = (today - dt.timedelta(days=7)).strftime("%d.%m.%Y")
        sheet.values = [["Дата", "Время", "Постов"],
                        [template_date, "10:00", "2"],
                        [upcoming, "09:00", "5"]]  # будущий день уже заполнен админом
        before = len(sheet.values)
        store.clean_expired_slots()
        future_rows = []
        for row in sheet.values[1:]:
            text = str(row[0]).strip()
            if not text:
                continue
            if dt.datetime.strptime(text, "%d.%m.%Y").date() == today + dt.timedelta(days=1):
                future_rows.append(row)
        self.assertEqual(len(future_rows), 1)  # день не пересоздаётся
        self.assertEqual([str(cell) for cell in future_rows[0][1:]], ["09:00", "5"])


if __name__ == "__main__":
    unittest.main()
