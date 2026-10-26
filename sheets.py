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
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from gspread import HTTPClient
from gspread.exceptions import APIError

from config import Config
from adaptive import AdaptiveSheets, BOOKING_FIELDS

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

# Служебные колонки листа «Услуги». ВСЕ остальные колонки — это классы (габариты) автомобилей.
SERVICE_FIXED_COLS = {"услуга", "описание", "доступна"}

# Колонки листа «Записи». Порядок в таблице может быть любым — бот ищет колонки по названию.
BOOKING_HEADERS = BOOKING_FIELDS


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


def parse_date(value: Any) -> date | None:
    text = str(value).strip()
    for date_format in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
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


def valid_phone(value: Any) -> bool:
    return (isinstance(value, str) and 10 <= len(digits(value)) <= 15 and
            re.fullmatch(r"[+\d\s().\-\u2010-\u2015\u2212\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]+", value) is not None)


def _failure(text: str, **extra: Any) -> dict:
    return {"ok": False, "error": text, **extra}


# ───────────────────────────── модель данных ─────────────────────────────

@dataclass
class Service:
    name: str
    description: str
    available: bool
    prices: dict[str, int]          # класс автомобиля -> цена


@dataclass
class Tariff:
    name: str
    items: list[str]                # названия услуг из листа «Услуги»
    discount: float                 # скидка в % за комплекс
    enabled: bool


@dataclass
class Catalog:
    size_classes: list[str]
    services: dict[str, Service]    # ключ — название в нижнем регистре
    tariffs: dict[str, Tariff]

    # ---- классы автомобилей
    def match_class(self, value: str) -> str | None:
        normalized_class = (value or "").strip().lower()
        if not normalized_class:
            return None
        for class_name in self.size_classes:
            if class_name.lower() == normalized_class:
                return class_name
        return None

    # ---- тариф = сумма цен ДОСТУПНЫХ услуг из состава минус скидка
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
        if not included:
            return None
        discount = min(max(tariff.discount, 0.0), 100.0)
        return {
            "price": int(round(total * (1 - discount / 100))),
            "included": included,
            "removed": removed,
        }

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
            tariff_price = self.tariff_quote(selected_tariff, size_class)
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
        return {
            "ok": True,
            "size_class": size_class,
            "tariff": tariff_name,
            "extras": extras,
            "lines": lines,
            "total": int(round(total)),
        }

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
                tariff_price = self.tariff_quote(tariff, size_class)
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


