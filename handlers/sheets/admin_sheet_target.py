"""«📊 Данные → 🔗 Какая таблица»: суперадмин сам подключает Google-таблицу события.

Вставляет ссылку из адресной строки (или голый ID) — бот вытаскивает ID, открывает таблицу
сервисным аккаунтом и только после этого спрашивает «переключить?». Нет доступа — говорит, кого
добавить в «Настройки доступа». Значение ложится в `bot_settings.google_sheet_id`, читает его
единственный резолвер `services/sheet_target.py` (пусто — таблица из `.env`, как раньше).

Только суперадмин (`config.ADMIN_IDS`): смена таблицы уводит ВСЕ записи бота в другой файл.
Гейт повторён в каждом хендлере — стейл-кнопка в чате живёт вечно (тот же приём, что у
«🔄 Новый сезон», handlers/cities/admin_cities.py). По той же причине «✅ Переключить» перепроверяет
доступ: между проверкой и нажатием мог пройти день. Форма шва — `from handlers.admin import
router`, импорт из хвоста handlers/admin.py."""
import asyncio
import html
import logging

from aiogram import F, types
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from config import config
from handlers.admin import router
from handlers.states import SheetTarget
from keyboards.builders import get_cancel_kb
from services import sheet_target
from services.settings.audit import delete_setting_by_admin, set_setting_by_admin

logger = logging.getLogger(__name__)

ONLY_SUPERADMIN = "Таблицу события меняет только суперадмин."

ASK_TEXT = (
    "Пришлите ссылку на Google-таблицу события — скопируйте её из адресной строки браузера, "
    "например:\nhttps://docs.google.com/spreadsheets/d/1AbC…/edit\n\n"
    "Бот сам проверит, что может в неё писать. Передумали — «Отмена»."
)
BAD_REF_TEXT = (
    "Не понял, что это за таблица. Пришлите ссылку из адресной строки браузера — она "
    "начинается с https://docs.google.com/spreadsheets/d/… Или нажмите «Отмена»."
)
NO_KEY_TEXT = (
    "На сервере бота нет ключа сервисного аккаунта Google — без него бот не может писать ни в "
    "одну таблицу. Это настраивает @qleafye. Таблицу не поменял."
)
CHECK_TIMEOUT_S = 20
TIMEOUT_TEXT = (
    f"Google не ответил за {CHECK_TIMEOUT_S} секунд — проверить таблицу не получилось. "
    "Попробуйте ещё раз: «🔗 Какая таблица» → «✏️ Указать другую таблицу». Таблицу не поменял."
)
GOOGLE_ERROR_TEXT = (
    "Google сейчас не ответил — проверить таблицу не получилось. Попробуйте прислать ссылку "
    "ещё раз через минуту. Таблицу не поменял."
)


def _is_superadmin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def no_access_text(email: str | None) -> str:
    """Что сделать человеку, если сервисный аккаунт таблицу не открыл."""
    who = f"<code>{html.escape(email)}</code>" if email else "адрес сервисного аккаунта бота (его знает @qleafye)"
    return (
        "⛔ Бот не может открыть эту таблицу — у него нет доступа (или ссылка ведёт не туда).\n\n"
        "Откройте таблицу → «Настройки доступа» (кнопка справа вверху) → добавьте "
        f"{who} с ролью «Редактор» → «Отправить». Потом пришлите ссылку сюда ещё раз.\n\n"
        "Таблицу не поменял."
    )


async def _in_bot() -> str | None:
    return await asyncio.to_thread(sheet_target.bot_sheet_id)  # чтение базы — не на цикле событий


