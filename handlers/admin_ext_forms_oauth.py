"""Доступ бота к Яндекс Формам: «🔑 Ключи приложения Яндекса» и «🔑 Войти через Яндекс».

Шов на общий `handlers.admin.router` (своего Router нет, декораторы в одну строку). Права —
в handlers/admin_caps.py: `extf_*` и состояния ExtFormOAuth / ExtFormAppKeys — `settings`.

Секреты (client_secret, код подтверждения) проходят через чат: сообщение с ними удаляется
сразу после чтения, значение никогда не показывается и не пишется в лог. Токены лежат только
в БД (external_form_connections) и в тексты ответов не попадают."""
import logging
import re

from aiogram import Bot, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import ext_forms_db as xdb
from handlers.admin import router
from handlers.states import ExtFormAppKeys, ExtFormOAuth
from keyboards.builders import get_cancel_kb
from secret_redact import register_secret
from services import ext_forms_yandex as yx

logger = logging.getLogger(__name__)

_CLIENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ORG_HEADER = "X-Org-Id"
_REDIRECT_URI = "https://oauth.yandex.ru/verification_code"

_BAD_CLIENT_ID = ("Не похоже на ClientID — это 32 символа из цифр и букв a–f, "
                  "например 0123456789abcdef0123456789abcdef")
_BAD_CODE_FORMAT = ("Не понял — пришлите только код со страницы Яндекса, без других слов, "
                    "например jpfsigsxfj3nyrof или 1234567")
# Яндекс выдавал 7 цифр, теперь — 16 букв и цифр; принимаем оба вида, регистр не трогаем.
_CODE_RE = re.compile(r"[A-Za-z0-9]{6,32}")
_CODE_REJECTED = "Код не подошёл или устарел — нажмите «🔑 Войти через Яндекс» ещё раз"
_BAD_ORG = ("Не понял — пришлите ID организации: число из Яндекс 360 (например 1234567) "
            "или 20 букв и цифр из Yandex Cloud (например bpf1a2b3c4d5e6f7g8h9)")
_ORG_RE = re.compile(r"\d{1,20}|[a-z0-9]{20}")

_APPKEYS_HELP = (
    "Как завести приложение Яндекса:\n"
    "1. Нужен отдельный аккаунт Яндекса под бота, не личный (его пароль передать вместе с ботом).\n"
    "2. Зайдите с него на oauth.yandex.ru и нажмите «Создать приложение».\n"
    "3. Платформа — «Для доступа к API или отладки».\n"
    "4. В правах отметьте только «Просмотр настроек форм».\n"
    f"5. В Redirect URI впишите {_REDIRECT_URI}\n"
    "6. Сохраните и скопируйте ClientID и Client secret — их попросит бот."
)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _back_row() -> list[InlineKeyboardButton]:
    from handlers.admin_sections import back_button  # ленивый шов: цикл на уровне модуля
    return [back_button("admin_ext_forms")]


async def _delete_quietly(bot: Bot, message) -> None:
    """Секрет не должен оставаться в чате; сбой удаления (нет прав, старое сообщение) не фатален."""
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        logger.debug("не удалось удалить сообщение с секретом")


async def _keys_source() -> str:
    if await xdb.get_app_secret("yandex_client_id") and await xdb.get_app_secret("yandex_client_secret"):
        return "bot"
    return "env" if await yx.get_yandex_app_creds() else "none"


# ---------- ключи приложения ----------

@router.callback_query(F.data == "extf_appkeys")
async def extf_appkeys(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    source = await _keys_source()
    status = {
        "bot": "Ключи заданы в боте.",
        "env": "Ключи взяты из файла настроек сервера. Можно задать свои — они будут главнее.",
        "none": "Ключи не заданы — без них вход через Яндекс не работает.",
    }[source]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✏️ Ввести ключи", "extf_appkeys_edit")],
        _back_row(),
    ])
    await callback.message.edit_text(f"🔑 Ключи приложения Яндекса\n\n{status}\n\n{_APPKEYS_HELP}",
                                     reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "extf_appkeys_edit")
