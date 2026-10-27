"""Load a fixture into the repository's offline gspread fakes; no network access."""
from copy import deepcopy
from pathlib import Path
import json
import re


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _coord(cell):
    letters, row = re.fullmatch(r"([A-Z]+)([1-9]\d*)", cell).groups()
    col = 0
    for letter in letters:
        col = col * 26 + ord(letter) - ord("A") + 1
    return int(row), col


def make_spreadsheet(case):
    # Run with PYTHONPATH=checks:. so this imports the existing offline fake.
    from tests.fakes import Spreadsheet, Worksheet

    sheets = []
    for index, spec in enumerate(case["sheets"], 1):
        ws = Worksheet(index, spec["title"], spec["rows"])
        for address, formula in spec.get("formulas", {}).items():
            row, col = _coord(address)
            while len(ws.values) < row:
                ws.values.append([])
            while len(ws.values[row - 1]) < col:
                ws.values[row - 1].append("")
            ws.values[row - 1][col - 1] = formula
        for address in spec.get("merges", []):
            first, last = address.split(":")
            r1, c1 = _coord(first)
            r2, c2 = _coord(last)
            ws.merges.append({"startRowIndex": r1 - 1, "endRowIndex": r2,
                              "startColumnIndex": c1 - 1, "endColumnIndex": c2})
            # Match XLSX semantics: only the upper-left value survives merging.
            for row in range(r1, r2 + 1):
                for col in range(c1, c2 + 1):
                    if (row, col) != (r1, c1) and row <= len(ws.values) and col <= len(ws.values[row - 1]):
                        ws.values[row - 1][col - 1] = ""
        sheets.append(ws)
    return Spreadsheet(sheets)


def mutate(spreadsheet, mutation):
    ws = spreadsheet.worksheet(mutation["sheet"])
    operation = mutation["operation"]
    if operation == "append_row":
        ws.values.append(deepcopy(mutation["row"]))
    elif operation == "insert_row":
        ws.values.insert(mutation["before"] - 1, deepcopy(mutation["row"]))
    elif operation == "set_cell":
        row, col = _coord(mutation["cell"])
        assert ws.values[row - 1][col - 1] == mutation["from"]
        ws.values[row - 1][col - 1] = mutation["to"]
    else:
        raise ValueError(f"Unknown mutation: {operation}")
