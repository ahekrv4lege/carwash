"""Детерминированные правила извлечения Sheets. Содержимое записей не сохраняется."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from functools import lru_cache
import hashlib
import json
import logging
import math
import os
import tempfile
from pathlib import Path
import re
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

VERSION = 1
MAX_DISCOVERY_CELLS = 1_000_000
ENGINE_REVISION = 3
KNOWN_PRICE_CLASSES = {"легковой", "кроссовер", "внедорожник", "минивэн", "внедорожник минивэн", "микроавтобус",
                       "седан", "хэтчбек", "универсал", "sedan", "suv", "crossover", "van", "minivan", "passenger car"}
NON_PRICE_HEADERS = {"id", "duration", "duration minutes", "duration hours", "длительность", "длительность мин",
                     "длительность работы", "время мин", "время работы", "минуты", "часы", "номер", "код"}
BOOKING_FIELDS = ["ID", "Создано", "Дата", "Время", "Имя", "Телефон", "Автомобиль", "Класс",
                  "Тариф", "Доп. услуги", "Состав", "Сумма", "Комментарий", "Telegram ID", "Username", "Статус"]
# Дополнительные поля записей: колонка может отсутствовать в старых таблицах,
# поэтому не обязана для записи и не участвует в канонической раскладке.
EXTRA_ROLE_FIELDS = {"bookings": ["Госномер", "Бокс"]}
ALIASES = {
    "Бокс": ["бокс", "пост", "ресурс", "bay", "resource"],
    "ID": ["id", "номер записи", "booking id", "номер брони"],
    "Создано": ["создано", "created", "created at", "дата создания"],
    "Дата": ["дата", "date", "день", "дата записи", "booking date"],
    "Время": ["время", "time", "время записи", "start time"],
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
    "Госномер": ["госномер", "гос номер", "госномер автомобиля", "номер авто", "номер машины",
                 "plate", "license plate", "car plate"],
    "Постов": ["постов", "мест", "боксов", "capacity", "bays", "пост", "бокс"],
    "Услуга": ["услуга", "service", "service name", "название услуги"],
    "Описание": ["описание", "description", "details"],
    "Доступна": ["доступна", "available", "active", "enabled", "активна", "выполняется", "выполняем"],
    "Доступен": ["доступен", "available", "active", "enabled", "активен", "доступна", "выполняется"],
    "Скидка %": ["скидка", "скидка %", "скидка процентов", "discount", "discount %", "discount percent"],
    "Длительность": ["длительность", "длительность мин", "длительность минуты", "длительность работы",
                     "время мин", "время работы", "время выполнения", "минуты", "мин",
                     "часы", "час", "duration", "duration minutes", "duration hours"],
    "Начало": ["начало", "start", "открытие", "старт"],
    "Конец": ["конец", "окончание", "закрытие", "end", "финиш"],
}
ROLE_FIELDS = {
    "bookings": BOOKING_FIELDS,
    "slots": ["Дата", "Время", "Постов"],
    "services": ["Услуга", "Описание", "Доступна"],
    "tariffs": ["Тариф", "Состав", "Скидка %", "Доступен"],
}
# Опциональные роли: их отсутствие не делает mappings неполными и не блокирует записи.
OPTIONAL_ROLE_FIELDS = {"durations": ["Услуга", "Длительность"]}
ALL_ROLE_FIELDS = {**ROLE_FIELDS, **OPTIONAL_ROLE_FIELDS}
READ_REQUIRED = {
    "bookings": {"ID", "Дата", "Время", "Telegram ID", "Статус"},
    "slots": {"Дата", "Время", "Постов"},
    "services": {"Услуга"},
    "tariffs": {"Тариф", "Состав"},
    "durations": {"Услуга", "Длительность"},
}
SCHEDULE_REQUIRED = {"Начало", "Конец"}
STATUS_MAP = {"новая": "Новая", "new": "Новая", "confirmed": "Подтверждена",
              "подтверждена": "Подтверждена", "cancelled": "Отмена", "canceled": "Отмена",
              "отменена": "Отмена", "отмена": "Отмена", "выполнена": "Выполнена", "done": "Выполнена"}


class MappingError(RuntimeError):
    pass


class SlotUnavailable(MappingError):
    """Время или пост занят, запись не создана. Текст безопасен для показа клиенту."""


DURATION_HEADER_RE = re.compile(r"длительность|duration|время (?:работы|выполнения)|^мин(?:ут\w*)?$|^час(?:ов|ы)?$")
DURATION_MAX_MINUTES = 12 * 60


def is_duration_header(value: Any) -> bool:
    text = normalize_header(value)
    return bool(text) and bool(DURATION_HEADER_RE.search(text))


def duration_unit_hint(value: Any) -> str | None:
    """Единица из названия колонки: «Длительность, ч» -> hours, «Длительность, мин» -> minutes."""
    text = normalize_header(value)
    if re.search(r"час|hour", text):
        return "hours"
    if re.search(r"мин|minute", text):
        return "minutes"
    return None


DURATION_WORDS = {"полчаса": 30, "час": 60, "часа": 60, "часов": 60, "часик": 60,
                  "полтора": 90, "два": 120, "две": 120, "три": 180, "четыре": 240, "пять": 300}


def _word_duration(text: str) -> int | None:
    """«полчаса», «полтора часа», «два часа» — когда смысл однозначен."""
    match = re.fullmatch(r"(\w+)(\s+час\w*)?", text)
    if not match:
        return None
    word = match.group(1)
    if word == "полтора":
        return 90 if match.group(2) else None  # «полтора» без часа не про длительность
    return DURATION_WORDS.get(word)


def _duration_total(minutes: float) -> int | None:
    result = int(round(minutes))
    return result if 0 < result <= DURATION_MAX_MINUTES else None


def parse_duration_minutes(value: Any, unit_hint: str | None = None) -> int | None:
    """Длительность в целых минутах из достоверных табличных форматов, иначе None.

    Понимаются: «90», «90 мин», «1,5 ч», «2 часа», «1 ч 30 мин», «1:30» и диапазон
    «10:00-11:30». Голое число понимается как минуты, дробное — как часы, если
    колонка сама не подсказывает единицу. Длительность не выводится из других данных.
    """
    text = str(value or "").strip().lower().replace("ё", "е").replace(",", ".")
    if not text:
        return None
    word = _word_duration(text)
    if word:
        return word
    span = re.fullmatch(r"(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})", text)
    if span:
        start, end = int(span[1]) * 60 + int(span[2]), int(span[3]) * 60 + int(span[4])
        return end - start if 0 <= start < end <= 24 * 60 else None
    combined = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:час\w*|ч|hours?|h)\s*(\d+(?:\.\d+)?)?\s*(?:минут\w*|мин|minutes?|m)?", text)
    if combined and re.search(r"час|hour|\bч\b|\bh\b", text):
        total = float(combined[1]) * 60 + (float(combined[2]) if combined[2] else 0.0)
        return _duration_total(total)
    single = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(час\w*|ч|hours?|h|минут\w*|мин|minutes?|m)", text)
    if single:
        hours = re.match(r"час|h|hour", single[2]) is not None
        return _duration_total(float(single[1]) * (60 if hours else 1))
    clock = re.fullmatch(r"(\d{1,2})[:.](\d{2})", text)
    if clock:
        return _duration_total(int(clock[1]) * 60 + int(clock[2]))
    number = re.fullmatch(r"(\d+(?:\.\d+)?)", text)
    if number:
        amount = float(number[1])
        if unit_hint == "hours":
            return _duration_total(amount * 60)
        if unit_hint == "minutes" or amount.is_integer():
            return _duration_total(amount)
        return _duration_total(amount * 60)
    return None


@lru_cache(maxsize=4096)
def _normalize_header_cached(value: str) -> str:
    return re.sub(r"[^\w%]+", " ", value.strip().lower().replace("ё", "е")).strip()


def normalize_header(value: Any) -> str:
    return _normalize_header_cached(str(value))


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


def file_stamp(path: Path) -> tuple | None:
    """Stamp for cache invalidation. Atomic replace always changes the inode, so this
    detects rewrites even when a coarse filesystem clock gives the same mtime."""
    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    return (status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


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
    # Дополнительные поля (например «Госномер») мапятся, только когда колонка есть в листе.
    for field in (*ALL_ROLE_FIELDS[role], *EXTRA_ROLE_FIELDS.get(role, ())):
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


def detect_duration_column(snapshot: dict, row_no: int, headers: list, used: set[int]) -> tuple[str, int] | None:
    """Колонка длительности: название про длительность, все значения разбираются.

    Возвращает (название шапки, номер колонки) или None. Служебные и ценовые
    колонки не рассматриваются; неоднозначные значения колонку не открывают.
    """
    hint = None
    for column, header in enumerate(headers, 1):
        if not header or column in used or not is_duration_header(header):
            continue
        hint = duration_unit_hint(header)
        samples = [str(value_at(snapshot["values"], row_index, column)).strip()
                   for row_index in range(row_no + 1, min(len(snapshot["values"]), row_no + 20) + 1)]
        samples = [sample for sample in samples if sample]
        if samples and all(parse_duration_minutes(sample, hint) for sample in samples):
            return str(header).strip(), column
    return None


def discover(snapshot: dict, expected: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Смотрим только заголовки и типы цен; значения клиентов остаются в памяти."""
    rules = []
    issues = []
    for row_no, headers in enumerate(snapshot["values"][:20], 1):
        for role in ALL_ROLE_FIELDS:
            columns, errors = header_mapping(headers, role)
            if role == "durations":
                if any(rule["header_row"] == row_no and "Длительность" in rule.get("columns", {})
                       for rule in rules if rule["role"] in {"services", "tariffs"}):
                    continue  # длительность уже разобрана колонкой таблицы услуг/тарифов
                duration_column = columns.get("Длительность")
                if not duration_column:
                    continue
                samples = [str(value_at(snapshot["values"], sample_row, duration_column)).strip()
                           for sample_row in range(row_no + 1, min(len(snapshot["values"]), row_no + 20) + 1)]
                if not any(parse_duration_minutes(sample, duration_unit_hint(headers[duration_column - 1]))
                           for sample in samples if sample):
                    continue  # колонка без значений — не таблица длительностей
            source = {"title": True} if role in {"slots", "bookings"} and "Дата" not in columns and title_date(snapshot["title"]) else None
            present = set(columns) | ({"Дата"} if source else set())
            tariff_prices = []
            if (role == "tariffs" and "Тариф" in columns
                    and "Telegram ID" not in header_mapping(headers, "bookings")[0]
                    and "Услуга" not in header_mapping(headers, "services")[0]):
                # Пакеты с фиксированной ценой: числовые колонки — классы вместо состава.
                used_tariff = set(columns.values())
                for column, header in enumerate(headers, 1):
                    if not header or column in used_tariff or is_duration_header(header):
                        continue
                    if re.fullmatch(r"-?\d[\d\s]*(?:[.,]\d+)?\s*(?:₽|руб\.?|RUB)?", str(header).strip(), re.I):
                        continue  # числовая шапка — не название класса
                    samples = [str(value_at(snapshot["values"], row_index, column)).strip()
                               for row_index in range(row_no + 1, min(len(snapshot["values"]), row_no + 20) + 1)]
                    samples = [value for value in samples if value]
                    if samples and all(re.fullmatch(r"-?\d[\d\s]*(?:[.,]\d+)?\s*(?:₽|руб\.?|RUB)?", value, re.I)
                                       for value in samples):
                        tariff_prices.append((str(header).strip(), column))
            if errors and (snapshot["title"] == expected.get(role) or len(present & READ_REQUIRED[role]) >= len(READ_REQUIRED[role]) - 1):
                issues.extend(f"Лист {snapshot['id']}, строка {row_no}: {issue}" for issue in errors)
            if not READ_REQUIRED[role] <= present and not (
                    role == "tariffs" and "Тариф" in columns and tariff_prices):
                continue
            # Слотоподобная строка записей не является отдельной таблицей слотов.
            if role == "slots" and ("Telegram ID" in header_mapping(headers, "bookings")[0]):
                continue
            if role == "slots" and any(
                normalize_header(header) in {normalize_header(alias) for alias in ALIASES["Конец"]}
                for header in headers if header
            ):
                continue  # лист с «Конец» — это график работы, а не таблица слотов
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
            rule_price_fields = []
            if role == "services":
                booking_columns, _ = header_mapping(headers, "bookings")
                if "Класс" in booking_columns and "Сумма" in booking_columns:
                    columns.update({field: booking_columns[field] for field in ("Класс", "Сумма")})
                    pivot = True
                used = set(columns.values())
                duration = detect_duration_column(snapshot, row_no, headers, used)
                if duration:
                    columns["Длительность"] = duration[1]
                    used.add(duration[1])
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
                    if not duration:
                        issues.append(f"Лист {snapshot['id']}: классы/колонки цен требуют ручной настройки")
                    continue
            if role == "bookings":
                # «Длительность» в записях нужна для занятости по интервалам; колонка
                # опциональна и распознаётся по названию даже на пустом листе.
                used_bookings = set(columns.values())
                for column, header in enumerate(headers, 1):
                    if header and column not in used_bookings and is_duration_header(header):
                        columns["Длительность"] = column
                        break
            rule = {"role": role, "sheet_id": snapshot["id"], "sheet_title": snapshot["title"],
                    "header_row": row_no, "first_data_row": row_no + 1, "end_row": None,
                    "columns": columns, "transforms": {"Дата": "date", "Время": "time"},
                    "status_map": dict(STATUS_MAP), "write_statuses": {"new": "Новая", "cancelled": "Отмена"},
                    "date_source": source, "permissions": {"read": True, "write": role == "bookings"},
                    "validation": {"read": False, "write": False, "issues": []}}
            if role == "services":
                rule["price_fields"] = prices
                if not pivot:
                    rule["unresolved_fields"] = [field for field in prices if normalize_header(field) not in KNOWN_PRICE_CLASSES]
                if pivot:
                    rule["pivot_prices"] = {"class_field": "Класс", "value_field": "Сумма"}
            if role == "tariffs":
                used = set(columns.values())
                used.update(column for _, column in tariff_prices)
                duration = detect_duration_column(snapshot, row_no, headers, used)
                if duration:
                    columns["Длительность"] = duration[1]
                    used.add(duration[1])
                for header, column in tariff_prices:
                    columns[header] = column
                if tariff_prices:
                    rule_price_fields = [header for header, _ in tariff_prices]
            if role == "tariffs" and rule_price_fields:
                rule["price_fields"] = rule_price_fields
            rule["structure"] = structure(snapshot, rule)
            rule["fingerprint"] = fingerprint(rule["structure"])
            rules.append(rule)
    if not rules:
        schedule = discover_schedule(snapshot)
        if schedule:
            rules.append(schedule)
        else:
            grid = discover_grid(snapshot)
            if grid:
                rules.append(grid)
            else:
                expected_role = next((role for role, title in expected.items() if title == snapshot["title"]), None)
                if expected_role:
                    issues.append(f"Лист {snapshot['id']}: ожидаемая область {expected_role} не распознана; "
                                  "нужна ручная настройка или интерпретация моделью")
                else:
                    issues.append(f"Лист {snapshot['id']}: структура не распознана; нужна ручная настройка")
    # Более одной таблицы одного назначения на листе не выбираем молча.
    for role in ALL_ROLE_FIELDS:
        candidates = [row_index for row_index in rules if row_index["role"] == role]
        if len(candidates) > 1:
            rules = [row_index for row_index in rules if row_index["role"] != role]
            issues.append(f"Лист {snapshot['id']}: несколько областей {role}; задайте границы вручную")
    return rules, issues


