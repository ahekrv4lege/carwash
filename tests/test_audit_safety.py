"""Регрессии независимого аудита; только синтетические таблицы."""
import tempfile
import unittest
from unittest.mock import patch

from adaptive import AdaptiveSheets, MappingError, atomic_json
from tests.fakes import fixture, booking, Worksheet


class AuditSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.ss = fixture(self.temp.name)
        self.a = self.store._adaptive
        self.ws = self.ss.worksheet("Записи")

    def rule(self, role):
        return next(r for r in self.a.refresh()["regions"] if r["role"] == role)

    def save(self):
        atomic_json(self.a.path, self.a.document)

    def test_deleted_availability_cannot_enable_disabled_service(self):
        self.a.refresh()
        ws = self.ss.worksheet("Услуги")
        for row in ws.values:
            row.pop(4)
        with self.assertRaises(MappingError):
            self.store.get_catalog(fresh=True)
        # Последующее безопасное изменение не снимает блокировку.
        for row in ws.values:
            row.insert(0, "Extra" if row is ws.values[0] else "")
        with self.assertRaises(MappingError):
            self.store.get_catalog(fresh=True)

    def test_deleted_discount_does_not_silently_change_price(self):
        self.a.refresh()
        ws = self.ss.worksheet("Тарифы")
        for row in ws.values:
            row.pop(2)
        with self.assertRaises(MappingError):
            self.store.get_catalog(fresh=True)

    def test_preflight_detects_capacity_conflict(self):
        original = self.a._preflight
        def conflict():
            self.ss.worksheet("Слоты").values[1][2] = "0"
            return original()
        with patch.object(self.a, "_preflight", side_effect=conflict), self.assertRaises(MappingError):
            self.store.create_booking(**booking())
        self.assertFalse(self.ws.writes)

    def test_preflight_detects_price_conflict(self):
        original = self.a._preflight
        def conflict():
            self.ss.worksheet("Услуги").values[1][2] = "999"
            return original()
        with patch.object(self.a, "_preflight", side_effect=conflict), self.assertRaises(MappingError):
            self.store.create_booking(**booking())
        self.assertFalse(self.ws.writes)

    def test_preflight_detects_booking_conflict(self):
        self.store.create_booking(**booking())
        original = self.a._preflight
        def conflict():
            self.ws.values[1][4] = "Edited externally"
            return original()
        with patch.object(self.a, "_preflight", side_effect=conflict), self.assertRaises(MappingError):
            self.store.create_booking(**booking(tg_id=222, car="Another"))
        self.assertEqual(len(self.ws.writes), 1)

    def test_grid_punctuation_and_closed_markers_are_occupied(self):
        ws = self.ss.worksheet("Слоты")
        ws.title = "2099-01-01"
        ws.values = [["Время", "Пост 1", "Пост 2", "Пост 3"], ["10:00", "-", "x", "!"]]
        self.assertEqual(self.store.free_slots("2099-01-01")["days"][0]["free_times"], [])

    def test_ambiguous_time_suffix_is_not_treated_as_24h_time(self):
        self.ss.worksheet("Слоты").values[1][1] = "10:00 PM"
        with self.assertRaises(MappingError):
            self.a.table("slots")

    def test_explicit_date_format_does_not_fallback_to_other_locale(self):
        self.a.refresh()
        self.rule("slots")["transforms"]["Дата"] = "date:%m/%d/%Y"
        self.save()
        self.a.refresh(validate=True)
        self.ss.worksheet("Слоты").values[1][0] = "31/12/2099"
        with self.assertRaises(MappingError):
            self.a.table("slots")

    def test_numeric_metadata_is_not_a_vehicle_class(self):
        ws = self.ss.worksheet("Услуги")
        for row, value in zip(ws.values, ["Duration", "30", "15"]):
            row.insert(2, value)
        self.assertNotIn("Duration", self.store.get_catalog().size_classes)

    def test_unknown_availability_cannot_default_to_enabled(self):
        self.ss.worksheet("Услуги").values[2][4] = "retired"
        with self.assertRaises(MappingError):
            self.store.get_catalog()

    def test_negative_and_malformed_prices_rejected(self):
        for value in ("-10", "700 text", "1e3", "NaN", "1,000", "1.000"):
            with self.subTest(value=value):
                self.ss.worksheet("Услуги").values[1][2] = value
                # Правила уже существуют: проверяется изменение данных, не распознавание шапки.
                if self.a.document is None:
                    self.ss.worksheet("Услуги").values[1][2] = "700"
                    self.a.refresh()
                    self.ss.worksheet("Услуги").values[1][2] = value
                with self.assertRaises(MappingError):
                    self.store.get_catalog(fresh=True)

    def test_duplicate_class_names_ignoring_case_rejected(self):
        ws = self.ss.worksheet("Услуги")
        ws.values[0][3] = "легковой"
        with self.assertRaises(MappingError):
            self.store.get_catalog()

    def test_insertions_beginning_middle_end_keep_existing_records(self):
        self.store.create_booking(**booking())
        for col in (0, 8, 17):
            for row in self.ws.values:
                row.insert(col, "Unrelated" + str(col) if row is self.ws.values[0] else "Keep")
            self.a.refresh(force=True)
        self.assertTrue(self.store.cancel_booking(1, 111)["ok"])
        for col in (0, 8, 17):
            self.assertEqual(self.ws.values[1][col], "Keep")

    def test_disabled_writes_block_create_and_cancel(self):
        self.store.create_booking(**booking())
        self.rule("bookings")["permissions"]["write"] = False
        self.save()
        self.a.refresh(validate=True)
        self.assertFalse(self.a.document["writes_enabled"])
        for operation in (lambda: self.store.cancel_booking(1, 111),
                          lambda: self.store.create_booking(**booking(tg_id=222, car="Other"))):
            with self.assertRaises(MappingError):
                operation()
        self.assertEqual(len(self.store.my_bookings(111)["bookings"]), 1)

    def test_transient_read_failure_does_not_serve_stale_bookings(self):
        self.store.create_booking(**booking())
        with patch.object(self.ws, "get_all_values", side_effect=TimeoutError("synthetic")):
            with self.assertRaises(TimeoutError):
                self.store.my_bookings(111)
        self.assertEqual(len(self.store.my_bookings(111)["bookings"]), 1)

    def test_unknown_write_result_survives_restart(self):
        self.ws.fail_write = True
        with self.assertRaises(MappingError):
            self.store.create_booking(**booking())
        self.ws.fail_write = False
        restarted, _ = fixture(self.temp.name)
        restarted._ss = self.ss
        with self.assertRaises(MappingError):
            restarted.create_booking(**booking())
        self.assertFalse(self.ws.writes)

    def test_failed_refresh_keeps_last_good_mapping_file(self):
        self.a.refresh()
        before = self.a.path.read_bytes()
        with patch.object(self.ss, "values_batch_get", side_effect=TimeoutError("synthetic")):
            with self.assertRaises(TimeoutError):
                self.a.refresh(force=True)
        self.assertEqual(self.a.path.read_bytes(), before)

    def test_mapping_summary_rejects_unvalidated_edits(self):
        self.a.refresh()
        self.rule("bookings")["columns"]["Имя"] = 20
        self.save()
        restarted = AdaptiveSheets(self.store.cfg, lambda: self.ss)
        doc = restarted.refresh()
        self.assertFalse(doc["writes_enabled"])
        self.assertEqual(doc["validation_status"], "needs_review")

    def test_date_cell_reference_requires_review_after_column_shift(self):
        self.a.refresh()
        rule = self.rule("slots")
        ws = self.ss.worksheet("Слоты")
        ws.values = [["02.01.2099", "01.01.2099"], ["Время", "Постов"], ["10:00", "2"]]
        rule.update(header_row=2, first_data_row=3, columns={"Время": 1, "Постов": 2}, date_source={"cell": [1, 2]})
        self.save()
        self.a.refresh(validate=True)
        # После сдвига старый B1 тоже содержит корректную, но другую дату.
        for row in ws.values:
            row.insert(0, "extra" if row is ws.values[1] else "")
        with self.assertRaises(MappingError):
            self.a.table("slots")

    def test_formula_header_never_used_as_writable_mapping(self):
        self.a.refresh()
        self.ws.values[0][15] = "=1+1"
        rule = self.rule("bookings")
        self.save()
        self.a.refresh(validate=True)
        self.assertFalse(self.rule("bookings")["validation"]["write"])

    def test_mid_scan_header_changes_abort_before_write(self):
        original = self.ws.get_all_values
        def unstable(value_render_option=None):
            result = original(value_render_option)
            if value_render_option == "FORMULA":
                # Разные версии шапки в двух режимах чтения.
                result[0][0] = "Other header"
            return result
        with patch.object(self.ws, "get_all_values", side_effect=unstable), self.assertRaises(MappingError):
            self.store.create_booking(**booking())
        self.assertFalse(self.ws.writes)

    def test_literal_equals_text_is_preserved_and_not_mistaken_for_formula(self):
        self.ws.literal_cells.add((2, 13))
        self.assertTrue(self.store.create_booking(**booking(comment="=literal customer text"))["ok"])
        self.assertTrue(self.store.cancel_booking(1, 111)["ok"])
        self.assertEqual(self.ws.values[1][12], "=literal customer text")

    def test_late_status_edit_is_not_overwritten_by_cancellation(self):
        self.store.create_booking(**booking())
        self.ws.before_get = lambda: self.ws.values[1].__setitem__(15, "Подтверждена")
        with self.assertRaises(MappingError):
            self.store.cancel_booking(1, 111)
        self.assertEqual(self.ws.values[1][15], "Подтверждена")

    def test_late_booking_edit_is_detected_on_final_read(self):
        self.store.create_booking(**booking())
        original = self.ws.get_all_values
        calls = 0
        def conflict(value_render_option=None, **kwargs):
            nonlocal calls
            if value_render_option == "FORMULA":
                calls += 1
                if calls == 2:
                    self.ws.values[1][0] = "999"
            return original(value_render_option=value_render_option, **kwargs)
        with patch.object(self.ws, "get_all_values", side_effect=conflict), self.assertRaises(MappingError):
            self.store.create_booking(**booking(tg_id=222, car="Another"))
        self.assertEqual(len(self.ws.writes), 1)

    def test_duplicate_day_regions_do_not_double_slot_capacity(self):
        ws = self.ss.worksheet("Слоты")
        ws.title = "Slots 2099-01-01"
        ws.values = [["Время", "Постов"], ["10:00", "2"]]
        self.ss.sheets.append(Worksheet(5, "Copy 2099-01-01", ws.values))
        with self.assertRaises(MappingError):
            self.store.free_slots("2099-01-01")

    def test_fingerprint_ignores_ordinary_booking_values(self):
        before = self.a.refresh()["structural_fingerprint"]
        self.store.create_booking(**booking())
        self.assertEqual(self.a.refresh(force=True)["structural_fingerprint"], before)

    def test_future_timestamp_does_not_postpone_schema_check(self):
        self.a.refresh()
        calls = self.ss.metadata_reads
        self.a.document["checked_at"] += 48 * 3600
        self.save()
        self.a.refresh()
        self.assertGreater(self.ss.metadata_reads, calls)

    def test_mapping_and_journal_files_are_private_on_linux(self):
        import os
        if os.name != "posix":
            self.skipTest("POSIX mode bits")
        self.store.create_booking(**booking())
        for path in (self.a.path, self.a.path.with_name("mapping-operations.json")):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_adaptive_http_client_does_not_blindly_retry_writes(self):
        import gspread
        self.store._ss = None
        with patch("gspread.service_account") as factory:
            self.store._spreadsheet()
        self.assertNotIn("http_client", factory.call_args.kwargs)
        legacy, _ = fixture(self.temp.name, sheets_adaptive_enabled=False)
        legacy._ss = None
        with patch("gspread.service_account") as factory:
            legacy._spreadsheet()
        self.assertEqual(factory.call_args.kwargs["http_client"], gspread.BackOffHTTPClient)

    def test_unverifiable_formula_cell_type_blocks_write(self):
        self.ws.literal_cells.add((2, 13))
        self.store.create_booking(**booking(comment="=literal"))
        original = self.ss.fetch_sheet_metadata
        def missing(params=None):
            return {"sheets": []} if (params or {}).get("includeGridData") else original(params)
        with patch.object(self.ss, "fetch_sheet_metadata", side_effect=missing), self.assertRaises(MappingError):
            self.store.cancel_booking(1, 111)
        self.assertEqual(self.ws.values[1][-1], "Новая")

    def test_invalid_discount_or_duplicate_composition_is_rejected(self):
        ws = self.ss.worksheet("Тарифы")
        for discount, items in [("150", "Мойка"), ("10", "Мойка, мойка"), ("bad", "Мойка")]:
            with self.subTest(discount=discount, items=items):
                ws.values[1][1:3] = [items, discount]
                with self.assertRaises(MappingError):
                    self.store.get_catalog(fresh=True)

    def test_unclassified_numeric_column_requires_manual_confirmation(self):
        ws = self.ss.worksheet("Услуги")
        for row, value in zip(ws.values, ["Unclassified number", "30", "15"]):
            row.insert(2, value)
        with self.assertRaises(MappingError):
            self.store.get_catalog()
        rule = self.rule("services")
        del rule["columns"]["Unclassified number"]
        rule["price_fields"].remove("Unclassified number")
        self.save()
        self.a.refresh(validate=True)
        self.assertEqual(self.store.get_catalog(fresh=True).size_classes, ["Легковой", "Кроссовер"])

    def test_google_quota_failure_is_not_retried_or_written(self):
        from types import SimpleNamespace
        from gspread.exceptions import APIError
        self.a.refresh()
        error = APIError(SimpleNamespace(status_code=429, json=lambda: {"error": {"code": 429, "message": "synthetic quota"}}))
        before = self.a.path.read_bytes()
        with patch.object(self.ss, "values_batch_get", side_effect=error) as call, self.assertRaises(APIError):
            self.store.create_booking(**booking())
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.a.path.read_bytes(), before)
        self.assertFalse(self.ws.writes)

    def test_removed_mapping_role_cannot_report_writes_enabled(self):
        self.a.refresh()
        self.a.document["regions"] = [r for r in self.a.document["regions"] if r["role"] != "bookings"]
        self.save()
        restored = AdaptiveSheets(self.store.cfg, lambda: self.ss)
        self.assertFalse(restored.refresh()["writes_enabled"])
        self.assertIn("bookings", restored.refresh()["missing_roles"])
