"""Только синтетические таблицы. Сетевые клиенты здесь не создаются."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import re

from config import Config
from sheets import BOOKING_HEADERS, Store


def coord(cell):
    letters, digits = re.fullmatch(r"([A-Z]+)(\d+)", cell).groups()
    column = 0
    for c in letters:
        column = column * 26 + ord(c) - 64
    return int(digits), column


class Worksheet:
    def __init__(self, id, title, values):
        self.id, self.title, self.values = id, title, deepcopy(values)
        self.row_count, self.col_count = 100, 30
        self.merges = []
        self.reads = 0
        self.writes = []
        self.formatting = {"A1": {"bold": True}, "A2:Z100": {"numberFormat": "TEXT"}}
        self.literal_cells = set()
        self.before_get = None
        self.fail_write = False

    def get_all_values(self, value_render_option=None, date_time_render_option=None):
        self.reads += 1
        result = deepcopy(self.values)
        if value_render_option != "FORMULA":
            result = [["42" if str(v).startswith("=") and (r, c) not in self.literal_cells else str(v)
                       for c, v in enumerate(row, 1)] for r, row in enumerate(result, 1)]
        return result

    def get(self, range_name, value_render_option=None, date_time_render_option=None):
        if self.before_get:
            fn, self.before_get = self.before_get, None
            fn()
        start, end = range_name.split(":")
        r1, c1 = coord(start)
        r2, c2 = coord(end)
        return [[self.values[r - 1][c - 1] if r <= len(self.values) and c <= len(self.values[r - 1]) else ""
                 for c in range(c1, c2 + 1)] for r in range(r1, r2 + 1)]

    def batch_update(self, updates, value_input_option=None):
        assert value_input_option == "RAW"
        if self.fail_write:
            raise OSError("simulated unknown response")
        self.writes.append(deepcopy(updates))
        for update in updates:
            row, column = coord(update["range"])
            while len(self.values) < row:
                self.values.append([])
            while len(self.values[row - 1]) < column:
                self.values[row - 1].append("")
            self.values[row - 1][column - 1] = update["values"][0][0]

    def update_cell(self, row, column, value):
        from adaptive import cell_name
        self.batch_update([{"range": cell_name(row, column), "values": [[value]]}], "RAW")

    def append_row(self, row, value_input_option=None):
        assert value_input_option == "RAW"
        self.values.append(deepcopy(row))
        self.writes.append(deepcopy(row))


class Spreadsheet:
    def __init__(self, sheets):
        self.sheets = sheets
        self.id = "synthetic"
        from tests.test_gspread_adapter import OfflineHTTP
        self.client = OfflineHTTP(self)
        self.metadata_reads = 0

    def fetch_sheet_metadata(self, params=None):
        self.metadata_reads += 1
        result = {"sheets": [{"properties": {"sheetId": s.id, "title": s.title, "sheetType": "GRID",
                                          "gridProperties": {"rowCount": s.row_count, "columnCount": s.col_count}},
                            "merges": deepcopy(s.merges)} for s in self.sheets]}
        if (params or {}).get("includeGridData"):
            for entry, worksheet in zip(result["sheets"], self.sheets):
                entry["data"] = [{"startRow": 0, "startColumn": 0, "rowData": [
                    {"values": [{"userEnteredValue": {"formulaValue": v}} if str(v).startswith("=") and (r, c) not in worksheet.literal_cells else {}
                                for c, v in enumerate(row, 1)]} for r, row in enumerate(worksheet.values, 1)]}]
        return result

    def values_batch_get(self, ranges, params=None):
        self.batch_reads = getattr(self, "batch_reads", 0) + 1
        return {"valueRanges": [{"values": self.worksheet(name[1:-1].replace("''", "'")).get_all_values(
            value_render_option=params.get("valueRenderOption"))} for name in ranges]}

    def get_worksheet_by_id(self, id):
        return next(s for s in self.sheets if s.id == id)

    def worksheet(self, title):
        return next(s for s in self.sheets if s.title == title)


def config(path, **kwargs):
    cfg = Config(bot_token="123456:synthetic_token_never_used_12345", ai_api_key="synthetic",
                 ai_model="existing-model", spreadsheet_id="synthetic-spreadsheet",
                 google_credentials_file="unused", timezone="Europe/Moscow", carwash_name="Test",
                 carwash_address="", carwash_phone="", currency="₽", admin_chat_id=None,
                 min_lead_minutes=0, catalog_cache_sec=60, slots_cache_sec=15, max_history=24,
                 sheet_services="Услуги", sheet_tariffs="Тарифы", sheet_slots="Слоты", sheet_bookings="Записи",
                 sheet_classes="Классы",
                 sheets_adaptive_enabled=True, sheets_mapping_file=str(Path(path) / "mapping.json"))
    return replace(cfg, **kwargs)


def fixture(path, **kwargs):
    cfg = config(path, **kwargs)
    spreadsheet = Spreadsheet([
        Worksheet(1, "Услуги", [["Услуга", "Описание", "Легковой", "Кроссовер", "Доступна"],
                                 ["Мойка", "Кузов", "700", "800", "да"], ["Воск", "Защита", "200", "300", "нет"]]),
        Worksheet(2, "Тарифы", [["Тариф", "Состав", "Скидка %", "Доступен"], ["Базовый", "Мойка, Воск", "10", "да"]]),
        Worksheet(3, "Слоты", [["Дата", "Время", "Постов"], ["01.01.2099", "10:00", "2"]]),
        Worksheet(4, "Записи", [BOOKING_HEADERS])])
    store = Store(cfg)
    store._spreadsheet = spreadsheet
    return store, spreadsheet


def booking(**kwargs):
    args = dict(date_str="2099-01-01", time_str="10:00", name="Synthetic", phone="00000000000", car="Fixture car",
                size_class="Легковой", tariff="Базовый", services=[], comment="", telegram_id=111, username=None)
    args.update(kwargs)
    return args


def suggest_mappings(adaptive, client=None):
    from mapping_ai import suggest_mappings as suggest
    if client is None:
        return suggest(adaptive)
    from unittest.mock import patch
    with patch("openai.OpenAI", return_value=client):
        return suggest(adaptive)
