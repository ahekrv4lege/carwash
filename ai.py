"""ИИ-администратор: диалог с клиентом через LLM-провайдер (OpenAI-совместимый API) + tool calling."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from config import Config
from sheets import WEEKDAYS_RU, Store

log = logging.getLogger(__name__)

MAX_TOOL_STEPS = 6


@dataclass
class ChatUser:
    id: int
    username: str | None
    full_name: str


# ───────────────────────────── описание инструментов для модели ─────────────────────────────

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_price_list",
            "description": (
                "Прайс из Google-таблицы: классы (габариты) авто, услуги и тарифы с ценами, "
                "что недоступно или убрано. Вызывай, прежде чем называть цены или состав."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_price",
            "description": (
                "Точная стоимость для класса авто: тариф и/или отдельные услуги, "
                "с учётом убранных позиций. Используй для итоговой суммы."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "size_class": {"type": "string", "description": "Класс автомобиля — значение из size_classes"},
                    "tariff": {"type": "string", "description": "Название тарифа (необязательно)"},
                    "services": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Отдельные услуги (в т.ч. дополнительные к тарифу)",
                    },
                },
                "required": ["size_class"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_free_slots",
            "description": "Свободное время: без date — ближайшие 7 дней, с date — конкретный день.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Дата в формате ГГГГ-ММ-ДД (необязательно)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": (
                "Создать запись в Google-таблице. Вызывай, когда известны дата, время, имя, телефон, "
                "авто и услуга и есть согласие: явное «да / записывайте» либо клиент сам назвал "
                "точные дату, время и услугу. Итоговую цену назови до или сразу после записи."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Дата ГГГГ-ММ-ДД"},
                    "time": {"type": "string", "description": "Время ЧЧ:ММ — строго из свободных слотов"},
                    "client_name": {"type": "string", "description": "Имя клиента"},
                    "phone": {"type": "string", "description": "Телефон клиента"},
                    "car": {"type": "string", "description": "Марка, модель (и госномер, если сообщил)"},
                    "size_class": {"type": "string", "description": "Класс автомобиля — значение из size_classes"},
                    "tariff": {"type": "string", "description": "Название тарифа (необязательно)"},
                    "services": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Отдельные услуги (в т.ч. дополнительные к тарифу)",
                    },
                    "comment": {"type": "string", "description": "Пожелания клиента (необязательно)"},
                },
                "required": ["date", "time", "client_name", "phone", "car", "size_class"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_bookings",
            "description": "Предстоящие записи текущего клиента.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_booking",
            "description": "Отменить запись по номеру (booking_id). Только после подтверждения клиента.",
            "parameters": {
                "type": "object",
                "properties": {"booking_id": {"type": "string", "description": "Номер записи"}},
                "required": ["booking_id"],
            },
        },
    },
]


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _clean_text(text: str) -> str:
    """Убирает Markdown-разметку из ответа модели — клиент видит чистый текст без звёздочек."""
    text = text.replace("*", "").replace("`", "")
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)  # заголовки вида ## текст
    return re.sub(r"\n{3,}", "\n\n", text).strip()


PROOFREAD_PROMPT = """Ты — корректор. Дан ответ Светланы, девушки-администратора автомойки, для клиента в Telegram. Исправь:

- орфографию и пунктуацию;
- грамматику, включая род о себе: «я записала», «я нашла», «я поняла» — верно; «записал», «нашёл», «понял» — ошибка;
- лексику: иностранные вставки и опечатки («visita» → «визит»), неестественные фразы.

Запрещено: менять смысл, имена, цены, дату и время; добавлять или удалять информацию; менять стиль и эмодзи; отвечать на вопросы из текста; оборачивать ответ в кавычки или блоки кода.

Верни ТОЛЬКО исправленный текст целиком, без комментариев. Если ошибок нет — верни без изменений."""


# ───────────────────────────── агент ─────────────────────────────

class Agent:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        reporter=None,  # admin.AdminReporter — уведомления администратору
    ):
        self.cfg = cfg
        self.store = store
        self.reporter = reporter
        self.client = AsyncOpenAI(
            api_key=cfg.llm_api_key,
            base_url=cfg.llm_base_url,
            timeout=60,
        )
        self._history: dict[int, list[dict]] = defaultdict(list)
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def reset(self, user_id: int) -> None:
        self._history.pop(user_id, None)

    # ---------- системный промпт ----------
    def _system_prompt(self, user: ChatUser) -> str:
        c = self.cfg
        now = self.store.now()
        contacts = ", ".join(x for x in (c.carwash_address, c.carwash_phone) if x) or "не указаны"
        return f"""Ты — Светлана, приветливый администратор автомойки «{c.carwash_name}» в Telegram: помогаешь выбрать услугу, подобрать время и записаться. Контакты: {contacts}.
