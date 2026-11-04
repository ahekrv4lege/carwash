"""Компактные факты клиента: модель выделяет смысл, Python проверяет источник и типы."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from sheets import digits, parse_date, parse_time, valid_phone


FIELDS = {"client_name", "phone", "car", "car_model", "size_class", "requested_service", "tariff",
          "services", "date", "time", "comment", "requested_datetime", "plate"}
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
        # Минуты перед «на» («в 10:00 на мойку») — не день месяца: двоеточие
        # перед числом исключает ложное «00 на».
        day_time = re.search(r"(?<![\d.:])(\d{1,2})(?:-?го)?\s+на\s+(?:\d{1,2}|[а-яё]+)\b", text)
        if day_time:
            return source_date(day_time[1], now)
        month_names = "|".join(month + r"\w*" for month in months)
        # В середине фразы dd.m без двузначного месяца — это чаще объём двигателя («3.5», «2.0»), а не дата.
        dates = re.findall(rf"\b(?:сегодня|завтра|послезавтра|{pattern}|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}\.\d{{2}}(?:\.\d{{4}})?|\d{{1,2}}(?:-?го)?\s+(?:{month_names})(?:\s+\d{{4}})?)\b", text)
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


def word_matches(word, other) -> bool:
    """Одно слово другому с точностью до падежа.

    Длинные слова сравниваются по первым 4 буквам; короткие — целиком или с
    падежным окончанием (до 2 букв), чтобы «фар» узнавалось в «фары».
    """
    if len(word) >= 5 or len(other) >= 5:
        return word[:4] == other[:4]
    return (word == other
            or other.startswith(word) and len(other) <= len(word) + 2
            or word.startswith(other) and len(word) <= len(other) + 2)


def inflection_grounded(value, source) -> bool:
    """Каждое слово значения есть в цитате клиента с точностью до падежа.

    «мойка кузова» и «мойку кузова» — одно и то же: совпадают первые 4 буквы слов.
    Короткие слова (до 4 букв) должны совпасть целиком. Пустое значение — не основание.
    """
    words = re.findall(r"[а-яёa-z0-9]+", folded(value or ""))
    source_words = re.findall(r"[а-яёa-z0-9]+", folded(source or ""))
    if not words or not source_words:
        return False
    for word in words:
        stem = word[:4] if len(word) >= 5 else word
        if not any((other[:4] if len(word) >= 5 else other) == stem for other in source_words):
            return False
    return True


def any_word_mentioned(name, source) -> bool:
    """Хотя бы одно слово значения названо в тексте (с точностью до падежа)."""
    words = re.findall(r"[а-яёa-z0-9]+", folded(name or ""))
    source_words = re.findall(r"[а-яёa-z0-9]+", folded(source or ""))
    return any(word_matches(word, other) for word in words for other in source_words)


def distinguishing_word_grounded(name, origin, source) -> bool:
    """Различающее слово уточняющего названия каталога названо клиентом.

    «Полировка фар» уточняет общий запрос «полировка»: если клиент назвал
    различающее слово («фары»), выбор варианта обоснован и без полного названия.
    Все слова названия должны быть известны: общие — из запроса, различающие —
    из текущего сообщения.
    """
    origin_stems = word_stems(origin or "")
    words = re.findall(r"[а-яёa-z0-9]+", folded(name or ""))
    if not words:
        return False
    source_words = re.findall(r"[а-яёa-z0-9]+", folded(source or ""))
    for word in words:
        stem = word[:4] if len(word) >= 5 else word
        if stem in origin_stems:
            continue
        if not any(word_matches(word, other) for other in source_words):
            return False
    return True


def word_stems(value) -> set[str]:
    """Нормализованные «основы» слов значения: регистр, ё и падежные окончания не важны."""
    return {(word[:4] if len(word) >= 5 else word)
            for word in re.findall(r"[а-яёa-z0-9]+", folded(value or ""))}


def stem_variants(request, names) -> list[str]:
    """Названия каталога, уточняющие общий запрос клиента.

    «полировка» уточняют «Полировка фар» и «Полировка кузова»: каждое слово
    запроса входит в название, но название содержит ещё и различающие слова.
    Совпадающие по составу слов названия («мойку» -> «Мойка») уточнением
    не считаются.
    """
    request_stems = word_stems(request)
    if not request_stems:
        return []
    return [name for name in names if request_stems < word_stems(name)]


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
            # Grounding car_model: значение должно быть дословными словами клиента,
            # а известная машина — согласованной с ним (цитата модели может быть короче).
            car_known = grounded.get("car") or known.get("car") or ""
            if (not car_known or folded(car_known) not in folded(source)) \
                    and folded(value) not in folded(text_value):
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
                inferred = origin and (folded(origin) in folded(source) or inflection_grounded(origin, source))
                direct = bool(matches) and (inflection_grounded(matches[0], source)
                                            or distinguishing_word_grounded(matches[0], origin, source))
                if len(matches) != 1 or not (carried or inferred or direct):
                    raise ValueError("unverified_catalog_value")
                if inferred and not carried and not direct and field in {"tariff", "services"}:
                    # Общие слова клиента («полировка») не должны молча превращаться
                    # в один из нескольких уточняющих вариантов каталога: это выбор
                    # за клиента. Точная цитата («полировку фар») выбор обосновывает.
                    if len(stem_variants(origin, names)) > 1:
                        raise ValueError("ambiguous_catalog_choice")
                    # Ровно совпадающее по словам название существует: выбор более
                    # узкого варианта («мойку» -> «Мойка двигателя» вместо «Мойка»)
                    # не обоснован словами клиента.
                    if (word_stems(matches[0]) != word_stems(origin)
                            and any(word_stems(name) == word_stems(origin) for name in names)):
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
        elif folded(value) not in folded(source) and not (field == "requested_service" and inflection_grounded(value, source)):
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
            state.pop("slot_pick", None)  # выбранный кнопками день больше не актуален
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


def model_state(state, plate_required: bool = False):
    result = {field_name: field_value for field_name, field_value in state.items() if field_name not in
              {"details", "facts", "tentative", "outstanding", "consumed_offers", "slot_menu", "slot_pick",
               "vehicle_question", "vehicle_family", "service_options", "vehicle_restored", "restored_vehicle",
               "profile_restored"}}
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
    # Госномер показывается только когда обязателен у этой автомойки; известный не повторяется.
    if plate_required and not facts.get("plate") and "plate" not in guesses:
        result["missing"].append("plate")
    if state.get("unavailable_time") == {field_name: facts.get(field_name) for field_name in ("date", "time")}:
        result["missing"].append("available_time")
    return result


_PLAIN_TABLE = str.maketrans({"—": "-", "–": "-", "«": '"', "»": '"', "“": '"', "”": '"'})


def plain_text(text):
    return text.translate(_PLAIN_TABLE)


_CLAIM_TEXT = "Оформление или отмену подтверждает система после вашего согласия. Пока результат операции не подтверждён; уточним детали."


_NEGATED_CLAIM = re.compile(
    r"\b(?:не|без|нельзя|невозможно|пока|если|после|когда|сначала)\b", re.I)


def safe_claim(text):
    """Причина, по которой текст выдаёт себя за результат операции, или None.

    Проверяются именно заявления о результате («вас записали», «запись создана»,
    «место забронировано»), а не любые слова «запись» или «записан». Поэтому
    «телефон записан», «запись подтверждается только кнопкой» и «запись пока
    не оформлена» остаются нормальными ответами.
    """
    if not isinstance(text, str) or not text:
        return None
    value = folded(text.translate(HIDDEN)).translate(CONFUSABLE)
    claims = (
        r"\bзаписа(?:ла|л)\s+(?:вас|вашу|клиента|на|в)\b",
        r"\bвас\s+записа(?:ла|л|ли)\b",
        r"\b(?:вас|вы)\s+(?:уже\s+)?(?:записали|записаны|оформили|оформлены)\b",
        r"\bуже\s+записан(?:ы|а|о)?\b",
        r"\b(?:создал[а]?|оформил[а]?|подтвердил[а]?|отменил[а]?|забронировал[а]?|"
        r"зарегистрировал[а]?)\s+(?:вас|вашу|запись|заказ|бронь|резерв|время|слот|место)\b",
        r"\b(?:добавил[а]?|изменил[а]?|обновил[а]?)\b[^.!?\n]{0,60}"
        r"\b(?:запись|записи|заказ|бронь)\b",
        r"\b(?:запись|бронь|бронирование|заявка|резерв\w*)\b.{0,80}"
        r"\b(?:создан\w*|оформлен\w*|подтвержден\w*|отменен\w*|принят\w*|"
        r"зарегистрирован\w*|внесен\w*|сохранен\w*|готов\w*)\b",
        r"\b(?:время|слот|место)\b.{0,60}"
        r"\b(?:забронирован\w*|закреплен\w*|зарезервирован\w*)\b",
        r"\b(?:успешно|уже)\s+(?:запис\w*|отмен\w*|забронир\w*)\b",
        r"\bжд[её]м вас\b",
    )
    for pattern in claims:
        for match in re.finditer(pattern, value, re.I | re.S):
            segment = match.group(0)
            before = value[max(0, match.start() - 50):match.start()]
            if _NEGATED_CLAIM.search(segment):
                continue
            if _NEGATED_CLAIM.search(before[-35:]):
                continue
            return "success_claim"
    # Настоящее время — тоже обещание результата, но только когда назван объект:
    # «записываю вас», «оформляю запись». «Записываю только после подтверждения»
    # и «не записываю без кнопки» — нормальные объяснения.
    present = re.search(
        r"\b(?:записываю|записываем|оформляю|оформляем|бронирую|бронируем|"
        r"резервирую|резервируем|запишу|оформлю|забронирую|зарезервирую)\s+"
        r"(?:вас|вашу|ваш|запись|заказ|бронь|время|слот|место|на|в)\b",
        value, re.I)
    if present:
        before = value[max(0, present.start() - 40):present.start()]
        tail = value[present.start():present.start() + 140]
        end = re.search(r"[.!?\n]", tail)
        sentence = tail[:end.start()] if end else tail
        question = bool(end and end.group() == "?")
        conditional = bool(re.search(r"\b(?:после|когда|если|потом|сначала)\b", sentence, re.I))
        if (not question and not conditional
                and not re.search(r"\b(?:не|без)\s+(?:\w+\s+){0,2}$", before, re.I)):
            return "success_claim"
    return None


def safe_reply(text):
    """Модель не является источником результата записи, даже после вычитки."""
    return text if safe_claim(text) is None else _CLAIM_TEXT


# Служебный текст не должен доходить до клиента: протокол памяти, названия
# инструментов, JSON, ошибки API, служебные поля и заголовки системного промпта.
# Детектор структурный (объекты с ключами в кавычках, snake_case, английские
# служебные слова), а не по отдельным «запрещённым фразам»: обычные русские
# ответы с ценами в кавычках он не задевает.
_TOOL_NAME = (r"\b(?:get_price_list|calculate_price|get_free_slots|get_my_bookings|"
              r"create_booking|cancel_booking|reschedule_booking|tool_calls|tool_choice|respond)\b")
_STATE_FIELD = (r"\b(?:car_model|size_class|client_name|requested_service|requested_datetime|"
                r"booking_id|phone_known|checked_at|free_times|not_included_now|table_unavailable|"
                r"confirmation_required|quote_changed|unavailable_services|display_text|tentative|respond)\b|"
                r"\bmissing_[a-z_]+\b")
_LEAK_PATTERNS = (
    re.compile(r'\{[^{}]*"[^"]+"\s*:|"(?:reply|facts|tentative)"\s*:'),
    re.compile(_TOOL_NAME, re.I),
    re.compile(_STATE_FIELD),
    re.compile(r"\b(?:CUSTOMER|STATE|TENTATIVE|FACTS|MISSING|RESPOND)\b"),
    re.compile(r"\b[a-z]+(?:_[a-z]+){2,}\b"),
    re.compile(r"\b\w*(?:Error|Exception|Traceback)\b|Traceback|status_code|asyncio\.|openai\.|json\.loads|gspread"),
    re.compile(r"\b(?:HTTP \d{3}|ошибка API|API-ключ|API key|spreadsheet|worksheet|credentials|сервисный аккаунт)\b", re.I),
    re.compile(r"\b(?:system prompt|системн\w+ промпт|"
               r"(?:мои|внутренние|системные|служебные)\s+инструкции|"
               r"инструкции\s+(?:для|от)\s+(?:модели|нейросети|разработчика)|"
               r"внутренн\w+ (?:инструмент|протокол|команд)|служебн\w+ (?:поле|данн|текст))\b"
               r"\s*[:=—-]\s*\S+", re.I),
    re.compile(r"\b(?:system prompt|системн\w+ промпт)\s+"
               r"(?:начинается|содержит|гласит|такой|следующий)\b", re.I),
    re.compile(r"^(?:РОЛЬ И КОНТЕКСТ|МАНЕРА РЕЧИ|ОФОРМЛЕНИЕ ДЛЯ TELEGRAM|КОНТЕКСТ И УТОЧНЕНИЯ|"
               r"КЛАСС И ПРАЙС|ДАТА И ВРЕМЯ|ЗАПИСЬ|ГРАНИЦЫ И ОШИБКИ|ПАМЯТЬ|ПРИМЕРЫ МАНЕРЫ|"
               r"АКТУАЛЬНЫЕ ДАННЫЕ АВТОМОЙКИ)\s*$", re.M),
)


def leak_reason(text):
    """Причина, по которой ответ модели нельзя показывать клиенту, или None."""
    if not isinstance(text, str) or not text:
        return None
    value = text.translate(HIDDEN)
    for pattern in _LEAK_PATTERNS:
        if pattern.search(value):
            return "internal_text"
    return None
