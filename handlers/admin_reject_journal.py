"""Phase 31 (31-11, D-18/D-19/D-20/D-29): журнал «🤖 Автоотказы» в чат-боте — кто, когда, по
какому правилу и сколько раз бот отклонил делегата; единственная дверь исключения из правила —
кнопка «↩️ Вернуть на модерацию» (D-06: «исключения — только через возврат из журнала»);
выгрузка журнала файлом для отчёта партнёрам (D-29); плюс вход в журнал с экрана правил.

Форма шва — та же, что `handlers/admin_app_list.py`/`handlers/admin_faq.py`: своего `Router()`
нет, хендлеры декорируют ОБЩИЙ `handlers.admin.router`; каждый декоратор — в одну строку
(инвариант cap-теста). `handlers.admin`/`handlers.admin_core`/`handlers.admin_caps` — на уровне
модуля (безопасно, цикла не создают — `handlers/admin_caps.py` явно не импортирует `handlers.
admin`/`handlers.admin_core`); `handlers.admin_sections` (`back_button`) — лениво внутри функции,
тот же приём, что у каждого соседнего шва этого раздела.

Запись — ТОЛЬКО через `services.reject_journal` (`journal_page`/`journal_entry_detail`/
`return_to_moderation`/`export_csv`) — единственная дверь мутации возврата; в этом файле нет ни
одной прямой записи полей делегата в обход сервиса (акцептанс-тест плана держит это grep'ом).

Экран журнала (и его callback'и `arj_*`) зарегистрирован в `ADMIN_CAPS` на `"moderate_reg"`
(журнал и возврат — работа модератора, не настройщика), хотя ВХОД в него виден с экрана правил
«🚫 Правила автоотказа» (`admin_reject_rules`), открытого под `"settings"`. Каждый хендлер этого
файла перепроверяет `has_capability(admin_id, "moderate_reg")` САМ (T-31-11-01) — переход с
чужого правом экрана не имеет права быть единственной защитой, а прямой вызов хендлера в тестах
вообще не проходит через `CapabilityMiddleware`.

ЗАПРЕТ, который держит тест-сторож (D-19, инцидент 06.09 — тихое массовое автоодобрение): на
экране журнала и на экране правил НЕТ и не может появиться кнопки, применяющей правила/возврат
КО ВСЕЙ очереди — каждое действие этого шва адресовано РОВНО одной заявке по её `entry_id`.
`tests/test_reject_rules_journal_ui.py` перебирает подписи кнопок обоих экранов и требует
отсутствия формулировок про применение к очереди.
"""
import html as html_module
import logging

from aiogram import F, types
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_setting
from handlers.admin import router
from handlers.admin_caps import has_capability
from handlers.admin_core import _admin_city_view
from services import quiet_hours
from services.reject_journal import (
    JOURNAL_PAGE,
    export_csv,
    journal_entry_detail,
    journal_line,
    journal_page,
    return_to_moderation,
)
from services.scheduler import _now_moscow_naive
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

_NO_ACCESS = "Недостаточно прав"

# Дефолт текста делегату при возврате — тот же приём, что `reject_text`/`services.applications.
# reject_message_text` (план 31-03/31-11): пустой ключ реестра не значит «ничего не отправлять».
DEFAULT_RETURN_TEXT = "Ваша заявка возвращена на обычную модерацию — её пересмотрит менеджер."


def _parse_page_data(data: str) -> tuple[bool, int]:
    """`arj_p:{include_returned}:{offset}` -> (include_returned, offset). Плановая нотация
    интерфейса (`arj_p:{offset}`) несла бы только оффсет и теряла бы фильтр «показывать
    возвращённые» при каждом перелистывании страницы — тот же класс бага, что решает `offset`
    в `apl:{status}:{offset}` (`handlers/admin_app_list.py`): состояние экрана целиком живёт в
    `callback_data`, FSM для одного флага не заводится. Не влияет на acceptance-однострочник
    плана (`arj_p:0` без второго `:` резолвит capability тем же префиксным совпадением
    `"arj_*"` — сама схема callback'а этот тест не парсит)."""
    parts = data.split(":", 2)
    include_returned = len(parts) >= 2 and parts[1] == "1"
    offset = 0
    if len(parts) >= 3:
        try:
            offset = int(parts[2])
        except ValueError:
            offset = 0
    return include_returned, max(0, offset)


