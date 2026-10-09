"""Квик 260911-mx6 (UAT-SEED-01..04): скрытая самообслуживаемая команда `/uat` — тестер
приёмки одной командой ставит СВОЙ аккаунт в нужное состояние (шесть кнопок) и выдаёт себе
роль (кнопками), без ручного заполнения анкеты в четырнадцать вопросов и без похода
менеджера в админку ради «одобрить заявку тестеру».

Почему самообслуживание, а не «сбросить чужого делегата»: FSM бота живёт в MemoryStorage и
`state.clear()` сбрасывает ТОЛЬКО того, кто прислал апдейт. Если бы `/uat` мог целить в
чужой telegram_id, человек посреди своей анкеты завис бы навсегда — команда поэтому жёстко
работает по `event.from_user.id`, второго id в callback_data нет вовсе.

Почему свой `Router()`, а не шов на `handlers.admin.router` (D-1, отклонение от буквальной
формулировки задачи — обязательное по безопасности): `CapabilityMiddleware` висит на
observers `admin.router` и работает deny-by-default — тестер-делегат (без ролей менеджера)
до хендлера на `admin.router` не дошёл бы вообще. Хуже: выбрав в пикере «Делегат — без
ролей», человек снял бы с себя staff-роль и навсегда потерял доступ к команде через
`admin.router`. Поэтому `router = Router()` — свой, независимый от `admin_caps`.

Почему он включается ПЕРВЫМ в `main.py` (раньше `admin.router`): побочный выигрыш — `/uat`,
набранный посреди анкеты, перехватывается до state-хендлеров `registration.router`. Иначе
тестер, зависший посреди чужого FSM-состояния, не смог бы сбросить себя сам — а это главный
сценарий использования команды.

Команда сознательно НЕ выведена ни в `/admin`, ни в `admin_sections.SECTIONS`, ни в меню, ни
в подсказках — только по точному имени `/uat`, и только тому, кто внесён в реестровый список
тестеров при включённом тумблере (два независимых слоя гейта, оба deny-by-default, оба
перепроверяются ВНУТРИ каждого хендлера ниже — тот же приём, что `admin_purge.py`
перепроверяет `config.ADMIN_IDS` в каждом своём хендлере).

Модуль назван `uat_seed.py`, НЕ `admin_*.py` — иначе попал бы под glob `handlers/admin*.py`,
которым `_admin_module_files()`-стиль сторожей собирает god-файлы админки."""
import html
import logging
import re

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.db import (
    add_staff,
    add_user,
    count_user_footprint,
    find_user_id_by_username,
    get_or_create_checkin_token,
    get_staff_roles,
    mark_reg_started,
    purge_miniapp_outbox_for_user,
    purge_user,
    record_checkin,
    record_user_consent,
    remove_staff,
    set_staff_city,
    set_user_status,
    upsert_reg_draft,
    username_needle,
)
from cities import (
    all_cities,
    cities_module_on,
    city_label,
    default_city_code,
    enabled_cities,
    normalize_city,
)
from handlers import admin_caps
from handlers.admin_purge import _footprint_lines
from reg_engine import SHORT_TRACK, answer_columns, columns_for_step, consent_entries
from services.checkin import ENTRY_POINT
from services.scheduler import cancel_payment_reminders
from services.timeutil import msk_now
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

router = Router()

_ID_RE = re.compile(r"^-?\d+$")

