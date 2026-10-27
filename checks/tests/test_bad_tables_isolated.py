"""Изолированные контрактные проверки семи локальных таблиц; без сети."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adaptive import MappingError
from ai import Agent, ChatUser
from sheets import Store
from tests.fakes import booking, config
from checks.fixtures.bad_tables.loader import load, make_spreadsheet, mutate

ROOT = Path(__file__).resolve().parents[1] / 'fixtures' / 'bad_tables'
FILES = sorted(ROOT.glob('0[1-7]_*.json'))

class Tables(unittest.TestCase):
    def setUp(self):
        self.tempdirs = []
        self.addCleanup(lambda: [item.cleanup() for item in self.tempdirs])

    def setup_case(self, number):
        case = load(next(ROOT.glob(f'{number:02}_*.json')))
        sheet = make_spreadsheet(case)
        temp = tempfile.TemporaryDirectory()
        self.tempdirs.append(temp)
        store = Store(config(temp.name))
        store._spreadsheet = sheet
        return case, store, sheet

    def snapshot(self, sheet):
        return deepcopy([s.values for s in sheet.sheets])

    def blocked(self, sheet, action):
        before = self.snapshot(sheet)
        try:
            result = action()
        except MappingError:
            result = {'ok': False}
        self.assertFalse(result.get('ok'), f'{result}; before={before[-1]!r}; after={self.snapshot(sheet)[-1]!r}')
        self.assertEqual(before, self.snapshot(sheet))
        self.assertFalse(any(s.writes for s in sheet.sheets))

    def test_seven_fixture_files(self):
        self.assertEqual(len(FILES), 7)

    def test_initial_discovery_persists_and_detects_ambiguous_change(self):
        _, store, sheet = self.setup_case(7)
        path = Path(store.config.sheets_mapping_file)
        self.assertFalse(path.exists())
        self.assertEqual(store.calculate_price('Легковой', 'Базовый', [])['total'], 630)
        self.assertTrue(path.exists())
        self.assertTrue(store._adaptive.document['writes_enabled'])
        reopened = Store(store.config)
        reopened._spreadsheet = sheet
        self.assertEqual(reopened.calculate_price('Легковой', 'Базовый', [])['total'], 630)
        headers = sheet.worksheet('Записи').values[0]
        headers.append('ID')
        self.blocked(sheet, lambda: reopened.create_booking(**booking(time_str='11:00')))

    def test_shifted_price_quote_availability_and_record(self):
        _, store, _ = self.setup_case(1)
        self.assertTrue(store.price_list()['ok'])
        q = store.calculate_price('Легковой', 'Базовый', [])
        self.assertEqual(q['total'], 630)
        self.assertEqual(q['lines'][0]['includes'], ['Мойка'])
        self.assertIn('Воск', q['lines'][0]['not_included_now'])
        self.assertIn('10:00', store.free_slots('2099-01-01')['days'][0]['free_times'])
        records = store.my_bookings(10101)['bookings']
        self.assertEqual([(r['booking_id'], r['date'], r['time'], int(r['total'])) for r in records], [('101', '2099-01-01', '10:00', 630)])

    def test_shifted_create_move_cancel(self):
        _, store, sheet = self.setup_case(1)
        created = store.create_booking(**booking(time_str='11:00'))
        self.assertTrue(created['ok'], created)
        moved = store.reschedule_booking(created['booking_id'], 111, '2099-01-01', '10:00')
        self.assertTrue(moved['ok'], moved)
        cancelled = store.cancel_booking(created['booking_id'], 111)
        self.assertTrue(cancelled['ok'], cancelled)
        self.assertEqual(len(sheet.worksheet('Записи').writes), 3)

    def test_unsafe_tables_block_create(self):
        for n in (2, 3, 4, 6):
            with self.subTest(case=n):
                _, store, sheet = self.setup_case(n)
                self.blocked(sheet, lambda: store.create_booking(**booking(time_str='11:00')))

    def test_unsafe_tables_block_cancel_and_move(self):
        for n in (2, 3, 4, 6):
            for operation in ('cancel', 'move'):
                with self.subTest(case=n, operation=operation):
                    _, store, sheet = self.setup_case(n)
                    fn = (lambda: store.cancel_booking(101, 10101)) if operation == 'cancel' else (lambda: store.reschedule_booking(101, 10101, '2099-01-01', '11:00'))
                    self.blocked(sheet, fn)

    def test_normal_catalog_and_availability(self):
        for n in (2, 3, 5, 6, 7):
            with self.subTest(case=n):
                _, store, _ = self.setup_case(n)
                self.assertTrue(store.price_list()['ok'])
                quote = store.calculate_price('Легковой', 'Базовый', [])
                self.assertEqual(quote['total'], 630)
                try:
                    slots = store.free_slots('2099-01-01')
                except MappingError:
                    self.assertIn(n, (2, 3, 6))
                else:
                    if n in (5, 7):
                        self.assertIn('11:00', slots['days'][0]['free_times'])
                    if n == 2:
                        self.assertTrue(slots['ok'])
                        self.assertEqual(set(slots['days'][0]['free_times']), {'10:00', '11:00'})
                        next_day = store.free_slots('2099-01-02')
                        self.assertIn('10:00', next_day['days'][0]['free_times'])

    def test_valid_catalog_and_race_lifecycle(self):
        for n in (5, 7):
            with self.subTest(case=n):
                _, store, sheet = self.setup_case(n)
                created = store.create_booking(**booking(time_str='11:00'))
                self.assertTrue(created['ok'], created)
                if n == 7:
                    cancelled = store.cancel_booking(created['booking_id'], 111)
                    self.assertTrue(cancelled['ok'], cancelled)
                    moved = store.reschedule_booking(101, 10101, '2099-01-01', '11:00')
                    self.assertTrue(moved['ok'], moved)
                else:
                    moved = store.reschedule_booking(created['booking_id'], 111, '2099-01-01', '10:00')
                    self.assertTrue(moved['ok'], moved)
                    cancelled = store.cancel_booking(created['booking_id'], 111)
                    self.assertTrue(cancelled['ok'], cancelled)
                self.assertEqual(len(sheet.worksheet('Записи').writes), 3)

    def test_catalog_contract(self):
        _, store, _ = self.setup_case(5)
        q = store.calculate_price('Легковой', 'Комплекс', [])
        self.assertTrue(q['ok'], q)
        self.assertEqual(q['total'], 630)
        self.assertEqual(q['lines'][0]['includes'], ['Мойка'])
        self.assertEqual(set(q['lines'][0]['not_included_now']), {'Воск', 'Несуществующая'})
        self.assertFalse(store.calculate_price('Легковой', 'Отключённый', [])['ok'])
        self.assertIsNone(store.get_catalog().resolve_class_rule('Тест Модель'))

    def test_empty_price_is_not_zero(self):
        _, store, sheet = self.setup_case(4)
        # Remove only the invalid price row; leave the blank price intact.
        sheet.worksheet('Услуги').values.pop(3)
        try:
            result = store.calculate_price('Легковой', None, ['Пена'])
        except MappingError:
            return
        self.assertFalse(result['ok'], result)

    def test_foreign_record_cannot_be_changed(self):
        for operation in ('cancel', 'move'):
            with self.subTest(operation=operation):
                _, store, sheet = self.setup_case(7)
                fn = (lambda: store.cancel_booking(101, 111)) if operation == 'cancel' else (lambda: store.reschedule_booking(101, 111, '2099-01-01', '11:00'))
                self.blocked(sheet, fn)

    def test_race_changes_after_preview(self):
        for name in ('occupied_slot', 'price_changed', 'row_shifted', 'cancelled'):
            with self.subTest(mutation=name):
                case, store, sheet = self.setup_case(7)
                quote = store.calculate_price('Легковой', 'Базовый', [], fresh=True)
                self.assertEqual(quote['total'], 630)
                self.assertIn('11:00', store.free_slots('2099-01-01')['days'][0]['free_times'])
                previous = store.my_bookings(10101)['bookings'][0]
                mutate(sheet, next(m for m in case['mutations'] if m['name'] == name))
                if name in ('occupied_slot', 'price_changed'):
                    fn = lambda: store.create_booking(**booking(time_str='11:00'), expected_quote=quote)
                else:
                    fn = lambda: store.cancel_booking(101, 10101, expected_booking=previous)
                self.blocked(sheet, fn)
                if name == 'price_changed':
                    self.assertEqual(store.calculate_price('Легковой', 'Базовый', [], fresh=True)['total'], 810)

    def test_move_rejects_shift_after_preview(self):
        case, store, sheet = self.setup_case(7)
        preview = store.reschedule_booking(101, 10101, '2099-01-01', '11:00', dry_run=True)
        self.assertTrue(preview['ok'], preview)
        change = deepcopy(next(m for m in case['mutations'] if m['name'] == 'row_shifted'))
        change['row'][3] = '12:00'
        mutate(sheet, change)
        self.blocked(sheet, lambda: store.reschedule_booking(101, 10101, '2099-01-01', '11:00', expected_booking=preview['booking']))

    def test_lost_reply_after_cancel_or_move_no_duplicate(self):
        for operation in ('cancel', 'move'):
            with self.subTest(operation=operation):
                _, store, sheet = self.setup_case(7)
                previous = store.my_bookings(10101)['bookings'][0]
                ws = sheet.worksheet('Записи')
                original = ws.batch_update
                def lost(updates, value_input_option=None):
                    original(updates, value_input_option=value_input_option)
                    raise OSError('response lost after apply')
                fn = (lambda: store.cancel_booking(101, 10101, expected_booking=previous)) if operation == 'cancel' else (lambda: store.reschedule_booking(101, 10101, '2099-01-01', '11:00', expected_booking=previous))
                with patch.object(ws, 'batch_update', side_effect=lost):
                    with self.assertRaises((MappingError, OSError)):
                        fn()
                after = self.snapshot(sheet)
                try:
                    result = fn()
                except MappingError:
                    result = {'ok': False}
                self.assertFalse(result['ok'], result)
                self.assertEqual(after, self.snapshot(sheet))
                self.assertEqual(len(ws.writes), 1)

    def test_lost_reply_after_write_no_duplicate(self):
        _, store, sheet = self.setup_case(7)
        ws = sheet.worksheet('Записи')
        original = ws.batch_update
        def lost(updates, value_input_option=None):
            original(updates, value_input_option=value_input_option)
            raise OSError('response lost after apply')
        with patch.object(ws, 'batch_update', side_effect=lost):
            with self.assertRaises(MappingError):
                store.create_booking(**booking(time_str='11:00'))
        after = self.snapshot(sheet)
        try:
            result = store.create_booking(**booking(time_str='11:00'))
        except MappingError:
            result = {'ok': False}
        self.assertFalse(result['ok'])
        self.assertEqual(after, self.snapshot(sheet))
        self.assertEqual(len(ws.writes), 1)

class Button(unittest.IsolatedAsyncioTestCase):
    async def test_reschedule_button_rejects_shifted_row(self):
        with tempfile.TemporaryDirectory() as d:
            case = load(next(ROOT.glob('07_*.json')))
            sheet = make_spreadsheet(case)
            store = Store(config(d)); store._spreadsheet = sheet
            with patch('ai.AsyncOpenAI'):
                agent = Agent(store.config, store)
            user = ChatUser(10101, None, 'Synthetic')
            args = {'booking_id': '101', 'date': '2099-01-01', 'time': '11:00'}
            result = await agent._prepare('reschedule_booking', args, user)
            self.assertTrue(result['ok'], result)
            agent._offer(user.id, agent._pending[user.id])
            offer = agent._pending[user.id]['offer_id']
            agent.mark_delivered(user.id, agent.delivery_token(user.id))
            change = deepcopy(next(m for m in case['mutations'] if m['name'] == 'row_shifted'))
            change['row'][3] = '12:00'
            mutate(sheet, change)
            before = deepcopy([s.values for s in sheet.sheets])
            answer = await agent.proposal_action(user, offer, 'confirm')
            self.assertIn('изменилось', answer)
            self.assertEqual(before, [s.values for s in sheet.sheets])
            self.assertFalse(any(s.writes for s in sheet.sheets))

    async def test_old_button_and_lost_reply(self):
        with tempfile.TemporaryDirectory() as d:
            sheet = make_spreadsheet(load(next(ROOT.glob('07_*.json'))))
            store = Store(config(d)); store._spreadsheet = sheet
            with patch('ai.AsyncOpenAI'):
                agent = Agent(store.config, store)
            user = ChatUser(111, None, 'Synthetic')
            args = dict(date='2099-01-01', time='11:00', client_name='Synthetic', phone='00000000000', car='Fixture car', size_class='Легковой', tariff='Базовый', services=[])
            self.assertTrue((await agent._prepare('create_booking', args, user))['ok'])
            agent._offer(user.id, agent._pending[user.id]); old = agent._pending[user.id]['offer_id']
            agent.mark_delivered(user.id, agent.delivery_token(user.id))
            await agent.proposal_action(user, old, 'edit')
            self.assertTrue((await agent._prepare('create_booking', args, user))['ok'])
            agent._offer(user.id, agent._pending[user.id]); current = agent._pending[user.id]['offer_id']
            agent.mark_delivered(user.id, agent.delivery_token(user.id))
            await agent.proposal_action(user, old, 'confirm')
            self.assertFalse(sheet.worksheet('Записи').writes)
            ws = sheet.worksheet('Записи'); original = ws.batch_update
            def lost(updates, value_input_option=None):
                original(updates, value_input_option=value_input_option)
                raise OSError('response lost after apply')
            with patch.object(ws, 'batch_update', side_effect=lost):
                answer = await agent.proposal_action(user, current, 'confirm')
            self.assertIn('Не удалось подтвердить', answer)
            before = deepcopy(ws.values)
            await agent.proposal_action(user, current, 'confirm')
            self.assertEqual(before, ws.values)
            self.assertEqual(len(ws.writes), 1)

class NewTable(unittest.TestCase):
    def test_new_table_creation_persistence_and_isolation(self):
        import os
        import json
        import run
        from config import _sheet_settings, table_mapping_path
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
                'SPREADSHEET_ID': 'synthetic-spreadsheet',
                'SHEETS_MAPPING_FILE': str(Path(d) / 'mapping.json')}, clear=True):
            old = Path(d) / 'mapping.json'
            old.write_text('{"spreadsheet_id": "previous"}', encoding='utf-8')
            old_journal = Path(d) / 'mapping-operations.json'
            old_journal.write_text('{"pending": true}', encoding='utf-8')
            case = load(next(ROOT.glob('07_*.json')))
            sheet = make_spreadsheet(case)
            def factory(cfg):
                store = Store(cfg); store._spreadsheet = sheet
                return store
            target = Path(run.prepare_new_table(config(d), factory))
            self.assertEqual(str(target), table_mapping_path('synthetic-spreadsheet'))
            self.assertEqual(json.loads(target.read_text())['spreadsheet_id'], 'synthetic-spreadsheet')
            self.assertEqual(old.read_text(), '{"spreadsheet_id": "previous"}')
            self.assertEqual(old_journal.read_text(), '{"pending": true}')
            self.assertFalse(any(ws.writes for ws in sheet.sheets))
            settings = _sheet_settings()
            self.assertEqual(settings['sheets_mapping_file'], str(target))
            self.assertTrue(settings['sheets_adaptive_enabled'])
            self.assertEqual(run.prepare_new_table(config(d), factory), str(target))

    def test_bad_new_table_never_replaces_previous_map(self):
        import os
        import run
        from config import table_mapping_path
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
                'SPREADSHEET_ID': 'synthetic-spreadsheet',
                'SHEETS_MAPPING_FILE': str(Path(d) / 'mapping.json')}, clear=True):
            old = Path(d) / 'mapping.json'; old.write_text('old', encoding='utf-8')
            sheet = make_spreadsheet(load(next(ROOT.glob('03_*.json'))))
            def factory(cfg):
                store = Store(cfg); store._spreadsheet = sheet
                return store
            with self.assertRaises(run.StartupError):
                run.prepare_new_table(config(d), factory)
            self.assertEqual(old.read_text(), 'old')
            self.assertFalse(Path(table_mapping_path('synthetic-spreadsheet')).exists())
            self.assertFalse(any(ws.writes for ws in sheet.sheets))
            self.assertEqual(len(list(Path(d).glob('*-candidate-*.json'))), 1)

    def test_existing_same_table_map_is_rebuilt_and_journal_preserved(self):
        import json
        import os
        import run
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
                'SPREADSHEET_ID': 'synthetic-spreadsheet',
                'SHEETS_MAPPING_FILE': str(Path(d) / 'mapping.json')}, clear=True):
            old = Path(d) / 'mapping.json'
            old.write_text('{"spreadsheet_id": "synthetic-spreadsheet", "obsolete": true}', encoding='utf-8')
            journal = Path(d) / 'mapping-operations.json'
            original_journal = '{"operation": {"state": "pending"}}'
            journal.write_text(original_journal, encoding='utf-8')
            sheet = make_spreadsheet(load(next(ROOT.glob('07_*.json'))))
            def factory(cfg):
                store = Store(cfg); store._spreadsheet = sheet
                return store
            self.assertEqual(run.prepare_new_table(config(d), factory), str(old))
            self.assertNotIn('obsolete', json.loads(old.read_text()))
            self.assertEqual(journal.read_text(), original_journal)
            self.assertFalse(any(ws.writes for ws in sheet.sheets))

    def test_invalid_rebuild_keeps_same_table_map_and_journal(self):
        import os
        import run
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
                'SPREADSHEET_ID': 'synthetic-spreadsheet',
                'SHEETS_MAPPING_FILE': str(Path(d) / 'mapping.json')}, clear=True):
            old = Path(d) / 'mapping.json'
            original = '{"spreadsheet_id": "synthetic-spreadsheet", "old": true}'
            old.write_text(original, encoding='utf-8')
            journal = Path(d) / 'mapping-operations.json'
            journal.write_text('{"pending": true}', encoding='utf-8')
            sheet = make_spreadsheet(load(next(ROOT.glob('03_*.json'))))
            def factory(cfg):
                store = Store(cfg); store._spreadsheet = sheet
                return store
            with self.assertRaises(run.StartupError):
                run.prepare_new_table(config(d), factory)
            self.assertEqual(old.read_text(), original)
            self.assertEqual(journal.read_text(), '{"pending": true}')
            self.assertFalse(any(ws.writes for ws in sheet.sheets))

    def test_new_table_flag_checks_before_start(self):
        import os
        import run
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True), \
             patch('sys.argv', ['run.py', '--new-table']), \
             patch.object(run, 'prepare_environment'), \
             patch.object(run, 'validate_config', return_value=config(d)), \
             patch.object(run, 'prepare_new_table', return_value=str(Path(d) / 'selected.json')) as prepare, \
             patch.object(run, 'run_forever') as start:
            self.assertEqual(run.main(), 0)
            prepare.assert_called_once()
            start.assert_called_once()
            self.assertEqual(os.environ['SHEETS_ADAPTIVE_ENABLED'], 'true')
            self.assertEqual(os.environ['SHEETS_MAPPING_FILE'], str(Path(d) / 'selected.json'))
