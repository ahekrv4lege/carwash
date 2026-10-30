"""Испорченные таблицы: общие сценарии для тестов (test_corrupted_tables.py).

Для каждого сценария портится копия синтетической таблицы из fakes.fixture(),
затем вызывается штатный метод Store и сравнивается итог с ожиданием:
"mapping-error" (внятный отказ адаптивного слоя), "ok"/"failure"/"value"
(штатный результат Store) либо "ok-or-mapping-error" (крайние случаи, которые
уточняются эмпирически и закрепляются после первого прогона).
Недопустимо: чужие исключения (KeyError, TypeError...) и молчаливое принятие
заведомо неверных данных — это ловится assert'ами в verify().

Координаты: листовые (row, col) начиная с 1 — как в Google Sheets.

Наблюдаемые и закреплённые здесь поведенческие соглашения (см. tests):
- bookings_bad_date «32.13.2099» → «Дата не соответствует выбранному преобразованию»;
- slots_bad_time «9:70» → «Время не соответствует выбранному преобразованию»;
- bookings_formula_blocks_cancel → «Запись в выбранную область не разрешена»;
- services_headers_lowercase → «требуется проверка mappings»;
- services_word_price «семьсот» → колонка цены выбрасывается, прайс без класса,
 мусор никогда не становится числом (проверяется verify).
"""
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable

from adaptive import MappingError
from tests.fakes import booking, fixture


@dataclass
class Scenario:
    id: str                                    # имя теста и раздел отчёта
    title: str                                 # человекочитаемое описание
    expect: str                                # mapping-error | ok | failure | value | ok-or-mapping-error
    action: Callable[[Any], Any]               # что проверяем после порчи
    expect_message: str = ""                   # подстрока сообщения MappingError
    disrupt: Callable[[Any, Any], None] | None = None    # как портим таблицу
    warmup: Callable[[Any], None] | None = None          # подготовка ДО порчи (mappings)
    verify: Callable[[Any], None] | None = None          # уточняющий контроль результата
    legacy: bool = False                       # сценарий офлайн-пути без адаптива


# ─────────────────────── механика порчи ───────────────────────

def set_cell(spreadsheet, title: str, row: int, col: int, value) -> None:
    spreadsheet.worksheet(title).values[row - 1][col - 1] = value


def append_row_copy(spreadsheet, title: str, source_row: int) -> None:
    """Полный дубль строки — базовая порча повторами."""
    worksheet = spreadsheet.worksheet(title)
    worksheet.values.append(list(worksheet.values[source_row - 1]))


def append_column(spreadsheet, title: str, header: str, cells: list) -> None:
    """Новая колонка: шапка в первой строке, значения со второй; пустые хвосты дополняются."""
    worksheet = spreadsheet.worksheet(title)
    tail = max(len(row) for row in worksheet.values)
    worksheet.values[0] = list(worksheet.values[0]) + [""] * (tail - len(worksheet.values[0])) + [header]
    for index, value in enumerate(cells, start=2):
        row = list(worksheet.values[index - 1]) if index - 1 < len(worksheet.values) else []
        row += [""] * (tail - len(row)) + [value]
        worksheet.values[index - 1] = row


def column_number(spreadsheet, title: str, header: str) -> int:
    """Порядковый номер колонки по названию без учёта регистра и пробелов."""
    headers = spreadsheet.worksheet(title).values[0]
    for index, item in enumerate(headers, 1):
        if str(item).strip().lower() == header.strip().lower():
            return index
    raise AssertionError(f"Нет колонки «{header}» на листе «{title}»: {headers}")


def booking_created(store) -> None:
    """Сценарии листа «Записи» стартуют с корректно созданной записи."""
    result = store.create_booking(**booking())
    if not result.get("ok"):
        raise AssertionError(f"Сценарий не смог создать базовую запись: {result}")


def make_booking_cell(spreadsheet, row: int, header: str, value) -> None:
    set_cell(spreadsheet, "Записи", row, column_number(spreadsheet, "Записи", header), value)


# ─────────────────────── действия проверки ───────────────────────

def act_price(store):
    return store.price_list()


def act_calculate(store):
    """Отдельная услуга «Мойка» на классе «Легковой» без тарифа."""
    return store.calculate_price("Легковой", None, ["Мойка"])