# Экран 1 — шесть состояний, в порядке кнопок.
_STATES: tuple[tuple[str, str], ...] = (
    ("fresh", "🆕 Новичок — анкеты нет"),
    ("draft", "✏️ Черновик — стою на шаге резюме"),
    ("pending", "📨 Заявка отправлена — ждёт проверки"),
    ("approved", "✅ Одобрен"),
    ("rejected", "🚫 Отклонён"),
    ("short", "⚡ Короткая (акционная) форма"),
)
# Приёмка регионального форума (СПб/Тюмень, 03.10): состояния, где важен город форума. Город —
# отдельный шаг кнопками сразу после выбора состояния; заявка ложится в текущий сезон
# (`event_season`) с `event_city` выбранного города — ровно то, по чему гейты чек-ина, SOS и
# программы решают, «свой» ли это делегат. Значение — статус заявки, который получит строка.
_FORUM_STATES: tuple[tuple[str, str], ...] = (
    ("fappr", "🎟 Одобрен на форум — с QR-пропуском"),
    ("farr", "🚪 Пришёл на форум — вход отмечен сегодня"),
    ("fpend", "📨 Заявка на форум — ждёт проверки"),
    ("frej", "🚫 Заявка на форум — отклонена"),
)
_FORUM_STATE_STATUS: dict[str, str] = {
    "fappr": "approved", "farr": "approved", "fpend": "pending", "frej": "rejected",
}
_STATE_LABELS: dict[str, str] = dict(_STATES + _FORUM_STATES)

# Экран 2 — роли, в порядке кнопок. Подписи здесь — для делегата-самообслуживания
# (короче и понятнее, чем "🛂 Менеджер регистраций"); подписи ВНУТРИ карточки/итога ниже
# берутся из admin_caps.ROLES[...]["label"] — второй копии подписей роли не заводим.
#
# Три форумные роли — НЕ новые роли в admin_caps, а те же записи ROLES, которыми форум реально
# пользуется, плюс привязка к городу (`staff.city`, `set_staff_city`):
# - волонтёр входа — `volunteer` (право `checkin`: сканер и «✅ Отметки на форуме»), как по ссылке-приглашению;
# - дежурный SOS — `reg_manager`: SOS-обращения уходят держателям `moderate_reg` своего города
#   (`services/sos.py` -> `capability_holders("moderate_reg", city=...)`);
# - менеджер форума города — `reg_manager` + `reg_volunteer`: «🎪 Форум: функции», SOS, заявки
#   (moderate_reg) плюс вход и одобрение на месте (checkin, checkin_approve).
_ROLES_PICK: tuple[tuple[str, str], ...] = (
    ("none", "Делегат — без ролей"),
    ("reg", "Менеджер заявок"),
    ("game", "Менеджер геймификации"),
    ("both", "Оба менеджера"),
    ("vol", "🎗 Волонтёр входа (сканер)"),
    ("sos", "🆘 Дежурный SOS"),
    ("fmgr", "🎪 Менеджер форума"),
)
_ROLE_PICK_LABELS: dict[str, str] = dict(_ROLES_PICK)
_ROLE_CODE_TO_ROLES: dict[str, tuple[str, ...]] = {
    "none": (),
    "reg": ("reg_manager",),
    "game": ("game_manager",),
    "both": ("reg_manager", "game_manager"),
    "vol": ("volunteer",),
    "sos": ("reg_manager",),
    "fmgr": ("reg_manager", "reg_volunteer"),
}
# Роли, привязанные к городу форума, и что они дают — человеческими словами для карточки.
_CITY_ROLE_HINTS: dict[str, str] = {
    "vol": "сканирует QR на входе (сканер и раздел «✅ Отметки на форуме») — только делегатов своего города",
    "sos": (
        "получает SOS-обращения делегатов своего города и отвечает на них; это права "
        "менеджера заявок, поэтому очередь заявок тоже откроется"
    ),
    "fmgr": (
        "«🎪 Форум: функции», SOS, заявки, отметка входа и одобрение на месте — "
        "всё в своём городе"
    ),
}

