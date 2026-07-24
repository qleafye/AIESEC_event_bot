"""SETTINGS_SCHEMA registry (REG-01/REG-02/REG-03, Phase 6 plan 06-01).

Single source of truth for `bot_settings` key metadata (type/group/label/prompt/default)
that both the admin UI and downstream consumers read from — the whole point of this
module is to remove the "coordination tax" of editing 4 scattered files (SETTINGS_FIELDS,
SETTINGS_GROUPS, PHOTO_FIELDS/FILE_FIELDS, and each ad-hoc consumer's own parse) every
time a setting is added or changed. Adding/editing a setting should require editing only
one entry in SETTINGS_SCHEMA.

Design (D-01..D-08, see .planning/phases/06-settings-schema-registry/06-CONTEXT.md):
- D-01: lives in its own top-level module — imports ONLY `database.db.get_setting`
  (one-directional dependency, no import of handlers.* — avoids import cycles).
- D-02: plain dict-by-key (`SETTINGS_SCHEMA = {key: {...}}`) — O(1) lookup, no dataclass
  (project convention: domain entities are plain dicts, see CONVENTIONS.md).
- D-03/D-04: type-driven parse dispatch. `type` taxonomy: toggle/int/list/date/text/enum/
  photo/file. An optional per-entry `"parse"` callable overrides the type-branch for rare
  special cases (exception, not the rule).
- D-05/D-08: `get_setting_typed(key)` is a thin async accessor — raw read via the existing
  `get_setting` (unchanged, D-07) then dispatch through the PURE sync `_parse_setting(key,
  raw)` helper, which is the unit-test surface (no DB, no async).

This plan (06-01) populates ONLY the `event` group (D-11 pilot: 10 text/enum keys + the
photo/file keys rendered on the event sub-screen) — later waves add reg/pay/party/consent/
toggle groups. Unmigrated keys simply are not present in SETTINGS_SCHEMA yet; `_parse_setting`
fails-soft (returns raw unchanged) for any key it doesn't recognize, so coexistence with the
legacy literal tables in handlers/admin.py holds throughout the incremental migration.
"""
from datetime import datetime

from database.db import get_setting

# REG-01: event-group entries — labels/prompts copied byte-for-byte from the pre-migration
# literal SETTINGS_FIELDS/PHOTO_FIELDS/FILE_FIELDS tables (handlers/admin.py) so the render
# snapshot test proves zero drift. Order of the text/enum keys matches the pre-migration
# SETTINGS_GROUPS "event" row (handlers/admin.py:398-401) — later consumers rely on this
# insertion order being preserved (dict iteration order) when filtering by group/type.
SETTINGS_SCHEMA = {
    "event_date": {
        "type": "text", "group": "event", "label": "🗓 Дата",
        "prompt": "Введите дату форума", "default": None,
    },
    "event_time": {
        "type": "text", "group": "event", "label": "⌚ Время",
        "prompt": "Введите время проведения", "default": None,
    },
    "event_place_name": {
        "type": "text", "group": "event", "label": "📍 Место",
        "prompt": "Введите название площадки", "default": None,
    },
    "event_place_address": {
        "type": "text", "group": "event", "label": "📫 Адрес",
        "prompt": "Введите адрес площадки", "default": None,
    },
    "contact_person": {
        "type": "text", "group": "event", "label": "👤 Контакт",
        "prompt": "Введите юзернейм контактного лица (например @username)", "default": None,
    },
    "contact_vk": {
        "type": "text", "group": "event", "label": "🔵 VK",
        "prompt": "Введите ссылку на группу ВК", "default": None,
    },
    "contact_tg": {
        "type": "text", "group": "event", "label": "🔹 TG",
        "prompt": "Введите ссылку на Telegram-канал", "default": None,
    },
    "start_text": {
        "type": "text", "group": "event", "label": "💬 Приветствие",
        "prompt": "Введите текст приветствия при /start (поддерживается HTML-разметка)",
        "default": None,
    },
    "event_name": {
        "type": "text", "group": "event", "label": "🎪 Название меро",
        "prompt": (
            "Название мероприятия в родительном падеже — подставляется в вопрос об "
            "ожиданиях (например: «конференции RusCo», «форума YouLead», «Годового отчёта»)"
        ),
        "default": None,
    },
    "event_type": {
        "type": "enum", "group": "event", "label": "🎭 Тип события",
        "options": ["forum", "conference", "custom"],
        "prompt": (
            "Напишите одно слово: forum (форум) / conference (конференция) / custom "
            "(вручную).\n\nДля forum и conference бот сам включит/выключит модули оплаты "
            "и согласий — потом можно поправить кнопками выше."
        ),
        "default": None,
    },
    # Photo/file entries rendered on the event sub-screen (handlers/admin.py PHOTO_FIELDS/
    # FILE_FIELDS). Registry key is the derived-key PREFIX, not the actual bot_settings row
    # (f"{prefix}_photo_file_id" / f"{prefix}_doc_file_id") — the upload-flow and lookup
    # mechanics stay special-cased outside the registry (D-10). Metadata only here.
    "program": {
        "type": "photo", "group": "event", "label": "📅 Программа",
        "prompt": "Отправьте фото программы (можно с подписью).", "default": None,
    },
    "speakers": {
        "type": "photo", "group": "event", "label": "🗣 Спикеры",
        "prompt": "Отправьте одно фото со всеми спикерами (можно с подписью).", "default": None,
    },
    "start": {
        "type": "photo", "group": "event", "label": "💬 Фото приветствия",
        "prompt": "Отправьте фото для приветственного сообщения (/start).", "default": None,
    },
    "venue": {
        "type": "photo", "group": "event", "label": "🏢 Площадка",
        "prompt": "Отправьте фото площадки (можно с подписью).", "default": None,
    },
    "reg_bonus": {
        "type": "file", "group": "event", "label": "🎁 Бонус за регистрацию",
        "prompt": "Отправьте файл или фото бонуса (можно с подписью).", "default": None,
    },
}


