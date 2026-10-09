"""Подписи кнопок главного меню делегата — из настроек.

Каждая кнопка меню подписана значением своей настройки (`MENU_LABEL_KEYS`, per_city): своё
у города, иначе общее, иначе дефолт реестра. Пустое значение = дефолт. Поэтому хендлер не
может стоять на `F.text == "..."`: фильтр `MenuButton` узнаёт кнопку по ЛЮБОЙ подписи, под
которой она может прийти от делегата, —

- дефолт реестра и его английская версия (`i18n_ui_en.MENU_EN`);
- конференционные подписи «Информации»/«Программы» (`event_type == "conference"`);
- старые подписи, которые висят на закэшированных клавиатурах (`LEGACY_MENU_TEXTS`);
- текущее значение настройки — общее и по каждому городу — и его английский перевод.

Статичные варианты (первые три) сверяются без чтения БД. Настроенные — одним снимком
`bot_settings` и одной картой переводов на сообщение: все фильтры меню одного апдейта делят
результат (`_UPDATE_CACHE`), а не ходят в БД каждый сам.

Модуль не знает о гейтах видимости (тумблер, одобрение) — их делает `keyboards/builders`.
Импортов handlers нет.
"""
import contextvars
import logging

from aiogram.filters import BaseFilter
from aiogram.types import Message

from cities import city_codes, get_setting_typed_for_city, per_city_key
from database.db import get_setting, settings_snapshot
from i18n_ui_en import MENU_EN
from services.i18n import load_map, tr
from settings_schema import SETTINGS_SCHEMA

logger = logging.getLogger(__name__)

