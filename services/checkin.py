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
from typing import TypedDict

import segno

from database.db import (
    SHEET_ARRIVAL_SET,
    enqueue_sheet_arrival,
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
from domain.regform.engine import is_past_season_row  # D-02: пропуск на форум не выдаём возвращенцу
from services.timeutil import aware_to_msk, msk_from_timestamp
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

_QR_SEP = "·"

# Phase 12 (FORUM-CHECKIN.md, D-08/D-18): точка входа «Вход» — тот же `point`, что
# `database.db.record_checkin`/`count_checkins_by_point` считают по умолчанию. Сессии
# программы (будущая фаза) получат свои point-код/слаг, вход остаётся отдельной
# константой — она уже размечена кнопкой в `handlers/admin_checkin.py`.
ENTRY_POINT = "entry"
ENTRY_POINT_LABEL = "🚪 Вход"

_DEFAULT_CAPTION = (
    "🎟 Твой QR для отметки на входе.\n\n"
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


def foreign_qr_code(qr_payload: str) -> str:
    """Код отказа для QR с чужой/пустой меткой события: `"foreign_event"` — строка в НАШЕМ
    формате пропуска (метка·ФИО·город·токен), но метка другого мероприятия/сезона;
    `"not_our_qr"` — это вообще не пропуск (случайный QR, ссылка, мусор)."""
    parts = (qr_payload or "").split(_QR_SEP)
    if len(parts) == 4 and parts[0].strip() and parts[-1].strip():
        return "foreign_event"
    return "not_our_qr"


async def foreign_qr_reason(code: str) -> str:
    """Текст плашки для `foreign_qr_code`. «Не пропуск» называет мероприятие, если менеджер
    задал название в родительном падеже (`event_name_genitive`, «форума Юлид»): «Это не
    QR-пропуск форума Юлид». Не задано — без названия (подставлять именительный после
    «пропуск» нельзя — падеж сломается)."""
    if code != "not_our_qr":
        return DENIAL_REASON_TEXT.get(code, code)
    from domain.settings.schema import get_setting_typed

    try:
        genitive = (await get_setting_typed("event_name_genitive") or "").strip()
    except Exception:
        genitive = ""
    if not genitive:
        return DENIAL_REASON_TEXT["not_our_qr"]
    return f"Это не QR-пропуск {genitive} — попросите открыть «🎟 Мой QR» в боте"


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
    # D-41 (ревью 28.09): отдельный код сканера для status='rejected' — `checkin_denial` его НЕ
    # отдаёт (там и отказ, и «на рассмотрении» — `not_approved`), уточняет сканер
    # (`services.onsite_reg.refine_denial`): волонтёр обязан видеть, что заявку ОТКЛОНИЛИ.
    "rejected": "Заявка отклонена менеджером",
    "past_season": "Делегат прошлого сезона",
    "foreign_event": "QR другого мероприятия",
    # Приёмка 01.10: строка вообще не в формате пропуска (случайный QR, ссылка, текст) — не
    # «другое мероприятие»: волонтёр иначе решит, что человек пришёл не туда.
    "not_our_qr": "Это не QR-пропуск — попросите открыть «🎟 Мой QR» в боте",
    # Форум-ночь B1 (идея №10): менеджер перевыпустил QR (handlers/admin.py::cmd_find_user ->
    # checkin_reissue_yes) — этот код УЖЕ не откроет вход, даже если делегат ещё не успел
    # открыть новый (database.db.get_checkin_token_replacement).
    "token_replaced": "QR заменён — попросите делегата открыть новый в «🎟 Мой QR»",
    # 25.09: токен узнал внешний резолвер (`register_token_resolver`), но отметку для такого
    # вида пропуска записывать пока некому (гостевые QR — будущая фича).
    "unknown_pass_kind": "Неизвестный тип пропуска — отправьте на стойку проблемных случаев",
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


# ── Точка расширения «токен QR -> человек» (25.09, под будущие гостевые пропуска) ────────────

# Код отказа «резолвер узнал токен, но записывать отметку для такого вида пропуска некому»
# (см. `resolve_scanned_user`). Подписи — `DENIAL_REASON_TEXT` выше,
# `services.venue_log.DENIAL_LABELS`, `handlers.admin_checkin._DENIAL_LABELS`.
UNKNOWN_PASS_KIND = "unknown_pass_kind"


class Resolved(TypedDict, total=False):
    """Что резолвер отдаёт про человека за токеном (см. `register_token_resolver`).

    - `kind` — вид пропуска: "delegate" (строка `users`) или иной ("guest" и т.п.);
    - `id` — идентификатор человека в источнике резолвера (у делегата — `telegram_id`);
    - `telegram_id` — Telegram ID, если он есть (у гостя может быть None);
    - `city` — город форума в том виде, как хранит источник (у делегата — сырой
      `users.event_city`; нормализует вызывающий через `cities.normalize_city`);
    - `full_name` — отображаемое имя для плашки сканера/журнала;
    - `denial` — статус допуска: None = пропускать, иначе код причины отказа (ключ
      `DENIAL_REASON_TEXT`/`venue_log.DENIAL_LABELS`);
    - `user` — строка `users` (только у "delegate": её ждут `record_arrival` и сканер);
      резолвер иного вида кладёт сюда свою запись или None.
    """

    kind: str
    id: object
    telegram_id: int | None
    city: str | None
    full_name: str | None
    denial: str | None
    user: dict | None


async def _users_token_resolver(token: str, **ctx) -> Resolved | None:
    """Встроенный резолвер делегатов: `get_user_by_checkin_token` + `checkin_denial`, плюс
    форум-ночь B1 (идея №10, перевыпуск QR): если токена нет в `users` (чужой QR или СВОЙ, но
    уже перевыпущенный), сверяемся с `checkin_token_replacements` — старый QR получает свою
    причину `'token_replaced'`, а не общий `'no_user'`, чтобы волонтёр понял: делегат
    СУЩЕСТВУЕТ, просто открыл старый скриншот вместо нового «🎟 Мой QR». Ни то ни другое —
    None («не мой токен», очередь следующих резолверов)."""
    user = await get_user_by_checkin_token(token)
    if user is None:
        if await get_checkin_token_replacement(token) is not None:
            return Resolved(kind="delegate", id=None, telegram_id=None, city=None,
                            full_name=None, denial="token_replaced", user=None)
        return None
    return Resolved(
        kind="delegate", id=user.get("telegram_id"), telegram_id=user.get("telegram_id"),
        city=user.get("event_city"), full_name=user.get("full_name"),
        denial=await checkin_denial(user), user=user,
    )


_token_resolvers: list = [_users_token_resolver]


def register_token_resolver(fn) -> None:
    """Подписать резолвер «токен из QR -> человек» (для будущих гостевых QR и т.п.). Сигнатура:

        async def resolver(token: str, **ctx) -> Resolved | dict | None

    Вернуть описание человека (поля — `Resolved`: минимум `kind`, `id`/`telegram_id`, `city`,
    `full_name`, `denial`) или None = «не мой токен». Резолверы опрашиваются по порядку
    регистрации, первый не-None побеждает; встроенный резолвер делегатов
    (`_users_token_resolver`) всегда ПЕРВЫЙ — чужой резолвер не может перехватить токен
    делегата. В `ctx` сейчас приходят `point` и `source` ("miniapp" — скан, "csv" — загрузка
    выгрузки); резолвер обязан принимать `**ctx` — поля могут добавляться. Повторная
    регистрация той же функции — no-op.

    Fail-soft: исключение ВНЕШНЕГО резолвера логируется и считается None (переход к
    следующему). Встроенный резолвер делегатов не глушится: сбой БД там — сбой скана, а не
    «QR не найден» (так было до реестра).

    Процессы: реестр — модульный, встроенный резолвер есть в любом процессе, импортировавшем
    `services.checkin` (бот — `handlers/admin_checkin.py`, Mini App — `miniapp/routers/checkin.py`).
    Внешний резолвер регистрировать в ОБОИХ процессах: скан QR идёт в Mini App
    (`/app/api/checkin/scan`), загрузка CSV — в боте. Удобно звать `register_token_resolver` на
    уровне модуля фичи и импортировать этот модуль из `main.py` и `miniapp/app.py`.

    Запись отметки для kind != "delegate" пока НЕ реализована: `resolve_scanned_user` отдаёт
    такому токену отказ `UNKNOWN_PASS_KIND` («неизвестный тип пропуска», пишется в журнал
    площадки). Будущий код гостей подключает свою отметку ТАМ — в `resolve_scanned_user`
    (ветка `kind != "delegate"`) и у её вызывающих (`miniapp/routers/checkin.py::checkin_scan`,
    `handlers/admin_checkin.py` — разбор CSV), которые сейчас передают дальше в
    `record_arrival` только строку `users`."""
    if fn not in _token_resolvers:
        _token_resolvers.append(fn)


def clear_token_resolvers() -> None:
    """Снять все внешние резолверы (для тестов); встроенный резолвер делегатов остаётся."""
    _token_resolvers[:] = [_users_token_resolver]


async def resolve_token(token: str | None, **ctx) -> Resolved | None:
    """Опросить резолверы по порядку, первый не-None побеждает (см. `register_token_resolver`).
    Пустой токен — None без опроса."""
    if not token:
        return None
    for fn in list(_token_resolvers):
        if fn is _users_token_resolver:
            resolved = await fn(token, **ctx)
        else:
            try:
                resolved = await fn(token, **ctx)
            except Exception:
                logger.exception("token resolver %r failed", fn)
                continue
        if resolved is not None:
            return resolved
    return None


async def resolve_scanned_user(token: str | None, **ctx) -> tuple[dict | None, str | None]:
    """Единая точка «токен из QR -> (делегат, код отказа)» для ОБОИХ вызывающих
    (`miniapp/routers/checkin.py`, `handlers/admin_checkin.py`) поверх реестра резолверов
    `resolve_token`. Делегат — `(строка users, checkin_denial(...))`, перевыпущенный QR —
    `(None, 'token_replaced')`, никто не узнал — `(None, 'no_user')`. Токен узнал резолвер
    иного вида (`kind != "delegate"`), а записи отметки для него пока нет —
    `(None, UNKNOWN_PASS_KIND)`: здесь подключит свою ветку будущий код гостей."""
    resolved = await resolve_token(token, **ctx)
    if resolved is None:
        return None, await checkin_denial(None)
    if resolved.get("kind") != "delegate":
        return None, UNKNOWN_PASS_KIND
    return resolved.get("user"), resolved.get("denial")


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


async def mark_arrived_in_sheet(
    telegram_id: int, status: str, scanned_at: str, *, city: str | None = None,
) -> None:
    """Форум-ночь B2 (идея №17): отметка входа -> колонка «Пришёл» Google-таблицы. Зовётся
    внутри `record_arrival` после `database.db.record_checkin` — для ВСЕХ источников (сканер
    Mini App, ручной поиск, CSV, авто-вход от сессии).

    Нагрузочный прогон 25.09: сам лист здесь НЕ трогается — только событие в
    `sheet_arrival_queue`, запись делает джоба бота пачками (`services/sheet_arrival_sync.py`).
    Раньше ячейка писалась синхронно: из Mini App (отдельный процесс без Google-кредов) — никогда,
    а в боте каждый первый скан до ответа волонтёру читал весь столбец id листа. Этот модуль
    и `miniapp/` не импортируют Google-листы вовсе (сторож tests/test_sheet_arrival_queue_260925.py).

    Событие — только на `'new'` и только если этот вход — самый ранний из имеющихся («Пришёл» —
    время ПЕРВОГО входа за форум; вход второго дня ячейку не меняет). Значение ячейки джоба всё
    равно берёт из базы, фильтр лишь не плодит пустых событий."""
    if status != "new":
        return
    first = await first_entry_scanned_at(telegram_id)
    if first is not None and first < scanned_at:
        return
    await enqueue_sheet_arrival(telegram_id, SHEET_ARRIVAL_SET, city)


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
    import domain.cities as _cities  # ленивый импорт — тот же приём, что в record_arrival

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
        await mark_arrived_in_sheet(user["telegram_id"], status, ts, city=user.get("event_city"))
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

    import domain.cities as _cities  # ленивый импорт — тот же приём, что services/program.py делает для msk_now

    delegate_city = _cities.normalize_city(user.get("event_city"))
    if session["city"] != delegate_city:
        delegate_label = await _cities.city_label(delegate_city)
        session_label = await _cities.city_label(session["city"])
        return {
            "status": "wrong_city",
            "reason_text": f"Делегат другого города: {delegate_label}. Эта сессия — {session_label}",
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
        await mark_arrived_in_sheet(
            user["telegram_id"], entry_status, entry_ts, city=user.get("event_city"),
        )
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
# «Под любое приложение»: бот не полагается на структуру колонок конкретного сканера. Код
# нашего события ищется регэкспом по каждой строке файла (CSV/TSV/TXT/JSON одинаково), токен —
# хвост `·[A-Za-z0-9_-]+` (ФИО внутри QR может содержать запятую или пробел — некавыченный CSV
# режет такую ячейку, регэксп нет). Время скана — из ячеек той же строки: дата-время одной
# ячейкой в любом из частых форматов или дата и время соседними колонками; если строка не
# разбирается выбранным разделителем, пробуются остальные («;» вместо «,» и наоборот). Дубли
# сводятся по ТОКЕНУ и дню скана: повтор того же делегата в тот же день — одна запись (с самым
# ранним временем), запись без времени поглощается записью с временем. Доступ к БД — в
# `database/db.py`; хендлер бота — `handlers/admin_checkin.py`.

_DECODE_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251")

_ISO_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?")
# дата «дд.мм.гггг», «м/д/гггг» или «гггг/мм/дд» (год может быть двузначным: «03.10.26»), между
# датой и временем — пробел, «T» или «, »; в конце — необязательные AM/PM (американская локаль).
_DATE_PART = r"(\d{1,2})([./])(\d{1,2})\2(\d{4}|\d{2})|(\d{4})[/.](\d{1,2})[/.](\d{1,2})"
_TIME_PART = r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,]\d+)?\s*(?:([AaPp])\.?[Mm]\.?)?"
_DMY_DT_RE = re.compile(rf"^(?:{_DATE_PART})\s*(?:,\s*|\s+|T)\s*(?:{_TIME_PART})$")
_DATE_ONLY_RE = re.compile(rf"^(?:{_DATE_PART}|(\d{{4}})-(\d{{2}})-(\d{{2}}))$")
_TIME_ONLY_RE = re.compile(rf"^{_TIME_PART}$")
_EPOCH_RE = re.compile(r"^\d{9,13}$")
_TOKEN_TAIL = r"[A-Za-z0-9_-]+"


def decode_scan_export(data: bytes) -> str:
    """Байты выгрузки -> текст. UTF-16 (экспорт «Unicode text» из Excel/iPhone) узнаётся по
    BOM или по нулевым байтам через один; дальше кодировки по очереди (utf-8 с BOM/без, cp1251 —
    частая для файлов, выгруженных на русской Windows); последний вариант заменяет
    нераспознанные байты, а не падает — лучше кривая пара символов, чем отказ разобрать файл
    целиком."""
    utf16 = None
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        utf16 = "utf-16"
    elif len(data) >= 4:
        head = data[:4096]
        even_zero = head[0::2].count(0)
        odd_zero = head[1::2].count(0)
        half = len(head) // 2
        if odd_zero > half * 0.3 and even_zero < half * 0.05:
            utf16 = "utf-16-le"
        elif even_zero > half * 0.3 and odd_zero < half * 0.05:
            utf16 = "utf-16-be"
    if utf16:
        return data.decode(utf16, errors="replace")
    for enc in _DECODE_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _hour24(hour: int, ampm: str | None) -> int:
    if not ampm:
        return hour
    if ampm in ("A", "a"):
        return 0 if hour == 12 else hour
    return hour if hour == 12 else hour + 12


def _date_from_groups(g: tuple, has_ampm: bool) -> tuple[int, int, int]:
    """(год, месяц, день) из групп `_DATE_PART`. «дд.мм.гггг» — день первым; «a/b/гггг» —
    месяц первым при AM/PM (американская локаль) или если второе число больше 12, иначе день
    первым (европейская «дд/мм/гггг»)."""
    a, sep, b, y, y2, m2, d2 = g[:7]
    if y2:
        return int(y2), int(m2), int(d2)
    a, b = int(a), int(b)
    year = int(y) if len(y) == 4 else 2000 + int(y)  # «03.10.26» — двузначный год
    if sep == "/" and (has_ampm or b > 12) and a <= 12:
        return year, a, b
    return year, b, a


def _parse_cell_datetime(cell: str) -> datetime | None:
    return _parse_cell_datetime_ex(cell)[0]


def _parse_cell_datetime_ex(cell: str) -> tuple[datetime | None, bool]:
    """Время ячейки + признак «наивное» (без зоны: время телефона сканера на площадке — местное
    время города). Со смещением/`Z` и unix-эпоха однозначны и уже переведены в МСК -> `False`."""
    cell = (cell or "").strip().strip('"').strip()
    if not cell:
        return None, False
    iso_comma = re.match(r"^(\d{4}-\d{2}-\d{2}),\s*(\d{1,2}:\d{2}(?::\d{2})?)$", cell)
    if iso_comma:
        cell = f"{iso_comma.group(1)} {iso_comma.group(2).zfill(5)}"
    if _ISO_DT_RE.match(cell):
        # Полная строка первой: `Z`/`+03:00` — это зона, её нельзя отрезать (`cell[:19]`
        # превращал «09:15Z» в «09:15 по Москве»). Aware -> МСК naive; naive — как есть
        # (время телефона сканера на площадке).
        full = cell[:-1] + "+00:00" if cell[-1:] in ("Z", "z") else cell
        try:
            parsed = datetime.fromisoformat(full)
            return aware_to_msk(parsed), parsed.tzinfo is None
        except ValueError:
            try:
                return datetime.fromisoformat(cell[:19]), True
            except ValueError:
                return None, False
    m = _DMY_DT_RE.match(cell)
    if m:
        g = m.groups()
        h, mi, sec, ampm = g[7:11]
        try:
            return datetime(*_date_from_groups(g, bool(ampm)), _hour24(int(h), ampm), int(mi), int(sec or 0)), True
        except (ValueError, TypeError):
            return None, False
    if _EPOCH_RE.match(cell):
        try:
            n = int(cell)
            if n > 10 ** 12:  # миллисекунды
                n //= 1000
            dt = msk_from_timestamp(n)
        except (ValueError, OSError, OverflowError):
            return None, False
        # номер телефона или id тоже бывают 9–13 цифрами — время только правдоподобное
        return (dt, False) if 2020 <= dt.year <= 2100 else (None, False)
    return None, False


def _parse_cell_date(cell: str) -> tuple[int, int, int] | None:
    m = _DATE_ONLY_RE.match((cell or "").strip().strip('"').strip())
    if not m:
        return None
    g = m.groups()
    if g[7]:
        return int(g[7]), int(g[8]), int(g[9])
    return _date_from_groups(g, False)


def _parse_cell_time(cell: str) -> tuple[int, int, int] | None:
    m = _TIME_ONLY_RE.match((cell or "").strip().strip('"').strip())
    if not m:
        return None
    h, mi, sec, ampm = m.groups()
    return _hour24(int(h), ampm), int(mi), int(sec or 0)


_SLASH_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?!\d)")


