"""Форум-ночь п.8 (идея №19 бэклога чек-ина): делегатская сторона «🆘 SOS».

D-31 (24.09, `.planning/FORUM-CHECKIN.md`, «SOS без категорий»): кнопка «🆘 SOS» СРАЗУ, без
единого уточняющего вопроса, создаёт заявку и публикует карточку в чат оргов («подробности ещё
не прислали») — категорийный визард (что случилось? -> текст/фото -> геопозиция) снесён
целиком, в экстренной ситуации важна скорость, не классификация. Делегат сразу попадает в режим
«дописываю SOS» (`SosReport.collecting`): ЛЮБОЕ его сообщение (текст, фото, геопозиция) уходит
В ТРЕД карточки И дописывает саму карточку (первый текст/фото снимает пометку «подробности ещё
не прислали»). Режим живёт до «Готово», «✅ Решено» у орга (`handlers/forum/admin_sos.py::
sos_resolve` -> `services.sos.close_delegate_collecting` сбрасывает FSM делегата по этой
заявке), таймаута (`sos_collecting_timeout_minutes`) или следующего `/start`.

Форма шва — та же, что у соседних делегатских экранов (FAQ/программа/чек-ин): своего
`Router()` нет, `from handlers.user_actions import router`; импортирован ХВОСТОМ
`handlers/user_actions.py`. Домен (карточка, привязка чата, эскалация, режим «дописываю»)
целиком в `services/sos.py` — здесь только FSM-шаги и точки отправки. Тексты — через
`reg_i18n.say` (тот же перевод делегатского чата, что остальные экраны); литералы этого модуля
зарегистрированы в `services/i18n_sources.py::code_literals()` (сторож
`tests/test_i18n_literal_corpus_guard_260906.py`, SCANNED_FILES дополнен этим модулем).

Анти-спам (пункт 1 плана «не чаще одного открытого SOS на делегата») — повторное «🆘 SOS», пока
предыдущий свой же SOS ещё свежий (`sos_reopen_window_minutes`), НЕ создаёт вторую заявку —
делегат попадает в тот же режим «дописываю SOS», привязанный к СУЩЕСТВУЮЩЕЙ заявке
(`sos_start`). «Старый» открытый SOS (окно истекло) новую заявку уже разрешает — с честной
ссылкой в карточке на прежний (`services.sos.render_card_text`)."""
import asyncio
import logging
from datetime import datetime

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import ReplyKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from domain.cities import default_city_code, get_setting_typed_for_city
from database.db import (
    add_sos_details, create_sos_report, get_open_sos_report, get_sos_report, get_user,
    mark_sos_collecting_started, set_sos_location,
)
from handlers.i18n import reg_i18n
from handlers.states import SosReport
from handlers.user_actions import _delegate_city, ensure_registered, router
from domain.i18n.ui_en import DONE_WORDS
from keyboards.builders import get_main_menu_kb
from keyboards.menu_dynamic import MenuButton
from services import sos as sos_service
from services.timeutil import msk_now
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

_LOCATION_BUTTON_TEXT = "📍 Отправить геопозицию"


async def _resolve_city(telegram_id: int) -> str | None:
    """SOS привязывается к чату КОНКРЕТНОГО города — «нет города» не бывает (в отличие от
    вопроса делегата, у которого фан-аут глобальный), тот же приём, что
    `handlers.forum.program._resolve_delegate_city`."""
    code = await _delegate_city(telegram_id)
    return code or default_city_code()


def _collecting_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=_LOCATION_BUTTON_TEXT, request_location=True)
    kb.button(text="Готово")
    kb.adjust(1, 1)
    return kb.as_markup(resize_keyboard=True)


# ── Ревью 24.09 (находка 3, сохранено при D-31): «свежий» открытый SOS того же делегата
# (младше `sos_reopen_window_minutes`) не создаёт вторую заявку — делегат попадает в режим
# «дописываю SOS», привязанный к СУЩЕСТВУЮЩЕЙ заявке. «Старый» — новый SOS разрешён (прежний
# остаётся открытым, в карточке нового — честная ссылка на него, `services.sos.render_card_text`/
# `database.db.create_sos_report(prior_open_report_id=...)`).

async def _reopen_window_minutes(city: str | None) -> float:
    raw = await get_setting_typed_for_city("sos_reopen_window_minutes", city)
    try:
        return float(raw) if raw else sos_service.DEFAULT_REOPEN_WINDOW_MINUTES
    except (TypeError, ValueError):
        return sos_service.DEFAULT_REOPEN_WINDOW_MINUTES


async def _is_recent_open_report(report: dict, city: str | None) -> bool:
    age = sos_service.report_age_minutes(report)
    if age is None:
        return False  # штамп не распарсился -> не блокируем повторно, ведём себя как «старый»
    return age < await _reopen_window_minutes(city)


