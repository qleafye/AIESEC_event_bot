"""Квик 260923 (AUTOREJ-REPORT, D-I): экран «📊 Отчётность автоотказа» — вкладка таблицы,
потолок пачки уведомлений, кнопка «обновить вкладку сейчас». Новый маленький экран (не вкладка
в `handlers/admin_reject_rules.py`, файл уже под потолком размера) — вход с экрана «🚫 Правила
автоотказа».

Форма шва — та же, что у соседей раздела (`handlers/admin_reject_rules.py`,
`handlers/admin_reject_journal.py`): своего `Router()` нет, хендлеры декорируют ОБЩИЙ
`handlers.admin.router`, каждый декоратор — в одну строку (инвариант cap-теста).
`handlers.admin` — на уровне модуля; `handlers.admin_sections` (`back_button`) — лениво внутри
функций, тот же приём, что у каждого соседнего шва.

Капа — ТА ЖЕ, что у экрана правил (`"settings"`, см. `handlers/admin_caps.py`): в отличие от
журнала автоотказов (`admin_reject_journal`, `"moderate_reg"` — работа модератора), здесь нет
разрыва прав между экраном-входом и этим экраном, поэтому повторная ручная проверка в каждом
хендлере не нужна (`CapabilityMiddleware` уже достаточно — тот же расклад, что у
`handlers/admin_reject_rules.py`).

Правка обоих ключей (`auto_reject_sheet_tab`, `reg_submit_digest_max_minutes`) идёт через ОБЩИЙ
редактор `settings_edit:{key}` (handlers/admin_settings.py) — второй редактор здесь не
заводится (D-I: правится кнопками, но ввод произвольного значения — общий механизм проекта)."""
import html as html_module

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_setting
from handlers.admin import router
from settings_schema import get_setting_typed


def _tab_label(tab: str | None) -> str:
    return f"«{tab}»" if tab else "не ведётся"


def _cap_label(cap_minutes) -> str:
    return "без потолка" if not cap_minutes else f"не позже {cap_minutes} мин"


async def render_reports_screen(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    tab = (await get_setting("auto_reject_sheet_tab") or "").strip()
    cap_minutes = await get_setting_typed("reg_submit_digest_max_minutes")

    lines = [
        "📊 <b>Отчётность автоотказа</b>", "",
        f"Вкладка в таблице: {html_module.escape(_tab_label(tab))}",
        f"Пачка заявок: {_cap_label(cap_minutes)}",
        "",
        "Подсказка: пачка уведомлений работает, когда в «📋 Заявки» → «📥 Уведомления о "
        "заявках» выбран режим «Пачкой (дайджест)».",
    ]

    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=f"📄 Вкладка в таблице: {_tab_label(tab)}",
            callback_data="settings_edit:auto_reject_sheet_tab",
        )],
        [InlineKeyboardButton(
            text=f"⏱ Пачка заявок: {_cap_label(cap_minutes)}",
            callback_data="settings_edit:reg_submit_digest_max_minutes",
        )],
    ]
    if tab:
        buttons.append([InlineKeyboardButton(text="🔄 Обновить вкладку сейчас", callback_data="arp_sync")])

    from handlers.admin_sections import back_button  # ленивый шов: цикл на уровне модуля
    buttons.append([back_button("admin_reject_rules")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_reject_reports")
async def admin_reject_reports(callback: types.CallbackQuery):
    text, kb = await render_reports_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "arp_sync")
async def arp_sync(callback: types.CallbackQuery):
    """Ручной «прогнать сейчас» — та же джоба, что интервальный автосинк
    (`services.scheduler.sync_auto_reject_sheet_job`), поэтому механика гарантированно не
    расходится с фоновым обновлением. Пустое имя вкладки -> алерт без похода в БД/лист."""
    tab = (await get_setting("auto_reject_sheet_tab") or "").strip()
    if not tab:
        await callback.answer("Сначала задайте имя вкладки.", show_alert=True)
        return
    from services.scheduler import sync_auto_reject_sheet_job
    result = await sync_auto_reject_sheet_job()
    if result < 0:
        await callback.answer("Не получилось — проверьте доступ бота к таблице.", show_alert=True)
        return
    await callback.answer(f"Готово: {result} строк.", show_alert=True)
