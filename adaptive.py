"""Детерминированные правила извлечения Sheets. Содержимое записей не сохраняется."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
import re
import threading
import time
from typing import Any

VERSION = 1
MAX_DISCOVERY_CELLS = 1_000_000
ENGINE_REVISION = 2
KNOWN_PRICE_CLASSES = {"легковой", "кроссовер", "внедорожник", "минивэн", "внедорожник минивэн", "микроавтобус",
                       "седан", "хэтчбек", "универсал", "sedan", "suv", "crossover", "van", "minivan", "passenger car"}
NON_PRICE_HEADERS = {"id", "duration", "duration minutes", "длительность", "длительность мин", "время мин", "номер", "код"}
BOOKING_FIELDS = ["ID", "Создано", "Дата", "Время", "Имя", "Телефон", "Автомобиль", "Класс",
                  "Тариф", "Доп. услуги", "Состав", "Сумма", "Комментарий", "Telegram ID", "Username", "Статус"]
ALIASES = {
    "ID": ["id", "номер записи", "booking id", "номер брони"],
    "Создано": ["создано", "created", "created at", "дата создания"],
    "Дата": ["дата", "date", "день", "дата записи", "booking date"],
    "Время": ["время", "time", "начало", "время записи", "start time"],
    "Имя": ["имя", "клиент", "client", "name", "client name", "фио"],
    "Телефон": ["телефон", "phone", "тел", "номер телефона"],
    "Автомобиль": ["автомобиль", "машина", "car", "vehicle", "авто"],
    "Класс": ["класс", "класс авто", "car class", "size class"],
    "Тариф": ["тариф", "tariff", "package", "комплекс"],
    "Доп. услуги": ["доп услуги", "extras", "дополнительные услуги"],
    "Состав": ["состав", "composition", "items", "включено"],
    "Сумма": ["сумма", "total", "итого", "стоимость", "price", "цена"],
    "Комментарий": ["комментарий", "comment", "notes", "примечание"],
    "Telegram ID": ["telegram id", "tg id", "telegram_id", "tg_id", "id клиента"],
    "Username": ["username", "ник", "telegram username"],
    "Статус": ["статус", "status", "состояние"],
    "Постов": ["постов", "мест", "боксов", "capacity", "bays", "пост", "бокс"],
    "Услуга": ["услуга", "service", "service name", "название услуги"],
    "Описание": ["описание", "description", "details"],
    "Доступна": ["доступна", "available", "active", "enabled", "активна"],
    "Доступен": ["доступен", "available", "active", "enabled", "активен", "доступна"],
    "Скидка %": ["скидка", "скидка %", "discount", "discount percent"],
}
ROLE_FIELDS = {
    "bookings": BOOKING_FIELDS,
    "slots": ["Дата", "Время", "Постов"],
    "services": ["Услуга", "Описание", "Доступна"],
    "tariffs": ["Тариф", "Состав", "Скидка %", "Доступен"],
}
READ_REQUIRED = {
    "bookings": {"ID", "Дата", "Время", "Telegram ID", "Статус"},
    "slots": {"Дата", "Время", "Постов"},
    "services": {"Услуга"},
    "tariffs": {"Тариф", "Состав"},
}
STATUS_MAP = {"новая": "Новая", "new": "Новая", "confirmed": "Подтверждена",
              "подтверждена": "Подтверждена", "cancelled": "Отменена", "canceled": "Отменена",
              "отменена": "Отменена", "отмена": "Отменена", "выполнена": "Выполнена", "done": "Выполнена"}


class MappingError(RuntimeError):
    pass


def normalize_header(value: Any) -> str:
    return re.sub(r"[^\w%]+", " ", str(value).strip().lower().replace("ё", "е")).strip()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def cell_name(row: int, column: int) -> str:
    output = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        output = chr(65 + remainder) + output
    return f"{output}{row}"


def value_at(values: list[list], row: int, column: int) -> Any:
    if row < 1 or column < 1:
        return ""
    return values[row - 1][column - 1] if row <= len(values) and column <= len(values[row - 1]) else ""


def date_value(value: Any, transform: str = "date") -> str:
    from sheets import parse_date
    try:
        if transform == "serial_date":
            parsed_date = (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
        elif transform.startswith("date:"):
            parsed_date = datetime.strptime(str(value), transform[5:]).date()
        else:
            parsed_date = parse_date(value)
        if parsed_date:
            return parsed_date.strftime("%d.%m.%Y")
    except (ValueError, TypeError, OverflowError):
        pass
    raise MappingError("Дата не соответствует выбранному преобразованию")


def time_value(value: Any, transform: str = "time") -> str:
    from sheets import parse_time
    try:
        if transform == "serial_time":
            minutes = round(float(value) * 1440)
            if 0 <= minutes < 1440:
                return f"{minutes // 60:02d}:{minutes % 60:02d}"
        elif re.fullmatch(r"\s*\d{1,2}[:.]\d{2}(?::00)?\s*", str(value)):
            result = parse_time(value)
            if result:
                return result
    except (ValueError, TypeError, OverflowError):
        pass
    raise MappingError("Время не соответствует выбранному преобразованию")


def title_date(title: str) -> str | None:
    from sheets import parse_date
    matches = re.findall(r"\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4}", title)
    dates = {parsed_date.isoformat() for text in matches if (parsed_date := parse_date(text))}
    return next(iter(dates)) if len(dates) == 1 else None


def rule_signature(rule: dict) -> str:
    return fingerprint({field_name: field_value for field_name, field_value in rule.items()
                   if field_name not in {"structure", "fingerprint", "validation", "last_changes", "validated_rule"}})


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary_file = Path(name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary_file.replace(path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary_file.unlink(missing_ok=True)


def header_mapping(headers: list, role: str) -> tuple[dict, list[str]]:
    columns = {}
    errors = []
    for field in ROLE_FIELDS[role]:
        aliases = {normalize_header(alias) for alias in ALIASES[field]}
        hits = [index for index, header in enumerate(headers, 1) if normalize_header(header) in aliases]
        if len(hits) == 1:
            columns[field] = hits[0]
        elif len(hits) > 1:
            errors.append(f"Несколько колонок для {field}")
    return columns, errors


def trim_headers(headers: list) -> list:
    result = [str(header).strip() for header in headers]
    while result and not result[-1]:
        result.pop()
    return result


def structure(snapshot: dict, rule: dict) -> dict:
    row = rule["header_row"]
    headers = trim_headers(snapshot["values"][row - 1]) if row <= len(snapshot["values"]) else []
    formula_columns = sorted({column_index for row_index, column_index in snapshot["formulas"] if row_index >= rule["first_data_row"]})
    return {"sheet_id": snapshot["id"], "title": snapshot["title"], "headers": headers,
            "header_row": row, "merges": snapshot["merges"], "formula_columns": formula_columns,
            "column_count": snapshot["column_count"], "grid_columns": rule.get("grid_columns", []),
            "date_source": rule.get("date_source")}


def diff_structure(before: dict, after: dict) -> dict:
    previous_headers, current_headers = before.get("headers", []), after.get("headers", [])
    return {
        "added": [index for index, header in enumerate(current_headers, 1) if header and header not in previous_headers],
        "deleted": [index for index, header in enumerate(previous_headers, 1) if header and header not in current_headers],
        "moved": [header for header in previous_headers if header and previous_headers.count(header) == current_headers.count(header) == 1 and previous_headers.index(header) != current_headers.index(header)],
        "renamed": [{"column": index + 1, "before": old_header, "after": new_header} for index, (old_header, new_header) in enumerate(zip(previous_headers, current_headers))
                    if old_header != new_header and old_header not in current_headers and new_header not in previous_headers],
        "other": [field_name for field_name in after if field_name != "headers" and before.get(field_name) != after[field_name]],
    }


def discover(snapshot: dict, expected: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Смотрим только заголовки и типы цен; значения клиентов остаются в памяти."""
    rules = []
    issues = []
    for row_no, headers in enumerate(snapshot["values"][:20], 1):
        for role in ROLE_FIELDS:
            columns, errors = header_mapping(headers, role)
            source = {"title": True} if role in {"slots", "bookings"} and "Дата" not in columns and title_date(snapshot["title"]) else None
            present = set(columns) | ({"Дата"} if source else set())
            if errors and (snapshot["title"] == expected.get(role) or len(present & READ_REQUIRED[role]) >= len(READ_REQUIRED[role]) - 1):
                issues.extend(f"Лист {snapshot['id']}, строка {row_no}: {issue}" for issue in errors)
            if not READ_REQUIRED[role] <= present:
                continue
            # Слотоподобная строка записей не является отдельной таблицей слотов.
            if role == "slots" and ("Telegram ID" in header_mapping(headers, "bookings")[0]):
                continue
            if role == "tariffs" and ("Услуга" in header_mapping(headers, "services")[0] or
                                      "Telegram ID" in header_mapping(headers, "bookings")[0]):
                continue
            if errors:
                issues.extend(f"Лист {snapshot['id']}, строка {row_no}: {issue}" for issue in errors)
                continue
            if len(set(columns.values())) != len(columns):
                issues.append(f"Лист {snapshot['id']}: неоднозначные колонки {role}")
                continue
            pivot = False
            if role == "services":
                booking_columns, _ = header_mapping(headers, "bookings")
                if "Класс" in booking_columns and "Сумма" in booking_columns:
                    columns.update({field: booking_columns[field] for field in ("Класс", "Сумма")})
                    pivot = True
                used = set(columns.values())
                prices = []
                for column, header in enumerate(headers, 1):
                    if not header or column in used or normalize_header(header) in NON_PRICE_HEADERS:
                        continue
                    samples = [value_at(snapshot["values"], row_index, column) for row_index in range(row_no + 1, min(len(snapshot["values"]), row_no + 20) + 1)]
                    samples = [str(field_value).strip() for field_value in samples if str(field_value).strip()]
                    # Цена должна быть числом с необязательной валютой, без произвольного текста.
                    if samples and all(re.fullmatch(r"-?\d[\d\s]*(?:[.,]\d+)?\s*(?:₽|руб\.?|RUB)?", field_value, re.I) for field_value in samples):
                        columns[str(header)] = column
                        prices.append(str(header))
                if not prices and not pivot:
                    issues.append(f"Лист {snapshot['id']}: классы/колонки цен требуют ручной настройки")
                    continue
            rule = {"role": role, "sheet_id": snapshot["id"], "sheet_title": snapshot["title"],
                    "header_row": row_no, "first_data_row": row_no + 1, "end_row": None,
                    "columns": columns, "transforms": {"Дата": "date", "Время": "time"},
                    "status_map": dict(STATUS_MAP), "write_statuses": {"new": "Новая", "cancelled": "Отменена"},
                    "date_source": source, "permissions": {"read": True, "write": role == "bookings"},
                    "validation": {"read": False, "write": False, "issues": []}}
            if role == "services":
                rule["price_fields"] = prices
                if not pivot:
                    rule["unresolved_fields"] = [field for field in prices if normalize_header(field) not in KNOWN_PRICE_CLASSES]
                if pivot:
                    rule["pivot_prices"] = {"class_field": "Класс", "value_field": "Сумма"}
            rule["structure"] = structure(snapshot, rule)
            rule["fingerprint"] = fingerprint(rule["structure"])
            rules.append(rule)
    if not rules:
        grid = discover_grid(snapshot)
        if grid:
            rules.append(grid)
        else:
            issues.append(f"Лист {snapshot['id']}: структура не распознана; нужна ручная настройка")
    # Более одной таблицы одного назначения на листе не выбираем молча.
    for role in ROLE_FIELDS:
        candidates = [row_index for row_index in rules if row_index["role"] == role]
        if len(candidates) > 1:
            rules = [row_index for row_index in rules if row_index["role"] != role]
            issues.append(f"Лист {snapshot['id']}: несколько областей {role}; задайте границы вручную")
    return rules, issues


