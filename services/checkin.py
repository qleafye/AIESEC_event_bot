"""Квик 260923 (форум-чекин, D-01..D-04): персональный QR одобренного делегата.

`build_checkin_qr(user)` — ЕДИНСТВЕННАЯ точка, которая знает формат содержимого QR. Её
переиспользует и хендлер главного меню (`handlers/user_actions.py::show_my_checkin_qr`), и
БУДУЩАЯ рассылка накануне форума (не эта задача — сканер/загрузка CSV тоже не входят сюда,
только выпуск и выдача QR). Сигнатура рассчитана на переиспользование заранее: чистая функция
без aiogram-типов на входе (`dict` строки `users`), возвращает `(png_bytes, caption)` —
вызывающий сам решает, как отправить (`answer_photo`, будущая рассылка и т.п.).

Статус делегата («одобрен») эта функция НЕ проверяет — гейт D-02 живёт в `checkin_denial()`
ниже, вызывающий (хендлер меню `show_my_checkin_qr`; будущая рассылка накануне форума)
ОБЯЗАН позвать её сам ПЕРЕД `build_checkin_qr`. Здесь только генерация содержимого для уже
допущенного делегата.

Формат текста внутри QR — `{tag}·{full_name}·{city}·{token}`, разделитель «·» (не запятая и не
пробел — оба встречаются в свободных полях ФИО/города). Порядок ЗАКРЕПЛЁН: парсер CSV сканера
ищет метку события в НАЧАЛЕ строки и берёт токен ХВОСТОМ — последним полем после последнего
«·». Менять порядок нельзя без синхронной правки парсера.

Phase 12 (FORUM-CHECKIN.md, D-09/D-10/D-13): вторая половина модуля — `parse_qr_payload`
(обратная операция к `build_payload`) и разбор выгрузки офлайн-приложения-сканера
(`decode_scan_export`/`find_checkin_records`) для `handlers/admin_checkin.py`. Доступ к БД
(токен делегата, запись отметки, счётчики) — `database/db.py`
(`get_or_create_checkin_token`/`get_user_by_checkin_token`/`record_checkin`/
`count_checkins_by_point`/`count_approved_current_season`), здесь — только чистая логика."""
from __future__ import annotations

import csv
import io
import logging
import re
import secrets
from datetime import datetime

import segno

from database.db import (
    first_entry_scanned_at,
    get_checkin_token_replacement,
    get_or_create_checkin_token,
    get_program_session,
    get_user_by_checkin_token,
    has_entry_on_other_day,
    list_program_sessions_for_city_day,
    record_checkin,
    record_session_checkin,
)
from reg_engine import is_past_season_row  # D-02: пропуск на форум не выдаём возвращенцу
from services.timeutil import aware_to_msk, msk_from_timestamp
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

_QR_SEP = "·"

# Phase 12 (FORUM-CHECKIN.md, D-08/D-18): точка входа «Вход» — тот же `point`, что
# `database.db.record_checkin`/`count_checkins_by_point` считают по умолчанию. Сессии
# программы (будущая фаза) получат свои point-код/слаг, вход остаётся отдельной
# константой — она уже размечена кнопкой в `handlers/admin_checkin.py`.
ENTRY_POINT = "entry"
ENTRY_POINT_LABEL = "🚪 Вход"

_DEFAULT_CAPTION = (
    "🎟 Твой QR для отметки на форуме.\n\n"
    "Сохрани его заранее (например, сделай скриншот) — на площадке может не быть сети, а фото "
    "из чата открывается и без интернета."
)


async def _event_tag() -> str:
    """Метка события внутри QR: явный `checkin_event_tag`, если менеджер его задал; иначе —
    `event_season` без пробелов и апострофа («YL'26» -> «YL26»). Пустые обе настройки (событие
    ещё не сконфигурировано) дают дефолт «EVENT» — генерация QR не должна падать из-за пустого
    реестра, просто получится менее говорящая метка."""
    tag = (await get_setting_typed("checkin_event_tag") or "").strip()
    if tag:
        return tag
    season = (await get_setting_typed("event_season") or "").strip()
    if season:
        return season.replace("'", "").replace(" ", "")
    return "EVENT"


