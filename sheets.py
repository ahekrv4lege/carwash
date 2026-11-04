"""Работа с Google Sheets и вся бизнес-логика: прайс, тарифы, свободные слоты, записи.

Цены, наличие услуг и свободное время считаются здесь, в коде, а не нейросетью —
модель только вызывает функции и пересказывает результат клиенту.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from gspread import HTTPClient
from gspread.exceptions import APIError, WorksheetNotFound
from requests.exceptions import ConnectionError as HTTPConnectionError, Timeout as HTTPTimeout
from google.auth.exceptions import TransportError

from config import Config
from adaptive import (AdaptiveSheets, BOOKING_HEADERS, BOOKING_FIELDS, MappingError, SlotUnavailable, cell_name,
                      is_duration_header, normalize_header, parse_duration_minutes)

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WORK_START_HOUR = 9
WORK_END_HOUR = 19
# Шаг перебора вариантов начала для записи по длительности услуги.
FREE_START_STEP_MIN = 30
WORK_HOURS_HINT = "Мы работаем ежедневно с 9:00 до 19:00. Выберите время в этом интервале."


def to_work_time(value: str) -> str:
    """«5» - это 17:00: утренние часы сдвигаем на вечер, если попадаем в график."""
    parts = str(value).strip().split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return str(value)
    if hour < WORK_START_HOUR:
        hour += 12
    if WORK_START_HOUR <= hour <= WORK_END_HOUR:
        return f"{hour:02d}:{minute:02d}"
    return f"{hour:02d}:{minute:02d}" if len(parts) > 1 else str(value)


def within_work_hours(value: str) -> bool:
    parts = str(value).strip().split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return False
    return WORK_START_HOUR <= hour <= WORK_END_HOUR and 0 <= minute <= 59

# Служебные колонки листа «Услуги». ВСЕ остальные колонки — это классы (габариты) автомобилей.
SERVICE_FIXED_COLS = {"услуга", "описание", "доступна", "длительность"}

# Колонки листа «Записи». Порядок в таблице может быть любым — бот ищет колонки по названию.
BOOKING_FIELDS = list(BOOKING_FIELDS)

# Фоны строк в RGB 0..1: отмена — красноватый, архив и прошедшее — серый, пустой ряд — белый.
CANCEL_COLOR = (0.98, 0.80, 0.80)
ARCHIVE_COLOR = (0.93, 0.93, 0.93)
WHITE_COLOR = (1.0, 1.0, 1.0)


# ───────────────────────────── вспомогательные функции ─────────────────────────────

def is_yes(value: Any, default: bool = True) -> bool:
    """«да / нет», TRUE / FALSE (чекбоксы), 1 / 0. Пустая ячейка = default."""
    normalized_value = str(value).strip().lower()
    if not normalized_value:
        return default
    if normalized_value in {"да", "yes", "true", "1", "y", "+", "✓", "✅", "истина", "есть"}:
        return True
    if normalized_value in {"нет", "no", "false", "0", "n", "-", "❌", "ложь"} or normalized_value.startswith(("нет", "не ")):
        return False
    return default


def to_number(value: Any) -> float | None:
    text = re.sub(r"[^\d,.\-]", "", str(value))
    text = text.replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


_MONTH_NAMES = (
    ("январ", "янв"), ("феврал", "фев"), ("март", "мар"), ("апрел", "апр"),
    ("ма[йя]", "ма[йя]"), ("июн", "июн"), ("июл", "июл"), ("август", "авг"),
    ("сентябр", "сен"), ("октябр", "окт"), ("ноябр", "ноя"), ("декабр", "дек"),
)


def parse_date(value: Any) -> date | None:
    text = str(value).strip()
    for date_format in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    # Модель и клиенты часто пишут «5 ноября 2026» / «5 ноя»; это та же дата,
    # что и 05.11.2026, и она не должна выпадать из проверки записи.
    normal = text.casefold().replace("ё", "е")
    def month_variants(full, short):
        variants = [rf"{full}\w*", rf"{short}\.?"]
        if len(full) > 4 and "[" not in full:
            variants.extend((rf"{full[:-1]}\.?", rf"{full[:4]}\.?"))
        return "|".join(variants)

    month_pattern = "|".join(month_variants(full, short) for full, short in _MONTH_NAMES)
    natural = re.fullmatch(
        r"(\d{1,2})(?:-?го)?\s+(" + month_pattern + r")"
        r"(?:\s+(\d{4})(?:\s*г(?:ода|\.)?)?)?", normal)
    if natural:
        month = next(index + 1 for index, (full, short) in enumerate(_MONTH_NAMES)
                     if re.fullmatch(month_variants(full, short), natural[2]))
        try:
            return date(int(natural[3]) if natural[3] else datetime.now().year,
                        month, int(natural[1]))
        except ValueError:
            return None
    return None


def parse_time(value: Any) -> str | None:
    """'9:00', '09:00:00', '9.30' -> 'HH:MM'"""
    match = re.match(r"^\s*(\d{1,2})[:.](\d{2})", str(value))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def pick(record: dict, *names: str) -> str:
    """Значение ячейки по названию колонки (без учёта регистра и пробелов по краям)."""
    wanted = {name.lower() for name in names}
    for key, cell_value in record.items():
        if str(key).strip().lower() in wanted:
            return str(cell_value).strip()
    return ""


def digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value))


def header_index(headers: list, *names: str) -> int | None:
    """Позиция колонки по названию (без учёта регистра); None, если колонки нет."""
    wanted = {name.strip().lower() for name in names}
    for index, header in enumerate(headers):
        if str(header).strip().lower() in wanted:
            return index
    return None


def valid_phone(value: Any) -> bool:
    return (isinstance(value, str) and 10 <= len(digits(value)) <= 15 and
            re.fullmatch(r"[+\d\s().\-\u2010-\u2015\u2212\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]+", value) is not None)


def _failure(text: str, **extra: Any) -> dict:
    return {"ok": False, "error": text, **extra}


def _car_key(value: str) -> str:
    return " ".join((value or "").strip().casefold().replace("ё", "е").split())


def _car_tokens(key: str) -> frozenset[str]:
    return frozenset(re.findall(r"\w+(?:[.,]\w+)*", key))


class FreeCapacity(int):
    """Число уже свободных боксов, а не общая вместимость."""


def time_to_minutes(value: str) -> int | None:
    parsed = parse_time(value)
    if not parsed:
        return None
    hour, minute = (int(part) for part in parsed.split(":"))
    return hour * 60 + minute


def minutes_label(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


@dataclass
class DurationSchedule:
    """График работы для записи по длительности услуги.

    «daily» — интервалы и ресурсы для дней без отдельной строки; строка с датой
    переопределяет график на эту дату. Значения берутся из таблицы, не выводятся.
    """

    daily: tuple[tuple[int, int], ...] = ()
    daily_posts: int = 1
    by_date: dict[date, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    posts_by_date: dict[date, int] = field(default_factory=dict)
    capacity_windows: dict[date | None, tuple[tuple[int, int, int | None], ...]] = field(default_factory=dict)

    def intervals(self, day: date) -> tuple[tuple[int, int], ...]:
        return self.by_date.get(day, self.daily)

    def posts(self, day: date) -> int:
        return self.posts_by_date.get(day, self.daily_posts)

    def windows(self, day: date):
        if self.capacity_windows:
            return self.capacity_windows.get(day, self.capacity_windows.get(None, ()))
        return tuple((a, b, self.posts(day)) for a, b in self.intervals(day))


# ───────────────────────────── модель данных ─────────────────────────────

@dataclass
class Service:
    name: str
    description: str
    available: bool
    prices: dict[str, int]          # класс автомобиля -> цена
    duration_minutes: int | None = None  # из таблицы; None — данных нет, не выводим


@dataclass
class Tariff:
    name: str
    items: list[str]                # названия услуг из листа «Услуги»
    discount: float                 # скидка в % за комплекс
    enabled: bool
    duration_minutes: int | None = None  # суммарная длительность пакета, если указана
    fixed_prices: dict[str, int] = field(default_factory=dict)  # класс -> цена пакета


@dataclass
class Catalog:
    size_classes: list[str]
    services: dict[str, Service]    # ключ — название в нижнем регистре
    tariffs: dict[str, Tariff]
    # Пары «автомобиль, класс» из необязательного листа «Классы».
    class_rules: list[tuple[str, str]] = field(default_factory=list)
    # В таблице есть достоверные длительности: запись по длительности возможна.
    durations_present: bool = False

    # ---- классы автомобилей
    def match_class(self, value: str) -> str | None:
        normalized_class = (value or "").strip().lower()
        if not normalized_class:
            return None
        for class_name in self.size_classes:
            if class_name.lower() == normalized_class:
                return class_name
        return None

    def class_rule_for(self, value: str) -> tuple[str, str | None]:
        """("none", None) | ("rule", класс) | ("ambiguous", None).

        Правило совпадает, если все слова названия правила есть в названии автомобиля
        (целыми словами: «X5» не совпадает с «X50»). Точное совпадение и самое
        конкретное правило важнее общего; при разных классах у равных кандидатов — неоднозначно.
        """
        needle = _car_key(value)
        if not needle:
            return "none", None
        tokens = _car_tokens(needle)
        exact = {size_class for car, size_class in self.class_rules if _car_key(car) == needle}
        if exact:
            return ("rule", next(iter(exact))) if len(exact) == 1 else ("ambiguous", None)
        best_size, best_classes = 0, set()
        for car, size_class in self.class_rules:
            rule_tokens = _car_tokens(_car_key(car))
            if not rule_tokens or not rule_tokens <= tokens:
                continue
            if len(rule_tokens) > best_size:
                best_size, best_classes = len(rule_tokens), {size_class}
            elif len(rule_tokens) == best_size:
                best_classes.add(size_class)
        if not best_classes:
            return "none", None
        return ("rule", next(iter(best_classes))) if len(best_classes) == 1 else ("ambiguous", None)

    def resolve_class_rule(self, value: str) -> str | None:
        status, size_class = self.class_rule_for(value)
        return size_class if status == "rule" else None

    def class_rule_problem(self, car: str | None, size_class: str) -> dict | None:
        """Класс из прайса для модели автомобиля обязателен: модель и клиент его не меняют."""
        if not car:
            return None
        status, required = self.class_rule_for(car)
        if status == "ambiguous":
            return _failure("По правилам прайса этот автомобиль подходит к нескольким классам. "
                            "Уточните модель и кузов клиента.", code="class_rule_ambiguous")
        if status == "rule" and required != size_class:
            return _failure(f"По правилам прайса для этого автомобиля класс «{required}». "
                            f"Используйте его и не меняйте класс по просьбе клиента.",
                            code="class_rule_mismatch", required_class=required)
        return None

    # ---- тариф: фиксированная цена пакета или сумма цен доступных услуг минус скидка
    class TariffConflict(ValueError):
        """Цена пакета противоречит расчёту по составу: источник неоднозначен."""

    def tariff_quote(self, tariff: Tariff, size_class: str) -> dict | None:
        included: list[str] = []
        removed: list[str] = []
        total = 0
        for item in tariff.items:
            service = self.services.get(item.lower())
            price = service.prices.get(size_class) if service else None
            if service and service.available and price is not None:
                included.append(service.name)
                total += price
            else:
                removed.append(service.name if service else item)
        fixed = tariff.fixed_prices.get(size_class)
        discount = min(max(tariff.discount, 0.0), 100.0)
        if fixed is None:
            if not included:
                return None  # нет ни цены пакета, ни доступного состава
            price = int(round(total * (1 - discount / 100)))
        else:
            price = fixed
            if included and price != int(round(total * (1 - discount / 100))):
                raise self.TariffConflict(
                    f"Тариф «{tariff.name}»: цена пакета {fixed} не совпадает с расчётом по составу "
                    f"({int(round(total * (1 - discount / 100)))}); уточните у администратора")
        return {"price": price, "included": included, "removed": removed}

    # ---- расчёт стоимости: тариф и/или отдельные услуги
    def quote(self, size_class: str, tariff: str | None, services: list[str] | None) -> dict:
        lines: list[dict] = []
        total = 0
        covered: set[str] = set()
        tariff_name = None

        if tariff and tariff.strip():
            selected_tariff = self.tariffs.get(tariff.strip().lower())
            if not selected_tariff or not selected_tariff.enabled:
                result = self._catalog_error(f"Тариф «{tariff}» не найден или сейчас недоступен")
                if tariff.strip().lower() in self.services:
                    result.update(field="tariff", code="service_is_not_tariff",
                                  clarification="Это отдельная услуга; уточните выбор, не подменяйте тариф автоматически")
                return result
            try:
                tariff_price = self.tariff_quote(selected_tariff, size_class)
            except self.TariffConflict as error:
                return self._catalog_error(str(error))
            if not tariff_price:
                return self._catalog_error(f"Тариф «{selected_tariff.name}» сейчас недоступен: все его позиции убраны")
            tariff_name = selected_tariff.name
            lines.append({
                "item": f"Тариф «{selected_tariff.name}»",
                "includes": tariff_price["included"],
                "not_included_now": tariff_price["removed"],
                "price": tariff_price["price"],
            })
            total += tariff_price["price"]
            covered = {item.lower() for item in tariff_price["included"]}

        extras: list[str] = []
        for raw in services or []:
            service = self.services.get(str(raw).strip().lower())
            if not service:
                result = self._catalog_error(f"Услуга «{raw}» не найдена в прайсе")
                if str(raw).strip().lower() in self.tariffs:
                    result.update(field="services", code="tariff_is_not_service")
                return result
            if not service.available:
                return self._catalog_error(f"Услуга «{service.name}» сейчас недоступна")
            price = service.prices.get(size_class)
            if price is None:
                return self._catalog_error(f"Услуга «{service.name}» не оказывается для класса «{size_class}»")
            if service.name.lower() in covered or service.name in extras:
                continue  # уже входит в тариф / дубль
            extras.append(service.name)
            lines.append({"item": service.name, "price": price})
            total += price

        if not lines:
            return self._catalog_error("Не выбран ни тариф, ни услуги")
        result = {
            "ok": True,
            "size_class": size_class,
            "tariff": tariff_name,
            "extras": extras,
            "lines": lines,
            "total": int(round(total)),
        }
        if self.durations_present:
            duration, missing = self._duration_for(tariff_name, covered, extras)
            if duration is not None and not missing:
                result["duration_minutes"] = duration
            if missing:
                result["duration_missing"] = missing
        return result

    def _duration_for(self, tariff_name: str | None, covered: set[str], extras: list[str]) -> tuple[int | None, list[str]]:
        """Длительность заказа: пакет целиком плюс доп. услуги, иначе сумма услуг.

        Нет достоверного значения хотя бы для одной позиции — «missing», ничего не выводим.
        """
        missing: list[str] = []
        total = 0
        if tariff_name:
            selected = self.tariffs.get(tariff_name.lower())
            if selected and selected.duration_minutes is not None:
                total += selected.duration_minutes
            else:
                for item in sorted(covered):
                    service = self.services.get(item)
                    if service is None or service.duration_minutes is None:
                        missing.append(service.name if service else item)
                    else:
                        total += service.duration_minutes
        for name in extras:
            service = self.services.get(name.lower())
            if service is None or service.duration_minutes is None:
                missing.append(service.name if service else name)
            else:
                total += service.duration_minutes
        return (total if total and not missing else None), missing

    def shortest_duration(self) -> int | None:
        """Минимальная известная длительность — оценка «а куда вообще можно успеть»."""
        known = [service.duration_minutes for service in self.services.values() if service.duration_minutes]
        known += [tariff.duration_minutes for tariff in self.tariffs.values() if tariff.duration_minutes]
        return min(known) if known else None

    def duration_for(self, tariff: str | None, services: list[str] | None) -> int | None:
        """Длительность выбранного заказа или None, если данных недостаточно."""
        if not self.durations_present:
            return None
        duration, missing = self._duration_for((tariff or "").strip() or None, set(), list(services or []))
        return duration if not missing else None

    def _catalog_error(self, text: str) -> dict:
        return _failure(
            text,
            available_tariffs=[tariff.name for tariff in self.tariffs.values() if tariff.enabled],
            available_services=[service.name for service in self.services.values() if service.available],
        )

    # ---- прайс для нейросети
    def price_list(self) -> dict:
        services = [
            {"name": service.name, "description": service.description, "prices": service.prices}
            for service in self.services.values() if service.available
        ]
        unavailable = [service.name for service in self.services.values() if not service.available]

        tariffs: list[dict] = []
        unavailable_tariffs: list[str] = []
        for tariff in self.tariffs.values():
            if not tariff.enabled:
                continue
            prices: dict[str, int] = {}
            removed: dict[str, list[str]] = {}
            for size_class in self.size_classes:
                try:
                    tariff_price = self.tariff_quote(tariff, size_class)
                except self.TariffConflict:
                    raise
                if tariff_price:
                    prices[size_class] = tariff_price["price"]
                    if tariff_price["removed"]:
                        removed[size_class] = tariff_price["removed"]
            if not prices:
                unavailable_tariffs.append(tariff.name)
                continue
            entry: dict[str, Any] = {
                "name": tariff.name,
                "composition": tariff.items,
                "discount_percent": tariff.discount,
                "prices": prices,
            }
            if removed:
                vals = list(removed.values())
                same = len(removed) == len(prices) and all(field_value == vals[0] for field_value in vals)
                entry["not_included_now"] = vals[0] if same else removed
                entry["note"] = (
                    "Часть позиций сейчас убрана администратором — цена уже пересчитана без них. "
                    "Обязательно сообщи об этом клиенту."
                )
            tariffs.append(entry)

        return {
            "ok": True,
            "size_classes": self.size_classes,
            "class_rules": [{"car": car, "class": size_class} for car, size_class in self.class_rules],
            "services": services,
            "tariffs": tariffs,
            "unavailable_services": unavailable,
            "unavailable_tariffs": unavailable_tariffs,
        }


# ───────────────────────────── хранилище (Google Sheets) ─────────────────────────────

class SheetsHTTPClient(HTTPClient):
    """Повторяем только чтение; неизвестный результат записи не повторяется."""
    def request(self, method, *arguments, **options):
        for attempt in range(3):
            try:
                return super().request(method, *arguments, **options)
            except APIError as error:
                retryable = error.code in {408, 429} or error.code >= 500
                if method.lower() != "get" or not retryable or attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
            except (HTTPConnectionError, HTTPTimeout, TransportError):
                if method.lower() != "get" or attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))


class Store:
    """Все методы синхронные — из asyncio вызываются через asyncio.to_thread."""

    def __init__(self, config: Config):
        self.config = config
        self.timezone = ZoneInfo(config.timezone)
        self._lock = threading.RLock()
        from coordination import BookingLock
        self._write_lock = BookingLock(config.booking_coordination_file)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._spreadsheet = None  # gspread.Spreadsheet, подключаемся лениво
        # Единственный backend: AI-карта + адресные операции по физическим координатам.
        self._adaptive = AdaptiveSheets(config, self._get_spreadsheet)
        self._observed = None
        self._class_rules_absent = False  # лист «Классы» отсутствует: не перечитываем его зря

    def refresh_schema(self, force: bool = True, validate: bool = False, *,
                       explicit: bool = False) -> dict:
        with self._lock:
            self._class_rules_absent = False
            document = self._adaptive.refresh(force=force, validate=validate, explicit=explicit)
            self._invalidate("catalog", "slots", "bookings", "bays")
            return document

    def check_schema(self, *, explicit: bool = False) -> dict:
        """Фоновая структурная проверка; AI только при реальном изменении."""
        with self._lock:
            document = self._adaptive.check_structure(explicit=explicit)
            self._invalidate("catalog", "slots", "bookings", "bays")
            return document

    # ---------- низкий уровень ----------
    def _get_spreadsheet(self):
        if self._spreadsheet is None:
            import gspread

            google_client = gspread.service_account(
                filename=self.config.google_credentials_file, scopes=SCOPES, http_client=SheetsHTTPClient
            )
            google_client.set_timeout((10, 30))
            self._spreadsheet = google_client.open_by_key(self.config.spreadsheet_id)
        return self._spreadsheet

    def _table(self, title: str, observed=None) -> tuple[list[str], list[dict[str, Any]]]:
        """Роль карты -> (канонические заголовки, строки-словари)."""
        roles = {self.config.sheet_services: "services", self.config.sheet_tariffs: "tariffs",
                 self.config.sheet_slots: "slots", self.config.sheet_bookings: "bookings",
                 self.config.sheet_durations: "durations", self.config.sheet_classes: "classes",
                 self.config.sheet_archive: "archive", self.config.sheet_resources: "resources"}
        if title not in roles:
            raise MappingError(f"Неизвестная роль листа для «{title}»")
        return self._adaptive.table(roles[title],
                                    _observed=self._observed if observed is None else observed)

    @contextmanager
    def fresh_tables(self):
        """Один свежий пакет для расчёта; финальный write-preflight остаётся независимым."""
        with self._write_lock, self._lock:
            previous = self._observed
            self._invalidate("catalog", "slots", "bookings", "bays")
            try:
                self._observed = self._adaptive.observe()
                yield
            except BaseException:
                self._invalidate("catalog", "slots", "bookings", "bays")
                raise
            finally:
                self._observed = previous

    def _cached(self, key: str, cache_seconds: int, loader: Callable[[], Any]) -> Any:
        with self._lock:
            now = time.monotonic()
            cached_value = self._cache.get(key)
            if cached_value and now - cached_value[0] < cache_seconds:
                return cached_value[1]
            value = loader()
            self._cache[key] = (now, value)
            return value

    def _invalidate(self, *keys: str) -> None:
        for field_name in keys:
            self._cache.pop(field_name, None)

    # ---------- время ----------
    def now(self) -> datetime:
        return datetime.now(self.timezone)

    def _slot_datetime(self, booking_date: date, booking_time: str) -> datetime:
        hour, minute = (int(part) for part in booking_time.split(":"))
        return datetime(booking_date.year, booking_date.month, booking_date.day, hour, minute, tzinfo=self.timezone)

    def _is_future(self, booking_date: date, booking_time: str) -> bool:
        return self._slot_datetime(booking_date, booking_time) >= self.now() + timedelta(minutes=self.config.min_lead_minutes)

    # ---------- прайс и тарифы ----------
    def get_catalog(self, fresh: bool = False) -> Catalog:
        with self._lock:
            if fresh:
                self._invalidate("catalog")
            return self._cached("catalog", self.config.catalog_cache_sec, self._load_catalog)

    def _load_catalog(self) -> Catalog:
        observed = self._observed
        if self._adaptive and observed is None:
            try:
                observed = self._adaptive.observe(roles=("services", "tariffs"))
            except MappingError as error:
                # Тарифов в книге может не быть вовсе — тогда цена считается по услугам.
                if "Нет подтверждённой области tariffs" not in str(error):
                    raise
                issues = (self._adaptive.document or {}).get("issues", [])
                if any("tariffs" in issue for issue in issues):
                    raise  # лист тарифов есть, но не распознан: молча терять его нельзя
                observed = self._adaptive.observe(roles=("services",))
        headers, rows = self._table(self.config.sheet_services, observed)
        service_duration_header = next((header for header in headers if is_duration_header(header)), None)
        # Классы без повторов: две колонки с одинаковым названием (даже в
        # разном регистре) не должны удваивать выбор в прайсе и подсказках.
        classes: list[str] = []
        seen_classes: set[str] = set()
        for header in headers:
            label = str(header).strip()
            normalized = normalize_header(label)
            if normalized and normalized not in SERVICE_FIXED_COLS and normalized not in seen_classes:
                seen_classes.add(normalized)
                classes.append(label)

        services: dict[str, Service] = {}
        for record in rows:
            name = pick(record, "Услуга")
            if not name:
                continue
            prices: dict[str, int] = {}
            for class_name in classes:
                price = to_number(record.get(class_name, ""))
                # Отрицательные и нечисловые значения — испорченные данные:
                # цена для класса не указана, а не ушла в расчёт со знаком минус.
                if price is not None and math.isfinite(price) and price >= 0:
                    prices[class_name] = int(round(price))
            services[name.lower()] = Service(
                name=name,
                description=pick(record, "Описание"),
                available=is_yes(pick(record, "Доступна"), default=True),
                prices=prices,
                duration_minutes=self._duration_value(record.get(service_duration_header, "")) if service_duration_header else None,
            )

        try:
            tariff_headers, tariff_rows = self._table(self.config.sheet_tariffs, observed)
        except MappingError as error:
            if "Нет подтверждённой области" not in str(error):
                raise  # лист тарифов есть, но повреждён: молча терять его нельзя
            issues = (self._adaptive.document or {}).get("issues", [])
            if any("tariffs" in issue for issue in issues):
                raise  # лист тарифов есть, но не распознан
            # Тарифов в книге может не быть: цена считается по услугам, выдумывать пакеты нельзя.
            tariff_headers, tariff_rows = [], []
        tariff_duration_header = next((header for header in tariff_headers if is_duration_header(header)), None)
        class_headers = [header for header in tariff_headers
                         if header not in {"Тариф", "Состав", "Скидка %", "Доступен", "Доступна", "Длительность"}
                         and not is_duration_header(header)]
        tariffs: dict[str, Tariff] = {}
        for record in tariff_rows:
            name = pick(record, "Тариф")
            if not name:
                continue
            items = [item.strip() for item in re.split(r"[,;\n]", pick(record, "Состав")) if item.strip()]
            for item in items:
                if item.lower() not in services:
                    log.warning("Тариф «%s»: услуга «%s» не найдена на листе «%s»",
                                name, item, self.config.sheet_services)
            fixed_prices = {}
            for header in class_headers:
                raw = record.get(header, "")
                if str(raw).strip():
                    price = to_number(raw)
                    if price is None or price < 0:
                        log.warning("Тариф «%s»: некорректная цена пакета «%s» = %r", name, header, raw)
                    else:
                        fixed_prices[header] = int(round(price))
            tariffs[name.lower()] = Tariff(
                name=name,
                items=items,
                discount=to_number(pick(record, "Скидка %", "Скидка")) or 0.0,
                enabled=is_yes(pick(record, "Доступен", "Доступна"), default=True),
                duration_minutes=self._duration_value(record.get(tariff_duration_header, "")) if tariff_duration_header else None,
                fixed_prices=fixed_prices,
            )
        self._apply_duration_overrides(services)
        durations_present = any(service.duration_minutes for service in services.values()) or any(
            tariff.duration_minutes for tariff in tariffs.values())
        return Catalog(classes, services, tariffs, self._load_class_rules(classes), durations_present)

    @staticmethod
    def _duration_value(raw: Any) -> int | None:
        """Минуты из ячейки; нечитаемое значение — «данных нет», а не догадка."""
        text = str(raw or "").strip()
        if not text:
            return None
        minutes = parse_duration_minutes(text)
        if minutes is None:
            log.warning("Не удалось прочитать длительность «%s» — значение пропущено", text)
        return minutes

    def _apply_duration_overrides(self, services: dict[str, Service]) -> None:
        """Лист «Длительности»: «Услуга» + «Длительность». Противоречие отключает значение."""
        for name, minutes in self._duration_overrides().items():
            service = services.get(name)
            if service is None:
                log.warning("Длительность «%s» отброшена: услуги нет в прайсе", name)
                continue
            if service.duration_minutes is not None and service.duration_minutes != minutes:
                log.warning("Противоречивые длительности «%s» (%s и %s) — значение не используется",
                            service.name, service.duration_minutes, minutes)
                services[name] = replace(service, duration_minutes=None)
            else:
                services[name] = replace(service, duration_minutes=minutes)

    def _duration_overrides(self) -> dict[str, int]:
        try:
            _, rows = self._adaptive.table("durations")
        except MappingError:
            return {}  # листа длительностей нет или он не подтверждён: это допустимо
        overrides: dict[str, int] = {}
        for record in rows:
            name = str(pick(record, "Услуга") or "").strip()
            minutes = self._duration_value(pick(record, "Длительность"))
            if name and minutes:
                overrides[name.lower()] = minutes
        return overrides

    # ---------- правила классов (лист «Классы») ----------
    def _load_class_rules(self, classes: list[str]) -> list[tuple[str, str]]:
        """Необязательные правила «модель автомобиля → класс» из карты Sheets."""
        if self._class_rules_absent:
            return []
        try:
            _, records = self._adaptive.table("classes")
        except MappingError:
            self._class_rules_absent = True
            return []  # листа классов может не быть — это допустимо
        by_lower = {class_name.lower(): class_name for class_name in classes}
        pairs: list[tuple[str, str]] = []
        for record in records:
            car = pick(record, "Автомобиль").strip()
            rule_class = pick(record, "Класс").strip()
            if not car or not rule_class:
                continue
            matched = by_lower.get(rule_class.lower())
            if not matched:
                log.warning("Правило «%s → %s» отброшено: класса нет в прайсе", car, rule_class)
                continue
            pairs.append((car, matched))
        variants: dict[str, set[str]] = {}
        for car, matched in pairs:
            variants.setdefault(car.casefold(), set()).add(matched)
        conflicts = {key for key, matched_classes in variants.items() if len(matched_classes) > 1}
        for key in conflicts:
            log.warning("У «%s» противоречивые классы (%s) — правило не используется",
                        key, ", ".join(sorted(variants[key])))
        return [(car, matched) for car, matched in pairs if car.casefold() not in conflicts]

    def price_list(self) -> dict:
        data = self.get_catalog().price_list()
        data["currency"] = self.config.currency
        return data

    def calculate_price(self, size_class: str, tariff: str | None, services: list[str], *,
                        fresh: bool = False, car: str | None = None) -> dict:
        catalog = self.get_catalog(fresh=fresh)
        matched_class = catalog.match_class(size_class)
        if not matched_class:
            return _failure("Неизвестный класс автомобиля", size_classes=catalog.size_classes)
        problem = catalog.class_rule_problem(car, matched_class)
        if problem:
            return problem
        result = catalog.quote(matched_class, tariff, services)
        result["currency"] = self.config.currency
        return result

    # ---------- слоты и записи ----------
    def _load_slots(self) -> dict[tuple[date, str], int] | DurationSchedule:
        _, rows = self._table(self.config.sheet_slots)
        if any("Начало" in record for record in rows):
            if any("Время" in record for record in rows):
                raise MappingError("Смешаны часовая вместимость и график работы; нужна одна раскладка")
            return self._load_duration_schedule(rows)
        if self._get_bays() is not None:
            raise MappingError("Именованные ограничения боксов требуют интервального графика с длительностями")
        slots: dict[tuple[date, str], int] = {}
        for record in rows:
            booking_date, booking_time = parse_date(pick(record, "Дата")), parse_time(pick(record, "Время"))
            if not booking_date or not booking_time:
                continue
            capacity_value = to_number(pick(record, "Постов", "Мест", "Боксов"))
            if capacity_value is None or capacity_value < 0 or not float(capacity_value).is_integer():
                raise MappingError("Не указано корректное количество постов для слота")
            capacity = int(capacity_value)
            key = (booking_date, booking_time)
            free = bool(record.get("__free_capacity__"))
            if key in slots and isinstance(slots[key], FreeCapacity) != free:
                raise MappingError("Для одного времени смешаны сетка боксов и общая вместимость")
            total = slots.get(key, 0) + capacity
            slots[key] = FreeCapacity(total) if free else total
        return slots

    @staticmethod
    def _load_duration_schedule(rows: list[dict[str, Any]]) -> DurationSchedule:
        """График работы: строки «Начало/Конец», опционально «Дата» и «Постов»."""
        daily: list[tuple[int, int]] = []
        daily_posts: int | None = None
        by_date: dict[date, list[tuple[int, int]]] = {}
        posts_by_date: dict[date, int] = {}
        windows = {}
        for record in rows:
            start, end = time_to_minutes(str(record.get("Начало", "")).strip()), time_to_minutes(str(record.get("Конец", "")).strip())
            if start is None and end is None:
                continue
            if start is None or end is None or start >= end:
                raise MappingError("Некорректный интервал графика работы")
            raw = str(record.get("Постов", "")).strip()
            posts = None
            if raw:
                value = to_number(raw)
                if value is None or value < 0 or not float(value).is_integer():
                    raise MappingError("Некорректное количество постов")
                posts = int(value)
            day_text = str(record.get("Дата", "")).strip()
            day = parse_date(day_text) if day_text else None
            if day:
                by_date.setdefault(day, []).append((start, end))
                posts_by_date[day] = max(posts_by_date.get(day, 0), posts or 0)
            else:
                daily_posts = max(daily_posts or 0, posts or 0)
                daily.append((start, end))
            windows.setdefault(day, []).append((start, end, posts))
        if not daily and not by_date:
            raise MappingError("В графике работы нет ни одного интервала")
        for spans in windows.values():
            for i, (a, b, capacity) in enumerate(spans):
                if any(a < d and c < b and capacity != other for c, d, other in spans[i + 1:]):
                    raise MappingError("Противоречивая вместимость пересекающихся интервалов")
        return DurationSchedule(tuple(daily), daily_posts or 0,
                                {day: tuple(spans) for day, spans in by_date.items()}, posts_by_date,
                                {day: tuple(spans) for day, spans in windows.items()})

    # ---------- запись по длительности услуги ----------
    def _booking_intervals(self, rows: list[dict[str, Any]], exclude: dict[str, Any] | None = None) -> tuple[list[tuple[date, int, int]], set[date]]:
        """Занятые интервалы активных записей и дни с записями без длительности."""
        intervals: list[tuple[date, int, int]] = []
        missing: set[date] = set()
        for record in rows:
            if exclude is not None and record is exclude:
                continue
            if not self._is_active(record):
                continue
            booking_date, booking_time = parse_date(pick(record, "Дата")), parse_time(pick(record, "Время"))
            if not booking_date or not booking_time:
                continue
            raw = str(pick(record, "Длительность") or "").strip()
            minutes = parse_duration_minutes(raw) if raw else None
            start = time_to_minutes(booking_time)
            if minutes is None or start is None:
                missing.add(booking_date)
                continue
            intervals.append((booking_date, start, start + minutes))
        return intervals, missing

    def _duration_free_starts(self, schedule: DurationSchedule, day: date,
                              intervals: list[tuple[date, int, int]], duration: int) -> list[int]:
        """Начала, куда услуга целиком помещается в график и лимит ресурсов."""
        same_day = [(start, end) for booked_day, start, end in intervals if booked_day == day]
        candidates: set[int] = set()
        for begin, end in schedule.intervals(day):
            cursor = begin
            while cursor < end:
                candidates.add(cursor)
                cursor += FREE_START_STEP_MIN
            for booked_start, booked_end in same_day:
                if begin <= booked_end < end:
                    candidates.add(booked_end)
        free = []
        for start in sorted(candidates):
            from availability import interval_problem
            if interval_problem(schedule.windows(day), same_day, start, start + duration) is None:
                free.append(start)
        return free

    def _duration_free_times(self, schedule: DurationSchedule, intervals: list[tuple[date, int, int]],
                             missing: set[date], day: date, duration: int) -> list[str]:
        if day in missing:
            return []
        return [minutes_label(start) for start in self._duration_free_starts(schedule, day, intervals, duration)
                if self._is_future(day, minutes_label(start))]

    def _quote_duration(self, quote: dict) -> tuple[int | None, list[str]]:
        return quote.get("duration_minutes"), list(quote.get("duration_missing", []))

    @staticmethod
    def _quote_services(quote):
        return list(dict.fromkeys(name for line in quote.get("lines", [])
                                  for name in line.get("includes", [line["item"]])))

    def _get_bays(self):
        def load():
            try:
                headers, rows = self._adaptive.table("resources")
            except MappingError:
                return None
            from resources import parse_bays
            values = [headers] + [[pick(record, header) for header in headers] for record in rows]
            catalog = self.get_catalog()
            return parse_bays(values, catalog.size_classes, catalog.services)
        return self._cached("bays", self.config.slots_cache_sec, load)

    def _bay_choice(self, rows, day, time_text, duration, size_class=None, services=None, exclude=None):
        bays = self._get_bays()
        if bays is None:
            return None, None
        if not size_class or not services:
            return None, "Для проверки бокса выберите класс автомобиля и услуги"
        from resources import eligible_bays
        start = time_to_minutes(time_text)
        eligible = eligible_bays(bays, day, start, start + duration, size_class, services)
        occupied = set()
        for row in rows:
            if row is exclude or not self._is_active(row) or parse_date(pick(row, "Дата")) != day:
                continue
            begin = time_to_minutes(pick(row, "Время"))
            length = parse_duration_minutes(pick(row, "Длительность"))
            if begin is None or length is None:
                return None, "В записях нет времени или длительности; нужна проверка администратора"
            if begin < start + duration and start < begin + length:
                bay = pick(row, "Бокс")
                if not bay or bay.casefold() not in {b["name"].casefold() for b in bays}:
                    return None, "В пересекающейся записи не указан бокс; нужна проверка администратора"
                occupied.add(bay.casefold())
        selected = next((bay for bay in eligible if bay.casefold() not in occupied), None)
        return selected, None if selected else "Нет доступного бокса с нужными допусками на всю длительность услуги"

    def _check_duration_start(self, schedule: DurationSchedule, rows: list[dict[str, Any]], day: date,
                              time_text: str, duration: int, exclude: dict | None = None,
                              size_class: str | None = None, services: list[str] | None = None) -> str | None:
        """None — старт подходит; иначе причина отказа (без выдуманных правил)."""
        start = time_to_minutes(time_text)
        if start is None:
            return "Неверное время записи"
        intervals, missing = self._booking_intervals(rows, exclude=exclude)
        if day in missing:
            return "В записях за этот день нет длительности; обратитесь к администратору"
        same_day = [(booked_start, booked_end) for booked_day, booked_start, booked_end in intervals if booked_day == day]
        from availability import interval_problem
        problem = interval_problem(schedule.windows(day), same_day, start, start + duration)
        return problem or self._bay_choice(rows, day, time_text, duration, size_class, services, exclude)[1]

    def grid_maintenance(self) -> dict:
        """Сверка сеток постов с записями: устаревшие свои маркеры снимаются,
        спорные и пропавшие фиксируются для администратора. Изменяются только
        собственные маркеры с проверенным ID; записи только читаются."""
        if not self._adaptive:
            return {"checked": 0, "cleared": [], "ambiguous": [], "missing": []}
        with self._write_lock, self._lock:
            self._invalidate("bookings")
            _, rows = self._get_bookings()
            by_id: dict[str, list[dict[str, Any]]] = {}
            for record in rows:
                by_id.setdefault(str(pick(record, "ID")), []).append(record)
            report = self._adaptive.grid_audit(by_id, self._is_active)
            if report["ambiguous"] or report["missing"]:
                log.warning("Сетка постов требует ручной сверки: %s", report["ambiguous"] + report["missing"])
            return report

    def analytics_snapshot(self) -> tuple[list[dict[str, Any]], dict]:
        """Свежие строки «Записей» и архива и вместимость «Слотов» для статистики.

        Отменённые и архивные записи живут в листе «Архив», но метрики периода
        (доля отмен, выполненные) должны считаться как раньше, — строки архива
        добавляются к строкам «Записей».
        """
        with self._lock:
            self._invalidate("bookings", "slots")
            rows = self._get_bookings()[1] + self._archive_rows()
            return rows, self._get_slots()

    def _archive_rows(self) -> list[dict[str, Any]]:
        """Архив из карты, а для вновь созданного служебного листа — по его шапке."""
        try:
            _, rows = self._adaptive.table("archive")
            return rows
        except MappingError:
            pass
        try:
            values = self._get_spreadsheet().worksheet(self.config.sheet_archive).get_all_values()
        except (WorksheetNotFound, StopIteration):
            return []
        except Exception as error:
            log.warning("Архив для статистики не прочитан (%s)", type(error).__name__)
            return []
        headers = [str(header).strip() for header in values[0]] if values else []
        rows: list[dict[str, Any]] = []
        for index, raw in enumerate(values[1:], start=2):
            if not any(str(cell).strip() for cell in raw):
                continue
            record: dict[str, Any] = {
                header: (raw[column_index] if column_index < len(raw) else "")
                for column_index, header in enumerate(headers) if header
            }
            record["__row__"] = index
            rows.append(record)
        return rows

    def _load_bookings(self) -> tuple[list[str], list[dict[str, Any]]]:
        return self._table(self.config.sheet_bookings)

    def _get_slots(self) -> dict[tuple[date, str], int]:
        return self._cached("slots", self.config.slots_cache_sec, self._load_slots)

    def booking_time(self, value, slots=None):
        slots = self._get_slots() if slots is None else slots
        return parse_time(str(value)) if isinstance(slots, DurationSchedule) else parse_time(to_work_time(value))

    def _get_bookings(self) -> tuple[list[str], list[dict[str, Any]]]:
        return self._cached("bookings", self.config.slots_cache_sec, self._load_bookings)

    @staticmethod
    def _is_active(record: dict) -> bool:
        return not pick(record, "Статус").lower().startswith("отмен")

    def _user_booking_position(self, rows: list[dict], telegram_id: int,
                               booking_date: date, booking_time: str, *, exclude: dict | None = None) -> int:
        """Порядковый номер записи среди активных будущих записей клиента — как их видит my_bookings.

        Считается по уже прочитанным строкам, без повторного чтения таблицы.
        """
        new_key = (booking_date.isoformat(), booking_time)
        position = 0
        for record in rows:
            if record is exclude or pick(record, "Telegram ID") != str(telegram_id) or not self._is_active(record):
                continue
            booking_day, booking_hour = parse_date(pick(record, "Дата")), parse_time(pick(record, "Время"))
            if not booking_day or not booking_hour or self._slot_datetime(booking_day, booking_hour) < self.now():
                continue
            if (booking_day.isoformat(), booking_hour) <= new_key:
                position += 1
        return position + 1

    def _booked_capacity(self, rows: list[dict]) -> Counter:
        busy: Counter = Counter()
        for record in rows:
            if not self._is_active(record):
                continue
            booking_date, booking_time = parse_date(pick(record, "Дата")), parse_time(pick(record, "Время"))
            if booking_date and booking_time:
                busy[(booking_date, booking_time)] += 1
        return busy

    @staticmethod
    def _capacity_left(slots: dict, busy: Counter, key: tuple) -> int:
        capacity = slots.get(key, 0)
        if isinstance(capacity, FreeCapacity):
            return int(capacity)  # сетка: уже учитывает занятые клетки
        return capacity - busy.get(key, 0)

    def _free_times(self, requested_date: date, slots: dict, busy: Counter) -> list[str]:
        return sorted(
            slot_time for (slot_date, slot_time) in slots
            if slot_date == requested_date and self._capacity_left(slots, busy, (slot_date, slot_time)) > 0 and self._is_future(slot_date, slot_time)
        )

    def free_slots(self, date_str: str | None = None, days: int = 7, duration_minutes: int | None = None,
                   *, requested_time: str | None = None, size_class: str | None = None,
                   tariff: str | None = None, services: list[str] | None = None) -> dict:
        if date_str:
            booking_date = parse_date(date_str)
            if not booking_date:
                return _failure("Неверная дата, нужен формат ГГГГ-ММ-ДД")
            dates = [booking_date]
        with self._lock:
            previous, now = self._observed, time.monotonic()
            expired = [key for key in ("slots", "bookings") if key not in self._cache
                       or now - self._cache[key][0] >= self.config.slots_cache_sec]
            try:
                if self._adaptive and previous is None and len(expired) == 2:
                    self._observed = self._adaptive.observe(roles=("slots", "bookings"))
                slots = self._get_slots()
                _, booking_rows = self._get_bookings()
            finally:
                self._observed = previous
        if isinstance(slots, DurationSchedule):
            if size_class:
                quote = self.calculate_price(size_class, tariff, services or [])
                if not quote.get("ok"):return quote
                duration_minutes = quote.get("duration_minutes") or duration_minutes
                services = self._quote_services(quote)
            if self._get_bays() is not None and (not size_class or not services):
                return _failure("Для проверки бокса выберите класс автомобиля и услуги")
            output = self._free_slots_duration(slots, booking_rows, dates[0] if date_str else None, days, duration_minutes)
            if output.get("ok"):
                for entry in output.get("days", []):
                    day = parse_date(entry["date"])
                    if self._get_bays() is not None:
                        entry["free_times"] = [t for t in entry["free_times"] if self._bay_choice(
                            booking_rows, day, t, duration_minutes or self.get_catalog().shortest_duration(), size_class, services)[1] is None]
                    if requested_time and date_str:
                        problem = self._check_duration_start(slots, booking_rows, day, requested_time,
                            duration_minutes or self.get_catalog().shortest_duration(), size_class=size_class, services=services)
                        output["requested_available"] = problem is None and self._is_future(day, requested_time)
                        if output["requested_available"]:
                            entry["free_times"] = sorted(set(entry["free_times"] + [requested_time]))
                        elif problem:output["requested_error"] = problem
            return output
        busy = self._booked_capacity(booking_rows)
        if not date_str:
            today = self.now().date()
            dates = [today + timedelta(days=index) for index in range(days)]

        result = []
        for booking_date in dates:
            times = self._free_times(booking_date, slots, busy)
            if times or date_str:
                result.append({
                    "date": booking_date.isoformat(),
                    "weekday": WEEKDAYS_RU[booking_date.weekday()],
                    "free_times": times,
                })
        output: dict[str, Any] = {"ok": True, "days": result}
        if not any(day["free_times"] for day in result):
            output["note"] = (
                "Свободного времени на эту дату нет."
                if date_str else f"На ближайшие {days} дней свободного времени нет."
            )
        return output

    def _free_slots_duration(self, schedule: DurationSchedule, booking_rows: list[dict[str, Any]],
                             requested: date | None, days: int, duration_minutes: int | None) -> dict:
        """Свободные старты для записи по длительности; без данных — безопасный отказ.

        Без выбранной услуги длительность не выдумывается: используется минимальная
        известная длительность, а финальная проверка при записи считает точно.
        """
        if requested and any(cap is None for _, _, cap in schedule.windows(requested)):
            return _failure("Не указана вместимость графика; уточните число постов у администратора")
        duration = duration_minutes
        if not duration:
            duration = self.get_catalog().shortest_duration()
        if not duration:
            return _failure("Нет данных о длительности услуг: свободное время посчитать нельзя. "
                            "Уточните услугу или обратитесь к администратору.")
        if requested:
            dates = [requested]
        else:
            today = self.now().date()
            dates = [today + timedelta(days=index) for index in range(days)]
        intervals, missing = self._booking_intervals(booking_rows)
        result = []
        for day in dates:
            times = self._duration_free_times(schedule, intervals, missing, day, duration)
            if times or requested:
                result.append({
                    "date": day.isoformat(),
                    "weekday": WEEKDAYS_RU[day.weekday()],
                    "free_times": times,
                })
        output: dict[str, Any] = {"ok": True, "days": result}
        notes = []
        if not any(day["free_times"] for day in result):
            notes.append("Свободного времени на эту дату нет." if requested
                         else f"На ближайшие {days} дней свободного времени нет.")
        if missing:
            notes.append("Есть записи без длительности: такие дни требуют ручной проверки администратором.")
        if requested and duration_minutes is None and duration != duration_minutes:
            notes.append(f"Показаны варианты для минимальной длительности {duration} мин; "
                         "точная проверка будет по выбранной услуге.")
        if notes:
            output["note"] = " ".join(notes)
        return output

    # ---------- создание записи ----------
    def create_booking(
        self, *, date_str: str, time_str: str, name: str, phone: str, car: str,
        size_class: str, tariff: str | None, services: list[str], comment: str,
        telegram_id: int, username: str | None, plate: str = "",
        expected_quote: dict | None = None,
    ) -> dict:
        booking_date, booking_time = parse_date(date_str), parse_time(to_work_time(time_str))
        if not booking_date or not booking_time:
            return _failure("Неверная дата или время. Нужно: дата ГГГГ-ММ-ДД, время ЧЧ:ММ")
        name, car = (name or "").strip(), (car or "").strip()
        if not name:
            return _failure("Не указано имя клиента")
        if not car:
            return _failure("Не указан автомобиль")
        if not valid_phone(phone):
            return _failure("Нужен корректный номер телефона клиента")

        with self.fresh_tables():  # один свежий снимок под блокировкой до завершения записи
            catalog = self.get_catalog(fresh=True)  # актуальные цены/наличие на момент записи
            matched_class = catalog.match_class(size_class)
            if not matched_class:
                return _failure("Неизвестный класс автомобиля", size_classes=catalog.size_classes)
            problem = catalog.class_rule_problem(car, matched_class)
            if problem:
                return problem
            quote = catalog.quote(matched_class, tariff, services)
            if not quote["ok"]:
                return quote

            if not self._is_future(booking_date, booking_time):
                return _failure("Это время уже прошло или слишком близко к текущему моменту")

            self._invalidate("slots", "bookings")  # слоты и записи — только свежие данные
            slots = self._get_slots()
            headers, booking_rows = self._get_bookings()
            booking_time = self.booking_time(time_str, slots)
            if not booking_time or not self._is_future(booking_date, booking_time):
                return _failure("Это время уже прошло или слишком близко к текущему моменту")
            # Госномер пишется только по физическому адресу из проверенной карты.
            # Если CAR_PLATE_REQUIRED=true, а колонки нет — запись блокируется,
            # чтобы не потерять обязательные данные молча.
            plate = (plate or "").strip()[:40]
            plate_column_present = "Госномер" in self._adaptive.booking_fields()
            if self.config.car_plate_required:
                if not plate_column_present:
                    return _failure("В таблице записей нет подтверждённой колонки для госномера; запись недоступна")
                if not plate:
                    return _failure("Не указан госномер автомобиля")
            elif plate and not plate_column_present:
                log.warning("Госномер не сохранён: в карте записей нет подходящей колонки")
            duplicate = next((record for record in booking_rows if pick(record, "Telegram ID") == str(telegram_id)
                              and parse_date(pick(record, "Дата")) == booking_date and parse_time(pick(record, "Время")) == booking_time
                              and pick(record, "Автомобиль").casefold() == car.casefold() and self._is_active(record)), None)
            if duplicate:
                # Повтор запроса после потерянного ответа: сверяем незакрытый журнал создания
                # по постоянному ID и подтверждаем оригинал, вторая строка не создаётся.
                if self._adaptive:
                    self._adaptive.settle_create(pick(duplicate, "ID"), telegram_id)
                return _failure("У клиента уже есть такая запись", booking_id=pick(duplicate, "ID"))
            duration_minutes = None
            if isinstance(slots, DurationSchedule):
                if not catalog.durations_present:
                    return _failure("Нет данных о длительности услуг; обратитесь к администратору")
                duration_minutes, duration_missing = self._quote_duration(quote)
                if duration_missing or not duration_minutes:
                    detail = ", ".join(duration_missing) if duration_missing else "не выбрана услуга или тариф"
                    return _failure(f"Нет данных о длительности: {detail}. Уточните у администратора.")
                if "Длительность" not in headers:
                    return _failure("В таблице записей нет колонки «Длительность»; обратитесь к администратору")
                selected_services = self._quote_services(quote)
                problem = self._check_duration_start(slots, booking_rows, booking_date, booking_time, duration_minutes,
                                                     size_class=matched_class, services=selected_services)
                if problem:
                    intervals, missing = self._booking_intervals(booking_rows)
                    return _failure(problem, free_times_that_day=self._duration_free_times(
                        slots, intervals, missing, booking_date, duration_minutes))
                bay, _ = self._bay_choice(booking_rows, booking_date, booking_time, duration_minutes, matched_class, selected_services)
                if bay and "Бокс" not in headers:
                    return _failure("Для именованных боксов добавьте колонку «Бокс» в таблицу записей")
            else:
                if not within_work_hours(booking_time):
                    return _failure(WORK_HOURS_HINT)
                busy = self._booked_capacity(booking_rows)
                if self._capacity_left(slots, busy, (booking_date, booking_time)) <= 0:
                    return _failure(
                        "Это время недоступно или уже занято",
                        free_times_that_day=self._free_times(booking_date, slots, busy),
                    )

            if expected_quote is not None:
                fields = ["size_class", "tariff", "extras", "lines", "total"]
                if quote.get("duration_minutes") is not None or expected_quote.get("duration_minutes") is not None:
                    fields.append("duration_minutes")
                if any(quote.get(field_name) != expected_quote.get(field_name) for field_name in fields):
                    return _failure("Цена или состав услуги изменились; нужно новое подтверждение",
                                 quote_changed=True, quote=dict(quote, currency=self.config.currency))

            if not self._adaptive:
                worksheet = self._get_spreadsheet().worksheet(self.config.sheet_bookings)
                if not headers:
                    worksheet.append_row(BOOKING_HEADERS, value_input_option="RAW")
                    headers = BOOKING_HEADERS

            booking_id = self._next_booking_id(booking_rows)

            composition = []
            for line in quote["lines"]:
                if "includes" in line:
                    text_value = f"{line['item']}: {', '.join(line['includes'])}"
                    if line["not_included_now"]:
                        text_value += f" (без: {', '.join(line['not_included_now'])})"
                    composition.append(text_value)
                else:
                    composition.append(line["item"])

            data = {
                "id": booking_id,
                "создано": self.now().strftime("%d.%m.%Y %H:%M"),
                "дата": booking_date.strftime("%d.%m.%Y"),
                "время": booking_time,
                "имя": name,
                "телефон": phone.strip(),
                "автомобиль": car,
                "класс": matched_class,
                "тариф": quote["tariff"] or "",
                "доп. услуги": ", ".join(quote["extras"]),
                "состав": "; ".join(composition),
                "сумма": quote["total"],
                "комментарий": (comment or "").strip(),
                "telegram id": str(telegram_id),
                "username": f"@{username}" if username else "",
                "статус": "Новая",
                "госномер": plate,
                **({} if duration_minutes is None else {"длительность": str(duration_minutes)}),
                **({"бокс": bay} if isinstance(slots, DurationSchedule) and bay else {}),
            }
            try:
                self._adaptive.create(data, expected_bookings=booking_rows)
            except SlotUnavailable as error:
                return _failure(str(error), free_times_that_day=self._free_times(booking_date, slots, busy))
            self._invalidate("bookings")
            self.refresh_bookings_sheet()

            return {
                "ok": True,
                "booking_id": booking_id,
                "user_no": str(self._user_booking_position(booking_rows, telegram_id, booking_date, booking_time)),
                "date": booking_date.isoformat(),
                "weekday": WEEKDAYS_RU[booking_date.weekday()],
                "time": booking_time,
                "car": car,
                "size_class": matched_class,
                "lines": quote["lines"],
                "total": quote["total"],
                "currency": self.config.currency,
                "message": "Запись создана и добавлена в Google-таблицу",
            }

    # ---------- записи клиента ----------
    @staticmethod
    def _next_booking_id(rows: list[dict]) -> int:
        """Номер новой записи: максимум среди известных ID + 1.

        Между циклами обслуживания номера растут и заново не используются:
        отменённые и архивные строки сохраняют ID и защищают его от повторного
        выделения; нечисловые наследственные ID при выделении игнорируются.
        Периодическая перенумерация (после хронологической сортировки и после
        удаления строк при переносе в архив) выставляет номера по порядку строк
        заново, поэтому ссылка на запись по номеру надёжна внутри интервала
        между циклами обслуживания.
        """
        identifiers = [int(item) for item in (pick(record, "ID") for record in rows) if str(item).isdigit()]
        return max(identifiers, default=0) + 1

    def refresh_bookings_sheet(self, *, sort: bool = False) -> None:
        """Сортировка/перенумерация только через карту физических координат."""
        with self._write_lock, self._lock:
            self._adaptive.sort_bookings(sort=sort)
            self._invalidate("bookings")

    def _paint_archive_sheet(self, archive) -> None:
        """Архивный лист: отменённые записи — красным, остальной архив — серым.

        Раскраска идёт по колонке «Статус»: отмены видно сразу среди истории.
        Пустые ряды сбрасываются в белый, чтобы старая краска не висела.
        """
        if not hasattr(archive, "format"):
            return
        try:
            values = archive.get_all_values()
        except Exception as error:
            log.warning("Архив: лист не раскрашен — не прочитан (%s)", type(error).__name__)
            return
        if len(values) < 2:
            return
        headers = [str(header).strip() for header in values[0]]
        status_index = header_index(headers, "Статус", "status")
        last_col = chr(ord("A") + len(headers) - 1) if 0 < len(headers) <= 26 else "Z"
        colors: dict[tuple[float, float, float], list[str]] = {}
        for offset, row_values in enumerate(values[1:]):
            sheet_row = offset + 2
            status = (str(row_values[status_index]).strip().lower()
                      if status_index is not None and status_index < len(row_values) else "")
            if not any(str(cell).strip() for cell in row_values):
                color = WHITE_COLOR  # пустой ряд сбрасываем в белый
            elif status.startswith("отмен"):
                color = CANCEL_COLOR  # отмена - красный
            else:
                color = ARCHIVE_COLOR  # архив - серый
            colors.setdefault(color, []).append(f"A{sheet_row}:{last_col}{sheet_row}")
        # Один HTTP-запрос на цвет: gspread объединяет список диапазонов в один batch_update.
        for color, ranges in colors.items():
            archive.format(ranges, {"backgroundColor": {"red": color[0], "green": color[1], "blue": color[2]}})

    def archive_old_bookings(self) -> int:
        """Прошедшие даты со статусом «Новая» помечаем «Архив».

        Колонку «Статус» берём из карты адаптива (rule["columns"]) — это физический
        адрес колонки листа. Позиция в каноническом списке полей (headers.index)
        с физикой не совпадает при перестановке колонок и направила бы «Архив»
        в чужое поле. Остальные данные строки не затрагиваются.
        """
        today = self.now().date()
        with self._write_lock, self._lock:
            headers, rows = self._table(self.config.sheet_bookings)
            if not rows or "Статус" not in headers:
                return 0
            candidates = []
            for record in rows:
                booking_date = parse_date(str(record.get("Дата", "")))
                status = str(record.get("Статус", "")).strip()
                if (booking_date and booking_date < today
                        and (not status or status.lower().startswith("нов"))):
                    candidates.append(record)
            if not candidates:
                return 0
            if self._adaptive:
                status_columns = {}
                for sheet_id in {record["__sheet_id__"] for record in candidates}:
                    try:
                        status_columns[sheet_id] = self._adaptive.writable_column(sheet_id, "Статус")
                    except MappingError as error:
                        log.warning("Архивация остановлена: %s", error)
                        return 0  # статус пишем только в подтверждённую картой колонку
            else:
                status_columns = {0: headers.index("Статус") + 1}
            updates, cells = [], []
            for record in candidates:
                column = status_columns[record["__sheet_id__"] if self._adaptive else 0]
                updates.append({"range": cell_name(record["__row__"], column), "values": [["Архив"]]})
                cells.append((record["__row__"], column))
            worksheet = self._get_spreadsheet().worksheet(self.config.sheet_bookings)
            try:
                worksheet.batch_update(updates, value_input_option="RAW")
            except AttributeError:
                # Синтетические листы в тестах умеют только поячеечную запись.
                for (row, column), update in zip(cells, updates):
                    worksheet.update_cell(row, column, update["values"][0][0])
            changed = len(updates)
            self._invalidate("bookings")
            return changed

    def move_archived_bookings(self) -> int:
        """Записи со статусами «Архив» и «Отмена» переносятся в отдельный архивный лист.

        Статус «Архив» ставит archive_old_bookings прошедшим датам; отменённые
        записи («Отмена») уезжают в архив сразу, независимо от даты, — в «Записях»
        остаются только живые записи. Лист хранит id и все данные записи
        (услуги, состав, сумму, статус) с той же шапкой, что у «Записей»;
        создаётся при первом переносе (имя — SHEET_ARCHIVE, по умолчанию
        «Архив»). Перенос идемпотентен: дубль — это та же запись, у которой
        совпали неизменяемые поля (ID, создано, дата, время, телефон); по одному
        ID сверяться нельзя — после перенумерации номера в «Записях» начинаются
        заново и совпадают с давними архивными. После успешной копии исходная
        строка удаляется из «Записей» физически; пустые ряды (в том числе
        оставшиеся от прежних чисток) убираются при каждом цикле — без пустых
        промежутков, следом колонка «ID» перенумеровывается по порядку строк.
        Строки с формулами или данными правее зоны бота не трогаются.
        Записи без распознанного ID не переносятся. Архивный лист раскрашивается:
        отмены — красным, остальной архив — серым; лист служебный, вне карт
        адаптива. Возвращает число скопированных записей.
        """
        with self._write_lock, self._lock:
            headers, rows = self._table(self.config.sheet_bookings)
            candidates = []
            for record in rows:
                status = pick(record, "Статус").lower()
                booking_id = pick(record, "ID")
                if (status.startswith("архив") or status.startswith("отмен")) and booking_id:
                    candidates.append((booking_id, record))
            copied, to_clear = 0, []
            if candidates:
                spreadsheet = self._get_spreadsheet()
                try:
                    archive = spreadsheet.worksheet(self.config.sheet_archive)
                    existing = archive.get_all_values()
                except (WorksheetNotFound, StopIteration):
                    try:
                        archive = spreadsheet.add_worksheet(self.config.sheet_archive,
                                                            rows=max(100, len(rows) + 1),
                                                            cols=max(16, len(headers)))
                    except AttributeError:
                        log.warning("Архив: лист «%s» не создан — перенос пропущен",
                                    self.config.sheet_archive)
                        return 0
                    archive.append_row(list(headers), value_input_option="RAW")
                    existing = [list(headers)]
                if not existing:
                    archive.append_row(list(headers), value_input_option="RAW")
                    existing = [list(headers)]
                archive_headers = [str(header).strip() for header in existing[0]]
                if "ID" not in archive_headers:
                    log.warning("Архив: в шапке листа «%s» нет колонки «ID» — перенос пропущен",
                                self.config.sheet_archive)
                    return 0
                # Дубль — совпадение неизменяемых полей записи; одного ID мало,
                # перенумерация использует номера повторно.
                key_fields = [header for header in ("ID", "Создано", "Дата", "Время", "Телефон", "Telegram ID")
                              if header in archive_headers]

                def archive_key(row_values):
                    if not key_fields:
                        return tuple(str(cell).strip() for cell in row_values)
                    cells = {header: (str(row_values[index]).strip() if index < len(row_values) else "")
                             for index, header in enumerate(archive_headers)}
                    return tuple(cells.get(field, "") for field in key_fields)

                known = {archive_key(row_values) for row_values in existing[1:]
                         if any(str(cell).strip() for cell in row_values)}
                for booking_id, record in candidates:
                    key = archive_key([pick(record, header) for header in archive_headers])
                    if key in known:
                        to_clear.append(record)  # копия уже в архиве с прошлого запуска
                        continue
                    try:
                        archive.append_row([pick(record, header) for header in archive_headers],
                                           value_input_option="RAW")
                    except Exception as error:
                        log.warning("Архив: запись %s не скопирована (%s) — источник не очищается",
                                    booking_id, type(error).__name__)
                        continue
                    known.add(key)
                    copied += 1
                    to_clear.append(record)
            deleted = self._remove_moved_bookings(to_clear, headers)
            if copied or deleted:
                try:
                    worksheet = self._get_spreadsheet().worksheet(self.config.sheet_bookings)
                except (WorksheetNotFound, StopIteration):
                    worksheet = None
                if worksheet is not None:
                    pass  # ID остаются ссылками клиентов; явная сортировка нумерует отдельно
            try:
                self._paint_archive_sheet(self._get_spreadsheet().worksheet(self.config.sheet_archive))
            except (WorksheetNotFound, StopIteration):
                pass  # архива ещё нет — красить нечего
            except Exception as error:
                log.warning("Архив: лист не раскрашен (%s)", type(error).__name__)
            if copied or deleted:
                log.info("В архивный лист «%s» перенесено записей: %d, удалено из «Записей»: %d",
                         self.config.sheet_archive, copied, deleted)
            return copied

    def _remove_moved_bookings(self, records, headers) -> int:
        """Физическое удаление перенесённых строк и пустых рядов из «Записей».

        Данные каждой удалённой строки уже лежат в архиве; пустые ряды (в том
        числе оставшиеся от прежних чисток) убираются при каждом цикле, чтобы
        в таблице не было промежутков. Безопасность: лист с формулами не
        трогается (удаление сдвинуло бы чужие ссылки); строка с данными правее
        зоны бота не удаляется. Возвращает число удалённых строк.
        """
        deleted, tails = 0, []
        if self._adaptive:
            grouped: dict = {sheet_id: [] for sheet_id in self._adaptive.booking_sheet_ids()}
            for record in records:
                grouped.setdefault(record["__sheet_id__"], []).append(record)
            jobs = [(sheet_id, self._get_spreadsheet().get_worksheet_by_id(sheet_id), sheet_records)
                    for sheet_id, sheet_records in grouped.items()]
        else:
            jobs = [(0, self._get_spreadsheet().worksheet(self.config.sheet_bookings), records)]
        for sheet_id, worksheet, sheet_records in jobs:
            delete = getattr(worksheet, "delete_rows", None)
            if delete is None:
                log.warning("Архив: лист %s не поддерживает удаление строк", sheet_id)
                continue
            try:
                values = worksheet.get_all_values(value_render_option="FORMULA")
            except Exception as error:
                log.warning("Архив: не удалось прочитать область %s (%s)", sheet_id, type(error).__name__)
                continue
            if any(str(cell).startswith("=") for row in values for cell in row):
                log.warning("Архив: в области %s есть формулы — строки не удаляются", sheet_id)
                continue
            mapped_by_row = {}
            for record in sheet_records:
                mapped = set()
                for field_name in record:
                    if field_name.startswith("__"):
                        continue
                    try:
                        if self._adaptive:
                            mapped.add(self._adaptive.writable_column(sheet_id, field_name))
                        elif field in headers:
                            # Офлайн: колонка поля — позиция в шапке листа «Записей».
                            mapped.add(headers.index(field) + 1)
                    except Exception as error:
                        log.warning("Архив: колонка поля %s не найдена (%s)", field, type(error).__name__)
                mapped_by_row[record["__row__"]] = mapped or set(range(1, len(headers) + 1))
            row_numbers = []
            for record in sheet_records:
                row_number = record["__row__"]
                mapped = mapped_by_row.get(row_number, set())
                row_values = values[row_number - 1] if row_number <= len(values) else []
                outside = [str(cell).strip() for index, cell in enumerate(row_values, 1)
                           if index not in mapped and str(cell).strip()]
                if outside:
                    log.warning("Архив: строка %d содержит данные вне зоны бота — не удаляется", row_number)
                    continue
                row_numbers.append(row_number)
            # Пустые ряды (в том числе от прежних чисток) убираем, чтобы не было промежутков.
            moved = set(row_numbers)
            for index in range(2, len(values) + 1):
                if index in moved or any(str(cell).strip() for cell in values[index - 1]):
                    continue
                row_numbers.append(index)
            for row_number in sorted(set(row_numbers), reverse=True):
                delete(row_number)
                deleted += 1
            tails.append((worksheet, values, deleted))
        # После удаления строк под данными может остаться старая краска (серые
        # ряды прошлых чисток): сбрасываем фон хвоста в белый, чтобы таблица
        # выглядела опрятно независимо от того, кто и когда её красил.
        last_col = chr(ord("A") + max(15, len(headers) - 1)) if 0 < len(headers) <= 26 else "Z"
        for worksheet, values, removed in tails:
            if not hasattr(worksheet, "format"):
                continue
            tail_start = len(values) - removed + 1  # первый ряд без данных после удаления
            last_row = getattr(worksheet, "row_count", None) or len(values) + 200
            if last_row >= tail_start:
                worksheet.format(f"A{tail_start}:{last_col}{last_row}",
                                 {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}})
        if deleted:
            self._invalidate("bookings")
        return deleted

    def clean_expired_slots(self) -> int:
        """Обслуживание слотов по карте: прошедшие строки, сдвиг, будущие даты."""
        today = self.now().date()
        with self._write_lock, self._lock:
            cleared, added = self._adaptive.clean_expired_slots(today)
            if cleared or added:
                self._invalidate("slots")
            return cleared

    def my_bookings(self, telegram_id: int) -> dict:
        with self._lock:
            self._invalidate("bookings")
            _, rows = self._get_bookings()
        identifiers = Counter(pick(record, "ID") for record in rows if pick(record, "ID"))
        if any(count > 1 for count in identifiers.values()):
            raise MappingError("повторяется идентификатор записи; список записей неоднозначен")
        output = []
        for record in rows:
            if pick(record, "Telegram ID") != str(telegram_id) or not self._is_active(record):
                continue
            booking_date, booking_time = parse_date(pick(record, "Дата")), parse_time(pick(record, "Время"))
            if not booking_date or not booking_time or self._slot_datetime(booking_date, booking_time) < self.now():
                continue
            output.append({
                "booking_id": pick(record, "ID"),
                "date": booking_date.isoformat(),
                "weekday": WEEKDAYS_RU[booking_date.weekday()],
                "time": booking_time,
                "car": pick(record, "Автомобиль"),
                "tariff": pick(record, "Тариф"),
                "extras": pick(record, "Доп. услуги"),
                "total": pick(record, "Сумма"),
                "status": pick(record, "Статус"),
                "_location": (record.get("__sheet_id__"), record.get("__row__")),
            })
        output.sort(key=lambda booking: (booking["date"], booking["time"]))
        return {"ok": True, "bookings": output}

    def cancel_booking(self, booking_id: Any, telegram_id: int, *, expected_booking: dict | None = None) -> dict:
        with self._write_lock, self._lock:
            self._invalidate("bookings")
            headers, rows = self._get_bookings()
            matches = [record for record in rows if pick(record, "ID") == str(booking_id).strip()]
            if len(matches) > 1:
                # Наследственный дубль ID: не выбираем строку вслепую.
                return _failure("Запись с таким номером неоднозначна; обратитесь к администратору")
            if not matches:
                return _failure("Запись с таким номером не найдена")
            record = matches[0]
            if pick(record, "Telegram ID") != str(telegram_id):
                return _failure("Запись с таким номером не найдена среди записей этого клиента")
            if not self._is_active(record):
                # Отмена могла прерваться до освобождения клетки: доводим по факту.
                released = self._adaptive.reconcile_cancel(record) if self._adaptive else None
                if released is True:
                    self._invalidate("bookings")
                    return _failure("Эта запись уже была отменена; место в сетке освобождено")
                return _failure("Эта запись уже отменена")
            booking_date, booking_time = parse_date(pick(record, "Дата")), parse_time(pick(record, "Время"))
            if expected_booking is not None:
                if "_location" in expected_booking and tuple(expected_booking["_location"]) != (record.get("__sheet_id__"), record.get("__row__")):
                    return _failure("Расположение записи изменилось. Подтвердите отмену заново")
                observed = {"date": booking_date.isoformat() if booking_date else "", "time": booking_time,
                            **{key: pick(record, column) for key, column in
                               (("car", "Автомобиль"), ("tariff", "Тариф"), ("extras", "Доп. услуги"),
                                ("total", "Сумма"), ("status", "Статус"))}}
                if any(str(value) != str(expected_booking.get(key)) for key, value in observed.items()):
                    return _failure("Детали записи изменились. Проверьте свои записи и подтвердите отмену заново")
            if booking_date and booking_time and self._slot_datetime(booking_date, booking_time) < self.now():
                return _failure("Прошедшую запись отменить нельзя")
            column = next((index for index, header in enumerate(headers, 1) if header.strip().lower() == "статус"), None)
            if not column:
                return _failure("В таблице нет колонки «Статус»")
            grid_released = self._adaptive.cancel(record, telegram_id)
            self._invalidate("bookings")
            self.refresh_bookings_sheet()
            return {
                "ok": True,
                "booking_id": str(booking_id),
                "date": booking_date.isoformat() if booking_date else "",
                "time": booking_time or "",
                "message": ("Запись отменена, слот освобождён" if grid_released in (None, True)
                            else "Запись отменена; место в сетке постов не освобождено автоматически, проверьте таблицу"),
            }

    def reschedule_booking(self, booking_id: Any, telegram_id: int, date_str: str, time_str: str,
                           *, expected_booking: dict | None = None, dry_run: bool = False) -> dict:
        new_date, new_time = parse_date(date_str), parse_time(to_work_time(time_str))
        if not new_date or not new_time:
            return _failure("Укажите предстоящую дату и время с учётом минимального интервала до записи")
        with self.fresh_tables():
            self._invalidate("slots", "bookings")
            slots = self._get_slots()
            headers, rows = self._get_bookings()
            new_time = self.booking_time(time_str, slots)
            if not new_time or not self._is_future(new_date, new_time):
                return _failure("Укажите предстоящую дату и время с учётом минимального интервала до записи")
            matches = [row for row in rows if pick(row, "ID") == str(booking_id).strip()]
            if len(matches) != 1 or pick(matches[0], "Telegram ID") != str(telegram_id):
                return _failure("Запись не найдена среди ваших записей")
            record = matches[0]
            if not self._is_active(record):
                return _failure("Эта запись уже отменена")
            old_date, old_time = parse_date(pick(record, "Дата")), parse_time(pick(record, "Время"))
            if not old_date or not old_time or self._slot_datetime(old_date, old_time) < self.now():
                return _failure("Прошедшую запись перенести нельзя")
            current = {"booking_id": str(booking_id), "date": old_date.isoformat(), "time": old_time,
                       "_location": (record.get("__sheet_id__"), record.get("__row__")),
                       **{key: pick(record, column) for key, column in (("car", "Автомобиль"),
                          ("tariff", "Тариф"), ("extras", "Доп. услуги"), ("total", "Сумма"), ("status", "Статус"))}}
            if expected_booking is not None and any(str(current.get(key)) != str(expected_booking.get(key))
                    for key in ("date", "time", "car", "tariff", "extras", "total", "status")):
                return _failure("Исходная запись изменилась. Проверьте её и подтвердите перенос заново")
            if expected_booking is not None and "_location" in expected_booking and tuple(expected_booking["_location"]) != current["_location"]:
                return _failure("Расположение записи изменилось. Подтвердите перенос заново")
            if (old_date, old_time) == (new_date, new_time):
                return _failure("Запись уже назначена на это время; перенос не требуется")
            try:
                self._adaptive.validate_reschedule(record, new_date.isoformat())
            except MappingError as error:
                return _failure(str(error))
            if isinstance(slots, DurationSchedule):
                raw = str(pick(record, "Длительность") or "").strip()
                moved_duration = parse_duration_minutes(raw) if raw else None
                if not moved_duration:
                    return _failure("В записи нет длительности; перенести нельзя. Обратитесь к администратору")
                catalog = self.get_catalog()
                quote = catalog.quote(pick(record, "Класс"), pick(record, "Тариф") or None,
                                      [v.strip() for v in pick(record, "Доп. услуги").split(",") if v.strip()])
                if not quote.get("ok"):return _failure("Не удалось проверить услуги переносимой записи")
                selected_services = self._quote_services(quote)
                problem = self._check_duration_start(slots, rows, new_date, new_time, moved_duration, exclude=record,
                                                     size_class=pick(record, "Класс"), services=selected_services)
                if problem:
                    intervals, missing = self._booking_intervals(rows)
                    return _failure(f"{problem}. Исходная запись сохранена",
                                    free_times_that_day=self._duration_free_times(
                                        slots, intervals, missing, new_date, moved_duration))
                bay, _ = self._bay_choice(rows, new_date, new_time, moved_duration, pick(record, "Класс"), selected_services, record)
            else:
                bay = None
                if not within_work_hours(new_time):return _failure(WORK_HOURS_HINT)
                busy = self._booked_capacity([row for row in rows if row is not record])
                if self._capacity_left(slots, busy, (new_date, new_time)) <= 0:
                    return _failure("Новое время занято. Исходная запись сохранена",
                                    free_times_that_day=self._free_times(new_date, slots, busy))
            result = {"ok": True, "booking": current, "booking_id": str(booking_id),
                      "date": new_date.isoformat(), "time": new_time, "car": current["car"],
                      "total": current["total"], "currency": self.config.currency}
            if dry_run:
                return result
            user_no = str(self._user_booking_position(rows, telegram_id, new_date, new_time, exclude=record))
            result["user_no"] = user_no
            try:
                result["grid_released"] = self._adaptive.reschedule(
                    record, telegram_id, new_date.isoformat(), new_time, bay=bay)
            except SlotUnavailable as error:
                return _failure(f"{error}. Исходная запись сохранена",
                                free_times_that_day=self._free_times(new_date, slots, busy))
            self._invalidate("bookings")
            self.refresh_bookings_sheet()
            return result
