"""Phase 16 (16-02, GAME-UI-02): служебное сообщение-счётчик сдачи задания (Экран 3).

Пока делегат собирает сдачу, бот держит ОДНО сообщение «Частей: N · 📸2 ✍️1» и редактирует
его на месте на каждую присланную часть — вместо нового «Принял, частей: N» на каждое фото.
Черновик живёт только в FSM (`gs_parts`); в БД (`game_submission_parts`) пишет лишь
`finalize_game_submission` по «✅ Готово». Здесь — чистый рендер текста/клавиатуры и один
fail-soft edit; хендлеры остаются в `handlers/user_actions.py` (вынесено сюда из-за потолка
размера модуля, см. tests/test_module_size_convention_260816.py).

Порядок видов фиксированный (photo, document, text, link) независимо от порядка прихода;
нулевые виды не показываются (по скетчу) — «одно фото + один текст» всегда «📸1 ✍️1».
"""
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from settings_schema import get_setting_typed

PART_KIND_ORDER = ("photo", "document", "text", "link")
PART_KIND_EMOJI = {"photo": "📸", "document": "📄", "text": "✍️", "link": "🔗"}


async def game_counter_text(parts: list[dict]) -> str:
    counts = {kind: 0 for kind in PART_KIND_ORDER}
    for part in parts:
        kind = part.get("kind")
        if kind in counts:
            counts[kind] += 1
    breakdown = " ".join(
        f"{PART_KIND_EMOJI[kind]}{counts[kind]}" for kind in PART_KIND_ORDER if counts[kind]
    )
    template = await get_setting_typed("game_proof_collected_template")
    try:
        text = template.format(count=len(parts), breakdown=breakdown)
    except (KeyError, IndexError, ValueError):
        # Менеджер сломал плейсхолдеры в реестре -- не падаем посреди сдачи.
        text = f"Частей: {len(parts)} · {breakdown}"
    if not breakdown:
        text = text.strip(" ·")  # пустая сдача: без висящего разделителя
    return text


async def game_counter_kb(parts: list[dict]) -> InlineKeyboardMarkup:
    """Ряд 1: «✅ Готово» (+ «🗑 Убрать последнее», только когда есть что убирать);
    ряд 2: «❌ Отмена» — тот же литерал, что у reply-клавиатуры get_cancel_kb()."""
    row = [InlineKeyboardButton(
        text=await get_setting_typed("game_proof_done_button"), callback_data="gs_done",
    )]
    if parts:
        row.append(InlineKeyboardButton(
            text=await get_setting_typed("game_proof_remove_last_button"),
            callback_data="gs_remove_last",
        ))
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="❌ Отмена", callback_data="gs_cancel")],
    ])


async def edit_counter(bot: Bot, data: dict, parts: list[dict]) -> None:
    """Редактирует счётчик, чьи chat_id/message_id лежат в FSM (`gs_counter_*`). Именно
    `bot.edit_message_text`, а не `message.edit_text`: хендлер вызван ЧУЖИМ входящим
    сообщением делегата (фото/текст), а редактируем — своё. Fail-soft (Pitfall 1): удалённый/
    устаревший счётчик не ломает сдачу — делегат по-прежнему может слать части и жать «Готово»."""
    try:
        await bot.edit_message_text(
            chat_id=data["gs_counter_chat_id"], message_id=data["gs_counter_msg_id"],
            text=await game_counter_text(parts), reply_markup=await game_counter_kb(parts),
        )
    except Exception:
        pass
