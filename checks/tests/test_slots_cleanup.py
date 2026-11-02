"""Физическая очистка устаревших дат в листе «Слоты».

Часовой пояс бота задаёт «сегодня»; сеточные раскладки (дата в названии),
график без даты, строки с формулами и данные вне раскладки не трогаются.
Все раскладки синтетические, сеть не используется.
"""
import tempfile
import unittest
from unittest.mock import patch

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

    def test_past_rows_are_cleared_future_rows_stay(self):
        cleaned = self.store.clean_expired_slots()
        self.assertEqual(cleaned, 2)
        sheet = self.spreadsheet.worksheet("Слоты")
        self.assertEqual([str(cell) for cell in sheet.values[1]], ["", "", ""])  # прошедшая строка пустая
        self.assertEqual([str(cell) for cell in sheet.values[2]], ["", "", ""])
        # Будущие слоты не тронуты
        self.assertEqual([str(cell) for cell in sheet.values[3]], [FUTURE, "10:00", "3"])
        self.assertEqual([str(cell) for cell in sheet.values[4]], [FUTURE, "11:00", "3"])
        # Повторная очистка ничего не находит
        self.assertEqual(self.store.clean_expired_slots(), 0)
        # После чистки в памяти остались только будущие даты
        slots = self.store._get_slots()
        self.assertFalse([date for date, time_value in slots if date.isoformat() <= PAST])

    def test_formulas_are_never_destroyed(self):
        sheet = self.spreadsheet.worksheet("Слоты")
        # Формула в прошедшей строке (вне раскладки): строка не чистится целиком.
        sheet.values.append([PAST, "12:00", "2", "=1+1"])
        cleaned = self.store.clean_expired_slots()
        self.assertEqual(cleaned, 2)  # формула держит свою строку: очищены две других прошлых
        row = sheet.values[-1]
        self.assertEqual([str(cell) for cell in row[:3]], [PAST, "12:00", "2"])  # формуле нет вреда
        self.assertEqual(str(row[3]), "=1+1")

    def test_history_outside_layout_is_kept(self):
        sheet = self.spreadsheet.worksheet("Слоты")
        sheet.values.append([PAST, "13:00", "2", "Формула строилась вручную"])  # колонка вне раскладки
        cleaned = self.store.clean_expired_slots()
        self.assertEqual(cleaned, 3)  # строка прошлого тоже очищена
        row = sheet.values[-1]
        self.assertEqual([str(cell) for cell in row[:3]], ["", "", ""])  # слоты стёрлись
        self.assertEqual(str(row[3]), "Формула строилась вручную")  # история соседней колонки сохранена

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

    def test_past_rows_are_cleared_without_adaptive(self):
        self.assertEqual(self.store.clean_expired_slots(), 1)
        sheet = self.spreadsheet.worksheet("Слоты")
        self.assertEqual([str(cell) for cell in sheet.values[1]], ["", "", ""])
        self.assertEqual([str(cell) for cell in sheet.values[2]], [FUTURE, "10:00", "3"])
        # После чистки слоты перечитываются чисто: прошедших дат в памяти нет
        slots = self.store._get_slots()
        self.assertFalse([date for date, time_value in slots if date.isoformat() <= PAST])


if __name__ == "__main__":
    unittest.main()
