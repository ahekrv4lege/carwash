from copy import deepcopy
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

from adaptive import AdaptiveSheets, MappingError, atomic_json
from tests.fakes import suggest_mappings
from sheets import BOOKING_HEADERS, Store
from tests.fakes import Worksheet, fixture, booking


class AdaptiveTests(unittest.TestCase):
    def test_free_slots_reuses_discovery_and_batches_expired_availability(self):
        self.assertEqual(self.store.free_slots("2099-01-01")["days"][0]["free_times"], ["10:00"])
        self.assertEqual(sum(sheet.reads for sheet in self.spreadsheet.sheets), 8)
        self.assertEqual(self.spreadsheet.batch_reads, 2)
        self.assertIsNone(self.store._observed)
        reads = {sheet.title: sheet.reads for sheet in self.spreadsheet.sheets}
        before = self.spreadsheet.batch_reads
        self.store._invalidate("slots", "bookings")
        self.spreadsheet.worksheet("Слоты").values[1][2] = "0"
        self.assertFalse(self.store.free_slots("2099-01-01")["days"][0]["free_times"])
        self.assertEqual(self.spreadsheet.batch_reads - before, 1)
        for sheet in self.spreadsheet.sheets:
            self.assertEqual(sheet.reads - reads[sheet.title], int(sheet.title in {"Слоты", "Записи"}))
        before = sum(sheet.reads for sheet in self.spreadsheet.sheets)
        self.store.free_slots("2099-01-01")
        self.assertEqual(sum(sheet.reads for sheet in self.spreadsheet.sheets), before)

    def test_free_slots_does_not_refresh_a_still_valid_booking_cache(self):
        self.store._get_bookings()
        before = self.spreadsheet.worksheet("Записи").reads
        self.store.free_slots("2099-01-01")
        self.assertEqual(self.spreadsheet.worksheet("Записи").reads, before)
        self.assertIsNone(self.store._observed)

    def test_free_slots_reuses_outer_snapshot_and_restores_it(self):
        with self.store.fresh_tables():
            observed = self.store._observed
            before = self.spreadsheet.batch_reads
            self.store.free_slots("2099-01-01")
            self.assertIs(self.store._observed, observed)
            self.assertEqual(self.spreadsheet.batch_reads, before)
        self.assertIsNone(self.store._observed)

    def test_failed_availability_batch_does_not_leak_snapshot_or_cached_result(self):
        self.a.refresh()
        with patch.object(self.spreadsheet, "values_batch_get", return_value={"valueRanges": []}):
            with self.assertRaises(MappingError):
                self.store.free_slots("2099-01-01")
        self.assertIsNone(self.store._observed)
        self.assertNotIn("slots", self.store._cache)
        self.assertNotIn("bookings", self.store._cache)
        self.assertEqual(self.store.free_slots("2099-01-01")["days"][0]["free_times"], ["10:00"])

    def test_invalid_slot_date_needs_no_google_reads_in_either_mode(self):
        legacy = Store(replace(self.store.config, sheets_adaptive_enabled=False))
        legacy._spreadsheet = self.spreadsheet
        for store in (self.store, legacy):
            for text in ("not-a-date", "2026-02-30"):
                self.assertFalse(store.free_slots(text)["ok"])
        self.assertEqual(self.spreadsheet.metadata_reads, 0)
        self.assertEqual(sum(sheet.reads for sheet in self.spreadsheet.sheets), 0)

    def test_catalog_reuses_discovery_values_and_batches_only_price_roles(self):
        self.assertEqual(self.store.calculate_price("Легковой", "Базовый", [])["total"], 630)
        self.assertEqual(self.spreadsheet.batch_reads, 2)
        self.assertEqual(sum(sheet.reads for sheet in self.spreadsheet.sheets), 8)
        reads = {sheet.title: sheet.reads for sheet in self.spreadsheet.sheets}
        before = self.spreadsheet.batch_reads
        self.spreadsheet.worksheet("Услуги").values[1][2] = "900"
        self.assertEqual(self.store.calculate_price("Легковой", "Базовый", [], fresh=True)["total"], 810)
        self.assertEqual(self.spreadsheet.batch_reads - before, 1)
        for sheet in self.spreadsheet.sheets:
            self.assertEqual(sheet.reads - reads[sheet.title], int(sheet.title in {"Услуги", "Тарифы"}))
        self.assertIsNone(self.store._observed)
        before = self.spreadsheet.batch_reads
        self.store.get_catalog()
        self.assertEqual(self.spreadsheet.batch_reads, before)

    def test_incomplete_price_batch_is_rejected_and_does_not_poison_cache(self):
        self.a.refresh()
        with patch.object(self.spreadsheet, "values_batch_get", return_value={"valueRanges": []}):
            with self.assertRaises(MappingError):
                self.store.get_catalog(fresh=True)
        self.assertNotIn("catalog", self.store._cache)
        self.assertIsNone(self.store._observed)
        self.assertEqual(self.store.calculate_price("Легковой", "Базовый", [])["total"], 630)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        self.a = self.store._adaptive
        self.worksheet = self.spreadsheet.worksheet("Записи")

    def rule(self, role="bookings"):
        return next(r for r in self.a.refresh()["regions"] if r["role"] == role)

    def save(self):
        atomic_json(self.a.path, self.a.document)

    def test_standard_booking_quote_create_cancel_and_duplicate(self):
        result = self.store.create_booking(**booking())
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 630)
        self.assertEqual(len(self.store.my_bookings(111)["bookings"]), 1)
        self.assertFalse(self.store.create_booking(**booking())["ok"])
        self.assertTrue(self.store.cancel_booking(1, 111)["ok"])
        self.assertEqual(self.worksheet.values[1][-1], "Отменена")
        self.assertEqual(self.store.my_bookings(111)["bookings"], [])

    def test_alternative_sheet_and_column_names(self):
        names = ["Booking ID", "Created at", "Booking date", "Start time", "Client", "Phone", "Vehicle",
                 "Car class", "Package", "Extras", "Items", "Total", "Notes", "TG_ID", "Username", "Status"]
        self.worksheet.values[0] = names
        self.worksheet.title = "Any bookings"
        self.assertTrue(self.store.create_booking(**booking())["ok"])
        self.assertEqual(self.worksheet.values[1][4], "Synthetic")
        self.assertEqual(self.worksheet.values[1][13], "111")

    def test_reordered_columns(self):
        self.worksheet.values[0] = list(reversed(BOOKING_HEADERS))
        self.assertTrue(self.store.create_booking(**booking())["ok"])
        self.assertEqual(self.worksheet.values[1][-1], 1)
        self.assertEqual(self.worksheet.values[1][0], "Новая")

    def test_inserted_column_preserves_existing_mappings(self):
        self.a.refresh()
        self.worksheet.values[0].insert(3, "Internal note")
        self.a.refresh(force=True)
        rule = self.rule()
        self.assertEqual(rule["columns"]["Время"], 5)
        self.assertIn(4, rule["last_changes"]["added"])
        self.assertTrue(self.store.create_booking(**booking())["ok"])
        self.assertEqual(self.worksheet.values[1][3], "")

    def test_known_rename_is_remapped(self):
        self.a.refresh()
        self.worksheet.values[0][3] = "Start time"
        self.a.refresh(force=True)
        self.assertTrue(self.rule()["validation"]["write"])
        self.assertEqual(len(self.rule()["last_changes"]["renamed"]), 1)

    def test_unknown_rename_requires_manual_revalidation(self):
        self.a.refresh()
        self.worksheet.values[0][3] = "Arrival window"
        self.a.refresh(force=True)
        self.assertFalse(self.rule()["validation"]["read"])
        with self.assertRaises(MappingError):
            self.store.create_booking(**booking())
        rule = self.rule()
        rule["columns"]["Время"] = 4
        self.save()
        self.a.refresh(validate=True)
        self.assertTrue(self.store.create_booking(**booking())["ok"])

    def test_deleted_required_column_blocks_read_and_write(self):
        self.a.refresh()
        self.worksheet.values[0].remove("Статус")
        self.a.refresh(force=True)
        self.assertFalse(self.rule()["validation"]["read"])
        with self.assertRaises(MappingError):
            self.a.table("bookings")
        self.assertFalse(self.worksheet.writes)

    def test_deleted_optional_column_keeps_safe_read(self):
        self.a.refresh()
        self.worksheet.values[0].remove("Комментарий")
        self.a.refresh(force=True)
        self.assertTrue(self.rule()["validation"]["read"])
        self.assertFalse(self.rule()["validation"]["write"])
        self.assertEqual(self.a.table("bookings")[1], [])

    def test_day_based_sheets_and_global_ids(self):
        self.spreadsheet.worksheet("Слоты").values = [["Время", "Постов"], ["10:00", "2"]]
        self.spreadsheet.worksheet("Слоты").title = "Slots 2099-01-01"
        self.worksheet.title = "Bookings 2099-01-01"
        self.worksheet.values[0].remove("Дата")
        other = Worksheet(5, "Bookings 2099-01-02", [self.worksheet.values[0]])
        self.spreadsheet.sheets.append(other)
        self.assertTrue(self.store.create_booking(**booking())["ok"])
        self.assertEqual(len(self.worksheet.writes), 1)
        self.assertEqual(other.writes, [])
        self.assertEqual(self.store.my_bookings(111)["bookings"][0]["date"], "2099-01-01")

    def test_date_cell_manual_mapping(self):
        self.a.refresh()
        rule = self.rule("slots")
        worksheet = self.spreadsheet.worksheet("Слоты")
        worksheet.values = [["01.01.2099"], ["Время", "Постов"], ["10:00", "2"]]
        rule.update(header_row=2, first_data_row=3, columns={"Время": 1, "Постов": 2}, date_source={"cell": [1, 1]})
        self.save()
        self.a.refresh(validate=True)
        self.assertEqual(self.a.table("slots")[1][0]["Дата"], "01.01.2099")

    def test_grid_read_only(self):
        worksheet = self.spreadsheet.worksheet("Слоты")
        worksheet.title = "2099-01-01"
        worksheet.values = [["Время", "Пост 1", "Бокс 2"], ["10:00", "free", "занято"]]
        self.assertEqual(self.store.free_slots("2099-01-01")["days"][0]["free_times"], ["10:00"])
        with self.assertRaisesRegex(MappingError, "только для чтения"):
            self.store.create_booking(**booking())
        self.assertFalse(self.worksheet.writes)

    def test_ambiguous_headers_are_not_guessed(self):
        self.worksheet.values[0].append("Booking ID")
        doc = self.a.refresh()
        self.assertTrue(any("Несколько колонок" in issue for issue in doc["issues"]))
        with self.assertRaises(MappingError):
            self.a.table("bookings")

    def test_multiple_ordinary_booking_sheets_are_ambiguous(self):
        self.spreadsheet.sheets.append(Worksheet(5, "Another", [BOOKING_HEADERS]))
        with self.assertRaisesRegex(MappingError, "Несколько областей"):
            self.a.table("bookings")

    def test_saved_configuration_reused_without_discovery(self):
        self.a.refresh()
        before = self.spreadsheet.metadata_reads
        restored = AdaptiveSheets(self.store.config, lambda: self.spreadsheet)
        restored.table("bookings")
        self.assertEqual(self.spreadsheet.metadata_reads, before)

    def test_data_freshness_independent_from_schema_ttl(self):
        self.store.free_slots("2099-01-01")
        before = self.spreadsheet.metadata_reads
        self.spreadsheet.worksheet("Слоты").values[1][2] = "0"
        self.store._invalidate("slots")
        self.assertEqual(self.store.free_slots("2099-01-01")["days"][0]["free_times"], [])
        self.assertEqual(self.spreadsheet.metadata_reads, before)

    def test_schema_interval_and_manual_force(self):
        self.a.refresh()
        before = self.spreadsheet.metadata_reads
        self.a.refresh()
        self.assertEqual(self.spreadsheet.metadata_reads, before)
        self.a.document["checked_at"] -= 25 * 3600
        self.save()
        self.a.refresh()
        self.assertEqual(self.spreadsheet.metadata_reads, before + 1)
        self.a.refresh(force=True)
        self.assertEqual(self.spreadsheet.metadata_reads, before + 2)

    def test_header_drift_detected_on_read_before_interval(self):
        self.a.refresh()
        self.worksheet.values[0].insert(0, "Extra")
        self.a.table("bookings")
        self.assertEqual(self.rule()["columns"]["ID"], 2)

    def test_formula_and_formatting_preserved_outside_mapped_fields(self):
        self.worksheet.values[0].append("Computed")
        self.worksheet.values.append([""] * 16 + ["=1+1"])
        formatting = deepcopy(self.worksheet.formatting)
        self.assertTrue(self.store.create_booking(**booking())["ok"])
        self.assertEqual(self.worksheet.values[1][16], "=1+1")
        self.assertEqual(self.worksheet.formatting, formatting)
        self.assertEqual(self.worksheet.writes[0][0]["range"], "A3")

    def test_formula_in_booking_field_blocks_writes(self):
        self.assertTrue(self.store.create_booking(**booking())["ok"])
        self.worksheet.values[1][11] = "=700-70"
        with self.assertRaises(MappingError):
            self.store.cancel_booking(1, 111)
        self.assertEqual(self.worksheet.values[1][11], "=700-70")

    def test_merged_cells_block_region(self):
        self.worksheet.merges = [{"startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": 2}]
        with self.assertRaises(MappingError):
            self.a.table("bookings")

    def test_unknown_write_result_is_not_retried(self):
        self.worksheet.fail_write = True
        with self.assertRaisesRegex(MappingError, "неизвестен"):
            self.store.create_booking(**booking())
        self.worksheet.fail_write = False
        with self.assertRaisesRegex(MappingError, "повтор заблокирован"):
            self.store.create_booking(**booking())
        self.assertFalse(self.worksheet.writes)

    def test_cancel_rechecks_identity(self):
        self.store.create_booking(**booking())
        self.worksheet.before_get = lambda: self.worksheet.values[1].__setitem__(13, "999")
        with self.assertRaisesRegex(MappingError, "перемещена"):
            self.store.cancel_booking(1, 111)
        self.assertEqual(self.worksheet.values[1][-1], "Новая")

    def test_bad_booking_data_cannot_free_capacity_silently(self):
        self.store.create_booking(**booking())
        self.worksheet.values[1][2] = "not a date"
        with self.assertRaises(MappingError):
            self.store.my_bookings(111)

    def test_mappings_store_no_customer_rows(self):
        self.store.create_booking(**booking())
        self.a.refresh(force=True)
        text = self.a.path.read_text(encoding="utf-8")
        self.assertNotIn("Synthetic", text)
        self.assertNotIn("00000000000", text)
        self.assertNotIn("Fixture car", text)

    def test_legacy_bot_mode_keeps_existing_behavior(self):
        store = Store(replace(self.store.config, sheets_adaptive_enabled=False))
        store._spreadsheet = self.spreadsheet
        self.assertTrue(store.create_booking(**booking())["ok"])
        self.assertTrue(store.cancel_booking(1, 111)["ok"])
        self.assertFalse(self.a.path.exists())

    def test_ai_disabled_and_no_startup_calls(self):
        client = Mock()
        self.a.refresh()
        with self.assertRaisesRegex(MappingError, "отключены"):
            suggest_mappings(self.a, client)
        client.chat.completions.create.assert_not_called()
        self.store.create_booking(**booking())
        client.chat.completions.create.assert_not_called()

    def test_ai_suggestions_cached_and_incremental_without_customer_data(self):
        self.a.config = replace(self.store.config, sheets_ai_mapping_enabled=True)
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = '{"suggestions": []}'
        self.store.create_booking(**booking())
        suggest_mappings(self.a, client)
        client.chat.completions.create.assert_not_called()
        self.worksheet.values[0][3] = "Arrival window"
        self.a.refresh(force=True)
        suggest_mappings(self.a, client)
        suggest_mappings(self.a, client)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.worksheet.values[0][3] = "Arrival hour"
        self.a.refresh(force=True)
        suggest_mappings(self.a, client)
        payload = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertIn("Arrival hour", payload)
        self.assertNotIn('"header": "Телефон"', payload)
        self.assertNotIn("Synthetic", payload)
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_transform_custom_date_and_serial_time(self):
        self.a.refresh()
        rule = self.rule("slots")
        rule["transforms"] = {"Дата": "date:%m/%d/%Y", "Время": "serial_time"}
        self.spreadsheet.worksheet("Слоты").values[1][:2] = ["01/02/2099", "0.5"]
        self.save()
        self.a.refresh(validate=True)
        row = self.a.table("slots")[1][0]
        self.assertEqual((row["Дата"], row["Время"]), ("02.01.2099", "12:00"))

    def test_long_service_price_table_reuses_catalog(self):
        self.spreadsheet.worksheet("Услуги").values = [["Service", "Description", "Car class", "Price", "Available"],
                                              ["Мойка", "Кузов", "Легковой", "700", "yes"],
                                              ["Мойка", "Кузов", "Кроссовер", "800", "yes"],
                                              ["Воск", "Защита", "Легковой", "200", "no"]]
        self.assertEqual(self.store.calculate_price("Кроссовер", None, ["Мойка"])["total"], 800)
        self.assertEqual(self.store.calculate_price("Легковой", "Базовый", [])["total"], 630)

    def test_manual_edits_need_validation(self):
        self.a.refresh()
        rule = self.rule()
        rule["columns"]["Имя"] = 100
        self.save()
        with self.assertRaises(MappingError):
            self.a.table("bookings")
        self.a.refresh(validate=True)
        self.assertFalse(self.rule()["validation"]["write"])

    def test_schema_discovery_uses_batch_reads(self):
        self.a.refresh()
        self.assertEqual(self.spreadsheet.metadata_reads, 1)
        self.assertEqual(self.spreadsheet.batch_reads, 2)

    def test_rebooking_after_cancel(self):
        first = self.store.create_booking(**booking())
        self.store.cancel_booking(first["booking_id"], 111)
        self.assertTrue(self.store.create_booking(**booking())["ok"])

    def test_deleted_sheet_blocks_affected_role(self):
        self.a.refresh()
        self.spreadsheet.sheets.remove(self.worksheet)
        self.a.refresh(force=True)
        with self.assertRaises(MappingError):
            self.a.table("bookings")

    def test_duplicate_service_does_not_override_price(self):
        self.spreadsheet.worksheet("Услуги").values.append(["Мойка", "Кузов", "1", "1", "да"])
        with self.assertRaises(MappingError):
            self.store.get_catalog()


if __name__ == "__main__":
    unittest.main()
