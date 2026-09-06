"""Phase 27 (27-02, LANG-01/02) — ядро перевода делегатской анкеты: контент-адресация,
резолюция языка делегата и одна точка `tr()`, которую воронки вывода (план 27-04 — Mini App,
план 27-05 — бот) зовут над уже готовым текстом.

Зависимости: `hashlib`, `logging`, `database.db`, `i18n_ui_en`, `settings_schema`. Из
`handlers` этот модуль НЕ импортирует ничего (aiogram-free, как `services/i18n_sources.py`);
`reg_engine` тоже не импортирует и не импортируется им — перевод врезается в воронки вывода
делегатского текста, а не в ядро анкеты (27-CONTEXT.md, A-03): `reg_engine`, резолвящий
~43 шага формы, продолжает возвращать байт-в-байт то же самое при выключенном и включённом
модуле — golden-снимки (`tests/test_reg_engine_parity.py`,
`tests/test_refac_snapshot_260816.py`) эту фазу не видят вообще.

Два яруса (A-02): ярус A — рукописный английский `i18n_ui_en.UI_EN` (служебные слова, тексты
ошибок валидации — литералы, участвующие в жёстких сравнениях aiogram). Ярус B — машинный
перевод контента менеджера, лежит в `database.db.translations`, ключ — `src_hash` РЕЗУЛЬТАТА
резолюции (не ключ реестра bot_settings): городская/трековая/дефолтная лестница уже схлопнута
в одну реально показанную строку к моменту, когда до неё доходит `tr()`.
"""
import hashlib
import logging

from database import db
from i18n_ui_en import UI_EN
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)