async def extf_appkeys_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ExtFormAppKeys.client_id)
    await callback.message.answer(
        "Пришлите ClientID приложения — 32 символа из цифр и букв, "
        "например 0123456789abcdef0123456789abcdef",
        reply_markup=get_cancel_kb())
    await callback.answer()


@router.message(StateFilter(ExtFormAppKeys), F.text == "Отмена")
async def extf_appkeys_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. Ключи не менялись.")


@router.message(ExtFormAppKeys.client_id, F.text)
async def extf_appkeys_client_id(message: types.Message, state: FSMContext):
    value = (message.text or "").strip().lower()
    if not _CLIENT_ID_RE.match(value):
        await message.answer(_BAD_CLIENT_ID)
        return
    await state.update_data(client_id=value)
    await state.set_state(ExtFormAppKeys.client_secret)
    await message.answer("Теперь пришлите Client secret. Я удалю сообщение сразу после сохранения.")


@router.message(ExtFormAppKeys.client_secret, F.text)
async def extf_appkeys_client_secret(message: types.Message, state: FSMContext, bot: Bot):
    secret = (message.text or "").strip()
    await _delete_quietly(bot, message)
    if not _CLIENT_ID_RE.match(secret.lower()):
        await message.answer("Не похоже на Client secret — это 32 символа из цифр и букв a–f. "
                             "Скопируйте его со страницы приложения и пришлите ещё раз.")
        return
    secret = secret.lower()
    data = await state.get_data()
    by = message.from_user.id if message.from_user else None
    await xdb.set_app_secret("yandex_client_id", data["client_id"], by)
    await xdb.set_app_secret("yandex_client_secret", secret, by)
    register_secret(secret)
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("🔑 Войти через Яндекс", "extf_oauth")], _back_row()])
    await message.answer("Ключи сохранены. Если вход уже был — войдите через Яндекс заново",
                         reply_markup=kb)


# ---------- вход через Яндекс ----------

def normalize_code(text: str | None) -> str | None:
    code = re.sub(r"\s+", "", text or "")
    return code if _CODE_RE.fullmatch(code) else None


@router.callback_query(F.data == "extf_oauth")
async def extf_oauth(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    creds = await yx.get_yandex_app_creds()
    if not creds:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_btn("🔑 Ключи приложения Яндекса", "extf_appkeys")], _back_row()])
        await callback.message.edit_text(
            "Сначала задайте ключи приложения Яндекса — без них вход не работает.", reply_markup=kb)
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть Яндекс", url=yx.authorize_url(creds[0]))],
        _back_row(),
    ])
    await state.set_state(ExtFormOAuth.code)
    await callback.message.edit_text(
        "Вход через Яндекс — три шага:\n"
        "1. Нажмите «Открыть Яндекс» и войдите под аккаунтом бота (не личным).\n"
        "2. Нажмите «Разрешить».\n"
        "3. Яндекс покажет код подтверждения — пришлите его сюда. Я удалю сообщение с кодом.",
        reply_markup=kb)
    await callback.message.answer("Жду код. Передумали — нажмите «Отмена».",
                                  reply_markup=get_cancel_kb())
    await callback.answer()


@router.message(StateFilter(ExtFormOAuth), F.text == "Отмена")
async def extf_oauth_cancel(message: types.Message, state: FSMContext):
    at_org_step = await state.get_state() == ExtFormOAuth.org_id.state
    await state.clear()
    if at_org_step:
        # К этому шагу токены уже сохранены — «не менялось» было бы неправдой.
        await message.answer(
            "Вход выполнен без организации. Если формы лежат в организации Яндекс 360, "
            "войдите ещё раз и укажите её ID.")
        return
    await message.answer("Отменено. Подключение не менялось.")