# Один словарь правдоподобных, явно тестовых ответов по колонкам users — второй копии этих
# значений в модуле нет. Ключи обязаны лежать в reg_engine.answer_columns() (сторог-тест).
#
# `full_name` лежит здесь же (не только в non-draft-ветке `_seed_state`), потому что в
# реальном флоу ФИО спрашивается ДО REG_FLOW, до шага "резюме" (handlers/registration.py,
# `cmd_start`/`_ask_full_name`, ~1633-1638) — засеянный черновик на шаге "resume" без ФИО в
# `answers` изображал состояние, недостижимое в реальной анкете (стенд-инцидент 15.09:
# `registration_complete … name=None`). `full_name` не входит в `columns_for_step("resume")`,
# поэтому фильтр черновика в `_seed_state` его не срежет.
_SEED_ANSWERS: dict[str, object] = {
    "full_name": "Тестовый Делегат (приёмка)",
    "age": "20",
    "phone": "+7 900 000-00-00",
    "vk_username": "vk.com/uat_test_delegate",
    "city": "Москва",
    "education_status": "Студент",
    "course": "3",
    "university": "Тестовый университет (приёмка)",
    "study_field": "IT",
    "goal": "Нетворкинг",
    "formats": "Очно",
    "expectations": "Тестовые ожидания (приёмка)",
    "source": "Соцсети АЙСЕК",
    "email": "uat.delegate@example.com",
    "resume_text": "Тестовое резюме (приёмка)",
}
assert set(_SEED_ANSWERS) <= set(answer_columns()), (
    "_SEED_ANSWERS содержит колонку вне reg_engine.answer_columns() — опечатка в имени"
)


async def _uat_open() -> bool:
    return await get_setting_typed("uat_seed_enabled") == "on"


async def _tester_ids_ok(user) -> bool:
    """D-3: сверка и по числовому id, и по @нику из самого апдейта. `find_user_id_by_username`
    оставлен (задача требует), но как единственный путь не годится — после первого же сброса
    делегата строк `users`/`reg_started` у него нет, и вторая команда перестала бы работать."""
    testers = await get_setting_typed("uat_seed_testers")
    if not testers:
        return False
    uid = user.id
    uname_needle = username_needle(getattr(user, "username", None))
    for raw in testers:
        entry = str(raw).strip()
        if not entry:
            continue
        if _ID_RE.match(entry):
            if int(entry) == uid:
                return True
            continue
        entry_needle = username_needle(entry)
        if entry_needle is None:
            continue
        if uname_needle is not None and entry_needle.lower() == uname_needle.lower():
            return True
        if await find_user_id_by_username(entry_needle) == uid:
            return True
    return False


async def _gate_ok(user) -> bool:
    return await _uat_open() and await _tester_ids_ok(user)


def _needs_city(state_code: str, role_code: str | None = None) -> bool:
    return state_code in _FORUM_STATE_STATUS or role_code in _CITY_ROLE_HINTS


def _parse_cb(data: str) -> tuple[str, str | None, str | None]:
    """`префикс:состояние[:роль[:город]]` -> (состояние, роль, город). Старая двухчастная форма
    `uat_go:{состояние}:{роль}` (без города) по-прежнему разбирается."""
    parts = data.split(":")
    state_code = parts[1] if len(parts) > 1 else ""
    role_code = parts[2] if len(parts) > 2 and parts[2] else None
    city_code = parts[3] if len(parts) > 3 and parts[3] else None
    return state_code, role_code, city_code


def _known_city(code: str | None) -> bool:
    return bool(code) and code in {c["code"] for c in all_cities()}


async def _forum_cities() -> list[dict]:
    """Города для шага выбора: включённые, если модуль городов включён; иначе (или если
    включённых нет) — весь справочник, чтобы шаг никогда не оказался пустым."""
    rows = await enabled_cities() if await cities_module_on() else []
    return rows or all_cities()


async def _cities_keyboard(prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=await city_label(c["code"]), callback_data=f"{prefix}:{c['code']}")]
        for c in await _forum_cities()
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="uat_no")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _role_display(role_code: str, city_name: str | None = None) -> str:
    roles = _ROLE_CODE_TO_ROLES[role_code]
    if not roles:
        return "Делегат — без ролей"
    if role_code in _CITY_ROLE_HINTS:
        return f"{_ROLE_PICK_LABELS[role_code]} — {city_name or 'город не выбран'}"
    return ", ".join(admin_caps.ROLES[r]["label"] for r in roles)


