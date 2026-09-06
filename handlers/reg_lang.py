"""Phase 27 (27-04, LANG-01) — выбор языка делегатской анкеты кнопками: экран на /start,
переключатель в главном меню, запись `users.lang`.

D-06 («бот для людей»): язык НИКОГДА не угадывается молча по `language_code` клиента Telegram
и не спрашивается кодом — только двумя кнопками с человеческими подписями. `resolve_lang`
(`services/i18n.py`) отдаёт `"ask"`, когда клиент не на русском и выбор ещё не сохранён; этот
шов — единственное место, которое показывает экран выбора и пишет ответ в `users.lang`.

Язык **никогда не хранится в FSM** — только `users.lang`: `MemoryStorage` сбрасывается
перезапуском бота, и делегат посреди анкеты откатился бы на русский без предупреждения (в
проекте уже был инцидент с рестартом посреди регистрации, см. память владельца). Единственное,
что этот шов кладёт в FSM, — RAW-строка deep-link аргументов `/start` (`_DEEPLINK_RESUME_KEY`),
на время между показом экрана и тапом по кнопке: `cmd_start` вызывает `offer_language` ДО того,
как разбирает `args` на город/трек/реферера/метку, и без этой временной подстраховки delegate,
пришедший по рекламной ссылке с не-русским клиентом, терял бы атрибуцию кампании, ответив на
вопрос о языке. Это не язык — атрибуция кампании и так уже кладётся в FSM в нескольких других
местах этого файла (rereg-баннер, восстановление трека) тем же приёмом.

Регистрирует хендлеры на общий `router` владельца (`handlers.registration`), импортируется из
его хвоста, как reg_consent/reg_resume/reg_handoff.
"""
from types import SimpleNamespace

from aiogram import Bot, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import set_user_lang
from settings_schema import get_setting_typed
from services.i18n import delegate_lang
from handlers.registration import router

LANG_PICK_PREFIX = "lang_pick:"
LANG_MENU_BUTTON_TEXT = "🌐 Язык / Language"

# Двуязычный текст НАМЕРЕННО (не перевод друг друга) — в момент показа язык делегата ещё не
# известен (иначе вопроса бы не было), показывать текст на угаданном языке значило бы то самое
# угадывание, которое D-06 запрещает.
_LANG_PICK_TEXT = "Выберите язык анкеты / Choose the form language"
_LANG_CONFIRM = {"ru": "✅ Русский язык выбран.", "en": "✅ English selected."}

# Транзитный ключ FSM — ТОЛЬКО raw-строка deep-link аргументов /start на время экрана выбора
# языка (см. докстринг модуля). Не путать с языком — тот живёт исключительно в users.lang.
_DEEPLINK_RESUME_KEY = "_deeplink_resume_args"


def _lang_pick_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data=f"{LANG_PICK_PREFIX}ru"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data=f"{LANG_PICK_PREFIX}en"),
    ]])


async def offer_language(message: types.Message, state: FSMContext, raw_args: str | None = None) -> bool:
    """Вызывается из `cmd_start` ДО первого делегатского текста (party-closed/приветствие/
    город/вилка трека ниже). Показывает экран ТОЛЬКО когда module on + `delegate_lang_ask_on_start
    == "on"` + `resolve_lang(...) == "ask"` (клиент не на русском, выбор ещё не сохранён) —
    иначе `False` без единого сообщения, поток `cmd_start` не меняется ни на шаг.

    `raw_args` — сырая строка `command.args` исходного /start (deep-link) — сохраняется в FSM
    ТОЛЬКО на время ожидания тапа (см. докстринг модуля), чтобы `lang_pick_choose` мог
    реинвокнуть `cmd_start` с той же атрибуцией кампании, как если бы вопроса о языке не было.

    Возвращает `True`, если экран показан (вызывающий обязан `return` немедленно)."""
    if await get_setting_typed("delegate_lang_ask_on_start") != "on":
        return False
    # getattr, не прямой атрибут: множество Fake-message в существующих тестах (написанных до
    # этого плана) не заводят `language_code` вовсе — прямой доступ уронил бы их AttributeError,
    # хотя они никакого отношения к языку не имеют. Тот же fail-soft принцип, что везде в этом
    # файле: отсутствие данных — "ru", никогда не исключение.
    language_code = getattr(message.from_user, "language_code", None)
    lang = await delegate_lang(message.from_user.id, language_code)
    if lang != "ask":
        return False
    if raw_args:
        await state.update_data(**{_DEEPLINK_RESUME_KEY: raw_args})
    await message.answer(_LANG_PICK_TEXT, reply_markup=_lang_pick_kb())
    return True


@router.message(F.text == LANG_MENU_BUTTON_TEXT)
async def menu_lang_open(message: types.Message) -> None:
    """Переключатель в главном меню — делегат сам просит сменить язык в ЛЮБОЙ момент, поэтому
    `delegate_lang_ask_on_start` (решает, спрашивать ли САМИМ на /start) здесь не гейтит: только
    `delegate_lang_enabled` (кнопка и так не рисуется в меню при выключенном модуле —
    `keyboards/builders.py::get_main_menu_kb` — но старое сообщение в чате должно молчать, если
    менеджер выключил модуль ПОСЛЕ того, как кнопка уже была отправлена делегату)."""
    if await get_setting_typed("delegate_lang_enabled") != "on":
        return
    await message.answer(_LANG_PICK_TEXT, reply_markup=_lang_pick_kb())


@router.callback_query(F.data.startswith("lang_pick:"))
async def lang_pick_choose(callback: types.CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """T-27-04-02: закрытое множество `{"ru", "en"}` — токен приходит из НАШИХ ЖЕ кнопок,
    но приходит от клиента, поэтому проверяется как недоверенный ввод. Незнакомый код — алерт
    и выход, `set_user_lang` не зовётся вовсе (второй рубеж — сам `set_user_lang` тоже отверг
    бы чужое значение, но здесь дешевле остановиться до похода в БД)."""
    code = callback.data[len(LANG_PICK_PREFIX):]
    if code not in ("ru", "en"):
        await callback.answer()
        return
    # T-27-04-02/докстринг модуля: язык — ТОЛЬКО users.lang, никогда FSM.
    await set_user_lang(callback.from_user.id, code)
    await callback.answer(_LANG_CONFIRM[code])
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    data = await state.get_data()
    raw_args = data.get(_DEEPLINK_RESUME_KEY) if data else None
    if raw_args:
        await state.update_data(**{_DEEPLINK_RESUME_KEY: None})
    # Продолжаем ТОТ ЖЕ путь, что был бы без вопроса о языке — реинвоук cmd_start тем же
    # приёмом подмены from_user, что party_pick (T-05-01 deviation): callback.message.from_user
    # — это бот, а не тапнувший делегат. command — облегчённый дубль CommandObject (только
    # .args, единственное поле, которое читает cmd_start) с восстановленной deep-link строкой.
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    command = SimpleNamespace(args=raw_args) if raw_args else None
    from handlers.registration import cmd_start
    await cmd_start(tap_message, state, bot, command=command)