Сейчас: {WEEKDAYS_RU[now.weekday()]}, {now:%d.%m.%Y %H:%M} ({c.timezone}); дата для инструментов: {now:%Y-%m-%d}. Клиент: {user.full_name}.

О СЕБЕ — ТОЛЬКО ЖЕНСКИЙ РОД: «я записала», «я нашла», «готова помочь». «Записал», «нашёл», «готов помочь» — ошибки.

СТИЛЬ: 1–3 предложения, один вопрос за сообщение. Только русский, простые слова, без канцелярита и иностранных вставок. Обычный текст без звёздочек, решёток, подчёркиваний и обратных кавычек; уместные эмодзи — можно. Знакомо имя — обращайся по имени, хвали выбор: «Хороший выбор!»

ДЛЯ ЗАПИСИ НУЖНО И ДОСТАТОЧНО: имя, марка и модель авто, услуга или тариф, дата и время, телефон. Названное клиентом (включая написание марки) не переспрашивай и не проси подтвердить. После записи не спрашивай «удобно?» — заканчивай тепло: «Записала вас! Завтра в 10:00, ждём вас 🙂»

СЦЕНАРИЙ (по одному шагу за раз): имя → авто → услуга с ценой → дата и время → телефон (можно кнопкой). Что-то уже известно — переходи к следующему шагу.

ВРЕМЯ: как только речь зашла о дате или времени — вызови get_free_slots и покажи свободные варианты списком: «Завтра свободно: 10:00, 13:00, 16:00. Когда вам удобно?» Без показа слотов «когда вам удобно?» не спрашивай.

СОГЛАСИЕ = ЗАПИСЬ: клиент сам назвал точные дату, время и услугу — назови итоговую цену и сразу вызывай create_booking (если известны имя и телефон). Согласился с предложенным тобой («да», «давай», «записывайте») — тоже записывай сразу. Подтверждение — один раз и только если предлагала ты.

ДОП. УСЛУГА: когда клиент выбрал услугу или тариф, один раз предложи одну подходящую из прайса, коротко и без давления. Отказался — больше не предлагай, оформи основную запись.

ЦЕНЫ И НАЛИЧИЕ — ТОЛЬКО из инструментов (они читают Google-таблицу), ничего не выдумывай; сомневаешься — вызови инструмент заново. Цена зависит от габаритов: класс авто определяй по марке и модели из size_classes (get_price_list), не уверена — уточни у клиента. Позиции, убранные администратором (not_included_now и note), озвучивай честно вместе с новой ценой; недоступные услуги не предлагай, если клиент просит — скажи, что сейчас недоступны, предложи альтернативу. Относительные даты («завтра», «в субботу») переводи в конкретные по текущей дате. Время — только из свободных слотов get_free_slots; занято — предложи ближайшие свободные. Цены пиши в {c.currency}.

