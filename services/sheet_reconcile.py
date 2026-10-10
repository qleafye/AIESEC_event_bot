"""Phase 33 (delegate-card admin actions) — «🔍 Сверить с БД» (раздел «📊 Данные», рядом с
«🔄 Синхронизация»/«♻️ Пересобрать таблицу»). Кнопка на карточке делегата (Кристина Мухина,
33-SEED) заменена веб-путём «переведи её в Москву»; сама сверка живёт здесь.

Шаг 1 (`build_report`) — ТОЛЬКО ЧТЕНИЕ: сравнивает `users` ТЕКУЩЕГО сезона (`reg_engine.
is_past_season_row` — те же 482 импортированных делегата прошлого сезона иначе завалили бы
отчёт ложными «нет строки») с РЕАЛЬНЫМИ вкладками таблицы. Раскладка «пользователь → вкладка →
шапка → строка» — тот же единый строитель, что у «🔄 Синхронизация»/«♻️ Пересобрать таблицу»
(`handlers.admin_sheets.build_sheet_batches`, Phase 25/квик 260915-4is) — не изобретаем
собственное правило резолва вкладки, DRY и ноль риска разъехаться.

Координатор 25.09 (живой прогон на стенде): читает и разбирает по строкам ТОЛЬКО делегатские
вкладки (главная + именные из раскладки выше) — «Незавершённые»/служебные журналы («🤖
Автоотказы» и т.п.)/гейма (`_known_non_delegate_tab_titles`) в это чтение не попадают вовсе
(экономия квоты + честность: id там из `reg_started`/своих таблиц, не из `users`, «нет в БД» по
ним — ложное срабатывание). Реальная вкладка, которую бот не знает ни в одной категории —
отдельным списком `unknown_tabs`, без разбора строк.

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

import gspread

from config import config
from services import sheet_target as _sheet_target
from database.db import _csv_safe, get_all_users_dicts, get_all_users_ids, get_setting
from domain.settings.schema import get_setting_typed
from domain.regform.engine import is_past_season_row
from domain.regform.labels import STATUS_LABELS
from services.decision_delivery import summarize_deliveries
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

# Координатор 25.09: недоставленные решения (одобрение/отказ) теперь читаются из БД —
# `users.decision_delivery_*` (database/db.py, пишет services/application_effects.py),
# раскладка на категории — `services.decision_delivery.summarize_deliveries` (общая точка с
# «📨 Переотправить решения», handlers/admin_sheet_reconcile.py). Решения ДО этой миграции
# остаются NULL и попадают в отдельную категорию «неизвестно», не смешиваются с «не доставлено».


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


def _read_all_tabs_snapshot_sync(
    delegate_titles: frozenset[str],
) -> tuple[str | None, dict[str, list[list[str]]], set[str]]:
    """Листинг вкладок — ОДИН вызов `worksheets()` (метаданные, без данных). `get_all_values()`
    (реальный сетевой вызов на вкладку) — ТОЛЬКО для делегатских вкладок (`delegate_titles` —
    именные вкладки `build_sheet_batches` + главная, резолвится ниже). Координатор 25.09
    (живой прогон на стенде): «Незавершённые»/служебные журналы («🤖 Автоотказы» и т.п.)/гейма —
    НЕ делегатские вкладки, сверка их вообще не читает (лишняя квота на данные, которые она всё
    равно не умеет разобрать осмысленно — id там из `reg_started`, не из `users`). Никогда не
    создаёт: сперва пробует уже открытую/закешированную главную вкладку (`_get_sheet()`, тот же
    код, что `_all_worksheets_sync` уже использует для кросс-вкладочного поиска в
    `update_row_by_id`) — без лишнего сетевого похода, если она уже настроена и закеширована;
    при отказе (главная не настроена — RuntimeError, см. `_get_sheet()`'s docstring) открывает
    таблицу напрямую и берёт список вкладок без резолва главной, как `_all_worksheets_sync`'s
    собственный fallback.

    Возвращает `(main_title, values_by_title, all_real_titles)`. `main_title` — реальный
    заголовок главной вкладки, если она настроена и открылась, иначе `None`. `values_by_title` —
    ТОЛЬКО прочитанные (делегатские) вкладки — если строка делегата лежит на другой ДЕЛЕГАТСКОЙ
    вкладке (перевод города, переименование), она попадёт сюда под своим настоящим именем и
    будет учтена как «строка на другой вкладке»; строка на служебной/незавершённой вкладке
    сверкой не ищется вовсе (координатор 25.09: «поиск... только среди делегатских вкладок»).
    `all_real_titles` — имена ВСЕХ вкладок в таблице (метаданные, дёшево) — нужен, чтобы отличить
    «вкладка не существует» (`missing_tabs`) от «существует, но не делегатская» (пропускаем) и
    построить «Неизвестные вкладки» (реальная, не делегатская, не незавершённые/служебная)."""
    main_title: str | None = None
    try:
        main_ws = sheets_service._get_sheet()
        main_title = main_ws.title
        worksheets = main_ws.spreadsheet.worksheets()
    except Exception:
        gc = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
        sh = gc.open_by_key(_sheet_target.sheet_id())
        worksheets = sh.worksheets()
    all_real_titles = {ws.title for ws in worksheets}
    read_titles = set(delegate_titles) | ({main_title} if main_title else set())
    values_by_title = {
        ws.title: ws.get_all_values() for ws in worksheets if ws.title in read_titles
    }
    return main_title, values_by_title, all_real_titles


async def _read_all_tabs_snapshot(
    delegate_titles: frozenset[str],
) -> tuple[str | None, dict[str, list[list[str]]], set[str]] | None:
    """Fail-soft wrapper (тот же контракт, что `list_worksheet_titles`): `None`, если Sheets не
    настроен или запрос упал — вызывающий `build_report` тогда отдаёт `report["ok"] is False`."""
    if not _sheet_target.sheets_enabled():
        return None
    try:
        return await asyncio.to_thread(_read_all_tabs_snapshot_sync, delegate_titles)
    except Exception as e:
        logger.warning(f"sheet_reconcile: read_all_tabs_snapshot failed (treating as unavailable): {e}")
        return None


async def _known_non_delegate_tab_titles() -> set[str]:
    """Реестровые имена вкладок, которые бот ведёт, но НЕ как делегатские — координатор 25.09
    (живой прогон на стенде, находка «ложные "нет в БД" на служебных вкладках»):

    - «Незавершённые» (default + по городу, `handlers.registration.city_incomplete_tab`) —
      id там из `reg_started`, не из `users`; сверка их не разбирает вовсе (проще, чем городить
      второе сравнение — координатор явно разрешил «просто пропускать»).
    - служебные журналы (опросы/предотбор/история правок/вопросы делегатов/автоотказы) и
      гейма (матрица+история, default + по городу, `services.game_sheets.game_tab_plan`) —
      читаются и пишутся СВОИМ экраном/джобой, сверке в них смотреть незачем.

    Пустое значение настройки (например, выключенный `auto_reject_sheet_tab`) не добавляет
    строку — такой вкладки бот не ведёт вовсе, она либо не существует, либо это чья-то ЧУЖАЯ
    вкладка со случайно совпавшим именем (не наш случай)."""
    from cities import cities_module_on, enabled_cities
    from handlers.registration import city_incomplete_tab
    from services.game_sheets import game_tab_plan

    titles: set[str] = {await city_incomplete_tab(None)}
    if await cities_module_on():
        for city in await enabled_cities():
            titles.add(await city_incomplete_tab(city["code"]))

    for key in ("polls_sheet_tab", "preselect_tab", "history_sheet_tab", "questions_sheet_tab"):
        value = (await get_setting_typed(key) or "").strip()
        if value:
            titles.add(value)
    auto_reject_tab = (await get_setting("auto_reject_sheet_tab") or "").strip()
    if auto_reject_tab:
        titles.add(auto_reject_tab)

    for entry in await game_tab_plan():
        if entry.get("tab"):
            titles.add(entry["tab"])

    return titles


async def build_report(*, city_scope: tuple | None = None) -> dict:
    """Только чтение. `report["ok"] is False` — таблица недоступна (Sheets не настроен или
    сбой API до похода по вкладкам) — остальные списки в этом случае пустые, `report["error"]`
    называет причину. Пересчитывается заново на КАЖДЫЙ вызов (нет кеша) — и для самого отчёта,
    и как первый шаг обоих массовых исправлений ниже.

    Читает ТОЛЬКО делегатские вкладки (главная + именные `build_sheet_batches`) и строит индекс
    id → [вкладки, где найдена строка] — координатор 25.09 (живой прогон на стенде): служебные
    вкладки (незавершённые/журналы/гейма) в этот поиск и в чтение вовсе не попадают
    (`_known_non_delegate_tab_titles`), реальная вкладка, которую бот не знает ни в одной
    категории — отдельным списком `unknown_tabs`, без разбора строк. Делегат, чья строка
    отсутствует на СВОЕЙ ожидаемой вкладке, но нашлась на ДРУГОЙ ДЕЛЕГАТСКОЙ — не попадает в
    `missing_rows` (иначе «Дописать недостающие строки» завела бы вторую строку рядом с уже
    существующей), а идёт в отдельную категорию `other_tab_rows`."""
    users = await _current_season_users(city_scope=city_scope)
    from handlers.admin_sheets import build_sheet_batches  # ленивый импорт против цикла

    batches = await build_sheet_batches(users)
    main_batch, named_batches = batches[0], batches[1:]
    named_titles = frozenset(b.tab for b in named_batches)

    # Координатор 25.09: раскладка доставки решения — над `users` из БД, от таблицы не зависит
    # вовсе, поэтому считаем ДО обращения к Sheets (снимок ниже может упасть, а этот раздел
    # отчёта должен остаться доступен и тогда).
    decision_delivery = summarize_deliveries(users)
    non_delegate_titles = await _known_non_delegate_tab_titles()

    snapshot = await _read_all_tabs_snapshot(named_titles)
    report: dict = {
        "ok": snapshot is not None,
        "error": None,
        "user_count": len(users),
        "missing_tabs": [],
        "missing_rows": [],
        "other_tab_rows": [],
        "duplicate_rows": [],
        "status_mismatch": [],
        "unknown_sheet_ids": [],
        "headerless_tabs": [],
        "unknown_tabs": [],
        "decision_delivery": decision_delivery,
    }
    if snapshot is None:
        report["error"] = "таблица недоступна (Google Sheets не настроен или ошибка API)"
        return report

    main_title, values_by_title, all_real_titles = snapshot
    all_db_ids = set(await get_all_users_ids())

    for batch in named_batches:
        if batch.tab not in all_real_titles:
            suggestion = difflib.get_close_matches(batch.tab, list(all_real_titles), n=1)
            report["missing_tabs"].append({
                "tab": batch.tab, "kind": batch.kind, "count": len(batch.users),
                "suggestion": suggestion[0] if suggestion else None,
            })

    delegate_titles = named_titles | ({main_title} if main_title else set())
    report["unknown_tabs"] = sorted(all_real_titles - delegate_titles - non_delegate_titles)

    id_to_user = {u["telegram_id"]: u for u in users}

    # Проход 1 — по КАЖДОЙ прочитанной вкладке (только делегатские, снимком выше): парсит
    # шапку/данные, копит duplicate_rows/unknown_sheet_ids/headerless_tabs (по всем ДЕЛЕГАТСКИМ
    # вкладкам, не только по ожидаемой раскладке — делегат мог оказаться на чужой делегатской
    # вкладке) и строит id-индекс для прохода 2. Служебные/незавершённые/гейма сюда не попадают
    # вовсе — координатор 25.09: поиск «строка на другой вкладке» и дубли — только среди
    # делегатских.
    rows_by_title: dict[str, dict[str, list[list]]] = {}
    status_col_by_title: dict[str, int] = {}
    id_index: dict[str, list[str]] = {}

    for title, values in values_by_title.items():
        if not values:
            rows_by_title[title] = {}
            status_col_by_title[title] = -1
            continue

        # Та же эвристика, что `services/sheets.py::_ensure_header_sync` уже применяет при
        # живом аппенде: первая ячейка — число (telegram_id) -> заголовка нет, это данные
        # (инцидент 13.09: «СПб Акция» без заголовков, lost-applications-260914).
        first_cell = (values[0][0] if values[0] else "").strip()
        headerless = bool(first_cell) and first_cell.lstrip("-").isdigit()
        if headerless:
            report["headerless_tabs"].append(title)
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
        rows_by_title[title] = rows_by_id
        status_col_by_title[title] = status_col

        dup_ids = {rid for rid, rows in rows_by_id.items() if len(rows) > 1 and rid.lstrip("-").isdigit()}
        for rid in sorted(dup_ids, key=int):
            report["duplicate_rows"].append({"tid": int(rid), "tab": title, "count": len(rows_by_id[rid])})

        for rid in rows_by_id:
            if not rid.lstrip("-").isdigit():
                continue
            id_index.setdefault(rid, []).append(title)
            tid = int(rid)
            if tid not in all_db_ids:
                report["unknown_sheet_ids"].append({"tid": tid, "tab": title})

    # Проход 2 — раскладка «делегат → своя ожидаемая вкладка»: статус сверяем только на своей
    # вкладке (дубль на своей — не сверяем, неясно, какая строка живая, тот же посыл, что и
    # раньше); своей строки нет — сначала смотрим id-индекс (вдруг она на ДРУГОЙ реальной
    # вкладке), и только если нигде — «нет строки». Своя вкладка вовсе не существует (уже в
    # missing_tabs) и нигде не найдена — делегат НЕ дублируется в missing_rows построчно
    # (аггрегата missing_tabs достаточно, «Дописать» туда всё равно не пишет).
    for tab_repr, batch in [(None, main_batch)] + [(b.tab, b) for b in named_batches]:
        own_title = main_title if tab_repr is None else (tab_repr if tab_repr in values_by_title else None)
        own_rows = rows_by_title.get(own_title, {}) if own_title is not None else {}
        own_dup_ids = {
            rid for rid, rows in own_rows.items() if len(rows) > 1 and rid.lstrip("-").isdigit()
        }
        status_col = status_col_by_title.get(own_title, -1) if own_title is not None else -1

        expected_ids = {str(u["telegram_id"]) for u in batch.users}
        for tid_str in sorted(expected_ids, key=int):
            u = id_to_user[int(tid_str)]
            rows = own_rows.get(tid_str)
            if rows:
                if tid_str in own_dup_ids:
                    continue  # уже в duplicate_rows — статус не сверяем
                if status_col >= 0:
                    row0 = rows[0]
                    sheet_label = (row0[status_col] if status_col < len(row0) else "").strip()
                    expected_label = STATUS_LABELS.get(u.get("status") or "pending")
                    if expected_label and sheet_label and sheet_label != expected_label:
                        report["status_mismatch"].append({
                            "tid": int(tid_str), "tab": tab_repr,
                            "expected_label": expected_label, "sheet_label": sheet_label,
                        })
                continue

            found = [t for t in id_index.get(tid_str, []) if t != own_title]
            if found:
                report["other_tab_rows"].append({
                    "tid": int(tid_str), "name": u.get("full_name"), "username": u.get("username"),
                    "own_tab": tab_repr, "found_tabs": found,
                })
            elif own_title is not None:
                report["missing_rows"].append({
                    "tid": int(tid_str), "name": u.get("full_name"), "username": u.get("username"),
                    "tab": tab_repr,
                })

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
    `done`/`failed` (причина словами на каждую).

    Неожиданный сбой (таблица не ответила, БД упала) ловится ЗДЕСЬ, а не только в хендлере —
    иначе счётчик `done`, накопленный до сбоя, терялся бы вместе с исключением, и хендлер не
    смог бы сказать «записано N из M (что успели)», только «упало». Возврат в этом случае несёт
    `ok=False`, `crashed=True` и уже накопленные `done`/`failed`/`total`."""
    key = f"append:{_scope_key(city_scope)}"
    if not _claim(key):
        return {"ok": False, "error": "уже выполняется — подождите завершения предыдущего запуска"}
    done = 0
    failed: list[dict] = []
    total = 0
    try:
        report = await build_report(city_scope=city_scope)
        if not report["ok"]:
            return {"ok": False, "error": report["error"] or "таблица недоступна"}
        if not report["missing_rows"]:
            return {"ok": True, "done": 0, "failed": [], "total": 0}
        total = len(report["missing_rows"])

        users = await _current_season_users(city_scope=city_scope)
        from handlers.admin_sheets import build_sheet_batches  # ленивый импорт против цикла

        batches = await build_sheet_batches(users)
        main_batch, named_batches = batches[0], batches[1:]
        batch_by_tab = {None: main_batch, **{b.tab: b for b in named_batches}}

        missing_by_tab: dict[str | None, list[dict]] = {}
        for item in report["missing_rows"]:
            missing_by_tab.setdefault(item["tab"], []).append(item)

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
        return {"ok": True, "done": done, "failed": failed, "total": total}
    except Exception as e:
        logger.error(f"apply_append_missing: неожиданный сбой после done={done}/{total}: {e}")
        return {
            "ok": False, "crashed": True, "error": "таблица не ответила",
            "done": done, "failed": failed, "total": total,
        }
    finally:
        _release(key)


async def apply_fix_statuses(*, city_scope: tuple | None = None) -> dict:
    """Правит статус ТОЛЬКО там, где на вкладке РОВНО одна строка делегата (дубли — только
    показываем, руками) — тем же путём, что решение модератора (`services.sheets::
    update_status_in_sheet`). По одному с паузой; сбой одного не прерывает остальные.

    Неожиданный сбой ловится ЗДЕСЬ же (см. `apply_append_missing`'s докстринг — тот же посыл):
    возврат несёт `ok=False`, `crashed=True` и уже накопленные `done`/`failed`/`total`, чтобы
    хендлер мог сказать «записано N из M», а не просто «упало»."""
    key = f"status:{_scope_key(city_scope)}"
    if not _claim(key):
        return {"ok": False, "error": "уже выполняется — подождите завершения предыдущего запуска"}
    done = 0
    failed: list[dict] = []
    total = 0
    try:
        report = await build_report(city_scope=city_scope)
        if not report["ok"]:
            return {"ok": False, "error": report["error"] or "таблица недоступна"}
        if not report["status_mismatch"]:
            return {"ok": True, "done": 0, "failed": [], "total": 0}
        total = len(report["status_mismatch"])

        for item in report["status_mismatch"]:
            ok = await sheets_service.update_status_in_sheet(item["tid"], item["expected_label"])
            if ok:
                done += 1
            else:
                failed.append({**item, "reason": "строка не найдена при записи"})
            await asyncio.sleep(_STATUS_PAUSE_S)
        return {"ok": True, "done": done, "failed": failed, "total": total}
    except Exception as e:
        logger.error(f"apply_fix_statuses: неожиданный сбой после done={done}/{total}: {e}")
        return {
            "ok": False, "crashed": True, "error": "таблица не ответила",
            "done": done, "failed": failed, "total": total,
        }
    finally:
        _release(key)


# ── Рендер отчёта человеческими словами + CSV ────────────────────────────────────────────────

def chunk_report_lines(lines: list[str], limit: int = 4096) -> list[str]:
    """Режет готовые (уже HTML-эскейпленные) строки на части ≤`limit` символов, никогда не
    разрывая строку пополам — граница чанка всегда между строками. Не изобретаем свой резчик:
    `moderation_card.split_for_telegram` уже делает это (и уже покрыт тестами) для «📄 Полная
    анкета» (handlers/admin_moderation.py::appr_full) — тот же класс задачи."""
    from domain.regform.moderation_card import split_for_telegram

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

    if report.get("unknown_tabs"):
        lines.append("❔ <b>Неизвестные вкладки</b> (бот не ведёт их ни в одной категории, не разбираем):")
        for tab in report["unknown_tabs"]:
            lines.append(f"  «{html_module.escape(str(tab))}»")
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

    if report["other_tab_rows"]:
        shown, extra = _first_n(report["other_tab_rows"])
        lines.append(f"🧭 <b>Строка не на своей вкладке</b> ({len(report['other_tab_rows'])}):")
        for it in shown:
            name = html_module.escape(str(it.get("name") or it["tid"]))
            username = it.get("username") or "-"
            own = html_module.escape(str(it["own_tab"] if it["own_tab"] is not None else "главная"))
            found = ", ".join(f"«{html_module.escape(t)}»" for t in it["found_tabs"])
            lines.append(f"  {name} ({html_module.escape(username)}): своя «{own}», нашлась на {found}")
        if extra:
            lines.append(f"  …и ещё {extra}")
        lines.append(
            "  Дописывать сюда не будем — перенесите строку в таблице руками или переведите "
            "делегата в нужный город через /find."
        )
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

    dd = report.get("decision_delivery") or {"failed": [], "blocked": [], "queued": [], "unknown": []}
    failed, blocked, queued, unknown = dd["failed"], dd["blocked"], dd["queued"], dd["unknown"]
    lines.append(
        f"📨 <b>Решения не доставлены</b> ({len(failed)}, из них бот заблокирован: {len(blocked)}):"
    )
    if failed:
        shown, extra = _first_n(failed)
        for it in shown:
            name = html_module.escape(str(it.get("name") or it["tid"]))
            username = it.get("username") or "-"
            decision_label = "одобрение" if it["decision"] == "approved" else "отказ"
            reason = html_module.escape(str(it.get("error") or "-"))
            lines.append(f"  {name} ({html_module.escape(username)}): {decision_label} — {reason}")
        if extra:
            lines.append(f"  …и ещё {extra}")
    if queued:
        lines.append(f"  в очереди (тихие часы, доставится само): {len(queued)}")
    lines.append(f"  неизвестно (до учёта доставки): {len(unknown)}")

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
    for it in report.get("other_tab_rows", []):
        writer.writerow([
            "Другая вкладка", it["tid"], _csv_safe(it.get("name") or ""),
            _csv_safe(it.get("username") or ""), it["own_tab"] or "(главная)",
            "нашлась на: " + ", ".join(it["found_tabs"]),
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
    for tab in report.get("unknown_tabs", []):
        writer.writerow(["Неизвестная вкладка", "", "", "", tab, "бот не ведёт её ни в одной категории"])

    dd = report.get("decision_delivery") or {}
    decision_label = {"approved": "одобрение", "rejected": "отказ"}
    for it in dd.get("resendable", []):
        writer.writerow([
            "Решение не доставлено", it["tid"], _csv_safe(it.get("name") or ""),
            _csv_safe(it.get("username") or ""), it.get("city") or "",
            f"{decision_label.get(it['decision'], it['decision'])}: {it.get('error') or '-'}",
        ])
    for it in dd.get("blocked", []):
        writer.writerow([
            "Решение — написать вручную (бот заблокирован)", it["tid"], _csv_safe(it.get("name") or ""),
            _csv_safe(it.get("username") or ""), it.get("city") or "",
            f"{decision_label.get(it['decision'], it['decision'])}: {it.get('error') or '-'}",
        ])
    for it in dd.get("unknown", []):
        writer.writerow([
            "Решение — доставка неизвестна", it["tid"], _csv_safe(it.get("name") or ""),
            _csv_safe(it.get("username") or ""), it.get("city") or "",
            decision_label.get(it["decision"], it["decision"]),
        ])
    return output.getvalue().encode("utf-8-sig")
