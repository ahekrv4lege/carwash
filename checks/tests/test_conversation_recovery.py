"""Штатная остановка и восстановление диалогов в новом процессе."""
import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ai import Agent, ChatUser
from config import ROOT
from bot import finish_updates, resume_recovered
from recovery import RecoveryMemory, SNAPSHOT_TTL_SEC
from tests.fakes import fixture
from tests.test_conversation import ARGS, response
from vehicles import SavedCar


def envelope(reply="Учту.", facts=None):
    return json.dumps({"reply": reply, "facts": facts or {}, "tentative": {}}, ensure_ascii=False)


class ConversationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        self.user = ChatUser(111, None, "Synthetic")
        self.agent = self.new_agent()

    def new_agent(self):
        with patch("ai.AsyncOpenAI"):
            agent = Agent(self.store.config, self.store)
        self.addCleanup(agent.recovery.close)
        self.addCleanup(agent.cars.close)
        agent.client.chat.completions.create = AsyncMock()
        return agent

    async def say(self, text, *outputs, agent=None, user=None, deliver=True):
        agent = agent or self.agent
        user = user or self.user
        agent.client.chat.completions.create.side_effect = list(outputs) or [response(text="Хорошо.")]
        answer = await agent.reply(user, text)
        if deliver:
            agent.mark_delivered(user.id, agent.delivery_token(user.id, answer))
            agent.mark_turn_delivered(user.id, answer)
        return answer

    async def restart(self, now=None):
        saved = await self.agent.save_conversations(now=now)
        self.assertGreaterEqual(saved, 0)
        agent = self.new_agent()
        restored = await agent.restore_conversations(now=now)
        return agent, restored

    async def test_last_four_pairs_facts_and_stage_survive_restart(self):
        for index in range(4):
            await self.say(f"Сообщение {index}", response(text=f"Ответ {index}"))
        await self.say(
            "Меня зовут Иван, 79990001122, у меня Toyota Camry, тариф Базовый, хочу на завтра в 10:00",
            response(calls=[("respond", {"reply": "Запомнила.", "facts": {
                "client_name": ["Иван", "Иван"],
                "phone": ["79990001122", "79990001122"],
                "car": ["Toyota Camry", "Toyota Camry"],
                "car_model": ["Toyota Camry", "Toyota Camry"],
                "size_class": ["Легковой", "Toyota Camry"],
                "date": ["2026-11-04", "завтра"],
                "time": ["10:00", "10:00"],
                "tariff": ["Базовый", "Базовый"],
            }, "tentative": {}})])
        )
        self.agent._state[111]["stage"] = "choosing_time"

        restarted, restored = await self.restart()
        self.assertEqual(restored, 1)
        history = restarted._history[111]
        self.assertEqual(len(history), 8)  # последние 4 пары
        self.assertEqual([message["role"] for message in history],
                         ["user", "assistant"] * 4)
        self.assertEqual(history[-2]["content"],
                         "Меня зовут Иван, [телефон скрыт], у меня Toyota Camry, тариф Базовый, хочу на завтра в 10:00")
        self.assertEqual(history[-1]["content"], "Запомнила.")
        facts = restarted._state[111]["facts"]
        self.assertEqual((facts["client_name"], facts["phone"]), ("Иван", "79990001122"))
        self.assertEqual(facts["car_model"], "Toyota Camry")
        self.assertEqual((facts["date"], facts["time"]), ("2026-11-04", "10:00"))
        self.assertEqual(restarted._state[111]["stage"], "choosing_time")
        self.assertEqual(restarted._state[111]["restored_vehicle"]["car"], "Toyota Camry")

    async def test_booking_halfway_is_restored_without_reasking_known_fields(self):
        self.agent._state[111].update(stage="collecting", facts={
            "client_name": "Иван", "phone": "79990001122", "car": "Toyota Camry",
            "car_model": "Toyota Camry", "size_class": "Легковой", "date": "2099-01-01",
            "tariff": "Базовый",
        }, vehicle_question=False)
        restarted, restored = await self.restart()
        self.assertEqual(restored, 1)
        facts = restarted._state[111]["facts"]
        self.assertEqual(facts["client_name"], "Иван")
        self.assertEqual(facts["phone"], "79990001122")
        self.assertEqual(facts["car_model"], "Toyota Camry")
        self.assertEqual(facts["date"], "2099-01-01")
        restarted._state[111]["facts"]["time"] = "10:00"
        result = await restarted._call_tool("create_booking", "{}", self.user)
        self.assertTrue(result.get("confirmation_required"), result)

    async def test_waiting_for_vehicle_clarification_survives_restart(self):
        self.agent._state[111].update(stage="collecting", vehicle_question=True,
                                      vehicle_family="BMW", vehicle_ambiguous=True,
                                      facts={"car": "bmx", "car_model": "BMW"})
        restarted, _ = await self.restart()
        state = restarted._state[111]
        self.assertTrue(state["vehicle_question"])
        self.assertTrue(state["vehicle_ambiguous"])
        self.assertEqual(state["vehicle_family"], "BMW")

    async def test_interrupted_ai_reply_resumes_once_after_restart(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(**kwargs):
            started.set()
            await release.wait()
            return response(text="Записала.")

        self.agent.client.chat.completions.create.side_effect = slow
        task = asyncio.create_task(self.agent.reply(self.user, "Хочу записаться на завтра в 10:00"))
        await asyncio.wait_for(started.wait(), 2)
        self.assertEqual(await self.agent.save_conversations(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        restarted = self.new_agent()
        self.assertEqual(await restarted.restore_conversations(), 1)
        pending = restarted.pending_recoveries()
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0][1]["processed"])
        self.assertEqual(pending[0][1]["text"], "Хочу записаться на завтра в 10:00")

        restarted.client.chat.completions.create.side_effect = [response(text="Продолжаем запись.")]
        bot, reporter = Mock(send_message=AsyncMock()), Mock(dialog=AsyncMock())
        await resume_recovered(restarted, bot, reporter)
        bot.send_message.assert_awaited()
        self.assertIn("Продолжаем запись.", bot.send_message.await_args.args[1])
        self.assertEqual(restarted.client.chat.completions.create.await_count, 1)
        self.assertEqual(restarted.pending_recoveries(), [])
        self.assertTrue(restarted._turns[111]["delivered"])

    async def test_processed_but_undelivered_answer_is_resent_without_ai(self):
        await self.say("Привет", response(text="Здравствуйте!"), deliver=False)
        self.assertEqual(await self.agent.save_conversations(), 1)
        restarted = self.new_agent()
        self.assertEqual(await restarted.restore_conversations(), 1)
        pending = restarted.pending_recoveries()
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0][1]["processed"])
        self.assertFalse(pending[0][1]["delivered"])

        bot, reporter = Mock(send_message=AsyncMock()), Mock(dialog=AsyncMock())
        await resume_recovered(restarted, bot, reporter)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.args[1], "Здравствуйте!")
        self.assertEqual(restarted.client.chat.completions.create.await_count, 0)
        self.assertEqual(restarted.pending_recoveries(), [])

    async def test_two_users_restore_their_own_state_and_profile(self):
        now = time.time()
        for user_id, name, stage in ((111, "Иван", "collecting"), (222, "Пётр", "choosing_time")):
            self.agent._state[user_id].update(stage=stage, facts={"client_name": name})
            self.agent._history[user_id] = [
                {"role": "user", "content": f"Привет от {user_id}"},
                {"role": "assistant", "content": f"Здравствуйте, {name}!"}]
            self.agent._last_activity[user_id] = now
            self.agent.recovery.save_profile(user_id, name, f"7999000{user_id}", now=now)
        self.assertEqual(await self.agent.save_conversations(now=now), 2)
        restarted = self.new_agent()
        self.assertEqual(await restarted.restore_conversations(now=now + 1), 2)
        self.assertEqual(restarted._state[111]["facts"]["client_name"], "Иван")
        self.assertEqual(restarted._state[222]["facts"]["client_name"], "Пётр")
        self.assertEqual(restarted._state[111]["stage"], "collecting")
        self.assertEqual(restarted._state[222]["stage"], "choosing_time")
        self.assertNotIn("Пётр", json.dumps(restarted._state[111], ensure_ascii=False))

    async def test_snapshot_only_covers_last_five_minutes_and_cleans_expired(self):
        now = time.time()
        self.agent._state[111].update(stage="collecting", facts={"client_name": "Свежий"})
        self.agent._last_activity[111] = now - 10
        self.agent._state[222].update(stage="collecting", facts={"client_name": "Старый"})
        self.agent._last_activity[222] = now - SNAPSHOT_TTL_SEC - 1
        self.assertEqual(await self.agent.save_conversations(now=now), 1)
        rows = await asyncio.to_thread(self.agent.recovery.load_snapshot, now)
        self.assertEqual([row["telegram_id"] for row in rows], [111])
        self.assertEqual(await asyncio.to_thread(
            self.agent.recovery.load_snapshot, now + SNAPSHOT_TTL_SEC + 2), [])

    async def test_permanent_profile_and_vehicle_survive_after_recovery_window(self):
        await self.say(
            "Меня зовут Иван, 79990001122, у меня Toyota Camry",
            response(calls=[("respond", {"reply": "Запомнила.", "facts": {
                "client_name": ["Иван", "Иван"], "phone": ["79990001122", "79990001122"],
                "car": ["Toyota Camry", "Toyota Camry"],
                "car_model": ["Toyota Camry", "Toyota Camry"],
                "size_class": ["Легковой", "Toyota Camry"]}, "tentative": {}})])
        )
        now = time.time()
        await self.agent.save_conversations(now=now)
        restarted = self.new_agent()
        self.assertEqual(await restarted.restore_conversations(now=now + SNAPSHOT_TTL_SEC + 30), 0)
        await self.say("Здравствуйте, хочу на мойку", response(text="Какую мойку хотите?"),
                       agent=restarted)
        facts = restarted._state[111]["facts"]
        self.assertEqual(facts["client_name"], "Иван")
        self.assertEqual(facts["phone"], "79990001122")
        self.assertEqual(facts["car_model"], "Toyota Camry")
        self.assertEqual(restarted._state[111]["restored_vehicle"]["car"], "Toyota Camry")

    async def test_deleted_vehicle_is_not_restored_from_snapshot(self):
        await self.say(
            "У меня Toyota Camry",
            response(calls=[("respond", {"reply": "Запомнила.", "facts": {
                "car": ["Toyota Camry", "Toyota Camry"],
                "car_model": ["Toyota Camry", "Toyota Camry"],
                "size_class": ["Легковой", "Toyota Camry"]}, "tentative": {}})])
        )
        car = self.agent.cars.for_user(111)[0]
        self.assertEqual(await self.agent.save_conversations(), 1)
        self.agent.cars.forget(111, car.id)
        restarted = self.new_agent()
        self.assertEqual(await restarted.restore_conversations(), 1)
        facts = restarted._state[111]["facts"]
        for field in ("car", "car_model", "size_class", "plate"):
            self.assertNotIn(field, facts)
        self.assertTrue(restarted._state[111]["vehicle_restored"])

    async def test_newer_profile_is_not_overwritten_by_older_snapshot(self):
        now = time.time()
        self.agent._state[111].update(stage="collecting", facts={
            "client_name": "Старое имя", "phone": "70000000000"})
        await self.agent.save_conversations(now=now)
        self.agent.recovery.save_profile(111, "Новое имя", "79990001122", now=now + 5)
        restarted = self.new_agent()
        self.assertEqual(await restarted.restore_conversations(now=now + 10), 1)
        facts = restarted._state[111]["facts"]
        self.assertEqual(facts["client_name"], "Новое имя")
        self.assertEqual(facts["phone"], "79990001122")

    async def test_snapshot_stores_individual_vehicle_id_not_model_id(self):
        self.agent._state[111].update(stage="collecting", facts={
            "car": "Toyota Camry", "car_model": "Toyota Camry"},
            restored_vehicle={"id": 77, "car": "Toyota Camry"})
        now = time.time()
        await self.agent.save_conversations(now=now)
        rows = await asyncio.to_thread(self.agent.recovery.load_snapshot, now)
        payload = rows[0]["payload"]
        self.assertEqual(payload["vehicle"]["id"], 77)
        self.assertNotIn("model_id", payload["vehicle"])

    async def test_two_vehicles_of_same_model_are_not_guessed_without_id(self):
        self.agent._state[111].update(stage="collecting", facts={
            "car": "Toyota Camry", "car_model": "Toyota Camry"})
        now = time.time()
        await self.agent.save_conversations(now=now)
        restarted = self.new_agent()
        same = [SavedCar(1, "Toyota Camry", "Легковой"), SavedCar(2, "Toyota Camry", "Легковой")]
        with patch.object(restarted.cars, "for_user", return_value=same):
            self.assertEqual(await restarted.restore_conversations(now=now + 1), 1)
        self.assertNotIn("restored_vehicle", restarted._state[111])
        self.assertEqual(restarted._state[111]["facts"]["car_model"], "Toyota Camry")

    async def test_individual_vehicle_id_wins_over_same_model_ambiguity(self):
        self.agent._state[111].update(stage="collecting", facts={
            "car": "Toyota Camry", "car_model": "Toyota Camry"},
            restored_vehicle={"id": 2, "car": "Toyota Camry"})
        now = time.time()
        await self.agent.save_conversations(now=now)
        restarted = self.new_agent()
        same = [SavedCar(1, "Toyota Camry", "Легковой"), SavedCar(2, "Toyota Camry", "Легковой")]
        with patch.object(restarted.cars, "for_user", return_value=same):
            self.assertEqual(await restarted.restore_conversations(now=now + 1), 1)
        self.assertEqual(restarted._state[111]["restored_vehicle"]["id"], 2)

    async def test_normal_messages_do_not_write_snapshot_to_disk(self):
        with patch.object(self.agent.recovery, "save_snapshot") as save:
            for index in range(3):
                await self.say(f"Вопрос {index}", response(text=f"Ответ {index}"))
        save.assert_not_called()

    async def test_snapshot_and_restore_are_fast_and_compact(self):
        now = time.time()
        for user_id in range(1, 101):
            self.agent._state[user_id].update(stage="collecting", facts={
                "client_name": f"Клиент {user_id}", "car_model": "Toyota Camry"})
            self.agent._last_activity[user_id] = now
            self.agent._history[user_id] = [
                {"role": "user" if index % 2 == 0 else "assistant",
                 "content": f"Сообщение {index} " * 20} for index in range(8)]
        started = time.perf_counter()
        self.assertEqual(await self.agent.save_conversations(now=now), 100)
        save_seconds = time.perf_counter() - started
        with sqlite3.connect(self.store.config.conversation_memory_file) as connection:
            count, total = connection.execute(
                "SELECT COUNT(*), SUM(LENGTH(payload)) FROM conversation_snapshots").fetchone()
        restarted = self.new_agent()
        started = time.perf_counter()
        self.assertEqual(await restarted.restore_conversations(now=now + 1), 100)
        restore_seconds = time.perf_counter() - started
        print(f"\nrecovery: save={save_seconds:.3f}s restore={restore_seconds:.3f}s "
              f"rows={count} bytes={total} per_user={total / count:.0f}")
        self.assertLess(save_seconds, 3)
        self.assertLess(restore_seconds, 3)
        self.assertLess(total / count, 20_000)

    async def test_graceful_shutdown_sequence_then_fresh_process_restores(self):
        await self.say("Привет", response(text="Здравствуйте!"))
        accepted = asyncio.create_task(asyncio.sleep(0.01))
        dispatcher = SimpleNamespace(_handle_update_tasks={accepted})
        await finish_updates(dispatcher)  # как в bot.main: ждём принятые обработчики
        self.assertTrue(accepted.done())
        self.assertEqual(await self.agent.save_conversations(), 1)
        script = (
            "import sys\n"
            "from recovery import RecoveryMemory\n"
            "store = RecoveryMemory(sys.argv[1])\n"
            "rows = store.load_snapshot()\n"
            "print(len(rows[0]['payload']['history']), rows[0]['payload']['history'][-1]['content'])\n"
            "store.close()\n")
        result = subprocess.run([sys.executable, "-c", script,
                                 self.store.config.conversation_memory_file],
                                cwd=str(ROOT), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "2 Здравствуйте!")

    async def test_fresh_python_process_reads_snapshot(self):
        self.agent._state[111].update(stage="collecting", facts={"client_name": "Иван"})
        await self.agent.save_conversations()
        script = (
            "import json, sys\n"
            "from recovery import RecoveryMemory\n"
            "store = RecoveryMemory(sys.argv[1])\n"
            "rows = store.load_snapshot()\n"
            "print(len(rows), rows[0]['payload']['facts']['client_name'])\n"
            "store.close()\n")
        result = subprocess.run([sys.executable, "-c", script,
                                 self.store.config.conversation_memory_file],
                                cwd=str(ROOT),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1 Иван")

    async def test_reset_clears_current_dialogue_but_keeps_permanent_memory(self):
        await self.say(
            "Меня зовут Иван, 79990001122, у меня Toyota Camry",
            response(calls=[("respond", {"reply": "Запомнила.", "facts": {
                "client_name": ["Иван", "Иван"], "phone": ["79990001122", "79990001122"],
                "car": ["Toyota Camry", "Toyota Camry"],
                "car_model": ["Toyota Camry", "Toyota Camry"],
                "size_class": ["Легковой", "Toyota Camry"]}, "tentative": {}})])
        )
        self.agent.reset(111)
        self.assertEqual(await self.agent.save_conversations(), 0)
        self.assertEqual(self.agent.recovery.profile(111)["name"], "Иван")
        self.assertEqual(self.agent.cars.for_user(111)[0].name, "Toyota Camry")
        await self.say("Привет", response(text="Здравствуйте!"))
        self.assertNotIn("client_name", self.agent._state[111].get("facts", {}))

    async def test_writing_stage_becomes_uncertain_and_is_not_replayed(self):
        self.agent._state[111].update(stage="writing", confirmed=True,
                                      facts={"client_name": "Иван", "phone": "79990001122"})
        self.agent._turns[111] = {"text": "Нажата кнопка «confirm».", "kind": "proposal",
                                  "processed": False, "delivered": False,
                                  "user": {"id": 111, "username": None, "full_name": "Synthetic"}}
        now = time.time()
        await self.agent.save_conversations(now=now)
        restarted = self.new_agent()
        self.assertEqual(await restarted.restore_conversations(now=now + 1), 1)
        self.assertEqual(restarted._state[111]["stage"], "uncertain")
        self.assertFalse(restarted._state[111]["confirmed"])
        self.assertEqual(restarted.pending_recoveries(), [])

    async def test_pending_proposal_after_restart_confirms_exactly_once(self):
        await self.say(
            "Хочу обсудить запись: " + json.dumps(dict(ARGS, car="Toyota Camry"), ensure_ascii=False),
            response(calls=[("get_price_list", {})]),
            response(calls=[("create_booking", dict(ARGS, car="Toyota Camry"))]))
        pending = self.agent._pending[111]
        offer_id = pending["offer_id"]
        self.assertTrue(pending["delivered"])
        self.assertEqual(await self.agent.save_conversations(), 1)

        restarted = self.new_agent()
        self.assertEqual(await restarted.restore_conversations(), 1)
        self.assertEqual(restarted._state[111]["stage"], "awaiting_confirmation")
        self.assertIn(restarted._pending[111]["offer_id"], {offer_id})
        answer = await restarted.proposal_action(self.user, offer_id, "confirm")
        self.assertIn("Записала", answer)
        self.assertEqual(len(self.spreadsheet.worksheet("Записи").writes), 1)
        self.assertNotIn(111, restarted._pending)

    async def test_finish_updates_waits_for_accepted_handlers_and_cancels_overdue(self):
        async def quick():
            await asyncio.sleep(0.01)

        async def slow():
            await asyncio.sleep(10)

        quick_task, slow_task = asyncio.create_task(quick()), asyncio.create_task(slow())
        dispatcher = SimpleNamespace(_handle_update_tasks={quick_task, slow_task})
        with patch("bot.SHUTDOWN_GRACE_SEC", 0.05):
            await finish_updates(dispatcher)
        self.assertTrue(quick_task.done())
        self.assertTrue(slow_task.cancelled())


if __name__ == "__main__":
    unittest.main()