def discover_schedule(snapshot: dict) -> dict | None:
    """Лист графика работы: Начало + Конец, опционально Дата и Постов.

    Строки без «Дата» задают ежедневный график; строка с датой переопределяет его
    на эту дату. «Постов» — число параллельных ресурсов в этот интервал.
    """
    for row_no, headers in enumerate(snapshot["values"][:20], 1):
        columns, errors = {}, []
        for field in ("Начало", "Конец", "Дата", "Постов"):
            aliases = {normalize_header(alias) for alias in ALIASES[field]}
            hits = [index for index, header in enumerate(headers, 1) if normalize_header(header) in aliases]
            if len(hits) == 1:
                columns[field] = hits[0]
            elif len(hits) > 1:
                errors.append(f"Несколько колонок для {field}")
        if errors or not SCHEDULE_REQUIRED <= set(columns):
            continue
        if "Время" in columns or "Telegram ID" in header_mapping(headers, "bookings")[0]:
            continue
        source = None
        if "Дата" not in columns and title_date(snapshot["title"]):
            source = {"title": True}
        rule = {"role": "slots", "sheet_id": snapshot["id"], "sheet_title": snapshot["title"],
                "header_row": row_no, "first_data_row": row_no + 1, "end_row": None,
                "columns": columns, "schedule_mode": "hours",
                "transforms": {"Дата": "date", "Начало": "time", "Конец": "time"},
                "status_map": dict(STATUS_MAP), "write_statuses": {"new": "Новая", "cancelled": "Отмена"},
                "date_source": source, "permissions": {"read": True, "write": False},
                "validation": {"read": False, "write": False, "issues": []}}
        rule["structure"] = structure(snapshot, rule)
        rule["fingerprint"] = fingerprint(rule["structure"])
        return rule
    return None


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
                "permissions": {"read": True, "write": True},
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
        self.stamp = None
        self.force_pending = config.sheets_force_schema_refresh
        self.worksheets = {}
        self.read_versions = {}
        self.ai_requests = 0
        self._ai_cache_key = None
        self.expected = {role: getattr(config, f"sheet_{role}") for role in ROLE_FIELDS}

    def _load(self) -> None:
        stamp = file_stamp(self.path)
        if self.document is not None and stamp == self.stamp:
            return
        if stamp is None:
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
            if not isinstance(rule, dict) or rule.get("role") not in ALL_ROLE_FIELDS:
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
        self.stamp = stamp

    def _save(self) -> None:
        atomic_json(self.path, self.document)
        self.stamp = file_stamp(self.path)

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
        if rule.get("role") not in ALL_ROLE_FIELDS or not isinstance(columns, dict):
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
        allowed = set(ROLE_FIELDS.get(rule["role"], OPTIONAL_ROLE_FIELDS.get(rule["role"], set())))
        # Дополнительные поля (например «Госномер») разрешены, когда колонка есть в листе.
        allowed |= set(EXTRA_ROLE_FIELDS.get(rule["role"], ()))
        allowed = allowed | set(rule.get("price_fields", []))
        if rule.get("pivot_prices"):
            allowed.update({"Класс", "Сумма"})
        if rule.get("prices_transposed"):
            allowed.add("Класс")
            allowed |= set(rule["columns"]) - {"Класс"}
        if rule.get("schedule_mode") == "hours":
            allowed.update({"Начало", "Конец"})
        if "Длительность" in columns:
            allowed.add("Длительность")
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
        time_transforms = {"time", "serial_time"}
        valid = {"Дата": {"date", "serial_date"}, "Время": time_transforms,
                 "Начало": time_transforms, "Конец": time_transforms}
        for field, transform in rule.get("transforms", {}).items():
            if (
                field not in valid
                or not isinstance(transform, str)
                or not (
                    transform in valid[field]
                    or (field == "Дата" and transform.startswith("date:") and len(transform) > 5)
                )
            ):
                errors.append("Некорректное преобразование даты/времени")
        required = SCHEDULE_REQUIRED if rule.get("schedule_mode") == "hours" else READ_REQUIRED[rule["role"]]
        if rule.get("prices_transposed"):
            required = {"Класс"}  # классы строками, услуги — колонки-ключи
        if rule["role"] == "tariffs":
            required = required - {"Состав"} if rule.get("price_fields") else required
        missing = required - present
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
        if rule["role"] == "slots" and rule.get("grid_columns"):
            # Сетка постов: клетка занимается маркером записи. Формулы в теле сетки блокируют запись.
            if any(row_index >= first and (end_row is None or row_index <= end_row) and column_index in set(rule["grid_columns"]) | {columns.get("Время")}
                   for row_index, column_index in snapshot["formulas"]):
                write_errors.append("Формулы в сетке постов: запись заблокирована")
        elif rule["role"] != "bookings":
            write_errors.append("Область доступна только для чтения")
        if rule["role"] == "bookings" and set(BOOKING_FIELDS) - present:
            write_errors.append("Для создания записей нужны все поля записи")
        if rule["role"] == "bookings" and any(
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
            or status_map.get(normalize_header(statuses.get("cancelled", ""))) != "Отмена"
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
        for extra_field in ("Начало", "Конец", "Длительность"):
            if extra_field in rule.get("columns", {}) and extra_field not in aliases:
                extra_aliases = {normalize_header(alias) for alias in ALIASES[extra_field]}
                hits = [index for index, header in enumerate(current_headers, 1) if normalize_header(header) in extra_aliases]
                if len(hits) == 1:
                    aliases[extra_field] = hits[0]
                elif len(hits) > 1:
                    ambiguous.append(f"Несколько колонок для {extra_field}")
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
                self._maybe_interpret_with_ai(regions, by_id, issues)
                self.document = {"version": VERSION, "spreadsheet_id": self.config.spreadsheet_id,
                                 "regions": regions, "relationships": [
                                     {"from": "bookings", "to": "slots", "fields": ["Дата", "Время"]},
                                     {"from": "tariffs", "to": "services", "fields": ["Состав", "Услуга"]}],
                                 "issues": issues}
                if self._ai_cache_key:
                    self.document["ai_regions"] = self.document.get("ai_regions", [])
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
                if regions != self.document["regions"] or issues != self.document.get("issues"):
                    self._maybe_interpret_with_ai(regions, by_id, issues)
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

    # ---- интерпретация структуры моделью: кандидаты сверяются с фактической таблицей

    def _ai_enabled(self) -> bool:
        return bool(getattr(self.config, "sheets_ai_mapping_enabled", True)
                    and getattr(self.config, "ai_api_key", ""))

    def _ai_candidates(self, snapshots: list[dict], issues: list[str]) -> list[dict]:
        """Один вызов модели на новое структурное представление книги."""
        import schema_ai
        compact = schema_ai.compact_workbook(snapshots)
        key = fingerprint(compact)
        if self._ai_cache_key == key and self.document and self.document.get("ai_regions") is not None:
            return self.document["ai_regions"]
        began = time.time()
        document = schema_ai.request_mapping(self.config, compact)
        self.ai_requests += 1
        candidates, model_issues = schema_ai.normalize_ai_document(document)
        issues.extend(model_issues)
        issues.append(f"Модель: интерпретация за {time.time() - began:.1f}с")
        self._ai_cache_key = key
        return candidates

    def _ai_rule(self, candidate: dict, snapshot: dict, issues: list[str]) -> dict | None:
        """Кандидат модели -> правило с проверенными номерами колонок."""
        title = candidate["sheet_title"]
        if title != snapshot["title"]:
            return None  # ищем только на своём листе; чужие названия — молча
        headers_all = snapshot["values"]
        header_row = candidate["header_row"] or 1
        if not 1 <= header_row <= min(20, len(headers_all)):
            issues.append(f"Модель: некорректная строка шапки {header_row} на листе «{title}»")
            return None
        headers = trim_headers(headers_all[header_row - 1])
        columns: dict[str, int] = {}
        canonical_keys = set(ALL_ROLE_FIELDS.get(role := candidate["role"], ())) | {"Класс", "Сумма", "Длительность"}
        for key, value in candidate["columns"].items():
            # Модель иногда меняет направление пары; нормализуем к «поле -> шапка».
            if key in canonical_keys or key.casefold() in KNOWN_PRICE_CLASSES:
                field, claimed = key, value
            elif value in canonical_keys or value.casefold() in KNOWN_PRICE_CLASSES:
                field, claimed = value, key
            else:
                field, claimed = key, value
            exact = [index for index, header in enumerate(headers, 1) if header == claimed]
            loose = [index for index, header in enumerate(headers, 1)
                     if normalize_header(header) == normalize_header(claimed)]
            hits = exact or loose
            if len(hits) == 1:
                columns[field] = hits[0]
            else:
                issues.append(f"Модель: шапка «{claimed}» ({field}) не подтверждена на листе «{title}»")
        role = candidate["role"]
        if not columns:
            return None
        if role in {"services", "tariffs"} and not candidate.get("orientation"):
            # Класс заявлен, но в листе есть ещё колонка с той же шапкой: неоднозначно.
            for field, claimed in list(candidate["columns"].items()):
                if field in ALL_ROLE_FIELDS[role] or field in {"Класс", "Сумма", "Длительность"}:
                    continue
                duplicates = [header for header in headers
                              if normalize_header(header) == normalize_header(claimed)]
                if len(duplicates) > 1:
                    issues.append(f"Модель: класс «{field}» неоднозначен на листе «{title}»: "
                                  f"несколько колонок {duplicates}")
                    return None
        if candidate.get("duration_column"):
            duration = detect_duration_column(snapshot, header_row, headers, set(columns.values()))
            if duration and "Длительность" not in columns:
                columns["Длительность"] = duration[1]
        price_fields = []
        if role in {"services", "tariffs"}:
            price_fields = [field for field in columns
                            if field not in ALL_ROLE_FIELDS[role] and field not in {"Класс", "Сумма", "Длительность"}]
        grid_columns = []
        for claimed in candidate.get("grid_columns", []):
            hits = [index for index, header in enumerate(headers, 1) if header == claimed]
            if len(hits) == 1:
                grid_columns.append(hits[0])
            else:
                issues.append(f"Модель: колонка сетки «{claimed}» не подтверждена на листе «{title}»")
        source = candidate.get("date_source")
        date_source = {"title": True} if source == "title" else (
            {"cell": source["cell"]} if isinstance(source, dict) and isinstance(source.get("cell"), list) else None)
        if role in {"slots", "bookings"} and "Дата" not in columns and date_source is None                 and title_date(snapshot["title"]):
            date_source = {"title": True}
        # Транспонировка объявлена, но ключами оказались классы — это обычная матрица.
        transposed = candidate.get("orientation") == "services_in_columns" and not any(
            key.casefold() in KNOWN_PRICE_CLASSES
            for key in candidate["columns"] if key not in ALL_ROLE_FIELDS.get(role, ()))
        rule = {"role": role, "sheet_id": snapshot["id"], "sheet_title": snapshot["title"],
                "header_row": header_row, "first_data_row": header_row + 1, "end_row": None,
                "columns": columns, "source": "ai",
                **({"prices_transposed": True} if transposed else {}),
                "transforms": {"Дата": "date", "Время": "time", "Начало": "time", "Конец": "time"},
                "status_map": dict(STATUS_MAP), "write_statuses": {"new": "Новая", "cancelled": "Отмена"},
                "date_source": date_source, "permissions": {"read": True, "write": role == "bookings"},
                "validation": {"read": False, "write": False, "issues": []}}
        if role in {"services", "tariffs"} and price_fields:
            rule["price_fields"] = price_fields
        if role == "services" and "Класс" in columns and "Сумма" in columns:
            rule["pivot_prices"] = {"class_field": "Класс", "value_field": "Сумма"}
        if grid_columns:
            rule["grid_columns"] = grid_columns
            rule["free_markers"] = ["", "свободно", "free", "open"]
        if candidate.get("schedule_mode") == "hours" and {"Начало", "Конец"} <= set(columns):
            rule["schedule_mode"] = "hours"
        rule["structure"] = structure(snapshot, rule)
        rule["fingerprint"] = fingerprint(rule["structure"])
        return rule

    def _apply_ai_candidates(self, candidates: list[dict], by_id: dict[int, dict],
                             regions: list[dict], issues: list[str]) -> None:
        covered = {(rule["role"], rule["sheet_id"], rule["header_row"]) for rule in regions}
        for candidate in candidates:
            snapshot = next((item for item in by_id.values()
                             if item["title"] == candidate["sheet_title"]), None)
            if snapshot is None:
                issues.append(f"Модель: лист «{candidate['sheet_title']}» не найден в книге")
                continue
            key = (candidate["role"], snapshot["id"], candidate["header_row"] or 1)
            if key in covered:
                continue  # детерминированное распознавание уже нашло эту область
            rule = self._ai_rule(candidate, snapshot, issues)
            if rule is None:
                continue
            self._validate_rule(rule, snapshot)
            regions.append(rule)
            covered.add(key)

    def _maybe_interpret_with_ai(self, regions: list[dict], by_id: dict[int, dict],
                                 issues: list[str]) -> None:
        """Модель привлекается, когда детерминированного распознавания недостаточно."""
        if not self._ai_enabled():
            return
        required_roles = {"bookings", "services", "tariffs", "slots"}
        missing = required_roles - {rule["role"] for rule in regions}
        unrecognized = any("не распознана" in issue or "требуют ручной настройки" in issue
                           for issue in issues)
        if not missing and not unrecognized:
            return
        try:
            candidates = self._ai_candidates(list(by_id.values()), issues)
        except Exception as error:
            issues.append(f"Интерпретация модели недоступна ({type(error).__name__}); "
                          "работает детерминированное распознавание")
            log.warning("AI-интерпретация структуры не выполнена (%s)", type(error).__name__)
            return
        # До трёх адресных подсказок: модель иногда пропускает обязательную роль.
        sheet_titles = ", ".join(repr(snapshot["title"]) for snapshot in by_id.values())
        for _attempt in range(3):
            still_missing = required_roles - {rule["role"] for rule in regions}
            if not still_missing:
                break
            try:
                import schema_ai
                compact = schema_ai.compact_workbook(list(by_id.values()))
                examples = {
                    "services": '{"role": "services", "sheet_title": "...", "header_row": 1, '
                                '"columns": {"Услуга": "<шапка>", "Легковой": "<шапка>"}}',
                    "tariffs": '{"role": "tariffs", "sheet_title": "...", "header_row": 1, '
                               '"columns": {"Тариф": "<шапка>", "Состав": "<шапка>", "Скидка %": "<шапка>"}}',
                    "slots": '{"role": "slots", "sheet_title": "...", "header_row": 1, '
                             '"columns": {"Дата": "<шапка>", "Время": "<шапка>", "Постов": "<шапка>"}}',
                    "bookings": '{"role": "bookings", "sheet_title": "...", "header_row": 1, '
                                '"columns": {"ID": "<шапка>", "Дата": "<шапка>", "Время": "<шапка>", '
                                '"Telegram ID": "<шапка>", "Статус": "<шапка>"}}',
                }
                reminder = ("В книге листы: " + sheet_titles + ". Ты не указал области: "
                                + ", ".join(sorted(still_missing))
                                + ". Проверь ещё раз все листы; если данные позволяют — укажи их. Пример формы: "
                                + "; ".join(examples[role] for role in sorted(still_missing)))
                retry = schema_ai.request_mapping_with_hint(self.config, compact, reminder)
                retry_candidates, retry_issues = schema_ai.normalize_ai_document(retry)
                issues.extend(retry_issues)
                self._apply_ai_candidates(retry_candidates, by_id, regions, issues)
            except Exception as error:
                issues.append(f"Повторная интерпретация модели недоступна ({type(error).__name__})")
                break

    def _summarize(self) -> None:
        self.document["missing_roles"] = sorted(set(ROLE_FIELDS) - {rule["role"] for rule in self.document["regions"]})
        ambiguous_roles = [role for role in ROLE_FIELDS
                       if len(group := [rule for rule in self.document["regions"] if rule["role"] == role]) > 1
                       and role != "services" and not all(rule.get("date_source") for rule in group)]
        self.document["ambiguous_roles"] = ambiguous_roles
        self.document["validation_status"] = "valid" if not self.document.get("missing_roles") and not ambiguous_roles and all(
            rule["validation"]["read"] for rule in self.document["regions"] if rule["role"] in ROLE_FIELDS) else "needs_review"
        self.document["writes_enabled"] = (self.document["validation_status"] == "valid" and
            all(rule["validation"].get("write") for rule in self.document["regions"] if rule["role"] == "bookings"))

    def _rules(self, role: str) -> list[dict]:
        self.refresh()
        rules = [rule for rule in self.document["regions"] if rule["role"] == role]
        if not rules:
            raise MappingError(f"Нет подтверждённой области {role}; настройте mappings")
        # Несколько обычных таблиц одной роли без явных дат/границ неоднозначны.
        if len(rules) > 1 and role != "services" and not all(rule.get("date_source") for rule in rules):
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

    def _extract_transposed_prices(self, rule: dict, values: list[list], title: str) -> list[dict]:
        """Классы строками, услуги колонками -> длинный формат «услуга, класс, цена»."""
        records = []
        end_row = min(len(values), rule.get("end_row") or len(values))
        class_column = rule["columns"]["Класс"]
        service_columns = [field for field in rule["columns"] if field != "Класс"]
        sheet_id = rule["sheet_id"]
        for row in range(rule["first_data_row"], end_row + 1):
            class_name = str(value_at(values, row, class_column)).strip()
            if not class_name:
                continue
            for service in service_columns:
                price = str(value_at(values, row, rule["columns"][service])).strip()
                if price:
                    records.append({"Услуга": service, class_name: price,
                                    "__row__": row, "__sheet_id__": sheet_id})
        return records

    def _extract_records(self, rule: dict, values: list[list], title: str) -> list[dict]:
        if rule.get("prices_transposed"):
            return self._extract_transposed_prices(rule, values, title)
        records = []
        end_row = min(len(values), rule.get("end_row") or len(values))
        # Инварианты цикла: не пересобираем их для каждой строки.
        columns_items = list(rule["columns"].items())
        transforms = rule.get("transforms", {})
        date_transform = transforms.get("Дата", "date")
        time_transform = transforms.get("Время", "time")
        has_date_source = bool(rule.get("date_source"))
        free = ({str(field_value).strip().casefold() for field_value in rule["free_markers"]}
                if rule.get("grid_columns") else None)
        statuses = ({normalize_header(field_name): field_value for field_name, field_value in rule.get("status_map", {}).items()}
                    if "Статус" in rule["columns"] else None)
        sheet_id = rule["sheet_id"]
        for row in range(rule["first_data_row"], end_row + 1):
            record = {field: str(value_at(values, row, column_index)).strip() for field, column_index in columns_items}
            if not any(record.values()):
                continue
            if has_date_source:
                record["Дата"] = self._date_source(rule, values, title)
            elif "Дата" in record and str(record["Дата"]).strip():
                record["Дата"] = date_value(record["Дата"], date_transform)
            if free is not None:
                record["Постов"] = str(sum(str(value_at(values, row, column_index)).strip().casefold() in free
                                          for column_index in rule["grid_columns"]))
                record["__free_capacity__"] = True
            if "Время" in record and str(record["Время"]).strip():
                record["Время"] = time_value(record["Время"], time_transform)
            for field in ("Начало", "Конец"):
                if field in record and str(record[field]).strip():
                    record[field] = time_value(record[field], transforms.get(field, "time"))
            if statuses is not None:
                record["Статус"] = statuses.get(normalize_header(record["Статус"]), record["Статус"])
            record["__row__"], record["__sheet_id__"] = row, sheet_id
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
                if rule.get("prices_transposed"):
                    # Классы строками: ключи-услуги не становятся классами, а записями «Услуга».
                    records.extend(self._extract_records(rule, values, worksheet.title))
                    continue
                fields = list(rule["columns"])
                if rule.get("date_source") and "Дата" not in fields:
                    fields.append("Дата")
                if rule.get("grid_columns"):
                    fields.append("Постов")
                headers.extend(field for field in fields if field not in headers)
                records.extend(self._extract_records(rule, values, worksheet.title))
            if role == "services" and any(record.get("pivot_prices") for record in rules):
                if not all(rule.get("pivot_prices") == rules[0].get("pivot_prices") for rule in rules):
                    raise MappingError("Распределённый прайс смешивает несовместимые представления")
                headers, records = self._pivot_prices(rules[0], records)
            if role == "services" and any(rule.get("prices_transposed") for rule in rules):
                # Из длинного формата собираем привычную матрицу «услуга × класс».
                classes: list[str] = []
                by_service: dict[str, dict] = {}
                for record in records:
                    service = record.get("Услуга", "")
                    item = by_service.setdefault(service, {"Услуга": service})
                    for key, value in record.items():
                        if key in {"Услуга", "__row__", "__sheet_id__"} or key in item:
                            continue
                        item[key] = value
                        if key not in classes:
                            classes.append(key)
                headers = ["Услуга", *classes]
                records = list(by_service.values())
            if role == "services":
                classes = [header for header in headers if header not in {"Услуга", "Описание", "Доступна", "Длительность"}]
                if len({normalize_header(header) for header in classes}) != len(classes):
                    raise MappingError("Повторяющиеся классы автомобилей")
            self._validate_records(role, records, rules)
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

    def _validate_records(self, role: str, records: list[dict], rules: list[dict] | None = None) -> None:
        from sheets import parse_date, parse_time, to_number
        if role in {"services", "tariffs"}:
            yes_no = {"", "да", "yes", "true", "1", "y", "+", "✓", "✅", "истина", "есть",
                      "нет", "no", "false", "0", "n", "-", "❌", "ложь"}
            for record in records:
                available = record.get("Доступна" if role == "services" else "Доступен", "").strip().casefold()
                if available not in yes_no:
                    raise MappingError("Неизвестное значение доступности услуги/тарифа")
                if role == "services":
                    price_fields = [field_name for field_name in record if field_name not in
                                    {"Услуга", "Описание", "Доступна", "Длительность", "__row__", "__sheet_id__"}]
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
                    price_fields = sorted({field for rule in (rules or []) if rule["role"] == "tariffs"
                                           for field in rule.get("price_fields", [])})
                    for field in price_fields:
                        value = str(record.get(field, "")).strip()
                        if value and (not re.fullmatch(r"\d[\d\s]*(?:[.,]\d{1,2})?\s*(?:₽|руб\.?|RUB)?", value, re.I)
                                      or (amount := to_number(value)) is None or amount < 0):
                            raise MappingError(f"Некорректная цена пакета «{field}»")
            field = "Услуга" if role == "services" else "Тариф"
            names = [record.get(field, "").casefold() for record in records]
            if any(not name for name in names) or len(names) != len(set(names)):
                raise MappingError(f"Пустые или повторяющиеся названия {role}")
            return
        if role == "durations":
            names = [record.get("Услуга", "").casefold() for record in records]
            if any(not name for name in names) or len(names) != len(set(names)):
                raise MappingError("Пустые или повторяющиеся услуги в длительностях")
            return
        if role not in {"slots", "bookings"}:
            return
        identifiers = set()
        for record in records:
            if role == "slots" and "Начало" in record:
                start_text, end_text = str(record.get("Начало", "")).strip(), str(record.get("Конец", "")).strip()
                if not start_text and not end_text:
                    continue
                start, end = parse_time(start_text), parse_time(end_text)
                if not start or not end or start >= end:
                    raise MappingError(f"Некорректный интервал графика работы, лист {record['__sheet_id__']}, строка {record['__row__']}")
                raw = str(record.get("Постов", "")).strip()
                if raw:
                    try:
                        number = float(raw.replace(",", "."))
                        if number < 0 or not number.is_integer():
                            raise ValueError
                    except ValueError:
                        raise MappingError("Некорректное количество постов") from None
                continue
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

    def _preflight(self) -> tuple[dict, dict]:
        """Полная ревалидация структуры и один свежий пакет чтения перед записью.

        Возвращаем (observed, tables): снимок и таблицы по ролям, чтобы вызывающие
        операции не извлекали одни и те же записи повторно.
        """
        observed = {}
        self.refresh(force=True, _observed=observed)
        tables = {}
        for role in ("services", "tariffs", "slots", "bookings"):
            tables[role] = self.table(role, _observed=observed)
        return observed, tables

    def _target(self, day: str | None = None, sheet_id: int | None = None) -> dict:
        rules = self._rules("bookings")
        if sheet_id is not None:
            rules = [rule for rule in rules if rule["sheet_id"] == sheet_id]
        elif len(rules) > 1 or rules[0].get("date_source"):
            rules = [rule for rule in rules if self._date_source(rule, self._date_cell_values(rule), rule["sheet_title"]) == date_value(day)]
        if len(rules) != 1 or not rules[0]["validation"].get("write"):
            raise MappingError("Запись в выбранную область не разрешена; проверьте mappings")
        return rules[0]

    def writable_column(self, sheet_id: int, field: str) -> int:
        """Физическая колонка канонического поля области листа (для адресных правок)."""
        rule = self._target(sheet_id=sheet_id)
        if field not in rule["columns"]:
            raise MappingError(f"В области «{rule['sheet_title']}» нет колонки {field}")
        return int(rule["columns"][field])

    def booking_fields(self) -> set:
        """Знать канонические поля записей всех подтвержённых областей (для проверок Store)."""
        with self.lock:
            try:
                rules = self._rules("bookings")
            except MappingError:
                return set()
            fields: set = set()
            for rule in rules:
                fields.update(rule["columns"])
            return fields

    def booking_sheet_ids(self) -> list:
        """Физические листы всех подтвержённых областей записей (для обслуживания)."""
        with self.lock:
            try:
                rules = self._rules("bookings")
            except MappingError:
                return []
            return [rule["sheet_id"] for rule in rules]

    def _date_cell_values(self, rule: dict) -> list[list]:
        """Сетка, содержащая только ячейку-источник даты, вместо чтения всего листа."""
        cell = (rule.get("date_source") or {}).get("cell")
        if not cell:
            return []
        row, column = cell
        raw = self._worksheet(rule["sheet_id"]).get(cell_name(row, column), value_render_option="FORMATTED_VALUE")
        values: list[list] = [[""] * column for _ in range(row)]
        values[row - 1][column - 1] = raw[0][0] if raw and raw[0] else ""
        return values

    def _journal(self) -> tuple[Path, dict]:
        path = self.path.with_name(self.path.stem + "-operations.json")
        if path.exists():
            try:
                entries = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(entries, dict) or not all(isinstance(field_value, dict) and field_value.get("state") in {"pending", "done"}
                                                           for field_value in entries.values()):
                    raise ValueError
                # Завершённые операции старше недели теряют смысл идемпотентности
                # (дубль всё равно отсечёт проверка записей), не копим журнал бесконечно.
                deadline = time.time() - 7 * 24 * 3600
                entries = {key: value for key, value in entries.items()
                           if value.get("state") != "done" or value.get("at", 0) >= deadline}
                return path, entries
            except ValueError:
                raise MappingError("Журнал операций повреждён; нужна ручная проверка") from None
        return path, {}

    # ---- сетка постов: клетка занимается маркером «Запись <ID>», освобождается при отмене/переносе

    def _grid_rules(self, day: str) -> list[dict]:
        """Сетка постов на дату. Сетка без права записи блокирует запись на эту дату."""
        wanted = date_value(day)
        rules = [rule for rule in self._rules("slots")
                 if rule.get("grid_columns") and self._date_source(rule, [], rule["sheet_title"]) == wanted]
        if any(not rule["validation"].get("write") for rule in rules):
            raise MappingError("Сетка постов на эту дату доступна только для чтения; проверьте mappings")
        if len(rules) > 1:
            raise MappingError("На эту дату несколько сеток постов; запись заблокирована")
        return rules

    def _grid_row_for_time(self, rule: dict, values: list[list], time_text: str) -> int:
        transform = rule.get("transforms", {}).get("Время", "time")
        end_row = min(len(values), rule.get("end_row") or len(values))
        rows = []
        for row in range(rule["first_data_row"], end_row + 1):
            raw = value_at(values, row, rule["columns"]["Время"])
            if str(raw).strip() and time_value(raw, transform) == time_text:
                rows.append(row)
        if not rows:
            raise SlotUnavailable("Этого времени нет в сетке постов")
        if len(rows) > 1:
            raise MappingError("Время повторяется в сетке постов; запись заблокирована")
        return rows[0]

    @staticmethod
    def _grid_cell_free(rule: dict, value: Any) -> bool:
        text = str(value).strip()
        if text.startswith("="):
            return False  # формула в клетке поста: не считаем её свободной
        return text.casefold() in {str(marker).strip().casefold() for marker in rule["free_markers"]}

    def _grid_claim(self, rule: dict, day: str, time_text: str, marker: str) -> dict:
        worksheet = self._worksheet(rule["sheet_id"])
        values = worksheet.get_all_values(value_render_option="FORMULA", date_time_render_option="FORMATTED_STRING")
        row = self._grid_row_for_time(rule, values, time_text)
        column = next((index for index in rule["grid_columns"] if self._grid_cell_free(rule, value_at(values, row, index))), None)
        if column is None:
            raise SlotUnavailable("На это время свободных постов нет")
        cell = cell_name(row, column)
        current = worksheet.get(f"{cell}:{cell}", value_render_option="FORMULA")
        value = current[0][0] if current and current[0] else ""
        if not self._grid_cell_free(rule, value):
            if str(value).strip() == marker.strip():
                # Своё занятие прошлой попытки (ответ потерян): повтор законен, клетку не трогаем.
                return {"sheet_id": rule["sheet_id"], "row": row, "column": column, "marker": marker}
            raise SlotUnavailable("Пост только что занят; выберите другое время")
        worksheet.batch_update([{"range": cell, "values": [[marker]]}], value_input_option="RAW")
        return {"sheet_id": rule["sheet_id"], "row": row, "column": column, "marker": marker}

    def _grid_release(self, claim: dict) -> bool:
        worksheet = self._worksheet(claim["sheet_id"])
        cell = cell_name(claim["row"], claim["column"])
        current = worksheet.get(f"{cell}:{cell}", value_render_option="FORMULA")
        value = current[0][0] if current and current[0] else ""
        if str(value).strip() != claim["marker"]:
            return False  # клетку уже изменили вручную: её не трогаем
        try:
            worksheet.batch_update([{"range": cell, "values": [[""]]}], value_input_option="RAW")
        except Exception:
            # Неизвестный результат: проверяем фактическую клетку, а не угадываем.
            current = worksheet.get(f"{cell}:{cell}", value_render_option="FORMULA")
            value = current[0][0] if current and current[0] else ""
            if str(value).strip() == claim["marker"]:
                raise
            return True
        return True

    def _grid_release_by_booking(self, rule: dict, time_text: str, booking_id: str) -> bool:
        worksheet = self._worksheet(rule["sheet_id"])
        values = worksheet.get_all_values(value_render_option="FORMULA", date_time_render_option="FORMATTED_STRING")
        row = self._grid_row_for_time(rule, values, time_text)
        marker = f"Запись {booking_id}"
        columns = [index for index in rule["grid_columns"] if str(value_at(values, row, index)).strip() == marker]
        if len(columns) != 1:
            return False
        return self._grid_release({"sheet_id": rule["sheet_id"], "row": row, "column": columns[0], "marker": marker})

    def _booking_row_state(self, rule: dict, row: int, expected: dict) -> bool | None:
        """True — строка содержит ожидаемые значения; False — нет; None — проверить не удалось."""
        try:
            worksheet = self._worksheet(rule["sheet_id"])
            raw = worksheet.get(f"A{row}:{cell_name(row, max(rule['columns'].values()))}", value_render_option="FORMATTED_VALUE")
            values = raw[0] if raw else []
            for field, wanted in expected.items():
                column = rule["columns"].get(field)
                if column is None:
                    return None
                cell = value_at([values], 1, column)
                if field == "Дата":
                    cell = date_value(cell, rule.get("transforms", {}).get("Дата", "date"))
                elif field == "Время":
                    cell = time_value(cell, rule.get("transforms", {}).get("Время", "time"))
                if str(cell).strip() != str(wanted).strip():
                    return False
            return True
        except Exception:
            return None

    def create(self, data: dict, expected_bookings: list[dict] | None = None) -> None:
        with self.lock:
            expected_versions = dict(self.read_versions)
            _, tables = self._preflight()
            for role in ROLE_FIELDS:
                if role not in expected_versions or self.read_versions[role] != expected_versions[role]:
                    raise MappingError("Данные или зависимости изменились перед записью; обновите предложение")
                if role == "bookings" and expected_bookings is not None and tables["bookings"][1] != expected_bookings:
                    raise MappingError("Записи изменились перед созданием; обновите свободное время")
            rule = self._target(data["дата"])
            worksheet = self._worksheet(rule["sheet_id"])
            # Не повторяем неопределённую операцию после сетевого сбоя/перезапуска.
            key = fingerprint({field_name: str(data[field_name]).strip().casefold() for field_name in ("telegram id", "дата", "время", "автомобиль")})
            journal_path, journal = self._journal()
            if key in journal:
                # Неопределённый итог прошлой попытки: своряем факт по постоянному ID,
                # а не повторяем запись вслепую.
                if self._reconcile_create(rule, data, journal, journal_path, key):
                    return  # строка с этим ID уже на листе: повторная запись не выполнялась
            # Сравниваем в том же режиме, что и таблица (FORMATTED_VALUE): в FORMULA телефон «+7…»,
            # в отображении «7…», и любая сверка ложно видит «строки изменились».
            values = worksheet.get_all_values(value_render_option="FORMATTED_VALUE")
            if rule.get("date_source") and self._date_source(rule, values, worksheet.title) != date_value(data["дата"]):
                raise MappingError("Дата области изменилась непосредственно перед записью")
            if expected_bookings is not None:
                target_records = [row_index for row_index in expected_bookings if row_index["__sheet_id__"] == rule["sheet_id"]]
                if self._extract_records(rule, values, worksheet.title) != target_records:
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
            grid_rules = self._grid_rules(data["дата"])
            # Журнал пишем до занятия клетки: сбой между занятием и журналом оставлял бы
            # маркер без записи и без доказательства для ручной сверки.
            journal[key] = {"state": "pending", "sheet_id": rule["sheet_id"], "row": row, "at": time.time(),
                            "id": str(data["id"])}
            atomic_json(journal_path, journal)
            if grid_rules:
                try:
                    self._grid_claim(grid_rules[0], data["дата"], data["время"], f"Запись {data['id']}")
                except MappingError:
                    # Ошибка до записи клетки (нет времени/поста, неоднозначная сетка):
                    # операции нет, повтор разрешён.
                    journal.pop(key, None)
                    try:
                        atomic_json(journal_path, journal)
                    except Exception:
                        log.warning("Не удалось очистить журнал после отказа сетки")
                    raise
                except Exception:
                    # Результат занятия клетки неизвестен: возможный маркер и журнал
                    # сохраняем, повтор блокируется до ручной сверки.
                    raise
            try:
                worksheet.batch_update(updates, value_input_option="RAW")
            except Exception:
                # Отсутствие строки не доказывает отсутствие записи: запрос мог дойти позже.
                # Подтверждённая строка — успех; во всех остальных случаях клетка и журнал остаются.
                if self._booking_row_state(rule, row, {"ID": data["id"]}) is not True:
                    raise MappingError("Результат записи неизвестен. Проверьте Sheets и локальный журнал перед повтором") from None
            journal[key]["state"] = "done"
            try:
                atomic_json(journal_path, journal)
            except Exception:
                # Строка уже подтверждена; сбой журнала не должен превращать успех в отказ.
                log.warning("Журнал операций не обновлён после подтверждённой записи")

    def _reconcile_create(self, rule: dict, data: dict, journal: dict, path, key: str) -> bool | None:
        """Неопределённый итог создания: сверяем лист по постоянному ID записи.

        Строка с этим ID и той же личностью есть — операция уже состоялась, ответ был
        потерян: журнал закрывается, повтор не выполняется. Строк с этим ID нет, а
        адресованная строка журнала пуста — первая попытка не доехала до листа:
        журнал снимается, повтор разрешается. Иначе исход спорный: остаётся
        заблокированным до ручной сверки, журнал не изменяется.
        Возвращает True — запись уже на листе; None — можно повторять.
        """
        worksheet = self._worksheet(rule["sheet_id"])
        try:
            values = worksheet.get_all_values(value_render_option="FORMATTED_VALUE")
        except Exception as error:
            log.warning("Сверка прошлой записи %s не удалась (%s)", data["id"], type(error).__name__)
            raise MappingError("Не удалось сверить прошлую запись; повторите позже") from None
        id_column = rule["columns"].get("ID")
        last_row = min(len(values), rule.get("end_row") or len(values))
        matches = [row_index for row_index in range(rule["first_data_row"], last_row + 1)
                   if id_column and str(value_at(values, row_index, id_column)).strip() == str(data["id"]).strip()]
        entry = journal.get(key) or {}
        if len(matches) > 1:
            raise MappingError("Несколько строк с этим ID записи; нужна ручная сверка") from None
        if len(matches) == 1:
            identity = {"ID": str(data["id"]), "Telegram ID": str(data["telegram id"]),
                        "Дата": data["дата"], "Время": data["время"]}
            if self._booking_row_state(rule, matches[0], identity) is not True:
                raise MappingError("Строка с этим ID хранит другие данные; нужна ручная сверка") from None
            entry["state"] = "done"
            try:
                atomic_json(path, journal)
            except Exception:
                # Строка подтверждена листом; сбой журнала не отменяет восстановление.
                log.warning("Журнал операций не обновлён после сверки создания")
            log.info("Создание восстановлено: запись %s уже на листе, повтор не выполнялся", data["id"])
            return True
        row = int(entry.get("row") or 0)
        if row and any(str(field_value).strip() for field_value in (values[row - 1] if row <= len(values) else [])):
            raise MappingError("Строка прошлой попытки занята другими данными; нужна ручная сверка") from None
        journal.pop(key, None)
        try:
            atomic_json(path, journal)
        except Exception:
            log.warning("Не удалось очистить журнал после сверки создания")
        log.info("Создание восстановлено: строка не дошла до листа, повтор разрешён")
        return None

    def cancel(self, record: dict, telegram_id: int) -> None:
        with self.lock:
            _, tables = self._preflight()
            rule = self._target(sheet_id=record["__sheet_id__"])
            matches = [candidate for candidate in tables["bookings"][1] if candidate["ID"] == record["ID"]]
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
            shown = worksheet.get(f"A{row}:{cell_name(row, max(rule['columns'].values()))}", value_render_option="FORMATTED_VALUE")
            candidates = self._extract_records(row_rule, shown, worksheet.title)
            if len(candidates) != 1 or any(candidates[0].get(field) != current.get(field) for field in rule["columns"]):
                raise MappingError("Строка записи перемещена или изменена; отмена заблокирована")
            journal_path, journal = self._journal()
            cancel_key = self._cancel_key(current)
            journal[cancel_key] = {"state": "pending", "sheet_id": rule["sheet_id"], "row": row, "at": time.time(),
                                   "marker": f"Запись {current['ID']}"}
            try:
                atomic_json(journal_path, journal)
            except Exception:
                log.warning("Не удалось записать журнал отмены; продолжаем по фактической таблице")
            grid_rules = self._grid_rules(current["Дата"]) if current.get("Дата") else []
            try:
                worksheet.batch_update([{"range": cell_name(row, rule["columns"]["Статус"]),
                                  "values": [[rule["write_statuses"]["cancelled"]]]}], value_input_option="RAW")
            except Exception:
                # Ответ потерян: успех подтверждаем фактическим статусом строки.
                if self._booking_row_state(rule, row, {"Статус": rule["write_statuses"]["cancelled"]}) is not True:
                    # Журнал остаётся pending как улика неизвестного результата.
                    raise MappingError("Результат отмены неизвестен. Проверьте таблицу перед повтором") from None
            grid_released = self._release_cancelled_cell(grid_rules, current)
            journal[cancel_key]["state"] = "done"
            key = fingerprint({field_name.lower(): str(current.get(field_name, "")).strip().casefold()
                          for field_name in ("Telegram ID", "Дата", "Время", "Автомобиль")})
            if journal.get(key, {}).get("state") == "done":
                del journal[key]
            try:
                atomic_json(journal_path, journal)
            except Exception:
                log.warning("Журнал операций не обновлён после подтверждённой отмены")
            return grid_released

    @staticmethod
    def _cancel_key(record: dict) -> str:
        return fingerprint({"cancel": str(record.get("ID", "")), "sheet": record.get("__sheet_id__"),
                            "row": record.get("__row__")})

    def _release_cancelled_cell(self, grid_rules: list[dict], current: dict) -> bool | None:
        """Освобождение клетки отменённой записи; сбой не отменяет саму отмену."""
        if not grid_rules:
            return None
        try:
            return self._grid_release_by_booking(grid_rules[0], current["Время"], str(current["ID"]))
        except Exception:
            log.warning("Клетку поста не удалось освободить при отмене записи %s", current.get("ID"))
            return False

    def grid_audit(self, bookings_by_id: dict[str, list[dict]], is_active) -> dict:
        """Сверка клеток сетки с записями: ограниченная, только подтверждённые выводы.

        Свой маркер «Запись <ID>» снимается, когда запись с этим ID ровно одна и она
        отменена, либо когда перенос уже подтверждён журналом. Чужие и ручные пометки,
        дубли ID и маркеры не на месте активной записи — только фиксируются.
        """
        from sheets import parse_date, parse_time
        with self.lock:
            report = {"checked": 0, "cleared": [], "ambiguous": [], "missing": []}
            try:
                journal_path, journal = self._journal()
            except MappingError:
                journal = {}
            for rule in self.document["regions"]:
                if rule["role"] != "slots" or not rule.get("grid_columns") or not rule["validation"].get("read"):
                    continue
                worksheet = self._worksheet(rule["sheet_id"])
                values = worksheet.get_all_values(value_render_option="FORMULA", date_time_render_option="FORMATTED_STRING")
                day = self._date_source(rule, values, worksheet.title)
                markers: dict[str, tuple[int, int]] = {}
                end_row = min(len(values), rule.get("end_row") or len(values))
                time_transform = rule.get("transforms", {}).get("Время", "time")
                for row in range(rule["first_data_row"], end_row + 1):
                    row_time = time_value(value_at(values, row, rule["columns"]["Время"]), time_transform)
                    for column in rule["grid_columns"]:
                        report["checked"] += 1
                        text = str(value_at(values, row, column)).strip()
                        match = re.fullmatch(r"Запись\s+(\d+)", text)
                        if not match:
                            continue  # чужие и ручные пометки не наши
                        booking_id = match.group(1)
                        markers.setdefault(f"Запись {booking_id}", (row, column))
                        records = bookings_by_id.get(booking_id, [])
                        cell = cell_name(row, column)
                        if len(records) != 1:
                            report["ambiguous"].append({"day": day, "cell": cell, "booking_id": booking_id,
                                                        "reason": "запись не найдена или дублируется"})
                            continue
                        record = records[0]
                        if is_active(record):
                            if (parse_date(record.get("Дата", "")) != parse_date(day)
                                    or parse_time(record.get("Время", "")) != row_time):
                                if not self._journal_has_marker(journal, text):
                                    report["ambiguous"].append({"day": day, "cell": cell, "booking_id": booking_id,
                                                                "reason": "маркер не на месте активной записи"})
                                    continue
                                try:
                                    if self._grid_release({"sheet_id": rule["sheet_id"], "row": row,
                                                           "column": column, "marker": text}):
                                        report["cleared"].append({"day": day, "cell": cell,
                                                                  "booking_id": booking_id,
                                                                  "reason": "перенос подтверждён журналом"})
                                    else:
                                        report["ambiguous"].append({"day": day, "cell": cell,
                                                                    "booking_id": booking_id,
                                                                    "reason": "клетка изменена вручную"})
                                except Exception:
                                    report["ambiguous"].append({"day": day, "cell": cell, "booking_id": booking_id,
                                                                "reason": "не удалось освободить клетку"})
                            continue  # активная запись: клетка на месте или спорная — выше
                        try:
                            if self._grid_release({"sheet_id": rule["sheet_id"], "row": row, "column": column,
                                                   "marker": text}):
                                report["cleared"].append({"day": day, "cell": cell, "booking_id": booking_id,
                                                          "reason": "запись отменена"})
                            else:
                                report["ambiguous"].append({"day": day, "cell": cell, "booking_id": booking_id,
                                                            "reason": "клетка изменена вручную"})
                        except Exception:
                            report["ambiguous"].append({"day": day, "cell": cell, "booking_id": booking_id,
                                                        "reason": "не удалось освободить клетку"})
                for booking_id, records in bookings_by_id.items():
                    if len(records) != 1 or not is_active(records[0]):
                        continue
                    record = records[0]
                    if parse_date(record.get("Дата", "")) != parse_date(day):
                        continue
                    if f"Запись {booking_id}" not in markers:
                        report["missing"].append({"day": day, "booking_id": booking_id,
                                                  "reason": "у активной записи нет клетки в сетке"})
            return report

    @staticmethod
    def _journal_has_marker(journal: dict, marker: str) -> bool:
        return any(entry.get("marker") == marker and entry.get("state") in {"pending", "done"}
                   for entry in journal.values())

    def clean_expired_slots(self, today, horizon_days: int = 7) -> tuple[int, int]:
        """Обслуживание слотовых областей линейной раскладки: три шага.

        1. Прошедшие даты очищаются адресной записью пустых значений в колонки
           раскладки; сегодняшние и будущие не трогаются, строки с формулами нет.
        2. Оставшиеся строки сдвигаются вверх без пустышек: перезапись целых строк
           с первой строки области. Сдвиг пропускается, если в области есть
           формулы, непонятные непустые строки или объединённые ячейки.
        3. Недостающие будущие даты (в пределах horizon_days) дополняются по
           шаблону прошлых недель того же дня недели: время и посты берутся
           как было, дата — новая в формате листа. Целые дни со слотами не
           пересоздаются.
        Сеточные раскладки (дата в названии листа) не обслуживаются. today —
        локальный день в часовом поясе бота. Возвращает (очищено, добавлено).
        """
        with self.lock:
            try:
                rules = self._rules("slots")
            except MappingError:
                return 0, 0
            cleared_rows = 0
            added_rows = 0
            for rule in rules:
                if rule.get("grid_columns") or "Дата" not in rule["columns"]:
                    continue  # дата содержится в названии листа — обслуживание не выполняется
                if rule["structure"].get("merges"):
                    log.warning("Слоты «%s»: объединённые ячейки в области — обслуживание пропущено",
                                rule["sheet_title"])
                    continue
                worksheet = self._worksheet(rule["sheet_id"])
                try:
                    values = worksheet.get_all_values(value_render_option="FORMULA",
                                                      date_time_render_option="FORMATTED_STRING")
                except Exception as error:
                    log.warning("Слоты «%s» не прочитаны; обслуживание пропущено (%s)",
                                rule["sheet_title"], type(error).__name__)
                    continue
                end_row = min(len(values), rule.get("end_row") or len(values))
                first_data_row = rule["first_data_row"]
                date_transform = rule.get("transforms", {}).get("Дата", "date")
                date_column = rule["columns"]["Дата"]

                known: dict = {}
                has_formulas = False
                has_unknown = False
                for row in range(first_data_row, end_row + 1):
                    raw = values[row - 1] if row <= len(values) else []
                    if not any(str(field_value).strip() for field_value in raw):
                        continue
                    if any(str(field_value).startswith("=") for field_value in raw):
                        has_formulas = True
                    try:
                        slot_date = datetime.strptime(
                            date_value(value_at(values, row, date_column), date_transform), "%d.%m.%Y").date()
                    except (MappingError, ValueError, TypeError):
                        has_unknown = True
                        continue
                    known.setdefault(slot_date, []).append((row, raw))

                # Шаг 1: автопроизводство недостающих будущих дат — до чистки, чтобы
                # шаблоном послужили ещё живые строки прошлых недель.
                appended_entries: list = []
                if not has_formulas:
                    appended, appended_entries = self._fill_future_slots(
                        worksheet, rule, values, today, horizon_days)
                    added_rows += appended
                    if appended:
                        log.info("Слоты «%s»: добавлено строк будущего расписания: %d",
                                 rule["sheet_title"], appended)

                # Шаг 2: очистка прошедших дат (формульные строки не трогаются).
                past_updates: list[dict] = []
                for slot_date, entries in list(known.items()):
                    if slot_date >= today:
                        continue
                    for row, raw in entries:
                        if any(str(field_value).startswith("=") for field_value in raw):
                            log.warning("Слоты «%s», строка %d: формула в прошедшей строке — не очищается",
                                        rule["sheet_title"], row)
                            continue
                        for column in rule["columns"].values():
                            past_updates.append({"range": cell_name(row, column), "values": [[""]]})
                        cleared_rows += 1
                    known.pop(slot_date)
                if past_updates:
                    self._batch_cells(worksheet, past_updates, rule["sheet_title"])
                if cleared_rows:
                    log.info("Слоты «%s»: очищено устаревших строк: %d", rule["sheet_title"], cleared_rows)

                # Шаг 3: сдвиг вверх будущих строк (при формульных или непонятных строках
                # сдвиг пропускается целиком — их позиции не меняются).
                future_rows = sorted(
                    tuple((row, raw) for entries in known.values() for (row, raw) in entries)
                    + tuple(appended_entries),
                    key=lambda item: item[0])
                if not has_formulas and not has_unknown:
                    appended_last_row = appended_entries[-1][0] if appended_entries else end_row
                    self._shift_slots_up(worksheet, rule, future_rows, first_data_row,
                                         max(end_row, appended_last_row))
                else:
                    log.info("Слоты «%s»: сдвиг пропущен (%s)", rule["sheet_title"],
                             "формулы в области" if has_formulas else "непонятные строки")
            return cleared_rows, added_rows

    def _batch_cells(self, worksheet, updates: list[dict], title: str) -> None:
        try:
            worksheet.batch_update(updates, value_input_option="RAW")
        except AttributeError:
            # Синтетические листы в тестах умеют только поячеечную запись.
            for update in updates:
                match = re.fullmatch(r"([A-Z]+)(\d+)", update["range"])
                if not match:
                    continue
                column_number = 0
                for letter in match.group(1):
                    column_number = column_number * 26 + ord(letter) - 64
                worksheet.update_cell(int(match.group(2)), column_number, update["values"][0][0])
        except Exception as error:
            log.warning("Слоты «%s»: запись ячеек не удалась (%s)", title, type(error).__name__)

    def _shift_slots_up(self, worksheet, rule: dict, entries: list, first_data_row: int, end_row: int) -> None:
        """Переписать будущие строки плотно с первой строки области и очистить хвост."""
        width = max(rule["columns"].values())
        updates: list[dict] = []
        for offset, (source_row, raw) in enumerate(entries):
            target_row = first_data_row + offset
            if target_row == source_row:
                continue  # строка уже на своём месте
            full = (list(raw) + [""] * width)[:width]
            for column, value in enumerate(full, 1):
                updates.append({"range": cell_name(target_row, column), "values": [[value]]})
        if updates:
            self._batch_cells(worksheet, updates, rule["sheet_title"])
        tail_clears = [{"range": cell_name(row, column), "values": [[""]]}
                       for row in range(first_data_row + len(entries), end_row + 1)
                       for column in rule["columns"].values()]
        if tail_clears:
            self._batch_cells(worksheet, tail_clears, rule["sheet_title"])

    def _fill_future_slots(self, worksheet, rule: dict, values: list, today, horizon_days: int) -> tuple[int, list]:
        """Дописать недостающие будущие даты по прошлым неделям (тот же день недели).

        Шаблон берётся из исходного снимка: строки прошлых недель могут быть
        стёрты очисткой. Новые строки дописываются за последним заданным слотом.
        Возвращает (добавлено строк, [(строка, содержимое)], ...) — сдвиг затем
        поднимает их вместе с остальными будущими строками.
        """
        end_row = min(len(values), rule.get("end_row") or len(values))
        date_transform = rule.get("transforms", {}).get("Дата", "date")
        width = max(rule["columns"].values())
        grouped: dict = {}
        for row in range(rule["first_data_row"], end_row + 1):
            raw = values[row - 1] if row <= len(values) else []
            if not any(str(field_value).strip() for field_value in raw):
                continue
            if any(str(field_value).startswith("=") for field_value in raw):
                continue  # формулы не используются как шаблон
            try:
                slot_date = datetime.strptime(
                    date_value(value_at(values, row, rule["columns"]["Дата"]), date_transform), "%d.%m.%Y").date()
            except (MappingError, ValueError, TypeError):
                continue
            grouped.setdefault(slot_date, []).append(list(raw) + [""] * width)
        missing = []
        for offset in range(1, horizon_days + 1):
            day = today + timedelta(days=offset)
            if day not in grouped:
                missing.append(day)
        if not missing:
            return 0, []
        start_row = rule["first_data_row"]
        for row in range(end_row, rule["first_data_row"] - 1, -1):
            raw = values[row - 1] if row <= len(values) else []
            if any(str(field_value).strip() for field_value in raw):
                start_row = row + 1
                break
        updates: list[dict] = []
        appended: list = []
        for day in missing:
            template = None
            for weeks_back in range(1, 5):
                candidate = day - timedelta(days=7 * weeks_back)
                if candidate in grouped:
                    template = grouped[candidate]
                    break
            if not template:
                continue
            for raw in template:
                new_raw = (list(raw) + [""] * width)[:width]
                new_raw[rule["columns"]["Дата"] - 1] = day.strftime("%d.%m.%Y")
                for column, value in enumerate(new_raw, 1):
                    updates.append({"range": cell_name(start_row, column), "values": [[value]]})
                appended.append((start_row, new_raw))
                start_row += 1
        if not updates:
            return 0, []
        self._batch_cells(worksheet, updates, rule["sheet_title"])
        return len(appended), appended

    def reconcile_cancel(self, record: dict) -> bool | None:
        """Доводим прерванную отмену: запись уже «Отмена», клетка могла остаться занятой.

        Клетка снимается только при точном совпадении маркера «Запись <ID>» и одной
        записи с этим ID; спорные клетки не трогаются.
        """
        with self.lock:
            if not record.get("Дата") or not record.get("ID"):
                return None
            try:
                grid_rules = self._grid_rules(record["Дата"])
            except MappingError:
                return None  # сетка неоднозначна или только для чтения: без ручной сверки не трогаем
            if not grid_rules:
                return None
            released = self._release_cancelled_cell(grid_rules, record)
            journal_path, journal = self._journal()
            key = self._cancel_key(record)
            if journal.get(key, {}).get("state") == "pending":
                journal[key]["state"] = "done"
                try:
                    atomic_json(journal_path, journal)
                except Exception:
                    log.warning("Журнал операций не обновлён после сверки отмены")
            return released

    def settle_create(self, booking_id: str, telegram_id: int) -> None:
        """Сверка незакрытого создания по постоянному ID: строка уже на листе.

        Потерянный ответ оставлял журнал pending; клиент повторил запрос, и дубль-проверка
        нашла его запись. Подтверждаем личность по строке закрываем журнал, чтобы не
        оставлять вечный маркер блокировки.
        """
        with self.lock:
            try:
                path, journal = self._journal()
            except MappingError:
                return
            targets = {key: entry for key, entry in journal.items()
                       if entry.get("state") == "pending" and str(entry.get("id", "")) == str(booking_id).strip()}
            if not targets:
                return
            settled = []
            for key, entry in targets.items():
                try:
                    rule = self._target(sheet_id=entry["sheet_id"])
                except MappingError:
                    continue  # область недоступна: журнал остаётся до ручной сверки
                if self._booking_row_state(rule, entry.get("row"),
                                           {"ID": str(booking_id).strip(), "Telegram ID": str(telegram_id)}) is True:
                    settled.append(key)
            if not settled:
                return  # строка не подтверждает личность: сверку делает администратор
            for key in settled:
                journal[key]["state"] = "done"
            log.info("Создание восстановлено по повтору клиента: запись %s подтверждена листом, журнал закрыт", booking_id)
            try:
                atomic_json(path, journal)
            except Exception:
                log.warning("Журнал операций не обновлён после сверки создания по ID")

    def validate_reschedule(self, record: dict, new_date: str) -> dict:
        rule = self._target(sheet_id=record["__sheet_id__"])
        if rule.get("date_source") and date_value(new_date) != date_value(record["Дата"]):
            raise MappingError("Перенос между отдельными областями дат требует администратора; исходная запись сохранена")
        if "Время" not in rule["columns"] or "Дата" not in rule["columns"] and date_value(new_date) != date_value(record["Дата"]):
            raise MappingError("Нет доступных для изменения колонок даты и времени")
        return rule

    def reschedule(self, record: dict, telegram_id: int, new_date: str, new_time: str, *, bay: str | None = None) -> None:
        with self.lock:
            versions = dict(self.read_versions)
            _, tables = self._preflight()
            for role in ("slots", "bookings"):
                if role not in versions or self.read_versions[role] != versions[role]:
                    raise MappingError("Слоты или записи изменились перед переносом; проверьте время заново")
            rule = self.validate_reschedule(record, new_date)
            records = tables["bookings"][1]
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
            shown = worksheet.get(f"A{row}:{cell_name(row, max(rule['columns'].values()))}", value_render_option="FORMATTED_VALUE")
            candidates = self._extract_records(row_rule, shown, worksheet.title)
            if len(candidates) != 1 or any(candidates[0].get(field) != record.get(field) for field in rule["columns"]):
                raise MappingError("Строка записи перемещена или изменена; перенос заблокирован")
            date = datetime.strptime(new_date, "%Y-%m-%d")
            updates = []
            if bay and "Бокс" not in rule["columns"]:raise MappingError("Для переносов нужен столбец «Бокс»")
            changes = {"Дата": date.strftime("%d.%m.%Y"), "Время": new_time}
            if bay:changes["Бокс"] = bay
            for field, value in changes.items():
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
            new_day = date.strftime("%d.%m.%Y")
            new_grid = self._grid_rules(new_day)
            old_grid = self._grid_rules(record["Дата"]) if record.get("Дата") else []
            if journal.get(key, {}).get("state") == "pending":
                resumed = self._resume_pending_move(rule, worksheet, row, record, journal, path, key, updates,
                                                    new_day, new_time, old_grid)
                if resumed is not None:
                    return resumed
                raise MappingError("Результат прошлого переноса неизвестен; нужна ручная сверка")
            # Журнал до занятия клетки: иначе сбой между ними оставлял бы маркер без следа.
            journal[key] = {"state": "pending", "sheet_id": rule["sheet_id"], "row": row, "at": time.time(),
                            "marker": f"Запись {record['ID']}",
                            "old": {"Дата": record.get("Дата", ""), "Время": record.get("Время", ""),
                                    **({"Бокс": record.get("Бокс", "")} if bay else {})},
                            "new": {"Дата": new_day, "Время": new_time, **({"Бокс": bay} if bay else {})}}
            try:
                atomic_json(path, journal)
            except Exception:
                log.warning("Не удалось записать журнал переноса; продолжаем по фактической таблице")
            if new_grid:
                try:
                    claim = self._grid_claim(new_grid[0], new_day, new_time, f"Запись {record['ID']}")
                except MappingError:
                    # Ошибка до записи клетки: операция не началась, повтор разрешён.
                    journal.pop(key, None)
                    try:
                        atomic_json(path, journal)
                    except Exception:
                        log.warning("Не удалось очистить журнал после отказа сетки при переносе")
                    raise
                except Exception:
                    # Результат занятия клетки неизвестен: журнал и возможный маркер сохраняем.
                    raise
                journal[key]["claim"] = dict(claim)
                try:
                    atomic_json(path, journal)
                except Exception:
                    log.warning("Не удалось зафиксировать занятую клетку в журнале переноса")
            try:
                worksheet.batch_update(updates, value_input_option="RAW")
            except Exception:
                expected = {field_name: value for field_name, value in {**changes, "Дата": new_day}.items()
                            if field_name in rule["columns"]}
                if not expected or self._booking_row_state(rule, row, expected) is not True:
                    # Результат неизвестен: новая клетка и старая запись остаются до ручной сверки.
                    raise MappingError("Результат переноса неизвестен. Проверьте таблицу перед повтором") from None
            journal[key]["state"] = "done"
            grid_released = self._release_old_move_cell(old_grid, record, new_day, new_time)
            original = fingerprint({field.lower(): str(record.get(field, "")).strip().casefold()
                                    for field in ("Telegram ID", "Дата", "Время", "Автомобиль")})
            if journal.get(original, {}).get("state") == "done":
                journal.pop(original)
            try:
                atomic_json(path, journal)
            except Exception:
                # Перенос уже подтверждён строкой; сбой журнала не делает его неуспешным.
                log.warning("Журнал операций не обновлён после подтверждённого переноса")
            return grid_released

    def _release_old_move_cell(self, old_grid: list[dict], record: dict, new_day: str, new_time: str) -> bool | None:
        if not old_grid or (record.get("Дата"), record.get("Время")) == (new_day, new_time):
            return None
        try:
            return self._grid_release_by_booking(old_grid[0], record["Время"], str(record["ID"]))
        except Exception:
            log.warning("Старую клетку поста не удалось освободить после переноса записи %s", record.get("ID"))
            return False

    def _resume_pending_move(self, rule: dict, worksheet, row: int, record: dict, journal: dict, path, key: str,
                             updates: list[dict], new_day: str, new_time: str, old_grid: list[dict]) -> bool | None:
        """Перенос с неизвестным исходом: доводим по фактическому состоянию строки.

        Строка уже на новом месте — снимаем старую клетку и закрываем журнал.
        Строка ещё на старом месте и клетка наша — повторяем запись строки один раз.
        Иначе состояние спорное: остаётся заблокированным до ручной сверки.
        """
        new_state = journal[key].get("new", {"Дата": new_day, "Время": new_time})
        old_state = journal[key].get("old", {"Дата": record.get("Дата", ""), "Время": record.get("Время", "")})
        if self._booking_row_state(rule, row, new_state) is True:
            journal[key]["state"] = "done"
            released = self._release_old_move_cell(old_grid, record, new_day, new_time)
            try:
                atomic_json(path, journal)
            except Exception:
                log.warning("Журнал операций не обновлён после сверки переноса")
            return released if released is not None else True
        if not journal[key].get("claim") or self._booking_row_state(rule, row, old_state) is not True:
            return None
        claim = journal[key]["claim"]
        claim_sheet = self._worksheet(claim["sheet_id"])
        cell = f"{cell_name(claim['row'], claim['column'])}:{cell_name(claim['row'], claim['column'])}"
        try:
            current = claim_sheet.get(cell, value_render_option="FORMULA")
        except Exception:
            return None
        value = current[0][0] if current and current[0] else ""
        if str(value).strip() != claim["marker"]:
            return None  # клетку занял кто-то другой: ручная сверка
        try:
            worksheet.batch_update(updates, value_input_option="RAW")
        except Exception:
            if self._booking_row_state(rule, row, new_state) is True:
                journal[key]["state"] = "done"
                released = self._release_old_move_cell(old_grid, record, new_day, new_time)
                try:
                    atomic_json(path, journal)
                except Exception:
                    log.warning("Журнал операций не обновлён после сверки переноса")
                return released if released is not None else True
            return None  # результат записи снова неизвестен: журнал остаётся
        journal[key]["state"] = "done"
        released = self._release_old_move_cell(old_grid, record, new_day, new_time)
        try:
            atomic_json(path, journal)
        except Exception:
            log.warning("Журнал операций не обновлён после возобновлённого переноса")
        return released if released is not None else True