def _swapped_reading(text: str, stamp: str) -> str | None:
    """Вторая читка неоднозначной даты «a/b/гггг» (оба числа ≤ 12 и не равны): «03/10/2026»
    бывает и 3 октября (д/м, RU/EU), и 10 марта (м/д, US; с AM/PM разбор выбирает его). `stamp`
    («YYYY-MM-DD…») — уже выбранная читка; возвращает ту же строку с переставленными месяцем и
    днём, если в строке файла есть такая неоднозначная дата. Какую читку взять, решает загрузка
    по окну форума/дню сессии (`services.checkin_csv_import`)."""
    y, m, d = int(stamp[:4]), int(stamp[5:7]), int(stamp[8:10])
    for match in _SLASH_DATE_RE.finditer(text):
        a, b = int(match.group(1)), int(match.group(2))
        if a <= 12 and b <= 12 and a != b and {a, b} == {m, d}:
            return f"{y:04d}-{d:02d}-{m:02d}{stamp[10:]}"
    return None


def _row_date(cells: list[str]) -> str | None:
    """День скана из строки, где есть только дата без времени («YYYY-MM-DD»). Год — не дальше
    года от текущего: случайное «1.2.34» в соседней колонке не становится датой."""
    from services.timeutil import msk_now
    this_year = msk_now().year
    for cell in cells:
        ymd = _parse_cell_date(cell)
        if not ymd or abs(ymd[0] - this_year) > 1:
            continue
        try:
            return datetime(*ymd).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _row_datetime(cells: list[str]) -> datetime | None:
    return _row_datetime_ex(cells)[0]


