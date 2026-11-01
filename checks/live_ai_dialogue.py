"""Живой диалог на настоящем провайдере поверх AI-интерпретированной раскладки.

Запуск: python checks/live_ai_dialogue.py
Книга синтетическая (изолированная), маппинг и цены — реальный код; модель —
настоящий DeepSeek из production-конфигурации. Telegram-транспорт не участвует.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
PROJECT = FOLDER.parent
sys.path[:0] = [str(PROJECT), str(FOLDER)]

from tests.fakes import Spreadsheet, Worksheet, BOOKING_HEADERS  # noqa: E402
from sheets import Store  # noqa: E402


def build_agent():
    import os
    import tempfile
    from unittest.mock import patch
    from config import load_config
    from ai import Agent, ChatUser

    config = load_config(sheets_only=True)
    mapping_dir = tempfile.mkdtemp(prefix="carwash-live-dialogue-")
    config = replace(config, sheets_adaptive_enabled=True, sheets_ai_mapping_enabled=True,
                     sheets_mapping_file=os.path.join(mapping_dir, "mapping.json"),
                     ai_enabled=True)
    spreadsheet = Spreadsheet([
        Worksheet(1, "Прайс-лист", [
            ["Наименование работы", "Примечание", "Стоимость седан", "Стоимость кроссовер", "Выполняется"],
            ["Мойка кузова", "Кузов", "700", "800", "да"],
            ["Полировка кузова", "Кузов", "2000", "2500", "да"],
            ["Химчистка салона", "Салон", "3000", "3500", "да"]]),
        Worksheet(2, "График", [["Начало смены", "Конец смены", "Мастерых"], ["09:00", "19:00", "2"]]),
        Worksheet(3, "Журнал", [BOOKING_HEADERS]),
    ])
    store = Store(config)
    store._spreadsheet = spreadsheet
    document = store._adaptive.refresh(force=True)  # прогрев: маппинг до диалога
    mapped = sorted({rule["role"] for rule in document["regions"]})
    print("[mapping] области:", mapped, "issues:", len(document["issues"]))
    agent = Agent(config, store)
    return agent, ChatUser(777, None, "Live")


async def turn(agent, user, text):
    began = time.time()
    answer = await agent.reply(user, text)
    agent.mark_delivered(user.id, agent.delivery_token(user.id))
    catalog = agent._state.get(user.id, {}).get("catalog") or {}
    return {"question": text, "answer": answer, "seconds": round(time.time() - began, 1),
            "services": catalog.get("services", []), "size_classes": catalog.get("size_classes", [])}


async def amain():
    logging.basicConfig(level=logging.WARNING)
    results = []
    agent, user = build_agent()
    try:
        for question in ("Сколько стоит мыть кроссовер?",
                         "А полировка седана?",
                         "Хочу химчистку салона на кроссовере, есть время завтра в 10?"):
            results.append(await turn(agent, user, question))
    finally:
        await agent.close()
    report = {"ok": True, "turns": results}
    checks = {
        "catalog_from_mapping": any("Мойка кузова" in item["services"] for item in results),
        "price_grounded_800": "800" in results[0]["answer"],
        "price_grounded_2000": "2000" in results[1]["answer"],
        "no_mapping_leak": "маппинг" not in results[0]["answer"].lower()
                           and "JSON" not in results[0]["answer"],
    }
    for name, passed in checks.items():
        if not passed:
            report["ok"] = False
            report.setdefault("failed_checks", []).append(name)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["ok"] else 1


def main():
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
