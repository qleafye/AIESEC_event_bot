"""Экран «🎨 Оформление» → «🎟 Карточка QR»: менеджер сам оформляет карточку QR делегата под
событие — цвет фона или картинка, цвет заголовка, цвет плашки-подсказки, своё лого, какие элементы
показывать — и видит превью картинкой до того, как делегаты получат новую карточку.

Шов: хендлеры регистрируются на общий `router` (`handlers.admin`), модуль импортируется из хвоста
`handlers/settings/admin_miniapp_theme.py`. Отдельный файл — чтобы экраны Mini App не упёрлись в
потолок размера. Права `qrct_*` / `state:QrCardTheme:*` (settings) добавляются в карту прав здесь
же, на импорте: `handlers/access/admin_caps.py` по потолку не растёт.

Правило «бот для людей»: цвет выбирается кнопками палитры с человеческими подписями из реестра,
HEX и коды ключей человеку не показываются. Все правки копятся черновиком (FSM-данные, ключ
`qrct_draft`: ключ реестра -> значение, `None` = «убрать») и пишутся в реестр только по «✅
Сохранить» после превью — через `set_setting_by_admin`/`delete_setting_by_admin` (журнал)."""
from __future__ import annotations

import html as html_module
import logging

from aiogram import F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

import shared.web_theme as web_theme
from domain.settings.schema import option_label, option_labels
from handlers.access.admin_caps import ADMIN_CAPS
from handlers.admin import router
from handlers.states import QrCardTheme
from services.forum.qr_card_theme import CARD_THEME_KEYS, PALETTE, hex_of, load_card_theme, resolve_values, sample_preview
from services.settings.audit import delete_setting_by_admin, set_setting_by_admin

logger = logging.getLogger(__name__)

# Права экрана — тот же довод, что у miniapp_theme_* (оформление = settings). Регистрируются на
# импорте шва, а не в admin_caps.py: у того потолок размера «не поднимать».
ADMIN_CAPS.update({
    "qrct_*": "settings",
    "state:QrCardTheme:*": "settings",
})

DRAFT_KEY = "qrct_draft"
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_IMAGE_MIMES = ("image/png", "image/jpeg")

# handle в callback_data -> (ключ реестра, подпись кнопки, эмодзи)
_COLOR_HANDLES: dict[str, tuple[str, str, str]] = {
    "bg": (CARD_THEME_KEYS["bg"], "Фон", "🎨"),
    "text": (CARD_THEME_KEYS["text"], "Цвет текста", "🔤"),
    "accent": (CARD_THEME_KEYS["accent"], "Плашка", "🟧"),
}
_TOGGLES: dict[str, tuple[str, str]] = {
    "watermark": (CARD_THEME_KEYS["show_watermark"], "Надписи на фоне"),
    "logo": (CARD_THEME_KEYS["show_logo"], "Лого"),
    "city": (CARD_THEME_KEYS["show_city"], "Город под именем"),
    "footer": (CARD_THEME_KEYS["show_footer"], "Плашка-подсказка"),
}
# slot -> (ключ реестра, состояние, подсказка)
_IMAGE_SLOTS = {
    "bg_image": (
        CARD_THEME_KEYS["bg_image"], QrCardTheme.bg_image,
        "🖼 <b>Картинка фона карточки QR</b>\n\nПришлите картинку фото или файлом PNG/JPEG (до 10 МБ). "
        "Она растянется на всю карточку за белым блоком с кодом, лишнее по краям обрежется.",
    ),
    "logo": (
        CARD_THEME_KEYS["logo"], QrCardTheme.logo,
        "🏷 <b>Своё лого на карточке QR</b>\n\nПришлите лого фото или файлом PNG/JPEG (до 10 МБ). "
        "Лого с прозрачным фоном присылайте только файлом PNG — фото Telegram прозрачность теряет. "
        "Лого встанет в левый верхний угол над заголовком, как есть, без перекраски.",
    ),
}
_STATE_SLOT = {"bg_image": "bg_image", "logo": "logo"}  # суффикс состояния QrCardTheme -> slot

