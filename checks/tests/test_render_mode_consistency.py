"""Регрессия: строка с телефоном, который в FORMULA-режиме «+7…», а в отображении «7…».

Найдено живым прогоном на тестовой таблице: предполётная проверка create сравнивала
строки из двух режимов чтения, видела «изменение» и блокировала любую новую запись
(клиент получал «Не удалось подтвердить»). Таблица в тесте имитирует реальный случай.
"""
import tempfile
import unittest

from adaptive import MappingError
from tests.fakes import booking, fixture


def display_strips_plus(worksheet):
    """Отображение (FORMATTED_VALUE) скрывает «+» у телефонов, как в реальной таблице.

    Касается и полного чтения, и чтения диапазона (get), как в gspread.
    """
    def hide(cell):
        text = str(cell)
        return text[1:] if text.startswith("+") and text[1:].isdigit() else cell

    original_all, original_get = worksheet.get_all_values, worksheet.get

    def shown_all(value_render_option=None, **kwargs):
        rows = original_all(value_render_option=value_render_option, **kwargs)
        return [[hide(cell) for cell in row] for row in rows] if value_render_option == "FORMATTED_VALUE" else rows

    def shown_get(range_name, value_render_option=None, **kwargs):
        rows = original_get(range_name, value_render_option=value_render_option, **kwargs)
        return [[hide(cell) for cell in row] for row in rows] if value_render_option == "FORMATTED_VALUE" else rows

    worksheet.get_all_values, worksheet.get = shown_all, shown_get
    return worksheet


class RenderModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        self.bookings = display_strips_plus(self.spreadsheet.worksheet("Записи"))
        self.bookings.values.append(["1", "01.11.2026 10:00", "01.01.2099", "10:00", "Никита", "+79127548866",
                                     "Toyota Camry", "Легковой", "Базовый", "", "", "630", "", "555",
                                     "", "Новая"])
        self.store._invalidate("slots", "bookings")

    def test_new_booking_is_not_blocked_by_phone_display_format(self):
        result = self.store.create_booking(**booking(telegram_id=222, car="Kia Rio", time_str="10:00"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.bookings.values), 3)

    def test_cancel_of_booking_with_typed_phone_is_not_blocked(self):
        result = self.store.cancel_booking(1, 555)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.bookings.values[1][-1], "Отмена")


if __name__ == "__main__":
    unittest.main()
