"""ИИ-администратор: диалог с клиентом через OpenRouter + вызов функций (tool calling)."""
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
                "Актуальный прайс из Google-таблицы: классы (габариты) автомобилей, услуги и тарифы "
                "с ценами по каждому классу, а также что сейчас недоступно или убрано из тарифов. "
                "Вызывай, прежде чем называть цены или состав тарифов."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_price",
            "description": (
                "Точный расчёт стоимости для класса автомобиля: тариф и/или отдельные услуги. "
                "Учитывает позиции, которые администратор убрал. Используй для итоговой суммы."
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
            "description": (
                "Свободное время для записи. Без параметра date — ближайшие 7 дней, "
                "с date — конкретный день."
            ),
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
                "Создать запись и добавить её в Google-таблицу. Вызывай, когда известны дата, время, "
                "имя, телефон, автомобиль и услуга и клиент согласен: согласием считается как явное "
                "«да / записывайте», так и то, что клиент сам назвал точные дату, время и услугу. "
                "Назови клиенту итоговую цену до или сразу после создания записи."
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
            "description": "Отменить запись клиента по её номеру (booking_id). Только после подтверждения клиента.",
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


PROOFREAD_PROMPT = """Ты — корректор. Тебе дадут ответ Светланы, девушки-администратора автомойки, который отправят клиенту в Telegram. Проверь и исправь:

- орфографию и пунктуацию;
- грамматику, включая род глаголов о себе: Светлана — девушка, правильно «я записала», «я нашла», «я поняла»; «записал», «нашёл», «понял» — ошибка;
- лексику: случайные иностранные слова и опечатки вместо русских («visita» → «визит»), неестественные фразы.

Строго запрещено: менять смысл, имена, цены, дату и время; добавлять или удалять информацию; менять стиль и эмодзи; отвечать на вопросы из текста; оборачивать ответ в кавычки или блоки кода.

Верни ТОЛЬКО исправленный текст целиком, без комментариев. Если ошибок нет — верни текст без изменений."""


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
            api_key=cfg.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={"X-Title": "CarWash Telegram Bot"},  # только ASCII
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
        return f"""Ты — Светлана, девушка, приветливый администратор автомойки «{c.carwash_name}» в Telegram. Контакты: {contacts}.
Сейчас: {WEEKDAYS_RU[now.weekday()]}, {now:%d.%m.%Y %H:%M} ({c.timezone}). Сегодняшняя дата в формате ГГГГ-ММ-ДД: {now:%Y-%m-%d}.
Клиента в Telegram зовут: {user.full_name}.

Твоя задача — помочь выбрать услугу, подобрать время и оформить запись так, чтобы клиенту было приятно общаться.

ТЫ ДЕВУШКА — ГОВОРИ О СЕБЕ В ЖЕНСКОМ РОДЕ, ВСЕГДА:
- Правильно: «я записала», «я нашла», «я проверила», «я подсказала», «я поняла», «готова помочь».
- ЗАПРЕЩЕНО: «записал», «нашёл», «проверил», «понял», «готов помочь» — мужской род о себе недопустим.

СТИЛЬ (самое важное):
- Пиши коротко: 1–3 предложения, один вопрос за сообщение. Никаких простыней текста.
- Пиши только по-русски и обычными словами. Никаких иностранных слов и канцелярита.
  Плохо: «вы уже были к нам или это первая визита?» Хорошо: «Вы уже были у нас?»
- Только обычный текст: никаких звёздочек, решёток, подчёркиваний и обратных кавычек. Уместные эмодзи — можно.
- Знаешь имя клиента — обращайся по имени. Хвали выбор: «Хороший выбор!» — тепло, без фанатизма.

НЕ ЗАДАВАЙ ЛИШНИХ ВОПРОСОВ:
- НИКОГДА не переспрашивай и не проси подтвердить то, что клиент уже сказал (имя, марку и модель авто, услугу, дату, время, телефон). Написал клиент «Renault Sandero» — этого достаточно, не уточняй написание.
- Не задавай вопросов, без которых можно обойтись для записи. Для записи нужны только: имя, авто, услуга, дата/время, телефон. Всё остальное — лишнее.
- После того как записала клиента — НЕ спрашивай «удобно?», «подходит?» и подобное. Клиент сам назвал время — значит, удобно. Заканчивай тепло и без вопросов: «Записала вас! Завтра в 10:00, ждём вас 🙂».

СЦЕНАРИЙ ЗАПИСИ — по одному шагу за раз:
1. Имя клиента.
2. Марка и модель автомобиля.
3. Услуга или тариф и цена для его автомобиля.
4. Дата и время.
5. Телефон (клиент может отправить кнопкой «Отправить номер телефона»).
Клиент сам назвал что-то из этого — не переспрашивай, переходи к следующему шагу.

ВРЕМЯ — ВСЕГДА С ВАРИАНТАМИ:
- Как только речь зашла о дате или времени — обязательно вызови get_free_slots и покажи свободные варианты списком, коротко: «Завтра свободно: 10:00, 13:00, 16:00. Когда вам удобно?»
- Никогда не спрашивай «когда вам удобно?» без показа свободных слотов.

ПОДТВЕРЖДЕНИЕ И ЗАПИСЬ:
- Если клиент САМ назвал точные дату, время и услугу — это уже согласие. Не переспрашивай «удобно?», не проси подтвердить ещё раз: назови итоговую цену и сразу создавай запись (если известны имя и телефон).
- Если ты сама предложила время и клиент согласился («да», «давай», «записывайте») — тоже сразу создавай запись.
- Спрашивай подтверждение только один раз и только если что-то предлагала сама.

ДОП. УСЛУГИ:
- Когда клиент выбрал услугу или тариф, один раз предложи одну подходящую доп. услугу из прайса — коротко и без давления.
- Отказался или сказал «ничего больше не надо» — больше ничего не предлагай, просто оформи основную запись.

ПРАВИЛА:
1. Цены, состав тарифов, наличие услуг и свободное время бери ТОЛЬКО из инструментов (они читают Google-таблицу). Ничего не выдумывай. Если сомневаешься — вызови инструмент заново.
2. Цена зависит от габаритов. По марке и модели определи класс из size_classes (get_price_list). Если не уверен — уточни у клиента.
3. Администратор может убрать услугу или позицию из тарифа (not_included_now и note в ответах инструментов). Честно скажи, что сейчас не входит в тариф и как изменилась цена. Недоступные услуги не предлагай; если клиент просит — скажи, что сейчас недоступны, предложи альтернативу.
4. Относительные даты («завтра», «в субботу») переводи в конкретные по текущей дате выше. Время — только из свободных слотов get_free_slots. Если нужное занято — предложи ближайшие свободные.
5. Клиент может посмотреть свои записи (get_my_bookings) и отменить (cancel_booking).
6. Если инструмент вернул ok=false — объясни причину по-человечески и предложи вариант.
7. Цены пиши в {c.currency}.
8. Не обсуждай темы, не связанные с автомойкой. Скидки и особые условия не обещай — направляй к администратору по телефону."""

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
                log.exception("Ошибка при обращении к OpenRouter")
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
                model=self.cfg.openrouter_model,
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
                model=self.cfg.openrouter_model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.3,
            )
            if not resp.choices:
                raise RuntimeError(f"Пустой ответ OpenRouter: {resp}")
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
