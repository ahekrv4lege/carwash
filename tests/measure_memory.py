"""Детерминированные измерения двух checkout; только mock AI и синтетические Sheets.

Из корня нужного checkout: python /путь/к/tests/measure_memory.py --label before --output tmp/before.json
Импорты идут из текущего checkout. JSON-байты — не токены.
"""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.environ.get('CARWASH_MEASURE_SOURCE', str(Path.cwd())))
from ai import Agent, ChatUser
from tests.fakes import fixture


def response(text="", calls=()):
    return N(choices=[N(finish_reason="tool_calls" if calls else "stop", message=N(content=text,
        tool_calls=[N(id=f"call{i}", function=N(name=name, arguments=json.dumps(args, ensure_ascii=False)))
                    for i, (name, args) in enumerate(calls)]))])


class SheetsMeter:
    def __init__(self, ss, rpc_delay_ms=0):
        self.calls = {}
        self.depth = 0
        for obj, methods in [(ss, ("fetch_sheet_metadata", "values_batch_get")),
                             *[(ws, ("get_all_values", "get", "batch_update", "append_row", "update_cell")) for ws in ss.sheets]]:
            for name in methods:
                method = getattr(obj, name)
                def wrap(*args, _method=method, _name=name, **kwargs):
                    if not self.depth:
                        self.calls[_name] = self.calls.get(_name, 0) + 1
                        if rpc_delay_ms:
                            time.sleep(rpc_delay_ms / 1000)
                    self.depth += 1
                    try:
                        return _method(*args, **kwargs)
                    finally:
                        self.depth -= 1
                setattr(obj, name, wrap)

    def counts(self):
        return dict(self.calls)


def retained_bytes(value, seen=None):
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(retained_bytes(k, seen) + retained_bytes(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple, set)):
        size += sum(retained_bytes(v, seen) for v in value)
    return size


async def measure(label, workload, rpc_delay_ms=0):
    root = Path.cwd() / "tmp"
    root.mkdir(exist_ok=True)
    mapping = root / f"{label}-{workload}-mapping.json"
    existing = set(root.iterdir())
    store, ss = fixture(root, sheets_mapping_file=str(mapping))
    store.now = lambda: datetime(2026, 10, 24, 9, tzinfo=ZoneInfo(store.cfg.timezone))
    meter = SheetsMeter(ss, rpc_delay_ms)
    with patch("ai.AsyncOpenAI"):
        agent = Agent(store.cfg, store)
    structured = hasattr(agent, "_ingest")
    requests, executions, requested = [], [], []
    queue = []

    async def transport(**kwargs):
        requests.append(deepcopy(kwargs))
        if not queue:
            raise AssertionError("fixture AI responses exhausted")
        result = queue.pop(0)
        requested.extend(c.function.name for c in result.choices[0].message.tool_calls)
        return result

    agent.client.chat.completions.create = AsyncMock(side_effect=transport)
    call = agent._call_tool

    async def tool(*args, **kwargs):
        executions.append(args[0])
        return await call(*args, **kwargs)

    agent._call_tool = tool
    user = ChatUser(111, None, "Synthetic")

    def final(reply, facts=None):
        return response(calls=[("respond", {"reply": reply, "facts": facts or {}})]) if structured else response(reply)

    async def say(text, *outputs):
        queue.extend(outputs)
        answer = await agent.reply(user, text)
        queue.clear()
        agent.mark_delivered(111, agent.delivery_token(111))
        return answer

    checkpoints = {}
    started = time.perf_counter()
    if workload in {"memory", "memory50"}:
        await say("У меня Toyota Camry 3.5", final("Учту автомобиль.", {"car": ["Toyota Camry 3.5", "Toyota Camry 3.5"]}))
        await say("Меня зовут Антон", final("Антон, подскажу услуги.", {"client_name": ["Антон", "Антон"]}))
        for i in range(48 if workload == "memory50" else 30):
            await say("Расскажите про обычную мойку", final("Уточним состав по прайсу."))
            if i in {7, 17, 27, 29, 47}:
                checkpoints[str(i + 3)] = len(json.dumps(requests[-1], ensure_ascii=False, separators=(",", ":")).encode())
        tail = json.dumps(requests[-1], ensure_ascii=False)
        correctness = {"car_in_last_request": "Toyota Camry 3.5" in tail,
                       "name_in_last_request": "Антон" in tail}
        (root / f"{label}-last-request.json").write_text(json.dumps(requests[-1], ensure_ascii=False, indent=2) + "\n")
    elif workload == "invalid_loop":
        bad = response(calls=[("calculate_price", {"size_class": "sedan", "tariff": "Мойка"})])
        await say("Какая цена?", *[deepcopy(bad) for _ in range(6)])
        correctness = {"no_booking_writes": not any(ws.writes for ws in ss.sheets)}
    else:
        args = dict(date="2099-01-01", time="10:00", client_name="Антон", phone="70001112233",
                    car="Toyota Camry 3.5", size_class="Легковой", tariff="Базовый")
        await say("Данные записи: " + json.dumps(args, ensure_ascii=False), response(calls=[("create_booking", args)]))
        if workload != "repeat_confirmation":
            await say("Адрес?", final("Адрес уточнит администратор."))
            await say("Покажите предложение ещё раз", response(calls=[("create_booking", {} if structured else args)]))
        answer = await say("Да, подтверждаю запись")
        if workload == "repeat_confirmation":
            await say("Да", final("Ваша запись уже оформлена."))
            await say("Да", final("Ваша запись уже оформлена."))
        correctness = {"one_booking_write": sum(len(ws.writes) for ws in ss.sheets) == 1,
                       "python_receipt": "Записала" in answer}
    sizes = [len(json.dumps(req, ensure_ascii=False, separators=(",", ":")).encode()) for req in requests]
    result = dict(ai_requests=len(requests), tool_executions=len(executions), serialized_request_bytes=sum(sizes),
                  ai_tool_requests=len(requested), business_tool_requests=sum(n != "respond" for n in requested),
                  synthetic_elapsed_ms=(time.perf_counter()-started)*1000, injected_rpc_delay_ms=rpc_delay_ms,
                  context_bytes=sum(len(json.dumps(r['messages'],ensure_ascii=False,separators=(',',':')).encode()) for r in requests),
                  schema_bytes=sum(len(json.dumps(r['tools'],ensure_ascii=False,separators=(',',':')).encode()) for r in requests),
                  first_request_bytes=sizes[0], last_request_bytes=sizes[-1], max_request_bytes=max(sizes),
                  context_checkpoints=checkpoints, synthetic_sheets_requests=sum(meter.counts().values()),
                  synthetic_sheets_by_method=meter.counts(), correctness=correctness,
                  retained_session_container_bytes_approx=retained_bytes([getattr(agent, k, {}) for k in
                      ("_state", "_history", "_pending", "_locks", "_generation", "_last_seen", "_active", "_turn_text", "_turn_sources")]),
                  system_prompt_bytes=len(agent._system_prompt(user).encode()))
    for path in (mapping, mapping.with_name(mapping.stem + "-operations.json")):
        if path not in existing and path.is_file():
            path.unlink()
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = {name: await measure(args.label, name) for name in ("memory", "invalid_loop", "booking")}
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