async def current_event_tag() -> str:
    """Публичная асинхронная обёртка `_event_tag()` — единственная точка, которую зовёт
    `handlers/admin_checkin.py` при разборе выгрузки офлайн-сканера (`find_checkin_records`
    ниже), чтобы искать код нашего события в файле ТЕМ ЖЕ значением, что уходит в свежий QR —
    генерация и разбор физически не могут разойтись, читая один и тот же `_event_tag()`."""
    return await _event_tag()


def build_payload(tag: str, full_name: str, city: str, token: str) -> str:
    """Чистая сборка строки QR — вынесена отдельно от `build_checkin_qr`, чтобы формат
    (порядок полей, разделитель «·», плейсхолдер «—» для пустых ФИО/города) был проверяем
    юнит-тестом без генерации самой картинки/обращения к БД. Токен ВСЕГДА последним полем —
    см. докстринг модуля."""
    full_name = (full_name or "").strip() or "—"
    city = (city or "").strip() or "—"
    return _QR_SEP.join([tag, full_name, city, token])


def parse_qr_payload(qr_payload: str) -> dict:
    """Обратная операция к `build_payload` — разбирает строку из отсканированного QR на поля
    `{"tag": ..., "full_name": ..., "city": ..., "token": ...}`. Токен ВСЕГДА последнее поле
    (см. докстринг модуля и `build_payload`), метка события — первое; никогда не бросает
    исключение — терпима к «чужому»/повреждённому коду (меньше 4 полей): токен есть, если хоть
    один разделитель нашёлся, ФИО/город — только при ровно 4 полях (собственный формат)."""
    parts = (qr_payload or "").split(_QR_SEP)
    if len(parts) < 2:
        return {"tag": "", "full_name": "", "city": "", "token": (parts[0].strip() if parts else "")}
    token = parts[-1].strip()
    tag = parts[0].strip()
    full_name = parts[1].strip() if len(parts) >= 4 else ""
    city = parts[2].strip() if len(parts) >= 4 else ""
    return {"tag": tag, "full_name": full_name, "city": city, "token": token}


# B (FORUM-CHECKIN.md, D-08/D-12/D-13, идея №9): человеческие причины отказа — ПОЛНОЕ
# предложение с подсказкой, что делать, для сканера Mini App (`miniapp/routers/checkin.py`).
# Единая точка перевода машинного кода в текст — `checkin_denial` ниже отдаёт коды
# ("no_user"/"not_approved"/"past_season"), эта карта живёт РЯДОМ с ней, а не в самом
# Mini App (импортировать нельзя — второй словарь тех же кодов в другом модуле разошёлся бы
# при следующей правке). `handlers/admin_checkin.py` держит СВОЮ, более терсную версию тех
# же кодов (`_DENIAL_LABELS`) — там строка идёт в компактный список отчёта загрузки CSV,
# здесь — крупная плашка на весь экран, разный жанр текста, не дубль одного и того же.
# "foreign_event" — код, которого `checkin_denial` НЕ возвращает (это отдельная проверка
# метки события ДО поиска пользователя по токену, метка ещё не наша — искать по её токену
# в нашей БД бессмысленно и рискованно), текст живёт здесь же, одним местом со всеми причинами.
DENIAL_REASON_TEXT = {
    "no_user": "QR не найден — отправьте на стойку проблемных случаев",
    "not_approved": "Заявка ещё на рассмотрении",
    "past_season": "Делегат прошлого сезона",
    "foreign_event": "QR другого мероприятия",
    # Форум-ночь B1 (идея №10): менеджер перевыпустил QR (handlers/admin.py::cmd_find_user ->
    # checkin_reissue_yes) — этот код УЖЕ не откроет вход, даже если делегат ещё не успел
    # открыть новый (database.db.get_checkin_token_replacement).
    "token_replaced": "QR заменён — попросите делегата открыть новый в «🎟 Мой QR»",
}


