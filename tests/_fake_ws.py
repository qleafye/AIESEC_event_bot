"""Фейковый лист gspread для тестов зеркала «как выгрузка Яндекса» (вкладка `UR REGS`).

Повторяет ровно те методы Worksheet, которыми пользуется `services/delegations/delegations_mirror.py`:
`row_values`, `col_values`, `batch_update` (любой A1-диапазон `X{r}:Y{r}`), `batch_format`
(цвет строки из диапазона), `add_rows`/`add_cols`, `row_count`/`col_count`, `title`,
`spreadsheet.fetch_sheet_metadata` (JSON формы Google с `effectiveFormat.backgroundColor`
по колонке A). `append_rows` и `insert_cols` бросают — для `UR REGS` они запрещены
(пустые строки 9–15 внутри данных и формулы Насти в P:Q).

`probe_sheet()` строит лист в раскладке 36-PROBE.md: шапка A–L, две строки данных, три
пустых, ещё две строки данных, P/Q со счётчиком вузов; строка 3 серая, строка 8 зелёная.
"""
from __future__ import annotations

import re

GREY = {"red": 0.851, "green": 0.851, "blue": 0.851}
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
GREEN = {"red": 0.714, "green": 0.843, "blue": 0.659}

PROBE_HEADER = [
    "ID", "Время начала заполнения формы", "Время создания", "ФИО", "Возраст", "Ваша почта",
    "Название университета", "Направление обученияе",
    "Бакалавриат или магистратура, номер курса", "Ник в телеграмме (через @)",
    "Ник в ВКонтакте (через @)", "Согласен(-на) на обработку персональных данных",
]

_RANGE = re.compile(r"^\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?$")


def col_to_idx(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def idx_to_col(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def parse_range(rng: str) -> tuple[int, int, int, int]:
    """'D3:M3' -> (row1, col1, row2, col2); одиночная ячейка 'M1' -> (1, 13, 1, 13)."""
    if "!" in rng:
        rng = rng.split("!", 1)[1]
    m = _RANGE.match(rng.strip())
    if not m:
        raise ValueError(f"неразобранный диапазон: {rng!r}")
    c1, r1, c2, r2 = m.groups()
    return int(r1), col_to_idx(c1), int(r2 or r1), col_to_idx(c2 or c1)


class FakeSpreadsheet:
    def __init__(self, ws: "FakeWS"):
        self._ws = ws

    def fetch_sheet_metadata(self, params=None):
        params = params or {}
        self._ws.calls.append(("fetch_sheet_metadata", params))
        ranges = params.get("ranges") or []
        r1, c1, r2, c2 = parse_range(ranges[0]) if ranges else (1, 1, max(len(self._ws.rows), 1), 1)
        row_data = []
        for r in range(r1, r2 + 1):
            colour = self._ws.formats.get(r, WHITE)
            row_data.append({"values": [{"effectiveFormat": {"backgroundColor": dict(colour)}}]})
        return {"sheets": [{"data": [{"rowData": row_data}]}]}


class FakeWS:
    def __init__(self, rows=None, formats=None, *, title="UR REGS", row_count=None, col_count=26):
        self.rows: list[list[str]] = [list(r) for r in (rows or [])]
        self.formats: dict[int, dict] = dict(formats or {})
        self.calls: list[tuple] = []
        self.title = title
        self.row_count = row_count if row_count is not None else max(len(self.rows), 20)
        self.col_count = col_count
        self.spreadsheet = FakeSpreadsheet(self)

    # ---- чтение ----
    def row_values(self, n):
        return list(self.rows[n - 1]) if len(self.rows) >= n else []

    def col_values(self, n):
        vals = [r[n - 1] if len(r) >= n else "" for r in self.rows]
        while vals and vals[-1] == "":
            vals.pop()  # gspread не отдаёт хвостовые пустые ячейки
        return vals

    def cell(self, r, c):
        if len(self.rows) < r or len(self.rows[r - 1]) < c:
            return ""
        return self.rows[r - 1][c - 1]

    # ---- запись ----
    def _ensure(self, r, c):
        while len(self.rows) < r:
            self.rows.append([])
        row = self.rows[r - 1]
        if len(row) < c:
            row.extend([""] * (c - len(row)))
        self.row_count = max(self.row_count, len(self.rows))

    def update(self, rng, values, value_input_option=None):
        self.calls.append(("update", rng, value_input_option))
        self._write(rng, values)

    def _write(self, rng, values):
        r1, c1, r2, c2 = parse_range(rng)
        for i, vals in enumerate(values):
            r = r1 + i
            if r > r2:
                raise AssertionError(f"значений больше, чем строк в диапазоне {rng}")
            if c1 + len(vals) - 1 > c2:
                raise AssertionError(f"значений больше, чем колонок в диапазоне {rng}")
            if r > self.row_count:
                raise AssertionError(f"строка {r} за пределами сетки ({self.row_count}) — exceeds grid limits")
            self._ensure(r, c1 + len(vals) - 1)
            for j, v in enumerate(vals):
                self.rows[r - 1][c1 - 1 + j] = v

    def batch_update(self, data, value_input_option=None):
        self.calls.append(("batch_update", [d["range"] for d in data], value_input_option))
        for d in data:
            self._write(d["range"], d["values"])

    def batch_format(self, formats):
        self.calls.append(("batch_format", [(f["range"], f["format"]) for f in formats]))
        for f in formats:
            r1, _, r2, _ = parse_range(f["range"])
            colour = f["format"]["backgroundColor"]
            for r in range(r1, r2 + 1):
                self.formats[r] = dict(colour)

    def add_rows(self, n):
        self.calls.append(("add_rows", n))
        self.row_count += int(n)

    def add_cols(self, n):
        self.calls.append(("add_cols", n))
        self.col_count += int(n)

    def insert_cols(self, *a, **k):
        raise AssertionError("вставка колонок запрещена")

    def append_rows(self, *a, **k):
        raise AssertionError("append_rows запрещён для UR REGS")

    def update_cell(self, *a, **k):
        raise AssertionError("update_cell пишет USER_ENTERED — запрещено")


def data_row(aid, *, started="2026-09-20 10:00:00", created="2026-09-20 10:05:00",
             name="Тест Тестов", age="20", email="t@example.com", uni="Тестовый университет",
             field="Экономика", course="2 курс бакалавриата", tg="@tester", vk="@tester_vk",
             consent="Да") -> list[str]:
    return [aid, started, created, name, age, email, uni, field, course, tg, vk, consent]


def probe_sheet(**kw) -> FakeWS:
    """Лист в раскладке 36-PROBE: шапка, ID …0001/…0002, пустые 4–6, …0003/…0004, P:Q Насти."""
    header = PROBE_HEADER + ["", "", "", "всего (ЦА):", "=SUM(Q2:Q16)"]
    rows = [
        header,
        data_row("2516200001", uni="Тестовый университет") + ["", "", "", "Тестовый университет", "2"],
        data_row("2516200002", uni="Другой вуз") + ["", "", "", "Другой вуз", "1"],
        [], [], [],
        data_row("2516200003", uni="Тестовый университет"),
        data_row("2516200004", uni="Третий вуз"),
    ]
    formats = {3: dict(GREY), 8: dict(GREEN)}
    return FakeWS(rows, formats, **kw)