_UNKNOWN_BUTTON = "Кнопка устарела — откройте экран «🎟 Карточка QR» заново."
_PREVIEW_FAILED = "Не получилось собрать превью — пришлите другую картинку фото или PNG-файлом"
_BAD_FILE = "Не понял — пришлите картинку фото или файлом PNG/JPEG"
_TOO_BIG = "Картинка больше 10 МБ — пришлите её поменьше (фото или файлом PNG/JPEG)."
_LOW_CONTRAST = "⚠️ Текст плохо виден на этом фоне — выберите цвет текста контрастнее."


async def _draft(state: FSMContext) -> dict:
    return dict((await state.get_data()).get(DRAFT_KEY) or {})


async def _put(state: FSMContext, key: str, value) -> None:
    draft = await _draft(state)
    draft[key] = value
    await state.update_data(**{DRAFT_KEY: draft})


def _contrast_warning(values: dict) -> str | None:
    """Плохой контраст объясняется словами и не блокирует сохранение. С картинкой фона цвет
    фона не виден — не проверяем."""
    if values[CARD_THEME_KEYS["bg_image"]]:
        return None
    text, bg = values[CARD_THEME_KEYS["text"]], values[CARD_THEME_KEYS["bg"]]
    if text not in PALETTE or bg not in PALETTE:
        return None
    if text == bg or web_theme.contrast_ratio(hex_of(text), hex_of(bg)) < 3:
        return _LOW_CONTRAST
    return None


def _color_label(key: str, value) -> str:
    return option_label(key, value) if value in PALETTE else option_label(key, "blue")


async def render_qr_card_theme_text(draft: dict) -> str:
    values = await resolve_values(draft)
    on = lambda key: values[key] != "off"  # noqa: E731
    lines = ["🎟 <b>Карточка QR</b>"]
    if draft:
        lines.append("✏️ Есть несохранённые изменения")
    lines += [
        "",
        "Картинка с QR-кодом, которую делегат получает для входа. Здесь меняется только "
        "оформление вокруг кода — сам код остаётся прежним.",
        "",
        f"🎨 Фон: {_color_label(CARD_THEME_KEYS['bg'], values[CARD_THEME_KEYS['bg']])}",
        "🖼 Картинка фона: " + ("есть (поверх цвета)" if values[CARD_THEME_KEYS["bg_image"]] else "нет"),
        f"🔤 Цвет текста: {_color_label(CARD_THEME_KEYS['text'], values[CARD_THEME_KEYS['text']])}",
        f"🟧 Плашка: {_color_label(CARD_THEME_KEYS['accent'], values[CARD_THEME_KEYS['accent']])}",
        "🏷 Лого: " + ("своё" if values[CARD_THEME_KEYS["logo"]] else "АЙСЕК"),
        "",
        "Что показывать на карточке:",
    ]
    for key, label in _TOGGLES.values():
        lines.append(("✅ " if on(key) else "☐ ") + label)
    warning = _contrast_warning(values)
    if warning:
        lines += ["", warning]
    lines += ["", "Правки не видны делегатам, пока вы не нажмёте «💾 Сохранить»."]
    return "\n".join(lines)


async def build_qr_card_theme_keyboard(draft: dict) -> InlineKeyboardMarkup:
    values = await resolve_values(draft)
    btn = InlineKeyboardButton
    bg_key, logo_key = CARD_THEME_KEYS["bg_image"], CARD_THEME_KEYS["logo"]
    rows = [[btn(
        text=f"🎨 Фон: {_color_label(CARD_THEME_KEYS['bg'], values[CARD_THEME_KEYS['bg']])}",
        callback_data="qrct_pick:bg",
    )]]
    if values[bg_key]:
        rows.append([
            btn(text="🖼 Другая картинка фона", callback_data="qrct_photo:bg_image"),
            btn(text="🗑 Убрать картинку фона", callback_data="qrct_remove:bg_image"),
        ])
    else:
        rows.append([btn(text="🖼 Картинка фона", callback_data="qrct_photo:bg_image")])
    for handle in ("text", "accent"):
        key, label, emoji = _COLOR_HANDLES[handle]
        rows.append([btn(text=f"{emoji} {label}: {_color_label(key, values[key])}", callback_data=f"qrct_pick:{handle}")])
    if values[logo_key]:
        rows.append([
            btn(text="🏷 Другое лого", callback_data="qrct_photo:logo"),
            btn(text="🗑 Вернуть лого АЙСЕК", callback_data="qrct_remove:logo"),
        ])
    else:
        rows.append([btn(text="🏷 Своё лого", callback_data="qrct_photo:logo")])
    toggles = [
        btn(text=("✅ " if values[key] != "off" else "☐ ") + label, callback_data=f"qrct_toggle:{name}")
        for name, (key, label) in _TOGGLES.items()
    ]
    rows += [toggles[:2], toggles[2:]]
    rows.append([
        btn(text="👁 Превью", callback_data="qrct_preview"),
        btn(text="💾 Сохранить", callback_data="qrct_save"),
    ])
    if draft:
        rows.append([btn(text="✖️ Отменить изменения", callback_data="qrct_discard")])
    rows.append([btn(text="↩️ Как было по умолчанию", callback_data="qrct_reset")])
    rows.append([btn(text="← К оформлению", callback_data="admin_miniapp_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(message, state: FSMContext, *, edit: bool = True) -> None:
    draft = await _draft(state)
    text = await render_qr_card_theme_text(draft)
    markup = await build_qr_card_theme_keyboard(draft)
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:  # сообщение-картинка (превью) или «not modified» — шлём новым
            pass
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="qrct_cancel_edit")],
    ])