async def checkin_denial(user: dict | None) -> str | None:
    """Единая точка правила допуска к чек-ину форума (решение владельца D-02: «QR только
    одобренным, наличие QR = пропуск на форум»). Переиспускается и хендлером меню
    (`handlers/user_actions.py::show_my_checkin_qr`), и БУДУЩЕЙ рассылкой QR накануне форума —
    обе точки обязаны звать её ПЕРЕД `build_checkin_qr`.

    Допуск есть, только если ВСЕ три условия верны: (1) пользователь существует, (2) его
    `status` строго `'approved'` (в отличие от `handlers.user_actions._gate_decision`, здесь
    NULL/неизвестные/будущий `waitlist` НЕ допускаются — тот гейт разрешает их ради обратной
    совместимости ~590 легаси-записей, а QR — материальный пропуск на площадку, легаси-станд
    там неуместен), (3) делегат НЕ прошлого сезона (`reg_engine.is_past_season_row` сверяет
    `users.season` с текущим `event_season`) — иначе 482 импортированных делегата 26/1 со
    `status='approved'` получили бы пропуск на текущий форум.

    Возвращает код причины отказа (`'no_user' | 'not_approved' | 'past_season'`) или `None`,
    если допуск есть. Чтение `event_season` — fail-soft (тот же приём, что у
    `handlers/user_actions.py::_returning_text_if_past_season`): сбой чтения настройки не
    должен блокировать пропуск уже одобренному делегату текущего события."""
    if not user:
        return "no_user"
    if user.get("status") != "approved":
        return "not_approved"
    try:
        event_season = await get_setting_typed("event_season") or None
        if is_past_season_row(user, event_season):
            return "past_season"
    except Exception as e:
        logger.error(f"checkin_denial: event_season resolve failed for {user.get('telegram_id')}: {e}")
    return None


async def resolve_scanned_user(token: str | None) -> tuple[dict | None, str | None]:
    """Единая точка «токен из QR -> (делегат, код отказа)» — оборачивает
    `get_user_by_checkin_token` + `checkin_denial` для ОБОИХ вызывающих
    (`miniapp/routers/checkin.py`, `handlers/admin_checkin.py`), плюс форум-ночь B1 (идея №10,
    перевыпуск QR): если токен НЕ находится в `users` (значит его больше нет — либо чужой QR,
    либо СВОЙ, но уже перевыпущенный), сверяемся с `checkin_token_replacements` ПЕРЕД тем, как
    сдаться на общем «не найден» — старый (замененный) QR получает свою причину
    `'token_replaced'`, а не общий `'no_user'`, чтобы волонтёр понял: делегат СУЩЕСТВУЕТ,
    просто открыл старый скриншот вместо нового «🎟 Мой QR»."""
    user = await get_user_by_checkin_token(token) if token else None
    if user is None and token:
        replacement = await get_checkin_token_replacement(token)
        if replacement is not None:
            return None, "token_replaced"
    return user, await checkin_denial(user)


async def build_checkin_payload(user: dict) -> str | None:
    """Строка содержимого QR для делегата `user`, БЕЗ рендера картинки — переиспользуется и
    `build_checkin_qr` ниже, и юнит-тестом на формат. Генерирует и сохраняет `checkin_token`,
    если его ещё нет (лениво, `get_or_create_checkin_token`). `None`, если пользователя нет."""
    telegram_id = user["telegram_id"]
    token = await get_or_create_checkin_token(telegram_id)
    if not token:
        return None
    tag = await _event_tag()
    return build_payload(tag, user.get("full_name"), user.get("event_city"), token)


async def build_checkin_qr(user: dict) -> tuple[bytes, str]:
    """`(png_bytes, caption)` для делегата `user` (строка `database.db.get_user`). Генерирует
    и сохраняет `checkin_token`, если его ещё нет (лениво, `get_or_create_checkin_token`) —
    второй вызов для того же делегата отдаёт PNG с тем же токеном внутри.

    `caption` возвращается НЕ переведённым (русский, из реестра `checkin_qr_caption_text`) —
    перевод на английский, как и у остальных ответов делегату, делает вызывающий
    (`reg_i18n.tr_text`) перед отправкой."""
    payload = await build_checkin_payload(user)
    if payload is None:
        raise ValueError(f"build_checkin_qr: нет пользователя {user.get('telegram_id')}")

    qr = segno.make(payload)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=6, border=2)

    caption = await get_setting_typed("checkin_qr_caption_text") or _DEFAULT_CAPTION
    return buf.getvalue(), caption