async def _recent_followup_text(report: dict, lang: str, tr_map: dict) -> str:
    """Часть А (ревью, найдено при проверке SOS-переводов): плейсхолдер `{claim_status}`
    подставлялся ДО перевода шаблона (`.format` поверх сырого текста реестра) — хеш уже
    подставленной строки никогда не совпадал с хешем шаблона в словаре перевода, и
    англоязычный делегат получал русский текст целиком, даже когда перевод шаблона был готов
    (тот же класс неполного перевода, что `recall_generic_prompt_text`,
    `handlers/registration.py:836`). Теперь — тот же порядок, что `reg_i18n.tr_fmt` везде в
    чате: шаблон переводится СНАЧАЛА, подстановка `{claim_status}` — ПОСЛЕ.
    `sos_service.claim_status_parts` отдаёт (шаблон, имя) отдельно — шаблон уходит через
    словарь (тир B), имя (собственное) переводится отдельно ТОЛЬКО ради переводимого фолбэка
    «коллега» (реальное имя просто не найдётся в словаре и останется как есть)."""
    raw = await get_setting_typed("sos_recent_followup_text")
    template, who = sos_service.claim_status_parts(report)
    if who is not None:
        claim_status = reg_i18n.tr_fmt(template, lang, tr_map, who=reg_i18n.tr_text(who, lang, tr_map))
    else:
        claim_status = reg_i18n.tr_text(template, lang, tr_map)
    return reg_i18n.tr_fmt(raw, lang, tr_map, claim_status=claim_status)