class Store:
    """Все методы синхронные — из asyncio вызываются через asyncio.to_thread."""

    def __init__(self, config: Config):
        self.config = config
        self.timezone = ZoneInfo(config.timezone)
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._spreadsheet = None  # gspread.Spreadsheet, подключаемся лениво
        self._adaptive = None
        self._observed = None
        if config.sheets_adaptive_enabled:
            self._adaptive = AdaptiveSheets(config, self._get_spreadsheet)

    def refresh_schema(self, force: bool = True, validate: bool = False) -> dict:
        if not self._adaptive:
            raise RuntimeError("Включите SHEETS_ADAPTIVE_ENABLED для проверки mappings")
        with self._lock:
            document = self._adaptive.refresh(force=force, validate=validate)
            self._invalidate("catalog", "slots", "bookings")
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
        """Лист -> (заголовки, список строк-словарей). Пустые строки пропускаются."""
        if self._adaptive:
            roles = {self.config.sheet_services: "services", self.config.sheet_tariffs: "tariffs",
                     self.config.sheet_slots: "slots", self.config.sheet_bookings: "bookings"}
            return self._adaptive.table(roles[title], _observed=self._observed if observed is None else observed)
        values = self._get_spreadsheet().worksheet(title).get_all_values()
        if not values:
            return [], []
        headers = [header.strip() for header in values[0]]
        rows: list[dict[str, Any]] = []
        for index, raw in enumerate(values[1:], start=2):
            if not any(str(cell).strip() for cell in raw):
                continue
            record: dict[str, Any] = {
                header: (raw[column_index] if column_index < len(raw) else "") for column_index, header in enumerate(headers) if header
            }
            record["__row__"] = index
            rows.append(record)
        return headers, rows

    @contextmanager
    def fresh_tables(self):
        """Один свежий пакет для расчёта; финальный write-preflight остаётся независимым."""
        with self._lock:
            previous = self._observed
            self._invalidate("catalog", "slots", "bookings")
            try:
                self._observed = self._adaptive.observe() if self._adaptive else None
                yield
            except BaseException:
                self._invalidate("catalog", "slots", "bookings")
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
            observed = self._adaptive.observe(roles=("services", "tariffs"))
        headers, rows = self._table(self.config.sheet_services, observed)
        # Классы без повторов: две колонки с одинаковым названием (даже в
        # разном регистре) не должны удваивать выбор в прайсе и подсказках.
        classes: list[str] = []
        seen_classes: set[str] = set()
        for header in headers:
            label = str(header).strip()
            lowered = label.lower()
            if lowered and lowered not in SERVICE_FIXED_COLS and lowered not in seen_classes:
                seen_classes.add(lowered)
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
            )

        _, tariff_rows = self._table(self.config.sheet_tariffs, observed)
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
            tariffs[name.lower()] = Tariff(
                name=name,
                items=items,
                discount=to_number(pick(record, "Скидка %", "Скидка")) or 0.0,
                enabled=is_yes(pick(record, "Доступен", "Доступна"), default=True),
            )
        return Catalog(classes, services, tariffs)

    def price_list(self) -> dict:
        data = self.get_catalog().price_list()
        data["currency"] = self.config.currency
        return data

    def calculate_price(self, size_class: str, tariff: str | None, services: list[str], *, fresh: bool = False) -> dict:
        catalog = self.get_catalog(fresh=fresh)
        matched_class = catalog.match_class(size_class)
        if not matched_class:
            return _failure("Неизвестный класс автомобиля", size_classes=catalog.size_classes)
        result = catalog.quote(matched_class, tariff, services)
        result["currency"] = self.config.currency
        return result

    # ---------- слоты и записи ----------
    def _load_slots(self) -> dict[tuple[date, str], int]:
        _, rows = self._table(self.config.sheet_slots)
        slots: dict[tuple[date, str], int] = {}
        for record in rows:
            booking_date, booking_time = parse_date(pick(record, "Дата")), parse_time(pick(record, "Время"))
            if not booking_date or not booking_time:
                continue
            capacity_value = to_number(pick(record, "Постов", "Мест", "Боксов"))
            capacity = 1 if capacity_value is None else max(int(capacity_value), 0)
            slots[(booking_date, booking_time)] = slots.get((booking_date, booking_time), 0) + capacity
        return slots

    def _load_bookings(self) -> tuple[list[str], list[dict[str, Any]]]:
        return self._table(self.config.sheet_bookings)

    def _get_slots(self) -> dict[tuple[date, str], int]:
        return self._cached("slots", self.config.slots_cache_sec, self._load_slots)

    def _get_bookings(self) -> tuple[list[str], list[dict[str, Any]]]:
        return self._cached("bookings", self.config.slots_cache_sec, self._load_bookings)

    @staticmethod
    def _is_active(record: dict) -> bool:
        return not pick(record, "Статус").lower().startswith("отмен")

    def _booked_capacity(self, rows: list[dict]) -> Counter:
        busy: Counter = Counter()
        for record in rows:
            if not self._is_active(record):
                continue
            booking_date, booking_time = parse_date(pick(record, "Дата")), parse_time(pick(record, "Время"))
            if booking_date and booking_time:
                busy[(booking_date, booking_time)] += 1
        return busy

    def _free_times(self, requested_date: date, slots: dict, busy: Counter) -> list[str]:
        return sorted(
            slot_time for (slot_date, slot_time), capacity in slots.items()
            if slot_date == requested_date and capacity - busy.get((slot_date, slot_time), 0) > 0 and self._is_future(slot_date, slot_time)
        )

    def free_slots(self, date_str: str | None = None, days: int = 7) -> dict:
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

    # ---------- создание записи ----------
    def create_booking(
        self, *, date_str: str, time_str: str, name: str, phone: str, car: str,
        size_class: str, tariff: str | None, services: list[str], comment: str,
        telegram_id: int, username: str | None,
        expected_quote: dict | None = None,
    ) -> dict:
        booking_date, booking_time = parse_date(date_str), parse_time(time_str)
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
            quote = catalog.quote(matched_class, tariff, services)
            if not quote["ok"]:
                return quote

            if not self._is_future(booking_date, booking_time):
                return _failure("Это время уже прошло или слишком близко к текущему моменту")

            self._invalidate("slots", "bookings")  # слоты и записи — только свежие данные
            slots = self._get_slots()
            headers, booking_rows = self._get_bookings()
            duplicate = next((record for record in booking_rows if pick(record, "Telegram ID") == str(telegram_id)
                              and parse_date(pick(record, "Дата")) == booking_date and parse_time(pick(record, "Время")) == booking_time
                              and pick(record, "Автомобиль").casefold() == car.casefold() and self._is_active(record)), None)
            if duplicate:
                return _failure("У клиента уже есть такая запись", booking_id=pick(duplicate, "ID"))
            busy = self._booked_capacity(booking_rows)
            if slots.get((booking_date, booking_time), 0) - busy.get((booking_date, booking_time), 0) <= 0:
                return _failure(
                    "Это время недоступно или уже занято",
                    free_times_that_day=self._free_times(booking_date, slots, busy),
                )

            if expected_quote is not None:
                fields = ("size_class", "tariff", "extras", "lines", "total")
                if any(quote.get(field_name) != expected_quote.get(field_name) for field_name in fields):
                    return _failure("Цена или состав услуги изменились; нужно новое подтверждение",
                                 quote_changed=True, quote=dict(quote, currency=self.config.currency))

            if not self._adaptive:
                worksheet = self._get_spreadsheet().worksheet(self.config.sheet_bookings)
                if not headers:
                    worksheet.append_row(BOOKING_HEADERS, value_input_option="RAW")
                    headers = BOOKING_HEADERS

            identifiers = [int(item) for item in (pick(record, "ID") for record in booking_rows) if item.isdigit()]
            booking_id = max(identifiers, default=0) + 1

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
            }
            if self._adaptive:
                self._adaptive.create(data, expected_bookings=booking_rows)
            else:
                row = [data.get(header.strip().lower(), "") for header in headers]
                worksheet.append_row(row, value_input_option="RAW")
            self._invalidate("bookings")

            return {
                "ok": True,
                "booking_id": booking_id,
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
    def my_bookings(self, telegram_id: int) -> dict:
        with self._lock:
            self._invalidate("bookings")
            _, rows = self._get_bookings()
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
            })
        output.sort(key=lambda booking: (booking["date"], booking["time"]))
        return {"ok": True, "bookings": output}

    def cancel_booking(self, booking_id: Any, telegram_id: int, *, expected_booking: dict | None = None) -> dict:
        with self._lock:
            self._invalidate("bookings")
            headers, rows = self._get_bookings()
            for record in rows:
                if pick(record, "ID") != str(booking_id).strip():
                    continue
                if pick(record, "Telegram ID") != str(telegram_id):
                    return _failure("Запись с таким номером не найдена среди записей этого клиента")
                if not self._is_active(record):
                    return _failure("Эта запись уже отменена")
                booking_date, booking_time = parse_date(pick(record, "Дата")), parse_time(pick(record, "Время"))
                if expected_booking is not None:
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
                if self._adaptive:
                    self._adaptive.cancel(record, telegram_id)
                else:
                    worksheet = self._get_spreadsheet().worksheet(self.config.sheet_bookings)
                    worksheet.update_cell(record["__row__"], column, "Отменена")
                self._invalidate("bookings")
                return {
                    "ok": True,
                    "booking_id": str(booking_id),
                    "date": booking_date.isoformat() if booking_date else "",
                    "time": booking_time or "",
                    "message": "Запись отменена, слот освобождён",
                }
            return _failure("Запись с таким номером не найдена")