# Форум-ночь B4 (идея №8): «🧪 Проверить приложение-сканер» — волонтёр без делегатского QR
# под рукой (сам не делегат) получает ФИКТИВНЫЙ код с той же меткой события, но заведомо
# ненастоящим токеном (`TEST` + случайные символы) — не спутать ни с одним реальным
# `secrets.token_urlsafe`-токеном делегата (те никогда не начинаются с «TEST»), и таким
# образом сканирование/загрузка выгрузки с этим кодом никогда не «находит» реального
# человека даже случайно (handlers/admin_checkin.py::checkin_test_file_step ничего не
# отмечает вообще, но эта же гарантия защищает от путаницы, если волонтёр всё же занесёт
# такую выгрузку в НАСТОЯЩУЮ загрузку — код останется просто «не найден»).
_TEST_TOKEN_PREFIX = "TEST"


async def build_test_qr() -> tuple[bytes, str]:
    """`(png_bytes, caption)` тестового QR — не привязан ни к какому делегату, только для
    проверки, что приложение-сканер волонтёра вообще читает формат кода этого события."""
    tag = await _event_tag()
    token = _TEST_TOKEN_PREFIX + secrets.token_urlsafe(6)
    payload = build_payload(tag, "Тестовый QR", "—", token)

    qr = segno.make(payload)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=6, border=2)

    caption = (
        "🧪 Тестовый QR — НЕ пропуск на форум, просто проверка вашего приложения-сканера. "
        "Отсканируйте его так же, как настоящий QR делегата."
    )
    return buf.getvalue(), caption


async def mark_arrived_in_sheet(telegram_id: int, status: str, scanned_at: str) -> None:
    """Форум-ночь B2 (идея №17): единая точка «отметка на форуме -> время в Google-таблице»,
    вызывается ВСЕМИ тремя источниками отметки СРАЗУ ПОСЛЕ `database.db.record_checkin`
    (miniapp/routers/checkin.py::checkin_scan/checkin_manual, handlers/admin_checkin.py::
    checkin_point_pick) — тот же приём, что `services.sheets.update_status_in_sheet` для
    «Статус» (services/sheets.py::update_arrived_in_sheet ниже — точечное обновление одной
    ячейки, не переаппенд строки).

    Только на `'new'` (первая отметка) — `'duplicate'` уже писала то же самое время скана при
    первой отметке, второй вызов Sheets API на тот же результат был бы просто тратой квоты.
    Вход каждый день: «Пришёл» — время ПЕРВОГО входа за форум. Новый вход второго дня ячейку
    не трогает (раньше уже есть вход); пишем, только если этот вход — самый ранний из имеющихся
    (обычный первый скан или CSV первого дня, загруженный после живых сканов второго).
    Сам вызов уже fail-soft (update_arrived_in_sheet ловит исключения и возвращает False) —
    отметка в БД к этому моменту уже сохранена вызывающим, лист может упасть без последствий
    для самого чек-ина (D-17)."""
    if status != "new":
        return
    first = await first_entry_scanned_at(telegram_id)
    if first is not None and first < scanned_at:
        return
    from services.sheets import update_arrived_in_sheet
    await update_arrived_in_sheet(telegram_id, scanned_at)


# ── Точка расширения «после ПЕРВОЙ отметки входа делегата» (24.09) ──────────────────────────

_first_entry_listeners: list = []


