"""AI-first карта Google Sheets и безопасные операции по физическим координатам.

Жизненный цикл:
1. снимок всей книги (метаданные, строки, формулы, объединения);
2. один вызов модели для смысловой разметки областей;
3. независимая проверка каждой координаты, типа и связи в Python;
4. атомарное сохранение проверенной карты, привязанной к SPREADSHEET_ID;
5. обычные операции читают и пишут только по сохранённым координатам;
6. структурный отпечаток проверяется на старте и каждые 6 часов;
7. изменилась структура — карта строится заново, проверяется и заменяется
   атомарно; данные, цены и обычные строки не вызывают AI.

Модуль не использует названия колонок как источник истины во время работы:
после активации карты единственный источник физических адресов — документ карты.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import logging
import math
import os
import re
import tempfile
from pathlib import Path
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

VERSION = 2
ENGINE_REVISION = 4
MAX_DISCOVERY_CELLS = 2_000_000
MAX_HEADER_SCAN_ROWS = 200
MAX_PROBE_ROWS = 40

BOOKING_FIELDS = [
    "ID", "Создано", "Дата", "Время", "Имя", "Телефон", "Автомобиль", "Класс",
    "Тариф", "Доп. услуги", "Состав", "Сумма", "Комментарий", "Telegram ID",
    "Username", "Статус", "Госномер", "Бокс", "Длительность",
]
# Базовый контракт записи: дополнительные поля (госномер/бокс/длительность)
# могут отсутствовать в старой книге и не должны блокировать запись сами по себе.
BOOKING_HEADERS = BOOKING_FIELDS[:16]
BOOKING_WRITE_REQUIRED = set(BOOKING_HEADERS)
EXTRA_ROLE_FIELDS = {"bookings": ["Госномер", "Бокс", "Длительность"]}
ROLE_FIELDS = {
    "bookings": BOOKING_FIELDS,
    "slots": ["Дата", "Время", "Постов", "Начало", "Конец", "Бокс"],
    "services": ["Услуга", "Описание", "Доступна", "Длительность"],
    "tariffs": ["Тариф", "Состав", "Скидка %", "Доступен", "Длительность", "Сумма"],
    "durations": ["Услуга", "Длительность"],
    "classes": ["Автомобиль", "Класс"],
    "resources": ["Бокс", "Дата", "Открытие поста", "Закрытие поста", "Статус", "Ограничение"],
    "archive": BOOKING_FIELDS,
}
OPTIONAL_ROLE_FIELDS = {
    "durations": ["Услуга", "Длительность"],
    "classes": ["Автомобиль", "Класс"],
    "resources": ["Бокс", "Открытие поста", "Закрытие поста", "Статус"],
    "archive": ["ID", "Статус"],
}
ALL_ROLE_FIELDS = {
    role: list(dict.fromkeys([*ROLE_FIELDS.get(role, ()), *OPTIONAL_ROLE_FIELDS.get(role, ())]))
    for role in set(ROLE_FIELDS) | set(OPTIONAL_ROLE_FIELDS)
}
CORE_ROLES = {"bookings", "services", "slots"}
WRITE_ROLES = {"bookings", "slots"}
READ_REQUIRED = {
    "bookings": {"ID", "Дата", "Время", "Telegram ID", "Статус"},
    "archive": {"ID", "Статус"},
    "slots": {"Время"},
    "services": {"Услуга"},
    "tariffs": {"Тариф"},
    "durations": {"Услуга", "Длительность"},
    "classes": {"Автомобиль", "Класс"},
    "resources": {"Бокс", "Открытие поста", "Закрытие поста", "Статус"},
}
SCHEDULE_REQUIRED = {"Начало", "Конец"}
STATUS_MAP = {
    "новая": "Новая", "new": "Новая", "новая заявка": "Новая",
    "confirmed": "Подтверждена", "подтверждена": "Подтверждена",
    "cancelled": "Отмена", "canceled": "Отмена", "отменена": "Отмена",
    "отмена": "Отмена", "отказ": "Отмена",
    "выполнена": "Выполнена", "done": "Выполнена", "completed": "Выполнена",
    "архив": "Архив", "archive": "Архив", "archived": "Архив",
}
KNOWN_PRICE_CLASSES = {
    "легковой", "кроссовер", "внедорожник", "минивэн", "внедорожник минивэн",
    "микроавтобус", "седан", "хэтчбек", "универсал", "sedan", "suv",
    "crossover", "van", "minivan", "passenger car",
}
NON_PRICE_HEADERS = {
    "id", "duration", "duration minutes", "duration hours", "длительность",
    "длительность мин", "длительность работы", "время мин", "время работы",
    "минуты", "часы", "номер", "код",
}

# Синонимы используются только для восстановления явно пропущенного моделью
# поля и для ручной диагностики. Первичное распознавание делает модель.
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
    "Госномер": ["госномер", "гос номер", "гос. номер", "госномер автомобиля",
                 "номер авто", "номер машины", "регистрационный знак", "plate",
                 "license plate", "car plate"],
    "Постов": ["постов", "мест", "боксов", "capacity", "bays", "пост", "бокс"],
    "Услуга": ["услуга", "service", "service name", "название услуги", "работа",
               "наименование работы"],
    "Описание": ["описание", "description", "details", "примечание"],
    "Доступна": ["доступна", "available", "active", "enabled", "активна",
                 "выполняется", "выполняем", "выполняется ли"],
    "Доступен": ["доступен", "available", "active", "enabled", "активен",
                 "доступна", "выполняется"],
    "Скидка %": ["скидка", "скидка %", "скидка процентов", "discount",
                 "discount %", "discount percent"],
    "Длительность": ["длительность", "длительность мин", "длительность минуты",
                     "длительность работы", "время мин", "время работы",
                     "время выполнения", "минуты", "мин", "часы", "час",
                     "duration", "duration minutes", "duration hours",
                     "минут на работу"],
    "Начало": ["начало", "start", "открытие", "старт", "открываемся", "открытие поста"],
    "Конец": ["конец", "окончание", "закрытие", "end", "финиш", "закрываемся",
              "закрытие поста"],
    "Открытие поста": ["открытие поста", "открытие", "начало работы поста"],
    "Закрытие поста": ["закрытие поста", "закрытие", "конец работы поста"],
    "Ограничение": ["ограничение", "допуск", "ограничения", "restriction"],
}

_FIELD_SYNONYMS = {
    "госномер": "Госномер", "гос номер": "Госномер", "гос. номер": "Госномер",
    "номер авто": "Госномер", "номер машины": "Госномер",
    "регистрационный знак": "Госномер", "license plate": "Госномер",
    "plate": "Госномер", "car plate": "Госномер",
    "created": "Создано", "created at": "Создано", "дата создания": "Создано",
    "date": "Дата", "booking date": "Дата", "time": "Время", "start time": "Время",
    "name": "Имя", "client": "Имя", "client name": "Имя", "фио": "Имя",
    "phone": "Телефон", "телефон": "Телефон", "тел": "Телефон",
    "vehicle": "Автомобиль", "car": "Автомобиль", "машина": "Автомобиль",
    "car class": "Класс", "size class": "Класс", "класс авто": "Класс",
    "package": "Тариф", "tariff": "Тариф", "комплекс": "Тариф",
    "extras": "Доп. услуги", "доп услуги": "Доп. услуги",
    "composition": "Состав", "items": "Состав", "включено": "Состав",
    "total": "Сумма", "итого": "Сумма", "стоимость": "Сумма", "price": "Сумма",
    "цена": "Сумма", "comment": "Комментарий", "notes": "Комментарий",
    "примечание": "Комментарий", "telegram id": "Telegram ID",
    "tg id": "Telegram ID", "telegram_id": "Telegram ID",
    "username": "Username", "ник": "Username", "status": "Статус",
    "состояние": "Статус", "service": "Услуга", "service name": "Услуга",
    "название услуги": "Услуга", "работа": "Услуга", "описание": "Описание",
    "description": "Описание", "available": "Доступна", "active": "Доступна",
    "enabled": "Доступна", "discount": "Скидка %", "скидка": "Скидка %",
    "duration": "Длительность", "начало": "Начало", "start": "Начало",
    "конец": "Конец", "end": "Конец", "постов": "Постов", "мест": "Постов",
    "capacity": "Постов", "bays": "Постов", "бокс": "Бокс", "bay": "Бокс",
    "resource": "Бокс", "открытие поста": "Открытие поста",
    "закрытие поста": "Закрытие поста", "ограничение": "Ограничение",
}
_CANONICAL_BY_FOLD = {
    field.casefold(): field
    for fields in (*ROLE_FIELDS.values(), *OPTIONAL_ROLE_FIELDS.values()) for field in fields
}


class MappingError(RuntimeError):
    pass


class SlotUnavailable(MappingError):
    """Время или пост занят, запись не создана. Текст безопасен для показа клиенту."""


DURATION_HEADER_RE = re.compile(
    r"длительность|duration|время (?:работы|выполнения)|^мин(?:ут\w*)?$|^час(?:ов|ы)?$"
)
DURATION_MAX_MINUTES = 12 * 60


def _normalize_header_cached(value: str) -> str:
    return re.sub(r"[^\w%]+", " ", value.strip().lower().replace("ё", "е")).strip()


def normalize_header(value: Any) -> str:
    return _normalize_header_cached(str(value))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


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


def title_date(title: str) -> str | None:
    from sheets import parse_date
    matches = re.findall(r"\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4}", title)
    dates = {parsed_date.isoformat() for text in matches if (parsed_date := parse_date(text))}
    return next(iter(dates)) if len(dates) == 1 else None


def date_value(value: Any, transform: str = "date") -> str:
    from sheets import parse_date
    try:
        if transform == "serial_date":
            parsed_date = (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
        elif transform and transform.startswith("date:"):
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


def rule_signature(rule: dict) -> str:
    return fingerprint({
        key: value for key, value in rule.items()
        if key not in {"structure", "fingerprint", "validation", "last_changes",
                       "validated_rule", "last_validation_at"}
    })


def file_stamp(path: Path) -> tuple | None:
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


def trim_headers(headers: list) -> list:
    result = [str(header).strip() for header in headers]
    while result and not result[-1]:
        result.pop()
    return result


def structure(snapshot: dict, rule: dict) -> dict:
    row = rule["header_row"]
    headers = trim_headers(snapshot["values"][row - 1]) if row <= len(snapshot["values"]) else []
    formula_columns = sorted({
        column_index for row_index, column_index in (snapshot.get("formulas") or [])
        if row_index >= rule["first_data_row"]
    })
    return {
        "sheet_id": snapshot["id"],
        "title": snapshot["title"],
        "headers": headers,
        "header_row": row,
        "merges": snapshot.get("merges") or [],
        "formula_columns": formula_columns,
        "column_count": snapshot.get("column_count", len(headers)),
        "grid_columns": rule.get("grid_columns", []),
        "date_source": rule.get("date_source"),
    }


def diff_structure(before: dict, after: dict) -> dict:
    previous_headers = before.get("headers", [])
    current_headers = after.get("headers", [])
    return {
        "added": [index for index, header in enumerate(current_headers, 1)
                  if header and header not in previous_headers],
        "deleted": [index for index, header in enumerate(previous_headers, 1)
                    if header and header not in current_headers],
        "moved": [header for header in previous_headers
                  if header and previous_headers.count(header) == current_headers.count(header) == 1
                  and previous_headers.index(header) != current_headers.index(header)],
        "renamed": [
            {"column": index + 1, "before": old_header, "after": new_header}
            for index, (old_header, new_header) in enumerate(zip(previous_headers, current_headers))
            if old_header != new_header and old_header not in current_headers
            and new_header not in previous_headers
        ],
        "other": [key for key in after if key != "headers" and before.get(key) != after[key]],
    }


def header_mapping(headers: list, role: str) -> tuple[dict, list[str]]:
    """Диагностическое сопоставление по синонимам (не основной путь discovery)."""
    columns = {}
    errors = []
    for field in (*ALL_ROLE_FIELDS.get(role, ()), *EXTRA_ROLE_FIELDS.get(role, ())):
        aliases = {normalize_header(alias) for alias in ALIASES.get(field, [])}
        hits = [index for index, header in enumerate(headers, 1)
                if normalize_header(header) in aliases]
        if len(hits) == 1:
            columns[field] = hits[0]
        elif len(hits) > 1:
            errors.append(f"Несколько колонок для {field}")
    return columns, errors


def is_duration_header(value: Any) -> bool:
    text = normalize_header(value)
    return bool(text) and bool(DURATION_HEADER_RE.search(text))


def duration_unit_hint(value: Any) -> str | None:
    text = normalize_header(value)
    if re.search(r"час|hour", text):
        return "hours"
    if re.search(r"мин|minute", text):
        return "minutes"
    return None


DURATION_WORDS = {
    "полчаса": 30, "час": 60, "часа": 60, "часов": 60, "часик": 60,
    "полтора": 90, "два": 120, "две": 120, "три": 180, "четыре": 240, "пять": 300,
}


def _word_duration(text: str) -> int | None:
    """«полчаса», «полтора часа», «два часа» — когда смысл однозначен."""
    match = re.fullmatch(r"(\w+)(\s+час\w*)?", text)
    if not match:
        return None
    word = match.group(1)
    if word == "полтора":
        return 90 if match.group(2) else None
    return DURATION_WORDS.get(word)


def _duration_total(minutes: float) -> int | None:
    result = int(round(minutes))
    return result if 0 < result <= DURATION_MAX_MINUTES else None


def parse_duration_minutes(value: Any, unit_hint: str | None = None) -> int | None:
    """Длительность в целых минутах из достоверных табличных форматов, иначе None."""
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


def canonical_status(value: Any) -> str | None:
    text = normalize_header(value)
    if not text:
        return None
    if text in STATUS_MAP:
        return STATUS_MAP[text]
    if text.startswith(("отмен", "cancel", "отказ")):
        return "Отмена"
    if text.startswith(("нов", "new")):
        return "Новая"
    if text.startswith(("подтвержд", "confirm")):
        return "Подтверждена"
    if text.startswith(("выполн", "done", "complete")):
        return "Выполнена"
    if text.startswith(("архив", "archiv")):
        return "Архив"
    return None


def _canonical_field(role: str, key: str) -> tuple[str | None, bool]:
    """Возвращает (каноническое поле, является ли ключ классом/ценой)."""
    text = str(key).strip()
    if not text:
        return None, False
    allowed = set(ROLE_FIELDS.get(role, ())) | set(EXTRA_ROLE_FIELDS.get(role, ()))
    if text in allowed:
        return text, False
    folded = text.casefold()
    by_fold = {field.casefold(): field for field in allowed}
    if folded in by_fold:
        return by_fold[folded], False
    normalized = normalize_header(text)
    if normalized in _FIELD_SYNONYMS:
        mapped = _FIELD_SYNONYMS[normalized]
        if mapped in allowed:
            return mapped, False
    if normalized in _CANONICAL_BY_FOLD:
        mapped = _CANONICAL_BY_FOLD[normalized]
        if mapped in allowed:
            return mapped, False
    return None, False


def _known_price_class(header: Any) -> bool:
    text = normalize_header(header)
    if text in KNOWN_PRICE_CLASSES:
        return True
    return any(token in text.split() for token in KNOWN_PRICE_CLASSES if " " not in token) or \
        any(token in text for token in KNOWN_PRICE_CLASSES if " " in token)


def _safe_error(error: Exception) -> str:
    text = " ".join(str(error).split())[:500]
    text = re.sub(r"(?i)(api[_-]?key|authorization|bearer)\s*[:=]\s*\S+", r"\1=<redacted>", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{10,}", "<redacted>", text)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


class AdaptiveSheets:
    """Единый mapping-backend: карта, проверка структуры и адресные операции."""

    def __init__(self, config, spreadsheet):
        self.config = config
        self.spreadsheet = spreadsheet
        self.path = Path(config.sheets_mapping_file)
        self.lock = threading.RLock()
        self.document = None
        self.stamp = None
        self.force_pending = bool(getattr(config, "sheets_force_schema_refresh", False))
        self.worksheets = {}
        self.read_versions = {}
        self.named_ranges = []
        self.unsupported_sheets = []
        self.ai_requests = 0
        self.last_error = ""
        self.expected = {}

    # ---------- загрузка и сохранение карты ----------

    def _load(self) -> None:
        stamp = file_stamp(self.path)
        if self.document is not None and stamp == self.stamp:
            return
        if stamp is None:
            self.document = None
            self.stamp = None
            return
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("version") != VERSION:
                raise ValueError("версия карты не поддерживается")
            if document.get("spreadsheet_id") != self.config.spreadsheet_id:
                raise ValueError("карта принадлежит другой таблице")
            if not isinstance(document.get("regions"), list):
                raise ValueError("нет списка областей")
            if not isinstance(document.get("checked_at"), (int, float)) or not math.isfinite(document["checked_at"]):
                raise ValueError("нет времени проверки")
            for rule in document["regions"]:
                if not isinstance(rule, dict) or rule.get("role") not in ALL_ROLE_FIELDS:
                    raise ValueError("некорректная область карты")
                if not isinstance(rule.get("columns"), dict) or not isinstance(rule.get("header_row"), int):
                    raise ValueError("некорректные координаты области")
                if rule.get("validated_rule") != rule_signature(rule):
                    rule["validation"] = {
                        "read": False, "write": False,
                        "issues": ["Ручные изменения требуют --validate-mappings"],
                        "write_issues": ["Ручные изменения требуют --validate-mappings"],
                    }
                    rule["manual_edit"] = True
        except (ValueError, OSError, TypeError) as error:
            log.error("Карта Sheets не загружена: %s. Нужна новая AI-разметка", error)
            self.document = None
            self.stamp = stamp
            return
        self.document = document
        self.stamp = stamp
        self.named_ranges = document.get("named_ranges") or []
        self._summarize()
        if document.get("engine_revision") != ENGINE_REVISION:
            log.info("Карта Sheets создана прежней версией движка; нужна детерминированная проверка")
            self.force_pending = True

    def _save(self) -> None:
        if self.document is None:
            return
        self.document["saved_at"] = time.time()
        if self.path.exists():
            try:
                backup = self.path.with_name(self.path.stem + ".bak" + self.path.suffix)
                backup.write_bytes(self.path.read_bytes())
            except OSError:
                pass
        atomic_json(self.path, self.document)
        self.stamp = file_stamp(self.path)

    def _worksheet(self, sheet_id: int):
        # Не кэшируем объект по id: после переименования листа старый title
        # приводил бы к ложному «структура изменилась».
        worksheet = self.spreadsheet().get_worksheet_by_id(sheet_id)
        self.worksheets[sheet_id] = worksheet
        return worksheet

    def _typed_formulas(self, sheet_id: int, title: str, candidates: list[tuple[int, int]]) -> set[tuple[int, int]]:
        found = set()
        if not candidates:
            return found
        prefix = "'" + title.replace("'", "''") + "'!"
        rows, columns = zip(*candidates)
        area = prefix + cell_name(min(rows), min(columns)) + ":" + cell_name(max(rows), max(columns))
        metadata = self.spreadsheet().fetch_sheet_metadata(params={
            "includeGridData": True,
            "ranges": [area],
            "fields": "sheets(properties(sheetId),data(startRow,startColumn,rowData(values(userEnteredValue(formulaValue)))))",
        })
        sheets = [sheet for sheet in metadata.get("sheets", [])
                  if sheet.get("properties", {}).get("sheetId") == sheet_id]
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
        self.named_ranges = [
            {"name": row_index.get("name"), "range": row_index.get("range")}
            for row_index in metadata.get("namedRanges", [])
        ]
        entries = [sheet for sheet in metadata.get("sheets", [])
                   if sheet["properties"].get("sheetType", "GRID") == "GRID"]
        self.unsupported_sheets = [
            f"Лист {sheet['properties']['sheetId']}: тип {sheet['properties'].get('sheetType')} не поддерживается"
            for sheet in metadata.get("sheets", []) if sheet not in entries
        ]
        ranges = []
        for entry in entries:
            props = entry["properties"]
            grid = props.get("gridProperties", {})
            if grid.get("rowCount", 0) * grid.get("columnCount", 0) > MAX_DISCOVERY_CELLS:
                raise MappingError(f"Лист {props['sheetId']} слишком большой для безопасного автоанализа")
            ranges.append("'" + props["title"].replace("'", "''") + "'")
        formatted, formulas = [], []
        for offset in range(0, len(ranges), 50):
            group = ranges[offset:offset + 50]
            formatted.extend(spreadsheet.values_batch_get(
                group, params={"valueRenderOption": "FORMATTED_VALUE"}).get("valueRanges", []))
            formulas.extend(spreadsheet.values_batch_get(
                group, params={"valueRenderOption": "FORMULA"}).get("valueRanges", []))
        if len(formatted) != len(entries) or len(formulas) != len(entries):
            raise MappingError("Google вернул неполный набор диапазонов")
        snapshots = []
        for entry, values, formula_values in zip(entries, formatted, formulas):
            props = entry["properties"]
            grid = props.get("gridProperties", {})
            positions = [
                (row_index, column_index)
                for row_index, row in enumerate(formula_values.get("values", []), 1)
                for column_index, cell_value in enumerate(row, 1)
                if str(cell_value).startswith("=")
            ]
            positions = sorted(self._typed_formulas(props["sheetId"], props["title"], positions))
            snapshots.append({
                "id": props["sheetId"],
                "title": props["title"],
                "values": values.get("values", []),
                "formulas": positions,
                "raw_values": formula_values.get("values", []),
                "merges": entry.get("merges", []),
                "column_count": grid.get("columnCount", 0),
                "row_count": grid.get("rowCount", 0),
                "index": props.get("index"),
                "sheet_type": props.get("sheetType", "GRID"),
            })
        return snapshots

    def _probe_snapshots(self) -> list[dict]:
        """Дешёвая проверка структуры: метаданные + строки вокруг известных шапок."""
        spreadsheet = self.spreadsheet()
        metadata = spreadsheet.fetch_sheet_metadata(params={"fields": "sheets(properties,merges),namedRanges"})
        self.named_ranges = [
            {"name": row_index.get("name"), "range": row_index.get("range")}
            for row_index in metadata.get("namedRanges", [])
        ]
        regions = (self.document or {}).get("regions", [])
        by_sheet: dict[int, list[dict]] = {}
        for rule in regions:
            by_sheet.setdefault(rule.get("sheet_id"), []).append(rule)
        ranges, entries = [], []
        for entry in metadata.get("sheets", []):
            props = entry["properties"]
            if props.get("sheetType", "GRID") != "GRID":
                continue
            grid = props.get("gridProperties", {})
            row_count = grid.get("rowCount", 0) or 1
            column_count = grid.get("columnCount", 0) or 1
            rows = 20
            wanted_columns = 60
            for rule in by_sheet.get(props["sheetId"], []):
                # Даже нестандартная шапка в строке 60 должна попасть в проверку.
                rows = max(rows, min(row_count, int(rule.get("header_row", 1)) + 2, 220))
                wanted_columns = max(wanted_columns, max(rule.get("columns", {}).values() or [0]) + 2)
            rows = min(rows, row_count)
            columns = min(max(wanted_columns, 1), column_count)
            range_name = "'" + props["title"].replace("'", "''") + "'!A1:" + cell_name(rows, columns)
            ranges.append(range_name)
            entries.append((props, entry, grid))
        values = []
        for offset in range(0, len(ranges), 50):
            values.extend(spreadsheet.values_batch_get(
                ranges[offset:offset + 50],
                params={"valueRenderOption": "FORMATTED_VALUE"}).get("valueRanges", []))
        if len(values) != len(ranges):
            raise MappingError("Google вернул неполный структурный снимок")
        snapshots = []
        for (props, entry, grid), value_range in zip(entries, values):
            snapshots.append({
                "id": props["sheetId"],
                "title": props["title"],
                "values": value_range.get("values", []),
                "formulas": [],
                "raw_values": [],
                "merges": entry.get("merges", []),
                "column_count": grid.get("columnCount", 0),
                "row_count": grid.get("rowCount", 0),
                "index": props.get("index"),
                "sheet_type": props.get("sheetType", "GRID"),
            })
        return snapshots

    # ---------- структурный отпечаток ----------

    def _structure_signature(self, snapshots: list[dict], regions: list[dict]) -> str:
        by_sheet: dict[int, list[dict]] = {}
        for rule in regions:
            by_sheet.setdefault(rule.get("sheet_id"), []).append(rule)
        sheets = []
        for snapshot in snapshots:
            views = []
            for rule in sorted(by_sheet.get(snapshot["id"], []),
                               key=lambda item: (item.get("role", ""), item.get("header_row", 0))):
                header_row = int(rule.get("header_row", 0))
                headers = trim_headers(snapshot["values"][header_row - 1]) \
                    if 0 < header_row <= len(snapshot["values"]) else []
                views.append({
                    "role": rule.get("role"),
                    "header_row": header_row,
                    "headers": headers,
                    "first_data_row": rule.get("first_data_row"),
                    "end_row": rule.get("end_row"),
                    "columns": rule.get("columns", {}),
                    "price_fields": rule.get("price_fields", []),
                    "grid_columns": rule.get("grid_columns", []),
                    "date_source": rule.get("date_source"),
                    "schedule_mode": rule.get("schedule_mode"),
                    "prices_transposed": bool(rule.get("prices_transposed")),
                    "pivot_prices": rule.get("pivot_prices"),
                })
            sheets.append({
                "id": snapshot.get("id"),
                "title": snapshot.get("title"),
                "type": snapshot.get("sheet_type", "GRID"),
                "merges": snapshot.get("merges") or [],
                "regions": views,
            })
        return fingerprint({
            "spreadsheet_id": self.config.spreadsheet_id,
            "sheets": sheets,
            "named_ranges": self.named_ranges,
        })

    # ---------- AI-разметка и проверка ----------

    def _resolve_header(self, headers: list[str], claimed: str) -> tuple[int, str] | None:
        text = str(claimed or "").strip()
        if not text:
            return None
        match = re.fullmatch(r"@\s*(\d+)|(?:column|колонка)\s*(\d+)", text, re.I)
        if match:
            index = int(match.group(1) or match.group(2))
            if 1 <= index <= len(headers):
                return index, str(headers[index - 1]).strip()
            return None
        exact = [index for index, header in enumerate(headers, 1) if str(header).strip() == text]
        if len(exact) == 1:
            return exact[0], str(headers[exact[0] - 1]).strip()
        normalized = normalize_header(text)
        loose = [index for index, header in enumerate(headers, 1)
                 if normalize_header(header) == normalized]
        if len(loose) == 1:
            return loose[0], str(headers[loose[0] - 1]).strip()
        return None

    def _candidate_rule(self, candidate: dict, snapshots_by_id: dict[int, dict],
                        by_title: dict[str, dict], issues: list[str]) -> dict | None:
        role = candidate["role"]
        snapshot = None
        if candidate.get("sheet_id") in snapshots_by_id:
            snapshot = snapshots_by_id[candidate["sheet_id"]]
        if snapshot is None and candidate.get("sheet_title"):
            snapshot = by_title.get(candidate["sheet_title"].casefold())
        if snapshot is None:
            issues.append(f"Модель: лист «{candidate.get('sheet_title')}» не найден в книге")
            return None
        values = snapshot.get("values") or []
        header_row = candidate.get("header_row")
        if not isinstance(header_row, int):
            # Модель иногда не указала строку: ищем строку с максимальным числом
            # подтверждённых заголовков среди первых строк, но не угадываем данные.
            best_row, best_hits = None, -1
            for row_number in range(1, min(len(values), MAX_HEADER_SCAN_ROWS) + 1):
                headers = trim_headers(values[row_number - 1])
                hits = 0
                for claimed in candidate.get("columns", {}).values():
                    if self._resolve_header(headers, claimed):
                        hits += 1
                if hits > best_hits:
                    best_row, best_hits = row_number, hits
            header_row = best_row if best_hits > 0 else 1
        if not 1 <= header_row <= min(MAX_HEADER_SCAN_ROWS, len(values)):
            issues.append(f"Модель: некорректная строка шапки {header_row} на листе «{snapshot['title']}»")
            return None
        headers = trim_headers(values[header_row - 1])
        columns: dict[str, int] = {}
        physical: dict[str, str] = {}
        price_fields: list[str] = []
        unresolved: list[str] = []
        extra_columns: list[dict] = []

        def add(field: str, claimed: str, *, price: bool = False) -> None:
            resolved = self._resolve_header(headers, claimed)
            if resolved is None:
                issues.append(f"Модель: шапка «{claimed}» ({field}) не подтверждена на листе «{snapshot['title']}»")
                unresolved.append(field)
                return
            index, actual = resolved
            if index in columns.values():
                # Одна физическая колонка не может быть двумя полями без явного правила.
                issues.append(f"Модель: колонка {index} назначена дважды ({field})")
                unresolved.append(field)
                return
            columns[field] = index
            physical[field] = actual or (f"@{index}")
            if price and field not in price_fields:
                price_fields.append(field)

        for key, claimed in (candidate.get("columns") or {}).items():
            field, _ = _canonical_field(role, key)
            if field is not None:
                add(field, claimed)
                continue
            # Старый/свободный формат модели: неизвестный ключ у услуг/тарифов —
            # это класс или услуга с ценой. Проверяем по числовым значениям.
            if role in {"services", "tariffs"}:
                add(str(key).strip(), claimed, price=True)
                continue
            resolved = self._resolve_header(headers, claimed)
            if resolved is not None:
                extra_columns.append({"header": resolved[1], "role": str(key).strip()})

        for key, claimed in (candidate.get("price_columns") or {}).items():
            add(str(key).strip(), claimed, price=True)

        # Служебные поля, которые модель могла не назвать, но которые однозначно
        # подтверждаются фактической шапкой. Это восстановление, не первичный поиск.
        salvaged = []
        for field in ("Госномер", "Бокс", "Длительность"):
            if role in {"bookings", "archive"} and field not in columns:
                aliases = {normalize_header(alias) for alias in ALIASES[field]}
                hits = [index for index, header in enumerate(headers, 1)
                        if normalize_header(header) in aliases]
                if len(hits) == 1:
                    columns[field] = hits[0]
                    physical[field] = str(headers[hits[0] - 1]).strip() or f"@{hits[0]}"
                    salvaged.append(field)
        if role in {"bookings", "archive", "slots"}:
            for field in ("Дата", "Время", "Постов", "Начало", "Конец"):
                if field in columns:
                    continue
                aliases = {normalize_header(alias) for alias in ALIASES.get(field, [])}
                hits = [index for index, header in enumerate(headers, 1)
                        if normalize_header(header) in aliases]
                if len(hits) == 1:
                    columns[field] = hits[0]
                    physical[field] = str(headers[hits[0] - 1]).strip() or f"@{hits[0]}"
                    salvaged.append(field)

        # Пустая шапка описания услуги: единственная текстовая колонка между
        # «Услуга» и первой ценой. Значение подтверждается данными, а не догадкой.
        if role == "services" and "Описание" not in columns:
            blank_candidates = []
            for index, header in enumerate(headers, 1):
                if str(header).strip() or index in columns.values():
                    continue
                samples = [str(value_at(values, row_index, index)).strip()
                           for row_index in range(header_row + 1, min(len(values), header_row + 8) + 1)]
                if any(samples):
                    blank_candidates.append(index)
            if len(blank_candidates) == 1:
                index = blank_candidates[0]
                columns["Описание"] = index
                physical["Описание"] = f"@{index}"
                salvaged.append("Описание")

        if not columns:
            return None

        # Числовые колонки, не назначенные полям: цены услуг/тарифов.
        if role in {"services", "tariffs"} and not candidate.get("orientation") == "long_prices":
            used = set(columns.values())
            for index, header in enumerate(headers, 1):
                if not str(header).strip() or index in used or is_duration_header(header):
                    continue
                samples = []
                for row_index in range(header_row + 1, min(len(values), header_row + 20) + 1):
                    text = str(value_at(values, row_index, index)).strip()
                    if text:
                        samples.append(text)
                if samples and all(re.fullmatch(
                        r"[-+]?\d[\d\s]*(?:[.,]\d+)?\s*(?:₽|руб\.?|RUB)?", sample, re.I)
                        for sample in samples):
                    field = str(header).strip()
                    if field not in columns:
                        columns[field] = index
                        physical[field] = field
                        if field not in price_fields:
                            price_fields.append(field)
                    used.add(index)

        orientation = candidate.get("orientation")
        prices_transposed = orientation in {"transposed", "services_in_columns"}
        if prices_transposed:
            # Ключи-услуги уже находятся в columns; поле «Класс» обязательно.
            price_fields = [key for key in columns if key != "Класс"]
        pivot = orientation == "long_prices" or (
            role == "services" and "Класс" in columns and "Сумма" in columns
        )
        if pivot:
            price_fields = []
        if role in {"services", "tariffs"} and not price_fields and not pivot and not prices_transposed:
            # Единственная цена без классов: «Сумма»/«Цена» — это price field.
            for field in ("Сумма", "Цена", "Стоимость"):
                if field in columns:
                    price_fields.append(field)
            if not price_fields:
                if role == "tariffs" and "Состав" in columns:
                    pass  # пакеты с составом и скидкой не обязаны иметь цены по классам
                else:
                    issues.append(f"Область {role} на листе «{snapshot['title']}»: не найдены колонки цен")
                    unresolved.append("цена")

        grid_columns: list[int] = []
        for claimed in candidate.get("grid_columns") or []:
            resolved = self._resolve_header(headers, claimed)
            if resolved is None:
                issues.append(f"Модель: колонка сетки «{claimed}» не подтверждена на листе «{snapshot['title']}»")
            else:
                grid_columns.append(resolved[0])
        if role == "slots" and not grid_columns:
            detected = [
                index for index, header in enumerate(headers, 1)
                if re.fullmatch(r"(?:пост|бокс|bay|post)\s*\d+", str(header).strip(), re.I)
            ]
            if detected:
                grid_columns = detected

        # Источник даты: колонка, название листа или одна ячейка.
        date_source = None
        raw_source = candidate.get("date_source") or {}
        source_type = raw_source.get("type") if isinstance(raw_source, dict) else None
        if source_type == "cell":
            row, column = raw_source.get("row"), raw_source.get("column")
            if isinstance(row, int) and isinstance(column, int) and row >= 1 and column >= 1:
                date_source = {"cell": [row, column]}
            else:
                issues.append(f"Модель: некорректная ячейка даты на листе «{snapshot['title']}»")
        elif source_type == "title" or (role in {"slots", "bookings", "archive"}
                                        and "Дата" not in columns and title_date(snapshot["title"])):
            date_source = {"title": True}
        if "Дата" in columns:
            date_source = None

        schedule_mode = candidate.get("schedule_mode")
        if role == "slots":
            if {"Начало", "Конец"} <= set(columns):
                schedule_mode = "hours"
            elif grid_columns:
                schedule_mode = "grid"
            else:
                schedule_mode = "slots"

        rule = {
            "role": role,
            "sheet_id": snapshot["id"],
            "sheet_title": snapshot["title"],
            "header_row": header_row,
            "first_data_row": header_row + 1,
            "end_row": candidate.get("end_row") if candidate.get("end_row") and candidate["end_row"] > header_row else None,
            "columns": columns,
            "physical_headers": physical,
            "price_fields": price_fields,
            "status_map": dict(STATUS_MAP),
            "write_statuses": {"new": "Новая", "cancelled": "Отмена"},
            "transforms": {"Дата": "date", "Время": "time", "Начало": "time", "Конец": "time"},
            "date_source": date_source,
            "orientation": orientation or "records",
            "permissions": {
                "read": True,
                "write": role in {"bookings"} or (role == "slots" and bool(grid_columns)),
            },
            "validation": {"read": False, "write": False, "issues": []},
            "source": "ai",
        }
        if price_fields:
            rule["price_fields"] = price_fields
        if prices_transposed:
            rule["prices_transposed"] = True
        if pivot:
            rule["pivot_prices"] = {"class_field": "Класс", "value_field": "Сумма"}
        if grid_columns:
            rule["grid_columns"] = grid_columns
            rule["free_markers"] = ["", "свободно", "free", "open", "свободен"]
        if schedule_mode:
            rule["schedule_mode"] = schedule_mode
        if salvaged:
            rule["salvaged_fields"] = salvaged
        if extra_columns:
            rule["extra_columns"] = extra_columns
        if unresolved:
            rule["unresolved_fields"] = sorted(set(unresolved))
        self._infer_transforms(rule, snapshot)
        self._infer_statuses(rule, snapshot, candidate.get("status_values") or [])
        rule["structure"] = structure(snapshot, rule)
        rule["fingerprint"] = fingerprint(rule["structure"])
        return rule

    def _infer_transforms(self, rule: dict, snapshot: dict) -> None:
        values = snapshot.get("values") or []
        header_row = rule["header_row"]
        transforms = rule.setdefault("transforms", {})
        for field, transform_field in (("Дата", "date"), ("Время", "time"), ("Начало", "time"), ("Конец", "time")):
            column = rule["columns"].get(field)
            if not column:
                continue
            samples = []
            for row_number in range(header_row + 1, min(len(values), header_row + 20) + 1):
                text = str(value_at(values, row_number, column)).strip()
                if text:
                    samples.append(text)
            if field == "Дата":
                numeric = [sample for sample in samples if re.fullmatch(r"\d+(?:[.,]\d+)?", sample)]
                if numeric and all(1 <= float(sample.replace(",", ".")) <= 100000 for sample in numeric):
                    transforms[field] = "serial_date"
                else:
                    transforms[field] = "date"
            else:
                numeric = [sample for sample in samples if re.fullmatch(r"0?\.\d+", sample)]
                if numeric and len(numeric) == len(samples):
                    transforms[field] = "serial_time"
                else:
                    transforms[field] = "time"

    def _infer_statuses(self, rule: dict, snapshot: dict, ai_values: list[str]) -> None:
        values = snapshot.get("values") or []
        header_row = rule["header_row"]
        column = rule["columns"].get("Статус")
        observed: list[str] = []
        if column:
            for row_number in range(header_row + 1, min(len(values), header_row + 100) + 1):
                text = str(value_at(values, row_number, column)).strip()
                if text:
                    observed.append(text)
        observed.extend(str(value).strip() for value in ai_values if str(value).strip())
        status_map = dict(STATUS_MAP)
        for value in observed:
            canonical = canonical_status(value)
            if canonical:
                status_map[normalize_header(value)] = canonical
        rule["status_map"] = status_map
        new_value = next((value for value in observed if canonical_status(value) == "Новая"), "Новая")
        cancel_value = next((value for value in observed if canonical_status(value) == "Отмена"), "Отмена")
        rule["write_statuses"] = {"new": new_value, "cancelled": cancel_value}

    def _salvage_missing_roles(self, regions: list[dict], snapshots: list[dict],
                               issues: list[str], roles: set[str] | None = None) -> None:
        """Неполный ответ модели: однозначное восстановление по фактическим шапкам.

        Это не первичный discovery: функция вызывается только после ответа модели
        (или при недоступности провайдера), а результат всё равно проходит полную
        проверку координат и типов.
        """
        present = {rule["role"] for rule in regions}
        wanted_roles = roles or set(CORE_ROLES)

        def alias_hits(field: str) -> list[int]:
            aliases = {normalize_header(alias) for alias in ALIASES.get(field, [])}
            return [index for index, header in enumerate(headers, 1)
                    if normalize_header(header) in aliases]

        for snapshot in snapshots:
            already_used = snapshot["id"] in {rule["sheet_id"] for rule in regions}
            for row_number in range(1, min(len(snapshot["values"]), MAX_HEADER_SCAN_ROWS) + 1):
                headers = trim_headers(snapshot["values"][row_number - 1])
                if len([header for header in headers if str(header).strip()]) < 2:
                    continue
                booking_like = header_mapping(headers, "bookings")[0]
                roles_to_try = set(wanted_roles - present)
                if title_date(snapshot["title"]) and "slots" in present:
                    roles_to_try.add("slots")
                if "services" in wanted_roles and "services" in present and not already_used:
                    roles_to_try.add("services")
                for role in sorted(roles_to_try):
                    if already_used and role != "services":
                        continue  # несколько таблиц одного листа размечает AI
                    if role == "archive" and not any(
                            token in normalize_header(snapshot["title"])
                            for token in ("архив", "archive")):
                        continue  # архивный журнал должен быть явно архивным листом
                    duplicate_price = False
                    if role in {"services", "tariffs", "slots", "durations"} and (
                            "Telegram ID" in booking_like or
                            ({"ID", "Дата", "Время", "Статус"} <= set(booking_like))):
                        continue
                    columns, errors = header_mapping(headers, role)
                    if role == "durations":
                        # Отдельный справочник длительностей, а не колонка внутри
                        # прайса услуг: иначе переопределения теряются.
                        extra = [str(header).strip() for index, header in enumerate(headers, 1)
                                 if str(header).strip() and index not in columns.values()
                                 and not is_duration_header(header)]
                        if extra:
                            continue
                    pivot = False
                    if role == "services":
                        for field in ("Класс", "Сумма"):
                            if field in columns:
                                continue
                            hits = alias_hits(field)
                            if len(hits) == 1:
                                columns[field] = hits[0]
                        pivot = {"Класс", "Сумма"} <= set(columns)
                        duration_hits = alias_hits("Длительность")
                        if "Длительность" not in columns and len(duration_hits) == 1:
                            columns["Длительность"] = duration_hits[0]
                        if not pivot:
                            price_fields = []
                            for index, header in enumerate(headers, 1):
                                if not str(header).strip() or index in columns.values() or is_duration_header(header):
                                    continue
                                samples = [str(value_at(snapshot["values"], sample_row, index)).strip()
                                           for sample_row in range(row_number + 1,
                                                                   min(len(snapshot["values"]), row_number + 8) + 1)]
                                samples = [sample for sample in samples if sample]
                                class_like = _known_price_class(header) or any(
                                    token in normalize_header(header)
                                    for token in ("цена", "стоимость", "price"))
                                if samples and (class_like or all(re.fullmatch(
                                        r"[-+]?\d[\d\s]*(?:[.,]\d+)?\s*(?:₽|руб\.?|RUB)?",
                                        sample, re.I) for sample in samples)):
                                    normalized_header = normalize_header(header)
                                    if normalized_header in {normalize_header(field) for field in price_fields}:
                                        duplicate_price = True
                                    price_fields.append(str(header).strip())
                            if not price_fields:
                                if errors:
                                    issues.extend(f"Восстановление {role}: {issue}" for issue in errors)
                                continue
                            for field in price_fields:
                                columns[field] = headers.index(field) + 1
                    elif role == "tariffs":
                        duration_hits = alias_hits("Длительность")
                        if "Длительность" not in columns and len(duration_hits) == 1:
                            columns["Длительность"] = duration_hits[0]
                        used = set(columns.values())
                        for index, header in enumerate(headers, 1):
                            if not str(header).strip() or index in used or is_duration_header(header):
                                continue
                            if normalize_header(header) in {normalize_header(alias) for alias in ALIASES["Скидка %"]}:
                                continue
                            samples = [str(value_at(snapshot["values"], sample_row, index)).strip()
                                       for sample_row in range(row_number + 1,
                                                               min(len(snapshot["values"]), row_number + 8) + 1)]
                            samples = [sample for sample in samples if sample]
                            class_like = _known_price_class(header) or any(
                                token in normalize_header(header)
                                for token in ("цена", "стоимость", "price"))
                            if samples and (class_like or all(re.fullmatch(
                                    r"[-+]?\d[\d\s]*(?:[.,]\d+)?\s*(?:₽|руб\.?|RUB)?",
                                    sample, re.I) for sample in samples)):
                                if any(normalize_header(headers[column - 1]) == normalize_header(header)
                                       for column in columns.values()):
                                    duplicate_price = True
                                columns[str(header).strip()] = index
                    elif role in {"bookings", "archive"}:
                        for field in ("Госномер", "Бокс", "Длительность"):
                            if field in columns:
                                continue
                            hits = alias_hits(field)
                            if len(hits) == 1:
                                columns[field] = hits[0]
                    elif role == "slots":
                        grid = [index for index, header in enumerate(headers, 1)
                                if re.fullmatch(r"(?:пост|бокс|bay|post)\s*\d+",
                                                str(header).strip(), re.I)]
                        if grid:
                            columns["Время"] = columns.get("Время", alias_hits("Время")[0]
                                                           if len(alias_hits("Время")) == 1 else None)
                            columns = {key: value for key, value in columns.items() if value}
                    if errors:
                        issues.extend(f"Восстановление {role}: {issue}" for issue in errors)
                        continue
                    title_day = title_date(snapshot["title"])
                    required = set(READ_REQUIRED[role])
                    if role in {"bookings", "archive"} and title_day and "Дата" not in columns:
                        required.discard("Дата")
                    if role == "slots":
                        if {"Начало", "Конец"} <= set(columns):
                            required = set(SCHEDULE_REQUIRED)
                        elif "Дата" not in columns and title_day:
                            required = {"Время"}
                        elif "Постов" not in columns and "Время" in columns:
                            # Сетка постов: вместимость вычисляется по колонкам постов.
                            pass
                    if role == "services" and pivot:
                        required = {"Услуга"}
                    if not required <= set(columns):
                        continue
                    rule = {
                        "role": role, "sheet_id": snapshot["id"], "sheet_title": snapshot["title"],
                        "header_row": row_number, "first_data_row": row_number + 1, "end_row": None,
                        "columns": columns, "physical_headers": {
                            field: str(headers[index - 1]).strip() or f"@{index}"
                            for field, index in columns.items()
                        },
                        "status_map": dict(STATUS_MAP),
                        "write_statuses": {"new": "Новая", "cancelled": "Отмена"},
                        "transforms": {"Дата": "date", "Время": "time", "Начало": "time", "Конец": "time"},
                        "date_source": ({"title": True}
                                        if role in {"slots", "bookings", "archive"}
                                        and "Дата" not in columns and title_day else None),
                        "orientation": "long_prices" if pivot else "records",
                        "permissions": {"read": True, "write": role == "bookings"},
                        "validation": {"read": False, "write": False, "issues": []},
                        "source": "deterministic-recovery",
                    }
                    if role == "services":
                        if pivot:
                            rule["pivot_prices"] = {"class_field": "Класс", "value_field": "Сумма"}
                        else:
                            rule["price_fields"] = [field for field in columns
                                                    if field not in ROLE_FIELDS["services"]
                                                    and field not in {"Класс", "Сумма", "Длительность"}]
                            unknown = [field for field in rule["price_fields"]
                                       if not _known_price_class(field)]
                            if unknown:
                                rule["unresolved_fields"] = sorted(unknown)
                                issues.append(
                                    "Восстановление services: незнакомые колонки цен "
                                    + ", ".join(unknown) + "; нужно подтверждение")
                        if duplicate_price:
                            rule["duplicate_price"] = True
                            issues.append("Восстановление services: дублирующиеся колонки цен")
                    if role == "slots":
                        grid = [index for index, header in enumerate(headers, 1)
                                if re.fullmatch(r"(?:пост|бокс|bay|post)\s*\d+",
                                                str(header).strip(), re.I)]
                        if grid:
                            rule["grid_columns"] = grid
                            rule["free_markers"] = ["", "свободно", "free", "open", "свободен"]
                            rule["schedule_mode"] = "grid"
                            rule["permissions"]["write"] = True
                        elif {"Начало", "Конец"} <= set(columns):
                            rule["schedule_mode"] = "hours"
                        else:
                            rule["schedule_mode"] = "slots"
                    if role == "tariffs":
                        rule["price_fields"] = [field for field in columns
                                                if field not in ROLE_FIELDS["tariffs"]
                                                and field not in {"Класс", "Сумма"}]
                    if duplicate_price:
                        rule["duplicate_price"] = True
                        issues.append(f"Восстановление {role}: дублирующиеся колонки цен")
                    self._infer_transforms(rule, snapshot)
                    self._infer_statuses(rule, snapshot, [])
                    rule["structure"] = structure(snapshot, rule)
                    rule["fingerprint"] = fingerprint(rule["structure"])
                    regions.append(rule)
                    present.add(role)
                    issues.append(f"Ответ модели был неполным: область {role} восстановлена по фактической шапке "
                                  f"листа «{snapshot['title']}» и требует проверки")
                    break

    def _validate_rule(self, rule: dict, snapshot: dict) -> dict:
        if rule.get("unresolved_fields"):
            rule["unresolved_fields"] = sorted(
                field for field in rule["unresolved_fields"]
                if field in rule.get("columns", {}) or field in rule.get("price_fields", []))
        errors = self._rule_structure(rule, snapshot)
        present = self._rule_fields(rule, snapshot, errors)
        formula_headers = self._rule_read_issues(rule, snapshot, errors)
        write_errors = self._rule_write_issues(rule, snapshot, errors, present, formula_headers)
        read_ok = not errors and bool(rule["permissions"].get("read"))
        rule["validation"] = {
            "read": read_ok,
            "write": read_ok and not write_errors and bool(rule["permissions"].get("write")),
            "issues": errors,
            "write_issues": write_errors,
            "validated_at": time.time(),
        }
        rule["validated_rule"] = rule_signature(rule)
        return rule

    def _rule_structure(self, rule: dict, snapshot: dict) -> list[str]:
        errors = []
        role = rule.get("role")
        columns = rule.get("columns", {})
        if role not in ALL_ROLE_FIELDS or not isinstance(columns, dict):
            raise MappingError("Неверное назначение области или columns")
        if any(not isinstance(field, str) or type(column_index) is not int
               for field, column_index in columns.items()):
            raise MappingError("columns: нужны строковые имена полей и целочисленные номера")
        for key, expected_type in (
            ("transforms", dict), ("status_map", dict), ("write_statuses", dict),
            ("price_fields", list), ("grid_columns", list), ("unresolved_fields", list),
            ("physical_headers", dict), ("extra_columns", list),
        ):
            if key in rule and not isinstance(rule[key], expected_type):
                raise MappingError("Некорректная структура параметра " + key)
        if not columns or any(column_index < 1 or column_index > snapshot["column_count"]
                              for column_index in columns.values()):
            errors.append("Некорректные номера колонок")
        if len(set(columns.values())) != len(columns):
            errors.append("Два поля ссылаются на одну колонку")
        allowed = set(ROLE_FIELDS.get(role, ())) | set(EXTRA_ROLE_FIELDS.get(role, ()))
        allowed |= set(rule.get("price_fields", []))
        if rule.get("pivot_prices"):
            allowed.update({"Класс", "Сумма"})
        if rule.get("prices_transposed"):
            allowed.add("Класс")
            allowed |= set(columns) - {"Класс"}
        if rule.get("schedule_mode") == "hours":
            allowed.update({"Начало", "Конец"})
        if "Длительность" in columns:
            allowed.add("Длительность")
        if set(columns) - allowed:
            errors.append("Неизвестные поля columns")
        if role in {"services", "tariffs"}:
            mapped_headers = [
                normalize_header(snapshot["values"][rule["header_row"] - 1][column_index - 1])
                if column_index <= len(snapshot["values"][rule["header_row"] - 1]) else ""
                for column_index in columns.values()
            ]
            if len(mapped_headers) != len(set(mapped_headers)):
                errors.append("Дублирующиеся колонки цен")
        first = rule.get("first_data_row")
        header = rule.get("header_row")
        end_row = rule.get("end_row")
        if (type(header) is not int or type(first) is not int
                or not 1 <= header < first <= snapshot["row_count"]):
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
                        not isinstance(source["cell"], list) or len(source["cell"]) != 2
                        or any(type(value) is not int or value < 1 for value in source["cell"])
                    ):
                        raise MappingError("Некорректная ссылка даты")
                    if source.get("title") is not True and "cell" not in source:
                        raise MappingError("Некорректный источник даты")
                    self._date_source(rule, snapshot["values"], snapshot["title"])
                    present.add("Дата")
                except MappingError:
                    errors.append("Не подтверждён источник даты")
        if rule.get("grid_columns"):
            if not all(type(column_index) is int and 1 <= column_index <= snapshot["column_count"]
                       for column_index in rule["grid_columns"]):
                errors.append("Некорректные grid_columns")
            present.add("Постов")
        valid = {
            "Дата": {"date", "serial_date"},
            "Время": {"time", "serial_time"},
            "Начало": {"time", "serial_time"},
            "Конец": {"time", "serial_time"},
        }
        for field, transform in rule.get("transforms", {}).items():
            if field not in valid or not isinstance(transform, str) or not (
                transform in valid[field]
                or (field == "Дата" and transform.startswith("date:") and len(transform) > 5)
            ):
                errors.append("Некорректное преобразование даты/времени")
        required = set(READ_REQUIRED.get(rule["role"], ()))
        if rule["role"] == "slots":
            if rule.get("schedule_mode") == "hours":
                required = set(SCHEDULE_REQUIRED)
            elif rule.get("grid_columns"):
                required = {"Время"}
            else:
                required = {"Дата", "Время", "Постов"}
        if rule["role"] == "tariffs":
            required -= {"Состав"} if rule.get("price_fields") else set()
        missing = required - present
        if missing:
            errors.append("Не хватает полей: " + ", ".join(sorted(missing)))
        if rule["role"] == "services" and not rule.get("price_fields") and not rule.get("pivot_prices") \
                and not rule.get("prices_transposed"):
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
        if any(merge.get("endRowIndex", 0) > header - 1
               and merge.get("startRowIndex", 0) < (end_row or snapshot["row_count"])
               for merge in snapshot.get("merges") or []):
            errors.append("Объединённые ячейки пересекают область")
        header_values = snapshot["values"][header - 1] if header <= len(snapshot["values"]) else []
        raw_headers = snapshot["raw_values"][header - 1] if header <= len(snapshot.get("raw_values") or []) else []
        formula_headers = {column_index for row_index, column_index in snapshot.get("formulas") or []
                           if row_index == header}
        if any(
            str(value_at([header_values], 1, column_index)).strip()
            != str(value_at([raw_headers], 1, column_index)).strip()
            for column_index in range(1, max(len(header_values), len(raw_headers)) + 1)
            if column_index not in formula_headers
        ):
            errors.append("Шапка изменилась во время чтения")
        unresolved = set(rule.get("unresolved_fields", []))
        if unresolved and (rule["role"] in {"services", "tariffs"}
                           or unresolved & READ_REQUIRED.get(rule["role"], set())):
            errors.append("Не подтверждены зависимости изменённых колонок")
        if rule.get("missing_fields"):
            errors.append("Ранее существовавшие поля исчезли: " + ", ".join(rule["missing_fields"]))
        if rule.get("duplicate_price"):
            errors.append("Дублирующиеся колонки цен")
        return formula_headers

    def _rule_write_issues(self, rule: dict, snapshot: dict, errors: list[str],
                           present: set[str], formula_headers: set[int]) -> list[str]:
        columns = rule["columns"]
        first, end_row = rule["first_data_row"], rule.get("end_row")
        unresolved = set(rule.get("unresolved_fields", []))
        write_errors = list(errors)
        if unresolved:
            write_errors.append("Требуется ручное сопоставление изменённых полей")
        if formula_headers & set(columns.values()):
            write_errors.append("Формулы в шапке записи")
        if rule["role"] == "slots" and rule.get("grid_columns"):
            if any(row_index >= first and (end_row is None or row_index <= end_row)
                   and column_index in set(rule["grid_columns"]) | {columns.get("Время")}
                   for row_index, column_index in snapshot.get("formulas") or []):
                write_errors.append("Формулы в сетке постов: запись заблокирована")
        elif rule["role"] != "bookings":
            write_errors.append("Область доступна только для чтения")
        if rule["role"] == "bookings" and BOOKING_WRITE_REQUIRED - present:
            write_errors.append("Для создания записей нужны все поля записи")
        if rule["role"] == "bookings" and self.config.car_plate_required and "Госномер" not in columns:
            write_errors.append("Обязательный госномер не имеет подтверждённой колонки")
        # Формулы в данных записи не блокируют создание новой строки и адресные
        # операции: cancel/reschedule проверяют конкретную строку перед изменением.
        # Блокируется только шапка (выше) и занятая формулами клетка сетки.
        statuses = rule.get("write_statuses", {})
        status_map = {normalize_header(field_name): field_value
                      for field_name, field_value in rule.get("status_map", {}).items()}
        if rule["role"] == "bookings" and (
            status_map.get(normalize_header(statuses.get("new", ""))) != "Новая"
            or status_map.get(normalize_header(statuses.get("cancelled", ""))) != "Отмена"
        ):
            write_errors.append("Не подтверждены значения статусов записи/отмены")
        return write_errors

    def _validate_document(self, document: dict, snapshots: list[dict]) -> dict:
        by_id = {snapshot["id"]: snapshot for snapshot in snapshots}
        for rule in document.get("regions", []):
            snapshot = by_id.get(rule.get("sheet_id"))
            if snapshot is None:
                rule["validation"] = {"read": False, "write": False,
                                      "issues": ["Лист удалён"], "write_issues": ["Лист удалён"]}
                continue
            rule["structure"] = structure(snapshot, rule)
            rule["fingerprint"] = fingerprint(rule["structure"])
            self._validate_rule(rule, snapshot)
        self._validate_relationships(document, snapshots)
        self._summarize(document)
        return document

    def _validate_relationships(self, document: dict, snapshots: list[dict]) -> None:
        issues = document.setdefault("issues", [])
        by_id = {snapshot["id"]: snapshot for snapshot in snapshots}
        service_names: set[str] = set()
        services_rule = next((rule for rule in document["regions"] if rule["role"] == "services"), None)
        if services_rule:
            snapshot = by_id.get(services_rule["sheet_id"])
            if snapshot and services_rule["validation"].get("read"):
                try:
                    records = self._extract_records(services_rule, snapshot["values"], snapshot["title"])
                    service_names = {str(record.get("Услуга", "")).strip().casefold()
                                     for record in records if str(record.get("Услуга", "")).strip()}
                except MappingError as error:
                    issues.append(f"Услуги: {error}")
        for rule in document["regions"]:
            if rule["role"] != "tariffs" or not rule["validation"].get("read"):
                continue
            snapshot = by_id.get(rule["sheet_id"])
            if not snapshot:
                continue
            try:
                records = self._extract_records(rule, snapshot["values"], snapshot["title"])
            except MappingError as error:
                issues.append(f"Тарифы: {error}")
                rule["validation"]["read"] = False
                continue
            missing = set()
            for record in records:
                composition = str(record.get("Состав", ""))
                for item in re.split(r"[,;\n]", composition):
                    name = item.strip().casefold()
                    if name and name not in service_names:
                        missing.add(item.strip())
            if missing:
                message = "Тарифы ссылаются на отсутствующие услуги: " + ", ".join(sorted(missing))
                issues.append(message)
                rule["validation"]["issues"].append(message)
                # Диагностируем несоответствие, но не блокируем весь прайс:
                # существующая бизнес-логика исключает неизвестные позиции из состава.
        document["relationships"] = [
            {"from": "bookings", "to": "slots", "fields": ["Дата", "Время"]},
            {"from": "tariffs", "to": "services", "fields": ["Состав", "Услуга"]},
        ]

    def _summarize(self, document: dict | None = None) -> None:
        document = document or self.document
        if not document:
            return
        regions = document.get("regions", [])
        present = {rule["role"] for rule in regions}
        document["missing_roles"] = sorted(set(ROLE_FIELDS) - present)
        document["required_missing"] = sorted(CORE_ROLES - present)
        ambiguous = []
        for role in set(ROLE_FIELDS):
            group = [rule for rule in regions if rule["role"] == role]
            if len(group) > 1 and role != "services" and not all(
                rule.get("date_source") or rule.get("grid_columns") for rule in group
            ):
                ambiguous.append(role)
        document["ambiguous_roles"] = ambiguous
        all_read = all(rule["validation"].get("read") for rule in regions
                       if rule["role"] in set(ROLE_FIELDS) - {"archive"})
        valid = not document.get("required_missing") and not ambiguous and all_read
        if document.get("stale"):
            valid = False
        document["validation_status"] = "valid" if valid else "needs_review"
        booking_rules = [rule for rule in regions if rule["role"] == "bookings"]
        document["writes_enabled"] = bool(
            valid and booking_rules and all(rule["validation"].get("write") for rule in booking_rules)
            and not document.get("stale")
        )

    def _discover(self, snapshots: list[dict], reason: str) -> dict:
        import schema_ai
        compact = schema_ai.compact_workbook(snapshots, self.named_ranges)
        issues: list[str] = []
        began = time.time()
        candidates: list[dict] = []
        call_meta: dict = {}
        recovery_reason = ""
        if getattr(self.config, "ai_enabled", True) and getattr(self.config, "ai_api_key", ""):
            try:
                document = schema_ai.request_mapping(self.config, compact)
                self.ai_requests += 1
                call_meta = dict(getattr(schema_ai, "LAST_CALL", {}) or {})
                call_meta.setdefault("duration_sec", round(time.time() - began, 3))
                candidates, model_issues = schema_ai.normalize_ai_document(document)
                issues.extend(model_issues)
            except Exception as error:
                recovery_reason = _safe_error(error)
                issues.append("AI-разметка недоступна: " + recovery_reason)
                log.error("AI-разметка Sheets не выполнена: %s; пробую проверенное "
                          "детерминированное восстановление", recovery_reason)
        else:
            recovery_reason = "AI-провайдер не настроен"
            issues.append(recovery_reason)
        by_id = {snapshot["id"]: snapshot for snapshot in snapshots}
        by_title = {snapshot["title"].casefold(): snapshot for snapshot in snapshots}
        regions = []
        covered = set()
        for candidate in candidates:
            snapshot = by_id.get(candidate.get("sheet_id")) or by_title.get(candidate.get("sheet_title", "").casefold())
            if snapshot is None:
                issues.append(f"Модель: лист «{candidate.get('sheet_title')}» не найден")
                continue
            key = (candidate["role"], snapshot["id"], candidate.get("header_row") or 1)
            if key in covered:
                continue
            rule = self._candidate_rule(candidate, by_id, by_title, issues)
            if rule is None:
                continue
            regions.append(rule)
            covered.add(key)
        if not regions or CORE_ROLES - {rule["role"] for rule in regions}:
            # Неполный ответ или отказ провайдера: детерминированное восстановление
            # только по фактическим шапкам и данным. Координаты всё равно проходят
            # полную проверку ниже и никогда не пишутся «на глаз».
            self._salvage_missing_roles(regions, snapshots, issues,
                                        CORE_ROLES | {"tariffs", "durations", "classes",
                                                      "resources", "archive"})
        if not regions:
            raise MappingError("Не удалось подтвердить ни одной области книги; карта не активирована")
        self._salvage_missing_roles(regions, snapshots, issues,
                                    CORE_ROLES | {"tariffs", "durations", "classes",
                                                  "resources", "archive"})
        # Несколько журналов записей без разделителя даты — неоднозначная
        # структура. Добавляем второй кандидат, чтобы validation_status
        # заблокировал запись, а не выбирал лист/таблицу наугад.
        booking_keys = {(rule["sheet_id"], rule.get("header_row"))
                        for rule in regions if rule["role"] == "bookings"}
        for snapshot in snapshots:
            if any(token in normalize_header(snapshot["title"])
                   for token in ("архив", "archive")):
                continue
            for row_number in range(1, min(len(snapshot["values"]), MAX_HEADER_SCAN_ROWS) + 1):
                if (snapshot["id"], row_number) in booking_keys:
                    continue
                headers = trim_headers(snapshot["values"][row_number - 1])
                columns, errors = header_mapping(headers, "bookings")
                if not errors and {"ID", "Дата", "Время", "Telegram ID", "Статус"} <= set(columns) \
                        and not title_date(snapshot["title"]):
                    extra = {
                        "role": "bookings", "sheet_id": snapshot["id"], "sheet_title": snapshot["title"],
                        "header_row": row_number, "first_data_row": row_number + 1, "end_row": None,
                        "columns": columns, "status_map": dict(STATUS_MAP),
                        "write_statuses": {"new": "Новая", "cancelled": "Отмена"},
                        "transforms": {"Дата": "date", "Время": "time"},
                        "date_source": None, "orientation": "records",
                        "permissions": {"read": True, "write": True},
                        "validation": {"read": False, "write": False, "issues": []},
                        "source": "ambiguity-check",
                    }
                    self._infer_transforms(extra, snapshot)
                    self._infer_statuses(extra, snapshot, [])
                    extra["structure"] = structure(snapshot, extra)
                    extra["fingerprint"] = fingerprint(extra["structure"])
                    regions.append(extra)
                    booking_keys.add((snapshot["id"], row_number))
                    issues.append(f"Несколько похожих журналов записей: «{snapshot['title']}», "
                                  f"строка {row_number}; запись заблокирована")
        # Второй недатированный слот-лист (например, график работы рядом с
        # таблицей слотов) делает расписание неоднозначным.
        slot_sheets = {rule["sheet_id"] for rule in regions if rule["role"] == "slots"}
        for snapshot in snapshots:
            if snapshot["id"] in slot_sheets or title_date(snapshot["title"]):
                continue
            if any(token in normalize_header(snapshot["title"])
                   for token in ("ограничен", "бокс", "ресурс", "resource")):
                continue
            for row_number in range(1, min(len(snapshot["values"]), MAX_HEADER_SCAN_ROWS) + 1):
                headers = trim_headers(snapshot["values"][row_number - 1])
                if any(normalize_header(header) in {"бокс", "открытие поста", "закрытие поста",
                                                    "ограничение", "статус"}
                       for header in headers if str(header).strip()):
                    continue
                columns, errors = header_mapping(headers, "slots")
                if errors:
                    continue
                if {"Начало", "Конец"} <= set(columns) or {"Дата", "Время", "Постов"} <= set(columns):
                    extra = {
                        "role": "slots", "sheet_id": snapshot["id"], "sheet_title": snapshot["title"],
                        "header_row": row_number, "first_data_row": row_number + 1, "end_row": None,
                        "columns": columns, "status_map": dict(STATUS_MAP),
                        "write_statuses": {"new": "Новая", "cancelled": "Отмена"},
                        "transforms": {"Дата": "date", "Время": "time", "Начало": "time", "Конец": "time"},
                        "date_source": None, "orientation": "records",
                        "schedule_mode": "hours" if {"Начало", "Конец"} <= set(columns) else "slots",
                        "permissions": {"read": True, "write": False},
                        "validation": {"read": False, "write": False, "issues": []},
                        "source": "ambiguity-check",
                    }
                    self._infer_transforms(extra, snapshot)
                    extra["structure"] = structure(snapshot, extra)
                    extra["fingerprint"] = fingerprint(extra["structure"])
                    regions.append(extra)
                    issues.append(f"Несколько расписаний без разделителя даты: «{snapshot['title']}»")
                    break
        previous_regions = (self.document or {}).get("regions", [])
        for rule in regions:
            previous_rule = next((old for old in previous_regions
                                  if old.get("role") == rule["role"]
                                  and old.get("sheet_id") == rule["sheet_id"]), None)
            if previous_rule and isinstance(previous_rule.get("structure"), dict):
                rule["last_changes"] = diff_structure(previous_rule["structure"], rule["structure"])
                missing_fields = sorted(set(previous_rule.get("columns", {})) - set(rule.get("columns", {})))
                if missing_fields:
                    blocking = [field for field in missing_fields
                                if rule["role"] in {"services", "tariffs"}
                                or field in READ_REQUIRED.get(rule["role"], set())
                                or (field == "Госномер" and self.config.car_plate_required)]
                    if blocking:
                        # Удалённая критичная колонка не должна молча менять
                        # цену/состав/владельца записи: блокируем область.
                        rule["missing_fields"] = blocking
                        issues.append(
                            f"Структура «{rule.get('sheet_title')}» изменилась: исчезли поля "
                            + ", ".join(blocking) + "; требуется проверка карты")
                    else:
                        rule["missing_optional_fields"] = missing_fields
                        issues.append(
                            f"Структура «{rule.get('sheet_title')}»: исчезли необязательные поля "
                            + ", ".join(missing_fields))
        # Дубли одной роли без разделителя даты/листа неоднозначны.
        for role in set(ROLE_FIELDS):
            group = [rule for rule in regions if rule["role"] == role]
            if len(group) > 1 and role != "services" and not all(
                rule.get("date_source") or rule.get("grid_columns") for rule in group
            ):
                issues.append(f"Несколько областей {role} без явного разделителя даты/листа")
        previous = self.document or {}
        now = time.time()
        new_document = {
            "version": VERSION,
            "engine_revision": ENGINE_REVISION,
            "spreadsheet_id": self.config.spreadsheet_id,
            "created_at": now,
            "checked_at": now,
            "mapping_version": int(previous.get("mapping_version", 0)) + 1,
            "source": "ai" if not recovery_reason or candidates else "deterministic-recovery",
            "reason": reason,
            "regions": regions,
            "relationships": [],
            "issues": issues,
            "named_ranges": self.named_ranges,
            "ai": dict(call_meta, calls=1 if call_meta else 0,
                       model=getattr(self.config, "sheets_ai_model", "") or self.config.ai_model,
                       fallback_reason=recovery_reason or None),
            "ai_total_calls": int(previous.get("ai_total_calls", 0)) + (1 if call_meta else 0),
            "failure": None,
            "stale": False,
            "last_check": "rebuilt",
        }
        self._validate_document(new_document, snapshots)
        new_document["structural_fingerprint"] = self._structure_signature(snapshots, regions)
        return new_document

    def _rebuild(self, snapshots: list[dict], fingerprint_value: str, *, explicit: bool = False) -> dict:
        previous = self.document
        failure = (previous or {}).get("failure") or {}
        if (not explicit and failure.get("fingerprint") == fingerprint_value
                and failure.get("at")):
            if previous:
                previous["stale"] = True
                previous["checked_at"] = time.time()
                previous["last_check"] = "failed_cached"
                previous["writes_enabled"] = False
                previous["validation_status"] = "needs_review"
                self._save()
                log.warning("Структура Sheets не изменилась после прошлой ошибки карты; "
                            "повторный платный вызов не выполняется. Нужен явный --refresh-schema")
            return previous or {}
        try:
            new_document = self._discover(snapshots, "startup" if previous is None else "structure changed")
        except Exception as error:
            message = _safe_error(error)
            self.last_error = message
            log.error("AI-карта Sheets не построена: %s", message)
            now = time.time()
            if previous is None:
                document = {
                    "version": VERSION,
                    "engine_revision": ENGINE_REVISION,
                    "spreadsheet_id": self.config.spreadsheet_id,
                    "created_at": now,
                    "checked_at": now,
                    "mapping_version": 0,
                    "regions": [],
                    "relationships": [],
                    "issues": [message],
                    "named_ranges": self.named_ranges,
                    "ai": dict(getattr(__import__("schema_ai"), "LAST_CALL", {}) or {}, calls=1),
                    "failure": {"fingerprint": fingerprint_value, "at": now, "error": message,
                                "attempts": int(failure.get("attempts", 0)) + 1},
                    "stale": True,
                    "last_check": "failed",
                    "validation_status": "needs_review",
                    "writes_enabled": False,
                }
            else:
                document = previous
                document["stale"] = True
                document["failure"] = {
                    "fingerprint": fingerprint_value, "at": now, "error": message,
                    "attempts": int(failure.get("attempts", 0)) + 1,
                }
                document["checked_at"] = now
                document["last_check"] = "failed"
                document["validation_status"] = "needs_review"
                document["writes_enabled"] = False
            self.document = document
            self._save()
            return document
        self.document = new_document
        self._save()
        self.force_pending = False
        self.last_error = ""
        log.info("Карта Sheets активирована: %s; областей %d; запись %s",
                 new_document.get("validation_status"), len(new_document["regions"]),
                 "разрешена" if new_document.get("writes_enabled") else "заблокирована")
        return new_document

    def _fill_observed(self, observed: dict, regions: list[dict]) -> None:
        identifiers = sorted({rule["sheet_id"] for rule in regions} - observed.keys())
        if not identifiers:
            return
        ranges = ["'" + self._worksheet(sheet_id).title.replace("'", "''") + "'"
                  for sheet_id in identifiers]
        for offset in range(0, len(ranges), 50):
            group = ranges[offset:offset + 50]
            values = self.spreadsheet().values_batch_get(
                group, params={"valueRenderOption": "FORMATTED_VALUE"}).get("valueRanges", [])
            if len(values) != len(group):
                raise MappingError("Google вернул неполный снимок таблиц")
            observed.update({
                sheet_id: {"values": value.get("values", [])}
                for sheet_id, value in zip(identifiers[offset:offset + 50], values)
            })

    def refresh(self, force: bool = False, validate: bool = False,
                _observed: dict | None = None, explicit: bool = False) -> dict:
        """Проверить структуру и при необходимости перестроить карту.

        force=True — выполнить структурную проверку сейчас (AI только при реальном
        изменении). explicit=True — разрешить платный повтор после прошлой ошибки
        (ручной --refresh-schema). validate=True — только детерминированная проверка.
        """
        with self.lock:
            self._load()
            now = time.time()
            if self.document is None:
                snapshots = self._snapshots()
                if _observed is not None:
                    _observed.update({snapshot["id"]: snapshot for snapshot in snapshots})
                return self._rebuild(snapshots, self._structure_signature(snapshots, []), explicit=True)
            document = self.document
            interval = float(getattr(self.config, "sheets_schema_check_interval_hours", 6)) * 3600
            age = now - document.get("checked_at", 0)
            if not (force or validate or self.force_pending or age < 0 or age >= interval):
                return document
            if validate:
                snapshots = self._snapshots()
                if _observed is not None:
                    _observed.update({snapshot["id"]: snapshot for snapshot in snapshots})
                self._validate_document(document, snapshots)
                document["checked_at"] = now
                document["last_check"] = "validated"
                document["structural_fingerprint"] = self._structure_signature(
                    snapshots, document.get("regions", []))
                self._save()
                return document
            probe = self._probe_snapshots()
            current = self._structure_signature(probe, document.get("regions", []))
            if current == document.get("structural_fingerprint"):
                document["checked_at"] = now
                document["last_check"] = "unchanged"
                document["last_check_at"] = now
                if document.get("stale"):
                    # Структура вернулась к сохранённой карте: разрешаем чтение,
                    # но запись только после полной проверки данных.
                    snapshots = self._snapshots()
                    self._validate_document(document, snapshots)
                    document["stale"] = False
                    document["failure"] = None
                    self._summarize(document)
                self._save()
                if _observed is not None:
                    try:
                        self._fill_observed(_observed, document.get("regions", []))
                    except Exception:
                        pass
                return document
            snapshots = self._snapshots()
            if _observed is not None:
                _observed.update({snapshot["id"]: snapshot for snapshot in snapshots})
            current = self._structure_signature(snapshots, document.get("regions", []))
            return self._rebuild(snapshots, current, explicit=explicit)

    def check_structure(self, explicit: bool = False) -> dict:
        """Публичная точка для фоновой проверки каждые 6 часов."""
        return self.refresh(force=True, explicit=explicit)

    def _rules(self, role: str) -> list[dict]:
        self.refresh()
        if self.document is None:
            raise MappingError("Карта Sheets не загружена; выполните AI-разметку")
        rules = [rule for rule in self.document["regions"] if rule["role"] == role]
        if not rules:
            raise MappingError(f"Нет подтверждённой области {role}; проверьте карту Sheets")
        if len(rules) > 1 and role != "services" and not all(
            rule.get("date_source") or rule.get("grid_columns") for rule in rules
        ):
            raise MappingError(f"Несколько областей {role}; удалите лишние правила или задайте date_source")
        if not all(rule["validation"].get("read") for rule in rules):
            raise MappingError(f"Чтение {role} заблокировано: требуется проверка карты Sheets")
        return rules

    # ---------- обслуживание журнала по физическим адресам ----------

    def sort_bookings(self, sort: bool = False) -> None:
        """Хронологическая сортировка и перенумерация ID по карте, без имён шапок."""
        from sheets import parse_date, parse_time
        with self.lock:
            try:
                self.refresh(force=True)
                rules = self._rules("bookings")
            except MappingError:
                return
            if len(rules) != 1:
                log.info("Сортировка пропущена: несколько журналов записей")
                return
            rule = rules[0]
            worksheet = self._worksheet(rule["sheet_id"])
            if not hasattr(worksheet, "update"):
                return
            values = worksheet.get_all_values(value_render_option="FORMULA",
                                              date_time_render_option="FORMATTED_STRING")
            header_index = rule["header_row"] - 1
            first = rule["first_data_row"]
            if len(values) <= header_index:
                return
            headers = trim_headers(values[header_index])
            if headers != rule["structure"]["headers"]:
                raise MappingError("Шапка журнала изменилась; сортировка заблокирована")
            if any(str(cell).startswith("=") for row in values[first - 1:] for cell in row):
                log.warning("«Записи»: формулы в данных — сортировка/перенумерация пропущены")
                return
            body = values[first - 1:]
            if sort:
                date_column = rule["columns"].get("Дата")
                time_column = rule["columns"].get("Время")
                if not date_column or not time_column:
                    return
                def order_key(row: list):
                    raw_date = str(value_at([row], 1, date_column)).strip()
                    raw_time = str(value_at([row], 1, time_column)).strip()
                    return (parse_date(raw_date) or datetime(9999, 12, 31).date(),
                            parse_time(raw_time) or "99:99")
                ordered = sorted(body, key=order_key)
                if ordered != body:
                    width = max(len(headers), max(rule["columns"].values()))
                    grid = [(list(row) + [""] * width)[:width] for row in ordered]
                    worksheet.update(f"A{first}:{cell_name(first + len(grid) - 1, width)}", grid,
                                     value_input_option="RAW")
            if sort:
                self.renumber_bookings(rule["sheet_id"])

    def renumber_bookings(self, sheet_id: int) -> int:
        """ID = 1..N по порядку строк; только в линейном журнале без формул."""
        with self.lock:
            try:
                rule = self._target(sheet_id=sheet_id)
            except MappingError:
                return 0
            if rule.get("grid_columns"):
                return 0  # маркеры сетки завязаны на текущие номера
            worksheet = self._worksheet(sheet_id)
            if not hasattr(worksheet, "update"):
                return 0
            first = rule["first_data_row"]
            values = worksheet.get_all_values(value_render_option="FORMULA",
                                              date_time_render_option="FORMATTED_STRING")
            if len(values) < first or any(str(cell).startswith("=") for row in values[first - 1:] for cell in row):
                return 0
            filled = [row for row in range(first, len(values) + 1)
                      if any(str(cell).strip() for cell in values[row - 1])]
            if not filled:
                return 0
            last = filled[-1]
            if len(filled) != last - first + 1:
                return 0
            id_column = rule["columns"].get("ID")
            if not id_column:
                return 0
            current = [str(value_at(values, row, id_column)).strip()
                       for row in range(first, last + 1)]
            expected = [str(number) for number in range(1, len(filled) + 1)]
            if current == expected:
                return 0
            column = cell_name(1, id_column)[:-1]
            worksheet.update(f"{column}{first}:{column}{last}",
                             [[value] for value in expected],
                             value_input_option="RAW")
            self.read_versions.pop("bookings", None)
            return last

    # ---------- чтение таблиц ----------

    def observe(self, force: bool = False, roles=ROLE_FIELDS) -> dict:
        """Свежий пакет выбранных ролей; кэш между операциями не используется."""
        with self.lock:
            observed = {}
            self.refresh(force=force, _observed=observed)
            rules = []
            for role in roles:
                try:
                    rules.extend(self._rules(role))
                except MappingError:
                    if role in CORE_ROLES:
                        raise
                    # Необязательная область отсутствует — это допустимо.
                    continue
            missing = {rule["sheet_id"] for rule in rules} - observed.keys()
            identifiers = sorted(missing)
            for offset in range(0, len(identifiers), 50):
                group = identifiers[offset:offset + 50]
                ranges = ["'" + self._worksheet(sheet_id).title.replace("'", "''") + "'"
                          for sheet_id in group]
                try:
                    values = self.spreadsheet().values_batch_get(
                        ranges, params={"valueRenderOption": "FORMATTED_VALUE"}).get("valueRanges", [])
                except Exception as error:
                    status = getattr(getattr(error, "response", None), "status_code", None)
                    if force or (status not in {400, 404} and type(error).__name__ != "WorksheetNotFound"):
                        raise
                    return self.observe(force=True, roles=roles)
                if len(values) != len(group):
                    raise MappingError("Google вернул неполный снимок таблиц")
                observed.update({
                    sheet_id: {"values": value.get("values", [])}
                    for sheet_id, value in zip(group, values)
                })
            for rule in rules:
                values = observed[rule["sheet_id"]]["values"]
                headers = trim_headers(values[rule["header_row"] - 1]) if len(values) >= rule["header_row"] else []
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
                    records.extend(self._extract_records(rule, values, worksheet.title))
                    continue
                fields = list(rule["columns"])
                if rule.get("date_source") and "Дата" not in fields:
                    fields.append("Дата")
                if rule.get("grid_columns"):
                    fields.append("Постов")
                headers.extend(field for field in fields if field not in headers)
                records.extend(self._extract_records(rule, values, worksheet.title))
            if role == "services" and any(rule.get("pivot_prices") for rule in rules):
                if not all(rule.get("pivot_prices") == rules[0].get("pivot_prices") for rule in rules):
                    raise MappingError("Распределённый прайс смешивает несовместимые представления")
                headers, records = self._pivot_prices(rules[0], records)
            if role == "services" and any(rule.get("prices_transposed") for rule in rules):
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
                classes = [header for header in headers
                           if header not in {"Услуга", "Описание", "Доступна", "Длительность"}]
                if len({normalize_header(header) for header in classes}) != len(classes):
                    raise MappingError("Повторяющиеся классы автомобилей")
            self._validate_records(role, records, rules)
            self.read_versions[role] = fingerprint((headers, records))
            return headers, records

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
            record_date = parse_date(record.get("Дата", ""))
            record_time = parse_time(record.get("Время", ""))
            if not record_date and not record_time:
                # Частично созданная/пустая строка не занимает место. Полностью
                # игнорировать её нельзя (ID остаётся зарезервированным), но и
                # блокировать чтение всего журнала из-за пустой даты нельзя.
                continue
            if not record_date or not record_time:
                raise MappingError(f"Некорректная дата/время {role}, лист {record['__sheet_id__']}, строка {record['__row__']}")
            if role == "bookings":
                # ID может быть наследственным/пустым/повторным — это разрешается
                # адресно. Telegram ID обязан быть положительным: иначе нельзя
                # безопасно сопоставить запись клиенту.
                if not re.fullmatch(r"[1-9]\d*", str(record.get("Telegram ID", "")).strip()):
                    raise MappingError("Некорректный Telegram ID клиента")
                identifiers.add(str(record.get("ID", "")))
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
            for role in ("services", "tariffs", "slots", "bookings"):
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
            width = max(rule["columns"].values())
            row_values = [""] * width
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
                row_values[column - 1] = value
            updates = [{"range": f"A{row}:{cell_name(row, width)}", "values": [row_values]}]
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