# Ключ кнопки меню -> ключ настройки с её подписью. Порядок — порядок кнопок в меню.
# Запись на сессии и тест получили настраиваемую подпись раньше остальных — их ключи
# остались прежними (у менеджеров уже настроены). «💳 Оплата» — не пункт MENU_BUTTONS
# (тумблера у неё нет, её показывает долг по чеку), но подпись настраивается так же.
MENU_LABEL_KEYS: dict[str, str] = {
    "menu_referral": "menu_referral_label",
    "menu_invites": "menu_invites_label",
    "menu_info": "menu_info_label",
    "menu_program": "menu_program_label",
    "menu_session_enroll": "session_enroll_menu_label",
    "menu_quiz": "quiz_menu_label",
    "menu_speakers": "menu_speakers_label",
    "menu_contacts": "menu_contacts_label",
    "menu_question": "menu_question_label",
    "menu_faq": "menu_faq_label",
    "menu_coins": "menu_coins_label",
    "menu_game_tasks": "menu_game_tasks_label",
    "menu_miniapp": "menu_miniapp_label",
    "menu_lang": "menu_lang_label",
    "menu_checkin_qr": "menu_checkin_qr_label",
    "menu_important": "menu_important_label",
    "menu_sos": "menu_sos_label",
    "menu_edit_anketa": "menu_edit_anketa_label",
    "menu_payment": "menu_payment_label",
}
# Прежнее имя (до того, как настраиваемыми стали все кнопки).
DYNAMIC_MENU_LABEL_KEYS = MENU_LABEL_KEYS
MENU_LABEL_SETTING_KEYS: frozenset[str] = frozenset(MENU_LABEL_KEYS.values())
# (ключ, подпись, подсказка) — строки экрана «✏️ Подписи кнопок меню» в чате, в порядке меню.
MENU_LABEL_FIELDS: list[tuple[str, str, str]] = [
    (k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in MENU_LABEL_KEYS.values()
]

# Национальная конференция (съезд АЙСЕК) — не форум: при event_type == "conference" и пустой
# (дефолтной) подписи две кнопки называются иначе. Своя подпись менеджера побеждает.
CONFERENCE_MENU_LABELS: dict[str, str] = {
    "menu_info": "ℹ️ О конференции",
    "menu_program": "📅 Программа конференции",
}

# D-29 объединил «🗓 Программа» (menu_schedule) с «📅 Программа форума». У делегатов с
# закэшированной старой клавиатурой кнопка осталась — её подписи (RU+EN) ведут в тот же экран.
LEGACY_MENU_TEXTS: dict[str, frozenset[str]] = {
    "menu_program": frozenset({"🗓 Программа", "🗓 Schedule"}),
}


def default_caption(menu_key: str) -> str:
    """Подпись по умолчанию (дефолт реестра)."""
    return str(SETTINGS_SCHEMA[MENU_LABEL_KEYS[menu_key]].get("default") or "")


def _with_en(text: str) -> set[str]:
    return {text, MENU_EN.get(text, text)}


def _static_texts(menu_key: str) -> frozenset[str]:
    found = _with_en(default_caption(menu_key))
    if menu_key in CONFERENCE_MENU_LABELS:
        found |= _with_en(CONFERENCE_MENU_LABELS[menu_key])
    found |= LEGACY_MENU_TEXTS.get(menu_key, frozenset())
    return frozenset(found)


# Подписи, известные без чтения БД: дефолт + EN, конференционные, старые.
STATIC_MENU_TEXTS: dict[str, frozenset[str]] = {mk: _static_texts(mk) for mk in MENU_LABEL_KEYS}
_STATIC_TEXT_TO_KEY: dict[str, str] = {}
for _mk, _texts in STATIC_MENU_TEXTS.items():
    for _t in _texts:
        _STATIC_TEXT_TO_KEY.setdefault(_t, _mk)


def _en_caption(caption: str, tr_map: dict[str, str]) -> str:
    """Английская версия подписи: рукописная (`MENU_EN`), иначе машинный перевод из корпуса,
    иначе сама подпись — пустой кнопки не бывает."""
    if caption in MENU_EN:
        return MENU_EN[caption]
    return tr(caption, "en", tr_map) or caption


async def caption_for(menu_key: str, city: str | None, lang: str = "ru", *,
                      conference: bool = False, tr_map: dict[str, str] | None = None) -> str:
    """Текущая подпись кнопки для города: своя, иначе общая, иначе дефолт (на конференции —
    конференционный дефолт); для EN — перевод. Никогда не пустая строка."""
    label_key = MENU_LABEL_KEYS[menu_key]
    default = default_caption(menu_key)
    try:
        value = await get_setting_typed_for_city(label_key, city)
    except Exception as e:
        logger.warning("caption_for(%s, %s) не прочитана: %s", menu_key, city, e)
        value = None
    caption = (str(value).strip() if value else "") or default
    if conference and caption == default and menu_key in CONFERENCE_MENU_LABELS:
        caption = CONFERENCE_MENU_LABELS[menu_key]
    if lang == "en":
        if tr_map is None:
            try:
                tr_map = await load_map("en")
            except Exception as e:
                logger.warning("карта EN-переводов не прочитана: %s", e)
                tr_map = {}
        caption = _en_caption(caption, tr_map)
    return caption


async def _configured_text_map() -> dict[str, str]:
    """Подпись -> ключ кнопки для НАСТРОЕННЫХ значений (общих и по каждому городу) и их
    английских версий. Один снимок настроек и одна карта переводов."""
    found: dict[str, str] = {}
    try:
        tr_map = await load_map("en")
    except Exception as e:
        logger.warning("карта EN-переводов не прочитана: %s", e)
        tr_map = {}
    async with settings_snapshot():
        for menu_key, label_key in MENU_LABEL_KEYS.items():
            raw_values = [await get_setting(label_key) or ""]
            for code in city_codes():
                composed = per_city_key(label_key, code)
                if composed:
                    raw_values.append(await get_setting(composed) or "")
            for raw in raw_values:
                raw = raw.strip()
                if not raw:
                    continue
                # Первая найденная пара побеждает: совпадение с подписью другой кнопки
                # не даёт сохранить проверка ввода (settings_validation).
                found.setdefault(raw, menu_key)
                found.setdefault(_en_caption(raw, tr_map), menu_key)
    return found


# (сообщение, карта) последнего апдейта: фильтры меню одного апдейта идут в одном
# asyncio-контексте и делят одну выборку. Сверка по `is` — другое сообщение = новая выборка.
_UPDATE_CACHE: contextvars.ContextVar[tuple[object, dict[str, str]] | None] = \
    contextvars.ContextVar("menu_label_update_cache", default=None)


async def menu_key_for_text(text: str | None, message: object | None = None) -> str | None:
    """Какая кнопка меню подписана этим текстом (любой город, любой язык, старая подпись)?
    None — не кнопка меню. Fail-soft: сбой чтения настроек = узнаём только статичные."""
    if not text:
        return None
    static = _STATIC_TEXT_TO_KEY.get(text)
    if static is not None:
        return static
    cached = _UPDATE_CACHE.get() if message is not None else None
    if cached is not None and cached[0] is message:
        return cached[1].get(text)
    try:
        configured = await _configured_text_map()
    except Exception as e:
        logger.warning("подписи кнопок меню не прочитаны: %s", e)
        return None
    if message is not None:
        _UPDATE_CACHE.set((message, configured))
    return configured.get(text)


async def all_dynamic_captions(menu_key: str | None = None) -> frozenset[str]:
    """Все подписи, под которыми кнопка (или любая кнопка меню) может прийти от делегата."""
    keys = [menu_key] if menu_key else list(MENU_LABEL_KEYS)
    found: set[str] = set()
    for mk in keys:
        found |= STATIC_MENU_TEXTS[mk]
    try:
        configured = await _configured_text_map()
    except Exception as e:
        logger.warning("подписи кнопок меню не прочитаны: %s", e)
        configured = {}
    found |= {t for t, mk in configured.items() if mk in keys}
    return frozenset(found)


async def is_dynamic_menu_text(text: str) -> bool:
    """Это подпись кнопки меню (любой кнопки, города, языка)? Fail-soft: False."""
    try:
        return await menu_key_for_text(text) is not None
    except Exception as e:
        logger.warning("is_dynamic_menu_text не отработал: %s", e)
        return False


class MenuButton(BaseFilter):
    """Сообщение — тап по кнопке меню `menu_key` под любой её актуальной подписью."""

    def __init__(self, menu_key: str):
        if menu_key not in MENU_LABEL_KEYS:
            raise KeyError(f"неизвестная кнопка меню: {menu_key}")
        self.menu_key = menu_key

    async def __call__(self, message: Message) -> bool:
        text = getattr(message, "text", None)
        if not text:
            return False
        try:
            return await menu_key_for_text(text, message) == self.menu_key
        except Exception as e:
            logger.warning("MenuButton(%s) не отработал: %s", self.menu_key, e)
            return False


# Прежнее имя фильтра (запись на сессии, тест).
DynamicMenuText = MenuButton