def register_first_entry_listener(fn) -> None:
    """Подписать async-слушателя на ПЕРВЫЙ ЗА ДЕНЬ вход делегата (для будущего «приветствия
    после первого скана» и т.п.). Сигнатура слушателя:

        async def listener(bot, user_id: int, city: str | None, day: str, **kwargs) -> None

    `city` — код города форума делегата (`cities.normalize_city(users.event_city)`), `day` —
    «YYYY-MM-DD» дня отметки (для CSV — день скана из файла). В `kwargs` сейчас приходят:
    `source` ("miniapp" — скан QR, "manual" — поиск по ФИО, "auto_session" — вход поставлен
    сканом на сессии, "csv" — загрузка выгрузки сканера), `by_staff_id` (кто отметил, может быть
    None), `scanned_at` («YYYY-MM-DD HH:MM:SS» по Москве), `approx` (время скана примерное),
    `session_id` (только у "auto_session"), `first_of_day` (всегда True — зов бывает только на
    первый вход дня), `first_of_forum` (True — это первый вход делегата за весь форум: входа в
    другие дни нет). Приветствие «один раз за форум» обязано смотреть на `first_of_forum`, а не
    полагаться на сам факт зова: на двухдневном форуме второй день зовёт слушателей ещё раз с
    `first_of_forum=False`. Слушатель обязан принимать `**kwargs` — поля могут добавляться.

    «Первый за день» = `database.db.record_checkin` реально вставил строку входа (`"new"`,
    rowcount от INSERT OR IGNORE по UNIQUE(telegram_id, point, day)); повторный скан в тот же
    день (`"duplicate"`) слушателей не зовёт. Зов — ПОСЛЕ коммита отметки, fail-soft: исключение слушателя логируется и не
    мешает ни отметке, ни остальным слушателям.

    Снятие отметки (идея №32: «↩️ Отменить» волонтёра или снятие менеджером,
    `services/venue_log.py`) слушателей НЕ откатывает и никого не зовёт — что слушатель уже
    сделал (приветствие ушло), то сделано. Строка входа при снятии удаляется, поэтому
    повторная отметка того же делегата снова будет `"new"` и позовёт слушателей ЕЩЁ РАЗ
    (`first_of_forum` снова True, если других дней нет).
    Слушатель обязан быть идемпотентным сам (например, помнить в своей таблице, кому уже
    отправил), а не полагаться на «первая отметка бывает один раз».

    CSV-импорт тоже зовёт слушателей (для каждой новой отметки) с `source="csv"` — выгрузку
    могут загрузить и после форума, поэтому слать ли что-то делегату, решает сам слушатель по
    `source`/`day`.

    Слушатели живут в ПРОЦЕССЕ БОТА. Mini App (сканер/поиск) — отдельный процесс без Bot: там
    `record_arrival` вызывается без `bot`, событие уходит в `miniapp_outbox`
    (`checkin_first_entry`), и бот зовёт слушателей при разборе очереди
    (`services/miniapp_outbox.py`) — с задержкой в один тик джобы. Регистрировать слушателя
    нужно в процессе бота (например, при импорте модуля фичи из `main.py`)."""
    if fn not in _first_entry_listeners:
        _first_entry_listeners.append(fn)


def clear_first_entry_listeners() -> None:
    """Снять всех слушателей (для тестов)."""
    _first_entry_listeners.clear()


async def fire_first_entry(bot, user_id: int, city: str | None, day: str, **kwargs) -> None:
    """Позвать всех слушателей первой отметки входа по очереди, fail-soft (см.
    `register_first_entry_listener`). Сам никогда не бросает."""
    for fn in list(_first_entry_listeners):
        try:
            await fn(bot, user_id, city, day, **kwargs)
        except Exception:
            logger.exception("first entry listener %r failed for %s", fn, user_id)


def _first_entry_event(user: dict, ts: str, source: str, by_staff_id, approx: bool, **extra) -> dict:
    import cities as _cities  # ленивый импорт — тот же приём, что в record_arrival

    return {
        "user_id": user["telegram_id"],
        "city": _cities.normalize_city(user.get("event_city")),
        "day": (ts or "")[:10],
        "source": source,
        "by_staff_id": by_staff_id,
        "scanned_at": ts,
        "approx": approx,
        **extra,
    }


async def _after_first_entry(result: dict, bot, event: dict) -> None:
    """Событие первого за день входа: кладёт его в `result["first_entry"]` (Mini App переносит
    в outbox) и, если есть `bot` (процесс бота), сразу зовёт слушателей. `first_of_forum` —
    входа в другие дни форума нет (см. `register_first_entry_listener`)."""
    event["first_of_day"] = True
    event["first_of_forum"] = not await has_entry_on_other_day(event["user_id"], event["day"])
    result["first_entry"] = event
    if bot is not None:
        await fire_first_entry(bot, **event)


# ── Форум-ночь п.5 (D-18..D-20): единая точка отметки на ЛЮБОЙ точке (вход и сессии) ─────────