def _state_display(state_code: str, city_name: str | None) -> str:
    label = _STATE_LABELS[state_code]
    return f"{label} — {city_name}" if state_code in _FORUM_STATE_STATUS and city_name else label


def _states_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"uat_st:{code}")]
        for code, label in _STATES + _FORUM_STATES
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="uat_no")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _roles_keyboard(
    state_code: str, city_code: str | None = None, city_name: str | None = None,
) -> InlineKeyboardMarkup:
    rows = []
    for code, label in _ROLES_PICK:
        data = f"uat_role:{state_code}:{code}"
        if city_code:
            data += f":{city_code}"
        if code in _CITY_ROLE_HINTS:
            label = f"{label} — {city_name}" if city_name else f"{label} — выбрать город"
        rows.append([InlineKeyboardButton(text=label, callback_data=data)])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="uat_no")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Права, ради которых форумная роль выдаётся; всё сверх этого карточка называет отдельно.
_FORUM_CAPS = frozenset({"moderate_reg", "checkin", "checkin_approve"})


async def _extra_caps_notes(roles: tuple[str, ...]) -> list[str]:
    """Форумная роль — это обычная роль из «👥 Роли и доступы» целиком: у «🛂 Менеджер
    регистраций» по умолчанию есть ещё модерация чеков оплаты. Говорим об этом прямо, по
    текущим правам ролей (менеджер мог их поменять)."""
    lines = []
    for role in roles:
        extra = [c for c in await get_setting_typed(admin_caps.role_caps_key(role)) or []
                 if c in admin_caps.CAP_LABELS and c not in _FORUM_CAPS]
        if extra:
            lines.append(
                f"Роль «{admin_caps.ROLES[role]['label']}» даёт ещё: "
                f"{', '.join(admin_caps.CAP_LABELS[c] for c in extra)} — так настроены её права "
                "в «👥 Роли и доступы»."
            )
    return lines


async def _role_notes(tid: int, role_code: str) -> list[str]:
    """Честные строки карточки про роли: что даст форумная роль, какие роли выключены
    менеджером (тогда прав не даст) и какие из нынешних ролей человека снимутся."""
    lines: list[str] = []
    wanted = _ROLE_CODE_TO_ROLES[role_code]
    if role_code in _CITY_ROLE_HINTS:
        lines.append(f"Что даст роль: {_CITY_ROLE_HINTS[role_code]}.")
        lines.extend(await _extra_caps_notes(wanted))
    for role in wanted:
        if await get_setting_typed(admin_caps.role_enabled_key(role)) != "on":
            lines.append(
                f"⚠️ Роль «{admin_caps.ROLES[role]['label']}» выключена в «👥 Роли и доступы» — "
                "прав она сейчас не даст."
            )
    dropped = [r for r in await get_staff_roles(tid) if r in admin_caps.ROLES and r not in wanted]
    if dropped:
        lines.append(
            "Снимутся ваши роли: " + ", ".join(admin_caps.ROLES[r]["label"] for r in dropped) + "."
        )
    return lines


def _forum_state_notes(state_code: str, *, done: bool = False) -> list[str]:
    if state_code == "fappr":
        return [("Выпущен" if done else "Будет выпущен") + " QR-пропуск — «🎟 Мой QR» его покажет."]
    if state_code == "farr":
        return [
            ("Выпущен QR-пропуск и поставлена" if done else "Будет выпущен QR-пропуск и поставлена")
            + " отметка входа на сегодня — как будто волонтёр отсканировал QR. "
            "Приветствие первого входа не придёт."
        ]
    return []


