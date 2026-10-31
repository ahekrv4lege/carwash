"""Офлайн-бенчмарки Carwash: Telegram, Google Sheets и AI замоканы.

Запуск из корня проекта: python3 checks/benchmarks.py [--quick]
Реальные API не используются: AI-клиент подменён AsyncMock, таблица — синтетической.
Результаты печатаются в stdout в виде таблицы метрик.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

FOLDER = Path(__file__).resolve().parent
PROJECT = FOLDER.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(FOLDER))

import ai  # noqa: E402
from ai import Agent, ChatUser  # noqa: E402
from sheets import Store, BOOKING_HEADERS  # noqa: E402
from tests.fakes import Spreadsheet, Worksheet, config as fake_config, booking  # noqa: E402
from tests.test_conversation import response  # noqa: E402


def rss_kb() -> int:
    with open(f"/proc/{os.getpid()}/status") as handle:
        for line in handle:
            if line.startswith("VmRSS"):
                return int(line.split()[1])
    return 0


def peak_rss_kb() -> int:
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


class BenchWorksheet(Worksheet):
    """Синтетический лист со счётчиками записи и раскраски (как у gspread)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.update_calls = 0
        self.format_calls = 0

    def update(self, range_name, values, **kwargs):
        self.update_calls += 1
        start_row = 2 if range_name.startswith("A2:") else 1
        for offset, row in enumerate(values):
            index = start_row - 1 + offset
            while len(self.values) <= index:
                self.values.append([])
            for column, cell in enumerate(row):
                while len(self.values[index]) <= column:
                    self.values[index].append("")
                self.values[index][column] = cell

    def format(self, ranges, cell_format):
        self.format_calls += 1  # один batch_update HTTP-запрос на вызов

    def api_calls(self) -> int:
        return self.reads + len(self.writes) + self.update_calls + self.format_calls


def api_calls(spreadsheet) -> dict:
    total = spreadsheet.metadata_reads + getattr(spreadsheet, "batch_reads", 0)
    detail = {"metadata": spreadsheet.metadata_reads, "batch_get": getattr(spreadsheet, "batch_reads", 0)}
    for sheet in spreadsheet.sheets:
        calls = sheet.api_calls() if isinstance(sheet, BenchWorksheet) else sheet.reads + len(sheet.writes)
        detail[sheet.title] = calls
        total += calls
    detail["total"] = total
    return detail


@contextmanager
def bench_store(bookings_rows=0, **kwargs):
    with tempfile.TemporaryDirectory() as folder:
        cfg = fake_config(folder, **kwargs)
        bookings = [BOOKING_HEADERS]
        for index in range(bookings_rows):
            row = [""] * len(BOOKING_HEADERS)
            row[0] = str(index + 1)
            row[2] = "01.01.2099"
            row[3] = f"{10 + index % 8:02d}:{(index * 7) % 60:02d}"
            row[5] = "00000000000"
            row[13] = "111"
            row[15] = "Новая"
            bookings.append(row)
        spreadsheet = Spreadsheet([
            Worksheet(1, "Услуги", [["Услуга", "Описание", "Легковой", "Кроссовер", "Доступна"],
                                     ["Мойка", "Кузов", "700", "800", "да"]]),
            Worksheet(2, "Тарифы", [["Тариф", "Состав", "Скидка %", "Доступен"], ["Базовый", "Мойка", "0", "да"]]),
            Worksheet(3, "Слоты", [["Дата", "Время", "Постов"],
                                    ["01.01.2099", "10:00", "40"], ["01.01.2099", "11:00", "40"],
                                    ["01.01.2099", "12:00", "40"], ["02.01.2099", "10:00", "40"]]),
            BenchWorksheet(4, "Записи", bookings)])
        spreadsheet.sheets[-1].row_count = bookings_rows + 100
        store = Store(cfg)
        store._spreadsheet = spreadsheet
        yield store, spreadsheet


async def fake_agent(store, responses_factory, **cfg_updates):
    transport = Mock(close=AsyncMock())
    transport.chat.completions.create = AsyncMock(side_effect=lambda **kw: responses_factory())
    with patch.object(ai, "AsyncOpenAI", return_value=transport):
        agent = Agent(store.config, store, **cfg_updates)
    agent._transport = transport
    return agent


