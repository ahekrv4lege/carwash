"""Регрессия: «на 12:00» — точное время, а не час без минут.

Найдено живым прогоном: «Перенесите мою запись на 12:00» уходило в вопрос «утро или вечер?».
Синтетические фразы.
"""
import unittest

from ai import hour_without_minutes


class BareHourTests(unittest.TestCase):
    def test_exact_time_with_minutes_is_not_ambiguous(self):
        for text in ("Перенесите мою запись на 12:00 тот же день.", "давайте на 12.30", "на 11:00 пожалуйста",
                     "на 9:15", "на 18.00"):
            with self.subTest(text=text):
                self.assertFalse(hour_without_minutes(text))

    def test_bare_hour_is_still_ambiguous(self):
        for text in ("запишите на 12", "на 5 вечера", "давайте на два", "на час"):
            with self.subTest(text=text):
                self.assertTrue(hour_without_minutes(text))


if __name__ == "__main__":
    unittest.main()
