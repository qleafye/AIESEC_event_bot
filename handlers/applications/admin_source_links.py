"""«🔗 Ссылки с метками» — экран роли «📣 Маркетинг (метки)» (право `source_links`).

Маркетологу нужно ровно две вещи: сделать ссылку со своей меткой и видеть, сколько заявок по
каждой метке пришло. Экран показывает счётчики из той же статистики, что «📈 Источники»
(`database.db.get_source_stats`: «источник -> число поданных анкет»), — только метка и число,
ни имён, ни контактов. «➕ Новая ссылка» спрашивает название метки и отдаёт готовую ссылку
(проверка и тексты общие с командой `/create_link`, `services/registration/source_links.py`).

Ссылку сохранять в боте не нужно: она работает сразу, а метка появляется в списке с первой
поданной по ней анкетой.

Форма шва — как у соседей: своего `Router()` нет, `from handlers.admin import router`, импорт
из хвоста `handlers/admin.py`. Права — `ADMIN_CAPS` (admin_source_links/srclink_*/
state:SourceLinkCreate:*), строка раздела — «📊 Данные» (`handlers/settings/admin_sections.py`).
"""
import html
import logging

from aiogram import Bot, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import get_source_stats
from handlers.admin import router
from handlers.states import SourceLinkCreate
from services.registration import source_links

logger = logging.getLogger(__name__)

# Одно сообщение Telegram — не больше 4096 символов; хвост сворачиваем в «…и ещё N».
_MAX_ROWS = 40

ASK_TAG_TEXT = (
    "➕ <b>Новая ссылка с меткой</b>\n\n"
    "Как назвать метку? Пришлите одно слово: латинские буквы, цифры, «_» и «-», без пробелов.\n"
    "Например: <code>vk_poster</code> — для афиши во ВКонтакте, <code>tg_channel</code> — для "
    "поста в канале.\n\n"
    "По этому названию вы потом найдёте метку в списке."
)


def _block(title: str, rows: list) -> list[str]:
    if not rows:
        return []
    lines = ["", title]
    for source, count in rows[:_MAX_ROWS]:
        lines.append(f"• {html.escape(str(source))} — {count}")
    if len(rows) > _MAX_ROWS:
        lines.append(f"…и ещё {len(rows) - _MAX_ROWS}")
    return lines


async def render_source_links_screen() -> tuple[str, InlineKeyboardMarkup]:
    from handlers.settings.admin_sections import back_button  # ленивый шов: модульный импорт даст цикл

    rows = list(await get_source_stats())
    # Метка — то, что могло прийти из ссылки (латиница/цифры/«_»/«-»); остальное — ответы
    # делегатов на вопрос «Откуда узнал» в анкете («ВК», «От друзей»). Та же граница, что у меток
    # кампаний на дашборде.
    tags = [r for r in rows if source_links.is_valid_tag(str(r[0]))]
    answers = [r for r in rows if not source_links.is_valid_tag(str(r[0]))]
    lines = [
        "🔗 <b>Ссылки с метками</b>",
        "",
        "Ссылка с меткой показывает, откуда пришёл делегат: каждая анкета, поданная по такой "
        "ссылке, засчитывается её метке. Ниже — сколько анкет подано по каждой метке.",
    ]
    if not rows:
        lines += ["", "<i>Пока ни одной поданной анкеты с меткой.</i>"]
    else:
        lines += _block("<b>Метки ссылок</b> — анкет:", tags)
        lines += _block("<b>Ответы в анкете «Откуда узнал»</b> — анкет:", answers)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новая ссылка", callback_data="srclink_new")],
        [back_button("admin_source_links")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data == "admin_source_links")
async def show_source_links(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_source_links_screen()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="srclink_cancel")],
    ])


@router.callback_query(F.data == "srclink_new")
async def source_link_new(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(SourceLinkCreate.waiting_for_tag)
    await callback.message.edit_text(ASK_TAG_TEXT, parse_mode="HTML", reply_markup=_cancel_kb())
    await callback.answer()


@router.callback_query(F.data == "srclink_cancel")
async def source_link_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await render_source_links_screen()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer("Отменено")


@router.message(StateFilter(SourceLinkCreate), Command("cancel"))
@router.message(StateFilter(SourceLinkCreate), F.text == "Отмена")
async def source_link_cancel_text(message: types.Message, state: FSMContext):
    await state.clear()
    text, kb = await render_source_links_screen()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(SourceLinkCreate.waiting_for_tag)
async def source_link_tag_step(message: types.Message, state: FSMContext, bot: Bot):
    raw = message.text or ""
    if not raw.strip():
        await message.answer(
            "Не понял: пришлите название метки текстом, одним словом — например, "
            "<code>vk_poster</code>.",
            parse_mode="HTML", reply_markup=_cancel_kb(),
        )
        return
    tag = source_links.clean_tag(raw)
    if not source_links.is_valid_tag(tag):
        await message.answer(
            source_links.bad_tag_text(raw, "vk_poster") + "\n\nПришлите другое название.",
            parse_mode="HTML", reply_markup=_cancel_kb(),
        )
        return
    await state.clear()
    link = source_links.build_link((await bot.get_me()).username, tag)
    text = source_links.link_reply_text(tag, link, where="«🔗 Ссылки с метками»")
    text += "\n\nНажмите на ссылку, чтобы скопировать. Сохранять её в боте не нужно — она уже работает."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Ещё одна ссылка", callback_data="srclink_new")],
        [InlineKeyboardButton(text="🔗 К ссылкам с метками", callback_data="admin_source_links")],
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