async def record_arrival(
    user: dict,
    point: str,
    *,
    source: str,
    scanned_at: str | None = None,
    approx: bool = False,
    by_staff_id: int | None = None,
    staff_name: str | None = None,
    bot=None,
) -> dict:
    """Единая точка «делегат — точка X — отметка» для ВСЕХ трёх источников (загрузка CSV,
    сканер Mini App, ручной поиск) — решает, какая функция БД нужна: `point == ENTRY_POINT`
    (или вообще не `session:...`) — старый путь один-в-один
    (`database.db.record_checkin`, идемпотентно, первый скан побеждает, D-10); `point` вида
    `"session:{id}"` (D-18) — делегат ОБЯЗАН быть из ТОГО ЖЕ города, что сессия (иначе
    `status="wrong_city"`, отказ словами — у входа такой проверки нет и не будет), иначе D-20
    («последний скан слота засчитывается», слот строит `services.program.parallel_group`) через
    `database.db.record_session_checkin`. Отметка на сессии САМА ставит вход
    (`source="auto_session"`), если его ещё не было, — делегат физически подтверждён на
    площадке, даже если отдельного скана на входе не случилось.

    Возвращает `{"status": ..., "scanned_at": ...}` плюс `"reason_text"` при `wrong_city` и
    `wrong_day`, `"previous_title"` при `moved`, `"day_mismatch": True` при несовпадении дня
    у CSV (см. ниже). `mark_arrived_in_sheet` вызывается ВНУТРИ (на настоящем новом входе,
    прямом или авто-от-сессии) — вызывающему (`handlers/admin_checkin.py`,
    `miniapp/routers/checkin.py`) звать его отдельно для этих трёх источников больше не нужно.

    Ревью (D-18, день сессии): раньше отметка на сессии верила присланному `point` вслепую —
    волонтёр со вчерашним/устаревшим списком точек в сканере мог отметить делегата на сессии,
    которая физически идёт в ДРУГОЙ день. `эффективный день` этого вызова — день `scanned_at`,
    если он передан, иначе день `msk_now()` (реальное «сейчас») — ОДНА формула на оба случая:
    у живого скана/ручного поиска (`source="miniapp"|"manual"`, `scanned_at` не передаётся
    ВООБЩЕ, см. `miniapp/routers/checkin.py`) эффективный день = сегодня; у загрузки CSV —
    день самого скана из файла, если он там есть, иначе день ЗАГРУЗКИ файла (тоже `msk_now()`
    на момент разбора) — «файл могут загрузить на следующий день», сверять с сегодня в этом
    случае неверно (нашёл ревью). Несовпадение живого источника — ОТКАЗ (`"wrong_day"`, сканер
    волонтёра прислал устаревшую точку, отметку ставить некуда). Несовпадение у CSV — НЕ отказ
    (выгрузка уже случилась, делегат физически отметился на площадке) — `record_session_checkin`
    всё равно вызывается, `day_mismatch=True` уходит в результат, `handlers/admin_checkin.py`
    показывает это отдельной строкой отчёта, а не режет отметки.

    Первая отметка входа (прямая или `auto_session`) дополнительно зовёт слушателей
    `register_first_entry_listener` (если передан `bot`) и кладёт событие в
    `result["first_entry"]` — Mini App без Bot переносит его в outbox сам.

    Идея №31/№32 (журнал площадки): живая отметка (`source` не "csv") со статусом new/moved
    пишет строку `venue_log` (`services.venue_log.log_live_checkin`, fail-soft), её id —
    `result["log_id"]`, ключ кнопки «↩️ Отменить» на плашке сканера. `staff_name` — снимок
    имени волонтёра для журнала."""
    if not (point or "").startswith("session:"):
        status, ts = await record_checkin(
            user["telegram_id"], point or ENTRY_POINT, source=source,
            scanned_at=scanned_at, approx=approx, by_staff_id=by_staff_id,
        )
        await mark_arrived_in_sheet(user["telegram_id"], status, ts)
        result = {"status": status, "scanned_at": ts}
        if status == "new" and source != "csv":
            result["log_id"] = await _venue_log().log_live_checkin(
                user, point or ENTRY_POINT, status=status, scanned_at=ts, source=source,
                by_staff_id=by_staff_id, staff_name=staff_name,
            )
        if status == "new" and (point or ENTRY_POINT) == ENTRY_POINT:
            await _after_first_entry(result, bot, _first_entry_event(user, ts, source, by_staff_id, approx))
        return result

    try:
        session_id = int(point.split(":", 1)[1])
    except (ValueError, IndexError):
        return {"status": "invalid_point"}
    session = await get_program_session(session_id)
    if session is None:
        return {"status": "invalid_point"}

    import cities as _cities  # ленивый импорт — тот же приём, что services/program.py делает для msk_now

    delegate_city = _cities.normalize_city(user.get("event_city"))
    if session["city"] != delegate_city:
        delegate_label = await _cities.city_label(delegate_city)
        session_label = await _cities.city_label(session["city"])
        return {
            "status": "wrong_city",
            "reason_text": f"Делегат с форума в {delegate_label}, эта сессия — {session_label}",
        }

    from services.timeutil import msk_now  # ленивый импорт — см. докстринг record_arrival

    effective_day = scanned_at[:10] if scanned_at else msk_now().strftime("%Y-%m-%d")
    day_mismatch = effective_day != session["day"]
    if day_mismatch and source != "csv":
        day_part = session["day"][8:10]
        month_part = session["day"][5:7]
        return {
            "status": "wrong_day",
            "reason_text": (
                f"Эта сессия не сегодня ({day_part}.{month_part}) — "
                "обновите список точек в сканере"
            ),
        }

    from services.program import parallel_group  # ленивый импорт — избегаем цикла на верхнем уровне

    day_sessions = await list_program_sessions_for_city_day(session["city"], session["day"])
    slot = parallel_group(session, day_sessions)
    slot_other_ids = [s["id"] for s in slot if s["id"] != session_id]

    previous_row: dict = {}
    status, ts, previous_id = await record_session_checkin(
        user["telegram_id"], session_id, slot_other_ids, source=source,
        scanned_at=scanned_at, approx=approx, by_staff_id=by_staff_id,
        previous_out=previous_row,
    )
    result: dict = {"status": status, "scanned_at": ts}
    if day_mismatch:  # только source == "csv" мог дойти досюда с day_mismatch=True
        result["day_mismatch"] = True
    if status == "moved" and previous_id is not None:
        prev = await get_program_session(previous_id)
        result["previous_title"] = prev["title"] if prev else None
    if status in ("new", "moved"):
        # Вход каждый день: авто-вход ставится на день и время САМОГО скана сессии (у CSV —
        # время из файла, а не день загрузки).
        entry_status, entry_ts = await record_checkin(
            user["telegram_id"], ENTRY_POINT, source="auto_session", by_staff_id=by_staff_id,
            scanned_at=ts, approx=approx,
        )
        await mark_arrived_in_sheet(user["telegram_id"], entry_status, entry_ts)
        if source != "csv":
            result["log_id"] = await _venue_log().log_live_checkin(
                user, point, status=status, scanned_at=ts, source=source,
                by_staff_id=by_staff_id, staff_name=staff_name,
                previous=previous_row or None,
                auto_entry_at=entry_ts if entry_status == "new" else None,
            )
        if entry_status == "new":
            await _after_first_entry(result, bot, _first_entry_event(
                user, entry_ts, "auto_session", by_staff_id, False, session_id=session_id,
            ))
    return result