def act_tariff(store):
    """Тариф «Базовый» (состав: Мойка + Воск) без доп. услуг."""
    return store.calculate_price("Легковой", "Базовый", [])


def act_free(store):
    return store.free_slots("2099-01-01")


def act_slots(store):
    store._invalidate("slots")
    return {(key[0].isoformat(), key[1]): count for key, count in store._get_slots().items()}


def act_my(store):
    return store.my_bookings(111)


def act_cancel(store):
    return store.cancel_booking(1, 111)


# ─────────────────────── проверочные замыкания ───────────────────────

def has_substring(fragment: str):
    def verify(result):
        text = str(result.get("error", "")) if isinstance(result, dict) else str(result)
        assert fragment in text, f"Ожидали «{fragment}» в ответе, пришло: {text}"
    return verify


def priced(total: int):
    def verify(result):
        assert result.get("ok"), f"Ожидали расчёт, пришёл отказ: {result}"
        assert result.get("total") == total, f"Ожидали итог {total}, пришло {result.get('total')}"
    return verify


def slots_sum(expected: dict):
    def verify(slots):
        assert slots == expected, f"Ожидали слоты {expected}, пришло {slots}"
    return verify


def garbage_price_becomes_nothing():
    """«семьсот» не является ценой: колонка выбрасывается, число не подменяется."""
    def verify(result):
        assert result.get("ok"), f"Ожидали прайс, пришёл отказ: {result}"
        find = next(item for item in result["services"] if item["name"] == "Мойка")
        assert "легковой" not in {key.casefold() for key in find["prices"]}, \
            f"Мусорная цена почему-то стала числом: {find['prices']}"
    return verify


def polishing_removed():
    """«Полировка» из чужого состава тарифа должна честно исчезнуть из расчёта."""
    def verify(result):
        assert result.get("ok"), f"Ожидали расчёт, пришёл отказ: {result}"
        line = result["lines"][0]
        assert "Полировка" in [str(item) for item in line.get("not_included_now", [])], \
            f"«Полировка» должна отображаться в not_included_now, линия: {line}"
    return verify


def no_negative_total():
    """Отрицательные цены не должны пробираться в суммы (офлайн-путь)."""
    def verify(result):
        if result.get("ok"):
            assert result.get("total", 0) > 0, f"Отрицательная сумма пробралась в итог: {result}"
            for line in result.get("lines", []):
                assert line.get("price", 0) > 0, f"Отрицательная строка в расчёте: {line}"
    return verify


def service_price_not_offered():
    """При отказе расчёта услуга должна честно называться «не оказывается для класса»."""
    def verify(result):
        assert result.get("ok") is False, f"Ожидали отказ, пришёл расчёт: {result}"
        assert "не оказывается" in result.get("error", ""), f"Ожидали объяснение отказа: {result}"
    return verify


# ─────────────────────── пространство workspace ───────────────────────

class Workspace:
    """Свежая временная копия таблиц и своя папка mappings, чтобы сценарии не мешали друг другу."""

    def __init__(self, legacy: bool = False):
        self.root = Path(tempfile.mkdtemp(prefix="corrupt-tables-"))
        self.store, self.spreadsheet = fixture(self.root, sheets_adaptive_enabled=not legacy)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def run_scenario(scenario: Scenario) -> dict:
    """Заводит чистые таблицы, портит их по сценарию и возвращает исход."""
    workspace = Workspace(legacy=scenario.legacy)
    try:
        store, spreadsheet = workspace.store, workspace.spreadsheet
        if scenario.warmup:
            scenario.warmup(store)
        if scenario.disrupt:
            scenario.disrupt(spreadsheet, store)
        try:
            result = scenario.action(store)
        except MappingError as error:
            return {"kind": "mapping-error", "message": str(error)}
        if isinstance(result, dict) and "ok" in result:
            return {"kind": "ok" if result["ok"] else "failure", "result": result}
        return {"kind": "value", "result": result}
    finally:
        workspace.cleanup()


def warmup_mappings(store) -> None:
    """Подтверждает mappings ДО порчи — так тестируется поток изменения структуры."""
    store.refresh_schema(force=True, validate=True)