def _row_datetime_ex(cells: list[str]) -> tuple[datetime | None, bool]:
    """Время скана из ячеек строки: дата-время одной ячейкой или дата + время соседними;
    второй элемент — «наивное» (местное время телефона), см. `_parse_cell_datetime_ex`."""
    for cell in cells:
        dt, naive = _parse_cell_datetime_ex(cell)
        if dt:
            return dt, naive
    for idx, cell in enumerate(cells):
        ymd = _parse_cell_date(cell)
        if not ymd:
            continue
        for other in cells[idx + 1:idx + 3] + cells[max(0, idx - 2):idx]:
            hms = _parse_cell_time(other)
            if hms:
                try:
                    return datetime(*ymd, *hms), True
                except ValueError:
                    return None, False
    return None, False


def _sniff_dialect(sample: str):
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return None


def _line_cells(line: str, delimiter: str) -> list[str]:
    try:
        row = next(csv.reader([line], delimiter=delimiter), [])
    except csv.Error:
        row = line.split(delimiter)
    return [(c or "").strip() for c in row]


def _qr_pattern(tag: str) -> re.Pattern:
    """Код события в произвольном тексте: метка, до двух полей (ФИО, город — внутри может быть
    запятая и пробел, но не перенос строки, кавычка, «;» или таб и не начало следующего кода) и
    токен последним полем."""
    prefix = re.escape(f"{tag}{_QR_SEP}")
    field = rf"(?:(?!{prefix})[^{_QR_SEP}\r\n\t;\"])*{_QR_SEP}"
    return re.compile(rf"{prefix}(?:{field}){{0,2}}({_TOKEN_TAIL})")