def _venue_log():
    """Ленивый импорт журнала площадки (`services.venue_log` сам ничего не импортирует отсюда,
    но держим верх модуля без новых зависимостей — тот же приём, что `cities`/`program`)."""
    from services import venue_log
    return venue_log


# ── Разбор выгрузки офлайн-сканера (D-09/D-10) ───────────────────────────────────────────────
#
# «Под любое приложение»: бот не полагается на конкретную структуру колонок конкретного
# сканера — ищет строки/ячейки, начинающиеся с текущей метки события + разделителя, ЛЮБЫМ из
# двух независимых способов (обычный CSV/TSV-разбор ловит ячейку целиком; сырой regex-скан по
# всему тексту ловит тот же код внутри JSON/произвольного текста, где csv.reader его бы
# токенизировал неверно) — и объединяет находки без дублей. Доступ к БД (токен делегата, запись
# отметки, счётчики) — в `database/db.py`; здесь — только чистая логика плюс `current_event_tag`
# выше. Хендлер бота — `handlers/admin_checkin.py`.

_DECODE_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251")

_ISO_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?")
_DMY_DT_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})[ T](\d{2}):(\d{2})(?::(\d{2}))?$")
_EPOCH_RE = re.compile(r"^\d{9,13}$")


def decode_scan_export(data: bytes) -> str:
    """Байты выгрузки -> текст. Перебирает кодировки по очереди (utf-8 с BOM/без, cp1251 —
    частая для файлов, выгруженных на русской Windows); последний вариант заменяет
    нераспознанные байты, а не падает — лучше кривая пара символов, чем отказ разобрать файл
    целиком."""
    for enc in _DECODE_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _parse_cell_datetime(cell: str) -> datetime | None:
    cell = (cell or "").strip()
    if not cell:
        return None
    if _ISO_DT_RE.match(cell):
        # Полная строка первой: `Z`/`+03:00` — это зона, её нельзя отрезать (`cell[:19]`
        # превращал «09:15Z» в «09:15 по Москве»). Aware -> МСК naive; naive — как есть
        # (время телефона сканера на площадке).
        full = cell[:-1] + "+00:00" if cell[-1:] in ("Z", "z") else cell
        try:
            return aware_to_msk(datetime.fromisoformat(full))
        except ValueError:
            try:
                return datetime.fromisoformat(cell[:19])
            except ValueError:
                return None
    m = _DMY_DT_RE.match(cell)
    if m:
        d, mo, y, h, mi, s = m.groups()
        try:
            return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
        except ValueError:
            return None
    if _EPOCH_RE.match(cell):
        try:
            n = int(cell)
            if n > 10 ** 12:  # миллисекунды
                n //= 1000
            return msk_from_timestamp(n)
        except (ValueError, OSError, OverflowError):
            return None
    return None


