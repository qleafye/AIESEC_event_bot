"""Module-size split из `handlers/admin_settings.py` (module-size convention,
tests/test_module_size_convention_260816.py): «🔄 Синхронизация» и «♻️ Пересобрать таблицу» —
единственные два экрана, что переписывают/дозаписывают ВСЮ Google-таблицу по вкладкам
(Phase 25, CITYQ-03: набор колонок и снимок схемы считаются один раз на код города, не на
вкладку и не на строку — commits 65a907d/7ce53b9/b7ebd85). Регистрируется на ТОТ ЖЕ общий
`admin.router`, что и остальные швы, импортируется из `handlers/admin.py` сразу после
`admin_settings` — эти два хендлера были последними, что физически трогают Sheets-схему в
исходном файле, дальше в нём шли CSV-экспорт/«Незавершённые» (остаются в admin_settings.py:
они читают/дозаписывают одну вкладку за раз, а не пересобирают всю таблицу — другая природа
операции, разрез не затрагивает).
"""
import html as html_module
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_all_users_dicts
from services.sheets import (
    get_existing_sheet_ids,
    append_rows_to_sheet,
    ensure_sheet_header,
    sync_named_worksheet,
    rebuild_main_sheet,
    REFUSED_UNPINNED_TAB,
    ensure_named_sheet_header,
    get_existing_named_sheet_ids,
    append_rows_to_named_sheet,
)
from handlers.reg_schema import (
    active_sheet_headers,
    set_sheet_schema,
    _sheet_value_map,
    city_row_tab,
    sheet_city_code,
)
from handlers.admin import router

logger = logging.getLogger(__name__)

# INVARIANT (13-01 cap-test, extended by 13-04 to scan every handlers/admin*.py file): every
# `@router.*` decorator below MUST fit on ONE line.

@router.callback_query(F.data == "admin_sync_sheet")
async def sync_sheet(callback: types.CallbackQuery):
    """UAT 25.08 (prod snapshot): sync used to always dozapisyvat missing delegates into the
    MAIN tab, even for delegates whose city routes them to a named tab — main tab («МСК») had
    46 rows that actually belonged to СПб/Тюмень. Routes each user through the SAME resolver the
    live append and rebuild_sheet use (city_row_tab), then appends missing rows per-tab, one
    try/except per city tab so a single tab failure never cancels the rest."""
    from handlers.admin_sections import op_return_keyboard  # ленивый шов
    await callback.answer("🔄 Синхронизация...")
    await callback.message.edit_text("🔄 Получаю данные из таблицы...", parse_mode="HTML")

    try:
        headers = await active_sheet_headers()  # only enabled columns, main tab (code=None)
        all_users = await get_all_users_dicts()

        # Phase 25 (CITYQ-03): each city tab gets its OWN column set (headers_by_code cache,
        # one active_sheet_headers(code) call per DISTINCT code — T-25-10 quota). The frozen
        # snapshot is deliberately NOT touched here (sync appends, it never rewrites/re-freezes
        # a tab's schema — that stays rebuild_sheet's job).
        headers_by_code: dict[str | None, list[str]] = {None: headers}
        main_users: list[dict] = []
        city_users: dict[str, list[dict]] = {}
        tab_code: dict[str, str] = {}
        for u in all_users:
            code = await sheet_city_code(u.get("event_city"))
            if code not in headers_by_code:
                headers_by_code[code] = await active_sheet_headers(code)
            tab = await city_row_tab(u.get("event_city"), u.get("participant_type"))
            if tab is None:
                main_users.append(u)
            else:
                city_users.setdefault(tab, []).append(u)
                tab_code[tab] = code

        # Основная вкладка — прежний порядок шагов, только теперь на своём подмножестве
        # пользователей (module off / нет городов -> main_users == all_users, поведение прежнее).
        await ensure_sheet_header(headers)  # шапка таблицы, если её ещё нет
        existing_ids = await get_existing_sheet_ids()
        main_missing = [u for u in main_users if u["telegram_id"] not in existing_ids]
        main_count = 0
        if main_missing:
            main_rows = [[_sheet_value_map(u).get(h, "-") for h in headers] for u in main_missing]
            main_count = await append_rows_to_sheet(main_rows)

        # Городские вкладки — каждая в своём try/except: одна упавшая не отменяет остальные.
        city_synced: list[tuple[str, int]] = []
        failed_tabs: list[str] = []
        for tab, trows in city_users.items():
            city_headers = headers_by_code[tab_code[tab]]
            try:
                # Шапка ДО чтения id: чтение отбрасывает первую строку как шапку, и на вкладке
                # без шапки первый делегат выпал бы из набора существующих и продублировался.
                await ensure_named_sheet_header(tab, city_headers)
                ids = await get_existing_named_sheet_ids(tab)
                if ids is None:
                    failed_tabs.append(tab)
                    continue
                missing_named = [u for u in trows if u["telegram_id"] not in ids]
                if not missing_named:
                    city_synced.append((tab, 0))
                    continue
                rows = [[_sheet_value_map(u).get(h, "-") for h in city_headers] for u in missing_named]
                n = await append_rows_to_named_sheet(tab, rows)
                if n < 0:
                    failed_tabs.append(tab)
                else:
                    city_synced.append((tab, n))
            except Exception as e:
                logger.warning(f"sync_sheet: city tab {tab!r} failed: {e}")
                failed_tabs.append(tab)

        total_count = main_count + sum(n for _tab, n in city_synced)
        # WR-06: только счётчики по вкладкам и id админа — никаких строк данных в логе.
        logger.info(
            f"admin={callback.from_user.id} action=sync_sheet main_added={main_count} "
            f"city_tabs={len(city_users)} city_added={total_count - main_count} "
            f"failed_tabs={len(failed_tabs)}"
        )

        if not city_users:
            # Модуль городов выключен (или у всех делегатов основная вкладка) — байт-в-байт
            # прежнее поведение и прежний текст.
            if not main_missing:
                await callback.message.edit_text(
                    "✅ Таблица синхронизирована, пропущенных записей нет.",
                    parse_mode="HTML",
                    reply_markup=await op_return_keyboard(callback.from_user.id, callback.data),
                )
                return
            await callback.message.edit_text(
                f"✅ Синхронизация завершена!\n\n"
                f"Добавлено записей: <b>{main_count}</b>",
                parse_mode="HTML",
                reply_markup=await op_return_keyboard(callback.from_user.id, callback.data),
            )
            return

        lines = [
            "✅ Синхронизация завершена!",
            "",
            f"Добавлено записей: <b>{total_count}</b>",
            f"Основная вкладка: <b>{main_count}</b>",
        ]
        for tab, n in city_synced:
            lines.append(f"{html_module.escape(tab)}: <b>{n}</b>")
        for tab in failed_tabs:
            lines.append(f"{html_module.escape(tab)}: <b>❌</b>")
        if failed_tabs:
            names = ", ".join(html_module.escape(t) for t in failed_tabs)
            lines.append(
                f"⚠️ Не удалось обновить вкладки: {names}. Остальное записано, "
                f"попробуйте нажать «Синхронизация» ещё раз."
            )

        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=await op_return_keyboard(callback.from_user.id, callback.data),
        )
    except Exception as e:
        logger.error(f"Sheet sync failed: {e}")
        await callback.message.edit_text(
            f"❌ Ошибка синхронизации:\n<code>{html_module.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=await op_return_keyboard(callback.from_user.id, callback.data),
        )


