"""Phase 33 (delegate-card admin actions) — «🔍 Сверить с БД» (раздел «📊 Данные», рядом с
«🔄 Синхронизация»/«♻️ Пересобрать таблицу»). Кнопка на карточке делегата (Кристина Мухина,
33-SEED) заменена веб-путём «переведи её в Москву»; сама сверка живёт здесь.

Шаг 1 (`build_report`) — ТОЛЬКО ЧТЕНИЕ: сравнивает `users` ТЕКУЩЕГО сезона (`reg_engine.
is_past_season_row` — те же 482 импортированных делегата прошлого сезона иначе завалили бы
отчёт ложными «нет строки») с РЕАЛЬНЫМИ вкладками таблицы. Раскладка «пользователь → вкладка →
шапка → строка» — тот же единый строитель, что у «🔄 Синхронизация»/«♻️ Пересобрать таблицу»
(`handlers.admin_sheets.build_sheet_batches`, Phase 25/квик 260915-4is) — не изобретаем
собственное правило резолва вкладки, DRY и ноль риска разъехаться.

Ни один читающий или пишущий вызов НЕ создаёт вкладку: `_read_tab_snapshot_sync` резолвит через
`services.sheets._open_named_or_main_sync` (тот же no-create контракт, что у `find_rows_by_id`/
`delete_row_by_id`, Phase 33 review R1 — памятка `standalone-script-sheet-traps`, находка 21.09:
разовый скрипт завёл пустую вкладку на проде именно через auto-create по имени). Шаг 2 —
массовые исправления (`apply_append_missing`/`apply_fix_statuses`) ПЕРЕСЧИТЫВАЮТ расхождения
заново (`build_report`) перед применением — состояние листа могло измениться между открытием
отчёта и тапом кнопки; повторный тап поймает in-memory замок (`_claim`/`_release`) и не
задвоит работу.

aiogram-free (тот же разрез, что `services/city_move.py`/`services/reject_journal.py`) — вызывающий
хендлер (`handlers/admin_sheet_reconcile.py`) строит текст/клавиатуры сам."""
from __future__ import annotations

import asyncio
import difflib
import logging

from database.db import _csv_safe, get_all_users_dicts, get_all_users_ids, get_setting
from reg_engine import is_past_season_row
from reg_labels import STATUS_LABELS
import services.sheets as sheets_service

logger = logging.getLogger(__name__)

STATUS_HEADER = sheets_service.STATUS_HEADER  # "Статус" — общий с services/sheets.py

_TRACK_LABEL = {"main": "полная анкета", "short": "короткая анкета", "party": "party"}

# Пауза между единичными пишущими вызовами массовых исправлений — не бьём Google API квотой
# (тот же посыл, что `services/application_effects.py::mass_approve_effects`'s 0.05с, здесь
# чуть щедрее — reconcile трогает МЕНЬШЕ строк за раз, но каждая может уйти на именованную
# вкладку с отдельным сетевым вызовом).
_APPEND_PAUSE_S = 0.3
_STATUS_PAUSE_S = 0.2

_SHEET_RESULT_TEXT = {
    "not_found_tab": "вкладки нет в таблице",
    "error": "ошибка таблицы",
}

# Недоставленные решения (одобрение/отказ): признака доставки в БД НЕТ — `apply_decision_effects`
# (services/application_effects.py) шлёт делегату напрямую и на сбое только логирует
# (`logger.error(f"Failed to notify rejected user ...")`), ничего не сохраняя. НЕ выдумываем
# признак — честно называем, что нужно, чтобы он появился (тот же паттерн, что уже есть у SOS:
# `sos_reports.delivery_failed_at`, services/sos.py::record_delivery_outcome).
DECISIONS_UNDELIVERED_NOTE = (
    "по базе это не определить: решения (одобрение/отказ) шлются напрямую ботом, а успех или "
    "сбой доставки нигде не сохраняется — только в лог сервера. Чтобы «кому не дошло» можно "
    "было увидеть здесь, нужен признак доставки на каждое решение (как у SOS-заявок, поле "
    "delivery_failed_at) — отдельная задача, не эта сверка."
)


def _user_matches_scope(user: dict, scope: tuple[str, tuple[str, ...]] | None) -> bool:
    """Тот же приём, что `database.db._city_clause`, но в Python над уже прочитанным списком
    (сверка и так проходит по всем current-season пользователям ради построения батчей —
    второй SQL-запрос был бы дублем). `scope` — дескриптор `cities.city_scope(...)` ПО ЗНАЧЕНИЮ,
    без импорта `cities` (тот же приём, что у `_city_clause` самого)."""
    if scope is None:
        return True
    code, exclude = scope
    raw = user.get("event_city")
    if not exclude:
        return raw == code
    return raw is None or raw not in exclude