async def _seed_consent(tid: int) -> None:
    """Стенд-инцидент 15.09: реальный флоу проходит согласие ДО ФИО, ДО REG_FLOW
    (`handlers/registration.py` ~1633-1638) — любое состояние с черновиком/заявкой обязано
    нести ту же подпись, иначе сеялка изображает пользователя, недостижимого в реальной
    анкете. Пишем через `database.db.record_user_consent` (та же функция, что и
    `handlers/reg_consent.py::consent_renew_accept`), без сырого SQL; версия — дефолтная
    (текущая `consent_version`), как и у настоящей подписи. Модуль согласий выключен —
    ничего не пишем (`outstanding_consents` в реальном флоу тоже промолчал бы)."""
    if await get_setting_typed("consent_enabled") != "on":
        return
    for _label, key in await consent_entries():
        await record_user_consent(tid, key, raw_button="UAT-сеялка (приёмка)")


async def _seed_event_city() -> str | None:
    """Квик 260916 (UAT-SEED-05): реальный делегат либо проходит развилку города и ВСЕГДА
    получает валидный `event_city` (модуль включён — `handlers/registration.py`'s city fork
    не пропускает дальше без выбора), либо `event_city` вовсе не участвует в резолве настроек
    (модуль выключен — `cities.get_setting_typed_for_city` тогда читает общее значение
    независимо от `event_city`, см. `services/reg_finalize.py::finalize_data`). Сеялка раньше
    всегда оставляла `event_city=NULL` при включённых городах — недостижимое для реальной
    анкеты состояние (стенд 16.09: тестер получил общий `approve_text` вместо городского).

    Модуль включён -> первый ВКЛЮЧЁННЫЙ город (`cities.enabled_cities()`, порядок как в
    `CITIES`), с фолбэком на `default_city_code()`, если почему-то ни одного включённого нет.
    Модуль выключен -> `None`, как у настоящего делегата в этом режиме."""
    if not await cities_module_on():
        return None
    codes = [c["code"] for c in await enabled_cities()]
    return normalize_city(codes[0]) if codes else default_city_code()


def _home_city_name(forum_label: str) -> str:
    """Подпись города ФОРУМА -> название города для `users.city` (где делегат ЖИВЁТ).

    Приёмка 09.10: подпись форума несёт дату («Санкт-Петербург, 3 октября»), а `users.city`
    питает подсказки вопроса «Выбери свой город» (`services.lookup.top_chips`) — засеянная
    подпись с датой вылезала у тестеров первой кнопкой. Дата отделена запятой."""
    return forum_label.split(",", 1)[0].strip() or forum_label


async def _seed_state(
    tid: int, username: str | None, state_code: str, forum_city: str | None = None,
) -> None:
    if state_code == "fresh":
        return
    await _seed_consent(tid)
    # Форумное состояние несёт ВЫБРАННЫЙ город даже при выключенном модуле городов: форумный
    # код (чек-ин, SOS, программа) сверяет город делегата через `normalize_city(event_city)`.
    event_city_code = forum_city if state_code in _FORUM_STATE_STATUS else await _seed_event_city()
    seed_answers = dict(_SEED_ANSWERS)
    if event_city_code:
        # «Согласованный» city: тот же город, что и event_city, а не оставшаяся от прошлого
        # состояния «Москва» — иначе засеянный делегат «учится в Москве» на «событии в СПб».
        seed_answers["city"] = _home_city_name(await city_label(event_city_code))
    if state_code == "draft":
        await mark_reg_started(tid, username)
        resume_cols = set(columns_for_step("resume"))
        patch = {k: v for k, v in seed_answers.items() if k not in resume_cols}
        await upsert_reg_draft(
            tid, kind="new", step="resume", patch=patch, source="bot",
            event_city=event_city_code,
        )
        return
    data = {
        **seed_answers,
        "telegram_id": tid,
        "username": username,
        "registration_date": msk_now().strftime("%Y-%m-%d %H:%M:%S"),
        "season": await get_setting_typed("event_season"),
        "event_city": event_city_code,
    }
    if state_code == "short":
        data["participant_type"] = SHORT_TRACK
    await add_user(data)
    status = _FORUM_STATE_STATUS.get(state_code) or ("pending" if state_code == "short" else state_code)
    await set_user_status(tid, status)
    if state_code in ("fappr", "farr"):
        # Как у настоящего одобренного: токен выпускается тем же `get_or_create_checkin_token`,
        # что зовёт «🎟 Мой QR» и рассылка QR (`services/checkin.py::build_checkin_payload`).
        await get_or_create_checkin_token(tid)
    if state_code == "farr":
        # Отметка входа сегодня — прямо в `checkins` (как ручная отметка), без записи в
        # Google-таблицу и без приветствия первого входа: засеянной строки в листе нет.
        await record_checkin(tid, ENTRY_POINT, source="manual")