async def _check(spreadsheet_id: str) -> tuple[str, str | None]:
    """Проверка доступа с потолком по времени: зависший Google не держит человека вечно."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(sheet_target.check_access_sync, spreadsheet_id), CHECK_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return "timeout", None


def _failure_text(verdict: str) -> str:
    if verdict == "no_access":
        return no_access_text(sheet_target.service_account_email())
    return {"timeout": TIMEOUT_TEXT, "no_key": NO_KEY_TEXT}.get(verdict, GOOGLE_ERROR_TEXT)


async def screen_text(in_bot: str | None) -> str:
    current = sheet_target.sheet_id()
    email = sheet_target.service_account_email()
    lines = ["🔗 <b>Google-таблица события</b>", ""]
    if current:
        lines.append(f'Бот пишет в <a href="{html.escape(sheet_target.sheet_url(current))}">эту таблицу</a>.')
        lines.append("Задана здесь, в боте." if in_bot else "Задана при установке бота.")
    else:
        lines.append("Таблица не подключена — регистрации пишутся только в базу бота.")
    lines.append("")
    if email:
        lines.append(
            f"У бота должен быть доступ «Редактор»: в таблице «Настройки доступа» добавьте "
            f"<code>{html.escape(email)}</code>."
        )
    else:
        lines.append("Ключа сервисного аккаунта на сервере нет — это настраивает @qleafye.")
    lines.append("")
    lines.append(
        "Новая таблица? Вкладки бот найдёт по именам из «📄 Вкладки таблицы», а каких нет — "
        "создаст при первой записи. Перенести уже собранные заявки — «♻️ Пересобрать таблицу»."
    )
    return "\n".join(lines)


def screen_kb(in_bot: str | None) -> InlineKeyboardMarkup:
    from handlers.settings.admin_sections import back_button  # ленивый шов: цикл на уровне модуля

    rows = [[InlineKeyboardButton(text="✏️ Указать другую таблицу", callback_data="sheet_target_set")]]
    if in_bot:
        label = ("↩️ Вернуть таблицу из установки" if sheet_target.env_sheet_id()
                 else "🚫 Отключить таблицу")
        rows.append([InlineKeyboardButton(text=label, callback_data="sheet_target_env")])
    rows.append([back_button("admin_sheet_target")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_screen(message: types.Message, in_bot: str | None, prefix: str = "") -> None:
    await message.answer(prefix + await screen_text(in_bot), parse_mode="HTML",
                         reply_markup=screen_kb(in_bot), disable_web_page_preview=True)


@router.callback_query(F.data == "admin_sheet_target")
async def sheet_target_screen(callback: types.CallbackQuery, state: FSMContext):
    if not _is_superadmin(callback.from_user.id):
        await callback.answer(ONLY_SUPERADMIN, show_alert=True)
        return
    await state.set_state(None)
    await _send_screen(callback.message, await _in_bot())
    await callback.answer()


@router.callback_query(F.data == "sheet_target_set")
async def sheet_target_set(callback: types.CallbackQuery, state: FSMContext):
    if not _is_superadmin(callback.from_user.id):
        await callback.answer(ONLY_SUPERADMIN, show_alert=True)
        return
    await state.set_state(SheetTarget.waiting_ref)
    await state.update_data(sheet_target_new=None)
    await callback.message.answer(ASK_TEXT, reply_markup=get_cancel_kb(), disable_web_page_preview=True)
    await callback.answer()


@router.message(StateFilter(SheetTarget), Command("cancel"))
@router.message(StateFilter(SheetTarget), F.text == "Отмена")
async def sheet_target_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено. Таблица прежняя.", reply_markup=ReplyKeyboardRemove())


@router.message(SheetTarget.waiting_ref)
async def sheet_target_receive(message: types.Message, state: FSMContext):
    if not _is_superadmin(message.from_user.id):
        await state.set_state(None)
        await message.answer(ONLY_SUPERADMIN, reply_markup=ReplyKeyboardRemove())
        return
    if (message.text or "").startswith("/"):
        # Команда, а не ссылка: человек ушёл из мастера — отпускаем его туда, куда он шёл.
        await state.set_state(None)
        raise SkipHandler()
    new_id = sheet_target.parse_sheet_ref(message.text)
    if not new_id:
        await message.answer(BAD_REF_TEXT, disable_web_page_preview=True)
        return
    if new_id == sheet_target.sheet_id():
        await state.set_state(None)
        await message.answer("Бот уже пишет в эту таблицу — ничего не меняю.", reply_markup=ReplyKeyboardRemove())
        return
    await message.answer("⏳ Проверяю доступ к таблице…", reply_markup=ReplyKeyboardRemove())
    verdict, detail = await _check(new_id)
    if verdict in ("timeout", "no_key"):
        await state.set_state(None)
        await message.answer(_failure_text(verdict))
        return
    if verdict != "ok":
        if verdict == "error":
            logger.warning(f"sheet_target: проверка таблицы {new_id} не удалась: {detail}")
        # Можно открыть доступ и прислать ссылку ещё раз — мастер ждёт дальше.
        await message.answer(_failure_text(verdict), parse_mode="HTML", reply_markup=get_cancel_kb())
        return
    await state.set_state(None)
    await state.update_data(sheet_target_new=new_id)
    title = html.escape(detail or "без названия")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Переключить", callback_data="sheet_target_apply")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_sheet_target")],
    ])
    await message.answer(
        f"✅ Доступ есть: «{title}».\n\nПереключить бота на эту таблицу? Новые записи пойдут "
        "туда; прежняя таблица останется как есть, из неё ничего не удаляется.",
        parse_mode="HTML", reply_markup=kb,
    )


@router.callback_query(F.data == "sheet_target_apply")
async def sheet_target_apply(callback: types.CallbackQuery, state: FSMContext):
    if not _is_superadmin(callback.from_user.id):
        await callback.answer(ONLY_SUPERADMIN, show_alert=True)
        return
    new_id = (await state.get_data()).get("sheet_target_new")
    if not new_id:
        await callback.answer("Кнопка устарела — пришлите ссылку ещё раз.", show_alert=True)
        return
    await callback.answer("⏳ Проверяю доступ…")
    verdict, _title = await _check(new_id)  # доступ могли отозвать после первой проверки
    if verdict != "ok":
        await state.update_data(sheet_target_new=None)
        await callback.message.answer(_failure_text(verdict), parse_mode="HTML")
        return
    await set_setting_by_admin(callback.from_user.id, sheet_target.SETTING_KEY, new_id)
    await state.update_data(sheet_target_new=None)
    sheet_target.remember(new_id)
    sheet_target.reset_client_caches()
    await _send_screen(callback.message, new_id, "✅ Готово: бот пишет в новую таблицу.\n\n")


@router.callback_query(F.data == "sheet_target_env")
async def sheet_target_env(callback: types.CallbackQuery):
    if not _is_superadmin(callback.from_user.id):
        await callback.answer(ONLY_SUPERADMIN, show_alert=True)
        return
    if sheet_target.env_sheet_id():
        text = ("↩️ Вернуть таблицу, заданную при установке бота?\n\nНовые записи пойдут в неё; "
                "таблица, заданная здесь, останется как есть, из неё ничего не удаляется.")
        go = "↩️ Вернуть"
    else:
        text = ("🚫 Отключить таблицу?\n\nЗаявки продолжат сохраняться в базу бота, но в "
                "Google-таблицу перестанут попадать. Сама таблица останется как есть.")
        go = "🚫 Отключить"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=go, callback_data="sheet_target_env_go")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_sheet_target")],
    ])
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "sheet_target_env_go")
async def sheet_target_env_go(callback: types.CallbackQuery):
    if not _is_superadmin(callback.from_user.id):
        await callback.answer(ONLY_SUPERADMIN, show_alert=True)
        return
    await delete_setting_by_admin(callback.from_user.id, sheet_target.SETTING_KEY)
    sheet_target.remember(None)
    sheet_target.reset_client_caches()
    await _send_screen(callback.message, None, "Готово.\n\n")
    await callback.answer()