def timed(coro):
    started = time.perf_counter()
    result = asyncio.run(coro)
    return result, time.perf_counter() - started


# ---------- сценарий A: простой ----------
def scenario_a() -> dict:
    gc.collect()
    base = rss_kb()
    with bench_store() as (store, spreadsheet):
        loaded = rss_kb()
        async def idle():
            agent = await fake_agent(store, lambda: response("Ответ."))
            await agent.reply(ChatUser(1, None, "A"), "привет")
            await agent.close()
            await asyncio.sleep(0)
        result, _ = timed(idle())
        gc.collect()
        after = rss_kb()
        tasks = len([t for t in asyncio.all_tasks() if not t.done()]) if False else None
    return {"rss_after_imports_kb": loaded - base, "rss_after_agent_and_reply_kb": after - base,
            "api_calls": api_calls(spreadsheet)}


# ---------- сценарий B: обычный диалог ----------
async def _scenario_b_run(messages: int, users: int):
    with bench_store() as (store, spreadsheet):
        counter = {"n": 0}
        def factory():
            counter["n"] += 1
            return response("Сейчас подскажу цены.")
        agent = await fake_agent(store, factory)
        samples = []
        started_all = time.perf_counter()
        for index in range(messages):
            user = ChatUser(1 + index % users, None, "B")
            began = time.perf_counter()
            await agent.reply(user, f"сколько стоит мойка {index}?")
            samples.append((time.perf_counter() - began) * 1000)
        wall = time.perf_counter() - started_all
        await agent.close()
        samples.sort()
        return {"messages": messages, "ai_calls": counter["n"], "wall_sec": round(wall, 3),
                "latency_avg_ms": round(statistics.mean(samples), 2),
                "latency_p95_ms": round(samples[int(len(samples) * 0.95) - 1], 2),
                "rss_kb": rss_kb(), "api_calls": api_calls(spreadsheet)}


def scenario_b(quick=False) -> dict:
    return timed(_scenario_b_run(20 if quick else 200, 5))[0]


# ---------- сценарий C: запись/отмена/перенос ----------
def scenario_c(quick=False) -> dict:
    loops = 3 if quick else 10

    def once():
        with bench_store() as (store, spreadsheet):
            before = api_calls(spreadsheet)["total"]
            timings = {}
            began = time.perf_counter()
            slots = store.free_slots("2099-01-01")
            timings["free_slots_ms"] = (time.perf_counter() - began) * 1000
            began = time.perf_counter()
            created = store.create_booking(**booking(time_str="11:00"))
            timings["create_ms"] = (time.perf_counter() - began) * 1000
            began = time.perf_counter()
            mine = store.my_bookings(111)
            timings["my_bookings_ms"] = (time.perf_counter() - began) * 1000
            began = time.perf_counter()
            moved = store.reschedule_booking(created["booking_id"], 111, "2099-01-01", "12:00")
            timings["reschedule_ms"] = (time.perf_counter() - began) * 1000
            began = time.perf_counter()
            cancelled = store.cancel_booking(created["booking_id"], 111)
            timings["cancel_ms"] = (time.perf_counter() - began) * 1000
            ok = all(r.get("ok") for r in (slots, created, mine, moved, cancelled))
            after = api_calls(spreadsheet)
            return {"ok": ok, "timings_ms": {k: round(v, 2) for k, v in timings.items()},
                    "api_calls_per_full_workflow": after["total"] - before, "api_calls": after}

    results = [once() for _ in range(loops)]
    timings = {key: round(statistics.mean(r["timings_ms"][key] for r in results), 2) for key in results[0]["timings_ms"]}
    return {"ok": all(r["ok"] for r in results),
            "avg_ms": timings,
            "api_calls_per_full_workflow": round(statistics.mean(r["api_calls_per_full_workflow"] for r in results), 1),
            "api_calls_last": results[-1]["api_calls"]}