@router.message(ExtFormOAuth.code, F.text)
async def extf_oauth_code(message: types.Message, state: FSMContext, bot: Bot):
    code = normalize_code(message.text)
    if code is None:
        await message.answer(_BAD_CODE_FORMAT)
        return
    await _delete_quietly(bot, message)
    try:
        tokens = await yx.exchange_code(code)
    except yx.YandexApiError as exc:
        await state.clear()
        if exc.reason == "bad_code":
            await message.answer(_CODE_REJECTED)
        elif exc.reason == "no_app_keys":
            await message.answer("Сначала задайте ключи приложения Яндекса: "
                                 "«🔑 Ключи приложения Яндекса».")
        else:
            await message.answer("Яндекс сейчас не отвечает. Попробуйте через пару минут: "
                                 "нажмите «🔑 Войти через Яндекс» ещё раз.")
        return
    by = message.from_user.id if message.from_user else None
    # Повторный вход не должен затирать ID организации: формы организации тогда начали бы
    # получать 403/404, пока менеджер не введёт его заново.
    prev = await xdb.get_yandex_connection()
    prev_org = prev.get("org_id") if prev else None
    await xdb.upsert_yandex_connection(
        org_id=prev_org, org_header=(prev.get("org_header") if prev else None) or _ORG_HEADER,
        access_token=tokens["access_token"], refresh_token=tokens.get("refresh_token"),
        expires_at=tokens.get("expires_at"), by=by)
    if prev_org:
        await state.clear()
        await message.answer(
            "✅ Вход выполнен. Организация осталась прежней, формы продолжат работать.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[_back_row()]))
        return
    await state.set_state(ExtFormOAuth.org_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Формы в личном аккаунте, без организации", "extf_oauth_noorg")]])
    await message.answer(
        "Пришлите ID организации, в которой лежат формы:\n"
        "• Яндекс 360 — число, например 1234567: tracker.yandex.ru → Администрирование → "
        "Организации → поле «Идентификатор»;\n"
        "• Yandex Cloud — 20 букв и цифр, например bpf1a2b3c4d5e6f7g8h9: "
        "org.yandex.cloud → Организация → Идентификатор.\n"
        "Формы в личном аккаунте бот прочитать не сможет — так устроен Яндекс.", reply_markup=kb)


async def _finish_org(target_message, org_id: str | None, by: int | None) -> bool:
    conn = await xdb.get_yandex_connection()
    if conn is None:
        await target_message.answer(_CODE_REJECTED)
        return True
    header = conn.get("org_header") or _ORG_HEADER
    if org_id:
        try:
            header = await yx.detect_org_header(conn, org_id)
        except yx.YandexApiError:
            await target_message.answer("Яндекс сейчас не отвечает — пришлите ID организации "
                                        "ещё раз через пару минут.")
            return False
        if header is None:
            await target_message.answer(
                "Яндекс не узнал эту организацию для аккаунта, под которым вы вошли. Проверьте ID "
                "и что аккаунт бота состоит в организации, и пришлите ID ещё раз.")
            return False
    await xdb.upsert_yandex_connection(
        org_id=org_id, org_header=header,
        access_token=conn["access_token"], refresh_token=conn.get("refresh_token"),
        expires_at=conn.get("expires_at"), by=by)
    kb = InlineKeyboardMarkup(inline_keyboard=[_back_row()])
    await target_message.answer("✅ Доступ к Яндекс Формам подключён", reply_markup=kb)
    return True


@router.message(ExtFormOAuth.org_id, F.text)
async def extf_oauth_org(message: types.Message, state: FSMContext):
    text = (message.text or "").strip().lower()
    if not _ORG_RE.fullmatch(text):
        await message.answer(_BAD_ORG)
        return
    if await _finish_org(message, text, message.from_user.id if message.from_user else None):
        await state.clear()


@router.callback_query(F.data == "extf_oauth_noorg")
async def extf_oauth_noorg(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await _finish_org(callback.message, None, callback.from_user.id)
    await callback.answer()