SCENARIOS: list[Scenario] = [
    # ── лист «Услуги»: пустые поля ──
    Scenario("services_empty_name", "Пустое название услуги",
             "mapping-error", act_price, expect_message="Пустые или повторяющиеся названия",
             disrupt=lambda sheet, _store: set_cell(sheet, "Услуги", 2, 1, "")),
    Scenario("services_empty_price", "Пустая цена: услуга не оказывается для класса",
             "failure", act_calculate, verify=service_price_not_offered(),
             disrupt=lambda sheet, _store: set_cell(sheet, "Услуги", 2, 3, "")),
    Scenario("services_empty_availability", "Пустая «Доступна» означает «доступна»",
             "ok", act_calculate, verify=priced(700),
             disrupt=lambda sheet, _store: set_cell(sheet, "Услуги", 2, 5, "")),

    # ── лист «Услуги»: мусор в значениях ──
    Scenario("services_garbage_availability", "Мусор в «Доступна» («может быть»)",
             "mapping-error", act_price, expect_message="Неизвестное значение доступности",
             disrupt=lambda sheet, _store: set_cell(sheet, "Услуги", 2, 5, "может быть")),
    Scenario("services_negative_price", "Отрицательная цена («-700»)",
             "mapping-error", act_price, expect_message="Некорректная цена услуги",
             disrupt=lambda sheet, _store: set_cell(sheet, "Услуги", 2, 3, "-700")),
    Scenario("services_word_price", "Цена словами («семьсот»)",
             "ok", act_price, verify=garbage_price_becomes_nothing(),
             disrupt=lambda sheet, _store: set_cell(sheet, "Услуги", 2, 3, "семьсот")),
    Scenario("services_price_with_currency", "Цена с пробелом и знаком валюты («1 700 ₽»)",
             "ok", act_calculate, verify=priced(1700),
             disrupt=lambda sheet, _store: set_cell(sheet, "Услуги", 2, 3, "1 700 ₽")),
    Scenario("services_zero_price", "Нулевая цена («0»): расчёт в 0 ₽ допускается",
             "ok", act_calculate, verify=priced(0),
             disrupt=lambda sheet, _store: set_cell(sheet, "Услуги", 2, 3, "0")),
    Scenario("services_formula_price", "Цена как формула («=B2*2»)",
             "ok-or-mapping-error", act_calculate,
             disrupt=lambda sheet, _store: set_cell(sheet, "Услуги", 2, 3, "=B2*2")),

    # ── лист «Услуги»: повторы ──
    Scenario("services_duplicate_rows", "Две услуги с одним названием",
             "mapping-error", act_price, expect_message="Пустые или повторяющиеся названия",
             disrupt=lambda sheet, _store: append_row_copy(sheet, "Услуги", 2)),

    # ── лист «Услуги»: шапки (mappings созданы заранее, порча после работы) ──
    Scenario("services_duplicate_class_column", "Две колонки одного класса «Легковой»",
             "mapping-error", act_price,
             warmup=warmup_mappings,
             disrupt=lambda sheet, _store: append_column(sheet, "Услуги", "Легковой", [700, 200])),
    Scenario("services_extra_text_column", "Чужая колонка «Заметки» с текстом не должна стать классом",
             "ok-or-mapping-error", act_price,
             warmup=warmup_mappings,
             disrupt=lambda sheet, _store: append_column(sheet, "Услуги", "Заметки", ["кошки", "собаки"])),
    Scenario("services_headers_lowercase", "Вся шапка в нижнем регистре",
             "mapping-error", act_price, expect_message="требуется проверка mappings",
             warmup=warmup_mappings,
             disrupt=lambda sheet, _store: set_lowercase_headers(sheet)),
    Scenario("services_swapped_class_columns", "Колонки классов поменялись местами",
             "ok", act_calculate, verify=priced(700),
             warmup=warmup_mappings,
             disrupt=lambda sheet, _store: swap_class_columns(sheet)),

    # ── лист «Тарифы» ──
    Scenario("tariffs_duplicate_composition", "Одна услуга дважды в составе тарифа",
             "mapping-error", act_tariff, expect_message="Повторяющиеся услуги в составе тарифа",
             disrupt=lambda sheet, _store: set_cell(sheet, "Тарифы", 2, 2, "Мойка, Мойка")),
    Scenario("tariffs_discount_over_100", "Скидка больше 100% («150»)",
             "mapping-error", act_tariff, expect_message="Некорректная скидка",
             disrupt=lambda sheet, _store: set_cell(sheet, "Тарифы", 2, 3, "150")),
    Scenario("tariffs_unknown_service", "В составе тарифа услуга, которой нет в прайсе",
             "ok", act_tariff, verify=polishing_removed(),
             disrupt=lambda sheet, _store: set_cell(sheet, "Тарифы", 2, 2, "Мойка, Полировка")),

    # ── лист «Слоты» ──
    Scenario("slots_bad_time", "Время «9:70»",
             "mapping-error", act_free, expect_message="Время не соответствует",
             disrupt=lambda sheet, _store: set_cell(sheet, "Слоты", 2, 2, "9:70")),
    Scenario("slots_negative_posts", "Постов «-1»",
             "mapping-error", act_free, expect_message="Некорректное количество постов",
             disrupt=lambda sheet, _store: set_cell(sheet, "Слоты", 2, 3, "-1")),
    Scenario("slots_fractional_posts", "Постов «1.5»",
             "mapping-error", act_free, expect_message="Некорректное количество постов",
             disrupt=lambda sheet, _store: set_cell(sheet, "Слоты", 2, 3, "1.5")),
    Scenario("slots_empty_posts", "Пустое «Постов»",
             "mapping-error", act_free, expect_message="Некорректное количество постов",
             disrupt=lambda sheet, _store: set_cell(sheet, "Слоты", 2, 3, "")),
    Scenario("slots_duplicate_rows", "Дубль строки слота — вместимость суммируется (2+2)",
             "value", act_slots, verify=slots_sum({("2099-01-01", "10:00"): 4}),
             disrupt=lambda sheet, _store: append_row_copy(sheet, "Слоты", 2)),

    # ── лист «Записи» (стартуют с корректно созданной записи) ──
    Scenario("bookings_bad_date", "Дата строки записи «32.13.2099»",
             "mapping-error", act_my, expect_message="Дата не соответствует",
             disrupt=lambda sheet, store: (
                 booking_created(store),
                 make_booking_cell(sheet, 2, "Дата", "32.13.2099"))),
    Scenario("bookings_duplicate_ids", "Две записи с одним ID",
             "mapping-error", act_my, expect_message="повторяется идентификатор",
             disrupt=lambda sheet, store: (
                 booking_created(store),
                 append_row_copy(sheet, "Записи", 2))),
    Scenario("bookings_formula_blocks_cancel", "Формула в строке записи блокирует отмену («=700» в Сумме)",
             "mapping-error", act_cancel, expect_message="Запись в выбранную область не разрешена",
             disrupt=lambda sheet, store: (
                 booking_created(store),
                 make_booking_cell(sheet, 2, "Сумма", "=700"))),

    # ── офлайн-путь (SHEETS_ADAPTIVE_ENABLED=false): мягкая валидация ──
    Scenario("legacy_negative_price", "Офлайн-прайс: «-700» не должен давать отрицательную сумму",
             "failure", act_calculate, verify=no_negative_total(), legacy=True,
             disrupt=lambda sheet, _store: set_cell(sheet, "Услуги", 2, 3, "-700")),
    Scenario("legacy_duplicate_class_column", "Офлайн-прайс: две колонки «Легковой» не дублируют класс",
             "ok", act_price, legacy=True,
             disrupt=lambda sheet, _store: append_column(sheet, "Услуги", "Легковой", [700, 200])),
    Scenario("legacy_availability_garbage", "Офлайн-прайс: чужое «Доступна» означает «доступна» (толерантный путь)",
             "ok", act_calculate, legacy=True,
             disrupt=lambda sheet, _store: set_cell(sheet, "Услуги", 2, 5, "может быть")),
]


def set_lowercase_headers(sheet) -> None:
    for col, lowercase in ((1, "услуга"), (2, "описание"), (3, "легковой"), (4, "кроссовер"), (5, "доступна")):
        set_cell(sheet, "Услуги", 1, col, lowercase)


def swap_class_columns(sheet) -> None:
    set_cell(sheet, "Услуги", 1, 3, "Кроссовер")
    set_cell(sheet, "Услуги", 1, 4, "Легковой")
    set_cell(sheet, "Услуги", 2, 3, 800)
    set_cell(sheet, "Услуги", 2, 4, 700)
    set_cell(sheet, "Услуги", 3, 3, 300)
    set_cell(sheet, "Услуги", 3, 4, 200)