def _parse_id(data: str) -> int | None:
    try:
        return int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


async def render_journal_screen(admin_id: int, offset: int = 0, include_returned: bool = False
                                 ) -> tuple[str, InlineKeyboardMarkup]:
    """(text, kb) — та же идиома, что `render_app_list_screen`/`render_faq_screen`. Счётчик
    «Всего: N» и строки списка идут из ОДНОГО вызова `journal_page` (тот же принцип, что у
    `services.applications.queue_page`) — второго запроса ради счётчика нет."""
    _scope, label = await _admin_city_view(admin_id)
    rows, total = await journal_page(admin_id, offset=offset, include_returned=include_returned)
    module_on = await get_setting_typed("reject_rules_enabled")

    lines = ["🤖 <b>Автоотказы</b>"]
    if label:
        lines.append(html_module.escape(str(label)))
    lines.append(f"Всего: {total}")
    total_pages = max(1, (total + JOURNAL_PAGE - 1) // JOURNAL_PAGE)
    current_page = offset // JOURNAL_PAGE + 1
    lines.append(f"Страница {current_page} из {total_pages}")
    lines.append("")

    if not rows:
        if not module_on:
            lines.append(
                "Правила пока никого не отклонили — модуль автоотказа сейчас выключен."
            )
        else:
            lines.append("Правила пока никого не отклонили.")
    else:
        for idx, row in enumerate(rows, start=offset + 1):
            lines.append(f"{idx}. {journal_line(row)}")

    text = "\n".join(lines)

    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        # Живая по БД строка, чей делегат уже не `rejected` (сам поправил анкету/решение
        # принял человек, план 31-11, orchestrator finding 2) — тексту уже некого возвращать,
        # кнопки под ней нет (сама причина закрытия уже напечатана `journal_line` выше).
        if row.get("returned_to_moderation_at") or row.get("stale_reason"):
            continue
        name = html_module.escape(str(row.get("full_name") or "") or "делегат")
        short_name = name if len(name) <= 30 else name[:29] + "…"
        buttons.append([InlineKeyboardButton(
            text=f"↩️ Вернуть: {short_name}", callback_data=f"arj_back:{row['id']}",
        )])

    toggle_icon = "✅" if include_returned else "☐"
    buttons.append([InlineKeyboardButton(
        text=f"{toggle_icon} Показывать возвращённые",
        callback_data=f"arj_all:{0 if include_returned else 1}",
    )])

    flag = 1 if include_returned else 0
    nav_row: list[InlineKeyboardButton] = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(
            text="⬅️", callback_data=f"arj_p:{flag}:{max(0, offset - JOURNAL_PAGE)}",
        ))
    if offset + JOURNAL_PAGE < total:
        nav_row.append(InlineKeyboardButton(
            text="➡️", callback_data=f"arj_p:{flag}:{offset + JOURNAL_PAGE}",
        ))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="📄 Выгрузить файлом", callback_data="arj_csv")])

    from handlers.admin_sections import back_button  # ленивый шов: цикл на уровне модуля
    buttons.append([back_button("admin_reject_journal")])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_reject_journal")
async def admin_reject_journal_open(callback: types.CallbackQuery):
    if not await has_capability(callback.from_user.id, "moderate_reg"):
        await callback.answer(_NO_ACCESS, show_alert=True)
        return
    text, kb = await render_journal_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arj_p:"))