async def _current_season_users(*, city_scope: tuple | None = None) -> list[dict]:
    event_season = (await get_setting("event_season") or "").strip() or None
    all_users = await get_all_users_dicts()
    return [
        u for u in all_users
        if not is_past_season_row(u, event_season) and _user_matches_scope(u, city_scope)
    ]


def _read_tab_snapshot_sync(tab_name: str | None) -> list[list[str]] | None:
    """ОДИН batch-вызов (`get_all_values`) на вкладку — заголовок + все строки одним запросом.
    `None`, если вкладки нет среди РЕАЛЬНЫХ (никогда не создаёт — резолвер тот же, что у
    `find_rows_by_id`/`delete_row_by_id`, `services/sheets.py::_open_named_or_main_sync`)."""
    sheet = sheets_service._open_named_or_main_sync(tab_name)
    if sheet is None:
        return None
    return sheet.get_all_values()


async def build_report(*, city_scope: tuple | None = None) -> dict:
    """Только чтение. `report["ok"] is False` — таблица недоступна (Sheets не настроен или
    сбой API до похода по вкладкам) — остальные списки в этом случае пустые, `report["error"]`
    называет причину. Пересчитывается заново на КАЖДЫЙ вызов (нет кеша) — и для самого отчёта,
    и как первый шаг обоих массовых исправлений ниже."""
    users = await _current_season_users(city_scope=city_scope)
    from handlers.admin_sheets import build_sheet_batches  # ленивый импорт против цикла

    batches = await build_sheet_batches(users)
    main_batch, named_batches = batches[0], batches[1:]

    real_titles = await sheets_service.list_worksheet_titles()
    report: dict = {
        "ok": real_titles is not None,
        "error": None,
        "user_count": len(users),
        "missing_tabs": [],
        "missing_rows": [],
        "duplicate_rows": [],
        "status_mismatch": [],
        "unknown_sheet_ids": [],
        "headerless_tabs": [],
        "decisions_undelivered_note": DECISIONS_UNDELIVERED_NOTE,
    }
    if real_titles is None:
        report["error"] = "таблица недоступна (Google Sheets не настроен или ошибка API)"
        return report

    real_set = set(real_titles)
    all_db_ids = set(await get_all_users_ids())

    for batch in named_batches:
        if batch.tab not in real_set:
            suggestion = difflib.get_close_matches(batch.tab, real_titles, n=1)
            report["missing_tabs"].append({
                "tab": batch.tab, "kind": batch.kind, "count": len(batch.users),
                "suggestion": suggestion[0] if suggestion else None,
            })

    # Главная вкладка читается ТОЛЬКО когда на неё реально маршрутизирован хоть один текущий
    # делегат — иначе `_open_named_or_main_sync(None)` (через `_get_sheet()`) требует явно
    # настроенную главную вкладку (bot_settings.main_sheet_tab / GOOGLE_SHEET_TAB) даже когда
    # сверке о ней нечего спрашивать (модуль городов включён, все текущие делегаты — в городах).
    batch_by_tab: dict[str | None, object] = {}
    if main_batch.users:
        batch_by_tab[None] = main_batch
    for b in named_batches:
        if b.tab in real_set:
            batch_by_tab[b.tab] = b

    id_to_user = {u["telegram_id"]: u for u in users}

    for tab, batch in batch_by_tab.items():
        values = await asyncio.to_thread(_read_tab_snapshot_sync, tab)
        if values is None:
            # Вкладку успели снести/переименовать между list_worksheet_titles() и открытием —
            # узкая гонка, пропускаем эту вкладку в ЭТОМ прогоне, а не падаем всем отчётом.
            logger.warning(f"sheet_reconcile: tab {tab!r} disappeared mid-read, skipping")
            continue
        expected_ids = {str(u["telegram_id"]) for u in batch.users}

        if not values:
            for tid_str in expected_ids:
                u = id_to_user[int(tid_str)]
                report["missing_rows"].append({
                    "tid": int(tid_str), "name": u.get("full_name"), "username": u.get("username"),
                    "tab": tab,
                })
            continue

        # Та же эвристика, что `services/sheets.py::_ensure_header_sync` уже применяет при
        # живом аппенде: первая ячейка — число (telegram_id) -> заголовка нет, это данные
        # (инцидент 13.09: «СПб Акция» без заголовков, lost-applications-260914).
        first_cell = (values[0][0] if values[0] else "").strip()
        headerless = bool(first_cell) and first_cell.lstrip("-").isdigit()
        if headerless:
            report["headerless_tabs"].append(tab if tab is not None else "(главная)")
            data_rows = values
            status_col = -1
        else:
            header = [h.strip() for h in values[0]]
            data_rows = values[1:]
            status_col = header.index(STATUS_HEADER) if STATUS_HEADER in header else -1

        rows_by_id: dict[str, list[list]] = {}
        for row in data_rows:
            rid = (row[0] if row else "").strip()
            if rid:
                rows_by_id.setdefault(rid, []).append(row)

        dup_ids = {rid for rid, rows in rows_by_id.items() if len(rows) > 1 and rid.lstrip("-").isdigit()}
        for rid in sorted(dup_ids, key=int):
            report["duplicate_rows"].append({"tid": int(rid), "tab": tab, "count": len(rows_by_id[rid])})

        for tid_str in sorted(expected_ids, key=int):
            rows = rows_by_id.get(tid_str)
            if not rows:
                u = id_to_user[int(tid_str)]
                report["missing_rows"].append({
                    "tid": int(tid_str), "name": u.get("full_name"), "username": u.get("username"),
                    "tab": tab,
                })
                continue
            if tid_str in dup_ids:
                continue  # уже в duplicate_rows — статус не сверяем, неясно, какая строка живая
            if status_col >= 0:
                row0 = rows[0]
                sheet_label = (row0[status_col] if status_col < len(row0) else "").strip()
                u = id_to_user[int(tid_str)]
                expected_label = STATUS_LABELS.get(u.get("status") or "pending")
                if expected_label and sheet_label and sheet_label != expected_label:
                    report["status_mismatch"].append({
                        "tid": int(tid_str), "tab": tab,
                        "expected_label": expected_label, "sheet_label": sheet_label,
                    })

        for rid in rows_by_id:
            if not rid.lstrip("-").isdigit():
                continue
            tid = int(rid)
            if tid not in all_db_ids:
                report["unknown_sheet_ids"].append({"tid": tid, "tab": tab})

    return report


