"""Зеркало формы делегаций во вкладку `UR REGS` «как выгрузка Яндекса».

Лист принадлежит команде (Настя вела его ручной выгрузкой): A = ID ответа, B = время начала
заполнения (у новых строк пусто — API его не отдаёт, у старых не трогаем), C = время создания,
D..L = девять вопросов формы в порядке шапки листа. Бот добавляет ровно одну служебную колонку
M «В боте» и ничего правее не трогает (в P:Q у Насти счётчик по вузам).

Правила записи (D-13/D-14):
- строка ищется по ID в колонке A; у известного ID обновляется ТОЛЬКО колонка M (D..L, A..C
  правит команда руками — перезапись затёрла бы её правки), дубликатов не бывает;
- новая строка пишется ПОСЛЕ последней непустой A явным диапазоном `A{r}:M{r}`, а не
  дописыванием «в конец» средствами gspread (у листа пустые строки 9–15 внутри данных,
  такое дописывание село бы не туда);
- вопрос формы, которому не нашлось колонки в D..L, в лист не пишется: сдвигать колонки
  Насти нельзя. Его подпись возвращается наружу — `drain_mirror` превращает её в
  неблокирующее предупреждение менеджеру (`external_forms.mirror_warning`);
- не-ЦА строки красятся серым `#D9D9D9`, ЦА-строки, бывшие серыми, возвращаются в белый;
  зелёные `#B6D7A8` (ручная пометка Насты) не перекрашиваются никогда;
- `value_input_option=RAW` + `database.db._sheet_safe`: RAW-ячейка в Google Sheets формулой
  не становится, апостроф-префикс испортил бы `@ник`/`-`/`+7…` (см. докстринг `_sheet_safe`).

Функции синхронные — вызываются через `asyncio.to_thread` из `ext_forms_mirror.drain_mirror`.
Доступ к листу — только через `ext_forms_mirror._open_tab_sync` (атрибут модуля: тесты
подменяют его FakeWS); вкладку зеркало не создаёт.

Сухая сверка `dry_run_sync` (D-31) ничего не пишет: считает строки листа, совпадения по ID,
новые и неизвестные листу ID, расхождения шапки D..L с подписями вопросов, свободна ли M.
Среди `answer_ids` могут быть надгробия удалённых в Яндексе ответов — такой ID без строки в
листе попадёт в «новых», хотя записан не будет (писатель идёт только по очереди ответов).
"""
from __future__ import annotations

import logging

from services import ext_forms_mirror

logger = logging.getLogger(__name__)

GREY = {"red": 0.851, "green": 0.851, "blue": 0.851}    # #D9D9D9 — серый «не ЦА» (36-PROBE)
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
GREEN = {"red": 0.714, "green": 0.843, "blue": 0.659}   # #B6D7A8 — пометка Насти, не трогать
_TOL = 0.02

FIRST_FORM_COL = 4   # D
LAST_FORM_COL = 12   # L
M_COL = 13           # M
M_HEADER = "В боте"
M_LABELS = {
    "ok_unlinked": "⏳ не заходил",
    "ok_linked": "✅ зашёл",
    "arrived": "🎟 пришёл",
    "no": "— не ЦА",
    "check": "❔ проверить курс",
}


class ColumnOccupiedError(Exception):
    """В M1 выбранного листа чужая подпись — бот туда не пишет."""


def _raw():
    from services.sheets import _RAW
    return _RAW


def _safe(value) -> str:
    from database.db import _sheet_safe
    return _sheet_safe("" if value is None else str(value))


