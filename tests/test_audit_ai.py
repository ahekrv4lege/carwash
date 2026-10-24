"""Детерминированные ответы и отказы AI; провайдер не вызывается."""
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from types import SimpleNamespace
from openai import RateLimitError, APIConnectionError, APITimeoutError

from adaptive import AdaptiveSheets, MappingError, atomic_json
from mapping_ai import suggest_mappings
from tests.fakes import fixture


class AuditAITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.ss = fixture(self.temp.name, sheets_ai_mapping_enabled=True)
        self.a = self.store._adaptive
        self.a.refresh()
        self.ss.worksheet("Записи").values[0][3] = "Arrival window"
        self.a.refresh(force=True)
        self.client = MagicMock()
        self.key = "4:bookings:1"
        self.path = self.a.path.with_name("mapping-suggestions.json")

    def response(self, value):
        self.client.chat.completions.create.return_value.choices[0].message.content = json.dumps(value)

    def valid(self):
        return {"suggestions": [{"region": self.key, "column": 4, "field": "Время", "reason": "Start time"}]}

    def test_valid_suggestion_remains_separate_from_mapping_and_sheets(self):
        self.response(self.valid())
        before = self.a.path.read_bytes()
        saved = suggest_mappings(self.a, self.client)
        self.assertEqual(saved["suggestions"][0]["field"], "Время")
        self.assertEqual(self.a.path.read_bytes(), before)
        self.assertFalse(any(ws.writes for ws in self.ss.sheets))
        request = self.client.chat.completions.create.call_args.kwargs
        self.assertNotIn("tools", request)
        self.assertEqual(request["model"], self.store.cfg.openrouter_model)

    def test_invalid_structured_results_are_rejected(self):
        values = [None, [], {}, {"suggestions": {}}, {"suggestions": [None]},
                  {"suggestions": [{"region": self.key, "column": 4}]}]
        for value in values:
            with self.subTest(value=value):
                self.response(value)
                with self.assertRaises(MappingError):
                    suggest_mappings(self.a, self.client)
                self.assertFalse(self.path.exists())

    def test_invalid_json_and_empty_choices_are_controlled_errors(self):
        self.client.chat.completions.create.return_value.choices[0].message.content = "{broken"
        with self.assertRaises(MappingError):
            suggest_mappings(self.a, self.client)
        self.client.chat.completions.create.return_value.choices = []
        with self.assertRaises(MappingError):
            suggest_mappings(self.a, self.client)

    def test_incorrect_hallucinated_and_missing_columns_are_rejected(self):
        for field, column, region in [("Invented", 4, self.key), ("Время", 99, self.key),
                                      ("ID", 4, self.key), ("Время", True, self.key),
                                      ("Время", 4.0, self.key), ("Время", 4, "invented")]:
            with self.subTest(field=field, column=column, region=region):
                self.path.unlink(missing_ok=True)
                self.response({"suggestions": [{"region": region, "column": column, "field": field}]})
                with self.assertRaises(MappingError):
                    suggest_mappings(self.a, self.client)
                self.assertFalse(self.path.exists())

    def test_duplicate_assignments_are_rejected(self):
        result = self.valid()
        result["suggestions"] *= 2
        self.response(result)
        with self.assertRaises(MappingError):
            suggest_mappings(self.a, self.client)

    def test_rate_limit_unavailable_and_timeout_do_not_leak_or_change_cache(self):
        request = SimpleNamespace(method="POST", url="https://example.invalid/api")
        errors = [RateLimitError("PRIVATE_SENTINEL", response=SimpleNamespace(status_code=429, request=request, headers={}), body=None),
                  APIConnectionError(request=request, message="PRIVATE_SENTINEL"), APITimeoutError(request=request)]
        before = self.a.path.read_bytes()
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.client.chat.completions.create.side_effect = error
                with self.assertRaises(MappingError) as raised:
                    suggest_mappings(self.a, self.client)
                self.assertNotIn("PRIVATE_SENTINEL", str(raised.exception))
                self.assertEqual(self.a.path.read_bytes(), before)
                self.assertFalse(self.path.exists())
        self.assertEqual(self.client.chat.completions.create.call_count, 3)

    def test_error_then_success_can_be_retried_explicitly(self):
        self.client.chat.completions.create.side_effect = TimeoutError("synthetic")
        with self.assertRaises(MappingError):
            suggest_mappings(self.a, self.client)
        self.client.chat.completions.create.side_effect = None
        self.response(self.valid())
        self.assertEqual(len(suggest_mappings(self.a, self.client)["suggestions"]), 1)

    def test_cached_suggestions_are_bound_to_spreadsheet(self):
        atomic_json(self.path, {"spreadsheet_id": "other-spreadsheet", "reviewed": {self.key: ["Arrival window"]},
                                "suggestions": []})
        self.response(self.valid())
        suggest_mappings(self.a, self.client)
        self.client.chat.completions.create.assert_called_once()

    def test_corrupt_suggestion_cache_is_controlled_and_does_not_call_ai(self):
        self.path.write_text("null")
        with self.assertRaises(MappingError):
            suggest_mappings(self.a, self.client)
        self.client.chat.completions.create.assert_not_called()

    def test_restart_uses_validated_mapping_without_any_ai_requests(self):
        # Независимые экземпляры, включая включённый флаг AI.
        self.ss.worksheet("Записи").values[0][3] = "Время"
        self.a.refresh(force=True)
        rule = next(r for r in self.a.document["regions"] if r["role"] == "bookings")
        rule["columns"]["Время"] = 4
        atomic_json(self.a.path, self.a.document)
        self.a.refresh(validate=True)
        with patch("openai.OpenAI", side_effect=AssertionError("No AI during startup")):
            for _ in range(3):
                restored = AdaptiveSheets(self.store.cfg, lambda: self.ss)
                self.assertEqual(restored.table("bookings")[1], [])

    def test_owned_ai_client_is_closed_after_provider_failure(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = TimeoutError("synthetic")
        with patch("openai.OpenAI", return_value=client), self.assertRaises(MappingError):
            suggest_mappings(self.a)
        client.close.assert_called_once()