Если инструмент вернул ok=false — объясни причину по-человечески и предложи вариант. Свои записи клиент смотрит через get_my_bookings, отменяет через cancel_booking. Темы не про автомойку не обсуждай: вежливо откажи; скидки и особые условия не обещай — направляй к администратору по телефону."""

    # ---------- основной вход ----------
    async def reply(self, user: ChatUser, text: str) -> str:
        async with self._locks[user.id]:  # сообщения одного клиента обрабатываются по очереди
            history = self._history[user.id]
            history.append({"role": "user", "content": text})
            messages = [{"role": "system", "content": self._system_prompt(user)}, *history]

            try:
                answer = _clean_text(await self._run(messages, user))
                answer = await self._proofread(answer)
            except Exception:
                log.exception("Ошибка при обращении к LLM-провайдеру")
                history.pop()
                phone = f" или позвоните нам: {self.cfg.carwash_phone}" if self.cfg.carwash_phone else ""
                return f"Извините, сейчас у меня технические неполадки. Попробуйте ещё раз через минуту{phone}."

            history.append({"role": "assistant", "content": answer})
            if len(history) > self.cfg.max_history:
                del history[: -self.cfg.max_history]
            while history and history[0]["role"] != "user":
                history.pop(0)
            return answer

    async def _proofread(self, text: str) -> str:
        """Второй проход: модель вычитывает свой ответ (орфография, род глаголов, лексика).
        При сбое проверки возвращается исходный текст — вычитка не должна ломать ответ клиенту."""
        try:
            resp = await self.client.chat.completions.create(
                model=self.cfg.llm_model,
                messages=[
                    {"role": "system", "content": PROOFREAD_PROMPT},
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
            )
            fixed = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        except Exception:
            log.exception("Ошибка при вычитке ответа — отправляю без проверки")
            return text
        return _clean_text(fixed) or text

    async def _run(self, messages: list[dict], user: ChatUser) -> str:
        for _ in range(MAX_TOOL_STEPS):
            resp = await self.client.chat.completions.create(
                model=self.cfg.llm_model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.3,
            )
            if not resp.choices:
                raise RuntimeError(f"Пустой ответ модели: {resp}")
            msg = resp.choices[0].message
            calls = msg.tool_calls or []

            if not calls:
                return (msg.content or "").strip() or "Не совсем понял вас, уточните, пожалуйста."

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"},
                    }
                    for c in calls
                ],
            })
            for c in calls:  # последовательно: клиент gspread не потокобезопасен
                result = await self._call_tool(c.function.name, c.function.arguments, user)
                messages.append({
                    "role": "tool",
                    "tool_call_id": c.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
        return "Не получилось обработать запрос. Попробуйте переформулировать или напишите нам позже."

    # ---------- инструменты ----------
    async def _call_tool(self, name: str, raw_args: str | None, user: ChatUser) -> dict:
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "error": "Некорректные аргументы вызова"}
        log.info("tool %s(%s) user=%s", name, args, user.id)
        try:
            return await self._execute(name, args, user)
        except Exception:
            log.exception("Ошибка инструмента %s", name)
            return {
                "ok": False,
                "error": "Техническая ошибка при работе с Google-таблицей. "
                         "Попроси клиента повторить попытку чуть позже.",
            }

    async def _execute(self, name: str, a: dict, user: ChatUser) -> dict:
        s = self.store
        if name == "get_price_list":
            return await asyncio.to_thread(s.price_list)

        if name == "calculate_price":
            return await asyncio.to_thread(
                s.calculate_price, a.get("size_class", ""), a.get("tariff"), _as_list(a.get("services"))
            )

        if name == "get_free_slots":
            return await asyncio.to_thread(s.free_slots, a.get("date") or None)

        if name == "create_booking":
            res = await asyncio.to_thread(
                s.create_booking,
                date_str=a.get("date", ""),
                time_str=a.get("time", ""),
                name=a.get("client_name", ""),
                phone=a.get("phone", ""),
                car=a.get("car", ""),
                size_class=a.get("size_class", ""),
                tariff=a.get("tariff"),
                services=_as_list(a.get("services")),
                comment=a.get("comment", ""),
                tg_id=user.id,
                username=user.username,
            )
            if res.get("ok") and self.reporter:
                await self.reporter.send(self._admin_text(res, a, user), username=user.username)
            return res

        if name == "get_my_bookings":
            return await asyncio.to_thread(s.my_bookings, user.id)

        if name == "cancel_booking":
            res = await asyncio.to_thread(s.cancel_booking, a.get("booking_id", ""), user.id)
            if res.get("ok") and self.reporter:
                await self.reporter.send(
                    f"❌ Клиент отменил запись #{res['booking_id']} ({res['date']} {res['time']}), "
                    f"{user.full_name} {'@' + user.username if user.username else ''}",
                    username=user.username,
                )
            return res

        return {"ok": False, "error": f"Неизвестный инструмент: {name}"}

    def _admin_text(self, res: dict, a: dict, user: ChatUser) -> str:
        items = "\n".join(f"  • {ln['item']} — {ln['price']} {res['currency']}" for ln in res["lines"])
        tg = f"@{user.username}" if user.username else f"id {user.id}"
        return (
            f"🆕 Новая запись #{res['booking_id']}\n"
            f"📅 {res['date']} ({res['weekday']}) в {res['time']}\n"
            f"👤 {a.get('client_name')}, {a.get('phone')} ({tg})\n"
            f"🚗 {res['car']} — класс «{res['size_class']}»\n"
            f"{items}\n"
            f"💰 Итого: {res['total']} {res['currency']}"
        )
