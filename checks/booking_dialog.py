"""Полный диалог: настоящий AI, временная таблица, без Telegram и production-записей."""
from datetime import timedelta

from ai import Agent, ChatUser
from tests.fakes import fixture


async def run_dialog(config, folder, emit=print):
    store, spreadsheet = fixture(folder)
    day = (store.now() + timedelta(days=1)).date().isoformat()
    spreadsheet.worksheet("Слоты").values.append([day, "11:00", "1"])
    agent = Agent(config, store)
    user = ChatUser(910001, None, "Тестовый клиент")
    calls = []
    call_tool = agent._call_tool

    async def trace_tool(name, arguments, client):
        calls.append(name)
        result = await call_tool(name, arguments, client)
        emit(f"Инструмент: {name}; ok={result.get('ok')}")
        if not result.get("ok"):
            emit(f"Аргументы: {arguments}; ошибка: {result.get('error')}")
        return result

    agent._call_tool = trace_tool

    async def say(text):
        emit(f"Клиент: {text}")
        answer = await agent.reply(user, text)
        emit(f"Бот: {answer}\n")
        agent.mark_delivered(user.id, agent.delivery_token(user.id, answer))
        assert not any(message in answer for message in (
            "Не удалось", "не получилось", "Некорректные аргументы", "Повторный запрос")), answer
        return answer

    try:
        for text in (
            "Привет! Хочу записаться на мойку. Меня зовут Никита.",
            "лансер девятый. Класс Легковой.",
            "Хочу услугу Мойка, без тарифа и дополнительных услуг. Сколько стоит?",
            "Хочу завтра в 11:00. Есть такое время?",
            "Мой телефон +7 000 111-22-33.",
            "Предложите запись на эти данные, я сначала проверю их.",
        ):
            await say(text)
            assert not store.my_bookings(user.id)["bookings"], "Запись создана до подтверждения"
            if user.id in agent._pending:
                break
        pending = agent._pending.get(user.id)
        assert pending and pending["action"] == "create_booking", "Бот не подготовил предложение записи"
        assert pending["args"]["date"] == day and pending["args"]["time"] == "11:00"
        assert pending["quote"]["total"] == 700
        assert not spreadsheet.worksheet("Записи").writes, "Запись в таблицу до подтверждения"
        await say("Да, подтверждаю запись")
        bookings = store.my_bookings(user.id)["bookings"]
        assert len(bookings) == 1 and bookings[0]["date"] == day and bookings[0]["time"] == "11:00"
        assert str(bookings[0]["total"]) == "700"
        writes = len(spreadsheet.worksheet("Записи").writes)
        for text in ("На когда у меня запись?", "А во сколько и сколько стоит?", "Покажи мои записи"):
            answer = await say(text)
            assert "11:00" in answer or "11.00" in answer, answer
            if "стоит" in text:
                assert "700" in answer, answer
        await say("Да, подтверждаю запись")
        assert len(store.my_bookings(user.id)["bookings"]) == 1, "Повторное подтверждение создало дубль"
        assert len(spreadsheet.worksheet("Записи").writes) == writes
        assert "get_my_bookings" in calls, "Бот не проверил свои записи инструментом"
        emit(f"ПРОЙДЕНО: одна запись {day} в 11:00 за 700 ₽, подтверждение и последующие вопросы. Дублей нет.")
        return bookings[0]
    finally:
        await agent.close()
