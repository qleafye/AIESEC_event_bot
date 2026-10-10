"""Подписи кнопок главного меню делегата — данные и правила узнавания (без aiogram).

Каждая кнопка подписана значением своей настройки (`MENU_LABEL_KEYS`, per_city): своё у
города, иначе общее, иначе дефолт реестра. Пустое значение = дефолт. Фильтр хендлеров
(`keyboards/menu_dynamic.MenuButton`) узнаёт кнопку по подписи С УЧЁТОМ ГОРОДА делегата:

1. текущее значение его города, потом общее (своё у города побеждает);
2. стандартные подписи: дефолт, его рукописный английский вариант (`i18n_ui_en.MENU_EN`),
   конференционные, старые с закэшированных клавиатур (`LEGACY_MENU_TEXTS`);
3. прежние значения тех же настроек (`menu_label_history`, последние `HISTORY_DEPTH`) — после
   переименования старая клавиатура у делегатов продолжает работать.

Английские варианты своих подписей — только ручные переводы менеджера (`manual=1`) и
`MENU_EN`; машинный перевод может смениться сам и оставить кнопку мёртвой.

Данные кэшируются в памяти процесса до следующей записи настройки или перевода
(`database.db.write_generation`); правка из приложения доходит через `on_setting_written`
(`settings_audit.run_setting_hooks`), плюс страховочный срок `_TTL_SECONDS`.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from cities import city_codes, get_setting_typed_for_city, per_city_key, split_per_city_key
from database import db
from i18n_ui_en import MENU_EN
from services.i18n import src_hash
from domain.settings.schema import SETTINGS_SCHEMA

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
MENU_LABEL_SETTING_KEYS: frozenset[str] = frozenset(MENU_LABEL_KEYS.values())
_MENU_KEY_OF_LABEL: dict[str, str] = {lk: mk for mk, lk in MENU_LABEL_KEYS.items()}

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
    # 10.10: дефолт «🪙 Мои монеты» стал «🪙 Мои баллы» — старая подпись с закэшированных
    # клавиатур ведёт туда же.
    "menu_coins": frozenset({"🪙 Мои монеты", "🪙 My coins"}),
}

# Служебный ключ bot_settings (не в реестре, менеджер его не правит): прежние подписи по
# каждому ключу настройки (общему и городскому), новые первыми.
HISTORY_KEY = "menu_label_history"
HISTORY_DEPTH = 3
_TTL_SECONDS = 300


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
STATIC_TEXT_TO_KEY: dict[str, str] = {}
for _mk, _texts in STATIC_MENU_TEXTS.items():
    for _t in _texts:
        STATIC_TEXT_TO_KEY.setdefault(_t, _mk)


def base_label_key(key: str) -> str | None:
    """Базовый ключ подписи для `key` (общего или городского), иначе None."""
    parsed = split_per_city_key(key)
    base = parsed[0] if parsed else key
    return base if base in MENU_LABEL_SETTING_KEYS else None


# ── кэш ─────────────────────────────────────────────────────────────────────────────────────

@dataclass
class LabelData:
    values: dict[str, str] = field(default_factory=dict)          # ключ настройки -> подпись
    history: dict[str, list[str]] = field(default_factory=dict)   # ключ настройки -> прежние


_cache: dict = {"data": None, "en": None}


def invalidate() -> None:
    _cache["data"] = None
    _cache["en"] = None


def _fresh(entry) -> bool:
    if entry is None:
        return False
    stamp, gen, path, _value = entry
    from config import config
    return (gen == db.write_generation() and path == config.DB_PATH
            and time.monotonic() - stamp < _TTL_SECONDS)


def _stamp(value):
    from config import config
    return (time.monotonic(), db.write_generation(), config.DB_PATH, value)


def _setting_keys() -> list[str]:
    keys: list[str] = []
    codes = city_codes()
    for lk in MENU_LABEL_KEYS.values():
        keys.append(lk)
        keys.extend(k for k in (per_city_key(lk, c) for c in codes) if k)
    return keys


def _parse_history(raw: str | None) -> dict[str, list[str]]:
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {k: [str(v) for v in vs][: HISTORY_DEPTH + 1] for k, vs in parsed.items() if isinstance(vs, list)}


async def label_data() -> LabelData:
    """Текущие подписи (общие и по городам) и прежние — один снимок настроек на кэш."""
    if _fresh(_cache["data"]):
        return _cache["data"][3]
    data = LabelData()
    async with db.settings_snapshot():
        for key in _setting_keys():
            value = (await db.get_setting(key) or "").strip()
            if value:
                data.values[key] = value
        data.history = _parse_history(await db.get_setting(HISTORY_KEY))
    _cache["data"] = _stamp(data)
    return data


async def manual_en_map() -> dict[str, str]:
    """Ручные английские переводы (`src_hash -> text`) — только для английских делегатов."""
    if _fresh(_cache["en"]):
        return _cache["en"][3]
    try:
        en = await db.fetch_manual_translations("en")
    except Exception as e:
        logger.warning("ручные EN-переводы не прочитаны: %s", e)
        en = {}
    _cache["en"] = _stamp(en)
    return en


def _en_of(text: str, en_map: dict[str, str] | None) -> str | None:
    if text in MENU_EN:
        return MENU_EN[text]
    if en_map:
        return en_map.get(src_hash(text))
    return None


# ── подпись на клавиатуре ───────────────────────────────────────────────────────────────

async def caption_for(menu_key: str, city: str | None, lang: str = "ru", *,
                      conference: bool = False, en_map: dict[str, str] | None = None) -> str:
    """Текущая подпись кнопки для города: своя, иначе общая, иначе дефолт (на конференции —
    конференционный дефолт); для EN — рукописный или ручной перевод, иначе сама подпись."""
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
        if en_map is None:
            en_map = await manual_en_map()
        caption = _en_of(caption, en_map) or caption
    return caption


# ── узнавание нажатия ───────────────────────────────────────────────────────────────────

def _scope_keys(label_key: str, city: str | None) -> list[str]:
    """Ключи настройки, видимые делегату города `city`: своё у города, потом общее."""
    own = per_city_key(label_key, city) if city else None
    return [own, label_key] if own else [label_key]


def resolve(data: LabelData, text: str, city: str | None,
            en_map: dict[str, str] | None = None) -> str | None:
    """Ключ кнопки, подписанной `text` для делегата города `city` (None — только общие)."""
    def variants(value: str) -> set[str]:
        found = {value}
        en = _en_of(value, en_map)
        if en:
            found.add(en)
        return found

    for scope in (0, 1):
        for mk, lk in MENU_LABEL_KEYS.items():
            keys = _scope_keys(lk, city)
            if scope >= len(keys):
                continue
            value = data.values.get(keys[scope])
            if value and text in variants(value):
                return mk
    if text in STATIC_TEXT_TO_KEY:
        return STATIC_TEXT_TO_KEY[text]
    for mk, lk in MENU_LABEL_KEYS.items():
        for key in _scope_keys(lk, city):
            if any(text in variants(old) for old in data.history.get(key, ())):
                return mk
    return None


def all_texts(data: LabelData, menu_key: str | None = None) -> frozenset[str]:
    """Все подписи (любой город, прежние, стандартные; RU + рукописный EN) — для сторожей
    ввода и быстрого «это точно не кнопка»."""
    keys = [menu_key] if menu_key else list(MENU_LABEL_KEYS)
    found: set[str] = set()
    for mk in keys:
        found |= STATIC_MENU_TEXTS[mk]
    for key, value in data.values.items():
        base = base_label_key(key)
        if base and _MENU_KEY_OF_LABEL[base] in keys:
            found |= _with_en(value)
    for key, olds in data.history.items():
        base = base_label_key(key)
        if base and _MENU_KEY_OF_LABEL[base] in keys:
            for old in olds:
                found |= _with_en(old)
    return frozenset(found)


# ── проверка ввода и история ────────────────────────────────────────────────────────────

async def label_owner_conflict(key: str, value: str) -> str | None:
    """Ключ ДРУГОЙ кнопки, у которой уже есть подпись `value` — стандартная или настроенная
    (общая или любого города). None — подпись свободна. Межгородские совпадения тоже
    запрещены: делегат, переехавший в другой город, иначе попал бы не в ту кнопку."""
    base = base_label_key(key)
    text = (value or "").strip()
    if not base or not text or text == "-":
        return None
    own = _MENU_KEY_OF_LABEL[base]
    for mk, texts in STATIC_MENU_TEXTS.items():
        if mk != own and text in texts:
            return mk
    data = await label_data()
    for setting_key, current in data.values.items():
        other = base_label_key(setting_key)
        if other and other != base and text in _with_en(current):
            return _MENU_KEY_OF_LABEL[other]
    return None


def conflict_text(owner_menu_key: str) -> str:
    return (
        f"Такая подпись уже у кнопки «{default_caption(owner_menu_key)}» — выберите другую, "
        "иначе нажатия перепутаются. Например <code>📅 Мои сессии</code>.\n\n"
        "Пришлите ещё раз или «-», чтобы вернуть подпись по умолчанию."
    )


async def label_conflict_text(key: str, value: str) -> str | None:
    owner = await label_owner_conflict(key, value)
    return conflict_text(owner) if owner else None


async def on_setting_written(key: str) -> None:
    """Хук записи настройки: новая подпись — в начало истории ключа, кэш сброшен."""
    if key == HISTORY_KEY:
        invalidate()
        return
    if not base_label_key(key):
        return
    new = (await db.get_setting(key) or "").strip()
    history = _parse_history(await db.get_setting(HISTORY_KEY))
    olds = [v for v in history.get(key, []) if v != new]
    if new:
        olds.insert(0, new)
    history[key] = olds[: HISTORY_DEPTH + 1]
    await db.set_setting(HISTORY_KEY, json.dumps(history, ensure_ascii=False))
    invalidate()