async def arj_page(callback: types.CallbackQuery):
    if not await has_capability(callback.from_user.id, "moderate_reg"):
        await callback.answer(_NO_ACCESS, show_alert=True)
        return
    include_returned, offset = _parse_page_data(callback.data)
    text, kb = await render_journal_screen(
        callback.from_user.id, offset=offset, include_returned=include_returned,
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arj_all:"))
async def arj_toggle_returned(callback: types.CallbackQuery):
    if not await has_capability(callback.from_user.id, "moderate_reg"):
        await callback.answer(_NO_ACCESS, show_alert=True)
        return
    include_returned = callback.data.split(":", 1)[1] == "1"
    text, kb = await render_journal_screen(callback.from_user.id, include_returned=include_returned)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arj_back:"))
async def arj_back_confirm(callback: types.CallbackQuery):
    """Экран подтверждения (CLAUDE.md — разрушительная/необратимая-для-очереди операция
    называет последствия ДО совершения): делегат, правило, число попыток и ТОЧНЫЙ текст,
    который делегат получит."""
    if not await has_capability(callback.from_user.id, "moderate_reg"):
        await callback.answer(_NO_ACCESS, show_alert=True)
        return
    entry_id = _parse_id(callback.data)
    if entry_id is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    entry, error = await journal_entry_detail(callback.from_user.id, entry_id)
    if entry is None:
        await callback.answer(error or "Запись недоступна — обновите список.", show_alert=True)
        return
    if not entry.get("returnable"):
        # Стейл-клавиатура (orchestrator finding 2): запись уже вернули или делегат сам вышел
        # из rejected — дружелюбный алерт вместо экрана подтверждения над закрытой записью.
        await callback.answer(
            "Эта заявка уже не ждёт возврата — список обновлён.", show_alert=True,
        )
        text, kb = await render_journal_screen(callback.from_user.id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return

    name = html_module.escape(str(entry.get("full_name") or "") or "делегат")
    rule = entry.get("rule_display") or "—"
    attempts = entry.get("attempt_count") or 0
    raw_return_text = await get_setting("reject_rules_return_text") or DEFAULT_RETURN_TEXT
    text = (
        "↩️ <b>Вернуть заявку на модерацию?</b>\n\n"
        f"{name} — попыток автоотказа: {attempts}\n"
        f"Правило: «{rule}»\n\n"
        "Заявка вернётся в очередь на модерацию, делегат получит сообщение:\n"
        f"«{html_module.escape(raw_return_text)}»"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Да, вернуть", callback_data=f"arj_backgo:{entry_id}")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_reject_journal")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("arj_backgo:"))
async def arj_back_go(callback: types.CallbackQuery):
    """Единственная дверь мутации — `services.reject_journal.return_to_moderation` (атомарный
    `claim`, второй тап проигрывает). Сбой отправки делегату — только в лог, возврат НЕ
    откатывается (T-31-11-04): заявка обязана вернуться в очередь даже если делегат заблокировал
    бота."""
    if not await has_capability(callback.from_user.id, "moderate_reg"):
        await callback.answer(_NO_ACCESS, show_alert=True)
        return
    entry_id = _parse_id(callback.data)
    if entry_id is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    entry, error = await return_to_moderation(callback.from_user.id, entry_id)
    if entry is None:
        # Человеческая причина (двойной тап, чужой город, делегат уже не rejected) — алерт и
        # перерисовка списка актуальным состоянием, не молчаливый провал.
        await callback.answer(error or "Не удалось вернуть заявку — обновите список.", show_alert=True)
        text, kb = await render_journal_screen(callback.from_user.id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return

    telegram_id = entry["telegram_id"]
    raw_return_text = await get_setting("reject_rules_return_text") or DEFAULT_RETURN_TEXT
    from services.i18n import context as _i18n_context, tr as _i18n_tr
    lang, tr_map = await _i18n_context(telegram_id)
    delegate_text = html_module.escape(_i18n_tr(raw_return_text, lang, tr_map))
    try:
        await quiet_hours.send_or_queue_text(
            _now_moscow_naive(), telegram_id, delegate_text,
            sender=lambda: callback.bot.send_message(telegram_id, delegate_text, parse_mode="HTML"),
        )
    except Exception as e:  # noqa: BLE001 — делегат мог заблокировать бота, возврат не откатываем
        logger.error(f"arj_back_go: failed to notify delegate {telegram_id}: {e}")

    text, kb = await render_journal_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Заявка возвращена на модерацию — делегат уведомлён.")


@router.callback_query(F.data == "arj_csv")
async def arj_csv_export(callback: types.CallbackQuery):
    """D-29: файл для отчёта партнёрам, срез по городам менеджера (`export_csv`). Пустой
    журнал — алерт, без отправки пустого файла."""
    if not await has_capability(callback.from_user.id, "moderate_reg"):
        await callback.answer(_NO_ACCESS, show_alert=True)
        return
    _rows, total = await journal_page(callback.from_user.id, include_returned=True)
    if total == 0:
        await callback.answer("Выгружать пока нечего.", show_alert=True)
        return
    filename, file_bytes = await export_csv(callback.from_user.id)
    document = BufferedInputFile(file_bytes, filename=filename)
    await callback.message.answer_document(
        document, caption="Журнал автоотказов — вся история по вашим городам",
    )
    await callback.answer()