# ── Шаг 2: массовые исправления ──────────────────────────────────────────────────────────────
# Двойной тап — простой in-memory замок (процесс один, перезапуск снимает сам собой, тот же
# посыл, что у остальных in-flight-флагов проекта): второй тап видит «уже выполняется» вместо
# повторной записи тех же строк.
_INFLIGHT: set[str] = set()


def _claim(key: str) -> bool:
    if key in _INFLIGHT:
        return False
    _INFLIGHT.add(key)
    return True


def _release(key: str) -> None:
    _INFLIGHT.discard(key)


def _scope_key(city_scope: tuple | None) -> str:
    return city_scope[0] if city_scope else "*"


async def apply_append_missing(*, city_scope: tuple | None = None) -> dict:
    """Дописывает недостающие строки ТОЛЬКО на существующие вкладки (`services.sheets::
    append_to_existing_named_sheet` — никогда не создаёт, Phase 33 review R1), с шапкой
    (`ensure_sheet_header`/`ensure_named_sheet_header` перед первой строкой на вкладку — та же
    последовательность, что `sync_sheet`, заодно чинит «вкладку без заголовков», если она
    попала в выборку). По одной строке с паузой; сбой одной НЕ прерывает остальные — итог несёт
    `done`/`failed` (причина словами на каждую)."""
    key = f"append:{_scope_key(city_scope)}"
    if not _claim(key):
        return {"ok": False, "error": "уже выполняется — подождите завершения предыдущего запуска"}
    try:
        report = await build_report(city_scope=city_scope)
        if not report["ok"]:
            return {"ok": False, "error": report["error"] or "таблица недоступна"}
        if not report["missing_rows"]:
            return {"ok": True, "done": 0, "failed": []}

        users = await _current_season_users(city_scope=city_scope)
        from handlers.admin_sheets import build_sheet_batches  # ленивый импорт против цикла

        batches = await build_sheet_batches(users)
        main_batch, named_batches = batches[0], batches[1:]
        batch_by_tab = {None: main_batch, **{b.tab: b for b in named_batches}}

        missing_by_tab: dict[str | None, list[dict]] = {}
        for item in report["missing_rows"]:
            missing_by_tab.setdefault(item["tab"], []).append(item)

        done = 0
        failed: list[dict] = []
        headers_ensured: set[str | None] = set()
        for tab, items in missing_by_tab.items():
            batch = batch_by_tab.get(tab)
            if batch is None:
                for it in items:
                    failed.append({**it, "reason": "вкладка исчезла при пересчёте"})
                continue
            row_by_tid = {u["telegram_id"]: r for u, r in zip(batch.users, batch.rows)}
            if tab not in headers_ensured:
                if tab is None:
                    await sheets_service.ensure_sheet_header(batch.headers)
                else:
                    await sheets_service.ensure_named_sheet_header(tab, batch.headers)
                headers_ensured.add(tab)
            for it in items:
                row = row_by_tid.get(it["tid"])
                if row is None:
                    failed.append({**it, "reason": "делегат больше не маршрутизируется на эту вкладку"})
                    continue
                result = await sheets_service.append_to_existing_named_sheet(tab, row)
                if result == "ok":
                    done += 1
                else:
                    failed.append({**it, "reason": _SHEET_RESULT_TEXT.get(result, result)})
                await asyncio.sleep(_APPEND_PAUSE_S)
        return {"ok": True, "done": done, "failed": failed}
    finally:
        _release(key)


