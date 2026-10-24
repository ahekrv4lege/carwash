"""Настоящие модели gspread с офлайн-подменой только HTTP-клиента."""
from copy import deepcopy
import re
import tempfile
import unittest

import gspread

from sheets import Store
from tests.fakes import fixture, booking


class OfflineHTTP(gspread.http_client.HTTPClient):
    def __init__(self, source):
        self.source = source
        self.updates = []

    def fetch_sheet_metadata(self, id, params=None):
        result = self.source.fetch_sheet_metadata(params)
        result["properties"] = {"title": "Synthetic", "locale": "ru_RU", "timeZone": "Europe/Moscow"}
        return result

    def values_batch_get(self, id, ranges, params=None):
        return self.source.values_batch_get(ranges, params)

    def values_get(self, id, range_name, params=None):
        match = re.fullmatch(r"'((?:[^']|'')+)'(?:!(.*))?", range_name)
        title, cells = match.groups()
        ws = self.source.worksheet(title.replace("''", "'"))
        render = (params or {}).get("valueRenderOption")
        values = ws.get(cells, render) if cells else ws.get_all_values(render)
        return {"range": range_name + "!A1:AD100" if not cells else range_name,
                "majorDimension": "ROWS", "values": values}

    def values_batch_update(self, id, body):
        self.updates.append(deepcopy(body))
        assert body["valueInputOption"] == "RAW"
        for item in body["data"]:
            title, cells = re.fullmatch(r"'((?:[^']|'')+)'!(.*)", item["range"]).groups()
            self.source.worksheet(title.replace("''", "'")).batch_update(
                [{"range": cells, "values": item["values"]}], value_input_option="RAW")
        return {"totalUpdatedCells": len(body["data"])}


class GspreadContractTests(unittest.TestCase):
    def test_real_sdk_discovery_create_and_cancel(self):
        with tempfile.TemporaryDirectory() as temp:
            original, fake = fixture(temp)
            http = OfflineHTTP(fake)
            ss = gspread.Spreadsheet(http, {"id": "synthetic"})
            store = Store(original.cfg)
            store._ss = ss
            self.assertTrue(store.create_booking(**booking())["ok"])
            self.assertEqual(len(store.my_bookings(111)["bookings"]), 1)
            self.assertTrue(store.cancel_booking(1, 111)["ok"])
            self.assertEqual(http.updates[-1]["data"][0]["range"], "'Записи'!P2")
            self.assertTrue(all(b["valueInputOption"] == "RAW" for b in http.updates))

    def test_cached_title_is_refreshed_after_real_sdk_range_error(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temp:
            original, fake = fixture(temp)
            http = OfflineHTTP(fake)
            ss = gspread.Spreadsheet(http, {"id": "synthetic"})
            store = Store(original.cfg)
            store._ss = ss
            store._adaptive.table("bookings")
            fake.worksheet("Записи").title = "Renamed bookings"
            values_get = http.values_get
            class InvalidOldRange(RuntimeError):
                response = SimpleNamespace(status_code=400)
            def renamed(id, range_name, params=None):
                if "'Записи'" in range_name:
                    raise InvalidOldRange("old cached title")
                return values_get(id, range_name, params)
            from unittest.mock import patch
            with patch.object(http, "values_get", side_effect=renamed):
                self.assertEqual(store._adaptive.table("bookings")[1], [])