async def _seed_role(tid: int, role_code: str, city_code: str | None = None) -> None:
    wanted = set(_ROLE_CODE_TO_ROLES[role_code])
    for role in admin_caps.ROLES:
        if role in wanted:
            await add_staff(tid, role, added_by=tid)
        else:
            await remove_staff(tid, role)
    if wanted:
        # Привязка к городу — одна на человека (`set_staff_city` пишет во все его строки staff).
        # Не форумная роль снимает привязку: иначе `reg_manager`, оставшийся от «дежурного SOS
        # СПб», молча продолжал бы видеть только СПб.
        await set_staff_city(tid, city_code if role_code in _CITY_ROLE_HINTS else None)


@router.message(Command("uat"))
async def cmd_uat(message: types.Message):
    if not await _gate_ok(message.from_user):
        return
    await message.answer(
        "🧪 <b>Сеялка состояний приёмки</b>\n\nВ какое состояние поставить СВОЙ аккаунт?\n\n"
        "Нижние четыре — для приёмки форума: после них бот спросит город.",
        parse_mode="HTML", reply_markup=_states_keyboard(),
    )


_ROLE_PROMPT = (
    "Какую роль себе выдать?\n\n"
    "Главным админом здесь стать нельзя: их назначает разработчик при запуске бота, "
    "выдать или снять эту роль из бота нельзя."
)


@router.callback_query(F.data.startswith("uat_st:"))
async def uat_pick_state(callback: types.CallbackQuery):
    if not await _gate_ok(callback.from_user):
        return
    state_code = callback.data.split(":", 1)[1]
    if state_code not in _STATE_LABELS:
        await callback.answer()
        return
    if state_code in _FORUM_STATE_STATUS:
        await callback.message.edit_text(
            f"Состояние: {_STATE_LABELS[state_code]}\n\nНа форум какого города?",
            reply_markup=await _cities_keyboard(f"uat_stc:{state_code}"),
        )
        await callback.answer()
        return
    text = f"Состояние: {_STATE_LABELS[state_code]}\n\n{_ROLE_PROMPT}"
    await callback.message.edit_text(text, reply_markup=_roles_keyboard(state_code))
    await callback.answer()


@router.callback_query(F.data.startswith("uat_stc:"))
async def uat_pick_state_city(callback: types.CallbackQuery):
    """Шаг города для форумного состояния: `uat_stc:{состояние}:{город}` -> пикер роли, где
    форумные роли уже подписаны этим городом."""
    if not await _gate_ok(callback.from_user):
        return
    state_code, city_code, _ = _parse_cb(callback.data)
    if state_code not in _FORUM_STATE_STATUS or not _known_city(city_code):
        await callback.answer()
        return
    city_name = await city_label(city_code)
    text = f"Состояние: {_state_display(state_code, city_name)}\n\n{_ROLE_PROMPT}"
    await callback.message.edit_text(
        text, reply_markup=_roles_keyboard(state_code, city_code, city_name),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("uat_role:"))
