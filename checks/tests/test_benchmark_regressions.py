import asyncio
import json
import multiprocessing
import subprocess
import sys
import os
from pathlib import Path
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import date
from unittest.mock import AsyncMock, Mock, patch

from adaptive import MappingError
from ai import Agent, ChatUser
from inbox import Inbox
from tests.fakes import Worksheet, booking, fixture
from tests.test_duration_schedule import duration_store
from tests.test_conversation import ARGS


class SharedWorksheet(Worksheet):
    """Сетевой адаптер заменён файлом; бизнес-логика Store/Adaptive настоящая."""
    @property
    def values(self):
        if hasattr(self, "active_values"):return self.active_values
        if hasattr(self, "path"):return json.loads(self.path.read_text())
        return self.initial_values

    @values.setter
    def values(self, value):self.initial_values = value

    def mutate(self, fn, *args, **kw):
        self.active_values = json.loads(self.path.read_text())
        try:
            time.sleep(.02)
            value = fn(*args, **kw)
            tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.active_values));tmp.replace(self.path)
            return value
        finally:del self.active_values

    def batch_update(self, *a, **kw):return self.mutate(super().batch_update, *a, **kw)
    def append_row(self, *a, **kw):return self.mutate(super().append_row, *a, **kw)
    def update(self, *a, **kw):return self.mutate(super().update, *a, **kw)


def booking_writer(folder, number, barrier, queue):
    try:
        store, sheet = fixture(Path(folder)/str(number))
        sheet.worksheet("Слоты").values[1][2]="1"
        if hasattr(store.config, "booking_coordination_file"):
            store = type(store)(replace(store.config, booking_coordination_file=str(Path(folder)/'coordinator.sqlite3')))
        book = sheet.worksheet("Записи")
        shared = SharedWorksheet(book.id, book.title, book.values)
        shared.path = Path(folder)/"bookings.json"
        sheet.sheets[sheet.sheets.index(book)] = shared
        store._spreadsheet = sheet
        store.refresh_schema(force=True)
        barrier.wait(15)
        result = store.create_booking(**booking(telegram_id=9000+number, car=f"Car {number}"))
        queue.put(result)
    except BaseException as error:queue.put({"exception":type(error).__name__})


class SchedulingRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store, self.sheet = duration_store(self.temp.name, schedule=[["09:00","19:00","2"]])

    def test_named_bays_on_fixed_grid_require_explicit_duration_schedule(self):
        store,sheet=fixture(self.temp.name)
        sheet.sheets.append(Worksheet(40,"Ограничения боксов",[
            ["Бокс","Открытие поста","Закрытие поста","Статус"],
            ["A","09:00","19:00","ремонт"]]))
        with self.assertRaises(MappingError):store._get_slots()

    def test_peak_concurrency_not_total_intersections(self):
        schedule = self.store._get_slots()
        rows = [{"Дата":"01.01.2099","Время":t,"Длительность":"60","Статус":"Новая"} for t in ["10:00","11:00"]]
        self.assertIsNone(self.store._check_duration_start(schedule,rows,date(2099,1,1),"10:00",120))
        rows[1]["Время"]="10:30"
        self.assertIsNotNone(self.store._check_duration_start(schedule,rows,date(2099,1,1),"10:00",120))

    def test_resource_windows_join_and_conflicting_permissions_fail_closed(self):
        from resources import parse_bays,eligible_bays
        values=[["Бокс","Открытие поста","Закрытие поста","Ограничение","Статус"],
            ["A","09:00","12:00","Только легковые","работает"],
            ["A","12:00","19:00","Только легковые","работает"]]
        bays=parse_bays(values,["Легковой"],["Мойка","Салон"])
        self.assertEqual(eligible_bays(bays,date(2099,1,1),690,750,"Легковой",["Мойка"]),["A"])
        values[2][1]="11:00";values[2][3]="Только Салон"
        with self.assertRaises(MappingError):parse_bays(values,["Легковой"],["Мойка","Салон"])

    def test_variable_capacity_and_unknown_capacity(self):
        schedule = self.store._load_duration_schedule([
            {"Дата":"01.01.2099","Начало":"09:00","Конец":"12:00","Постов":"2"},
            {"Дата":"01.01.2099","Начало":"12:00","Конец":"19:00","Постов":"1"}])
        rows=[{"Дата":"01.01.2099","Время":"12:00","Длительность":"60","Статус":"Новая"}]
        self.assertIsNotNone(self.store._check_duration_start(schedule,rows,date(2099,1,1),"11:30",60))
        self.assertIsNone(self.store._check_duration_start(schedule,[],date(2099,1,1),"11:30",60))
        unknown=self.store._load_duration_schedule([{"Начало":"09:00","Конец":"19:00","Постов":""}])
        self.assertIn("вместимость",self.store._check_duration_start(unknown,[],date(2099,1,1),"11:00",60))

    def test_move_excludes_own_old_interval(self):
        self.sheet.worksheet(self.store.config.sheet_slots).values[1][2]="1"
        created=self.store.create_booking(**booking(time_str="10:00"))
        self.assertTrue(created["ok"],created)
        moved=self.store.reschedule_booking(created["booking_id"],111,"2099-01-01","10:30")
        self.assertTrue(moved["ok"],moved)

    def test_distributed_prices_and_conflicts(self):
        original=self.sheet.worksheet("Услуги")
        self.sheet.sheets.remove(original)
        self.sheet.sheets.extend([
            Worksheet(31,"Кузов",[["Услуга","Класс","Сумма","Доступна"],["Мойка","Легковой","700","да"]]),
            Worksheet(32,"Салон",[["Услуга","Класс","Сумма","Доступна"],["Салон","Легковой","300","да"]])])
        self.store.refresh_schema(force=True)
        quote=self.store.calculate_price("Легковой",None,["Мойка","Салон"],fresh=True)
        self.assertEqual(quote["total"],1000)
        self.sheet.worksheet("Салон").values.append(["Мойка","Легковой","900","да"])
        with self.assertRaises(MappingError):self.store.get_catalog(fresh=True)

    def test_named_bay_assignment_duration_repair_and_cancellation(self):
        header=self.sheet.worksheet("Записи").values[0];header.append("Бокс")
        self.sheet.sheets.append(Worksheet(40,"Ограничения боксов",[
            ["Бокс","Открытие поста","Закрытие поста","Ограничение","Статус"],
            ["Бокс 1","09:00","19:00","Кроссоверы и легковые","ремонт"],
            ["Бокс 2","09:00","17:00","Только легковые","работает"],
            ["Бокс 4","17:00","19:00","Только легковые","работает"]]))
        self.store.refresh_schema(force=True)
        bad=self.store.create_booking(**booking(time_str="10:00",size_class="Кроссовер",car="Toyota RAV4"))
        self.assertFalse(bad["ok"]);self.assertIn("допусками",bad["error"])
        good=self.store.create_booking(**booking(time_str="10:00"))
        self.assertTrue(good["ok"],good)
        rows=self.store._get_bookings()[1];self.assertEqual(rows[0]["Бокс"],"Бокс 2")
        self.assertFalse(self.store.create_booking(**booking(time_str="10:15",telegram_id=222))["ok"])
        moved=self.store.reschedule_booking(good["booking_id"],111,"2099-01-01","17:30")
        self.assertTrue(moved["ok"],moved)
        self.assertEqual(self.store._get_bookings()[1][0]["Бокс"],"Бокс 4")
        self.assertTrue(self.store.cancel_booking(good["booking_id"],111)["ok"])
        self.assertTrue(self.store.create_booking(**booking(time_str="17:30",telegram_id=333))["ok"])

    def test_independent_processes_share_booking_transaction(self):
        _,sheet=fixture(self.temp.name)
        path=Path(self.temp.name)/"bookings.json";path.write_text(json.dumps(sheet.worksheet("Записи").values))
        ctx=multiprocessing.get_context("spawn");queue=ctx.Queue();barrier=ctx.Barrier(4)
        workers=[ctx.Process(target=booking_writer,args=(self.temp.name,i,barrier,queue)) for i in range(4)]
        for p in workers:p.start()
        results=[queue.get(timeout=25) for _ in workers]
        for p in workers:p.join(10);self.assertEqual(p.exitcode,0)
        self.assertFalse(any("exception" in r for r in results),results)
        accepted=sum(bool(r.get("ok")) for r in results)
        rows=json.loads(path.read_text())
        self.assertEqual(accepted,1,results)
        self.assertEqual(len(rows),2)


class DialogueAndRecoveryRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_failure_keeps_response_and_later_sends_without_ai(self):
        import bot
        from aiogram.types import Message
        with tempfile.TemporaryDirectory() as folder:
            store,_=fixture(folder)
            with patch("ai.AsyncOpenAI",return_value=Mock(close=AsyncMock())):agent=Agent(replace(store.config,reply_min_delay_sec=0),store)
            self.addAsyncCleanup(agent.close)
            agent.reply=AsyncMock(return_value="Проверенный ответ")
            agent.slot_keyboard=AsyncMock(return_value=None)
            message=Message.model_validate({"message_id":1,"date":0,"chat":{"id":111,"type":"private"},
                "from":{"id":111,"is_bot":False,"first_name":"Test"},"text":"Когда мойка?"})
            sender=Mock(__aenter__=AsyncMock(),__aexit__=AsyncMock(return_value=False))
            reporter=Mock(dialog=AsyncMock())
            with patch("bot.ChatActionSender.typing",return_value=sender),patch.object(Message,"answer",new_callable=AsyncMock,side_effect=OSError("transport")):
                with self.assertRaises(OSError):await bot.ask_agent(message,agent,reporter,message.text)
            self.assertEqual(len(agent.inbox.pending()),1)
            transport=Mock(send_message=AsyncMock())
            await bot.resume_inbox(agent,transport,reporter,processed_only=True)
            self.assertEqual(agent.inbox.pending(),[])
            agent.reply.assert_awaited_once()
            transport.send_message.assert_awaited_once_with(111,"Проверенный ответ",reply_markup=None)

    async def test_typing_and_internal_error_still_produce_russian_response(self):
        import bot
        from aiogram.types import Message
        with tempfile.TemporaryDirectory() as folder:
            store,_=fixture(folder)
            with patch("ai.AsyncOpenAI",return_value=Mock(close=AsyncMock())):agent=Agent(replace(store.config,reply_min_delay_sec=0),store)
            self.addAsyncCleanup(agent.close)
            agent.reply=AsyncMock(side_effect=RuntimeError("synthetic failure"))
            agent.slot_keyboard=AsyncMock(side_effect=OSError("keyboard"))
            message=Message.model_validate({"message_id":2,"date":0,"chat":{"id":111,"type":"private"},
                "from":{"id":111,"is_bot":False,"first_name":"Test"},"text":"Мойка"})
            sender=Mock(__aenter__=AsyncMock(side_effect=OSError("typing")),__aexit__=AsyncMock(return_value=False))
            with patch("bot.ChatActionSender.typing",return_value=sender),patch.object(Message,"answer",new_callable=AsyncMock) as answer:
                await bot.ask_agent(message,agent,Mock(dialog=AsyncMock(side_effect=OSError("log"))),message.text)
            self.assertIn("Не удалось",answer.await_args.args[0])
            self.assertEqual(agent.inbox.pending(),[])

    async def test_inbox_survives_hard_process_exit_before_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"inbox.sqlite3"
            code="from inbox import Inbox; import os,sys; q=Inbox(sys.argv[1]); k=q.accept(111,7,{'id':111,'username':None,'full_name':'Test'},'Мойка'); q.prepare([k],'Проверенный ответ',{}); os._exit(73)"
            result=subprocess.run([sys.executable,"-c",code,str(path)],timeout=15)
            self.assertEqual(result.returncode,73)
            pending=Inbox(path).pending();self.assertEqual(len(pending),1)
            self.assertEqual(pending[0]["answer"],"Проверенный ответ")

    async def test_arbitrary_minute_is_checked_with_full_service_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            store,_=duration_store(folder)
            client=Mock(close=AsyncMock())
            with patch("ai.AsyncOpenAI",return_value=client):agent=Agent(store.config,store)
            self.addAsyncCleanup(agent.close)
            result=await agent._prepare("create_booking",dict(ARGS,time="11:17"),ChatUser(111,None,"Test"))
            self.assertTrue(result.get("confirmation_required"),result)
            self.assertEqual(agent._pending[111]["args"]["time"],"11:17")

    async def test_inbox_persists_unacknowledged_batch_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"inbox.sqlite3";q=Inbox(path);user={"id":111,"username":None,"full_name":"Test"}
            a=q.accept(111,1,user,"Мойка");b=q.accept(111,2,user,"На завтра")
            self.assertEqual(q.accept(111,1,user,"Мойка"),a)
            q.prepare([a,b],"Нужно уточнить время",{})
            restored=Inbox(path).pending();self.assertEqual(len(restored),1)
            self.assertEqual(restored[0]["keys"],[a,b]);self.assertEqual(restored[0]["answer"],"Нужно уточнить время")
            q.acknowledge([a,b]);self.assertEqual(q.pending(),[])
            self.assertTrue(q.delivered([a,b]))
