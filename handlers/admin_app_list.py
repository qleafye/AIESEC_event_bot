"""Квик 260914-rgq (RGQ-01): экран «📇 Список заявок» в разделе «📋 Заявки» — менеджер видит
постранично одобренных/отклонённых/ожидающих делегатов: кликабельное имя — ник — дата решения,
чтобы не терять людей между решением и заездом (запрос менеджера RealTalk 14.09).

Форма шва — Phase 13 (REFAC-01): своего `Router()` нет, хендлеры декорируют ОБЩИЙ
`handlers.admin.router`; модуль подключается ХВОСТОМ `handlers/admin_sections.py` (см. импорт
СРАЗУ ПОСЛЕ `admin_lookup` там же). `admin_core` на уровне модуля безопасен, `admin_sections`
даёт цикл и потому лениво внутри функции — тот же приём, что у каждого другого шва этого
раздела (`handlers/admin_questions.py`, копия формы которого этот модуль почти дословно
повторяет)."""
import html as html_module
from datetime import datetime

from aiogram import F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import count_applications, list_applications_page
from handlers.admin import router
from handlers.admin_core import _admin_city_view

PAGE = 15

# Коды статусов менеджеру нигде не показываются — только подписи (CLAUDE.md: кодовые значения
# человеку вводить/видеть не даём).
STATUS_LABELS = {
    "approved": "✅ Одобренные",
    "rejected": "❌ Отклонённые",
    "pending": "⏳ Ожидают",
}
_STATUS_ORDER = ("approved", "rejected", "pending")


def _short_stamp(raw) -> str:
    """`ДД.ММ ЧЧ:ММ` из метки, которая УЖЕ московская (нет сдвига — `services.questions.
    format_stamp` здесь неприменима, она двигает UTC -> МСК, а `decided_at`/`registration_date`
    в этой семье пишутся `timeutil.msk_now`, второй сдвиг дал бы «будущее»). Фейл-софт: пустая
    или нераспознанная метка — «—», а не исключение."""
    if not raw:
        return "—"
    text = str(raw)[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            stamp = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return stamp.strftime("%d.%m %H:%M")
    return "—"


def _username(raw) -> str:
    if not raw:
        return "(без ника)"
    return html_module.escape("@" + str(raw).strip().lstrip("@"))


def _row_text(number: int, row: dict) -> str:
    name = html_module.escape(str(row.get("full_name") or "") or "—")
    username = _username(row.get("username"))
    stamp = _short_stamp(row.get("decided_at"))
    tg_id = row["telegram_id"]
    return f'{number}. <a href="tg://user?id={tg_id}">{name}</a> — {username} — {stamp}'


_DATE_CAPTION = {
    "approved": "решение",
    "rejected": "решение",
    "pending": "подана",
}


async def render_app_list_screen(
    admin_id: int, status: str = "approved", offset: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    """«Функция возвращает (text, kb)» idiom (форма `render_questions_screen`). WR-05: одно
    чтение города на экран — тот же scope уходит и в счётчики, и в выборку, иначе счётчик в
    шапке разойдётся со списком под ним."""
    if status not in STATUS_LABELS:
        status = "approved"
    scope, label = await _admin_city_view(admin_id)
    counts = await count_applications(city_scope=scope)
    rows = await list_applications_page(status=status, city_scope=scope, limit=PAGE, offset=offset)

    total = counts.get(status, 0)
    total_pages = max(1, (total + PAGE - 1) // PAGE)
    current_page = offset // PAGE + 1

    lines = ["📇 <b>Список заявок</b>"]
    lines.append(
        f"✅ {counts['approved']} · ❌ {counts['rejected']} · ⏳ {counts['pending']}"
    )
    if label:
        lines.append(html_module.escape(str(label)))
    lines.append(f"Показаны: {STATUS_LABELS[status]} ({_DATE_CAPTION[status]})")
    lines.append(f"Страница {current_page} из {total_pages}")

    if not rows:
        lines.append("")
        lines.append("Пока пусто.")
    else:
        for i, row in enumerate(rows):
            lines.append("")
            lines.append(_row_text(offset + i + 1, row))

    lines.append("")
    lines.append("Найти конкретного делегата: /find @ник")

    text = "\n".join(lines)

    buttons: list[list[InlineKeyboardButton]] = []
    status_row = [
        InlineKeyboardButton(
            text=("• " if opt == status else "") + STATUS_LABELS[opt],
            callback_data=f"apl:{opt}:0",
        )
        for opt in _STATUS_ORDER
    ]
    buttons.append(status_row)

    nav_row: list[InlineKeyboardButton] = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(
            text="⬅️", callback_data=f"apl:{status}:{max(0, offset - PAGE)}",
        ))
    if offset + PAGE < total:
        nav_row.append(InlineKeyboardButton(
            text="➡️", callback_data=f"apl:{status}:{offset + PAGE}",
        ))
    if nav_row:
        buttons.append(nav_row)

    from handlers.admin_sections import back_button  # ленивый шов: цикл на уровне модуля
    buttons.append([back_button("admin_app_list")])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_app_list")
async def admin_app_list_open(callback: types.CallbackQuery):
    text, kb = await render_app_list_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("apl:"))
async def apl_page(callback: types.CallbackQuery):
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Некорректная страница", show_alert=True)
        return
    _, status_raw, offset_raw = parts
    try:
        offset = int(offset_raw)
    except ValueError:
        offset = -1
    if offset < 0:
        await callback.answer("Некорректная страница", show_alert=True)
        return
    status = status_raw if status_raw in STATUS_LABELS else "approved"
    text, kb = await render_app_list_screen(callback.from_user.id, status=status, offset=offset)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()