async def uat_pick_role(callback: types.CallbackQuery):
    if not await _gate_ok(callback.from_user):
        return
    state_code, role_code, city_code = _parse_cb(callback.data)
    if state_code not in _STATE_LABELS or role_code not in _ROLE_PICK_LABELS:
        await callback.answer()
        return
    if city_code is not None and not _known_city(city_code):
        await callback.answer()
        return
    if _needs_city(state_code, role_code) and city_code is None:
        # Форумная роль при нефорумном состоянии: город спрашиваем здесь, тем же шагом.
        await callback.message.edit_text(
            f"Роль: {_ROLE_PICK_LABELS[role_code]}\n\nВ каком городе?",
            reply_markup=await _cities_keyboard(f"uat_role:{state_code}:{role_code}"),
        )
        await callback.answer()
        return
    tid = callback.from_user.id
    city_name = await city_label(city_code) if city_code else None
    footprint = await count_user_footprint(tid)
    lines = _footprint_lines(footprint)
    parts = [
        "⚠️ <b>Сбросить и засеять свой аккаунт?</b>",
        "",
        f"Новое состояние: {html.escape(_state_display(state_code, city_name))}",
        *[html.escape(n) for n in _forum_state_notes(state_code)],
        f"Роль: {html.escape(_role_display(role_code, city_name))}",
        *[html.escape(n) for n in await _role_notes(tid, role_code)],
    ]
    if lines:
        parts += ["", "🗑 <b>Пропадёт прямо сейчас:</b>", *lines]
    parts += ["", "Вернуть нельзя. Строка в Google-таблице (если была) останется."]
    go = f"uat_go:{state_code}:{role_code}" + (f":{city_code}" if city_code else "")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Сбросить и засеять", callback_data=go),
        InlineKeyboardButton(text="Отмена", callback_data="uat_no"),
    ]])
    await callback.message.edit_text("\n".join(parts), parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("uat_go:"))
async def uat_execute(callback: types.CallbackQuery, state: FSMContext):
    if not await _gate_ok(callback.from_user):
        return
    state_code, role_code, city_code = _parse_cb(callback.data)
    if state_code not in _STATE_LABELS or role_code not in _ROLE_PICK_LABELS:
        await callback.answer()
        return
    if city_code is not None and not _known_city(city_code):
        await callback.answer()
        return
    if _needs_city(state_code, role_code) and city_code is None:
        await callback.answer("Город не выбран — начните заново с /uat.", show_alert=True)
        return
    tid = callback.from_user.id
    username = callback.from_user.username
    city_code = normalize_city(city_code) if city_code else None

    await state.clear()
    result = await purge_user(tid)
    outbox_removed = await purge_miniapp_outbox_for_user(tid)
    try:
        cancel_payment_reminders(tid)
    except Exception:
        pass  # fail-soft: джобы напоминаний живут в отдельном jobs.sqlite (как в
        # admin_purge.py), сбой их отмены не должен мешать уже совершённому сбросу

    await _seed_state(tid, username, state_code, city_code)
    await _seed_role(tid, role_code, city_code)

    logger.warning(
        "uat_seed: uid=%s состояние=%s роль=%s город=%s, стёрто=%s, outbox=%s",
        tid, state_code, role_code, city_code, result, outbox_removed,
    )

    city_name = await city_label(city_code) if city_code else None
    lines = _footprint_lines(result)
    parts = [
        "✅ <b>Готово.</b>",
        "",
        f"Состояние: {html.escape(_state_display(state_code, city_name))}",
        *[html.escape(n) for n in _forum_state_notes(state_code, done=True)],
        f"Роль: {html.escape(_role_display(role_code, city_name))}",
    ]
    if lines:
        parts += ["", "🗑 <b>Стёрто:</b>", *lines]
    parts += ["", "Отправьте /start."]
    await callback.message.edit_text("\n".join(parts), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "uat_no")
async def uat_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("Отменено. Ничего не стёрто.")
    await callback.answer()
