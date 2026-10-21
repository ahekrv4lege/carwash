"""Создаёт в вашей Google-таблице листы-шаблоны с примерами: Услуги, Тарифы, Слоты, Записи.
Уже заполненные листы не трогает. Запуск (один раз): python setup_sheet.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta

import gspread

from config import load_config
from sheets import BOOKING_HEADERS, SCOPES

CLASSES = ["Легковой", "Кроссовер", "Внедорожник/минивэн"]

SERVICES_HEADER = ["Услуга", "Описание", *CLASSES, "Доступна"]
SERVICES = [
    ["Мойка кузова", "Бесконтактная мойка, пена, сушка", 700, 800, 1000, "да"],
    ["Пылесос салона", "Салон и багажник", 400, 500, 600, "да"],
    ["Протирка салона", "Влажная уборка пластика и стёкол изнутри", 300, 350, 450, "да"],
    ["Чернение резины", "Нанесение чернителя на шины", 200, 250, 300, "да"],
    ["Горячий воск", "Защитное покрытие кузова", 500, 600, 800, "да"],
    ["Мойка двигателя", "Бережная мойка подкапотного пространства", 800, 900, 1000, "да"],
]

TARIFFS_HEADER = ["Тариф", "Состав", "Скидка %", "Доступен"]
TARIFFS = [
    ["Экспресс", "Мойка кузова, Чернение резины", 0, "да"],
    ["Стандарт", "Мойка кузова, Пылесос салона, Протирка салона", 10, "да"],
    ["Комплекс", "Мойка кузова, Пылесос салона, Протирка салона, Чернение резины, Горячий воск", 15, "да"],
]

SLOTS_HEADER = ["Дата", "Время", "Постов"]


def make_slots(days: int = 14, first_hour: int = 9, last_hour: int = 19, posts: int = 2) -> list[list]:
    rows = []
    today = date.today()
    for i in range(days):
        d = today + timedelta(days=i)
        for h in range(first_hour, last_hour + 1):
            rows.append([d.strftime("%d.%m.%Y"), f"{h:02d}:00", posts])
    return rows


def yes_no_dropdown(ss, ws, col: int) -> None:
    """Выпадающий список «да / нет» в колонке (col — номер с нуля)."""
    ss.batch_update({"requests": [{"setDataValidation": {
        "range": {
            "sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 1000,
            "startColumnIndex": col, "endColumnIndex": col + 1,
        },
        "rule": {
            "condition": {"type": "ONE_OF_LIST",
                          "values": [{"userEnteredValue": "да"}, {"userEnteredValue": "нет"}]},
            "showCustomUi": True, "strict": False,
        },
    }}]})


def ensure_sheet(ss, title: str, header: list, rows: list[list], dropdown_col: int | None = None):
    try:
        ws = ss.worksheet(title)
        if ws.get_all_values():
            print(f"⏭  Лист «{title}» уже заполнен — пропускаю")
            return
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=max(len(rows) + 300, 500), cols=max(len(header) + 2, 10))
    ws.append_rows([header, *rows], value_input_option="RAW")
    ws.freeze(rows=1)
    ws.format("1:1", {"textFormat": {"bold": True}})
    if dropdown_col is not None:
        yes_no_dropdown(ss, ws, dropdown_col)
    print(f"✅ Лист «{title}» создан ({len(rows)} строк примера)")


def main() -> None:
    cfg = load_config(sheets_only=True)
    try:
        client_email = json.load(open(cfg.google_credentials_file, encoding="utf-8"))["client_email"]
    except Exception as e:
        sys.exit(f"Не удалось прочитать {cfg.google_credentials_file}: {e}")

    try:
        gc = gspread.service_account(filename=cfg.google_credentials_file, scopes=SCOPES)
        ss = gc.open_by_key(cfg.spreadsheet_id)
    except Exception as e:
        sys.exit(
            f"Не удалось открыть таблицу: {e!r}\n"
            f"Проверьте SPREADSHEET_ID и дайте доступ «Редактор» этому адресу:\n  {client_email}"
        )

    print(f"Таблица: {ss.title}")
    ensure_sheet(ss, cfg.sheet_services, SERVICES_HEADER, SERVICES, dropdown_col=len(SERVICES_HEADER) - 1)
    ensure_sheet(ss, cfg.sheet_tariffs, TARIFFS_HEADER, TARIFFS, dropdown_col=len(TARIFFS_HEADER) - 1)
    ensure_sheet(ss, cfg.sheet_slots, SLOTS_HEADER, make_slots())
    ensure_sheet(ss, cfg.sheet_bookings, BOOKING_HEADERS, [])
    print("\nГотово. Отредактируйте цены под себя и запускайте бота: python bot.py")


if __name__ == "__main__":
    main()