async def _enter_collecting(state: FSMContext, report_id: int, city: str | None) -> None:
    await state.set_state(SosReport.collecting)
    await state.update_data(
        sos_collecting_report_id=report_id,
        sos_collecting_city=city,
        sos_collecting_started=msk_now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    # То же время — в БД: планировщику (утренний QR) FSM не виден, а переоткрытие старой
    # заявки перезапускает таймер дозаписи (`services.sos.may_be_collecting`).
    try:
        await mark_sos_collecting_started(report_id)
    except Exception as e:
        logger.warning("sos._enter_collecting: время входа в дозапись не записано: %s", e)


async def _collecting_timeout_minutes(city: str | None) -> float:
    raw = await get_setting_typed_for_city("sos_collecting_timeout_minutes", city)
    try:
        return float(raw) if raw else sos_service.DEFAULT_COLLECTING_TIMEOUT_MINUTES
    except (TypeError, ValueError):
        return sos_service.DEFAULT_COLLECTING_TIMEOUT_MINUTES


async def _is_collecting_expired(started_raw: str | None, city: str | None) -> bool:
    if not started_raw:
        return False  # штамп потерян (гонка/старая версия state) -> не режем сессию
    try:
        started = datetime.strptime(started_raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    elapsed_minutes = (msk_now() - started).total_seconds() / 60
    return elapsed_minutes >= await _collecting_timeout_minutes(city)


async def _expire_collecting(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await reg_i18n.say(
        message, await get_setting_typed("sos_collecting_expired_text"),
        reply_markup=await get_main_menu_kb(message.from_user.id),
    )


# Двойной тап «🆘 SOS»: aiogram обрабатывает апдейты параллельно, и между
# `get_open_sos_report` и `create_sos_report` второй тап успевал создать вторую карточку.
# Лок на пользователя сериализует оба тапа — второй дожидается первого и уходит в ветку
# «сигнал уже у оргкомитета» с тем же текстом. Счётчик держателей — чтобы убрать лок из
# словаря только когда его никто не ждёт.
_sos_start_locks: dict[int, list] = {}  # user_id -> [asyncio.Lock, держателей]


async def _may_send_sos(message: types.Message) -> bool:
    """SOS — про безопасность, а не про статус заявки: на площадке в день форума может
    оказаться и тот, кого ещё не одобрили (walk-in в очереди стойки — `pending`), и
    отклонённый, пришедший с другом. Поэтому одобренным — как раньше; ожидающим и отклонённым —
    пока у их города открыто окно SOS (`forum_date` + `sos_active_days`, то же, что показывает
    кнопку). Вне окна — обычный гейт (`ensure_registered`) со своим текстом; без анкеты вовсе —
    тоже он («отправь /start»): карточке нужны хотя бы имя и телефон."""
    user = await get_user(message.from_user.id)
    if user is not None and user.get("status") in ("pending", "rejected"):
        if await sos_service.is_sos_active_for_city(await _resolve_city(message.from_user.id)):
            return True
    return await ensure_registered(message)


# 🆘 SOS — кнопка главного меню
@router.message(MenuButton("menu_sos"))
async def sos_start(message: types.Message, state: FSMContext):
    if not await _may_send_sos(message):
        return
    uid = message.from_user.id
    entry = _sos_start_locks.setdefault(uid, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            await _sos_start_locked(message, state)
    finally:
        entry[1] -= 1
        if entry[1] == 0 and _sos_start_locks.get(uid) is entry:
            del _sos_start_locks[uid]


async def _sos_start_locked(message: types.Message, state: FSMContext):
    city = await _resolve_city(message.from_user.id)
    open_report = await get_open_sos_report(message.from_user.id)
    prior_open_id = None
    if open_report is not None:
        if await _is_recent_open_report(open_report, city):
            logger.info(f"User {message.from_user.id} reopened recent SOS #{open_report['id']}")
            await _enter_collecting(state, open_report["id"], city)
            lang, tr_map = await reg_i18n.ctx_for(message)
            await reg_i18n.say(
                message, await _recent_followup_text(open_report, lang, tr_map),
                reply_markup=_collecting_kb(),
            )
            return
        prior_open_id = open_report["id"]
    logger.info(f"User {message.from_user.id} opened SOS")
    await _create_and_notify(message, state, city, prior_open_id)


async def _create_and_notify(message: types.Message, state: FSMContext, city: str | None,
                              prior_open_id: int | None) -> None:
    """D-31: карточка публикуется МГНОВЕННО (пункт 1) — без вопроса «что случилось», сразу за
    нажатием кнопки. `PostCardResult.delivered_total` — реальное число доставок (ревью 24.09,
    находка 1), делегат слышит честный текст, а не пустое «получили», если карточка не дошла
    НИКУДА (чат упал, фоллбэк-веер разошёлся нулю получателей)."""
    report_id = await create_sos_report(
        message.from_user.id, city, prior_open_report_id=prior_open_id,
    )
    logger.info(f"User {message.from_user.id} created SOS #{report_id}")

    try:
        result = await sos_service.post_card(message.bot, report_id)
    except Exception as e:
        logger.error(f"sos._create_and_notify: post_card({report_id}) failed: {e}")
        result = sos_service.PostCardResult()
    await sos_service.record_delivery_outcome(report_id, result)

    try:
        minutes_raw = await get_setting_typed_for_city("sos_escalation_minutes", city)
        minutes = int(minutes_raw) if minutes_raw else sos_service.DEFAULT_ESCALATION_MINUTES
    except (TypeError, ValueError):
        minutes = sos_service.DEFAULT_ESCALATION_MINUTES
    sos_service.schedule_escalation(report_id, minutes)

    await _enter_collecting(state, report_id, city)

    if result.delivered_total > 0:
        await reg_i18n.say(
            message, await get_setting_typed("sos_sent_text"),
            reply_markup=_collecting_kb(),
        )
        return

    # Ни в чат, ни фоллбэком в личку — делегат не должен уйти с пустым «получили». Одна
    # повторная попытка доставки через минуту (джоба перечитывает статус). Делегат всё равно
    # остаётся в режиме «дописываю SOS» — то, что он пришлёт, уйдёт ПРИ следующей успешной
    # доставке/повторе (карточка сама перерисуется, `services.sos.refresh_card`).
    logger.error(f"sos._create_and_notify: SOS #{report_id} не доставлен НИКОМУ, ставлю повтор")
    sos_service.schedule_delivery_retry(report_id)
    await reg_i18n.say(
        message, await get_setting_typed("sos_delivery_failed_text"),
        reply_markup=_collecting_kb(),
    )
    # Контакт (телефон и т.п.) — сырое значение, НЕ переводится (тот же приём, что
    # contact_person/contact_vk/contact_tg в handlers/user_actions.py::show_contacts),
    # отдельным сообщением, чтобы не портить сопоставление корпуса перевода выше.
    #
    # Часть А (ревью SOS-переводов): бот шлёт с дефолтным `parse_mode=HTML` (main.py), а это
    # поле — свободный ввод менеджера — случайный «<3»/непарный «<»/«&» в тексте уронил бы
    # отправку с необработанным исключением (делегат в экстренной ситуации остался бы без
    # контакта вовсе). Тот же WR-04-фоллбэк, что `handlers/user_actions.py::show_contacts`:
    # провал с разметкой -> один повтор без неё, а не пустая рука в самый неподходящий момент.
    contact = await get_setting_typed_for_city("sos_fallback_contact_text", city)
    if contact:
        try:
            await message.answer(contact)
        except Exception as e:
            logger.error(f"sos._create_and_notify: экстренный контакт не ушёл с HTML, повтор без разметки: {e}")
            try:
                await message.answer(contact, parse_mode=None)
            except Exception as e2:
                logger.error(f"sos._create_and_notify: экстренный контакт не ушёл даже без разметки: {e2}")


# ── Режим «дописываю SOS» (D-31) — ЛЮБОЕ сообщение делегата уходит в тред карточки И дописывает
# саму карточку первым текстом/фото; «Готово» закрывает режим, таймаут закрывает его молча (без
# явного действия делегата). «Решено» со стороны орга закрывает режим из админского хендлера
# (`services.sos.close_delegate_collecting`).

@router.message(SosReport.collecting, F.text.in_(DONE_WORDS))
async def sos_collecting_done(message: types.Message, state: FSMContext):
    await state.clear()
    await reg_i18n.say(
        message, await get_setting_typed("sos_done_text"),
        reply_markup=await get_main_menu_kb(message.from_user.id),
    )


@router.message(SosReport.collecting, F.location)
async def sos_collecting_location(message: types.Message, state: FSMContext):
    data = await state.get_data()
    report_id = data.get("sos_collecting_report_id")
    city = data.get("sos_collecting_city")
    if report_id is None:
        await state.clear()
        return
    if await _is_collecting_expired(data.get("sos_collecting_started"), city):
        await _expire_collecting(message, state)
        return
    await set_sos_location(report_id, message.location.latitude, message.location.longitude)
    await sos_service.relay_delegate_message(message, report_id)
    await sos_service.refresh_card(message.bot, report_id)


@router.message(SosReport.collecting)
async def sos_collecting_step(message: types.Message, state: FSMContext):
    """Сообщение делегата в режиме «дописываю SOS» — в карточку и в её тред. «✅ Решено» у орга
    сбрасывает это состояние снаружи (`services.sos.close_delegate_collecting`, адресный
    `StorageKey` делегата), так что после решения сюда уже не попадаем."""
    data = await state.get_data()
    report_id = data.get("sos_collecting_report_id")
    city = data.get("sos_collecting_city")
    if report_id is None:
        await state.clear()
        return
    if await _is_collecting_expired(data.get("sos_collecting_started"), city):
        await _expire_collecting(message, state)
        return
    text = message.text or message.caption
    photo_id = message.photo[-1].file_id if message.photo else None
    if text or photo_id:
        # Первый текст делегата встаёт в саму карточку. В ЧАТЕ SOS он всё равно уходит и
        # копией в тред: правка сообщения в Telegram не даёт уведомления, и без копии суть
        # («астма, 2 этаж») появилась бы в карточке молча. Дубль убираем только в личке
        # (приёмка 01.10: там копия карточки и дописка — у одного человека подряд).
        #
        # «Встал ли текст» решает сама запись (`add_sos_details` -> rowcount), а не строка,
        # прочитанная заранее: два быстрых сообщения обрабатываются параллельно, и второе,
        # решив «я тоже встану в карточку», терялось целиком. Без копии обходимся, только
        # когда текст реально в карточке И карточку удалось перерисовать хоть где-то.
        text_landed = await add_sos_details(report_id, text=text, photo_file_id=photo_id)
        edited = await sos_service.refresh_card(message.bot, report_id)
        if text_landed and message.text and not photo_id and edited:
            row = await get_sos_report(report_id)
            if row is not None and not (row.get("chat_id") and row.get("card_message_id")):
                return
    await sos_service.relay_delegate_message(message, report_id)


# ── Ответ делегата на «Ответ по SOS» — снова в тред карточки (пункт 3 плана) ────────────────
#
# Маркер — ТОЛЬКО "🆘" (без "🆔"): это личное сообщение делегату (handlers/forum/admin_sos.py::
# admin_reply_to_sos шлёт «🆘 Ответ по SOS #<id>:»), его telegram_id тут ни при чём — в отличие
# от карточки В ЧАТЕ ОРГОВ (services/sos.py::render_card_text), где "🆔"+"🆘" вместе отличают
# реплай орга от произвольного ответа в группе.

def _is_sos_followup(message: types.Message) -> bool:
    replied = message.reply_to_message
    if not replied or not replied.text:
        return False
    return "🆘" in replied.text and "SOS #" in replied.text


@router.message(_is_sos_followup)
async def sos_delegate_followup(message: types.Message):
    import re

    match = re.search(r"SOS #([0-9]+)", message.reply_to_message.text)
    if not match:
        return
    report_id = int(match.group(1))
    from database.db import get_sos_report

    report = await get_sos_report(report_id)
    if report is None or report.get("telegram_id") != message.from_user.id:
        return  # чужая карточка/устаревшая ссылка — тихо, ничего не пересылаем не по адресу
    await sos_service.relay_delegate_message(message, report_id)