def _col_letter(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def m_label(status: dict | None) -> str:
    """Подпись колонки M по строке `mirror_status_for`. Привязанный человек — «в боте»
    при любом вердикте; без оценки — «проверить»."""
    status = status or {}
    if status.get("linked"):
        return M_LABELS["arrived"] if status.get("arrived") else M_LABELS["ok_linked"]
    ta = status.get("ta_status")
    if ta == "no":
        return M_LABELS["no"]
    if ta == "ok":
        return M_LABELS["ok_unlinked"]
    return M_LABELS["check"]


def _norm(label) -> str:
    return " ".join(str(label or "").casefold().split())


def _map_columns(columns: list[dict], header: list[str]):
    """qkey -> номер колонки (1-based) в D..L. Сначала по подписи вопроса против шапки листа,
    потом по позиции (3 + position), если такая колонка ещё свободна. Возвращает
    (cmap, extra_labels, fallbacks[(col, sheet_label, form_label)])."""
    head = [str(h) for h in header[FIRST_FORM_COL - 1:LAST_FORM_COL]]
    head += [""] * (LAST_FORM_COL - FIRST_FORM_COL + 1 - len(head))
    by_label: dict[str, int] = {}
    for i, h in enumerate(head):
        key = _norm(h)
        if key and key not in by_label:
            by_label[key] = FIRST_FORM_COL + i
    cmap: dict[str, int] = {}
    used: set[int] = set()
    pending: list[dict] = []
    for c in columns:
        col = by_label.get(_norm(c.get("label")))
        if col is not None and col not in used:
            cmap[str(c["qkey"])] = col
            used.add(col)
        else:
            pending.append(c)
    extra: list[str] = []
    fallbacks: list[tuple[int, str, str]] = []
    for c in pending:
        try:
            pos = int(c.get("position") or 0)
        except (TypeError, ValueError):
            pos = 0
        col = FIRST_FORM_COL - 1 + pos
        if pos >= 1 and col <= LAST_FORM_COL and col not in used:
            cmap[str(c["qkey"])] = col
            used.add(col)
            fallbacks.append((col, head[col - FIRST_FORM_COL], str(c.get("label") or "")))
        else:
            extra.append(str(c.get("label") or c.get("qkey") or ""))
    return cmap, extra, fallbacks


def column_map(columns: list[dict], header: list[str]) -> tuple[dict[str, int], list[str]]:
    cmap, extra, _ = _map_columns(columns, header)
    return cmap, extra


def _id_index(ids: list[str]) -> tuple[dict[str, int], int, int]:
    """(ID -> номер строки первого вхождения, число дублей, последняя непустая строка)."""
    index: dict[str, int] = {}
    dups = 0
    last = 1
    for i, v in enumerate(ids):
        row = i + 1
        if v:
            last = row
        if row == 1 or not v:
            continue
        if v in index:
            dups += 1
        else:
            index[v] = row
    return index, dups, last


def _kind(colour: dict | None) -> str:
    if not isinstance(colour, dict):
        return "unknown"
    for name, ref in (("white", WHITE), ("grey", GREY), ("green", GREEN)):
        if all(abs(float(colour.get(ch, 0.0)) - ref[ch]) <= _TOL for ch in ("red", "green", "blue")):
            return name
    return "other"


def _row_colours(ws, tab: str, last_row: int) -> dict[int, dict]:
    """Фон колонки A строк 2..last_row через fetch_sheet_metadata. Исключения — наружу."""
    if last_row < 2:
        return {}
    title = str(getattr(ws, "title", None) or tab).replace("'", "''")
    meta = ws.spreadsheet.fetch_sheet_metadata({
        "includeGridData": True,
        "ranges": [f"'{title}'!A2:A{last_row}"],
        "fields": "sheets.data.rowData.values.effectiveFormat.backgroundColor",
    })
    out: dict[int, dict] = {}
    sheets = meta.get("sheets") or []
    data = (sheets[0].get("data") or []) if sheets else []
    row_data = (data[0].get("rowData") or []) if data else []
    for i, rd in enumerate(row_data):
        values = (rd or {}).get("values") or []
        colour = ((values[0] or {}).get("effectiveFormat") or {}).get("backgroundColor") if values else None
        if isinstance(colour, dict):
            out[2 + i] = colour
    return out


def write_export_sync(tab: str, columns: list[dict], rows: list[tuple[int, dict, dict]],
                      greyed_out: dict | None = None):
    """rows: (answer_row_id, answer, status). Возврат (appended, updated, extra_questions)
    или None, если вкладки нет. `extra_questions` — подписи вопросов формы без колонки в D..L
    (в лист не пишутся, менеджера предупреждает drain). `greyed_out` — словарь, который функция
    заполняет {answer_id: 1|0} по строкам, где бот сам поставил или снял серый фон: вызывающий
    сохраняет флаг, чтобы потом не снять серый, поставленный командой."""
    ws = ext_forms_mirror._open_tab_sync(tab)
    if ws is None:
        return None
    header = [str(h) for h in (ws.row_values(1) or [])]
    m1 = header[M_COL - 1].strip() if len(header) >= M_COL else ""
    if m1 and m1 != M_HEADER:
        raise ColumnOccupiedError(tab)
    ext_forms_mirror._ensure_cols(ws, M_COL)
    batch: list[dict] = []
    if not m1:
        batch.append({"range": f"{_col_letter(M_COL)}1", "values": [[M_HEADER]]})

    ids = [str(v).strip() for v in (ws.col_values(1) or [])]
    index, dups, last_nonempty = _id_index(ids)
    if dups:
        logger.warning("delegations_mirror: в листе «%s» %d дублей ID в колонке A — обновляю "
                       "первое вхождение", tab, dups)
    cmap, extra, _ = _map_columns(columns, header)

    new_ids = {str(a.get("answer_id") or "").strip() for _, a, _ in rows} - set(index)
    next_row = last_nonempty + 1
    row_count = getattr(ws, "row_count", None)
    need = next_row + len(new_ids) - 1
    if isinstance(row_count, int) and need > row_count:
        ws.add_rows(need - row_count)

    appended = updated = 0
    new_rows: set[int] = set()
    targets: dict[int, dict] = {}
    for _, answer, status in rows:
        aid = str(answer.get("answer_id") or "").strip()
        values: dict[int, str] = {}
        for item in answer.get("payload") or []:
            col = cmap.get(str(item.get("q")))
            if col:
                values[col] = _safe(item.get("value"))
        m = m_label(status)
        r = index.get(aid)
        if r is None:
            r = next_row
            next_row += 1
            index[aid] = r
            new_rows.add(r)
            full = [_safe(aid), "", _safe(answer.get("answered_at"))]
            full += [values.get(c, "") for c in range(FIRST_FORM_COL, LAST_FORM_COL + 1)]
            full.append(m)
            batch.append({"range": f"A{r}:{_col_letter(M_COL)}{r}", "values": [full]})
            appended += 1
        else:
            # Известная строка: только колонка M. D..L Настя правит руками (исправленное ФИО,
            # допечатанный ник), пересыл ответа из формы затёр бы эти правки — в том числе
            # пустым значением поверх заполненной ячейки.
            batch.append({"range": f"{_col_letter(M_COL)}{r}", "values": [[m]]})
            updated += 1
        targets[r] = (GREY if (status or {}).get("ta_status") == "no" else WHITE, aid, status or {})

    if batch:
        ws.batch_update(batch, value_input_option=_raw())

    # Цвет: серым бот красит только белую строку, в белый возвращает только ту, что покрасил
    # сам (флаг `greyed`); серое от руки, зелёные и неизвестные не трогаем.
    current: dict[int, dict] = {}
    colours_ok = True
    try:
        current = _row_colours(ws, tab, max(targets) if targets else 1)
    except Exception as exc:  # noqa: BLE001 — цвет не стоит сорванной записи
        colours_ok = False
        logger.warning("delegations_mirror: не прочитать цвета листа «%s»: %s", tab, type(exc).__name__)
    formats: list[dict] = []
    flags: dict = {}
    for r, (target, aid, status) in sorted(targets.items()):
        if r in new_rows:
            kind = _kind(current.get(r, WHITE)) if colours_ok else "white"
        elif colours_ok:
            kind = _kind(current.get(r))
        else:
            continue
        ours = bool(status.get("greyed"))
        if target is GREY:
            if kind == "white":
                formats.append({"range": f"A{r}:{_col_letter(M_COL)}{r}",
                                "format": {"backgroundColor": dict(GREY)}})
                flags[aid] = 1
        elif kind == "grey" and ours:
            formats.append({"range": f"A{r}:{_col_letter(M_COL)}{r}",
                            "format": {"backgroundColor": dict(WHITE)}})
            flags[aid] = 0
        elif kind == "white" and ours:
            flags[aid] = 0  # команда сама вернула белый — запомненный серый больше не наш
    if greyed_out is not None:
        greyed_out.update(flags)
    if formats:
        ws.batch_format(formats)

    if extra:
        logger.warning("delegations_mirror: «%s»: добавлено %d, обновлено %d; вопросы без колонки: %s",
                       tab, appended, updated, ", ".join(extra))
    else:
        logger.info("delegations_mirror: «%s»: добавлено %d, обновлено %d", tab, appended, updated)
    return appended, updated, extra


def dry_run_sync(tab: str, columns: list[dict], answer_ids: list[str]) -> dict | None:
    """Сверка без записи. None — вкладки нет."""
    ws = ext_forms_mirror._open_tab_sync(tab)
    if ws is None:
        return None
    header = [str(h) for h in (ws.row_values(1) or [])]
    ids = [str(v).strip() for v in (ws.col_values(1) or [])]
    index, dups, last_nonempty = _id_index(ids)
    sheet_ids = set(index)
    wanted = {str(a).strip() for a in answer_ids if str(a).strip()}
    cmap, extra, fallbacks = _map_columns(columns, header)
    m1 = header[M_COL - 1].strip() if len(header) >= M_COL else ""
    grey = green = 0
    colours_ok = True
    try:
        for r, colour in _row_colours(ws, tab, last_nonempty).items():
            if r > last_nonempty or not (ids[r - 1] if r - 1 < len(ids) else ""):
                continue
            kind = _kind(colour)
            grey += kind == "grey"
            green += kind == "green"
    except Exception as exc:  # noqa: BLE001
        colours_ok = False
        logger.warning("delegations_mirror: сверка «%s»: цвета не прочитаны: %s", tab, type(exc).__name__)
    head_cells = header[:LAST_FORM_COL] + [""] * (LAST_FORM_COL - len(header))
    return {
        "sheet_rows": sum(1 for v in ids[1:] if v),
        "matched": len(wanted & sheet_ids),
        "new": len(wanted - sheet_ids),
        "unknown_sheet_ids": len(sheet_ids - wanted),
        "duplicate_sheet_ids": dups,
        "header": head_cells,
        "header_diff": [(_col_letter(col), sheet_label, form_label)
                        for col, sheet_label, form_label in fallbacks],
        "m_free": (not m1) or m1 == M_HEADER,
        "m_header": m1,
        "extra_questions": extra,
        "grey_rows": grey,
        "green_rows": green,
        "colours_ok": colours_ok,
        "mapped_questions": len(cmap),
    }