async def _send_preview(callback: types.CallbackQuery, state: FSMContext, *, ask_save: bool) -> None:
    draft = await _draft(state)
    try:
        theme = await load_card_theme(overrides=draft, bot=callback.bot)
        png = await sample_preview(theme)
    except Exception as e:
        logger.warning(f"qrct: превью карточки не собралось: {e}")
        await callback.message.answer(_PREVIEW_FAILED)
        return
    caption = "Так карточку увидят делегаты."
    if ask_save:
        caption += " Сохранить?"
    warning = _contrast_warning(await resolve_values(draft))
    if warning:
        caption += "\n\n" + warning
    markup = None
    if ask_save:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Сохранить", callback_data="qrct_save_go"),
            InlineKeyboardButton(text="✏️ Ещё поправить", callback_data="qrct_open"),
        ]])
    await callback.message.answer_photo(
        BufferedInputFile(png, filename="qr-card-preview.png"), caption=caption, reply_markup=markup,
    )


# ── экран ────────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "qrct_open")
async def qrct_open(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)  # черновик (данные FSM) не трогаем
    await callback.answer()
    await _show(callback.message, state)


@router.callback_query(F.data == "qrct_cancel_edit")
async def qrct_cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await callback.answer()
    await _show(callback.message, state)


@router.callback_query(F.data.startswith("qrct_pick:"))
async def qrct_pick(callback: types.CallbackQuery, state: FSMContext):
    handle = callback.data.split(":", 1)[1]
    if handle not in _COLOR_HANDLES:
        await callback.answer(_UNKNOWN_BUTTON, show_alert=True)
        return
    key, label, emoji = _COLOR_HANDLES[handle]
    current = (await resolve_values(await _draft(state)))[key]
    labels = option_labels(key)
    rows = [
        [InlineKeyboardButton(
            text=("✅ " if name == current else "") + labels.get(name, name),
            callback_data=f"qrct_set:{handle}:{name}",
        )]
        for name in PALETTE
    ]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="qrct_open")])
    await callback.message.edit_text(
        f"{emoji} <b>{html_module.escape(label)}</b> карточки QR — выберите цвет:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("qrct_set:"))
async def qrct_set(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[1] not in _COLOR_HANDLES or parts[2] not in PALETTE:
        await callback.answer(_UNKNOWN_BUTTON, show_alert=True)
        return
    await _put(state, _COLOR_HANDLES[parts[1]][0], parts[2])
    await callback.answer()
    await _show(callback.message, state)


@router.callback_query(F.data.startswith("qrct_toggle:"))
async def qrct_toggle(callback: types.CallbackQuery, state: FSMContext):
    name = callback.data.split(":", 1)[1]
    if name not in _TOGGLES:
        await callback.answer(_UNKNOWN_BUTTON, show_alert=True)
        return
    key = _TOGGLES[name][0]
    current = (await resolve_values(await _draft(state)))[key]
    await _put(state, key, "on" if current == "off" else "off")
    await callback.answer()
    await _show(callback.message, state)


@router.callback_query(F.data.startswith("qrct_photo:"))
async def qrct_photo(callback: types.CallbackQuery, state: FSMContext):
    slot = callback.data.split(":", 1)[1]
    if slot not in _IMAGE_SLOTS:
        await callback.answer(_UNKNOWN_BUTTON, show_alert=True)
        return
    _key, fsm_state, prompt = _IMAGE_SLOTS[slot]
    await callback.message.edit_text(prompt, parse_mode="HTML", reply_markup=_cancel_keyboard())
    await state.set_state(fsm_state)
    await callback.answer()


@router.callback_query(F.data.startswith("qrct_remove:"))
async def qrct_remove(callback: types.CallbackQuery, state: FSMContext):
    slot = callback.data.split(":", 1)[1]
    if slot not in _IMAGE_SLOTS:
        await callback.answer(_UNKNOWN_BUTTON, show_alert=True)
        return
    await _put(state, _IMAGE_SLOTS[slot][0], None)
    await callback.answer()
    await _show(callback.message, state)


@router.callback_query(F.data == "qrct_preview")
async def qrct_preview(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await _send_preview(callback, state, ask_save=False)


@router.callback_query(F.data == "qrct_save")
async def qrct_save(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await _send_preview(callback, state, ask_save=True)


@router.callback_query(F.data == "qrct_save_go")
async def qrct_save_go(callback: types.CallbackQuery, state: FSMContext):
    draft = await _draft(state)
    for key, value in draft.items():
        if key not in CARD_THEME_KEYS.values():
            continue
        if value is None:
            await delete_setting_by_admin(callback.from_user.id, key)
        else:
            await set_setting_by_admin(callback.from_user.id, key, value)
    await state.update_data(**{DRAFT_KEY: {}})
    await callback.answer()
    await callback.message.answer("✅ Оформление карточки QR сохранено")
    await _show(callback.message, state, edit=False)


@router.callback_query(F.data == "qrct_discard")
async def qrct_discard(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(**{DRAFT_KEY: {}})
    await callback.answer("Изменения отменены")
    await _show(callback.message, state)


@router.callback_query(F.data == "qrct_reset")
async def qrct_reset(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "Сбросить оформление карточки QR? Пропадут: выбранные цвета, картинка фона, своё лого, "
        "выключенные элементы. Карточка станет как по умолчанию.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, сбросить", callback_data="qrct_reset_go"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="qrct_open"),
        ]]),
    )
    await callback.answer()


@router.callback_query(F.data == "qrct_reset_go")
async def qrct_reset_go(callback: types.CallbackQuery, state: FSMContext):
    for key in CARD_THEME_KEYS.values():
        await delete_setting_by_admin(callback.from_user.id, key)
    await state.update_data(**{DRAFT_KEY: {}})
    await callback.answer("Оформление карточки сброшено")
    await _show(callback.message, state)


# ── приём картинки (фон/лого) ─────────────────────────────────────────────────────────────
# Декораторы в одну строку: golden-снапшот роутеров читает только строки «@router.».

async def _accept_image(message: types.Message, state: FSMContext, file_id: str) -> None:
    raw_state = await state.get_state() or ""
    slot = _STATE_SLOT.get(raw_state.split(":", 1)[-1])
    if slot is None:  # недостижимо при живом StateFilter
        return
    await _put(state, _IMAGE_SLOTS[slot][0], file_id)
    await state.set_state(None)
    await message.answer("Картинка в черновике — посмотрите «👁 Превью» и нажмите «💾 Сохранить».")
    await _show(message, state, edit=False)


@router.message(StateFilter(QrCardTheme.bg_image, QrCardTheme.logo), F.photo)
async def qrct_photo_step(message: types.Message, state: FSMContext):
    await _accept_image(message, state, message.photo[-1].file_id)


@router.message(StateFilter(QrCardTheme.bg_image, QrCardTheme.logo), F.document)
async def qrct_document_step(message: types.Message, state: FSMContext):
    doc = message.document
    if (doc.mime_type or "") not in _IMAGE_MIMES:
        await message.answer(_BAD_FILE, reply_markup=_cancel_keyboard())
        return
    if (getattr(doc, "file_size", None) or 0) > _MAX_IMAGE_BYTES:
        await message.answer(_TOO_BIG, reply_markup=_cancel_keyboard())
        return
    await _accept_image(message, state, doc.file_id)


@router.message(StateFilter(QrCardTheme.bg_image, QrCardTheme.logo))
async def qrct_image_step_invalid(message: types.Message):
    await message.answer(_BAD_FILE, reply_markup=_cancel_keyboard())
