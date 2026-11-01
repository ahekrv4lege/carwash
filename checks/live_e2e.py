"""Сквозные проверки на настоящих DeepSeek и Google Sheets (тестовая таблица из .env).

Запуск (локально, конфигурация и учётные данные берутся с сервера в память процесса):
    python checks/live_e2e.py <сценарий> [<сценарий> ...] | all

Что делает:
- читает .env и service-account с сервера по ssh, ничего не пишет на диск и не печатает;
- снимает значения всех четырёх листов до прогона и восстанавливает их после;
- считает токены по usage ответов DeepSeek и HTTP-запросы к Google Sheets;
- пишет JSON-отчёт в /tmp/opencode (вне репозитория).

Telegram-транспорт здесь не участвует: вызывается тот же Agent.reply/proposal_action,
что и обработчики бота.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
PROJECT = FOLDER.parent
sys.path[:0] = [str(PROJECT), str(FOLDER)]

REMOTE = "carwash"
REPORT = Path("/tmp/opencode/e2e_report.json")
SHEET_TITLES = ("Услуги", "Тарифы", "Слоты", "Записи")
ENV_KEYS = ("SPREADSHEET_ID", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_FALLBACKS",
            "AI_TIMEOUT_SEC", "AI_REQUEST_BUDGET_SEC", "AI_REQUEST_RETRIES", "AI_REASONING_EFFORT",
            "AI_TEMPERATURE", "AI_MAX_OUTPUT_TOKENS", "AI_TOKEN_LIMIT_PARAMETER", "AI_REASONING_SUPPORTED",
            "AI_HISTORY_CHARS", "AI_RECENT_TURNS", "MIN_LEAD_MINUTES", "TIMEZONE", "CURRENCY",
            "SHEET_SERVICES", "SHEET_TARIFFS", "SHEET_SLOTS", "SHEET_BOOKINGS", "SHEET_CLASSES",
            "SHEET_DURATIONS", "AI_ENABLED", "AI_PROOFREAD_ENABLED")


# ---------- секреты с сервера: только в памяти процесса ----------

def _ssh(command: str) -> str:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", REMOTE, command], capture_output=True,
                          text=True, timeout=120, check=True).stdout


def load_remote_env() -> None:
    raw = _ssh("cd /root/carwash && cat .env")
    values = {}
    for line in raw.splitlines():
        match = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    for key in ENV_KEYS:
        if values.get(key) and key not in os.environ:
            os.environ[key] = values[key]
    # Токен бота не используется; подставляем синтетический, чтобы каталог предложений был изолирован.
    os.environ["BOT_TOKEN"] = "123456789:synthetic_e2e_token_not_used_12345"


def load_remote_credentials() -> dict:
    return json.loads(_ssh("cat /root/carwash/credentials.json"))


# ---------- счётчики: токены DeepSeek и HTTP-запросы Google Sheets ----------

class Meter:
    def __init__(self):
        self.label = "idle"
        self.ai: list[dict] = []
        self.sheets: list[dict] = []

    def reset_label(self, label: str):
        self.label = label


METER = Meter()


def install_ai_meter():
    from openai.resources.chat.completions import AsyncCompletions

    original = AsyncCompletions.create

    async def metered(self, *args, **kwargs):
        began = time.time()
        response = await original(self, *args, **kwargs)
        usage = getattr(response, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", 0) if usage else 0
        message = response.choices[0].message if getattr(response, "choices", None) else None
        calls = [f"{call.function.name}({(call.function.arguments or '')[:500]})"
                 for call in (getattr(message, "tool_calls", None) or [])]
        METER.ai.append({
            "label": METER.label, "model": kwargs.get("model"), "seconds": round(time.time() - began, 2),
            "output": ((getattr(message, "content", None) or "")[:400], calls),
            "prompt": getattr(usage, "prompt_tokens", None), "completion": getattr(usage, "completion_tokens", None),
            "cached": getattr(details, "cached_tokens", 0) if details else 0,
            "reasoning": reasoning or 0,
            "tools": len(kwargs.get("tools") or []),
            "messages": len(kwargs.get("messages") or []),
        })
        return response

    AsyncCompletions.create = metered


def make_counting_http():
    from sheets import SheetsHTTPClient

    class CountingHTTP(SheetsHTTPClient):
        def request(self, method, *arguments, **options):
            endpoint = str(arguments[0]) if arguments else str(options.get("url", ""))
            kind = "other"
            if "batchUpdate" in endpoint:
                kind = "values.batchUpdate" if "/values:" in endpoint else "spreadsheet.batchUpdate"
            elif "values:batchGet" in endpoint or "/values:" in endpoint:
                kind = "values.batchGet"
            elif "/values/" in endpoint:
                kind = "values.write" if method.lower() in {"put", "post"} else "values.get"
            elif endpoint.rstrip("/").endswith("/spreadsheets") or "?fields=" in endpoint:
                kind = "metadata"
            started = time.time()
            try:
                return super().request(method, *arguments, **options)
            finally:
                METER.sheets.append({"label": METER.label, "method": method.upper(), "kind": kind,
                                     "seconds": round(time.time() - started, 3)})

    return CountingHTTP


def open_test_spreadsheet(spreadsheet_id: str):
    import gspread
    from google.oauth2.service_account import Credentials
    from sheets import SCOPES

    info = load_remote_credentials()
    credentials = Credentials.from_service_account_info(info, scopes=SCOPES)
    client = gspread.authorize(credentials, http_client=make_counting_http())
    client.set_timeout((10, 60))
    return client.open_by_key(spreadsheet_id)


# ---------- снимок и восстановление тестовой таблицы ----------

def snapshot(spreadsheet) -> dict:
    """Два представления каждого листа: FORMULA (исходные значения) и FORMATTED_VALUE (отображение)."""
    result = {}
    for title in SHEET_TITLES:
        worksheet = spreadsheet.worksheet(title)
        result[title] = {
            "formula": worksheet.get_all_values(value_render_option="FORMULA"),
            "shown": worksheet.get_all_values(value_render_option="FORMATTED_VALUE"),
        }
    return result


def diff_snapshots(before: dict, after: dict, limit: int = 12) -> list[str]:
    found = []
    for title in SHEET_TITLES:
        for view in ("formula", "shown"):
            for row_no, (left, right) in enumerate(_zip_rows(before[title][view], after[title][view]), 1):
                for column in range(max(len(left), len(right))):
                    x = left[column] if column < len(left) else ""
                    y = right[column] if column < len(right) else ""
                    if str(x) != str(y):
                        found.append(f"{title}/{view}!R{row_no}C{column + 1}: {x!r} -> {y!r}")
                        if len(found) >= limit:
                            return found
    return found


def _zip_rows(left, right):
    for index in range(max(len(left), len(right))):
        yield (left[index] if index < len(left) else [], right[index] if index < len(right) else [])


def restore(spreadsheet, saved: dict) -> None:
    """Значения пишутся как есть (RAW), формулы — отдельно как USER_ENTERED, чтобы не менять типы ячеек."""
    for title, views in saved.items():
        values = views["formula"]
        worksheet = spreadsheet.worksheet(title)
        worksheet.clear()
        if not values:
            continue
        width = max(len(row) for row in values)
        padded = [list(row) + [""] * (width - len(row)) for row in values]
        worksheet.update(values=padded, range_name=f"A1:{_col(width)}{len(padded)}",
                         value_input_option="RAW")
        formulas = [{"range": f"{_col(column + 1)}{row + 1}", "values": [[cell]]}
                    for row, line in enumerate(padded) for column, cell in enumerate(line)
                    if isinstance(cell, str) and cell.startswith("=")]
        if formulas:
            worksheet.batch_update(formulas, value_input_option="USER_ENTERED")


def _col(index: int) -> str:
    name = ""
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


# ---------- сборка приложения на реальных сервисах ----------

def build(spreadsheet):
    from sheets import Store
    from ai import Agent
    from config import load_config

    base = load_config(sheets_only=True)
    folder = tempfile.mkdtemp(prefix="carwash-e2e-")
    config = replace(base, sheets_adaptive_enabled=True, sheets_ai_mapping_enabled=True,
                     vehicle_memory_file=os.path.join(folder, "memory.sqlite3"),
                     sheets_mapping_file=os.path.join(folder, "mapping.json"),
                     ai_enabled=True, catalog_cache_sec=0, slots_cache_sec=0)
    store = Store(config)
    store._spreadsheet = spreadsheet
    _trace_store_operations(store)
    agent = Agent(config, store)
    return agent, store, config


TRACE: list[dict] = []


def _trace_store_operations(store) -> None:
    """Исключения записей попадают в отчёт с traceback: иначе агент показывает общий текст."""
    import traceback

    for name in ("create_booking", "cancel_booking", "reschedule_booking"):
        original = getattr(store, name)

        def wrapped(*args, __original=original, __name=name, **kwargs):
            try:
                return __original(*args, **kwargs)
            except Exception as error:
                TRACE.append({"operation": __name, "error": f"{type(error).__name__}: {error}",
                              "traceback": traceback.format_exc()[-1500:]})
                raise
        setattr(store, name, wrapped)


def run_report(entries: list[dict]) -> None:
    existing = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else []
    existing.extend(entries)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")


def summarize_meter(label: str) -> dict:
    ai = [row for row in METER.ai if row["label"] == label]
    sheets = [row for row in METER.sheets if row["label"] == label]
    kinds: dict[str, int] = {}
    for row in sheets:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    return {
        "ai_calls": len(ai),
        "prompt_tokens": sum(row["prompt"] or 0 for row in ai),
        "completion_tokens": sum(row["completion"] or 0 for row in ai),
        "cached_tokens": sum(row["cached"] or 0 for row in ai),
        "reasoning_tokens": sum(row["reasoning"] or 0 for row in ai),
        "ai_seconds": round(sum(row["seconds"] for row in ai), 2),
        "sheets_requests": len(sheets),
        "sheets_by_kind": kinds,
    }


# ---------- сценарии ----------

def tomorrow(store) -> str:
    return (store.now() + timedelta(days=1)).date().isoformat()


def booking_rows(spreadsheet) -> list[dict]:
    values = spreadsheet.worksheet("Записи").get_all_values()
    if not values:
        return []
    headers = values[0]
    return [dict(zip(headers, row)) for row in values[1:] if any(cell.strip() for cell in row)]


TRANSCRIPT: list[dict] = []


async def say(agent, user, text: str) -> str:
    answer = await agent.reply(user, text)
    agent.mark_delivered(user.id, agent.delivery_token(user.id, answer))
    state = agent._state.get(user.id, {})
    TRANSCRIPT.append({"label": METER.label, "customer": text, "bot": answer,
                       "pending": (agent._pending.get(user.id) or {}).get("action"),
                       "facts": {k: v for k, v in (state.get("facts") or {}).items() if k != "phone"},
                       "tentative": state.get("tentative") or {},
                       "ambiguous_time": state.get("ambiguous_time")})
    return answer


async def confirm_pending(agent, user) -> str:
    pending = agent._pending.get(user.id)
    if not pending:
        return "NO_PENDING"
    answer = await agent.proposal_action(user, pending["offer_id"], "confirm")
    agent.mark_delivered(user.id, agent.delivery_token(user.id, answer))
    return answer


async def scenario_lifecycle(spreadsheet) -> dict:
    """Реальный цикл: создать → перенести → отменить, с проверкой строк таблицы."""
    from ai import ChatUser

    agent, store, config = build(spreadsheet)
    user = ChatUser(930001, None, "E2E Lifecycle")
    day = tomorrow(store)
    checks = {}
    try:
        METER.reset_label("lifecycle.create")
        await say(agent, user, "Здравствуйте! Хочу записаться на мойку кузова на завтра в 11:00, "
                               "машина Toyota Camry 3.5, меня зовут Тест, телефон +7 900 111-22-33.")
        if user.id not in agent._pending:
            await say(agent, user, "Да, оформляйте, пожалуйста. Предложите запись.")
        pending = agent._pending.get(user.id, {})
        checks["proposal_created"] = pending.get("action") == "create_booking"
        checks["before_confirm_no_row"] = not any(
            row.get("Telegram ID") == str(user.id) for row in booking_rows(spreadsheet))
        if checks["proposal_created"]:
            answer = await confirm_pending(agent, user)
            checks["confirm_answer"] = answer[:160]
        mine = [row for row in booking_rows(spreadsheet) if row.get("Telegram ID") == str(user.id)]
        checks["rows_after_create"] = len(mine)
        if mine:
            row = mine[0]
            checks["row"] = {key: row.get(key) for key in ("ID", "Дата", "Время", "Автомобиль", "Класс", "Сумма", "Статус")}
            checks["date_ok"] = row.get("Дата") == day.replace("-", ".")
            checks["time_ok"] = row.get("Время") == "11:00"
            checks["car_ok"] = "camry" in row.get("Автомобиль", "").lower()
        # повторное подтверждение не должно создавать вторую строку
        if user.id in agent._pending:
            await confirm_pending(agent, user)
        checks["rows_after_repeat_confirm"] = len([row for row in booking_rows(spreadsheet)
                                                   if row.get("Telegram ID") == str(user.id)])

        METER.reset_label("lifecycle.reschedule")
        await say(agent, user, "Перенесите мою запись на 12:00 тот же день.")
        if user.id in agent._pending and agent._pending[user.id]["action"] == "reschedule_booking":
            answer = await confirm_pending(agent, user)
            checks["reschedule_answer"] = answer[:160]
        mine = [row for row in booking_rows(spreadsheet) if row.get("Telegram ID") == str(user.id)
                and row.get("Статус") != "Отмена"]
        checks["time_after_reschedule"] = mine[0].get("Время") if mine else None

        METER.reset_label("lifecycle.cancel")
        await say(agent, user, "Отмените, пожалуйста, эту запись.")
        if user.id in agent._pending and agent._pending[user.id]["action"] == "cancel_booking":
            answer = await confirm_pending(agent, user)
            checks["cancel_answer"] = answer[:160]
        active = [row for row in booking_rows(spreadsheet) if row.get("Telegram ID") == str(user.id)
                  and row.get("Статус") != "Отмена"]
        checks["active_after_cancel"] = len(active)
    finally:
        await agent.close()
    return {"scenario": "lifecycle", "checks": checks, "transcript": TRANSCRIPT[-12:], "trace": TRACE[-3:],
            "meter": {"create": summarize_meter("lifecycle.create"),
                      "reschedule": summarize_meter("lifecycle.reschedule"),
                      "cancel": summarize_meter("lifecycle.cancel")}}


async def scenario_price_questions(spreadsheet) -> dict:
    """Цены в диалоге должны совпадать с расчётом Python по реальной таблице."""
    from ai import ChatUser
    from sheets import Store

    agent, store, config = build(spreadsheet)
    user = ChatUser(930002, None, "E2E Price")
    expected = {}
    rows = []
    try:
        METER.reset_label("price.single")
        question = "Сколько стоит мойка кузова для Toyota Camry 3.5?"
        answer = await say(agent, user, question)
        expected["Легковой:Мойка кузова"] = store.calculate_price("Легковой", None, ["Мойка кузова"]).get("total")
        rows.append({"question": question, "answer": answer[:200], "expected_total": expected["Легковой:Мойка кузова"],
                     "matches": str(expected["Легковой:Мойка кузова"]) in answer.replace(" ", "")})
        METER.reset_label("price.followup")
        question = "А если комплекс на кроссовере?"
        answer = await say(agent, user, question)
        tariffs = store.get_catalog(fresh=True).tariffs
        comp = next((name for name in tariffs if "комплекс" in name.lower()), None)
        expected_total = store.calculate_price("Кроссовер", comp, []).get("total") if comp else None
        rows.append({"question": question, "answer": answer[:200], "expected_total": expected_total,
                     "matches": bool(expected_total) and str(expected_total) in answer.replace(" ", "")})
    finally:
        await agent.close()
    return {"scenario": "price_questions", "rows": rows,
            "meter": {"single": summarize_meter("price.single"), "followup": summarize_meter("price.followup")}}


async def scenario_long_dialog(spreadsheet, turns: int = 10) -> dict:
    """Многоходовой диалог: факты из первых реплик должны сохраняться."""
    from ai import ChatUser

    agent, store, config = build(spreadsheet)
    user = ChatUser(930003, None, "E2E Long")
    script = [
        "Здравствуйте, меня зовут Ирина.",
        "У меня Kia Rio, машина маленькая.",
        "Сколько стоит мойка кузова?",
        "А пылесос салона есть?",
        "Сколько он стоит для моей машины?",
        "Ладно, пока не надо пылесоса, только мойка.",
        "Какой у меня класс машины?",
        "А сколько займёт мойка?",
        "Хорошо, а как меня зовут?",
        "Спасибо, пока всё.",
    ][:turns]
    log = []
    METER.reset_label("long.dialog")
    try:
        for text in script:
            answer = await say(agent, user, text)
            log.append({"q": text, "a": answer[:180], "state_facts": sorted((agent._state.get(user.id, {}).get("facts") or {}).keys())})
    finally:
        await agent.close()
    return {"scenario": "long_dialog", "turns": len(log), "log": log,
            "name_retained": any("Ирина" in item["a"] for item in log[-2:]),
            "meter": summarize_meter("long.dialog")}


async def scenario_parallel_prices(spreadsheet, users: int = 5) -> dict:
    """Реальный DeepSeek: параллельные диалоги разных клиентов."""
    from ai import ChatUser

    agent, store, config = build(spreadsheet)
    METER.reset_label("parallel")
    started = time.time()
    try:
        async def one(index):
            began = time.time()
            user = ChatUser(940000 + index, None, f"P{index}")
            try:
                answer = await say(agent, user, "Сколько стоит мойка кузова для Kia Rio?")
                return {"user": index, "seconds": round(time.time() - began, 2), "ok": "Не удалось" not in answer,
                        "answer": answer[:120]}
            except Exception as error:
                return {"user": index, "seconds": round(time.time() - began, 2), "ok": False,
                        "error": type(error).__name__}
        results = await asyncio.gather(*(one(index) for index in range(users)))
    finally:
        await agent.close()
    latencies = sorted(item["seconds"] for item in results)
    return {"scenario": "parallel_prices", "users": users, "wall_seconds": round(time.time() - started, 2),
            "p50": latencies[len(latencies) // 2], "p95": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
            "failures": sum(1 for item in results if not item["ok"]), "results": results,
            "meter": summarize_meter("parallel")}


SCENARIOS = {
    "lifecycle": scenario_lifecycle,
    "prices": scenario_price_questions,
    "long": scenario_long_dialog,
    "parallel": scenario_parallel_prices,
}


async def amain(names: list[str]) -> int:
    load_remote_env()
    install_ai_meter()
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    spreadsheet = open_test_spreadsheet(spreadsheet_id)
    saved = snapshot(spreadsheet)
    results = []
    try:
        for name in names:
            began = time.time()
            try:
                result = await SCENARIOS[name](spreadsheet)
                result["ok"] = True
            except Exception as error:  # отчёт по сценарию, а не падение всего прогона
                result = {"scenario": name, "ok": False, "error": f"{type(error).__name__}: {str(error)[:300]}"}
            result["seconds"] = round(time.time() - began, 1)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False, default=str)[:1500], flush=True)
    finally:
        restore(spreadsheet, saved)
        after = snapshot(spreadsheet)
        diff = diff_snapshots(saved, after)
        print("RESTORED:", after == saved, "diff:", diff, flush=True)
    run_report(results)
    return 0 if all(item.get("ok") for item in results) else 1


if __name__ == "__main__":
    chosen = sys.argv[1:] or ["all"]
    names = list(SCENARIOS) if chosen == ["all"] else chosen
    raise SystemExit(asyncio.run(amain(names)))
