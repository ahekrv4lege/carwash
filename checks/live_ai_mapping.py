"""Живой прогон AI-интерпретации структуры книги на настоящем провайдере.

Запуск: python checks/live_ai_mapping.py [имя_сценария|all]
Требует реальные ключи провайдера (как production .env). Google Sheets здесь
не используется: раскладки задаются синтетическими листами, зато вызов модели,
валидация маппинга, цены и свободное время — реальный код проекта.
Провайдер получает компакт без данных клиентов.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
PROJECT = FOLDER.parent
sys.path[:0] = [str(PROJECT), str(FOLDER)]

from tests.fakes import Spreadsheet, Worksheet  # noqa: E402
from sheets import Store, BOOKING_HEADERS  # noqa: E402

WEEK = [["Дата", "Время", "Постов"], ["01.01.2099", "10:00", "2"]]


def scenarios():
    return {
        "classic_ru": {
            "sheets": {
                "Услуги": [["Услуга", "Описание", "Легковой", "Кроссовер", "Доступна"],
                           ["Мойка", "Кузов", "700", "800", "да"]],
                "Тарифы": [["Тариф", "Состав", "Скидка %", "Доступен"], ["Базовый", "Мойка", "10", "да"]],
                "Слоты": WEEK,
                "Записи": [BOOKING_HEADERS],
            },
            "ai_expected": False,
            "check": lambda store: store.calculate_price("Легковой", "Базовый", [])["total"] == 630,
        },
        "unfamiliar_headers": {
            "sheets": {
                "Прайс-лист": [["Наименование работы", "Примечание", "Стоимость седан", "Стоимость кроссовер", "Выполняется"],
                               ["Мойка кузова", "Кузов", "700", "800", "да"],
                               ["Полировка", "Кузов", "2000", "2500", "да"]],
                "График": [["Начало смены", "Конец смены", "Мастерых"], ["09:00", "19:00", "2"]],
                "Журнал": [BOOKING_HEADERS],
            },
            "check": lambda store: store.calculate_price("Легковой", None, ["Мойка кузова"])["total"] == 700,
        },
        "transposed_matrix": {
            "sheets": {
                "Цены": [["Класс авто", "Мойка", "Воск", "Химчистка"],
                         ["Легковой", "700", "200", "3000"],
                         ["Кроссовер", "800", "250", "3500"]],
                "Расписание": [["День", "Часы работы", "Постов"],
                               ["ежедневно", "09:00-19:00", "2"]],
                "Записи": [BOOKING_HEADERS],
            },
            "check": lambda store: store.calculate_price("Кроссовер", None, ["Химчистка"])["total"] == 3500,
        },
        "long_format": {
            "sheets": {
                "Прайс": [["Услуга", "Класс", "Стоимость"],
                          ["Мойка", "Легковой", "700"],
                          ["Мойка", "Кроссовер", "800"],
                          ["Воск", "Легковой", "200"]],
                "Записи": [BOOKING_HEADERS],
                "Слоты": WEEK,
            },
            "check": lambda store: store.calculate_price("Легковой", None, ["Мойка"])["total"] == 700,
        },
        "fixed_price_packages": {
            "sheets": {
                "Услуги": [["Услуга", "Легковой", "Кроссовер"], ["Мойка", "700", "800"]],
                "Тарифы": [["Название пакета", "Цена седан", "Цена кроссовер", "Активен"],
                           ["Экспресс", "700", "800", "да"],
                           ["Комплекс", "1500", "1800", "да"]],
                "Записи": [BOOKING_HEADERS],
                "Слоты": WEEK,
            },
            "check": lambda store: store.calculate_price("Кроссовер", "Комплекс", [])["total"] == 1800,
        },
        "package_price_and_composition": {
            "sheets": {
                "Услуги": [["Услуга", "Легковой"], ["Мойка", "700"], ["Полировка", "1000"]],
                "Тарифы": [["Тариф", "Состав", "Скидка %", "Цена", "Доступен"],
                           ["Полный", "Мойка, Полировка", "0", "1700", "да"]],
                "Записи": [BOOKING_HEADERS],
                "Слоты": WEEK,
            },
            "check": lambda store: store.calculate_price("Легковой", "Полный", [])["total"] == 1700,
        },
        "multisheet_durations": {
            "sheets": {
                "Услуги": [["Услуга", "Легковой", "Длительность"], ["Мойка", "700", ""], ["Полировка", "1000", "2 ч"]],
                "Сколько времени": [["Услуга", "Минут на работу"], ["Мойка", "полчаса"]],
                "Режим": [["Открытие", "Закрытие", "Постов"], ["09:00", "19:00", "1"]],
                "Записи": [BOOKING_HEADERS],
            },
            "check": lambda store: (
                store.calculate_price("Легковой", None, ["Полировка"])["duration_minutes"] == 120
                and len(store.free_slots("2099-01-01", 1, 30)["days"][0]["free_times"]) > 0),
        },
        "english_mixed": {
            "sheets": {
                "Services": [["Service", "Sedan", "SUV", "Available"],
                             ["Body wash", "700", "1000", "yes"]],
                "Schedule": [["Open", "Close", "Bays"], ["9:00", "19:00", "1"]],
                "Bookings": [["ID", "Created", "Date", "Time", "Name", "Phone", "Vehicle", "Car class",
                              "Package", "Extras", "Items", "Total", "Notes", "TG_ID", "Username", "Status"]],
            },
            "check": lambda store: store.calculate_price("SUV", None, ["Body wash"])["total"] == 1000,
        },
        "decorative_rows": {
            "sheets": {
                "Прайс": [[], ["АВТОМОЙКА «БЛСК»"], [], ["Услуга", "", "Легковой", "Кроссовер"],
                          ["Мойка", "", "700", "800"]],
                "Записи": [BOOKING_HEADERS],
                "Слоты": WEEK,
            },
            "check": lambda store: store.calculate_price("Легковой", None, ["Мойка"])["total"] == 700,
        },
        "formula_backed_prices": {
            # Проверяется привязка маппинга и сохранность формул: синтетический лист
            # возвращает вычисленное значение «42», поэтому цену проверяем по нему.
            "sheets": {
                "Услуги": [["Услуга", "Легковой", "Кроссовер"], ["Мойка", "=700*1.1", "800"]],
                "Записи": [BOOKING_HEADERS],
                "Слоты": WEEK,
            },
            "check": lambda store: store.calculate_price("Легковой", None, ["Мойка"])["total"] == 42,
        },
        "unavailable_service": {
            "sheets": {
                "Услуги": [["Услуга", "Легковой", "Выполняем"], ["Мойка", "700", "да"], ["Реставрация", "5000", "нет"]],
                "Записи": [BOOKING_HEADERS],
                "Слоты": WEEK,
            },
            "check": lambda store: not store.calculate_price("Легковой", None, ["Реставрация"])["ok"],
        },
        "ambiguous_prices": {
            "sheets": {
                "Услуги": [["Услуга", "Легковой", "Легковой "], ["Мойка", "700", "900"]],
                "Записи": [BOOKING_HEADERS],
                "Слоты": WEEK,
            },
            # Отказ может быть мягким (ok=False) или жёстким (MappingError) — оба безопасны.
            "check": lambda store: not _price_ok(store, "Легковой", None, ["Мойка"]),
        },
        "capacity_slots": {
            "sheets": {
                "Услуги": [["Услуга", "Легковой"], ["Мойка", "700"]],
                "Тарифы": [["Тариф", "Состав"], ["Базовый", "Мойка"]],
                "Слоты": [["Дата", "Время", "Мест"], ["01.01.2099", "10:00", "2"]],
                "Записи": [BOOKING_HEADERS],
            },
            "check": lambda store: store.create_booking(**dict(
                __import__("tests.fakes", fromlist=["booking"]).booking()))["ok"],
        },
        "post_grid": {
            "sheets": {
                "Услуги": [["Услуга", "Легковой"], ["Мойка", "700"]],
                "Тарифы": [["Тариф", "Состав"], ["Базовый", "Мойка"]],
                "Записи": [BOOKING_HEADERS],
            },
            "grid_sheet": ["01.01.2099", [["Время", "Пост 1", "Пост 2"], ["10:00", "", ""], ["11:00", "", ""]]],
            "check": lambda store: store.create_booking(**dict(
                __import__("tests.fakes", fromlist=["booking"]).booking()))["ok"],
        },
        "duration_schedule": {
            "sheets": {
                "Услуги": [["Услуга", "Легковой", "Время работы"], ["Мойка", "700", "90 мин"]],
                "Тарифы": [["Тариф", "Состав", "Длительность"], ["Комплекс", "Мойка", "2 часа"]],
                "График смен": [["Начало", "Конец", "Мастерых"], ["09:00", "19:00", "1"]],
                "Записи": [BOOKING_HEADERS],
            },
            "check": lambda store: (
                store.calculate_price("Легковой", "Комплекс", [])["duration_minutes"] == 120
                and not store.create_booking(**dict(
                    __import__("tests.fakes", fromlist=["booking"]).booking(time_str="18:30")))["ok"]),
        },
    }


def _price_ok(store, *arguments) -> bool:
    from adaptive import MappingError
    try:
        return bool(store.calculate_price(*arguments).get("ok"))
    except MappingError:
        return False


def build_store(scenario):
    sheets = [Worksheet(index + 1, title, values)
              for index, (title, values) in enumerate(scenario["sheets"].items())]
    if "grid_sheet" in scenario:
        title, values = scenario["grid_sheet"]
        sheets = [sheet for sheet in sheets if sheet.title != "Слоты"]
        sheets.append(Worksheet(len(sheets) + 1, title, values))
    spreadsheet = Spreadsheet(sheets)
    import os
    import tempfile
    from dataclasses import replace
    from config import load_config
    config = load_config(sheets_only=True)  # Telegram-токен для этого прогона не нужен
    # Изолированный mappings-файл: чужие и производственные маппинги не подмешиваем.
    mapping_dir = tempfile.mkdtemp(prefix="carwash-live-mapping-")
    config = replace(config, sheets_adaptive_enabled=True, sheets_ai_mapping_enabled=True,
                     sheets_mapping_file=os.path.join(mapping_dir, "mapping.json"))
    store = Store(config)
    store._spreadsheet = spreadsheet
    return store


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="*", default=["all"])
    arguments = parser.parse_args()
    chosen = scenarios()
    if arguments.names != ["all"]:
        chosen = {name: chosen[name] for name in arguments.names}
    summary = []
    for name, scenario in chosen.items():
        began = time.time()
        entry = {"scenario": name, "ok": False, "seconds": 0.0, "error": None, "regions": [], "issues": []}
        try:
            store = build_store(scenario)
            document = store._adaptive.refresh(force=True)
            entry["regions"] = [(rule["role"], rule["sheet_title"], rule["validation"]["read"],
                                 rule["validation"]["issues"] + rule["validation"].get("write_issues", []))
                                for rule in document["regions"]]
            entry["issues"] = list(document["issues"])
            if not scenario["check"](store):
                entry["error"] = "бизнес-проверка не сошлась"
            else:
                entry["ok"] = True
        except Exception as error:  # noqa: BLE001 — сводка по каждому сценарию
            entry["error"] = f"{type(error).__name__}: {error}"[:200]
        entry["seconds"] = round(time.time() - began, 1)
        summary.append(entry)
        print(json.dumps(entry, ensure_ascii=False))
    failed = [entry["scenario"] for entry in summary if not entry["ok"]]
    print("ИТОГ: %d/%d сценариев прошли; не прошли: %s" % (
        len(summary) - len(failed), len(summary), ", ".join(failed) or "нет"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