def discover_grid(snapshot: dict) -> dict | None:
    """Безопасный узкий случай: датированный лист, Время + Пост 1/Бокс 2..."""
    if not title_date(snapshot["title"]):
        return None
    for row_no, headers in enumerate(snapshot["values"][:20], 1):
        columns, _ = header_mapping(headers, "slots")
        bays = [index for index, header in enumerate(headers, 1) if re.fullmatch(r"(?:пост|бокс|bay)\s*\d+", str(header).strip(), re.I)]
        if "Время" not in columns or not bays:
            continue
        rule = {"role": "slots", "sheet_id": snapshot["id"], "sheet_title": snapshot["title"],
                "header_row": row_no, "first_data_row": row_no + 1, "end_row": None,
                "columns": {"Время": columns["Время"]}, "grid_columns": bays,
                "free_markers": ["", "свободно", "free", "open"], "date_source": {"title": True},
                "transforms": {"Дата": "date", "Время": "time"},
                "permissions": {"read": True, "write": False},
                "validation": {"read": False, "write": False, "issues": []}}
        rule["structure"] = structure(snapshot, rule)
        rule["fingerprint"] = fingerprint(rule["structure"])
        return rule
    return None


class AdaptiveSheets:
    def __init__(self, config, spreadsheet):
        self.config = config
        self.spreadsheet = spreadsheet
        self.path = Path(config.sheets_mapping_file)
        self.lock = threading.RLock()
        self.document = None
        self.mtime = None
        self.force_pending = config.sheets_force_schema_refresh
        self.worksheets = {}
        self.read_versions = {}
        self.expected = {role: getattr(config, f"sheet_{role}") for role in ROLE_FIELDS}

    def _load(self) -> None:
        mtime = self.path.stat().st_mtime_ns if self.path.exists() else None
        if self.document is not None and mtime == self.mtime:
            return
        if mtime is None:
            self.document = None
            return
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("version") != VERSION or document.get("spreadsheet_id") != self.config.spreadsheet_id:
                raise ValueError
            if not isinstance(document.get("regions"), list) or not isinstance(document.get("checked_at"), (int, float)):
                raise ValueError
            if not math.isfinite(document["checked_at"]):
                raise ValueError
        except (ValueError, OSError):
            raise MappingError("Некорректная или чужая конфигурация Sheets; проверьте файл mappings") from None
        for rule in document["regions"]:
            if not isinstance(rule, dict) or rule.get("role") not in ROLE_FIELDS:
                raise MappingError("Некорректная область mappings")
            validation = rule.get("validation")
            if (rule.get("validated_rule") != rule_signature(rule) or not isinstance(validation, dict) or
                any(type(validation.get(field_name)) is not bool for field_name in ("read", "write"))):
                rule["validation"] = {"read": False, "write": False,
                                      "issues": ["Ручные изменения требуют --validate-mappings"]}
        self.document = document
        if document.get("engine_revision") != ENGINE_REVISION:
            self.force_pending = True
        self._summarize()
        self.mtime = mtime

    def _save(self) -> None:
        atomic_json(self.path, self.document)
        self.mtime = self.path.stat().st_mtime_ns

    def _worksheet(self, sheet_id: int):
        if sheet_id not in self.worksheets:
            self.worksheets[sheet_id] = self.spreadsheet().get_worksheet_by_id(sheet_id)
        return self.worksheets[sheet_id]

    def _typed_formulas(self, sheet_id: int, title: str, candidates: list[tuple[int, int]]) -> set[tuple[int, int]]:
        # FORMULA render returns literal '=...' strings too; only formulaValue is authoritative.
        found = set()
        prefix = "'" + title.replace("'", "''") + "'!"
        if not candidates:
            return found
        rows, columns = zip(*candidates)
        area = prefix + cell_name(min(rows), min(columns)) + ":" + cell_name(max(rows), max(columns))
        metadata = self.spreadsheet().fetch_sheet_metadata(params={
            "includeGridData": True, "ranges": [area],
            "fields": "sheets(properties(sheetId),data(startRow,startColumn,rowData(values(userEnteredValue(formulaValue)))))"})
        sheets = [sheet for sheet in metadata.get("sheets", []) if sheet.get("properties", {}).get("sheetId") == sheet_id]
        if len(sheets) != 1:
            raise MappingError("Не удалось проверить тип ячеек с формулами")
        for block in sheets[0].get("data", []):
            for row_index, row in enumerate(block.get("rowData", []), block.get("startRow", 0) + 1):
                for column_index, cell in enumerate(row.get("values", []), block.get("startColumn", 0) + 1):
                    if cell.get("userEnteredValue", {}).get("formulaValue") is not None:
                        found.add((row_index, column_index))
        return found & set(candidates)

    def _snapshots(self) -> list[dict]:
        spreadsheet = self.spreadsheet()
        metadata = spreadsheet.fetch_sheet_metadata(params={"fields": "sheets(properties,merges),namedRanges"})
        self.named_ranges = [{"name": row_index.get("name"), "range": row_index.get("range")} for row_index in metadata.get("namedRanges", [])]
        entries = [sheet for sheet in metadata.get("sheets", []) if sheet["properties"].get("sheetType", "GRID") == "GRID"]
        self.unsupported_sheets = [f"Лист {sheet['properties']['sheetId']}: тип {sheet['properties'].get('sheetType')} не поддерживается"
                                   for sheet in metadata.get("sheets", []) if sheet not in entries]
        ranges = []
        self.worksheets.clear()
        for entry in entries:
            props = entry["properties"]
            grid = props.get("gridProperties", {})
            if grid.get("rowCount", 0) * grid.get("columnCount", 0) > MAX_DISCOVERY_CELLS:
                raise MappingError(f"Лист {props['sheetId']} слишком большой для безопасного автоанализа")
            ranges.append("'" + props["title"].replace("'", "''") + "'")
            # Конструктор использует уже полученные метаданные, без повторного HTTP GET.
            from gspread import Worksheet
            self.worksheets[props["sheetId"]] = Worksheet(spreadsheet, props, spreadsheet.id, spreadsheet.client)
        formatted, formulas = [], []
        for offset in range(0, len(ranges), 50):
            group = ranges[offset:offset + 50]
            formatted.extend(spreadsheet.values_batch_get(group, params={"valueRenderOption": "FORMATTED_VALUE"}).get("valueRanges", []))
            formulas.extend(spreadsheet.values_batch_get(group, params={"valueRenderOption": "FORMULA"}).get("valueRanges", []))
        if len(formatted) != len(entries) or len(formulas) != len(entries):
            raise MappingError("Google вернул неполный набор диапазонов")
        snapshots = []
        for entry, values, formula_values in zip(entries, formatted, formulas):
            props = entry["properties"]
            grid = props["gridProperties"]
            positions = [(row_index, column_index) for row_index, row in enumerate(formula_values.get("values", []), 1)
                         for column_index, cell_value in enumerate(row, 1) if str(cell_value).startswith("=")]
            positions = sorted(self._typed_formulas(props["sheetId"], props["title"], positions))
            snapshots.append({"id": props["sheetId"], "title": props["title"], "values": values.get("values", []),
                              "formulas": positions, "raw_values": formula_values.get("values", []), "merges": entry.get("merges", []),
                              "column_count": grid.get("columnCount", 0), "row_count": grid.get("rowCount", 0)})
        return snapshots

    def _rule_structure(self, rule: dict, snapshot: dict) -> list[str]:
        errors = []
        columns = rule.get("columns", {})
        if rule.get("role") not in ROLE_FIELDS or not isinstance(columns, dict):
            raise MappingError("Неверное назначение области или columns")
        if any(
            not isinstance(field, str) or type(column_index) is not int
            for field, column_index in columns.items()
        ):
            raise MappingError("columns: нужны строковые имена полей и целочисленные номера")
        for key, expected_type in (
            ("transforms", dict),
            ("status_map", dict),
            ("write_statuses", dict),
            ("price_fields", list),
            ("grid_columns", list),
            ("unresolved_fields", list),
        ):
            if key in rule and not isinstance(rule[key], expected_type):
                raise MappingError("Некорректная структура параметра " + key)
        if not columns or any(
            column_index < 1 or column_index > snapshot["column_count"] for column_index in columns.values()
        ):
            errors.append("Некорректные номера колонок")
        if len(set(columns.values())) != len(columns):
            errors.append("Два поля ссылаются на одну колонку")
        allowed = set(ROLE_FIELDS[rule["role"]]) | set(rule.get("price_fields", []))
        if rule.get("pivot_prices"):
            allowed.update({"Класс", "Сумма"})
        if set(columns) - allowed:
            errors.append("Неизвестные поля columns")
        first = rule.get("first_data_row")
        header = rule.get("header_row")
        end_row = rule.get("end_row")
        if (
            type(header) is not int
            or type(first) is not int
            or not 1 <= header < first <= snapshot["row_count"]
        ):
            raise MappingError("Некорректные границы области")
        if end_row is not None and (
            type(end_row) is not int or end_row < first or end_row > snapshot["row_count"]
        ):
            raise MappingError("Некорректная end_row")
        permissions = rule.get("permissions", {})
        if not isinstance(permissions, dict) or any(
            type(permissions.get(field_name)) is not bool for field_name in ("read", "write")
        ):
            raise MappingError("permissions.read/write должны быть true или false")
        return errors

    def _rule_fields(self, rule: dict, snapshot: dict, errors: list[str]) -> set[str]:
        columns = rule["columns"]
        present = set(columns)
        source = rule.get("date_source")
        if source is not None:
            if not isinstance(source, dict) or set(source) not in ({"title"}, {"cell"}):
                errors.append("Некорректный date_source")
            else:
                try:
                    if "cell" in source and (
                        not isinstance(source["cell"], list)
                        or len(source["cell"]) != 2
                        or any(
                            type(field_value) is not int or field_value < 1 for field_value in source["cell"]
                        )
                    ):
                        raise MappingError("Некорректная ссылка даты")
                    if source.get("title") is not True and "cell" not in source:
                        raise MappingError("Некорректный источник даты")
                    self._date_source(rule, snapshot["values"], snapshot["title"])
                    present.add("Дата")
                except MappingError:
                    errors.append("Не подтверждён источник даты")
        if rule.get("grid_columns"):
            if not all(
                type(column_index) is int and 1 <= column_index <= snapshot["column_count"]
                for column_index in rule["grid_columns"]
            ):
                errors.append("Некорректные grid_columns")
            present.add("Постов")
        for field, transform in rule.get("transforms", {}).items():
            valid = {"Дата": {"date", "serial_date"}, "Время": {"time", "serial_time"}}
            if (
                field not in valid
                or not isinstance(transform, str)
                or not (
                    transform in valid[field]
                    or (field == "Дата" and transform.startswith("date:") and len(transform) > 5)
                )
            ):
                errors.append("Некорректное преобразование даты/времени")
        missing = READ_REQUIRED[rule["role"]] - present
        if missing:
            errors.append("Не хватает полей: " + ", ".join(sorted(missing)))
        if rule["role"] == "services" and not rule.get("price_fields") and not rule.get("pivot_prices"):
            errors.append("Не заданы price_fields или pivot_prices")
        if rule.get("pivot_prices"):
            pivot = rule["pivot_prices"]
            if not isinstance(pivot, dict) or any(
                pivot.get(field_name) not in columns for field_name in ("class_field", "value_field")
            ):
                errors.append("Некорректное правило pivot_prices")
        if any(price_field not in columns for price_field in rule.get("price_fields", [])):
            errors.append("Неизвестная колонка цены")
        return present

    def _rule_read_issues(self, rule: dict, snapshot: dict, errors: list[str]) -> set[int]:
        header, end_row = rule["header_row"], rule.get("end_row")
        # Объединения в области данных/заголовка могут менять принадлежность ячеек.
        if any(
            merge.get("endRowIndex", 0) > header - 1
            and merge.get("startRowIndex", 0) < (end_row or snapshot["row_count"])
            for merge in snapshot["merges"]
        ):
            errors.append("Объединённые ячейки пересекают область")
        header_values = snapshot["values"][header - 1] if header <= len(snapshot["values"]) else []
        raw_headers = snapshot["raw_values"][header - 1] if header <= len(snapshot["raw_values"]) else []
        formula_headers = {
            column_index for row_index, column_index in snapshot["formulas"] if row_index == header
        }
        if any(
            str(value_at([header_values], 1, column_index)).strip()
            != str(value_at([raw_headers], 1, column_index)).strip()
            for column_index in range(1, max(len(header_values), len(raw_headers)) + 1)
            if column_index not in formula_headers
        ):
            errors.append("Шапка изменилась во время чтения")
        unresolved = set(rule.get("unresolved_fields", []))
        if unresolved and (
            rule["role"] in {"services", "tariffs"}
            or unresolved & (READ_REQUIRED[rule["role"]] | {"date_source", "grid_columns"})
        ):
            errors.append("Не подтверждены зависимости изменённых колонок")
        return formula_headers

    def _rule_write_issues(self, rule, snapshot, errors, present, formula_headers) -> list[str]:
        columns = rule["columns"]
        first, end_row = rule["first_data_row"], rule.get("end_row")
        unresolved = set(rule.get("unresolved_fields", []))
        write_errors = list(errors)
        if unresolved:
            write_errors.append("Требуется ручное сопоставление изменённых полей")
        if formula_headers & set(columns.values()):
            write_errors.append("Формулы в шапке записи")
        if rule["role"] != "bookings" or rule.get("grid_columns"):
            write_errors.append("Область доступна только для чтения")
        if set(BOOKING_FIELDS) - present:
            write_errors.append("Для создания записей нужны все поля записи")
        if any(
            column_index in columns.values()
            and row_index >= first
            and (end_row is None or row_index <= end_row)
            for row_index, column_index in snapshot["formulas"]
        ):
            write_errors.append("Формулы в колонках записи: запись заблокирована")
        statuses = rule.get("write_statuses", {})
        status_map = {
            normalize_header(field_name): field_value
            for field_name, field_value in rule.get("status_map", {}).items()
        }
        if rule["role"] == "bookings" and (
            status_map.get(normalize_header(statuses.get("new", ""))) != "Новая"
            or status_map.get(normalize_header(statuses.get("cancelled", ""))) != "Отменена"
        ):
            write_errors.append("Не подтверждены значения статусов записи/отмены")
        return write_errors

    def _validate_rule(self, rule: dict, snapshot: dict) -> dict:
        errors = self._rule_structure(rule, snapshot)
        present = self._rule_fields(rule, snapshot, errors)
        formula_headers = self._rule_read_issues(rule, snapshot, errors)
        write_errors = self._rule_write_issues(rule, snapshot, errors, present, formula_headers)
        read_ok = not errors and rule["permissions"]["read"]
        rule["validation"] = {
            "read": read_ok,
            "write": read_ok and not write_errors and rule["permissions"]["write"],
            "issues": errors,
            "write_issues": write_errors,
        }
        rule["validated_rule"] = rule_signature(rule)
        return rule

    def _relocate(self, rule: dict, snapshot: dict) -> dict:
        """Сохраняем ручные правила; пересчитываем только сдвинутые/изменённые колонки."""
        updated = deepcopy(rule)
        previous_headers = rule["structure"]["headers"]
        current_headers = trim_headers(snapshot["values"][rule["header_row"] - 1]) if rule["header_row"] <= len(snapshot["values"]) else []
        aliases, ambiguous = header_mapping(current_headers, rule["role"])
        if rule.get("pivot_prices"):
            extras, extra_errors = header_mapping(current_headers, "bookings")
            aliases.update({field_name: field_value for field_name, field_value in extras.items() if field_name in {"Класс", "Сумма"}})
            ambiguous.extend(issue for issue in extra_errors if any(field in issue for field in ("Класс", "Сумма")))
        columns = {}
        unresolved = list(rule.get("unresolved_fields", []))
        for field, column in rule["columns"].items():
            old_header = str(previous_headers[column - 1]) if column <= len(previous_headers) else ""
            hits = [index for index, header in enumerate(current_headers, 1) if old_header and str(header) == old_header]
            if len(hits) == 1:
                columns[field] = hits[0]
            elif field in aliases and not any(field in issue for issue in ambiguous):
                columns[field] = aliases[field]
            else:
                unresolved.append(field)
        updated["columns"] = columns
        if (rule.get("date_source") or {}).get("cell") and previous_headers != current_headers:
            unresolved.append("date_source")
        if rule.get("grid_columns"):
            grid = []
            for column in rule["grid_columns"]:
                label = previous_headers[column - 1] if column <= len(previous_headers) else None
                hits = [index for index, header in enumerate(current_headers, 1) if header == label]
                if len(hits) != 1:
                    unresolved.append("grid_columns")
                else:
                    grid.append(hits[0])
            updated["grid_columns"] = grid
        updated["sheet_title"] = snapshot["title"]
        updated["structure"] = structure(snapshot, updated)
        updated["fingerprint"] = fingerprint(updated["structure"])
        updated["last_changes"] = diff_structure(rule["structure"], updated["structure"])
        updated["unresolved_fields"] = sorted(set(unresolved))
        self._validate_rule(updated, snapshot)
        if unresolved or ambiguous:
            if set(unresolved) & (READ_REQUIRED[rule["role"]] | set(rule.get("price_fields", []))) or ambiguous:
                updated["validation"]["read"] = False
            updated["validation"]["write"] = False
            updated["validation"]["issues"].append("Требуется ручное сопоставление: " + ", ".join(unresolved + ambiguous))
        return updated

    def refresh(self, force: bool = False, validate: bool = False, _observed: dict | None = None) -> dict:
        with self.lock:
            self._load()
            now = time.time()
            if self.document and not (force or validate or self.force_pending):
                if 0 <= now - self.document.get("checked_at", 0) < self.config.sheets_schema_check_interval_hours * 3600:
                    return self.document
            snapshots = self._snapshots()
            by_id = {snapshot["id"]: snapshot for snapshot in snapshots}
            if _observed is not None:
                _observed.update(by_id)
            issues = list(self.unsupported_sheets)
            if self.document is None:
                regions = []
                for snapshot in snapshots:
                    rules, found_issues = discover(snapshot, self.expected)
                    issues.extend(found_issues)
                    regions.extend(self._validate_rule(rule, snapshot) for rule in rules)
                self.document = {"version": VERSION, "spreadsheet_id": self.config.spreadsheet_id,
                                 "regions": regions, "relationships": [
                                     {"from": "bookings", "to": "slots", "fields": ["Дата", "Время"]},
                                     {"from": "tariffs", "to": "services", "fields": ["Состав", "Услуга"]}],
                                 "issues": issues}
            else:
                regions = []
                known = set()
                for rule in self.document["regions"]:
                    snapshot = by_id.get(rule["sheet_id"])
                    known.add(rule["sheet_id"])
                    if not snapshot:
                        rule["validation"] = {"read": False, "write": False, "issues": ["Лист удалён"]}
                    elif validate:
                        rule = deepcopy(rule)
                        rule.pop("unresolved_fields", None)
                        self._validate_rule(rule, snapshot)
                        rule["structure"] = structure(snapshot, rule)
                        rule["fingerprint"] = fingerprint(rule["structure"])
                    elif rule.get("validated_rule") != rule_signature(rule):
                        rule["validation"] = {"read": False, "write": False,
                                              "issues": ["Ручные изменения требуют --validate-mappings"]}
                    elif fingerprint(structure(snapshot, rule)) != rule.get("fingerprint"):
                        rule = self._relocate(rule, snapshot)
                    else:
                        if self.document.get("engine_revision") != ENGINE_REVISION and rule["role"] == "services" and not rule.get("pivot_prices"):
                            rule["unresolved_fields"] = sorted(set(rule.get("unresolved_fields", [])) |
                                {field for field in rule.get("price_fields", []) if normalize_header(field) not in KNOWN_PRICE_CLASSES})
                        self._validate_rule(rule, snapshot)
                    regions.append(rule)
                for snapshot in snapshots:
                    if snapshot["id"] not in known:
                        rules, found_issues = discover(snapshot, self.expected)
                        issues.extend(found_issues)
                        regions.extend(self._validate_rule(rule, snapshot) for rule in rules)
                self.document["regions"] = regions
                self.document["issues"] = issues
            self.document["named_ranges"] = self.named_ranges
            self.document["checked_at"] = now
            self.document["structural_fingerprint"] = fingerprint([
                {"id": snapshot["id"], "title": snapshot["title"], "columns": snapshot["column_count"], "merges": snapshot["merges"]}
                for snapshot in snapshots] + self.named_ranges + [rule.get("fingerprint") for rule in self.document["regions"]])
            self.document["engine_revision"] = ENGINE_REVISION
            self._summarize()
            self._save()
            self.force_pending = False
            return self.document

    def _summarize(self) -> None:
        self.document["missing_roles"] = sorted(set(ROLE_FIELDS) - {rule["role"] for rule in self.document["regions"]})
        ambiguous_roles = [role for role in ROLE_FIELDS
                       if len(group := [rule for rule in self.document["regions"] if rule["role"] == role]) > 1
                       and not all(rule.get("date_source") for rule in group)]
        self.document["ambiguous_roles"] = ambiguous_roles
        self.document["validation_status"] = "valid" if not self.document.get("missing_roles") and not ambiguous_roles and all(
            rule["validation"]["read"] for rule in self.document["regions"]) else "needs_review"
        self.document["writes_enabled"] = (self.document["validation_status"] == "valid" and
            all(rule["validation"].get("write") for rule in self.document["regions"] if rule["role"] == "bookings") and
            not any(rule.get("grid_columns") for rule in self.document["regions"]))

    def _rules(self, role: str) -> list[dict]:
        self.refresh()
        rules = [rule for rule in self.document["regions"] if rule["role"] == role]
        if not rules:
            raise MappingError(f"Нет подтверждённой области {role}; настройте mappings")
        # Несколько обычных таблиц одной роли без явных дат/границ неоднозначны.
        if len(rules) > 1 and not all(rule.get("date_source") for rule in rules):
            raise MappingError(f"Несколько областей {role}; удалите лишние правила или задайте date_source")
        if not all(rule["validation"].get("read") for rule in rules):
            raise MappingError(f"Чтение {role} заблокировано: требуется проверка mappings")
        return rules

    def _date_source(self, rule: dict, values: list[list], title: str) -> str:
        source = rule.get("date_source") or {}
        if source.get("title"):
            return date_value(title_date(title) or "")
        if source.get("cell"):
            return date_value(value_at(values, *source["cell"]), rule.get("transforms", {}).get("Дата", "date"))
        raise MappingError("Не задан источник даты")

    def _extract_records(self, rule: dict, values: list[list], title: str) -> list[dict]:
        records = []
        end_row = min(len(values), rule.get("end_row") or len(values))
        for row in range(rule["first_data_row"], end_row + 1):
            record = {field: str(value_at(values, row, column_index)).strip() for field, column_index in rule["columns"].items()}
            if not any(record.values()):
                continue
            transforms = rule.get("transforms", {})
            if rule.get("date_source"):
                record["Дата"] = self._date_source(rule, values, title)
            elif "Дата" in record:
                record["Дата"] = date_value(record["Дата"], transforms.get("Дата", "date"))
            if rule.get("grid_columns"):
                free = {str(field_value).strip().casefold() for field_value in rule["free_markers"]}
                record["Постов"] = str(sum(str(value_at(values, row, column_index)).strip().casefold() in free
                                          for column_index in rule["grid_columns"]))
                record["__free_capacity__"] = True
            if "Время" in record:
                record["Время"] = time_value(record["Время"], transforms.get("Время", "time"))
            if "Статус" in record:
                statuses = {normalize_header(field_name): field_value for field_name, field_value in rule.get("status_map", {}).items()}
                record["Статус"] = statuses.get(normalize_header(record["Статус"]), record["Статус"])
            record["__row__"], record["__sheet_id__"] = row, rule["sheet_id"]
            records.append(record)
        return records

    def observe(self, force: bool = False, roles=ROLE_FIELDS) -> dict:
        """Свежий пакет выбранных ролей; по умолчанию все четыре, без кэша между операциями."""
        with self.lock:
            observed = {}
            self.refresh(force=force, _observed=observed)
            rules = [rule for role in roles for rule in self._rules(role)]
            missing = {rule["sheet_id"] for rule in rules} - observed.keys()
            identifiers = sorted(missing)
            for offset in range(0, len(identifiers), 50):
                group = identifiers[offset:offset + 50]
                ranges = ["'" + self._worksheet(sheet_id).title.replace("'", "''") + "'" for sheet_id in group]
                try:
                    values = self.spreadsheet().values_batch_get(ranges, params={"valueRenderOption": "FORMATTED_VALUE"}).get("valueRanges", [])
                except Exception as error:
                    status = getattr(getattr(error, "response", None), "status_code", None)
                    if force or (status not in {400, 404} and type(error).__name__ != "WorksheetNotFound"):
                        raise
                    return self.observe(force=True, roles=roles)
                if len(values) != len(group):
                    raise MappingError("Google вернул неполный снимок таблиц")
                observed.update({sheet_id: {"values": value.get("values", [])} for sheet_id, value in zip(group, values)})
            for rule in rules:
                values = observed[rule["sheet_id"]]["values"]
                headers = trim_headers(values[rule["header_row"]-1]) if len(values) >= rule["header_row"] else []
                if headers != rule["structure"]["headers"] or self._worksheet(rule["sheet_id"]).title != rule["sheet_title"]:
                    if force:
                        raise MappingError("Структура продолжает меняться; повторите запрос")
                    return self.observe(force=True, roles=roles)
            return observed

    def table(self, role: str, _attempt: int = 0, _observed: dict | None = None) -> tuple[list[str], list[dict]]:
        with self.lock:
            rules = self._rules(role)
            headers, records = [], []
            dates = set()
            for rule in rules:
                worksheet = self._worksheet(rule["sheet_id"])
                try:
                    values = _observed[rule["sheet_id"]]["values"] if _observed is not None else worksheet.get_all_values()
                except Exception as error:
                    status = getattr(getattr(error, "response", None), "status_code", None)
                    if not _attempt and (status in {400, 404} or type(error).__name__ == "WorksheetNotFound"):
                        self.refresh(force=True)
                        return self.table(role, _attempt=1)
                    raise
                if len(rules) > 1 and rule.get("date_source"):
                    source_date = self._date_source(rule, values, worksheet.title)
                    if source_date in dates:
                        raise MappingError("Несколько областей одной роли для одной даты")
                    dates.add(source_date)
                current = trim_headers(values[rule["header_row"] - 1]) if len(values) >= rule["header_row"] else []
                if current != rule["structure"]["headers"] or worksheet.title != rule["sheet_title"]:
                    if _observed is not None:
                        self.refresh(force=True)
                        raise MappingError("Структура изменилась во время снимка; повторите запрос")
                    if _attempt:
                        raise MappingError("Структура продолжает меняться; повторите чтение позже")
                    self.refresh(force=True)
                    return self.table(role, _attempt=1)
                fields = list(rule["columns"])
                if rule.get("date_source") and "Дата" not in fields:
                    fields.append("Дата")
                if rule.get("grid_columns"):
                    fields.append("Постов")
                headers.extend(field for field in fields if field not in headers)
                records.extend(self._extract_records(rule, values, worksheet.title))
            if role == "services" and any(record.get("pivot_prices") for record in rules):
                if len(rules) != 1:
                    raise MappingError("Для длинного прайса нужна одна область services")
                headers, records = self._pivot_prices(rules[0], records)
            if role == "services":
                classes = [header for header in headers if header not in {"Услуга", "Описание", "Доступна"}]
                if len({header.casefold() for header in classes}) != len(classes):
                    raise MappingError("Повторяющиеся классы автомобилей")
            self._validate_records(role, records)
            self.read_versions[role] = fingerprint((headers, records))
            return headers, records

    def _pivot_prices(self, rule: dict, records: list[dict]) -> tuple[list[str], list[dict]]:
        pivot = rule["pivot_prices"]
        by_service = {}
        classes = []
        for record in records:
            name = record.get("Услуга", "")
            matched_class = record.get(pivot["class_field"], "")
            price = record.get(pivot["value_field"], "")
            if not name or not matched_class or matched_class.casefold() in {"услуга", "описание", "доступна"}:
                raise MappingError("Длинный прайс: нет услуги/класса или класс совпадает со служебным полем")
            if matched_class not in classes:
                classes.append(matched_class)
            item = by_service.setdefault(name.casefold(), {"Услуга": name, "Описание": record.get("Описание", ""),
                                                          "Доступна": record.get("Доступна", ""),
                                                          "__row__": record["__row__"], "__sheet_id__": record["__sheet_id__"]})
            if matched_class in item:
                raise MappingError("Длинный прайс: повтор услуги и класса")
            if item["Описание"] != record.get("Описание", "") or item["Доступна"] != record.get("Доступна", ""):
                raise MappingError("Длинный прайс: разные описания/доступность одной услуги")
            item[matched_class] = price
        return ["Услуга", "Описание", *classes, "Доступна"], list(by_service.values())

    def _validate_records(self, role: str, records: list[dict]) -> None:
        from sheets import parse_date, parse_time, to_number
        if role in {"services", "tariffs"}:
            yes_no = {"", "да", "yes", "true", "1", "y", "+", "✓", "✅", "истина", "есть",
                      "нет", "no", "false", "0", "n", "-", "❌", "ложь"}
            for record in records:
                available = record.get("Доступна" if role == "services" else "Доступен", "").strip().casefold()
                if available not in yes_no:
                    raise MappingError("Неизвестное значение доступности услуги/тарифа")
                if role == "services":
                    price_fields = [field_name for field_name in record if field_name not in {"Услуга", "Описание", "Доступна", "__row__", "__sheet_id__"}]
                    if len({field_name.casefold() for field_name in price_fields}) != len(price_fields):
                        raise MappingError("Повторяющиеся классы автомобилей")
                    for field in price_fields:
                        value = record.get(field, "")
                        if value and (not re.fullmatch(r"\d[\d\s]*(?:[.,]\d{1,2})?\s*(?:₽|руб\.?|RUB)?", value, re.I) or
                                      (amount := to_number(value)) is None or not math.isfinite(amount)):
                            raise MappingError("Некорректная цена услуги")
                else:
                    discount = record.get("Скидка %", "")
                    if discount and (not re.fullmatch(r"(?:\d+(?:[.,]\d+)?)\s*%?", discount) or
                                     not 0 <= (to_number(discount) or 0) <= 100):
                        raise MappingError("Некорректная скидка")
                    items = [item.strip().casefold() for item in re.split(r"[,;\n]", record.get("Состав", "")) if item.strip()]
                    if len(items) != len(set(items)):
                        raise MappingError("Повторяющиеся услуги в составе тарифа")
            field = "Услуга" if role == "services" else "Тариф"
            names = [record.get(field, "").casefold() for record in records]
            if any(not name for name in names) or len(names) != len(set(names)):
                raise MappingError(f"Пустые или повторяющиеся названия {role}")
            return
        if role not in {"slots", "bookings"}:
            return
        identifiers = set()
        for record in records:
            if not parse_date(record.get("Дата", "")) or not parse_time(record.get("Время", "")):
                raise MappingError(f"Некорректная дата/время {role}, лист {record['__sheet_id__']}, строка {record['__row__']}")
            if role == "bookings":
                key = record.get("ID", "")
                if not key or key in identifiers or not record.get("Telegram ID", ""):
                    raise MappingError("Отсутствует или повторяется идентификатор записи/клиента")
                if not re.fullmatch(r"[1-9]\d*", record["Telegram ID"]):
                    raise MappingError("Некорректный Telegram ID клиента")
                identifiers.add(key)
            else:
                raw = record.get("Постов", "")
                try:
                    number = float(raw.replace(",", "."))
                    if number < 0 or not number.is_integer():
                        raise ValueError
                except ValueError:
                    raise MappingError("Некорректное количество постов") from None

    def _preflight(self) -> dict:
        # Один пакет чтения используется также для проверки зависимостей цены и занятости.
        observed = {}
        self.refresh(force=True, _observed=observed)
        for role in ("services", "tariffs", "slots", "bookings"):
            self.table(role, _observed=observed)
        if any(rule.get("grid_columns") for rule in self._rules("slots")):
            raise MappingError("Сетка постов доступна только для чтения; создание/отмена записей заблокированы")
        return observed

    def _target(self, day: str | None = None, sheet_id: int | None = None) -> dict:
        rules = self._rules("bookings")
        if sheet_id is not None:
            rules = [rule for rule in rules if rule["sheet_id"] == sheet_id]
        elif len(rules) > 1 or rules[0].get("date_source"):
            rules = [rule for rule in rules if self._date_source(rule, self._worksheet(rule["sheet_id"]).get_all_values() if (rule.get("date_source") or {}).get("cell") else [],
                                                          rule["sheet_title"]) == date_value(day)]
        if len(rules) != 1 or not rules[0]["validation"].get("write"):
            raise MappingError("Запись в выбранную область не разрешена; проверьте mappings")
        return rules[0]

    def _journal(self) -> tuple[Path, dict]:
        path = self.path.with_name(self.path.stem + "-operations.json")
        if path.exists():
            try:
                entries = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(entries, dict) or not all(isinstance(field_value, dict) and field_value.get("state") in {"pending", "done"}
                                                           for field_value in entries.values()):
                    raise ValueError
                return path, entries
            except ValueError:
                raise MappingError("Журнал операций повреждён; нужна ручная проверка") from None
        return path, {}

    def create(self, data: dict, expected_bookings: list[dict] | None = None) -> None:
        with self.lock:
            expected_versions = dict(self.read_versions)
            observed = self._preflight()
            for role in ROLE_FIELDS:
                _, current_records = self.table(role, _observed=observed)
                if role not in expected_versions or self.read_versions[role] != expected_versions[role]:
                    raise MappingError("Данные или зависимости изменились перед записью; обновите предложение")
                if role == "bookings" and expected_bookings is not None and fingerprint(current_records) != fingerprint(expected_bookings):
                    raise MappingError("Записи изменились перед созданием; обновите свободное время")
            rule = self._target(data["дата"])
            worksheet = self._worksheet(rule["sheet_id"])
            # Не повторяем неопределённую операцию после сетевого сбоя/перезапуска.
            key = fingerprint({field_name: str(data[field_name]).strip().casefold() for field_name in ("telegram id", "дата", "время", "автомобиль")})
            journal_path, journal = self._journal()
            if key in journal:
                raise MappingError("Такая операция уже выполнена или ожидает ручной сверки; повтор заблокирован")
            values = worksheet.get_all_values(value_render_option="FORMULA", date_time_render_option="FORMATTED_STRING")
            if rule.get("date_source") and self._date_source(rule, values, worksheet.title) != date_value(data["дата"]):
                raise MappingError("Дата области изменилась непосредственно перед записью")
            if expected_bookings is not None:
                target_records = [row_index for row_index in expected_bookings if row_index["__sheet_id__"] == rule["sheet_id"]]
                if fingerprint(self._extract_records(rule, values, worksheet.title)) != fingerprint(target_records):
                    raise MappingError("Строки изменились непосредственно перед записью")
            raw_header = trim_headers(values[rule["header_row"] - 1]) if len(values) >= rule["header_row"] else []
            if raw_header != rule["structure"]["headers"]:
                raise MappingError("Шапка изменилась непосредственно перед записью")
            used = [row_index for row_index in range(rule["first_data_row"], min(len(values), rule.get("end_row") or len(values)) + 1)
                    if any(str(field_value).strip() for field_value in values[row_index - 1])]
            row = max(used, default=rule["first_data_row"] - 1) + 1
            if row > (rule.get("end_row") or worksheet.row_count):
                raise MappingError("В области записи нет свободных строк; расширьте её вручную")
            if any(str(field_value).strip() for field_value in (values[row - 1] if row <= len(values) else [])):
                raise MappingError("Строка назначения уже занята")
            updates = []
            for field, column in rule["columns"].items():
                value = data.get(field.lower(), "")
                if field == "Статус":
                    value = rule["write_statuses"]["new"]
                if field == "Дата":
                    transform = rule.get("transforms", {}).get("Дата", "date")
                    if transform == "serial_date":
                        value = (datetime.strptime(value, "%d.%m.%Y") - datetime(1899, 12, 30)).days
                    elif transform.startswith("date:"):
                        value = datetime.strptime(value, "%d.%m.%Y").strftime(transform[5:])
                if field == "Время" and rule.get("transforms", {}).get("Время") == "serial_time":
                    hour, minute = map(int, value.split(":"))
                    value = (hour * 60 + minute) / 1440
                updates.append({"range": cell_name(row, column), "values": [[value]]})
            journal[key] = {"state": "pending", "sheet_id": rule["sheet_id"], "row": row, "at": time.time()}
            atomic_json(journal_path, journal)
            try:
                worksheet.batch_update(updates, value_input_option="RAW")
            except Exception:
                raise MappingError("Результат записи неизвестен. Проверьте Sheets и локальный журнал перед повтором") from None
            journal[key]["state"] = "done"
            atomic_json(journal_path, journal)

    def cancel(self, record: dict, telegram_id: int) -> None:
        with self.lock:
            self._preflight()
            rule = self._target(sheet_id=record["__sheet_id__"])
            _, records = self.table("bookings")
            matches = [candidate for candidate in records if candidate["ID"] == record["ID"]]
            if len(matches) != 1:
                raise MappingError("Идентификатор записи неоднозначен")
            current = matches[0]
            identity = ("ID", "Telegram ID", "Дата", "Время", "Статус", "__row__", "__sheet_id__")
            if any(current.get(field_name) != record.get(field_name) for field_name in identity) or current["Telegram ID"] != str(telegram_id):
                raise MappingError("Запись изменилась; отмена заблокирована")
            worksheet = self._worksheet(rule["sheet_id"])
            # Ещё одна адресная проверка в FORMULA перед изменением одной ячейки.
            row = current["__row__"]
            raw = worksheet.get(f"A{row}:{cell_name(row, max(rule['columns'].values()))}", value_render_option="FORMULA", date_time_render_option="FORMATTED_STRING")
            candidates = [(row, column_index) for column_index, field_value in enumerate(raw[0] if raw else [], 1) if str(field_value).startswith("=")]
            if self._typed_formulas(rule["sheet_id"], worksheet.title, candidates):
                raise MappingError("Формула в строке записи; отмена заблокирована")
            row_rule = {**rule, "first_data_row": 1, "end_row": 1, "date_source": None}
            candidates = self._extract_records(row_rule, raw, worksheet.title)
            if len(candidates) != 1 or any(candidates[0].get(field) != current.get(field) for field in rule["columns"]):
                raise MappingError("Строка записи перемещена или изменена; отмена заблокирована")
            journal_path, journal = self._journal()
            worksheet.batch_update([{"range": cell_name(row, rule["columns"]["Статус"]),
                              "values": [[rule["write_statuses"]["cancelled"]]]}], value_input_option="RAW")
            key = fingerprint({field_name.lower(): str(current.get(field_name, "")).strip().casefold()
                          for field_name in ("Telegram ID", "Дата", "Время", "Автомобиль")})
            if journal.get(key, {}).get("state") == "done":
                del journal[key]
                atomic_json(journal_path, journal)

    def validate_reschedule(self, record: dict, new_date: str) -> dict:
        rule = self._target(sheet_id=record["__sheet_id__"])
        if rule.get("date_source") and date_value(new_date) != date_value(record["Дата"]):
            raise MappingError("Перенос между отдельными областями дат требует администратора; исходная запись сохранена")
        if "Время" not in rule["columns"] or "Дата" not in rule["columns"] and date_value(new_date) != date_value(record["Дата"]):
            raise MappingError("Нет доступных для изменения колонок даты и времени")
        return rule

    def reschedule(self, record: dict, telegram_id: int, new_date: str, new_time: str) -> None:
        with self.lock:
            versions = dict(self.read_versions)
            observed = self._preflight()
            for role in ("slots", "bookings"):
                self.table(role, _observed=observed)
                if role not in versions or self.read_versions[role] != versions[role]:
                    raise MappingError("Слоты или записи изменились перед переносом; проверьте время заново")
            rule = self.validate_reschedule(record, new_date)
            _, records = self.table("bookings", _observed=observed)
            matches = [candidate for candidate in records if candidate["ID"] == record["ID"]]
            if len(matches) != 1 or matches[0] != record or record["Telegram ID"] != str(telegram_id):
                raise MappingError("Исходная запись изменилась; перенос заблокирован")
            worksheet, row = self._worksheet(rule["sheet_id"]), record["__row__"]
            raw = worksheet.get(f"A{row}:{cell_name(row, max(rule['columns'].values()))}",
                                value_render_option="FORMULA", date_time_render_option="FORMATTED_STRING")
            formulas = [(row, column) for column, value in enumerate(raw[0] if raw else [], 1) if str(value).startswith("=")]
            if self._typed_formulas(rule["sheet_id"], worksheet.title, formulas):
                raise MappingError("В строке записи есть формула; перенос заблокирован")
            row_rule = {**rule, "first_data_row": 1, "end_row": 1, "date_source": None}
            candidates = self._extract_records(row_rule, raw, worksheet.title)
            if len(candidates) != 1 or any(candidates[0].get(field) != record.get(field) for field in rule["columns"]):
                raise MappingError("Строка записи перемещена или изменена; перенос заблокирован")
            date = datetime.strptime(new_date, "%Y-%m-%d")
            updates = []
            for field, value in (("Дата", date.strftime("%d.%m.%Y")), ("Время", new_time)):
                if field not in rule["columns"]:
                    continue
                transform = rule.get("transforms", {}).get(field, "")
                if field == "Дата" and transform == "serial_date":
                    value = (date - datetime(1899, 12, 30)).days
                elif field == "Дата" and transform.startswith("date:"):
                    value = date.strftime(transform[5:])
                elif field == "Время" and transform == "serial_time":
                    hour, minute = map(int, new_time.split(":"))
                    value = (hour * 60 + minute) / 1440
                updates.append({"range": cell_name(row, rule["columns"][field]), "values": [[value]]})
            path, journal = self._journal()
            key = fingerprint({"move": record, "date": new_date, "time": new_time})
            if journal.get(key, {}).get("state") == "pending":
                raise MappingError("Результат прошлого переноса неизвестен; нужна ручная сверка")
            journal[key] = {"state": "pending", "sheet_id": rule["sheet_id"], "row": row, "at": time.time()}
            atomic_json(path, journal)
            try:
                worksheet.batch_update(updates, value_input_option="RAW")
            except Exception:
                raise MappingError("Результат переноса неизвестен. Проверьте таблицу перед повтором") from None
            journal[key]["state"] = "done"
            original = fingerprint({field.lower(): str(record.get(field, "")).strip().casefold()
                                    for field in ("Telegram ID", "Дата", "Время", "Автомобиль")})
            if journal.get(original, {}).get("state") == "done":
                journal.pop(original)
            atomic_json(path, journal)