async def apply_fix_statuses(*, city_scope: tuple | None = None) -> dict:
    """Правит статус ТОЛЬКО там, где на вкладке РОВНО одна строка делегата (дубли — только
    показываем, руками) — тем же путём, что решение модератора (`services.sheets::
    update_status_in_sheet`). По одному с паузой; сбой одного не прерывает остальные."""
    key = f"status:{_scope_key(city_scope)}"
    if not _claim(key):
        return {"ok": False, "error": "уже выполняется — подождите завершения предыдущего запуска"}
    try:
        report = await build_report(city_scope=city_scope)
        if not report["ok"]:
            return {"ok": False, "error": report["error"] or "таблица недоступна"}
        if not report["status_mismatch"]:
            return {"ok": True, "done": 0, "failed": []}

        done = 0
        failed: list[dict] = []
        for item in report["status_mismatch"]:
            ok = await sheets_service.update_status_in_sheet(item["tid"], item["expected_label"])
            if ok:
                done += 1
            else:
                failed.append({**item, "reason": "строка не найдена при записи"})
            await asyncio.sleep(_STATUS_PAUSE_S)
        return {"ok": True, "done": done, "failed": failed}
    finally:
        _release(key)


# ── Рендер отчёта человеческими словами + CSV ────────────────────────────────────────────────

def chunk_report_lines(lines: list[str], limit: int = 4096) -> list[str]:
    """Режет готовые (уже HTML-эскейпленные) строки на части ≤`limit` символов, никогда не
    разрывая строку пополам — граница чанка всегда между строками. Не изобретаем свой резчик:
    `moderation_card.split_for_telegram` уже делает это (и уже покрыт тестами) для «📄 Полная
    анкета» (handlers/admin_moderation.py::appr_full) — тот же класс задачи."""
    from moderation_card import split_for_telegram

    return split_for_telegram("\n".join(lines), limit=limit)


def _first_n(items: list, n: int = 10) -> tuple[list, int]:
    return items[:n], max(0, len(items) - n)


