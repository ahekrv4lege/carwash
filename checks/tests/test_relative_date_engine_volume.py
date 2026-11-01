"""Регрессия: объём двигателя «3.5» в названии автомобиля не должен быть датой.

Найдено живым прогоном: «У меня Toyota Camry 3.5, запишите на завтра в 11:00» давало
две даты («3.5» и «завтра») и теряло дату. Синтетические данные.
"""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from conversation import source_date

NOW = datetime(2026, 11, 1, 21, 0, tzinfo=ZoneInfo("Europe/Moscow"))


class EngineVolumeIsNotADateTests(unittest.TestCase):
    def test_relative_day_is_found_next_to_engine_volume(self):
        self.assertEqual(source_date("У меня Toyota Camry 3.5, запишите на завтра в 11:00", NOW, embedded=True).isoformat(),
                         "2026-11-02")

    def test_engine_volumes_alone_are_not_dates(self):
        for text in ("у меня Camry 3.5", "Rio 2.0 хочу мойку", "Kia 1.6 на мойку"):
            with self.subTest(text=text):
                self.assertIsNone(source_date(text, NOW, embedded=True))

    def test_two_digit_month_dates_still_work_inside_a_sentence(self):
        self.assertEqual(source_date("запишите на 03.11 в 11:00", NOW, embedded=True).isoformat(), "2026-11-03")
        self.assertEqual(source_date("на 5.11 в 10 утра", NOW, embedded=True).isoformat(), "2026-11-05")


if __name__ == "__main__":
    unittest.main()
