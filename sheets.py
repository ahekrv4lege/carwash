"""Работа с Google Sheets и вся бизнес-логика: прайс, тарифы, свободные слоты, записи.

Цены, наличие услуг и свободное время считаются здесь, в коде, а не нейросетью —
модель только вызывает функции и пересказывает результат клиенту.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from config import Config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

# Служебные колонки листа «Услуги». ВСЕ остальные колонки — это классы (габариты) автомобилей.
SERVICE_FIXED_COLS = {"услуга", "описание", "доступна"}

# Колонки листа «Записи». Порядок в таблице может быть любым — бот ищет колонки по названию.
BOOKING_HEADERS = [
    "ID", "Создано", "Дата", "Время", "Имя", "Телефон", "Автомобиль", "Класс",
    "Тариф", "Доп. услуги", "Состав", "Сумма", "Комментарий", "Telegram ID", "Username", "Статус",
]


# ───────────────────────────── вспомогательные функции ─────────────────────────────

def is_yes(value: Any, default: bool = True) -> bool:
    """«да / нет», TRUE / FALSE (чекбоксы), 1 / 0. Пустая ячейка = default."""
    v = str(value).strip().lower()
    if not v:
        return default
    if v in {"да", "yes", "true", "1", "y", "+", "✓", "✅", "истина", "есть"}:
        return True
    if v in {"нет", "no", "false", "0", "n", "-", "❌", "ложь"} or v.startswith(("нет", "не ")):
        return False
    return default


def to_number(value: Any) -> float | None:
    s = re.sub(r"[^\d,.\-]", "", str(value))
    s = s.replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(value: Any) -> date | None:
    s = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_time(value: Any) -> str | None:
    """'9:00', '09:00:00', '9.30' -> 'HH:MM'"""
    m = re.match(r"^\s*(\d{1,2})[:.](\d{2})", str(value))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def pick(rec: dict, *names: str) -> str:
    """Значение ячейки по названию колонки (без учёта регистра и пробелов по краям)."""
    wanted = {n.lower() for n in names}
    for key, val in rec.items():
        if str(key).strip().lower() in wanted:
            return str(val).strip()
    return ""


def digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value))


def _fail(text: str, **extra: Any) -> dict:
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
        v = (value or "").strip().lower()
        if not v:
            return None
        for c in self.size_classes:
            if c.lower() == v:
                return c
        found = [c for c in self.size_classes if v in c.lower()]
        return found[0] if len(found) == 1 else None

    # ---- тариф = сумма цен ДОСТУПНЫХ услуг из состава минус скидка
    def tariff_quote(self, t: Tariff, cls: str) -> dict | None:
        included: list[str] = []
        removed: list[str] = []
        total = 0
        for item in t.items:
            s = self.services.get(item.lower())
            price = s.prices.get(cls) if s else None
            if s and s.available and price is not None:
                included.append(s.name)
                total += price
            else:
                removed.append(s.name if s else item)
        if not included:
            return None
        discount = min(max(t.discount, 0.0), 100.0)
        return {
            "price": int(round(total * (1 - discount / 100))),
            "included": included,
            "removed": removed,
        }

    # ---- расчёт стоимости: тариф и/или отдельные услуги
    def quote(self, cls: str, tariff: str | None, services: list[str] | None) -> dict:
        lines: list[dict] = []
        total = 0
        covered: set[str] = set()
        tariff_name = None

        if tariff and tariff.strip():
            t = self.tariffs.get(tariff.strip().lower())
            if not t or not t.enabled:
                return self._err(f"Тариф «{tariff}» не найден или сейчас недоступен")
            r = self.tariff_quote(t, cls)
            if not r:
                return self._err(f"Тариф «{t.name}» сейчас недоступен: все его позиции убраны")
            tariff_name = t.name
            lines.append({
                "item": f"Тариф «{t.name}»",
                "includes": r["included"],
                "not_included_now": r["removed"],
                "price": r["price"],
            })
            total += r["price"]
            covered = {x.lower() for x in r["included"]}

        extras: list[str] = []
        for raw in services or []:
            s = self.services.get(str(raw).strip().lower())
            if not s:
                return self._err(f"Услуга «{raw}» не найдена в прайсе")
            if not s.available:
                return self._err(f"Услуга «{s.name}» сейчас недоступна")
            price = s.prices.get(cls)
            if price is None:
                return self._err(f"Услуга «{s.name}» не оказывается для класса «{cls}»")
            if s.name.lower() in covered or s.name in extras:
                continue  # уже входит в тариф / дубль
            extras.append(s.name)
            lines.append({"item": s.name, "price": price})
            total += price

        if not lines:
            return self._err("Не выбран ни тариф, ни услуги")
        return {
            "ok": True,
            "size_class": cls,
            "tariff": tariff_name,
            "extras": extras,
            "lines": lines,
            "total": int(round(total)),
        }

    def _err(self, text: str) -> dict:
        return _fail(
            text,
            available_tariffs=[t.name for t in self.tariffs.values() if t.enabled],
            available_services=[s.name for s in self.services.values() if s.available],
        )

    # ---- прайс для нейросети
    def price_list(self) -> dict:
        services = [
            {"name": s.name, "description": s.description, "prices": s.prices}
            for s in self.services.values() if s.available
        ]
        unavailable = [s.name for s in self.services.values() if not s.available]

        tariffs: list[dict] = []
        unavailable_tariffs: list[str] = []
        for t in self.tariffs.values():
            if not t.enabled:
                continue
            prices: dict[str, int] = {}
            removed: dict[str, list[str]] = {}
            for cls in self.size_classes:
                r = self.tariff_quote(t, cls)
                if r:
                    prices[cls] = r["price"]
                    if r["removed"]:
                        removed[cls] = r["removed"]
            if not prices:
                unavailable_tariffs.append(t.name)
                continue
            entry: dict[str, Any] = {
                "name": t.name,
                "composition": t.items,
                "discount_percent": t.discount,
                "prices": prices,
            }
            if removed:
                vals = list(removed.values())
                same = len(removed) == len(prices) and all(v == vals[0] for v in vals)
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

class Store:
    """Все методы синхронные — из asyncio вызываются через asyncio.to_thread."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._ss = None  # gspread.Spreadsheet, подключаемся лениво

    # ---------- низкий уровень ----------
    def _spreadsheet(self):
        if self._ss is None:
            import gspread

            kwargs: dict[str, Any] = {}
            backoff = getattr(gspread, "BackOffHTTPClient", None)  # авто-повтор при лимитах API
            if backoff:
                kwargs["http_client"] = backoff
            gc = gspread.service_account(
                filename=self.cfg.google_credentials_file, scopes=SCOPES, **kwargs
            )
            self._ss = gc.open_by_key(self.cfg.spreadsheet_id)
        return self._ss

    def _table(self, title: str) -> tuple[list[str], list[dict[str, Any]]]:
        """Лист -> (заголовки, список строк-словарей). Пустые строки пропускаются."""
        values = self._spreadsheet().worksheet(title).get_all_values()
        if not values:
            return [], []
        headers = [h.strip() for h in values[0]]
        rows: list[dict[str, Any]] = []
        for i, raw in enumerate(values[1:], start=2):
            if not any(str(c).strip() for c in raw):
                continue
            rec: dict[str, Any] = {
                h: (raw[j] if j < len(raw) else "") for j, h in enumerate(headers) if h
            }
            rec["__row__"] = i
            rows.append(rec)
        return headers, rows

    def _cached(self, key: str, ttl: int, loader: Callable[[], Any]) -> Any:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = loader()
        self._cache[key] = (now, value)
        return value

    def _invalidate(self, *keys: str) -> None:
        for k in keys:
            self._cache.pop(k, None)

    # ---------- время ----------
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def _slot_dt(self, d: date, t: str) -> datetime:
        h, m = (int(x) for x in t.split(":"))
        return datetime(d.year, d.month, d.day, h, m, tzinfo=self.tz)

    def _is_future(self, d: date, t: str) -> bool:
        return self._slot_dt(d, t) >= self.now() + timedelta(minutes=self.cfg.min_lead_minutes)

    # ---------- прайс и тарифы ----------
    def get_catalog(self, fresh: bool = False) -> Catalog:
        if fresh:
            self._invalidate("catalog")
        return self._cached("catalog", self.cfg.catalog_cache_sec, self._load_catalog)

    def _load_catalog(self) -> Catalog:
        headers, rows = self._table(self.cfg.sheet_services)
        classes = [h for h in headers if h and h.lower() not in SERVICE_FIXED_COLS]

        services: dict[str, Service] = {}
        for r in rows:
            name = pick(r, "Услуга")
            if not name:
                continue
            prices: dict[str, int] = {}
            for c in classes:
                p = to_number(r.get(c, ""))
                if p is not None:
                    prices[c] = int(round(p))
            services[name.lower()] = Service(
                name=name,
                description=pick(r, "Описание"),
                available=is_yes(pick(r, "Доступна"), default=True),
                prices=prices,
            )

        _, trows = self._table(self.cfg.sheet_tariffs)
        tariffs: dict[str, Tariff] = {}
        for r in trows:
            name = pick(r, "Тариф")
            if not name:
                continue
            items = [x.strip() for x in re.split(r"[,;\n]", pick(r, "Состав")) if x.strip()]
            for item in items:
                if item.lower() not in services:
                    log.warning("Тариф «%s»: услуга «%s» не найдена на листе «%s»",
                                name, item, self.cfg.sheet_services)
            tariffs[name.lower()] = Tariff(
                name=name,
                items=items,
                discount=to_number(pick(r, "Скидка %", "Скидка")) or 0.0,
                enabled=is_yes(pick(r, "Доступен", "Доступна"), default=True),
            )
        return Catalog(classes, services, tariffs)

    def price_list(self) -> dict:
        data = self.get_catalog().price_list()
        data["currency"] = self.cfg.currency
        return data

    def calculate_price(self, size_class: str, tariff: str | None, services: list[str]) -> dict:
        cat = self.get_catalog()
        cls = cat.match_class(size_class)
        if not cls:
            return _fail("Неизвестный класс автомобиля", size_classes=cat.size_classes)
        res = cat.quote(cls, tariff, services)
        res["currency"] = self.cfg.currency
        return res

    # ---------- слоты и записи ----------
    def _load_slots(self) -> dict[tuple[date, str], int]:
        _, rows = self._table(self.cfg.sheet_slots)
        slots: dict[tuple[date, str], int] = {}
        for r in rows:
            d, t = parse_date(pick(r, "Дата")), parse_time(pick(r, "Время"))
            if not d or not t:
                continue
            n = to_number(pick(r, "Постов", "Мест", "Боксов"))
            cap = 1 if n is None else max(int(n), 0)
            slots[(d, t)] = slots.get((d, t), 0) + cap
        return slots

    def _load_bookings(self) -> tuple[list[str], list[dict[str, Any]]]:
        return self._table(self.cfg.sheet_bookings)

    def _get_slots(self) -> dict[tuple[date, str], int]:
        return self._cached("slots", self.cfg.slots_cache_sec, self._load_slots)

    def _get_bookings(self) -> tuple[list[str], list[dict[str, Any]]]:
        return self._cached("bookings", self.cfg.slots_cache_sec, self._load_bookings)

    @staticmethod
    def _is_active(rec: dict) -> bool:
        return not pick(rec, "Статус").lower().startswith("отмен")

    def _busy(self, rows: list[dict]) -> Counter:
        busy: Counter = Counter()
        for r in rows:
            if not self._is_active(r):
                continue
            d, t = parse_date(pick(r, "Дата")), parse_time(pick(r, "Время"))
            if d and t:
                busy[(d, t)] += 1
        return busy

    def _free_times(self, d: date, slots: dict, busy: Counter) -> list[str]:
        return sorted(
            t for (dd, t), cap in slots.items()
            if dd == d and cap - busy.get((dd, t), 0) > 0 and self._is_future(dd, t)
        )

    def free_slots(self, date_str: str | None = None, days: int = 7) -> dict:
        slots = self._get_slots()
        _, brows = self._get_bookings()
        busy = self._busy(brows)

        if date_str:
            d = parse_date(date_str)
            if not d:
                return _fail("Неверная дата, нужен формат ГГГГ-ММ-ДД")
            dates = [d]
        else:
            today = self.now().date()
            dates = [today + timedelta(days=i) for i in range(days)]

        result = []
        for d in dates:
            times = self._free_times(d, slots, busy)
            if times or date_str:
                result.append({
                    "date": d.isoformat(),
                    "weekday": WEEKDAYS_RU[d.weekday()],
                    "free_times": times,
                })
        out: dict[str, Any] = {"ok": True, "days": result}
        if not any(day["free_times"] for day in result):
            out["note"] = (
                "Свободного времени на эту дату нет."
                if date_str else f"На ближайшие {days} дней свободного времени нет."
            )
        return out

    # ---------- создание записи ----------
    def create_booking(
        self, *, date_str: str, time_str: str, name: str, phone: str, car: str,
        size_class: str, tariff: str | None, services: list[str], comment: str,
        tg_id: int, username: str | None,
    ) -> dict:
        d, t = parse_date(date_str), parse_time(time_str)
        if not d or not t:
            return _fail("Неверная дата или время. Нужно: дата ГГГГ-ММ-ДД, время ЧЧ:ММ")
        name, car = (name or "").strip(), (car or "").strip()
        if not name:
            return _fail("Не указано имя клиента")
        if not car:
            return _fail("Не указан автомобиль")
        if len(digits(phone)) < 10:
            return _fail("Нужен корректный номер телефона клиента")

        with self._lock:  # защита от двойной записи на один слот
            cat = self.get_catalog(fresh=True)  # актуальные цены/наличие на момент записи
            cls = cat.match_class(size_class)
            if not cls:
                return _fail("Неизвестный класс автомобиля", size_classes=cat.size_classes)
            q = cat.quote(cls, tariff, services)
            if not q["ok"]:
                return q

            if not self._is_future(d, t):
                return _fail("Это время уже прошло или слишком близко к текущему моменту")

            self._invalidate("slots", "bookings")  # слоты и записи — только свежие данные
            slots = self._get_slots()
            headers, brows = self._get_bookings()
            busy = self._busy(brows)
            if slots.get((d, t), 0) - busy.get((d, t), 0) <= 0:
                return _fail(
                    "Это время недоступно или уже занято",
                    free_times_that_day=self._free_times(d, slots, busy),
                )

            ws = self._spreadsheet().worksheet(self.cfg.sheet_bookings)
            if not headers:
                ws.append_row(BOOKING_HEADERS, value_input_option="RAW")
                headers = BOOKING_HEADERS

            ids = [int(x) for x in (pick(r, "ID") for r in brows) if x.isdigit()]
            booking_id = max(ids, default=0) + 1

            composition = []
            for ln in q["lines"]:
                if "includes" in ln:
                    txt = f"{ln['item']}: {', '.join(ln['includes'])}"
                    if ln["not_included_now"]:
                        txt += f" (без: {', '.join(ln['not_included_now'])})"
                    composition.append(txt)
                else:
                    composition.append(ln["item"])

            data = {
                "id": booking_id,
                "создано": self.now().strftime("%d.%m.%Y %H:%M"),
                "дата": d.strftime("%d.%m.%Y"),
                "время": t,
                "имя": name,
                "телефон": phone.strip(),
                "автомобиль": car,
                "класс": cls,
                "тариф": q["tariff"] or "",
                "доп. услуги": ", ".join(q["extras"]),
                "состав": "; ".join(composition),
                "сумма": q["total"],
                "комментарий": (comment or "").strip(),
                "telegram id": str(tg_id),
                "username": f"@{username}" if username else "",
                "статус": "Новая",
            }
            row = [data.get(h.strip().lower(), "") for h in headers]
            ws.append_row(row, value_input_option="RAW")
            self._invalidate("bookings")

            return {
                "ok": True,
                "booking_id": booking_id,
                "date": d.isoformat(),
                "weekday": WEEKDAYS_RU[d.weekday()],
                "time": t,
                "car": car,
                "size_class": cls,
                "lines": q["lines"],
                "total": q["total"],
                "currency": self.cfg.currency,
                "message": "Запись создана и добавлена в Google-таблицу",
            }

    # ---------- записи клиента ----------
    def my_bookings(self, tg_id: int) -> dict:
        self._invalidate("bookings")
        _, rows = self._get_bookings()
        out = []
        for r in rows:
            if digits(pick(r, "Telegram ID")) != str(tg_id) or not self._is_active(r):
                continue
            d, t = parse_date(pick(r, "Дата")), parse_time(pick(r, "Время"))
            if not d or not t or self._slot_dt(d, t) < self.now():
                continue
            out.append({
                "booking_id": pick(r, "ID"),
                "date": d.isoformat(),
                "weekday": WEEKDAYS_RU[d.weekday()],
                "time": t,
                "car": pick(r, "Автомобиль"),
                "tariff": pick(r, "Тариф"),
                "extras": pick(r, "Доп. услуги"),
                "total": pick(r, "Сумма"),
                "status": pick(r, "Статус"),
            })
        out.sort(key=lambda b: (b["date"], b["time"]))
        return {"ok": True, "bookings": out}

    def cancel_booking(self, booking_id: Any, tg_id: int) -> dict:
        with self._lock:
            self._invalidate("bookings")
            headers, rows = self._get_bookings()
            for r in rows:
                if pick(r, "ID") != str(booking_id).strip():
                    continue
                if digits(pick(r, "Telegram ID")) != str(tg_id):
                    return _fail("Запись с таким номером не найдена среди записей этого клиента")
                if not self._is_active(r):
                    return _fail("Эта запись уже отменена")
                d, t = parse_date(pick(r, "Дата")), parse_time(pick(r, "Время"))
                if d and t and self._slot_dt(d, t) < self.now():
                    return _fail("Прошедшую запись отменить нельзя")
                col = next((i for i, h in enumerate(headers, 1) if h.strip().lower() == "статус"), None)
                if not col:
                    return _fail("В таблице нет колонки «Статус»")
                ws = self._spreadsheet().worksheet(self.cfg.sheet_bookings)
                ws.update_cell(r["__row__"], col, "Отменена")
                self._invalidate("bookings")
                return {
                    "ok": True,
                    "booking_id": str(booking_id),
                    "date": d.isoformat() if d else "",
                    "time": t or "",
                    "message": "Запись отменена, слот освобождён",
                }
            return _fail("Запись с таким номером не найдена")
