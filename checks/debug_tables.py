"""Отладочный отчёт по испорченным таблицам: сценарии из tests.corruption_cases.

Для каждого сценария печатаем: ПОРТАЦИЯ (какие ячейки и как изменены),
ВХОД (таблица после порчи), РЕЗУЛЬТАТ (что вернул Store: отказ MappingError,
отказ-словарь или штатный расчёт) и ВЕРДИКТ (совпадает ли исход с ожиданием).
Отчёт сохраняется в debug_tables_report.txt рядом с этим файлом. Данные
синтетические: сети и квоты нет, реальная таблица проекта не участвует.

Запуск: .venv\\Scripts\\python.exe checks\\debug_tables.py
"""
from copy import deepcopy
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(PROJECT), str(Path(__file__).resolve().parent)]

from adaptive import MappingError  # noqa: E402
from tests.corruption_cases import SCENARIOS, Workspace  # noqa: E402

REPORT = Path(__file__).resolve().parent / "debug_tables_report.txt"

EXPECTED_LABELS = {
    "mapping-error": "внятный отказ MappingError",
    "ok": "штатный расчёт/ответ (ok=True)",
    "failure": "штатный отказ (ok=False) без чужого исключения",
    "value": "штатные внутренние данные",
    "ok-or-mapping-error": "расчёт или честный отказ (оба допустимы)",
}


def render_rows(title: str, rows: list) -> list:
    lines = [f"Лист «{title}» после порчи:"]
    for number, row in enumerate(rows, 1):
        cells = [str(cell) if str(cell) else "·" for cell in row]
        lines.append(f"  строка {number}: " + " | ".join(cells))
    return lines


def diff_cells(before: list, after: list) -> list:
    """Список изменений по ячейкам + появившиеся строки."""
    changes = []
    for number, (old_row, new_row) in enumerate(zip(before, after), 1):
        for column, (old, new) in enumerate(zip(old_row, new_row), 1):
            if str(old) != str(new):
                changes.append(f"  ячейка (строка {number}, столбец {column}): «{old}» → «{new}»")
    for number in range(len(before) + 1, len(after) + 1):
        changes.append(f"  добавлена строка {number}: " + " | ".join(str(cell) for cell in after[number - 1]))
    return changes


def short_result(result) -> str:
    """Компактный читаемый итог штатного ответа Store."""
    if not isinstance(result, dict):
        return repr(result)
    if result.get("ok") is False:
        return f"отказ: {result.get('error')!r}"
    summary = []
    if "total" in result:
        summary.append(f"итог {result['total']}{result.get('currency', '')}")
    if result.get("lines"):
        summary.append("линии: " + "; ".join(str(line) for line in result["lines"]))
    if "bookings" in result:
        summary.append(f"записей в списке: {len(result['bookings'])}")
    if "size_classes" in result:
        summary.append("классы: " + ", ".join(result["size_classes"]))
        for service in result.get("services", []):
            summary.append(f"цены {service['name']}: {service.get('prices')}")
    if "days" in result:
        for day in result["days"]:
            summary.append(f"{day['date']}: свободно {day['free_times']}")
    return "; ".join(summary) or repr(result)


def verdict_of(scenario, outcome: dict) -> str:
    kind = outcome.get("kind")
    allowed = {scenario.expect} if scenario.expect != "ok-or-mapping-error" else {"ok", "mapping-error"}
    if kind in allowed:
        base = "✓ соответствует ожиданию: " + EXPECTED_LABELS[scenario.expect]
        message = outcome.get("message", "")
        return base + (f" — {message}" if message else "")
    return (f"✗ НЕ СОВПАЛО: ждали «{EXPECTED_LABELS[scenario.expect]}», "
            f"пришло «{EXPECTED_LABELS.get(kind, kind)}»")


def scenario_report(scenario) -> list:
    lines = [f"### {scenario.id}",
             f"Сценарий: {scenario.title}" + (" (офлайн-путь без адаптива)" if scenario.legacy else "")]
    workspace = Workspace(legacy=scenario.legacy)
    try:
        store, spreadsheet = workspace.store, workspace.spreadsheet
        pristine = {sheet.title: deepcopy(sheet.values) for sheet in spreadsheet.sheets}
        if scenario.warmup:
            scenario.warmup(store)
        if scenario.disrupt:
            scenario.disrupt(spreadsheet, store)
        for sheet in spreadsheet.sheets:
            changes = diff_cells(pristine[sheet.title], sheet.values)
            if changes:
                lines.append("ПОРТАЦИЯ:")
                lines.extend(changes)
                lines.append("ВХОД — испорченная таблица:")
                lines.extend(render_rows(sheet.title, sheet.values))
        try:
            result = scenario.action(store)
        except MappingError as error:
            outcome = {"kind": "mapping-error", "message": str(error)}
            lines.append(f"РЕЗУЛЬТАТ: отказ MappingError — {error}")
        else:
            if isinstance(result, dict) and "ok" in result:
                kind = "ok" if result["ok"] else "failure"
            else:
                kind, result = "value", result
            outcome = {"kind": kind, "result": result}
            if kind in {"ok", "failure"}:
                lines.append(f"РЕЗУЛЬТАТ ({'расчёт' if kind == 'ok' else 'отказ'}): {short_result(result)}")
            else:
                lines.append(f"РЕЗУЛЬТАТ: {result!r}")
        lines.append(f"ВЕРДИКТ: {verdict_of(scenario, outcome)}")
    finally:
        workspace.cleanup()
    lines.append("")
    return lines


def main() -> int:
    blocks = ["ОТЛАДКА: ИСПОРЧЕННЫЕ ТАБЛИЦЫ (задача №4 из TODO.md)",
              "Таблицы синтетические; сеть, ключи и реальный Google Sheets не задействованы.",
              "=" * 72, ""]
    passed, failed = 0, 0
    for scenario in SCENARIOS:
        try:
            block = scenario_report(scenario)
        except Exception as error:  # noqa: BLE001 — отчёт обязан показать ЛЮБОЙ падёж
            block = [f"### {scenario.id}",
                     f"Сценарий: {scenario.title}",
                     f"РЕЗУЛЬТАТ: ЧУЖОЕ ИСКЛЮЧЕНИЕ — {type(error).__name__}: {error}",
                     f"ВЕРДИКТ: ✗ НЕ СОВПАЛО: ждали «{EXPECTED_LABELS[scenario.expect]}», "
                     f"пришло «чужое исключение {type(error).__name__}»", ""]
        verdict = next((line for line in block if line.startswith("ВЕРДИКТ:")), "")
        passed += verdict.startswith("ВЕРДИКТ: ✓")
        failed += verdict.startswith("ВЕРДИКТ: ✗")
        blocks.extend(block)
    blocks.append("=" * 72)
    blocks.append(f"ИТОГ: ✓ совпало {passed} из {passed + failed}; ✗ не совпало {failed}.")
    REPORT.write_text("\n".join(blocks), encoding="utf-8")
    print(f"Отчёт готов: {REPORT}")
    print(f"✓ совпало {passed} из {passed + failed}; ✗ не совпало {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
