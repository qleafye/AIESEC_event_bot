"""Импорт старых ответов личной Яндекс Формы из файла выгрузки (XLSX или CSV).

Без новых зависимостей: XLSX читается стандартной библиотекой (zipfile + xml). Колонка с ID
ответа становится answer_id (по нему анкета не задвоится ни при повторной загрузке файла, ни
с ответом, уже пришедшим вебхуком), колонка со временем — answered_at, остальные — вопросы.
В лог значения анкет не попадают.
"""
from __future__ import annotations

import csv
import io
import logging
import posixpath
import re
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from database import ext_forms_db as ef
from services.ext_forms.ext_forms_google import _parse_ts
from services.ext_forms.ext_forms_ingest import ingest_answer

logger = logging.getLogger(__name__)

MAX_ROWS = 5000
MAX_UNPACKED = 50 * 1024 * 1024

_ID_HEADERS = {"id", "id ответа", "номер ответа", "answer id", "answer_id"}
_TIME_HEADERS = {"время создания", "время отправки", "время отправки ответа", "дата", "created"}
_ANSWER_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,40}$")
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_EXCEL_EPOCH = datetime(1899, 12, 30)


class ImportFileError(Exception):
    """Файл не годится; `text` — что сделать менеджеру."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


# ---------- чтение файла ----------

def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Za-z]+", ref or "")
    if not letters:
        return -1
    n = 0
    for ch in letters.group(0).upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _num(text: str) -> str:
    """Число из ячейки: целое без «.0» и без экспоненты (ID ответа не должен стать 1.2E+11)."""
    text = text.strip()
    try:
        return str(int(text))
    except ValueError:
        pass
    try:
        d = Decimal(text)
    except InvalidOperation:
        return text
    if d == d.to_integral_value():
        return str(int(d))
    return text


def _xml(zf: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        data = zf.read(name)
    except KeyError:
        return None
    # ElementTree не защищён от раздувания сущностей: в выгрузке Яндекса DTD быть не может.
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ImportFileError("Файл выглядит повреждённым. Выгрузите ответы заново из Яндекс Форм.")
    return ET.fromstring(data)


def _iter(el: ET.Element, local: str):
    """Потомки по локальному имени тега (iter() не понимает «{*}»)."""
    return (e for e in el.iter() if e.tag.rpartition("}")[2] == local)


def _si_text(si: ET.Element) -> str:
    return "".join(t.text or "" for t in _iter(si, "t"))


def _sheet_path(zf: zipfile.ZipFile) -> str:
    wb = _xml(zf, "xl/workbook.xml")
    rels = _xml(zf, "xl/_rels/workbook.xml.rels")
    if wb is not None and rels is not None:
        sheet = next(iter(_iter(wb, "sheet")), None)
        rid = sheet.get(f"{{{_REL_NS}}}id") if sheet is not None else None
        for rel in _iter(rels, "Relationship"):
            if rid and rel.get("Id") == rid:
                target = rel.get("Target") or ""
                if target.startswith("/"):
                    return target.lstrip("/")
                return posixpath.normpath(posixpath.join("xl", target))
    return "xl/worksheets/sheet1.xml"


def _read_xlsx(data: bytes) -> list[list[str]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportFileError("Не получилось открыть файл как XLSX. Выгрузите ответы заново из "
                              "Яндекс Форм (XLSX или CSV).") from None
    with zf:
        if sum(i.file_size for i in zf.infolist()) > MAX_UNPACKED:
            raise ImportFileError("Файл слишком большой после распаковки. Выгрузите ответы "
                                  "частями или в формате CSV.")
        try:
            shared: list[str] = []
            sst = _xml(zf, "xl/sharedStrings.xml")
            if sst is not None:
                shared = [_si_text(si) for si in _iter(sst, "si")]
            sheet = _xml(zf, _sheet_path(zf))
        except ET.ParseError:
            raise ImportFileError("Файл повреждён. Выгрузите ответы заново из Яндекс Форм.") from None
    if sheet is None:
        raise ImportFileError("В файле нет листа с ответами. Выгрузите ответы заново из Яндекс Форм.")

    rows: list[list[str]] = []
    for row in _iter(sheet, "row"):
        cells: dict[int, str] = {}
        nxt = 0
        for c in _iter(row, "c"):
            idx = _col_index(c.get("r") or "")
            if idx < 0:
                idx = nxt
            nxt = idx + 1
            kind = c.get("t")
            v = c.find("{*}v")
            if kind == "inlineStr":
                is_ = c.find("{*}is")
                value = _si_text(is_) if is_ is not None else ""
            elif v is None or v.text is None:
                value = ""
            elif kind == "s":
                try:
                    value = shared[int(v.text)]
                except (ValueError, IndexError):
                    value = ""
            elif kind in ("str", "e"):
                value = v.text
            elif kind == "b":
                value = "Да" if v.text.strip() == "1" else "Нет"
            else:
                value = _num(v.text)
            cells[idx] = value
        width = (max(cells) + 1) if cells else 0
        rows.append([cells.get(i, "") for i in range(width)])
    return rows


def _read_csv(data: bytes) -> list[list[str]]:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ImportFileError("Не понял кодировку файла. Выгрузите ответы из Яндекс Форм в XLSX.")
    if "\x00" in text:
        raise ImportFileError("Это не похоже на выгрузку ответов. Пришлите файл XLSX или CSV "
                              "из Яндекс Форм.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    try:
        return [list(r) for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    except csv.Error:
        raise ImportFileError("Не получилось прочитать файл как CSV. Выгрузите ответы заново.") from None


def read_export_rows(data: bytes, filename: str = "") -> list[list[str]]:
    """Строки выгрузки как списки строк; бросает ImportFileError с понятным текстом."""
    if data[:4] == b"PK\x03\x04" or (filename or "").lower().endswith(".xlsx"):
        rows = _read_xlsx(data)
    else:
        rows = _read_csv(data)
    if not any(any(c.strip() for c in r) for r in rows):
        raise ImportFileError("В файле нет ответов. Выгрузите ответы заново из Яндекс Форм.")
    return rows


# ---------- строки -> анкеты ----------

def _answered_at(value: str) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    # Допущение: время в выгрузке уже московское (как в интерфейсе Яндекс Форм у менеджера),
    # поэтому, в отличие от вебхука (UTC), сдвиг не делаем.
    if re.fullmatch(r"\d+(\.\d+)?", v):
        try:
            return (_EXCEL_EPOCH + timedelta(days=float(v))).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError):
            return None
    got = _parse_ts(v)
    if got:
        return got
    for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def export_to_answers(
    rows: list[list[str]],
) -> tuple[list[tuple[str, str | None, list[dict]]], int]:
    """-> ([(answer_id, answered_at, items)], число строк без ID)."""
    if not rows:
        raise ImportFileError("В файле нет ответов. Выгрузите ответы заново из Яндекс Форм.")
    header = [str(h).strip() for h in rows[0]]
    id_idx = next((i for i, h in enumerate(header) if h.lower() in _ID_HEADERS), None)
    if id_idx is None:
        raise ImportFileError("В файле нет колонки с ID ответа — выгрузите ответы заново из "
                              "Яндекс Форм")
    time_idx = next((i for i, h in enumerate(header) if h.lower() in _TIME_HEADERS), None)
    if len(rows) - 1 > MAX_ROWS:
        raise ImportFileError(f"В файле больше {MAX_ROWS} ответов — загрузите его частями.")

    seen: dict[str, int] = {}
    titles: list[str] = []
    for i, h in enumerate(header):
        base = h or f"Колонка {i + 1}"
        n = seen.get(base, 0) + 1
        seen[base] = n
        titles.append(base if n == 1 else f"{base} ({n})")

    out: list[tuple[str, str | None, list[dict]]] = []
    no_id = 0
    for row in rows[1:]:
        cells = [str(c) for c in row]
        if not any(c.strip() for c in cells):
            continue
        cells += [""] * (len(header) - len(cells))
        answer_id = cells[id_idx].strip()
        if not _ANSWER_ID_RE.match(answer_id):
            no_id += 1
            continue
        answered_at = _answered_at(cells[time_idx]) if time_idx is not None else None
        items = [{"q": titles[i], "label": titles[i], "value": cells[i]}
                 for i in range(len(header)) if i != id_idx and i != time_idx]
        out.append((answer_id, answered_at, items))
    return out, no_id


async def import_answers(form: dict, rows: list[list[str]]) -> dict:
    """Сохраняет анкеты формы; -> {added, existed, no_id}. Уже известные (в том числе
    удалённые) не трогаются, повтор того же файла добавляет 0."""
    answers, no_id = export_to_answers(rows)
    known = await ef.known_answer_ids(form["id"])
    added = existed = 0
    for answer_id, answered_at, items in answers:
        if answer_id in known:
            existed += 1
            continue
        if await ingest_answer(form, answer_id=answer_id, answered_at=answered_at,
                               items=items, raw=None):
            added += 1
            known.add(answer_id)
        else:
            existed += 1
    return {"added": added, "existed": existed, "no_id": no_id}