def _sniff_dialect(sample: str):
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return None


def find_checkin_records(text: str, tag: str) -> list[dict]:
    """Каждая находка кода нашего события в `text` (CSV/TSV/TXT/JSON — любой экспорт
    приложения-сканера): `{"qr": <строка QR>, "scanned_at": "YYYY-MM-DD HH:MM:SS" | None}`.
    `scanned_at` — время из СОСЕДНЕЙ ячейки той же строки CSV, если она похожа на дату-время
    (ISO / «дд.мм.гггг чч:мм[:сс]» / unix-эпоха секунды-или-миллисекунды); `None` — время скана
    в файле не нашлось, вызывающий (`handlers/admin_checkin.py`) подставляет время загрузки с
    флагом «примерное» (D-10). Без дублей — один и тот же QR-текст входит в результат один раз,
    даже если обе стратегии его поймали."""
    if not tag or not text:
        return []
    prefix = f"{tag}{_QR_SEP}"
    records: list[dict] = []
    seen: set[str] = set()

    # Стратегия 1: настоящий CSV/TSV-разбор (уважает кавычки/встроенные разделители) —
    # позволяет заодно посмотреть на СОСЕДНИЕ ячейки той же строки в поисках времени скана.
    sample = text[:4096]
    dialect = _sniff_dialect(sample)
    try:
        reader = csv.reader(io.StringIO(text), dialect) if dialect else csv.reader(io.StringIO(text))
        for row in reader:
            cells = [(c or "").strip() for c in row]
            for idx, cell in enumerate(cells):
                if not cell.startswith(prefix) or cell in seen:
                    continue
                seen.add(cell)
                scanned_at = None
                for other_idx, other in enumerate(cells):
                    if other_idx == idx:
                        continue
                    dt = _parse_cell_datetime(other)
                    if dt:
                        scanned_at = dt.strftime("%Y-%m-%d %H:%M:%S")
                        break
                records.append({"qr": cell, "scanned_at": scanned_at})
    except csv.Error:
        pass

    # Стратегия 2: сырой regex-скан всего текста — ловит код там, где построчный CSV-разбор
    # его не токенизировал (JSON-экспорт вроде Binary Eye, произвольный текст). Без контекста
    # строки — время скана здесь всегда None (примерное). Обрезаем ТОЛЬКО по типичным
    # разделителям колонок/JSON/переносам строк — не по пробелу: поле ФИО внутри самого QR
    # (D-04, «Иванов Иван») законно содержит пробел, и обрезка по \s откусила бы фамилию от
    # имени, оставляя в `seen` не ту строку, что нашла стратегия 1 (дубль вместо дедупа).
    pattern = re.compile(re.escape(prefix) + r'[^,;\t\r\n"\'\]}]*')
    for m in pattern.finditer(text):
        cell = m.group(0)
        if cell not in seen:
            seen.add(cell)
            records.append({"qr": cell, "scanned_at": None})

    return records