def src_hash(text: str) -> str:
    """Контент-адрес строки — sha256 от текста БЕЗ обрамляющих пробелов, усечённый до 32 hex-
    символов (128 бит — коллизия при масштабе ~300 делегатских строк не рассматривается).
    Правка русского исходника (даже на один символ после strip) даёт другой хеш — старая
    строка в `translations` становится недостижимой без отдельной инвалидации (LANG-03)."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


def tr(text, lang: str, tr_map: dict[str, str]):
    """Единая точка перевода — вызывается НАД уже резолвленным текстом (после того как
    городская/трековая/дефолтная лестница реестра отработала), не подменяет резолюцию.

    Тождественный возврат при `lang == "ru"` — ЖЁСТКОЕ требование, не стилистика:
    `tests/test_miniapp_labels_drift.py` сверяет `is`-тождество объектов подписей Mini App, а
    `tests/test_reg_engine_parity.py`/`tests/test_refac_snapshot_260816.py` фиксируют русский
    текст байт-в-байт. Поэтому при `lang == "ru"` (и на пустом/`None` тексте — fail-soft на
    любом языке) функция отдаёт ТОТ ЖЕ объект `text`, а не копию и не `str(text)`.

    Порядок резолюции при `lang != "ru"`:
    1. Ярус A (`i18n_ui_en.UI_EN`) — если строка там есть, побеждает ВСЕГДА, даже если для
       неё уже есть (устаревшая или просто отличающаяся) запись в `tr_map`: ярус A рукописный
       и участвует в сравнениях фильтров, машинный перевод не должен иметь шанса его перебить.
    2. `tr_map` (карта `src_hash -> text`, план 27-02 `database.db.fetch_translations`,
       загруженная ОДИН раз за запрос через `load_map` ниже) — по хешу текущего текста.
    3. Fail-soft: ни яруса A, ни перевода в карте нет -> возвращается русский `text` как есть
       (делегат никогда не видит дыру вместо текста, D-04)."""
    if not text or lang == "ru":
        return text
    layer_a = UI_EN.get(text)
    if layer_a is not None:
        return layer_a
    return tr_map.get(src_hash(text)) or text


async def load_map(lang: str) -> dict[str, str]:
    """Карта `src_hash -> text` для языка — ОДНА выборка на запрос делегата, не N SELECT-ов
    на каждый переводимый текст (`form_spec()` резолвит ~43 шага по 1-4 чтения настроек
    каждый — поход в БД на каждый текст удвоил бы чтения на рендер анкеты).

    `lang == "ru"` -> пустой словарь и НОЛЬ чтений (`tr()` для русского и так возвращает текст
    как есть, поход в БД был бы чистой тратой). Модуль-глобальный кеш намеренно НЕ заводится:
    процессов, которые могут дёрнуть это, два (бот и `miniapp`) — понадобился бы ревизионный
    счётчик синхронизации между ними, то есть целый класс багов инвалидации ради экономии
    одного локального SELECT на запрос."""
    if lang == "ru":
        return {}
    return await db.fetch_translations(lang)


def resolve_lang(module_on: bool, stored: str | None, language_code: str | None) -> str:
    """Чистая функция — лестница резолюции языка делегата (27-RESEARCH.md, план 27-04 её
    вызывает на `/start` и в главном меню):

    1. Модуль выключен -> `"ru"` безусловно, даже если у делегата уже есть сохранённый `"en"`
       (A-05: выключенный модуль не меняет поведение бота ни на байт).
    2. Есть сохранённый выбор (`users.lang` — `"ru"` или `"en"`) -> он и побеждает; делегат
       явно выбирал, клиентский `language_code` тут уже не имеет значения.
    3. `language_code` клиента Telegram начинается с `"ru"` -> `"ru"` (не надо спрашивать
       человека, чей клиент и так по-русски).
    4. Иначе -> `"ask"` — отдельный ТРЕТИЙ исход, не молчаливое `"en"`: язык клиента Telegram
       не равен языку человека (часть российских делегатов держит клиент на английском),
       молча переключить анкету на английский было бы регрессом. Вызывающий показывает выбор
       кнопками (D-06, «бот для людей» — ничего не угадываем, спрашиваем)."""
    if not module_on:
        return "ru"
    if stored in ("ru", "en"):
        return stored
    if language_code and language_code.startswith("ru"):
        return "ru"
    return "ask"


async def delegate_lang(telegram_id: int, language_code: str | None = None) -> str:
    """Обёртка над `resolve_lang` для реального делегата — сама читает тумблер модуля и
    сохранённый язык. Fail-soft по всей цепочке (D-04): любое исключение (нет пользователя,
    сбой БД) -> `"ru"` и `logger.error`, делегат никогда не видит ошибку из-за языка.

    Может вернуть `"ask"` (см. `resolve_lang`) — это НЕ ошибка, это сигнал вызывающему
    показать экран выбора языка."""
    try:
        module_on = await get_setting_typed("delegate_lang_enabled") == "on"
        user = await db.get_user(telegram_id)
        stored = user.get("lang") if user else None
        return resolve_lang(module_on, stored, language_code)
    except Exception:  # noqa: BLE001 — намеренно широкий fail-soft (D-04)
        logger.error("delegate_lang: сбой резолюции языка для %s", telegram_id, exc_info=True)
        return "ru"


async def context(telegram_id: int, language_code: str | None = None) -> tuple[str, dict]:
    """`(lang, tr_map)` одним вызовом — то, что зовут воронки вывода делегатского текста
    (план 27-04 — Mini App, план 27-05 — бот) перед тем, как прогнать резолвленные тексты
    формы через `tr()`. Если `delegate_lang` вернул `"ask"` (язык ещё не выбран), карта
    переводов всё равно грузится для `stored`-независимого случая — вызывающий сам решает,
    показывать ли экран выбора вместо анкеты; `tr_map` пустая для нерешённого/русского языка,
    т.к. `load_map` не читает БД при `lang == "ru"`."""
    lang = await delegate_lang(telegram_id, language_code)
    tr_map = await load_map(lang if lang in ("ru", "en") else "ru")
    return lang, tr_map