def _parse_setting(key, raw):
    """Pure sync parse dispatch (D-03/D-08) — no DB, no async, the unit-test surface.

    Looks up `SETTINGS_SCHEMA[key]` for `type`/`default` and dispatches by type. If `key`
    is not (yet) registered, returns `raw` unchanged — fail-soft, so coexistence with
    unmigrated keys (still read via raw `get_setting` elsewhere) holds during the
    incremental migration.
    """
    entry = SETTINGS_SCHEMA.get(key)
    if entry is None:
        return raw

    # Optional per-entry override (D-03) — rare exception, not the rule.
    override = entry.get("parse")
    if override is not None:
        return override(raw)

    entry_type = entry["type"]
    default = entry.get("default")

    if entry_type == "text":
        # text default is None; the render layer's own truthiness check handles empty
        # display — only a genuinely missing (None) raw falls back to default.
        return raw if raw is not None else default

    if entry_type == "enum":
        # CRITICAL byte-for-byte contract (D-15): falsy -> default. Both None AND
        # empty-string resolve to default — this matches the live `get_setting(k) or
        # "<default>"` idiom every feature-switch consumer relies on. Do NOT use
        # `is not None` here — it would diverge from the live idiom on empty-string.
        return raw if raw else default

    if entry_type in ("photo", "file"):
        # Passthrough raw file_id (D-10); None falls back to default (None).
        return raw if raw is not None else default

    if entry_type == "int":
        # Lifted verbatim from services/scheduler.py::_int_or_default /
        # services/reminders.py::_reminder_interval — positive int or default;
        # None/empty/garbage/<=0 -> default.
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    if entry_type == "date":
        # Lifted verbatim from services/scheduler.py::_parse_schedule_dt — admin datetime
        # 'ДД.ММ.ГГГГ ЧЧ:ММ'; bad input -> default (None for unset date entries).
        try:
            return datetime.strptime(raw.strip(), "%d.%m.%Y %H:%M")
        except (TypeError, ValueError, AttributeError):
            return default

    if entry_type == "list":
        # Lifted verbatim from handlers/registration.py::_get_options (splitlines/strip),
        # extended to also accept the `;` inline separator already used elsewhere in the
        # project for the Telegram "Enter = send" mobile trap.
        if raw:
            items = [
                segment.strip()
                for line in raw.splitlines()
                for segment in line.split(";")
                if segment.strip()
            ]
            if items:
                return items
        return list(default) if default else []

    if entry_type == "toggle":
        # Lifted verbatim from handlers/admin.py::_is_question_on (the reg_q question
        # idiom, admin.py:2095-2097) — returns bool.
        return (raw == "on") if raw is not None else (default == "on")

    # Unknown type in the registry itself — fail-soft rather than raise.
    return raw


async def get_setting_typed(key: str):
    """Thin async accessor (D-05) — raw read via the existing `get_setting` (database.db,
    unchanged, D-07) then dispatch through the pure `_parse_setting`. Does not duplicate
    raw I/O; calls `get_setting` exactly once."""
    raw = await get_setting(key)
    return _parse_setting(key, raw)
