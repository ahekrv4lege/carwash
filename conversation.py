"""Компактные факты клиента: модель выделяет смысл, Python проверяет источник и типы."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from sheets import digits, parse_date, parse_time, valid_phone


FIELDS = {"client_name", "phone", "car", "car_model", "size_class", "requested_service", "tariff",
          "services", "date", "time", "comment", "requested_datetime"}
CATALOG_FIELDS = {"size_class", "tariff", "services"}
QUOTE_FIELDS = {"car", "size_class", "tariff", "services", "requested_service"}
HIDDEN = dict.fromkeys([*range(0x200b, 0x2010), *range(0x202a, 0x202f), *range(0x2060, 0x2070), 0xfeff])
CONFUSABLE = str.maketrans('aceopxy', 'асеорху')
PHONE = re.compile(r"\+?\d[\d\s().\-\u2010-\u2015\u2212\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]{8,}\d(?![\w:])")


def strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_field")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("non_finite_json")
    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid)


def folded(value):
    return " ".join(value.casefold().replace("ё", "е").split())


def same(field, left, right):
    if field == "phone":
        return digits(left) == digits(right)
    if field == "services":
        return sorted(map(folded, left or [])) == sorted(map(folded, right or []))
    return folded(left or "") == folded(right or "")


def unpack(content):
    """Пустой content допустим рядом с tools; свободный текст не теряет факты молча."""
    text = (content or "").strip()
    if not text:
        return "", {}, {}
    if not text.startswith(("{", "```")):
        raise ValueError("missing_memory_envelope")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    data = strict_json(text)
    if not isinstance(data, dict) or set(data) - {"reply", "facts", "tentative"}:
        raise ValueError("invalid_memory_envelope")
    reply = data.get("reply", "")
    if not isinstance(reply, str):
        raise ValueError("invalid_reply")
    return reply, data.get("facts", {}), data.get("tentative", {})


def source_date(source, now, embedded=False):
    text = folded(source)
    relative = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
    if text in relative:
        return now.date() + timedelta(days=relative[text])
    day_only = re.fullmatch(r"(\d{1,2})(?:-?го)?", text)
    if day_only:
        try:
            return date(now.year, now.month, int(day_only[1]))
        except ValueError:
            return None
    short = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", text)
    if short:
        try:
            return date(now.year, int(short[2]), int(short[1]))
        except ValueError:
            return None
    pattern = r"понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббот[ау]|воскресенье"
    months = ("январ", "феврал", "март", "апрел", "ма[йя]", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр")
    natural = re.fullmatch(r"(\d{1,2})(?:-?го)?\s+(" + "|".join(month + r"\w*" for month in months) + r")(?:\s+(\d{4})(?:\s*г(?:ода|\.)?)?)?", text)
    if natural:
        month = next(i + 1 for i, name in enumerate(months) if re.fullmatch(name + r"\w*", natural[2]))
        try:
            return date(int(natural[3]) if natural[3] else now.year, month, int(natural[1]))
        except ValueError:
            return None
    if embedded:
        day_time = re.search(r"(?<![\d.])(\d{1,2})(?:-?го)?\s+на\s+(?:\d{1,2}|[а-яё]+)\b", text)
        if day_time:
            return source_date(day_time[1], now)
        month_names = "|".join(month + r"\w*" for month in months)
        dates = re.findall(rf"\b(?:сегодня|завтра|послезавтра|{pattern}|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}\.\d{{1,2}}(?:\.\d{{4}})?|\d{{1,2}}(?:-?го)?\s+(?:{month_names})(?:\s+\d{{4}})?)\b", text)
        dates = [value for value in dates if source_date(value, now) is not None]
        return source_date(dates[0], now) if len(dates) == 1 else None
    weekday = re.fullmatch(rf"(?:в |во )?({pattern})", text)
    if weekday:
        names = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресенье")
        day = next(index for index, name in enumerate(names) if weekday[1].startswith(name))
        return now.date() + timedelta(days=(day - now.weekday()) % 7)
    return parse_date(source)


def source_time(source, embedded=False):
    text = folded(source)
    words = {"час": 1, "один": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
             "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
             "десять": 10, "одиннадцать": 11, "двенадцать": 12}
    token = r"(?:\d{1,2}(?:[:.]\d{2})?|" + "|".join(words) + r")"
    if embedded:
        clocks = re.findall(r"\b(?:в|к|на)\s+(" + token + r")(?![\w:])", text)
        if re.search(r"\b(?:на|завтра|сегодня|послезавтра|запиш\w*|перенес\w*|перенос\w*|тогда|вместо)\b|\d{1,2}\.\d{1,2}", text) or PHONE.search(text):
            clocks += re.findall(r"(?<![\w:.])(\d{1,2}:\d{2})(?![\d:])", text)
        clocks = list(dict.fromkeys(value for clock in clocks if (value := source_time(clock)) is not None))
        if len(clocks) == 1:
            hour, minute = map(int, clocks[0].split(":"))
            if re.search(r"\b(?:вечер\w*|дня)\b", text) and 1 <= hour <= 11:
                hour += 12
            elif re.search(r"\bноч\w*\b", text) and hour == 12:
                hour = 0
            return f"{hour:02d}:{minute:02d}"
        return source_time(text)
    clock = re.fullmatch(r"(?:в\s*|к\s*|на\s*)?(" + token + r")(?:\s*(?:час(?:а|ов)?|ч))?(?:\s+(?:утра|дня|вечера|ночи))?", text)
    if not clock:
        return None
    raw = clock[1]
    value = parse_time(f"{words[raw]}:00") if raw in words else parse_time(raw if ":" in raw or "." in raw else f"{raw}:00")
    if value and re.search(r"\b(?:вечера|дня)\b", text) and 1 <= int(value[:2]) <= 11:
        value = f"{int(value[:2]) + 12:02d}:{value[3:]}"
    return value


def literal_car_patch(patch, text):
    entry = patch.get("car") if isinstance(patch, dict) else None
    model = patch.get("car_model") if isinstance(patch, dict) else None
    if (isinstance(entry, list) and len(entry) == 2 and all(isinstance(value, str) and value.strip() for value in entry)
            and isinstance(model, list) and len(model) == 2 and entry == model
            and folded(entry[1]) in folded(text) and folded(entry[0]) not in folded(entry[1])):
        return dict(patch, car=[entry[1], entry[1]])
    if (not isinstance(entry, list) or len(entry) != 2 or not all(isinstance(value, str) for value in entry)
            or folded(entry[0]) != folded(entry[1]) or folded(entry[1]) in folded(text)):
        return patch
    words = re.findall(r"[^\W_]+", entry[1])
    if not words:
        return patch
    pattern = r"\s+".join(re.escape(word) + (r"(?:а|я|у|ю|ом|ем|е|ы|и)?"
        if re.fullmatch(r"[а-яё]{4,}", word, re.I) else "") for word in words)
    matches = list(re.finditer(r"(?<!\w)" + pattern + r"(?!\w)", text, re.I))
    if len(matches) != 1:
        return patch
    literal = matches[0].group()
    result = dict(patch)
    for field in ("car", "car_model", "size_class"):
        value = patch.get(field)
        if isinstance(value, list) and len(value) == 2 and isinstance(value[1], str) and folded(value[1]) == folded(entry[1]):
            result[field] = [literal if field == "car" else value[0], literal]
    return result


def validate_patch(patch, text, now, catalog=None, tentative=False, known=None):
    if not isinstance(patch, dict) or set(patch) - FIELDS:
        raise ValueError("invalid_fact_fields")
    result = {}
    known = known or {}
    text_value = folded(text) if patch else ""
    grounded = dict(known)
    for field in ("car", "requested_service"):
        entry = patch.get(field)
        if (isinstance(entry, list) and len(entry) == 2 and all(isinstance(value, str) and value.strip() for value in entry)
                and folded(entry[0]) in folded(entry[1]) and folded(entry[1]) in text_value):
            grounded[field] = entry[0]
    for field, entry in patch.items():
        if not isinstance(entry, list) or len(entry) != 2:
            raise ValueError("invalid_fact_entry")
        value, source = entry
        car_source = (field in {"car_model", "size_class"} and value is not None and known.get("car")
                      and isinstance(source, str) and folded(source) == folded(known["car"]))
        if not tentative and not car_source and (not isinstance(source, str) or not source.strip() or folded(source) not in text_value):
            raise ValueError("missing_customer_source")
        if value is None:
            result[field] = None
            continue
        if field == "services":
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or len(value) > 30 or any(not isinstance(field_value, str) for field_value in value):
                raise ValueError("invalid_services")
        elif not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise ValueError("invalid_fact_value")
        else:
            value = value.strip()
        if tentative:
            result[field] = value
            continue
        if field == "car_model":
            if not grounded.get("car") or folded(grounded["car"]) not in folded(source):
                raise ValueError("missing_car_source")
        elif field in CATALOG_FIELDS:
            if catalog is None:
                raise ValueError("catalog_required")
            names = (catalog.size_classes if field == "size_class" else
                     [service.name for service in catalog.services.values()] if field == "services" else
                     [tariff.name for tariff in catalog.tariffs.values()])
            values = value if field == "services" else [value]
            canonical = []
            for raw in values:
                matches = [name for name in names if folded(name) == folded(raw)]
                previous = known.get(field, []) if field == "services" else [known.get(field, "")]
                carried = any(folded(raw) == folded(field_value) for field_value in previous)
                origin = grounded.get("car" if field == "size_class" else "requested_service", "")
                inferred = origin and folded(origin) in folded(source)
                if len(matches) != 1 or not (carried or inferred) and folded(matches[0]) not in folded(source):
                    raise ValueError("unverified_catalog_value")
                canonical.append(matches[0])
            value = list(dict.fromkeys(canonical)) if field == "services" else canonical[0]
        elif field == "phone":
            if not valid_phone(value) or digits(value) != digits(source):
                raise ValueError("invalid_phone_source")
        elif field == "date":
            parsed_date, origin = parse_date(value), source_date(source, now, embedded=True)
            if not parsed_date or parsed_date != origin:
                raise ValueError("invalid_date_source")
            value = parsed_date.isoformat()
        elif field == "time":
            if not re.fullmatch(r"\s*\d{1,2}[:.]\d{2}(?::00)?\s*", value):
                raise ValueError("invalid_time_source")
            value = parse_time(value)
            if not value or value != source_time(source, embedded=True):
                raise ValueError("invalid_time_source")
        elif folded(value) not in folded(source):
            raise ValueError("invented_customer_fact")
        result[field] = value
    return result


def salvage_patch(patch, text, now, catalog=None, tentative=False, known=None):
    if not isinstance(patch, dict):
        raise ValueError("invalid_fact_fields")
    result = {}
    for field in sorted(patch, key=lambda name: name not in {"car", "requested_service"}):
        try:
            result.update(validate_patch({field: patch[field]}, text, now, catalog, tentative,
                                         {**(known or {}), **result}))
        except (ValueError, TypeError):
            continue
    return result


def apply_patch(state, changes, tentative):
    facts = state.setdefault("facts", {})
    guesses = state.setdefault("tentative", {})
    invalidated = set(state.get("invalidated", []))
    changed = {field_name for field_name, field_value in changes.items() if not same(field_name, facts.get(field_name), field_value)}
    if "requested_service" in changed and not ({"tariff", "services"} & changes.keys()):
        changes = dict(changes, tariff=None, services=None)
        changed.update({"tariff", "services"})
    if "car" in changed and facts.get("car") and "size_class" not in changes:
        changes = dict(changes, size_class=None)
        changed.add("size_class")
    if "car" in changed and facts.get("car_model") and "car_model" not in changes:
        changes = dict(changes, car_model=None)
    if "tariff" in changed and facts.get("tariff") and "services" not in changes:
        changes = dict(changes, services=[])
        changed.add("services")
    for field, value in changes.items():
        if value is None:
            facts.pop(field, None)
            invalidated.add(field)
        else:
            facts[field] = value
            invalidated.discard(field)
        guesses.pop(field, None)
    for field, value in tentative.items():
        if value is None:
            guesses.pop(field, None)
        else:
            guesses[field] = value
    if changed:
        state.update(stage="collecting", confirmed=False)
        state.pop("details", None)
        state.pop("outstanding", None)
        if changed & QUOTE_FIELDS:
            state.pop("quote", None)
            state.pop("selection", None)
        if "date" in changed:
            state.pop("availability", None)
        if {"date", "time"} & changed:
            state.pop("unavailable_time", None)
    if not guesses:
        state.pop("tentative", None)
    if invalidated:
        state["invalidated"] = sorted(invalidated)
    else:
        state.pop("invalidated", None)


def redact_phone(text, known=None):
    number = digits(known or "")
    def hide(match):
        value = match[0]
        positions = [index for index, character in enumerate(value) if character.isdecimal()]
        dense = "".join(value[index] for index in positions)
        start = dense.find(number) if number else -1
        if start >= 0:
            first, last = positions[start], positions[start + len(number) - 1] + 1
            if first and value[first-1] == "+":
                first -= 1
            return value[:first] + "[телефон скрыт]" + value[last:]
        if match.start() and text[match.start()-1].isalnum():
            return value
        return "[телефон скрыт]" if 10 <= len(dense) <= 15 else value
    return PHONE.sub(hide, text)


def model_state(state):
    result = {field_name: field_value for field_name, field_value in state.items() if field_name not in {"details", "facts", "tentative", "outstanding", "consumed_offers"}}
    for key in ("facts", "tentative"):
        values = {field_name: field_value for field_name, field_value in state.get(key, {}).items() if field_name != "phone"}
        if state.get(key, {}).get("phone"):
            values["phone_known"] = True
        if values:
            result[key] = values
    selection = result.get("selection")
    if selection and all(same(field_name, field_value, state.get("facts", {}).get(field_name)) for field_name, field_value in selection.items()):
        result.pop("selection")
    facts = state.get("facts", {})
    guesses = state.get("tentative", {})
    result["missing"] = [field_name for field_name in ("client_name", "phone", "car", "size_class", "date", "time")
                         if not facts.get(field_name) or field_name in guesses or field_name in {"date", "time"} and "requested_datetime" in guesses]
    if not facts.get("tariff") and not facts.get("services") or "requested_service" in guesses:
        result["missing"].append("tariff_or_services")
    if state.get("unavailable_time") == {field_name: facts.get(field_name) for field_name in ("date", "time")}:
        result["missing"].append("available_time")
    return result


def plain_text(text):
    return text.translate(str.maketrans({"—": "-", "–": "-", "«": '"', "»": '"', "“": '"', "”": '"'}))


def safe_reply(text):
    """Модель не является источником результата записи, даже после вычитки."""
    value = folded(text.translate(HIDDEN)).translate(CONFUSABLE)
    claim = (r"\bзапис(?:ал[аи]?|аны?|ано|ала|ался|ались)\b|"
             r"\b(?:запись|бронь|бронирование|заявка|резерв\w*)\b.{0,90}"
             r"\b(?:создан\w*|оформлен\w*|подтвержден\w*|отменен\w*|готов\w*|успеш\w*|зарегистрирован\w*|принят\w*|сохранен\w*|внесен\w*)\b|"
             r"\b(?:создал\w*|оформил\w*|подтвердил\w*|отменил\w*|забронировал\w*|зарегистрировал\w*)\b|"
             r"\b(?:успешно|уже)\s+(?:запис\w*|отмен\w*|забронир\w*)\b|"
             r"\b(?:время|слот|место)\b.{0,60}\b(?:забронирован\w*|закреплен\w*|зарезервирован\w*)\b")
    if re.search(claim, value, re.S) or re.search(r"\b(?:готово|оформлено|заброниров\w*|отменена|зарезервиров\w*|зафиксировал\w*)\b|\bждем вас\b", value):
        return "Оформление или отмену подтверждает система после вашего согласия. Пока результат операции не подтверждён; уточним детали."
    return text
