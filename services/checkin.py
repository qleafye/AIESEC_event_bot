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

from database.db import get_checkin_token_replacement, get_or_create_checkin_token, get_user_by_checkin_token
from reg_engine import is_past_season_row  # D-02: пропуск на форум не выдаём возвращенцу
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
    Сам вызов уже fail-soft (update_arrived_in_sheet ловит исключения и возвращает False) —
    отметка в БД к этому моменту уже сохранена вызывающим, лист может упасть без последствий
    для самого чек-ина (D-17)."""
    if status != "new":
        return
    from services.sheets import update_arrived_in_sheet
    await update_arrived_in_sheet(telegram_id, scanned_at)


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
        try:
            return datetime.fromisoformat(cell[:19].replace("T", " ") if "T" not in cell[:19] else cell[:19])
        except ValueError:
            try:
                return datetime.fromisoformat(cell.rstrip("Zz")[:19])
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
            return datetime.fromtimestamp(n)
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
