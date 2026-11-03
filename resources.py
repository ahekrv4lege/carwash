"""Явные окна работы и допуски именованных боксов."""
import re
from adaptive import MappingError


def parse_bays(values, classes, services):
    from sheets import parse_date, time_to_minutes
    if not values:
        raise MappingError("Лист ограничений боксов пуст")
    headers = [str(x).strip() for x in values[0]]
    if not {"Бокс", "Открытие поста", "Закрытие поста", "Статус"} <= set(headers):
        raise MappingError("Для боксов нужны Бокс, Открытие поста, Закрытие поста и Статус")
    result = []
    for row in values[1:]:
        if not any(str(x).strip() for x in row):continue
        r = dict(zip(headers, (str(x).strip() for x in row)))
        start, end = time_to_minutes(r.get("Открытие поста", "")), time_to_minutes(r.get("Закрытие поста", ""))
        day = parse_date(r["Дата"]) if r.get("Дата") else None
        if not r.get("Бокс") or start is None or end is None or start >= end or (r.get("Дата") and not day):
            raise MappingError("Некорректное окно или дата бокса")
        status = r.get("Статус", "").casefold()
        if status not in {"работает", "открыт", "доступен", "да", "open", "ремонт", "закрыт", "нет", "перерыв"}:
            raise MappingError("Неизвестный статус бокса")
        remaining = r.get("Ограничение", "").casefold()
        allowed_classes, allowed_services = set(), set()
        for name in classes:
            stem = name.casefold()
            if stem.startswith("легков"):stem = "легков"
            elif stem.startswith("кроссовер"):stem = "кроссовер"
            pattern = r"\b" + re.escape(stem) + r"\w*\b"
            if re.search(pattern, remaining):
                allowed_classes.add(name)
                remaining = re.sub(pattern, " ", remaining)
        for name in sorted(services, key=len, reverse=True):
            if name.casefold() in remaining:
                allowed_services.add(name)
                remaining = remaining.replace(name.casefold(), " ")
        remaining = re.sub(r"\b(?:только|и|или|все|любые|классы|услуги|автомобили|авто)\b", " ", remaining)
        if re.search(r"\w", remaining):
            raise MappingError("Неподдерживаемое ограничение бокса; задайте точные классы и услуги")
        result.append(dict(name=r["Бокс"], day=day, start=start, end=end,
                           open=status in {"работает", "открыт", "доступен", "да", "open"},
                           classes=allowed_classes, services=allowed_services))
    for i, a in enumerate(result):
        for b in result[i+1:]:
            same_day = a["day"] is None or b["day"] is None or a["day"] == b["day"]
            if (a["name"].casefold() == b["name"].casefold() and same_day and a["open"] and b["open"]
                    and a["start"] < b["end"] and b["start"] < a["end"]
                    and (a["classes"], a["services"]) != (b["classes"], b["services"])):
                raise MappingError("Противоречивые допуски пересекающихся окон одного бокса")
    return result


def eligible_bays(bays, day, start, end, size_class, services):
    from availability import interval_problem
    result = []
    for name in sorted({b["name"] for b in bays}):
        rows = [b for b in bays if b["name"] == name and b["day"] in (None, day)]
        if any(not b["open"] and b["start"] < end and start < b["end"] for b in rows):continue
        windows = [(b["start"],b["end"],1) for b in rows if b["open"]
                   and (not b["classes"] or size_class in b["classes"])
                   and (not b["services"] or set(services) <= b["services"])]
        if interval_problem(windows, [], start, end) is None:result.append(name)
    return result
