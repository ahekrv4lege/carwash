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


def norm(value: Any) -> str:
    return re.sub(r"[^\w%]+", " ", str(value).strip().lower().replace("ё", "е")).strip()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def cell_name(row: int, col: int) -> str:
    out = ""
    while col:
        col, rem = divmod(col - 1, 26)
        out = chr(65 + rem) + out
    return f"{out}{row}"


def value_at(values: list[list], row: int, col: int) -> Any:
    if row < 1 or col < 1:
        return ""
    return values[row - 1][col - 1] if row <= len(values) and col <= len(values[row - 1]) else ""


def date_value(value: Any, transform: str = "date") -> str:
    from sheets import parse_date
    try:
        if transform == "serial_date":
            d = (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
        elif transform.startswith("date:"):
            d = datetime.strptime(str(value), transform[5:]).date()
        else:
            d = parse_date(value)
        if d:
            return d.strftime("%d.%m.%Y")
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
    dates = {d.isoformat() for text in matches if (d := parse_date(text))}
    return next(iter(dates)) if len(dates) == 1 else None


def rule_signature(rule: dict) -> str:
    return digest({k: v for k, v in rule.items()
                   if k not in {"structure", "fingerprint", "validation", "last_changes", "validated_rule"}})


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        temp.replace(path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def header_mapping(headers: list, role: str) -> tuple[dict, list[str]]:
    columns = {}
    errors = []
    for field in ROLE_FIELDS[role]:
        aliases = {norm(a) for a in ALIASES[field]}
        hits = [i for i, h in enumerate(headers, 1) if norm(h) in aliases]
        if len(hits) == 1:
            columns[field] = hits[0]
        elif len(hits) > 1:
            errors.append(f"Несколько колонок для {field}")
    return columns, errors


def trim_headers(headers: list) -> list:
    result = [str(h).strip() for h in headers]
    while result and not result[-1]:
        result.pop()
    return result


def structure(snapshot: dict, rule: dict) -> dict:
    row = rule["header_row"]
    headers = trim_headers(snapshot["values"][row - 1]) if row <= len(snapshot["values"]) else []
    formula_columns = sorted({c for r, c in snapshot["formulas"] if r >= rule["first_data_row"]})
    return {"sheet_id": snapshot["id"], "title": snapshot["title"], "headers": headers,
            "header_row": row, "merges": snapshot["merges"], "formula_columns": formula_columns,
            "column_count": snapshot["column_count"], "grid_columns": rule.get("grid_columns", []),
            "date_source": rule.get("date_source")}


def diff_structure(before: dict, after: dict) -> dict:
    old, new = before.get("headers", []), after.get("headers", [])
    return {
        "added": [i for i, h in enumerate(new, 1) if h and h not in old],
        "deleted": [i for i, h in enumerate(old, 1) if h and h not in new],
        "moved": [h for h in old if h and old.count(h) == new.count(h) == 1 and old.index(h) != new.index(h)],
        "renamed": [{"column": i + 1, "before": a, "after": b} for i, (a, b) in enumerate(zip(old, new))
                    if a != b and a not in new and b not in old],
        "other": [k for k in after if k != "headers" and before.get(k) != after[k]],
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
                issues.extend(f"Лист {snapshot['id']}, строка {row_no}: {e}" for e in errors)
            if not READ_REQUIRED[role] <= present:
                continue
            # Слотоподобная строка записей не является отдельной таблицей слотов.
            if role == "slots" and ("Telegram ID" in header_mapping(headers, "bookings")[0]):
                continue
            if role == "tariffs" and ("Услуга" in header_mapping(headers, "services")[0] or
                                      "Telegram ID" in header_mapping(headers, "bookings")[0]):
                continue
            if errors:
                issues.extend(f"Лист {snapshot['id']}, строка {row_no}: {e}" for e in errors)
                continue
            if len(set(columns.values())) != len(columns):
                issues.append(f"Лист {snapshot['id']}: неоднозначные колонки {role}")
                continue
            pivot = False
            if role == "services":
                booking_columns, _ = header_mapping(headers, "bookings")
                if "Класс" in booking_columns and "Сумма" in booking_columns:
                    columns.update({f: booking_columns[f] for f in ("Класс", "Сумма")})
                    pivot = True
                used = set(columns.values())
                prices = []
                for col, h in enumerate(headers, 1):
                    if not h or col in used or norm(h) in NON_PRICE_HEADERS:
                        continue
                    samples = [value_at(snapshot["values"], r, col) for r in range(row_no + 1, min(len(snapshot["values"]), row_no + 20) + 1)]
                    samples = [str(v).strip() for v in samples if str(v).strip()]
                    # Цена должна быть числом с необязательной валютой, без произвольного текста.
                    if samples and all(re.fullmatch(r"-?\d[\d\s]*(?:[.,]\d+)?\s*(?:₽|руб\.?|RUB)?", v, re.I) for v in samples):
                        columns[str(h)] = col
                        prices.append(str(h))
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
                    rule["unresolved_fields"] = [field for field in prices if norm(field) not in KNOWN_PRICE_CLASSES]
                if pivot:
                    rule["pivot_prices"] = {"class_field": "Класс", "value_field": "Сумма"}
            rule["structure"] = structure(snapshot, rule)
            rule["fingerprint"] = digest(rule["structure"])
            rules.append(rule)
    if not rules:
        grid = discover_grid(snapshot)
        if grid:
            rules.append(grid)
        else:
            issues.append(f"Лист {snapshot['id']}: структура не распознана; нужна ручная настройка")
    # Более одной таблицы одного назначения на листе не выбираем молча.
    for role in ROLE_FIELDS:
        candidates = [r for r in rules if r["role"] == role]
        if len(candidates) > 1:
            rules = [r for r in rules if r["role"] != role]
            issues.append(f"Лист {snapshot['id']}: несколько областей {role}; задайте границы вручную")
    return rules, issues


def discover_grid(snapshot: dict) -> dict | None:
    """Безопасный узкий случай: датированный лист, Время + Пост 1/Бокс 2..."""
    if not title_date(snapshot["title"]):
        return None
    for row_no, headers in enumerate(snapshot["values"][:20], 1):
        columns, _ = header_mapping(headers, "slots")
        bays = [i for i, h in enumerate(headers, 1) if re.fullmatch(r"(?:пост|бокс|bay)\s*\d+", str(h).strip(), re.I)]
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
        rule["fingerprint"] = digest(rule["structure"])
        return rule
    return None


class AdaptiveSheets:
    def __init__(self, cfg, spreadsheet):
        self.cfg = cfg
        self.spreadsheet = spreadsheet
        self.path = Path(cfg.sheets_mapping_file)
        self.lock = threading.RLock()
        self.document = None
        self.mtime = None
        self.force_pending = cfg.sheets_force_schema_refresh
        self.worksheets = {}
        self.read_versions = {}
        self.expected = {role: getattr(cfg, f"sheet_{role}") for role in ROLE_FIELDS}

    def _load(self) -> None:
        mtime = self.path.stat().st_mtime_ns if self.path.exists() else None
        if self.document is not None and mtime == self.mtime:
            return
        if mtime is None:
            self.document = None
            return
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or doc.get("version") != VERSION or doc.get("spreadsheet_id") != self.cfg.spreadsheet_id:
                raise ValueError
            if not isinstance(doc.get("regions"), list) or not isinstance(doc.get("checked_at"), (int, float)):
                raise ValueError
            if not math.isfinite(doc["checked_at"]):
                raise ValueError
        except (ValueError, OSError):
            raise MappingError("Некорректная или чужая конфигурация Sheets; проверьте файл mappings") from None
        for rule in doc["regions"]:
            if not isinstance(rule, dict) or rule.get("role") not in ROLE_FIELDS:
                raise MappingError("Некорректная область mappings")
            validation = rule.get("validation")
            if (rule.get("validated_rule") != rule_signature(rule) or not isinstance(validation, dict) or
                any(type(validation.get(k)) is not bool for k in ("read", "write"))):
                rule["validation"] = {"read": False, "write": False,
                                      "issues": ["Ручные изменения требуют --validate-mappings"]}
        self.document = doc
        if doc.get("engine_revision") != ENGINE_REVISION:
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
        rows, cols = zip(*candidates)
        area = prefix + cell_name(min(rows), min(cols)) + ":" + cell_name(max(rows), max(cols))
        metadata = self.spreadsheet().fetch_sheet_metadata(params={
            "includeGridData": True, "ranges": [area],
            "fields": "sheets(properties(sheetId),data(startRow,startColumn,rowData(values(userEnteredValue(formulaValue)))))"})
        sheets = [sheet for sheet in metadata.get("sheets", []) if sheet.get("properties", {}).get("sheetId") == sheet_id]
        if len(sheets) != 1:
            raise MappingError("Не удалось проверить тип ячеек с формулами")
        for block in sheets[0].get("data", []):
            for r, row in enumerate(block.get("rowData", []), block.get("startRow", 0) + 1):
                for c, cell in enumerate(row.get("values", []), block.get("startColumn", 0) + 1):
                    if cell.get("userEnteredValue", {}).get("formulaValue") is not None:
                        found.add((r, c))
        return found & set(candidates)

    def _snapshots(self) -> list[dict]:
        ss = self.spreadsheet()
        metadata = ss.fetch_sheet_metadata(params={"fields": "sheets(properties,merges),namedRanges"})
        self.named_ranges = [{"name": r.get("name"), "range": r.get("range")} for r in metadata.get("namedRanges", [])]
        entries = [s for s in metadata.get("sheets", []) if s["properties"].get("sheetType", "GRID") == "GRID"]
        self.unsupported_sheets = [f"Лист {s['properties']['sheetId']}: тип {s['properties'].get('sheetType')} не поддерживается"
                                   for s in metadata.get("sheets", []) if s not in entries]
        ranges = []
        self.worksheets.clear()
        for entry in entries:
            props = entry["properties"]
            grid = props.get("gridProperties", {})
            if grid.get("rowCount", 0) * grid.get("columnCount", 0) > MAX_DISCOVERY_CELLS:
                raise MappingError(f"Лист {props['sheetId']} слишком большой для безопасного автоанализа")
            ranges.append("'" + props["title"].replace("'", "''") + "'")
            # Конструктор использует уже полученные метаданные, без повторного HTTP GET.
            if hasattr(ss, "client"):
                from gspread import Worksheet
                self.worksheets[props["sheetId"]] = Worksheet(ss, props, ss.id, ss.client)
            else:
                self.worksheets[props["sheetId"]] = ss.get_worksheet_by_id(props["sheetId"])
        formatted, formulas = [], []
        for offset in range(0, len(ranges), 50):
            group = ranges[offset:offset + 50]
            formatted.extend(ss.values_batch_get(group, params={"valueRenderOption": "FORMATTED_VALUE"}).get("valueRanges", []))
            formulas.extend(ss.values_batch_get(group, params={"valueRenderOption": "FORMULA"}).get("valueRanges", []))
        if len(formatted) != len(entries) or len(formulas) != len(entries):
            raise MappingError("Google вернул неполный набор диапазонов")
        snapshots = []
        for entry, values, formula_values in zip(entries, formatted, formulas):
            props = entry["properties"]
            grid = props["gridProperties"]
            positions = [(r, c) for r, row in enumerate(formula_values.get("values", []), 1)
                         for c, val in enumerate(row, 1) if str(val).startswith("=")]
            positions = sorted(self._typed_formulas(props["sheetId"], props["title"], positions))
            snapshots.append({"id": props["sheetId"], "title": props["title"], "values": values.get("values", []),
                              "formulas": positions, "raw_values": formula_values.get("values", []), "merges": entry.get("merges", []),
                              "column_count": grid.get("columnCount", 0), "row_count": grid.get("rowCount", 0)})
        return snapshots

    def _validate_rule(self, rule: dict, snapshot: dict) -> dict:
        errors = []
        columns = rule.get("columns", {})
        if rule.get("role") not in ROLE_FIELDS or not isinstance(columns, dict):
            raise MappingError("Неверное назначение области или columns")
        if any(not isinstance(f, str) or type(c) is not int for f, c in columns.items()):
            raise MappingError("columns: нужны строковые имена полей и целочисленные номера")
        for key, expected_type in (("transforms", dict), ("status_map", dict), ("write_statuses", dict),
                                   ("price_fields", list), ("grid_columns", list), ("unresolved_fields", list)):
            if key in rule and not isinstance(rule[key], expected_type):
                raise MappingError("Некорректная структура параметра " + key)
        if not columns or any(c < 1 or c > snapshot["column_count"] for c in columns.values()):
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
        end = rule.get("end_row")
        if type(header) is not int or type(first) is not int or not 1 <= header < first <= snapshot["row_count"]:
            raise MappingError("Некорректные границы области")
        if end is not None and (type(end) is not int or end < first or end > snapshot["row_count"]):
            raise MappingError("Некорректная end_row")
        permissions = rule.get("permissions", {})
        if not isinstance(permissions, dict) or any(type(permissions.get(k)) is not bool for k in ("read", "write")):
            raise MappingError("permissions.read/write должны быть true или false")
        present = set(columns)
        source = rule.get("date_source")
        if source is not None:
            if not isinstance(source, dict) or set(source) not in ({"title"}, {"cell"}):
                errors.append("Некорректный date_source")
            else:
                try:
                    if "cell" in source and (not isinstance(source["cell"], list) or len(source["cell"]) != 2 or
                        any(type(v) is not int or v < 1 for v in source["cell"])):
                        raise MappingError("Некорректная ссылка даты")
                    if source.get("title") is not True and "cell" not in source:
                        raise MappingError("Некорректный источник даты")
                    self._date_source(rule, snapshot["values"], snapshot["title"])
                    present.add("Дата")
                except MappingError:
                    errors.append("Не подтверждён источник даты")
        if rule.get("grid_columns"):
            if not all(type(c) is int and 1 <= c <= snapshot["column_count"] for c in rule["grid_columns"]):
                errors.append("Некорректные grid_columns")
            present.add("Постов")
        for field, transform in rule.get("transforms", {}).items():
            valid = {"Дата": {"date", "serial_date"}, "Время": {"time", "serial_time"}}
            if field not in valid or not isinstance(transform, str) or not (transform in valid[field] or
                    (field == "Дата" and transform.startswith("date:") and len(transform) > 5)):
                errors.append("Некорректное преобразование даты/времени")
        missing = READ_REQUIRED[rule["role"]] - present
        if missing:
            errors.append("Не хватает полей: " + ", ".join(sorted(missing)))
        if rule["role"] == "services" and not rule.get("price_fields") and not rule.get("pivot_prices"):
            errors.append("Не заданы price_fields или pivot_prices")
        if rule.get("pivot_prices"):
            pivot = rule["pivot_prices"]
            if not isinstance(pivot, dict) or any(pivot.get(k) not in columns for k in ("class_field", "value_field")):
                errors.append("Некорректное правило pivot_prices")
        if any(p not in columns for p in rule.get("price_fields", [])):
            errors.append("Неизвестная колонка цены")
        # Объединения в области данных/заголовка могут менять принадлежность ячеек.
        if any(m.get("endRowIndex", 0) > header - 1 and
               m.get("startRowIndex", 0) < (end or snapshot["row_count"]) for m in snapshot["merges"]):
            errors.append("Объединённые ячейки пересекают область")
        header_values = snapshot["values"][header - 1] if header <= len(snapshot["values"]) else []
        raw_headers = snapshot["raw_values"][header - 1] if header <= len(snapshot["raw_values"]) else []
        formula_headers = {c for r, c in snapshot["formulas"] if r == header}
        if any(str(value_at([header_values], 1, c)).strip() != str(value_at([raw_headers], 1, c)).strip()
               for c in range(1, max(len(header_values), len(raw_headers)) + 1) if c not in formula_headers):
            errors.append("Шапка изменилась во время чтения")
        unresolved = set(rule.get("unresolved_fields", []))
        if unresolved and (rule["role"] in {"services", "tariffs"} or
                           unresolved & (READ_REQUIRED[rule["role"]] | {"date_source", "grid_columns"})):
            errors.append("Не подтверждены зависимости изменённых колонок")
        read_ok = not errors and bool(rule.get("permissions", {}).get("read"))
        write_errors = list(errors)
        if unresolved:
            write_errors.append("Требуется ручное сопоставление изменённых полей")
        if formula_headers & set(columns.values()):
            write_errors.append("Формулы в шапке записи")
        if rule["role"] != "bookings" or rule.get("grid_columns"):
            write_errors.append("Область доступна только для чтения")
        if set(BOOKING_FIELDS) - present:
            write_errors.append("Для создания записей нужны все поля записи")
        if any(c in columns.values() and r >= first and (end is None or r <= end) for r, c in snapshot["formulas"]):
            write_errors.append("Формулы в колонках записи: запись заблокирована")
        statuses = rule.get("write_statuses", {})
        status_map = {norm(k): v for k, v in rule.get("status_map", {}).items()}
        if rule["role"] == "bookings" and (status_map.get(norm(statuses.get("new", ""))) != "Новая" or
                                           status_map.get(norm(statuses.get("cancelled", ""))) != "Отменена"):
            write_errors.append("Не подтверждены значения статусов записи/отмены")
        rule["validation"] = {"read": read_ok,
                              "write": read_ok and not write_errors and bool(rule.get("permissions", {}).get("write")),
                              "issues": errors, "write_issues": write_errors}
        rule["validated_rule"] = rule_signature(rule)
        return rule

    def _relocate(self, rule: dict, snapshot: dict) -> dict:
        """Сохраняем ручные правила; пересчитываем только сдвинутые/изменённые колонки."""
        updated = deepcopy(rule)
        old = rule["structure"]["headers"]
        new = trim_headers(snapshot["values"][rule["header_row"] - 1]) if rule["header_row"] <= len(snapshot["values"]) else []
        aliases, ambiguous = header_mapping(new, rule["role"])
        if rule.get("pivot_prices"):
            extras, extra_errors = header_mapping(new, "bookings")
            aliases.update({k: v for k, v in extras.items() if k in {"Класс", "Сумма"}})
            ambiguous.extend(e for e in extra_errors if any(f in e for f in ("Класс", "Сумма")))
        columns = {}
        unresolved = list(rule.get("unresolved_fields", []))
        for field, col in rule["columns"].items():
            header = str(old[col - 1]) if col <= len(old) else ""
            hits = [i for i, h in enumerate(new, 1) if header and str(h) == header]
            if len(hits) == 1:
                columns[field] = hits[0]
            elif field in aliases and not any(field in a for a in ambiguous):
                columns[field] = aliases[field]
            else:
                unresolved.append(field)
        updated["columns"] = columns
        if (rule.get("date_source") or {}).get("cell") and old != new:
            unresolved.append("date_source")
        if rule.get("grid_columns"):
            grid = []
            for col in rule["grid_columns"]:
                label = old[col - 1] if col <= len(old) else None
                hits = [i for i, h in enumerate(new, 1) if h == label]
                if len(hits) != 1:
                    unresolved.append("grid_columns")
                else:
                    grid.append(hits[0])
            updated["grid_columns"] = grid
        updated["sheet_title"] = snapshot["title"]
        updated["structure"] = structure(snapshot, updated)
        updated["fingerprint"] = digest(updated["structure"])
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
                if 0 <= now - self.document.get("checked_at", 0) < self.cfg.sheets_schema_check_interval_hours * 3600:
                    return self.document
            snapshots = self._snapshots()
            by_id = {s["id"]: s for s in snapshots}
            if _observed is not None:
                _observed.update(by_id)
            issues = list(self.unsupported_sheets)
            if self.document is None:
                regions = []
                for snapshot in snapshots:
                    rules, found_issues = discover(snapshot, self.expected)
                    issues.extend(found_issues)
                    regions.extend(self._validate_rule(rule, snapshot) for rule in rules)
                self.document = {"version": VERSION, "spreadsheet_id": self.cfg.spreadsheet_id,
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
                        rule["fingerprint"] = digest(rule["structure"])
                    elif rule.get("validated_rule") != rule_signature(rule):
                        rule["validation"] = {"read": False, "write": False,
                                              "issues": ["Ручные изменения требуют --validate-mappings"]}
                    elif digest(structure(snapshot, rule)) != rule.get("fingerprint"):
                        rule = self._relocate(rule, snapshot)
                    else:
                        if self.document.get("engine_revision") != ENGINE_REVISION and rule["role"] == "services" and not rule.get("pivot_prices"):
                            rule["unresolved_fields"] = sorted(set(rule.get("unresolved_fields", [])) |
                                {f for f in rule.get("price_fields", []) if norm(f) not in KNOWN_PRICE_CLASSES})
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
            missing_roles = set(ROLE_FIELDS) - {r["role"] for r in self.document["regions"]}
            self.document["missing_roles"] = sorted(missing_roles)
            self.document["checked_at"] = now
            self.document["structural_fingerprint"] = digest([
                {"id": s["id"], "title": s["title"], "columns": s["column_count"], "merges": s["merges"]}
                for s in snapshots] + self.named_ranges + [r.get("fingerprint") for r in self.document["regions"]])
            self.document["engine_revision"] = ENGINE_REVISION
            self._summarize()
            self._save()
            self.force_pending = False
            return self.document

    def _summarize(self) -> None:
        self.document["missing_roles"] = sorted(set(ROLE_FIELDS) - {r["role"] for r in self.document["regions"]})
        ambiguous_roles = [role for role in ROLE_FIELDS
                       if len(group := [r for r in self.document["regions"] if r["role"] == role]) > 1
                       and not all(r.get("date_source") for r in group)]
        self.document["ambiguous_roles"] = ambiguous_roles
        self.document["validation_status"] = "valid" if not self.document.get("missing_roles") and not ambiguous_roles and all(
            r["validation"]["read"] for r in self.document["regions"]) else "needs_review"
        self.document["writes_enabled"] = (self.document["validation_status"] == "valid" and
            all(r["validation"].get("write") for r in self.document["regions"] if r["role"] == "bookings") and
            not any(r.get("grid_columns") for r in self.document["regions"]))

    def _rules(self, role: str) -> list[dict]:
        self.refresh()
        rules = [r for r in self.document["regions"] if r["role"] == role]
        if not rules:
            raise MappingError(f"Нет подтверждённой области {role}; настройте mappings")
        # Несколько обычных таблиц одной роли без явных дат/границ неоднозначны.
        if len(rules) > 1 and not all(r.get("date_source") for r in rules):
            raise MappingError(f"Несколько областей {role}; удалите лишние правила или задайте date_source")
        if not all(r["validation"].get("read") for r in rules):
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
        end = min(len(values), rule.get("end_row") or len(values))
        for row in range(rule["first_data_row"], end + 1):
            rec = {f: str(value_at(values, row, c)).strip() for f, c in rule["columns"].items()}
            if not any(rec.values()):
                continue
            transforms = rule.get("transforms", {})
            if rule.get("date_source"):
                rec["Дата"] = self._date_source(rule, values, title)
            elif "Дата" in rec:
                rec["Дата"] = date_value(rec["Дата"], transforms.get("Дата", "date"))
            if rule.get("grid_columns"):
                free = {str(v).strip().casefold() for v in rule["free_markers"]}
                rec["Постов"] = str(sum(str(value_at(values, row, c)).strip().casefold() in free
                                          for c in rule["grid_columns"]))
            if "Время" in rec:
                rec["Время"] = time_value(rec["Время"], transforms.get("Время", "time"))
            if "Статус" in rec:
                statuses = {norm(k): v for k, v in rule.get("status_map", {}).items()}
                rec["Статус"] = statuses.get(norm(rec["Статус"]), rec["Статус"])
            rec["__row__"], rec["__sheet_id__"] = row, rule["sheet_id"]
            records.append(rec)
        return records

    def observe(self, force: bool = False) -> dict:
        """Свежие данные четырёх ролей одним пакетом; не кэш между операциями."""
        with self.lock:
            observed = {}
            self.refresh(force=force, _observed=observed)
            rules = [rule for role in ROLE_FIELDS for rule in self._rules(role)]
            missing = {r["sheet_id"] for r in rules} - observed.keys()
            ids = sorted(missing)
            for offset in range(0, len(ids), 50):
                group = ids[offset:offset + 50]
                ranges = ["'" + self._worksheet(i).title.replace("'", "''") + "'" for i in group]
                try:
                    values = self.spreadsheet().values_batch_get(ranges, params={"valueRenderOption": "FORMATTED_VALUE"}).get("valueRanges", [])
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if force or (status not in {400, 404} and type(exc).__name__ != "WorksheetNotFound"):
                        raise
                    return self.observe(force=True)
                if len(values) != len(group):
                    raise MappingError("Google вернул неполный снимок таблиц")
                observed.update({i: {"values": value.get("values", [])} for i, value in zip(group, values)})
            for rule in rules:
                values = observed[rule["sheet_id"]]["values"]
                headers = trim_headers(values[rule["header_row"]-1]) if len(values) >= rule["header_row"] else []
                if headers != rule["structure"]["headers"] or self._worksheet(rule["sheet_id"]).title != rule["sheet_title"]:
                    if force:
                        raise MappingError("Структура продолжает меняться; повторите запрос")
                    return self.observe(force=True)
            return observed

    def table(self, role: str, _attempt: int = 0, _observed: dict | None = None) -> tuple[list[str], list[dict]]:
        with self.lock:
            rules = self._rules(role)
            headers, records = [], []
            dates = set()
            for rule in rules:
                ws = self._worksheet(rule["sheet_id"])
                try:
                    values = _observed[rule["sheet_id"]]["values"] if _observed is not None else ws.get_all_values()
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if not _attempt and (status in {400, 404} or type(exc).__name__ == "WorksheetNotFound"):
                        self.refresh(force=True)
                        return self.table(role, _attempt=1)
                    raise
                if len(rules) > 1 and rule.get("date_source"):
                    source_date = self._date_source(rule, values, ws.title)
                    if source_date in dates:
                        raise MappingError("Несколько областей одной роли для одной даты")
                    dates.add(source_date)
                current = trim_headers(values[rule["header_row"] - 1]) if len(values) >= rule["header_row"] else []
                if current != rule["structure"]["headers"] or ws.title != rule["sheet_title"]:
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
                headers.extend(f for f in fields if f not in headers)
                records.extend(self._extract_records(rule, values, ws.title))
            if role == "services" and any(r.get("pivot_prices") for r in rules):
                if len(rules) != 1:
                    raise MappingError("Для длинного прайса нужна одна область services")
                headers, records = self._pivot_prices(rules[0], records)
            if role == "services":
                classes = [h for h in headers if h not in {"Услуга", "Описание", "Доступна"}]
                if len({h.casefold() for h in classes}) != len(classes):
                    raise MappingError("Повторяющиеся классы автомобилей")
            self._validate_records(role, records)
            self.read_versions[role] = digest((headers, records))
            return headers, records

    def _pivot_prices(self, rule: dict, records: list[dict]) -> tuple[list[str], list[dict]]:
        pivot = rule["pivot_prices"]
        by_service = {}
        classes = []
        for rec in records:
            name = rec.get("Услуга", "")
            cls = rec.get(pivot["class_field"], "")
            price = rec.get(pivot["value_field"], "")
            if not name or not cls or cls.casefold() in {"услуга", "описание", "доступна"}:
                raise MappingError("Длинный прайс: нет услуги/класса или класс совпадает со служебным полем")
            if cls not in classes:
                classes.append(cls)
            item = by_service.setdefault(name.casefold(), {"Услуга": name, "Описание": rec.get("Описание", ""),
                                                          "Доступна": rec.get("Доступна", ""),
                                                          "__row__": rec["__row__"], "__sheet_id__": rec["__sheet_id__"]})
            if cls in item:
                raise MappingError("Длинный прайс: повтор услуги и класса")
            if item["Описание"] != rec.get("Описание", "") or item["Доступна"] != rec.get("Доступна", ""):
                raise MappingError("Длинный прайс: разные описания/доступность одной услуги")
            item[cls] = price
        return ["Услуга", "Описание", *classes, "Доступна"], list(by_service.values())

    def _validate_records(self, role: str, records: list[dict]) -> None:
        from sheets import parse_date, parse_time, to_number
        if role in {"services", "tariffs"}:
            yes_no = {"", "да", "yes", "true", "1", "y", "+", "✓", "✅", "истина", "есть",
                      "нет", "no", "false", "0", "n", "-", "❌", "ложь"}
            for rec in records:
                available = rec.get("Доступна" if role == "services" else "Доступен", "").strip().casefold()
                if available not in yes_no:
                    raise MappingError("Неизвестное значение доступности услуги/тарифа")
                if role == "services":
                    price_fields = [k for k in rec if k not in {"Услуга", "Описание", "Доступна", "__row__", "__sheet_id__"}]
                    if len({k.casefold() for k in price_fields}) != len(price_fields):
                        raise MappingError("Повторяющиеся классы автомобилей")
                    for field in price_fields:
                        value = rec.get(field, "")
                        if value and (not re.fullmatch(r"\d[\d\s]*(?:[.,]\d{1,2})?\s*(?:₽|руб\.?|RUB)?", value, re.I) or
                                      (amount := to_number(value)) is None or not math.isfinite(amount)):
                            raise MappingError("Некорректная цена услуги")
                else:
                    discount = rec.get("Скидка %", "")
                    if discount and (not re.fullmatch(r"(?:\d+(?:[.,]\d+)?)\s*%?", discount) or
                                     not 0 <= (to_number(discount) or 0) <= 100):
                        raise MappingError("Некорректная скидка")
                    items = [i.strip().casefold() for i in re.split(r"[,;\n]", rec.get("Состав", "")) if i.strip()]
                    if len(items) != len(set(items)):
                        raise MappingError("Повторяющиеся услуги в составе тарифа")
            field = "Услуга" if role == "services" else "Тариф"
            names = [rec.get(field, "").casefold() for rec in records]
            if any(not n for n in names) or len(names) != len(set(names)):
                raise MappingError(f"Пустые или повторяющиеся названия {role}")
            return
        if role not in {"slots", "bookings"}:
            return
        ids = set()
        for rec in records:
            if not parse_date(rec.get("Дата", "")) or not parse_time(rec.get("Время", "")):
                raise MappingError(f"Некорректная дата/время {role}, лист {rec['__sheet_id__']}, строка {rec['__row__']}")
            if role == "bookings":
                key = rec.get("ID", "")
                if not key or key in ids or not rec.get("Telegram ID", ""):
                    raise MappingError("Отсутствует или повторяется идентификатор записи/клиента")
                if not re.fullmatch(r"[1-9]\d*", rec["Telegram ID"]):
                    raise MappingError("Некорректный Telegram ID клиента")
                ids.add(key)
            else:
                raw = rec.get("Постов", "")
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
            self._rules(role)
        if any(r.get("grid_columns") for r in self._rules("slots")):
            raise MappingError("Сетка постов доступна только для чтения; создание/отмена записей заблокированы")
        return observed

    def _target(self, day: str | None = None, sheet_id: int | None = None) -> dict:
        rules = self._rules("bookings")
        if sheet_id is not None:
            rules = [r for r in rules if r["sheet_id"] == sheet_id]
        elif len(rules) > 1 or rules[0].get("date_source"):
            rules = [r for r in rules if self._date_source(r, self._worksheet(r["sheet_id"]).get_all_values() if (r.get("date_source") or {}).get("cell") else [],
                                                          r["sheet_title"]) == date_value(day)]
        if len(rules) != 1 or not rules[0]["validation"].get("write"):
            raise MappingError("Запись в выбранную область не разрешена; проверьте mappings")
        return rules[0]

    def _journal(self) -> tuple[Path, dict]:
        path = self.path.with_name(self.path.stem + "-operations.json")
        if path.exists():
            try:
                entries = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(entries, dict) or not all(isinstance(v, dict) and v.get("state") in {"pending", "done"}
                                                           for v in entries.values()):
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
                if role == "bookings" and expected_bookings is not None and digest(current_records) != digest(expected_bookings):
                    raise MappingError("Записи изменились перед созданием; обновите свободное время")
            rule = self._target(data["дата"])
            ws = self._worksheet(rule["sheet_id"])
            # Не повторяем неопределённую операцию после сетевого сбоя/перезапуска.
            key = digest({k: str(data[k]).strip().casefold() for k in ("telegram id", "дата", "время", "автомобиль")})
            journal_path, journal = self._journal()
            if key in journal:
                raise MappingError("Такая операция уже выполнена или ожидает ручной сверки; повтор заблокирован")
            values = ws.get_all_values(value_render_option="FORMULA", date_time_render_option="FORMATTED_STRING")
            if rule.get("date_source") and self._date_source(rule, values, ws.title) != date_value(data["дата"]):
                raise MappingError("Дата области изменилась непосредственно перед записью")
            if expected_bookings is not None:
                target_records = [r for r in expected_bookings if r["__sheet_id__"] == rule["sheet_id"]]
                if digest(self._extract_records(rule, values, ws.title)) != digest(target_records):
                    raise MappingError("Строки изменились непосредственно перед записью")
            raw_header = trim_headers(values[rule["header_row"] - 1]) if len(values) >= rule["header_row"] else []
            if raw_header != rule["structure"]["headers"]:
                raise MappingError("Шапка изменилась непосредственно перед записью")
            used = [r for r in range(rule["first_data_row"], min(len(values), rule.get("end_row") or len(values)) + 1)
                    if any(str(v).strip() for v in values[r - 1])]
            row = max(used, default=rule["first_data_row"] - 1) + 1
            if row > (rule.get("end_row") or ws.row_count):
                raise MappingError("В области записи нет свободных строк; расширьте её вручную")
            if any(str(v).strip() for v in (values[row - 1] if row <= len(values) else [])):
                raise MappingError("Строка назначения уже занята")
            updates = []
            for field, col in rule["columns"].items():
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
                    h, m = map(int, value.split(":"))
                    value = (h * 60 + m) / 1440
                updates.append({"range": cell_name(row, col), "values": [[value]]})
            journal[key] = {"state": "pending", "sheet_id": rule["sheet_id"], "row": row, "at": time.time()}
            atomic_json(journal_path, journal)
            try:
                ws.batch_update(updates, value_input_option="RAW")
            except Exception:
                raise MappingError("Результат записи неизвестен. Проверьте Sheets и локальный журнал перед повтором") from None
            journal[key]["state"] = "done"
            atomic_json(journal_path, journal)

    def cancel(self, record: dict, tg_id: int) -> None:
        with self.lock:
            self._preflight()
            rule = self._target(sheet_id=record["__sheet_id__"])
            _, records = self.table("bookings")
            matches = [r for r in records if r["ID"] == record["ID"]]
            if len(matches) != 1:
                raise MappingError("Идентификатор записи неоднозначен")
            current = matches[0]
            identity = ("ID", "Telegram ID", "Дата", "Время", "Статус", "__row__", "__sheet_id__")
            if any(current.get(k) != record.get(k) for k in identity) or current["Telegram ID"] != str(tg_id):
                raise MappingError("Запись изменилась; отмена заблокирована")
            ws = self._worksheet(rule["sheet_id"])
            # Ещё одна адресная проверка в FORMULA перед изменением одной ячейки.
            row = current["__row__"]
            raw = ws.get(f"A{row}:{cell_name(row, max(rule['columns'].values()))}", value_render_option="FORMULA", date_time_render_option="FORMATTED_STRING")
            candidates = [(row, c) for c, v in enumerate(raw[0] if raw else [], 1) if str(v).startswith("=")]
            if self._typed_formulas(rule["sheet_id"], ws.title, candidates):
                raise MappingError("Формула в строке записи; отмена заблокирована")
            row_rule = {**rule, "first_data_row": 1, "end_row": 1, "date_source": None}
            candidates = self._extract_records(row_rule, raw, ws.title)
            if len(candidates) != 1 or any(candidates[0].get(f) != current.get(f) for f in rule["columns"]):
                raise MappingError("Строка записи перемещена или изменена; отмена заблокирована")
            journal_path, journal = self._journal()
            ws.batch_update([{"range": cell_name(row, rule["columns"]["Статус"]),
                              "values": [[rule["write_statuses"]["cancelled"]]]}], value_input_option="RAW")
            key = digest({k.lower(): str(current.get(k, "")).strip().casefold()
                          for k in ("Telegram ID", "Дата", "Время", "Автомобиль")})
            if journal.get(key, {}).get("state") == "done":
                del journal[key]
                atomic_json(journal_path, journal)