def render_report_lines(report: dict, *, city_label: str | None = None) -> list[str]:
    """Готовые (HTML-эскейпленные) строки отчёта, ДО чанкования (`chunk_report_lines`). Не
    зовёт Telegram API сама (aiogram-free модуль) — только строит текст."""
    import html as html_module

    lines: list[str] = ["🔍 <b>Сверка таблицы с БД</b>"]
    if city_label:
        lines.append(f"Город: {html_module.escape(city_label)}")
    lines.append(f"Делегатов текущего сезона: <b>{report['user_count']}</b>")
    lines.append("")

    if not report["ok"]:
        lines.append(f"❌ {html_module.escape(str(report['error'] or 'таблица недоступна'))}")
        return lines

    if report["missing_tabs"]:
        lines.append("📄 <b>Вкладки, которых нет в таблице:</b>")
        for it in report["missing_tabs"]:
            kind_label = _TRACK_LABEL.get(it["kind"], it["kind"])
            tab = html_module.escape(str(it["tab"]))
            note = f" — делегатов: {it['count']} ({kind_label})"
            if it.get("suggestion"):
                note += f"; похожая вкладка есть: «{html_module.escape(it['suggestion'])}»?"
            lines.append(f"  ожидалась «{tab}»{note}")
        lines.append("")
    else:
        lines.append("📄 Все ожидаемые вкладки на месте.")
        lines.append("")

    if report["headerless_tabs"]:
        lines.append("⚠️ <b>Вкладки без заголовков</b> (первая строка — данные, не подписи):")
        for tab in report["headerless_tabs"]:
            lines.append(f"  «{html_module.escape(str(tab))}»")
        lines.append("")

    shown, extra = _first_n(report["missing_rows"])
    if report["missing_rows"]:
        lines.append(f"👤 <b>Делегаты без строки на своей вкладке</b> ({len(report['missing_rows'])}):")
        for it in shown:
            name = html_module.escape(str(it.get("name") or it["tid"]))
            # database.db.store_username канонически хранит username УЖЕ с «@» (или плейсхолдер
            # «-»/пусто) — второй «@» здесь не приписываем.
            username = it.get("username") or "-"
            tab = html_module.escape(str(it["tab"] if it["tab"] is not None else "главная"))
            lines.append(f"  {name} ({html_module.escape(username)}) → «{tab}»")
        if extra:
            lines.append(f"  …и ещё {extra}")
        lines.append("")

    if report["duplicate_rows"]:
        lines.append(f"🧬 <b>Дубли строк</b> ({len(report['duplicate_rows'])}):")
        shown, extra = _first_n(report["duplicate_rows"])
        for it in shown:
            tab = html_module.escape(str(it["tab"] if it["tab"] is not None else "главная"))
            lines.append(f"  id {it['tid']} на «{tab}»: {it['count']} строк")
        if extra:
            lines.append(f"  …и ещё {extra}")
        lines.append("")

    if report["status_mismatch"]:
        lines.append(f"🔄 <b>Расхождение статуса</b> ({len(report['status_mismatch'])}):")
        shown, extra = _first_n(report["status_mismatch"])
        for it in shown:
            tab = html_module.escape(str(it["tab"] if it["tab"] is not None else "главная"))
            lines.append(
                f"  id {it['tid']} на «{tab}»: в БД «{html_module.escape(it['expected_label'])}», "
                f"в листе «{html_module.escape(it['sheet_label'])}»"
            )
        if extra:
            lines.append(f"  …и ещё {extra}")
        lines.append("")

    if report["unknown_sheet_ids"]:
        lines.append(f"❓ <b>Строки с id, которого нет в БД</b> ({len(report['unknown_sheet_ids'])}, не трогаем):")
        shown, extra = _first_n(report["unknown_sheet_ids"])
        for it in shown:
            tab = html_module.escape(str(it["tab"] if it["tab"] is not None else "главная"))
            lines.append(f"  id {it['tid']} на «{tab}»")
        if extra:
            lines.append(f"  …и ещё {extra}")
        lines.append("")

    lines.append("📨 <b>Недоставленные решения (одобрение/отказ):</b>")
    lines.append(f"  {html_module.escape(report['decisions_undelivered_note'])}")

    return lines


def report_to_csv_bytes(report: dict) -> bytes:
    """Полный список (без обрезки до 10) — для кнопки «📥 Полный список (CSV)»."""
    import csv
    import io

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["Раздел", "telegram_id", "Имя", "Username", "Вкладка", "Доп. инфо"])
    for it in report.get("missing_tabs", []):
        note = f"ожидалось {it['count']} делегатов ({it['kind']})"
        if it.get("suggestion"):
            note += f"; похожая: {it['suggestion']}"
        writer.writerow(["Нет вкладки", "", "", "", it["tab"], note])
    for it in report.get("missing_rows", []):
        writer.writerow([
            "Нет строки", it["tid"], _csv_safe(it.get("name") or ""),
            _csv_safe(it.get("username") or ""), it["tab"] or "(главная)", "",
        ])
    for it in report.get("duplicate_rows", []):
        writer.writerow(["Дубль", it["tid"], "", "", it["tab"] or "(главная)", f"{it['count']} строк"])
    for it in report.get("status_mismatch", []):
        writer.writerow([
            "Статус", it["tid"], "", "", it["tab"] or "(главная)",
            f"в БД «{it['expected_label']}», в листе «{it['sheet_label']}»",
        ])
    for it in report.get("unknown_sheet_ids", []):
        writer.writerow(["Неизвестный id", it["tid"], "", "", it["tab"] or "(главная)", ""])
    for tab in report.get("headerless_tabs", []):
        writer.writerow(["Без заголовков", "", "", "", tab, ""])
    return output.getvalue().encode("utf-8-sig")