@router.callback_query(F.data == "admin_rebuild_sheet")
async def rebuild_sheet_confirm(callback: types.CallbackQuery):
    """Quick 260813-sdl: пересборка делает sheet.clear() и перезаписывает ВСЕ строки — то есть
    сносит любые ручные правки менеджеров на листе. До этого она запускалась одним тапом, без
    вопроса; соседняя destructive-кнопка «🧹 Убрать дубли» подтверждение имела всегда. Гейт
    зеркалит dedupe: сама работа переехала в admin_rebuild_sheet_go."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♻️ Да, пересобрать", callback_data="admin_rebuild_sheet_go")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_menu")],
    ])
    await callback.message.edit_text(
        "♻️ <b>Пересобрать таблицу?</b>\n\n"
        "Перезапишу на основной вкладке <b>шапку и все строки</b> из базы бота: колонки "
        "встанут в порядке анкеты, «Статус» получит выпадашку и цвета.\n\n"
        "⚠️ Лист очищается целиком и заполняется заново. <b>Любые ручные правки и заметки, "
        "которых нет в базе бота, пропадут безвозвратно.</b> Если менеджеры что-то дописывали "
        "прямо в таблице — сначала сохраните копию (Файл → Создать копию).",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "admin_rebuild_sheet_go")
async def rebuild_sheet(callback: types.CallbackQuery):
    """Полная пересборка листа данных: перезаписать шапку + ВСЕ строки в текущем порядке
    колонок, применить выпадашку/цвета к «Статус». Выравнивает старые строки после смены
    порядка колонок (Таня п.1/п.5). Внимание: перезаписывает ручные правки на листе."""
    # Гейт подтверждения дал callback «…_go», в разделе объявлена сама кнопка — называем её.
    from handlers.admin_sections import op_return_keyboard  # ленивый шов
    await callback.answer("♻️ Пересборка...")
    logger.info(f"admin={callback.from_user.id} action=rebuild_sheet start")
    await callback.message.edit_text("♻️ Пересобираю таблицу (перезапись всех строк)…", parse_mode="HTML")

    try:
        main_headers = await active_sheet_headers()  # only enabled columns, main tab (code=None)
        all_users = await get_all_users_dicts()
        # UAT 17.08 (fast): the rebuild used to dump EVERY user into the main tab regardless of
        # city, so after one «Пересобрать» the main tab held СПб/Тюмень rows too while live
        # appends kept routing them to their own tabs -- the sheets drifted apart. Route each
        # row through the SAME resolver the live append uses (city_row_tab: default city / module
        # off -> None -> main tab; other city -> its named tab) and full-refresh every touched
        # city tab alongside the main one. Module off => city_row_tab is always None => byte-
        # identical to the old behaviour.
        #
        # Phase 25 (CITYQ-03): headers are no longer ONE list shared by every tab — each city
        # gets its OWN column set (`active_sheet_headers(code)`), computed exactly once per
        # DISTINCT code (headers_by_code cache — T-25-10, Google Sheets quota). `code` is
        # deliberately NOT threaded through participant_type: a city's party/short delegates
        # still land in a named party/short tab (via city_row_tab) but that tab's columns come
        # from the SAME full-track set as the city's main tab — pre-existing scope gap (rebuild/
        # sync have always been track-blind for city tabs), not something this plan fixes.
        headers_by_code: dict[str | None, list[str]] = {None: main_headers}
        main_rows: list[list] = []
        city_rows: dict[str, list[list]] = {}
        tab_code: dict[str, str] = {}
        for u in all_users:
            code = await sheet_city_code(u.get("event_city"))
            if code not in headers_by_code:
                headers_by_code[code] = await active_sheet_headers(code)
            headers = headers_by_code[code]
            row = [_sheet_value_map(u).get(h, "-") for h in headers]
            tab = await city_row_tab(u.get("event_city"), u.get("participant_type"))
            if tab is None:
                main_rows.append(row)
            else:
                city_rows.setdefault(tab, []).append(row)
                tab_code[tab] = code
        rows = main_rows
        count = await rebuild_main_sheet(main_headers, rows)
        city_synced: list[tuple[str, int, int]] = []
        if count >= 0:
            for tab, trows in city_rows.items():
                city_headers = headers_by_code[tab_code[tab]]
                n = await sync_named_worksheet(tab, city_headers, trows)
                city_synced.append((tab, n, len(city_headers)))
                if n >= 0:
                    # Снимок вкладки морозим ТОЛЬКО после успешной записи — неудачная
                    # sync_named_worksheet не должна заморозить снимок под физическую шапку,
                    # которая на лист так и не легла.
                    await set_sheet_schema(city_headers, tab_code[tab])
        if count == REFUSED_UNPINNED_TAB:
            await callback.message.edit_text(
                "⛔ Пересборка отключена: основная вкладка не задана.\n\n"
                "Без неё пересборка могла бы задеть не ту вкладку. Укажите вкладку в "
                "«⚙️ Настройки → 📄 Вкладки таблицы → 📄 Основная (регистрации)» — сработает "
                "сразу, без перезапуска. Вариант для разработчика — <code>GOOGLE_SHEET_TAB</code> "
                "в .env (тогда нужен перезапуск).",
                parse_mode="HTML",
                reply_markup=await op_return_keyboard(callback.from_user.id, "admin_rebuild_sheet"),
            )
            return
        if count < 0:
            await callback.message.edit_text(
                "❌ Пересборка не выполнена (таблица не настроена или ошибка API). Смотри логи.",
                parse_mode="HTML",
                reply_markup=await op_return_keyboard(callback.from_user.id, "admin_rebuild_sheet"),
            )
            return
        # CR-9: rebuild is the re-sync point — freeze the snapshot to the header just written
        # so subsequent registrations align to the rebuilt physical header.
        await set_sheet_schema(main_headers)
        city_line = ""
        if city_synced:
            parts = [
                f"{html_module.escape(t)}: <b>{n if n >= 0 else '❌'}</b> ({cols} кол.)"
                for t, n, cols in city_synced
            ]
            city_line = "Городские вкладки: " + ", ".join(parts) + "\n"
        await callback.message.edit_text(
            f"✅ Таблица пересобрана!\n\n"
            f"Строк записано (основная): <b>{count}</b> ({len(main_headers)} кол.)\n"
            f"{city_line}"
            f"Колонки выстроены в порядке анкеты, «Статус» с выпадашкой и цветами.",
            parse_mode="HTML",
            reply_markup=await op_return_keyboard(callback.from_user.id, "admin_rebuild_sheet"),
        )
    except Exception as e:
        logger.error(f"Sheet rebuild failed: {e}")
        await callback.message.edit_text(
            f"❌ Ошибка пересборки:\n<code>{html_module.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=await op_return_keyboard(callback.from_user.id, "admin_rebuild_sheet"),
        )


