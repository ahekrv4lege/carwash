"""Административная статистика по записям: только чтение, без обращений к сети.

Источник — уже прочитанные строки листа «Записи» и вместимость листа «Слоты».
Перенос записи меняет дату и время в той же строке, поэтому перенесённая запись
считается один раз. Сумма записи — это «забронировано», а не полученная оплата.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sheets import DurationSchedule, FreeCapacity, parse_date, parse_time, pick, to_number

PERIOD_CHOICES = (1, 7, 30)
DEFAULT_PERIOD = 7
# Одна запись занимает один часовой слот (провизорная длительность 60 минут).
SLOT_MINUTES = 60
_SERVICE_SPLIT = re.compile(r"[;,\n]")


@dataclass
class Stats:
    days: int
    start: date
    end: date
    total: int = 0
    canceled: int = 0
    completed: int = 0
    open_past: int = 0
    upcoming: int = 0
    by_tariff: Counter = field(default_factory=Counter)
    by_class: Counter = field(default_factory=Counter)
    by_service: Counter = field(default_factory=Counter)
    by_hour: Counter = field(default_factory=Counter)
    booked_value: float = 0.0
    value_missing: int = 0
    occupied_hours: int = 0
    capacity_hours: int | None = 0
    over_capacity_hours: int = 0
    unlisted_hours: int = 0
    skipped_rows: int = 0
    upcoming_total: int = 0

    @property
    def cancellation_rate(self) -> float | None:
        return self.canceled / self.total if self.total else None

    @property
    def utilization(self) -> float | None:
        if self.capacity_hours is None or not self.capacity_hours:
            return None
        return self.occupied_hours / self.capacity_hours


def _status_kind(status: str) -> str:
    lowered = status.strip().lower()
    if lowered.startswith("отмен"):
        return "canceled"
    if lowered.startswith("архив") or lowered.startswith("выполн"):
        return "completed"
    return "open"


def _services(record: dict) -> set[str]:
    names = set()
    for column in ("Состав", "Доп. услуги"):
        for part in _SERVICE_SPLIT.split(pick(record, column)):
            name = " ".join(part.split())
            if name:
                names.add(name)
    return names


def compute(rows: list[dict], slots: dict[tuple[date, str], Any], today: date, days: int = DEFAULT_PERIOD) -> Stats:
    """Метрики за последние `days` дней, включая сегодня. Предстоящие записи — отдельно."""
    start = today - timedelta(days=days - 1)
    stats = Stats(days=days, start=start, end=today)
    # Запись по длительности не имеет слотов: «часы постов» не выводим, остальное считаем.
    duration_mode = isinstance(slots, DurationSchedule)
    free_grid = duration_mode or any(isinstance(capacity, FreeCapacity) for capacity in slots.values())
    if not duration_mode:
        for (slot_date, _), capacity in slots.items():
            if start <= slot_date <= today:
                stats.capacity_hours = None if free_grid else stats.capacity_hours + int(capacity)
    booked_per_slot: Counter = Counter()
    for record in rows:
        booking_date, booking_time = parse_date(pick(record, "Дата")), parse_time(pick(record, "Время"))
        if not booking_date or not booking_time:
            stats.skipped_rows += 1
            continue
        kind = _status_kind(pick(record, "Статус"))
        if booking_date > today:
            if kind == "open":
                stats.upcoming_total += 1
            continue
        if not start <= booking_date:
            continue
        stats.total += 1
        if kind == "canceled":
            stats.canceled += 1
            continue
        if kind == "completed":
            stats.completed += 1
        else:
            stats.open_past += 1
        stats.occupied_hours += 1
        booked_per_slot[(booking_date, booking_time)] += 1
        stats.by_tariff[pick(record, "Тариф") or "без тарифа"] += 1
        stats.by_class[pick(record, "Класс") or "не указан"] += 1
        stats.by_hour[booking_time] += 1
        for service in _services(record):
            stats.by_service[service] += 1
        amount = to_number(pick(record, "Сумма"))
        if amount is None:
            stats.value_missing += 1
        else:
            stats.booked_value += amount
    for key, booked in booked_per_slot.items():
        if duration_mode:
            continue  # занятость считается интервалами, а не слотами
        if key not in slots:
            stats.unlisted_hours += booked
        elif not free_grid:
            stats.over_capacity_hours += max(0, booked - int(slots[key]))
    return stats


def render(stats: Stats) -> str:
    label = {1: "сегодня", 7: "7 дней", 30: "30 дней"}.get(stats.days, f"{stats.days} дн.")
    lines = [f"Статистика: {label} ({stats.start.strftime('%d.%m.%Y')} — {stats.end.strftime('%d.%m.%Y')})", ""]
    lines.append(f"Всего записей: {stats.total}")
    lines.append(f"Выполнено/архив: {stats.completed}; открытых за прошедшие даты: {stats.open_past}")
    lines.append(f"Отменено: {stats.canceled}" + (f" ({stats.cancellation_rate:.0%})" if stats.cancellation_rate is not None else ""))
    lines.append(f"Предстоящих открытых записей (всего): {stats.upcoming_total}")
    if stats.utilization is None:
        lines.append("Загрузка постов: недоступна (нет полной вместимости по слотам)")
    else:
        lines.append(f"Загрузка постов: {stats.utilization:.0%} ({stats.occupied_hours} из {stats.capacity_hours} часов)")
    booked = f"{stats.booked_value:,.0f}".replace(",", " ")
    lines.append(f"Сумма активных записей: {booked} ₽ — забронировано, не оплачено")
    if stats.value_missing:
        lines.append(f"Без суммы в таблице: {stats.value_missing}")
    if stats.over_capacity_hours:
        lines.append(f"Записей сверх вместимости слота: {stats.over_capacity_hours} (проверьте таблицу)")
    if stats.unlisted_hours:
        lines.append(f"Записей на время, которого нет в «Слоты»: {stats.unlisted_hours}")
    if stats.skipped_rows:
        lines.append(f"Пропущено строк без даты или времени: {stats.skipped_rows}")
    for title, counter in (("По тарифам", stats.by_tariff), ("По классам", stats.by_class),
                           ("По услугам", stats.by_service), ("По часам", stats.by_hour)):
        if counter:
            lines.append("")
            lines.append(title + ":")
            lines.extend(f"  {name}: {count}" for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0])))
    return "\n".join(lines)
