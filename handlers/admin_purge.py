"""Квик 260910-ro7 (DELU-01..08): скрытая суперадминская команда `/delete_user <id|@username>` —
приёмка требует «чистого» тестового аккаунта, чтобы «новый делегат» снова стал новым без
ручного лазания в SQLite на сервере. Команда сознательно НЕ выведена ни в `/admin`, ни в
`admin_sections.SECTIONS`, ни в меню, ни в подсказках — только по точному имени.

Форма шва — эталон `handlers/admin_faq.py`: своего `Router()` нет, `from handlers.admin import
router`, каждый декоратор — в одну строку со строковым литералом (инвариант cap-теста
`test_roles_phase8.py`). `admin_caps` импортируется на уровне модуля — цикла не образует
(`handlers/admin_caps.py` сам не импортирует `handlers.admin`, см. его докстринг).

Право `ADMIN_CAPS["cmd:delete_user"] = "settings"` необходимо, но НЕ достаточно — настоящий
гейт `config.ADMIN_IDS`, повторно проверяется внутри КАЖДОГО из трёх хендлеров ниже (тот же
приём, что `admin_season_reset`/`season_reset_go` в `handlers/admin_cities.py`): менеджер с
правом `settings`, но не суперадмин, до удаления не доходит, а посторонний вообще не получает
ответа (deny-by-default мидлвари `admin_caps`, отдельная проверка ей не заменяется)."""
import html
import logging
import re

from aiogram import F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from cities import city_label, normalize_city
from database.db import count_user_footprint, find_user_id_by_username, get_staff_roles, get_user, purge_user
from handlers import admin_caps
from handlers.admin import router
from reg_labels import STATUS_LABELS
from services.scheduler import cancel_payment_reminders

logger = logging.getLogger(__name__)

_ID_ARG_RE = re.compile(r"^-?\d+$")

# Человеческие подписи непустых групп следа — порядок карточки, ключи совпадают с группами
# database.db.USER_PURGE_TABLES (второго списка групп нет и здесь: подписи читаются по тем
# же ключам, что вернул count_user_footprint/purge_user).
_GROUP_LABELS: dict[str, str] = {
    "application": "заявка",
    "draft": "незавершённая анкета",
    "game": "сдачи заданий",
    "coins": "записи монет",
    "questions": "вопросы делегата",
    "history": "записи истории правок",
    "consents": "согласия",
    "decisions": "решения по заявке",
    "queue": "уведомления в очереди",
    "events": "события анкеты",
    "deliveries": "отметки о доставке рассылок и опросов",
}


def _footprint_lines(footprint: dict) -> list[str]:
    return [
        f"• {label}: {footprint[key]}"
        for key, label in _GROUP_LABELS.items()
        if footprint.get(key)
    ]


def _delete_total(footprint: dict) -> int:
    """Сумма ВСЕХ групп удаления (без referrals_kept — та связь не удаляется, значит не
    считается частью следа, который делает человека «найденным»)."""
    return sum(v for k, v in footprint.items() if k != "referrals_kept")


async def _city_text(user: dict | None) -> str:
    raw_city = (user or {}).get("event_city") or (user or {}).get("city")
    if not raw_city:
        return "—"
    return await city_label(normalize_city(raw_city))


async def _build_card(tid: int, user: dict | None, footprint: dict, staff_roles: list[str]) -> tuple[str, InlineKeyboardMarkup]:
    full_name = (user or {}).get("full_name")
    username = (user or {}).get("username")
    status = (user or {}).get("status")
    city_text = await _city_text(user)
    parts = [
        "⚠️ <b>Удалить делегата?</b>",
        "",
        f"Имя: {html.escape(full_name or '—')}",
        f"Username: {html.escape(username or '—')}",
        f"ID: <code>{tid}</code>",
        f"Статус заявки: {STATUS_LABELS.get(status, '—')}",
        f"Город: {html.escape(city_text)}",
        "",
        "🗑 <b>Пропадёт навсегда:</b>",
        *_footprint_lines(footprint),
    ]
    if staff_roles:
        role_labels = ", ".join(
            admin_caps.ROLES.get(r, {}).get("label", r) for r in staff_roles
        )
        parts += [
            "",
            f"⚠️ У этого человека есть роль менеджера ({html.escape(role_labels)}) — "
            "удаление делегатских данных её не тронет.",
        ]
    if footprint.get("referrals_kept"):
        parts += ["", f"Привёл {footprint['referrals_kept']} делегатов — их заявки останутся."]
    parts += ["", "Вернуть нельзя. Строка в Google-таблице останется — удалить её нужно руками."]
    text = "\n".join(parts)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Удалить навсегда", callback_data=f"delu_go:{tid}"),
        InlineKeyboardButton(text="Отмена", callback_data="delu_no"),
    ]])
    return text, kb


@router.message(Command("delete_user"))
async def cmd_delete_user(message: types.Message):
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("Недостаточно прав.")
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer(
            "Формат: <code>/delete_user 123456789</code> (telegram_id) или "
            "<code>/delete_user @username</code>.",
            parse_mode="HTML",
        )
        return

    raw = args[1].strip()
    tid = int(raw) if _ID_ARG_RE.match(raw) else await find_user_id_by_username(raw)
    if tid is None:
        await message.answer(f"Не нашёл пользователя «{html.escape(raw)}».", parse_mode="HTML")
        return

    footprint = await count_user_footprint(tid)
    if _delete_total(footprint) == 0:
        await message.answer(f"Не нашёл пользователя «{html.escape(raw)}».", parse_mode="HTML")
        return

    user = await get_user(tid)
    staff_roles = await get_staff_roles(tid)
    text, kb = await _build_card(tid, user, footprint, staff_roles)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("delu_go:"))
async def delete_user_confirm(callback: types.CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    tid = int(callback.data.split(":", 1)[1])
    result = await purge_user(tid)
    if _delete_total(result) == 0:
        await callback.message.edit_text("Уже удалено.")
        await callback.answer()
        return
    try:
        cancel_payment_reminders(tid)
    except Exception:
        pass  # fail-soft: джобы напоминаний живут в отдельном jobs.sqlite (D-3), сбой их
        # отмены не должен мешать уже совершённому удалению делегата
    logger.warning(
        "delete_user: admin_id=%s удалил делегата tid=%s, счётчики=%s",
        callback.from_user.id, tid, result,
    )
    lines = _footprint_lines(result)
    text = "🗑 <b>Удалено:</b>\n" + "\n".join(lines) if lines else "🗑 <b>Удалено.</b>"
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "delu_no")
async def delete_user_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("Отменено. Ничего не удалено.")
    await callback.answer()