# ---------- сценарий D: параллельные диалоги ----------
async def _scenario_d_run(concurrency: int, per_user: int = 3):
    with bench_store() as (store, spreadsheet):
        counter = {"n": 0}
        def factory():
            counter["n"] += 1
            return response("Отвечаю на сообщение.")
        agent = await fake_agent(store, factory)
        latencies: list[float] = []
        failures = []

        async def customer(user_id: int):
            user = ChatUser(user_id, None, f"D{user_id}")
            for step in range(per_user):
                began = time.perf_counter()
                try:
                    await agent.reply(user, f"вопрос {user_id}-{step}")
                except Exception as error:  # noqa: BLE001
                    failures.append(type(error).__name__)
                latencies.append((time.perf_counter() - began) * 1000)

        began = time.perf_counter()
        await asyncio.gather(*(customer(1000 + i) for i in range(concurrency)))
        wall = time.perf_counter() - began
        await agent.close()
        latencies.sort()
        return {"concurrency": concurrency, "total_requests": concurrency * per_user,
                "failures": len(failures), "wall_sec": round(wall, 3),
                "throughput_rps": round(concurrency * per_user / wall, 2),
                "latency_avg_ms": round(statistics.mean(latencies), 1),
                "latency_p95_ms": round(latencies[int(len(latencies) * 0.95) - 1], 1),
                "rss_kb": rss_kb()}


def scenario_d(quick=False) -> dict:
    levels = [1, 10, 30] if quick else [1, 10, 30, 50, 100]
    return {str(level): timed(_scenario_d_run(level))[0] for level in levels}


# ---------- сценарий E: большие таблицы ----------
def scenario_e(quick=False) -> dict:
    rows = 1000 if quick else 5000
    with bench_store(bookings_rows=rows) as (store, spreadsheet):
        gc.collect()
        before_rss = rss_kb()
        result = {}
        began = time.perf_counter()
        mine = store.my_bookings(111)
        result["my_bookings_ms"] = round((time.perf_counter() - began) * 1000, 1)
        began = time.perf_counter()
        slots = store.free_slots("2099-01-01")
        result["free_slots_ms"] = round((time.perf_counter() - began) * 1000, 1)
        began = time.perf_counter()
        created = store.create_booking(**booking(time_str="11:00"))
        result["create_ms"] = round((time.perf_counter() - began) * 1000, 1)
        result["ok"] = bool(mine.get("ok") and slots.get("ok") and created.get("ok"))
        result["peak_rss_kb"] = peak_rss_kb()
        result["rss_delta_kb"] = peak_rss_kb() - before_rss
        result["api_calls"] = api_calls(spreadsheet)
        return result


# ---------- сценарий F: длительная нагрузка ----------
async def _scenario_f_run(operations: int):
    with bench_store() as (store, spreadsheet):
        counter = {"n": 0}
        def factory():
            counter["n"] += 1
            return response("Ответ.")
        agent = await fake_agent(store, factory)
        samples = []
        gc.collect()
        started_rss = rss_kb()
        for step in range(operations):
            user = ChatUser(2000 + step % 40, None, "F")
            await agent.reply(user, f"сообщение {step}")
            if step % 50 == 0:
                store.free_slots("2099-01-01")
                samples.append(rss_kb())
        await agent.close()
        gc.collect()
        await asyncio.sleep(0)
        tasks = len(asyncio.all_tasks())
        agent.cleanup_sessions()
        return {"operations": operations, "rss_start_kb": started_rss, "rss_end_kb": rss_kb(),
                "rss_growth_kb": rss_kb() - started_rss, "rss_max_sample_kb": max(samples),
                "open_tasks": tasks, "ai_calls": counter["n"],
                "sessions_left": len(agent._state), "history_max": max((len(h) for h in agent._history.values()), default=0)}


def scenario_f(quick=False) -> dict:
    return timed(_scenario_f_run(200 if quick else 2000))[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--json", action="store_true", help="только JSON в stdout")
    arguments = parser.parse_args()

    results = {}
    for name, function in (("A_idle", lambda: scenario_a()),
                           ("B_dialog", lambda: scenario_b(arguments.quick)),
                           ("C_booking", lambda: scenario_c(arguments.quick)),
                           ("D_parallel", lambda: scenario_d(arguments.quick)),
                           ("E_large", lambda: scenario_e(arguments.quick)),
                           ("F_sustained", lambda: scenario_f(arguments.quick))):
        results[name] = function()
        if not arguments.json:
            print(f"== {name} ==\n{json.dumps(results[name], ensure_ascii=False, indent=2)}", flush=True)
    if arguments.json:
        print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