def find_checkin_records(text: str, tag: str) -> list[dict]:
    """Каждый делегат, найденный в `text` (CSV/TSV/TXT/JSON — любой экспорт
    приложения-сканера): `{"qr": <строка QR>, "scanned_at": "YYYY-MM-DD HH:MM:SS" | None}`.
    `scanned_at` — время из ячеек той же строки файла (ISO / «дд.мм.гг[гг][,] чч:мм[:сс]» /
    «м/д/гггг чч:мм AM» / дата и время отдельными колонками / unix-эпоха); `None` — время
    скана в файле не нашлось. Если в строке есть хотя бы дата — она в `"day"` («YYYY-MM-DD»);
    неоднозначная «a/b/гггг» даёт ещё `"alt"` — ту же отметку с переставленными днём и месяцем;
    день записи без даты и времени выбирает загрузка (`services.checkin_csv_import`), отметка
    помечается «примерной» (D-10). Дубли сводятся по токену: одна запись на делегата и
    день скана (самое раннее время дня), запись без времени поглощается записью с временем."""
    if not tag or not text:
        return []
    pattern = _qr_pattern(tag)
    dialect = _sniff_dialect(text[:4096])
    delimiters = [dialect.delimiter] if dialect else []
    delimiters += [d for d in (",", ";", "\t") if d not in delimiters]

    found: list[tuple[str, str, str | None]] = []  # (qr, token, scanned_at) в порядке файла
    naive_stamps: set[str] = set()  # метки из ячеек без зоны — местное время телефона
    for line in text.splitlines():
        matches = list(pattern.finditer(line))
        if not matches:
            continue
        scanned_at = day = None
        if len(matches) == 1:  # несколько кодов в строке (JSON одной строкой) — время не угадать
            rest = line.replace(matches[0].group(0), "")
            for delim in delimiters:
                dt, naive = _row_datetime_ex(_line_cells(rest, delim))
                if dt:
                    scanned_at = dt.strftime("%Y-%m-%d %H:%M:%S")
                    if naive:
                        naive_stamps.add(scanned_at)
                    break
            if scanned_at is None:
                for delim in delimiters:
                    day = _row_date(_line_cells(rest, delim))
                    if day:
                        break
        alt = _swapped_reading(line, scanned_at or day) if (scanned_at or day) else None
        for m in matches:
            found.append((m.group(0), m.group(1), scanned_at, day, alt))

    timed_tokens = {token for _qr, token, scanned_at, _day, _alt in found if scanned_at}
    records: list[dict] = []
    by_key: dict[tuple[str, str | None], dict] = {}
    for qr, token, scanned_at, day, alt in found:
        if scanned_at is None and token in timed_tokens:
            continue
        key = (token, scanned_at[:10] if scanned_at else day)
        rec = by_key.get(key)
        if rec is None:
            rec = {"qr": qr, "scanned_at": scanned_at}
            if day:
                rec["day"] = day
            if alt:
                rec["alt"] = alt  # вторая читка «a/b/гггг» — см. _swapped_reading
            by_key[key] = rec
            records.append(rec)
        elif scanned_at and scanned_at < rec["scanned_at"]:
            rec["scanned_at"] = scanned_at
    for rec in records:
        if rec["scanned_at"] in naive_stamps:
            rec["naive"] = True  # время — местное (часы города); в МСК его переводит загрузка
    return records
