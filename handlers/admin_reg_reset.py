"""Phase 33 (delegate-card admin actions, задача 1): «🧹 Сбросить зависшую анкету» — кнопка на
карточке `/find` (`handlers/admin.py::cmd_find_user`), сам сброс — `services/reg_stuck_reset.py`.

Не форумная функция (админ-действие модератора) — своей строки в хабе «🎪 Форум: функции» нет
и не нужно, тот же посыл, что у соседних швов фазы (`admin_city_move.py`/
`admin_revert_pending.py`/`admin_resubmit_grant.py`/`admin_edit_grant.py`).

Шов той же формы, что соседние: своего `Router()` нет, хендлеры декорируют ОБЩИЙ
`admin.router`, модуль импортируется ХВОСТОМ `handlers/admin.py` (golden snapshot: чистая
вставка). `_city_allowed` — импорт из `handlers/admin_checkin.py` (не владеем файлом, только
вызываем).

`fsm_storage` — хвостовой параметр хендлера `regreset_apply` (aiogram кладёт FSM-хранилище
диспетчера в данные хендлера под этим именем, тот же приём, что `handlers/admin_sos.py::
sos_resolve`) — нужен `services/reg_stuck_reset.reset_stuck_registration`, чтобы дотянуться до
`StorageKey` делегата и решить, чистить ли его текущее FSM-состояние."""
import html as html_module
import logging

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cities import normalize_city
from database.db import get_user
from handlers.admin import router
from handlers.admin_checkin import _city_allowed
from services.reg_stuck_reset import preview_stuck_reset, reset_stuck_registration

logger = logging.getLogger(__name__)

_CITY_FORBIDDEN_ALERT = "Этот город вне вашей зоны ответственности."
_NOT_FOUND_ALERT = "Делегат не найден — возможно, карточка устарела."
_NO_DRAFT_ALERT = "Черновика уже нет — возможно, делегат сам успел закончить или сбросить анкету."

_KIND_LABELS = {"new": "новая анкета (ещё не подана)", "edit": "правка уже поданной анкеты"}


def _parse_tid(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.lstrip("-").isdigit() else None


def _activity_line(preview: dict) -> str:
    minutes = preview.get("minutes_since_activity")
    step = preview.get("step") or "-"
    if minutes is None:
        return f"Последняя активность: неизвестно, шаг {html_module.escape(str(step))}"
    return f"Последняя активность: {int(minutes)} мин назад, шаг {html_module.escape(str(step))}"


async def _render_confirm(tid: int, notify: bool) -> tuple[str, InlineKeyboardMarkup] | None:
    """`None` — делегата нет или черновика уже нет (гонка между открытием карточки и тапом по
    кнопке) — вызывающий сам решает, каким алертом это показать."""
    user = await get_user(tid)
    preview = await preview_stuck_reset(tid)
    if preview is None:
        return None

    name = html_module.escape(str((user or {}).get("full_name") or tid))
    kind_label = _KIND_LABELS.get(preview["kind"], preview["kind"])

    lines = [
        "🧹 <b>Сбросить зависшую анкету?</b>\n",
        f"{name}",
        f"Черновик: {kind_label}",
        _activity_line(preview),
    ]
    if preview.get("is_recent"):
        lines.append(
            "\n⚠️ Делегат, возможно, прямо сейчас заполняет анкету — сброс прервёт его."
        )
    lines.append("")
    if preview["kind"] == "edit":
        lines.append(
            "Что изменится: удалится только черновик текущей правки. Уже поданная анкета "
            "(данные делегата в базе) НЕ тронется."
        )
    else:
        lines.append(
            "Что изменится: удалится незавершённый черновик анкеты и отметка «начал(а) "
            "регистрацию» — следующий /start начнётся с чистого листа. Поданной анкеты у "
            "делегата нет, удалять нечего."
        )
    lines.append("Текущий шаг в чате (если делегат сейчас на нём) тоже сбросится.")

    toggle_text = f"🔔 Сообщить делегату: {'ВКЛ' if notify else 'ВЫКЛ'}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=f"regreset_toggle:{tid}:{0 if notify else 1}")],
        [InlineKeyboardButton(
            text="🧹 Сбросить", callback_data=f"regreset_apply:{tid}:{1 if notify else 0}",
        )],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"regreset_cancel:{tid}")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("regreset_start:"))
async def regreset_start(callback: types.CallbackQuery):
    tid = _parse_tid(callback.data.split(":", 1)[1])
    if tid is None:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    user = await get_user(tid)
    # `user is None` — делегат из reg_started, не users (задача 2) — city_allowed проверяем
    # по тому, что реально есть.
    city_source = (user or {}).get("event_city")
    if not await _city_allowed(callback.from_user.id, normalize_city(city_source)):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return

    rendered = await _render_confirm(tid, notify=True)
    if rendered is None:
        await callback.answer(_NO_DRAFT_ALERT, show_alert=True)
        return
    text, kb = rendered
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("regreset_toggle:"))
async def regreset_toggle(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid = _parse_tid(parts[1])
    if tid is None or parts[2] not in ("0", "1"):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    user = await get_user(tid)
    if not await _city_allowed(callback.from_user.id, normalize_city((user or {}).get("event_city"))):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return

    rendered = await _render_confirm(tid, notify=(parts[2] == "1"))
    if rendered is None:
        await callback.answer(_NO_DRAFT_ALERT, show_alert=True)
        return
    text, kb = rendered
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("regreset_apply:"))
async def regreset_apply(callback: types.CallbackQuery, fsm_storage=None):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid = _parse_tid(parts[1])
    if tid is None or parts[2] not in ("0", "1"):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    notify = parts[2] == "1"
    user = await get_user(tid)
    admin_id = callback.from_user.id
    # Тот же двойной перечёт прав, что у revert_pending/resubg/editg — между экранами могло
    # пройти любое время.
    if not await _city_allowed(admin_id, normalize_city((user or {}).get("event_city"))):
        await callback.answer(_CITY_FORBIDDEN_ALERT, show_alert=True)
        return

    report = await reset_stuck_registration(
        callback.bot, fsm_storage, tid, by_admin=admin_id, notify=notify,
    )
    if not report.get("ok"):
        await callback.message.edit_text(
            f"❌ Не сбросил(а): {html_module.escape(str(report.get('error') or '-'))}",
        )
        await callback.answer()
        return

    name = html_module.escape(str((user or {}).get("full_name") or tid))
    lines = [f"✅ Анкета делегата <b>{name}</b> сброшена."]
    if report.get("fsm_cleared"):
        lines.append("Текущий шаг в чате тоже сброшен.")
    if notify:
        lines.append("Делегату отправлено сообщение." if report.get("notified") else "Сообщение делегату отправить не удалось.")
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML")
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("regreset_cancel:"))
async def regreset_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("✖️ Сброс анкеты отменён.")
    await callback.answer()
