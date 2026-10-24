"""Компактные факты клиента: модель выделяет смысл, Python проверяет источник и типы."""
from __future__ import annotations

import json
import re
from datetime import timedelta

from sheets import digits, parse_date, parse_time, valid_phone


FIELDS = {"client_name", "phone", "car", "size_class", "requested_service", "tariff",
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


def validate_patch(patch, text, now, catalog=None, tentative=False, known=None):
    if not isinstance(patch, dict) or set(patch) - FIELDS:
        raise ValueError("invalid_fact_fields")
    result = {}
    known = known or {}
    text_value = folded(text) if patch else ""
    for field, entry in patch.items():
        if not isinstance(entry, list) or len(entry) != 2:
            raise ValueError("invalid_fact_entry")
        value, source = entry
        if not tentative and (not isinstance(source, str) or not source.strip() or folded(source) not in text_value):
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
        if field in CATALOG_FIELDS:
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
                if len(matches) != 1 or not carried and folded(matches[0]) not in folded(source):
                    raise ValueError("unverified_catalog_value")
                canonical.append(matches[0])
            value = list(dict.fromkeys(canonical)) if field == "services" else canonical[0]
        elif field == "phone":
            if not valid_phone(value) or digits(value) != digits(source):
                raise ValueError("invalid_phone_source")
        elif field == "date":
            parsed_date, origin = parse_date(value), parse_date(source)
            relative = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
            if folded(source) in relative:
                origin = now.date() + timedelta(days=relative[folded(source)])
            if not parsed_date or parsed_date != origin:
                raise ValueError("invalid_date_source")
            value = parsed_date.isoformat()
        elif field == "time":
            if any(not re.fullmatch(r"\s*\d{1,2}[:.]\d{2}(?::00)?\s*", field_value) for field_value in (source, value)):
                raise ValueError("invalid_time_source")
            value = parse_time(value)
            if not value or value != parse_time(source):
                raise ValueError("invalid_time_source")
        elif folded(value) not in folded(source):
            raise ValueError("invented_customer_fact")
        result[field] = value
    return result


def apply_patch(state, changes, tentative):
    facts = state.setdefault("facts", {})
    guesses = state.setdefault("tentative", {})
    invalidated = set(state.get("invalidated", []))
    changed = {field_name for field_name, field_value in changes.items() if not same(field_name, facts.get(field_name), field_value)}
    if "requested_service" in changed and not ({"tariff", "services"} & changes.keys()):
        changes = dict(changes, tariff=None, services=None)
        changed.update({"tariff", "services"})
    if "requested_datetime" in changed and not ({"date", "time"} & changes.keys()):
        changes = dict(changes, date=None, time=None)
        changed.update({"date", "time"})
    if "car" in changed and facts.get("car") and "size_class" not in changes:
        changes = dict(changes, size_class=None)
        changed.add("size_class")
    if "date" in changed and facts.get("date") and "time" not in changes:
        changes = dict(changes, time=None)
        changed.add("time")
    if "services" in changed and "tariff" not in changes:
        changes = dict(changes, tariff=None)
        changed.add("tariff")
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
    result = {field_name: field_value for field_name, field_value in state.items() if field_name not in {"details", "facts", "tentative", "outstanding"}}
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
